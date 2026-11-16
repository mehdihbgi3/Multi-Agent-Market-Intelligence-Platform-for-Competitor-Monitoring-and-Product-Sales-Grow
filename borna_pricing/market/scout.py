from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import timedelta

from .. import db, fmt
from ..clock import iso, now_utc, parse_iso
from ..service import ServiceError
from . import sites as site_mod
from . import textnorm as tn
from .browser import BrowserUnavailable
from .fetcher import FetchBlocked

log = logging.getLogger("borna.market")

BASE_QUERY = "سنسور اکسیژن"
VEHICLE_QUERIES = ("پراید", "پژو ۲۰۶", "پژو ۴۰۵", "پارس", "سمند", "دنا", "تیبا", "رانا", "ساینا", "کوییک", "پیکان",
                   "L90", "EF7", "ام وی ام", "لیفان", "چانگان", "جک", "آریزو", "X22")
ECU_QUERIES = ("زیمنس", "ساژم", "ME17", "بوش", "NTK", "دلفی")
POLICY_NOTE = "به تصمیم مدیریت برنا، منبع‌هایی که کاشف بازار پیدا می‌کند خودکار مجازند؛ مدیر می‌تواند هر کدام را ممنوع کند."
SOURCE_PREFIX = "scout-"
BLOCK_STATUSES = frozenset({401, 403, 429, 490, 503})
OSM_TIMEOUT = 180
OSM_MAX_BYTES = 40_000_000


class SiteBlocked(FetchBlocked):
    pass


def _blocked(site, exc: FetchBlocked) -> SiteBlocked:
    return SiteBlocked(f"{exc} {site.label} درخواست‌های خودکار را محدود کرد یا بست؛ خواندن {site.label} "
                       "تا اجرای بعد متوقف شد.", status=exc.status)


def source_key(site) -> str:
    return SOURCE_PREFIX + site.key


def product_queries(profiles: list[dict], extra=()) -> list[str]:
    qs = [BASE_QUERY] + [f"{BASE_QUERY} {w}" for w in VEHICLE_QUERIES + ECU_QUERIES]
    for p in profiles:
        qs += list(p.get("part_nos") or [])
    qs += list(extra)
    out: list[str] = []
    for q in qs:
        q = re.sub(r"\s+", " ", str(q)).strip()
        if q and q not in out:
            out.append(q)
    return out


def peek(ms, name: str, queries: list[str], n: int) -> list[str]:
    if not queries:
        return []
    with ms.db() as c:
        cur = int(db.meta_get(c, f"scout_cursor:{name}") or 0) % len(queries)
    return [queries[(cur + i) % len(queries)] for i in range(min(n, len(queries)))]


def advance(ms, name: str, done: int, total: int) -> None:
    if done <= 0 or total <= 0:
        return
    with ms.db() as c, db.tx(c):
        cur = int(db.meta_get(c, f"scout_cursor:{name}") or 0)
        db.meta_set(c, f"scout_cursor:{name}", str((cur + done) % total))


def ensure_source(ms, site) -> dict:
    now = iso(now_utc())
    key = source_key(site)
    with ms.db() as c, db.tx(c):
        row = c.execute("SELECT id FROM sources WHERE key=?", (key,)).fetchone()
        if row is None:
            sid = c.execute(
                "INSERT INTO sources(key, name, kind, domain, start_urls, unit, price_level, parser, interval_hours,"
                " delay_seconds, max_pages, policy_status, policy_note, policy_by, policy_at, active, note, created_at,"
                " created_by, updated_at) VALUES (?,?,?,?, '[]', ?,?, 'auto', ?,?, ?, 'approved', ?, 'system', ?, 1, ?,"
                " ?, 'system', ?)",
                (key, site.label, site.kind, site.domain, site.unit, site.price_level, ms.cfg.scout.interval_hours,
                 ms.cfg.scout.delay_seconds, 200, POLICY_NOTE, now, "منبع کاشف بازار", now, now)).lastrowid
            db.audit(c, now, "system", "source_create", {"source": key, "domain": site.domain, "by": "market_scout"})
        else:
            sid = row["id"]
    return ms.get_source(sid)


def upsert_businesses(c, businesses: list, source_label: str, now: str) -> int:
    new = 0
    for b in businesses:
        key = (getattr(b, "key", None) or re.sub(r"\s+", " ", tn.norm(b.name)).strip())[:200]
        if not key:
            continue
        city = (b.city or None) and b.city[:60]
        address = (getattr(b, "address", None) or None) and b.address[:200]
        phone = (getattr(b, "phone", None) or None) and b.phone[:80]
        row = c.execute("SELECT id, sources FROM leads WHERE name_norm=?", (key,)).fetchone()
        if row is None:
            c.execute("INSERT INTO leads(name, name_norm, sources, n_listings, example_url, kind, city, address,"
                      " phone, first_seen_at, last_seen_at) VALUES (?,?,?,0,?,?,?,?,?,?,?)",
                      (b.name.strip()[:120], key, json.dumps([source_label], ensure_ascii=False), b.url, b.kind,
                       city, address, phone, now, now))
            new += 1
        else:
            srcs = sorted(set(json.loads(row["sources"] or "[]")) | {source_label})
            c.execute("UPDATE leads SET sources=?, last_seen_at=?, example_url=COALESCE(example_url, ?),"
                      " city=COALESCE(city, ?), address=COALESCE(address, ?), phone=COALESCE(phone, ?) WHERE id=?",
                      (json.dumps(srcs, ensure_ascii=False), now, b.url, city, address, phone, row["id"]))
    return new


class _Browsers:
    def __init__(self, ms):
        self.ms = ms
        self.browser = None
        self.error: str | None = None

    def get(self):
        if self.browser is None and self.error is None:
            b = self.ms.browser_factory()
            try:
                self.browser = b.__enter__()
            except BrowserUnavailable as exc:
                self.error = str(exc)
            except Exception as exc:
                log.exception("browser start failed")
                self.error = f"مرورگر نامرئی باز نشد: {exc.__class__.__name__}"
        if self.browser is None:
            raise BrowserUnavailable(self.error or "مرورگر نامرئی در دسترس نیست.")
        return self.browser

    def close(self):
        if self.browser is not None:
            try:
                self.browser.__exit__(None, None, None)
            finally:
                self.browser = None


def _open(site, url: str, mode: str, ready, collect, fetcher, browsers: _Browsers, delay: int, parse, state: dict):
    page, parsed, light_err = None, None, None
    if mode in ("light", "auto"):
        try:
            page = fetcher.get(url, site.domain, delay)
            parsed = parse(page.text(), page.url)
        except FetchBlocked as exc:
            if exc.status in BLOCK_STATUSES:
                raise _blocked(site, exc) from None
            if exc.status is not None or mode == "light":
                raise
            light_err = exc
            page = None
        if page is not None and (mode == "light" or parsed.cards > 0):
            return page, parsed, None
    if state.get("no_browser"):
        if page is not None:
            return page, parsed, None
        raise light_err or FetchBlocked(state["no_browser"])
    try:
        rendered = browsers.get().render(url, site.domain, ready=ready, collect=collect, delay=delay)
    except FetchBlocked as exc:
        blocked = exc.status in BLOCK_STATUSES
        if blocked or isinstance(exc, BrowserUnavailable):
            state["no_browser"] = str(exc)
        if page is not None:
            return page, parsed, f"صفحه بدون مرورگر نامرئی چیزی نداشت و مرورگر نامرئی هم نتوانست: {exc}"
        if blocked:
            raise _blocked(site, exc) from None
        if light_err is not None:
            raise FetchBlocked(f"{light_err}؛ مرورگر نامرئی هم نتوانست: {exc}") from None
        raise
    return rendered, parse(rendered.text(), rendered.url), None


def _order_products(ms, src: dict, site, urls: list[str], limit: int) -> list[str]:
    if limit <= 0 or not urls:
        return []
    with ms.db() as c:
        seen = {r["g"]: r["t"] for r in c.execute(
            "SELECT json_extract(attrs, '$.group') AS g, MAX(last_seen_at) AS t FROM external_products"
            " WHERE source_id=? AND json_extract(attrs, '$.group') IS NOT NULL GROUP BY g", (src["id"],))}
    uniq = list(dict.fromkeys(urls))
    uniq.sort(key=lambda u: (seen.get(site.group_of(u)) is not None, seen.get(site.group_of(u)) or ""))
    return uniq[:limit]


def _scout_site(ms, site, src: dict, queries: list[str], fetcher, browsers: _Browsers, user: str, run_id) -> dict:
    cfg = ms.cfg.scout
    delay = max(int(src["delay_seconds"] or 0), cfg.delay_for(site.key))
    st = ms._new_stats(src)
    st["businesses"] = st["new_businesses"] = 0
    st["blocked"] = False
    drops: list[str] = []
    products: list[str] = []
    state: dict = {}
    words = peek(ms, site.key, queries, cfg.queries_for(site.key))
    repair_words = list(cfg.repair_keywords) if site.key == "divar" else []
    repairs = peek(ms, "divar_repair", repair_words, max(1, cfg.queries_for(site.key) // 2)) if repair_words else []
    done_words = done_repairs = 0
    try:
        for q in words:
            for url in site.search_urls(q, cfg.pages_per_query, cfg.divar_region):
                try:
                    page, parsed, note = _open(site, url, site.mode, site.ready, site.collect, fetcher, browsers,
                                               delay, site.parse_search, state)
                except SiteBlocked:
                    raise
                except FetchBlocked as exc:
                    st["errors"].append(f"«{q}»: {exc}")
                    break
                if note:
                    st["errors"].append(f"«{q}»: {note}")
                st["pages"] += 1
                ms._store_page(src, page, parsed.items, [], user, run_id, st, drops, f"«{q}»", check_drop=False)
                products += parsed.product_urls
                if not parsed.cards:
                    break
            done_words += 1
        for url in _order_products(ms, src, site, products, cfg.product_pages):
            try:
                page, parsed, note = _open(site, url, site.product_mode, site.product_ready, None, fetcher, browsers,
                                           delay, site.parse_product, state)
            except SiteBlocked:
                raise
            except FetchBlocked as exc:
                st["errors"].append(f"{url[:80]}: {exc}")
                continue
            if note:
                st["errors"].append(f"{url[:80]}: {note}")
            st["pages"] += 1
            ms._store_page(src, page, parsed.items, [], user, run_id, st, drops, url[:80], check_drop=False)
        for q in repairs:
            for url in site.repair_urls(q, cfg.divar_region):
                try:
                    page, parsed, note = _open(site, url, site.mode, site.ready, None, fetcher, browsers, delay,
                                               site.parse_repairs, state)
                except SiteBlocked:
                    raise
                except FetchBlocked as exc:
                    st["errors"].append(f"«{q}»: {exc}")
                    continue
                if note:
                    st["errors"].append(f"«{q}»: {note}")
                st["pages"] += 1
                n = len(parsed.businesses)
                ms._store_page(src, page, [], [], user, run_id, st, drops, f"«{q}»", check_drop=False,
                               empty_note=f"{fmt.fa(n)} تعمیرگاه پیدا شد." if n else "تعمیرگاهی پیدا نشد.")
                with ms.db() as c, db.tx(c):
                    st["new_businesses"] += upsert_businesses(c, parsed.businesses, site.label, iso(now_utc()))
                st["businesses"] += n
            done_repairs += 1
    except SiteBlocked as exc:
        st["blocked"] = True
        st["errors"].append(str(exc))
    except Exception as exc:
        log.exception("scout %s failed", site.key)
        st["errors"].append(f"خطای داخلی هنگام خواندن {site.label}: {exc.__class__.__name__}")
    advance(ms, site.key, done_words, len(queries))
    advance(ms, "divar_repair", done_repairs, len(repair_words))
    st["repairs"], st["parts"] = st["businesses"], 0
    ms._finish_run(src, st, drops, user, found_other=st["businesses"] > 0)
    return st


def osm_next(ms, src: dict):
    last = src.get("last_success_at")
    return parse_iso(last) + timedelta(days=ms.cfg.scout.osm_days) if last else None


def _osm_get(fetcher, site, urls, query: str, delay: int):
    errors = []
    for base in urls:
        host = urllib.parse.urlsplit(base).hostname or base
        try:
            page = fetcher.get(site.request_url(base, query), host, delay)
            site_mod.overpass_elements(page.text())
            return page
        except (FetchBlocked, ValueError) as exc:
            errors.append(f"{host}: {exc}")
    raise FetchBlocked("؛ ".join(errors) or "نشانی سرویس نقشه در [scout] osm_urls نیست.")


def _scout_osm(ms, site, src: dict, user: str, run_id) -> dict:
    cfg = ms.cfg.scout
    st = ms._new_stats(src)
    st.update(businesses=0, new_businesses=0, repairs=0, parts=0, blocked=False)
    fetcher = ms.fetcher_factory()
    fetcher.timeout = max(fetcher.timeout, OSM_TIMEOUT)
    fetcher.max_bytes = max(fetcher.max_bytes, OSM_MAX_BYTES)
    delay = max(int(src["delay_seconds"] or 0), cfg.delay_seconds)
    try:
        shops = _osm_get(fetcher, site, cfg.osm_urls, site.shops_query(), delay)
        st["pages"] += 1
        try:
            places = _osm_get(fetcher, site, cfg.osm_urls, site.places_query(), delay).text()
            st["pages"] += 1
        except FetchBlocked as exc:
            places = None
            st["errors"].append(f"فهرست شهرها خوانده نشد و شهر فقط از نشانی ثبت‌شده آمد: {exc}")
        parsed = site.parse_map(shops.text(), places)
        from ..sales.service import SUPPLIER_WORDS
        for b in parsed.businesses:
            if b.kind == "seller" and any(w in tn.norm(b.name) for w in SUPPLIER_WORDS):
                b.kind = "supplier"
        st["businesses"] = len(parsed.businesses)
        st["repairs"] = sum(1 for b in parsed.businesses if b.kind == "repair")
        st["parts"] = st["businesses"] - st["repairs"]
        ms._store_page(src, shops, [], [], user, run_id, st, [], site.label, check_drop=False,
                       empty_note=(f"{fmt.fa(st['repairs'])} تعمیرگاه و {fmt.fa(st['parts'])} فروشگاه لوازم یدکی "
                                   f"پیدا شد؛ {fmt.fa(parsed.skipped)} مکان بی‌نام، با نام کلی یا نامرتبط کنار رفت."))
        with ms.db() as c, db.tx(c):
            st["new_businesses"] = upsert_businesses(c, parsed.businesses, site.label, iso(now_utc()))
    except FetchBlocked as exc:
        st["errors"].append(str(exc))
    except Exception as exc:
        log.exception("scout osm failed")
        st["errors"].append(f"خطای داخلی هنگام خواندن {site.label}: {exc.__class__.__name__}")
    ms._finish_run(src, st, [], user, found_other=st["businesses"] > 0)
    return st


def run(ms, user: str = "system", run_id: int | None = None) -> dict:
    cfg = ms.cfg.scout
    if not cfg.enabled:
        raise ServiceError("کاشف بازار در تنظیمات خاموش است ([scout] enabled).")
    if not cfg.sites and not cfg.osm:
        raise ServiceError("هیچ سایتی در [scout] sites ثبت نشده و نقشه ([scout] osm) هم خاموش است.")
    queries = product_queries(ms.profiles(), cfg.extra_keywords)
    fetcher = ms.fetcher_factory()
    browsers = _Browsers(ms)
    keys = ("pages", "items", "observations", "new_listings", "quarantined", "businesses", "new_businesses")
    out = {"sites": {}, **{k: 0 for k in keys}, "errors": [], "n_errors": 0}
    failed = 0
    try:
        for key in cfg.sites:
            site = site_mod.SITES[key]
            src = ensure_source(ms, site)
            if src["policy_status"] == "blocked" or not src["active"]:
                out["sites"][key] = {"skipped": "منبع ممنوع یا غیرفعال است."}
                continue
            st = _scout_site(ms, site, src, queries, fetcher, browsers, user, run_id)
            failed += int(not st["healthy"])
            out["sites"][key] = {k: st.get(k, 0) for k in keys + ("repairs", "parts", "healthy", "blocked")}
            for k in keys:
                out[k] += st[k]
            out["n_errors"] += len(st["errors"])
            out["errors"] += [f"{site.label}: {e}" for e in st["errors"][:12]]
        if cfg.osm:
            site = site_mod.OSM
            src = ensure_source(ms, site)
            nxt = osm_next(ms, src)
            if src["policy_status"] == "blocked" or not src["active"]:
                out["sites"][site.key] = {"skipped": "منبع ممنوع یا غیرفعال است."}
            elif nxt and now_utc() < nxt:
                out["sites"][site.key] = {"skipped": f"نقشه هر {fmt.fa(cfg.osm_days)} روز یک بار خوانده می‌شود؛ "
                                                     f"نوبت بعد: {fmt.jdate(iso(nxt))}"}
            else:
                st = _scout_osm(ms, site, src, user, run_id)
                failed += int(not st["healthy"])
                out["sites"][site.key] = {k: st.get(k, 0) for k in keys + ("repairs", "parts", "healthy", "blocked")}
                for k in keys:
                    out[k] += st.get(k, 0)
                out["n_errors"] += len(st["errors"])
                out["errors"] += [f"{site.label}: {e}" for e in st["errors"][:12]]
    finally:
        browsers.close()
    out["matching"] = ms.match_pending(user)
    if getattr(ms, "sales", None) is not None:
        out["leads"] = ms.sales.refresh_leads()
    active = [k for k, v in out["sites"].items() if "skipped" not in v]
    if not active:
        out["_status"] = "skipped"
        out["_message"] = "همه سایت‌های کاشف بازار ممنوع یا غیرفعال‌اند."
    elif failed == len(active):
        out["_status"] = "failed"
        out["_message"] = "از هیچ سایتی چیزی خوانده نشد؛ خطاها را ببینید."
    elif failed or out["n_errors"]:
        out["_status"] = "partial"
    return out
