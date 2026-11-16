from __future__ import annotations

import json
import threading
import time
import urllib.parse

from .. import db, fmt, fx
from ..clock import iso, now_utc
from . import prices, scout, sites as site_mod
from .fetcher import FetchBlocked

PROBE = "سنسور اکسیژن"
OSM_BOX = "35.68,51.36,35.74,51.44"
OSM_PROBE = (f'[out:json][timeout:30];(node["shop"="car_repair"]({OSM_BOX});node["shop"="car_parts"]({OSM_BOX}););'
             "out tags center 40;")
META_KEY = "site_check"
STATUS = {"ok": "سالم", "empty": "باز شد ولی آگهی نداشت", "blocked": "مسدود شد", "unreachable": "در دسترس نیست",
          "error": "خطا", "off": "خاموش"}
_lock = threading.Lock()


class _Counting(scout._Browsers):
    def __init__(self, ms):
        super().__init__(ms)
        self.used = 0

    def get(self):
        self.used += 1
        return super().get()


def _base(key: str, label: str, url: str | None) -> dict:
    return {"key": key, "label": label, "url": url, "status": "error", "seconds": 0.0, "cards": 0, "items": 0,
            "priced": 0, "products": 0, "browser": False, "http": None, "sample": [], "message": ""}


def check_site(ms, site, query: str, fetcher, browsers: _Counting) -> dict:
    cfg = ms.cfg.scout
    url = site.search_urls(query, 1, cfg.divar_region)[0]
    out = _base(site.key, site.label, url)
    before = browsers.used
    t0 = time.monotonic()
    try:
        page, parsed, note = scout._open(site, url, site.mode, site.ready, site.collect, fetcher, browsers,
                                         cfg.delay_for(site.key), site.parse_search, {})
        out["http"] = page.status
        out["cards"], out["items"] = parsed.cards, len(parsed.items)
        out["priced"] = sum(1 for i in parsed.items if prices.parse_price(i.price or "", site.unit).kind == "price")
        out["sample"] = [{"title": (i.title or "")[:90], "price": i.price, "seller": i.seller} for i in parsed.items[:3]]
        out["products"] = len(parsed.product_urls)
        if not parsed.items and parsed.product_urls:
            out["sample"] = [{"title": u[:90], "price": None, "seller": None} for u in parsed.product_urls[:3]]
        found = bool(parsed.items or parsed.product_urls)
        out["status"] = "ok" if found else "empty"
        if note:
            out["message"] = note
        elif not found:
            out["message"] = "صفحه باز شد ولی آگهی‌ای در آن پیدا نشد؛ شاید ساختار سایت عوض شده یا جست‌وجو نتیجه نداشت."
        elif not parsed.items:
            out["message"] = (f"صفحه جست‌وجو {fmt.fa(len(parsed.product_urls))} کالا داشت؛ قیمت فروشنده‌ها در صفحه "
                              "خود هر کالا خوانده می‌شود.")
    except scout.SiteBlocked as exc:
        out["status"], out["message"] = "blocked", str(exc)
    except FetchBlocked as exc:
        out["status"] = "unreachable" if exc.status is None else "error"
        out["http"] = exc.status
        out["message"] = str(exc)
    except Exception as exc:
        out["status"], out["message"] = "error", f"{exc.__class__.__name__}: {exc}"[:300]
    out["seconds"] = round(time.monotonic() - t0, 1)
    out["browser"] = browsers.used > before
    return out


def check_osm(ms) -> dict:
    site = site_mod.OSM
    cfg = ms.cfg.scout
    fetcher = ms.fetcher_factory()
    fetcher.timeout = max(fetcher.timeout, 60)
    fetcher.max_bytes = max(fetcher.max_bytes, 8_000_000)
    out = _base(site.key, site.label, cfg.osm_urls[0] if cfg.osm_urls else None)
    errors = []
    t0 = time.monotonic()
    for base in cfg.osm_urls:
        host = urllib.parse.urlsplit(base).hostname or base
        try:
            page = fetcher.get(site.request_url(base, OSM_PROBE), host, cfg.delay_seconds)
            parsed = site.parse_map(page.text(), None)
            out.update(url=base, http=page.status, items=len(site_mod.overpass_elements(page.text())),
                       cards=len(parsed.businesses), status="ok" if parsed.businesses else "empty",
                       sample=[{"title": b.name, "price": None, "seller": b.city} for b in parsed.businesses[:3]],
                       message="" if parsed.businesses else "سرویس نقشه جواب داد ولی در محدوده آزمون (مرکز تهران) "
                                                             "کسب‌وکاری برنگرداند.")
            break
        except (FetchBlocked, ValueError) as exc:
            errors.append(f"{host}: {exc}")
    else:
        out["status"] = "unreachable" if errors else "off"
        out["message"] = "؛ ".join(errors) or "نشانی سرویس نقشه در [scout] osm_urls نیست."
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def check_rate(cfg) -> dict:
    out = _base("rate", f"نرخ دلار ({fx.label(cfg)})", None)
    t0 = time.monotonic()
    try:
        res = fx.fetch(cfg)
        out["http"] = res.http_status
        if res.ok:
            out.update(status="ok", items=1, sample=[{"title": "دلار آزاد", "price": f"{fmt.money(res.value_toman)} تومان",
                                                      "seller": None}],
                       message="" if cfg.source.mode == "live" else f"حالت دریافت نرخ «{cfg.source.mode}» است، نه live.")
        else:
            out["status"] = "error" if res.http_status else "unreachable"
            out["message"] = res.message
    except Exception as exc:
        out["message"] = f"{exc.__class__.__name__}: {exc}"[:300]
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def run(ms, keys: list[str] | None = None, query: str = PROBE) -> dict:
    cfg = ms.cfg.scout
    wanted = [k for k in (keys or list(cfg.sites) + (["osm"] if cfg.osm else []))]
    results = [check_rate(ms.cfg)]
    fetcher = ms.fetcher_factory()
    browsers = _Counting(ms)
    try:
        for key in wanted:
            if key == "osm":
                continue
            site = site_mod.SITES.get(key)
            if site is not None:
                results.append(check_site(ms, site, query, fetcher, browsers))
    finally:
        browsers.close()
    if "osm" in wanted:
        results.append(check_osm(ms))
    report = {"at": iso(now_utc()), "query": query, "results": results,
              "ok": sum(1 for r in results if r["status"] == "ok"), "total": len(results)}
    with ms.db() as c, db.tx(c):
        db.meta_set(c, META_KEY, json.dumps(report, ensure_ascii=False))
    return report


def last(ms) -> dict | None:
    with ms.db() as c:
        raw = db.meta_get(c, META_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def running() -> bool:
    return _lock.locked()


def start(ms, user: str) -> bool:
    if not _lock.acquire(blocking=False):
        return False

    def work():
        try:
            run(ms)
            with ms.db() as c, db.tx(c):
                db.audit(c, iso(now_utc()), user, "site_check", {})
        finally:
            _lock.release()
    threading.Thread(target=work, name="borna-site-check", daemon=True).start()
    return True


def lines(report: dict) -> list[str]:
    out = [f"بررسی سایت‌ها در {fmt.jdatetime(report['at'])}؛ جست‌وجوی آزمون: «{report['query']}»",
           f"{fmt.fa(report['ok'])} از {fmt.fa(report['total'])} سالم."]
    for r in report["results"]:
        head = f"- {r['label']}: {STATUS.get(r['status'], r['status'])}"
        if r["status"] in ("ok", "empty") and r["key"] != "rate":
            head += (f"؛ {fmt.fa(r['items'])} آگهی ({fmt.fa(r['priced'])} با قیمت)"
                     + (f"، {fmt.fa(r.get('products', 0))} صفحه کالا" if r.get("products") else "")
                     + ("، با مرورگر نامرئی" if r["browser"] else "") + f"، {fmt.fa(r['seconds'])} ثانیه")
        out.append(head)
        for s in r["sample"]:
            out.append("    - " + "، ".join(x for x in (s.get("title"), s.get("price"), s.get("seller")) if x))
        if r["message"]:
            out.append("    " + r["message"])
    return out
