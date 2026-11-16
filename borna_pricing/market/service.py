from __future__ import annotations

import json
import logging
import re
import statistics
import threading
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal

from .. import db, fmt
from ..clock import TEHRAN, iso, now_utc, parse_iso
from ..service import ServiceError
from . import catalog, compare, evidence, extract, matching, presets, prices, textnorm as tn
from .fetcher import FetchBlocked, Fetcher, normalize_domain

log = logging.getLogger("borna.market")

SOURCE_KINDS = {"marketplace": "بازار آنلاین (مثل باسلام)", "aggregator": "مقایسه قیمت (مثل ترب، ایمالز)",
                "shop": "فروشگاه اینترنتی", "manufacturer": "سایت سازنده یا رقیب", "channel": "کانال پیام‌رسان",
                "manual": "ثبت دستی"}
POLICY_LABELS = {"pending": "در انتظار بررسی", "approved": "مجاز", "blocked": "ممنوع"}
OBS_STATUS = {"ok": "معتبر", "pending": "در انتظار تأیید", "quarantined": "قرنطینه", "rejected": "رد شد"}
STATE_LABELS = {"new": "تازه", "proposed": "پیشنهاد تطبیق", "matched": "تطبیق‌شده", "unmatched": "بدون تطبیق"}
CHANNELS = {"phone": "تماس تلفنی", "visit": "مراجعه حضوری", "invoice": "فاکتور یا پیش‌فاکتور", "whatsapp": "واتس‌اپ",
            "telegram": "تلگرام", "bale": "بله", "eitaa": "ایتا", "instagram": "اینستاگرام", "website": "سایت",
            "other": "سایر"}
MANUAL_KEY = "manual"
AMBIGUOUS_LIMIT = 5
AUTO_NOTE = "تأیید خودکار تطبیق قوی"
DROP_MIN_ITEMS = 4
GROUP_NOTE = "به‌تبعیت از تأیید آگهی هم‌گروه (همان کالا، فروشنده دیگر)"
NO_ITEMS = "صفحه خوانده شد ولی هیچ کالایی پیدا نشد؛ روش خواندن یا الگو را بررسی کنید."
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,39}$")


def _j(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _median(values):
    return int(statistics.median(values)) if values else None


def _with_preset(data: dict) -> dict:
    key = str(data.get("preset") or "").strip()
    if not key:
        return data
    preset = presets.PRESETS.get(key)
    if preset is None:
        raise ServiceError(f"قالب آماده «{key[:40]}» شناخته نشد.")
    return {**data, "parser": "regex", "item_regex": preset.regex}


def _drop_note(before: int | None, after: int) -> str | None:
    if before is None or before < DROP_MIN_ITEMS or after * 2 >= before:
        return None
    return (f"افت ناگهانی تعداد کالا (از {fmt.fa(before)} به {fmt.fa(after)})؛ "
            "ممکن است ساختار سایت عوض شده باشد.")


class MarketService:

    def __init__(self, svc, fetcher_factory=None, browser_factory=None):
        self.svc = svc
        self.cfg = svc.cfg
        self.store = evidence.EvidenceStore(self.cfg.paths.evidence_dir)
        self.fetcher_factory = fetcher_factory or self._default_fetcher
        self.browser_factory = browser_factory or self._default_browser
        self._agent_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.sales = None
        self.ai = None

    def _default_fetcher(self):
        c = self.cfg.collector
        return Fetcher(c.user_agent, timeout=c.timeout_seconds, max_bytes=c.max_bytes, min_delay=c.min_delay_seconds,
                       respect_robots=c.respect_robots)

    def _default_browser(self):
        from .browser import Browser
        return Browser(self.cfg.browser, min_delay=max(self.cfg.scout.delay_seconds, self.cfg.collector.min_delay_seconds))

    def scout(self, user: str = "system", run_id: int | None = None) -> dict:
        from . import scout
        return scout.run(self, user, run_id)

    def db(self):
        return self.svc.db()

    @staticmethod
    def _audit(c, user, action, details):
        db.audit(c, iso(now_utc()), user, action, details)

    def _manual_source(self, c) -> int:
        row = c.execute("SELECT id FROM sources WHERE key=?", (MANUAL_KEY,)).fetchone()
        if row:
            return row["id"]
        now = iso(now_utc())
        return c.execute(
            "INSERT INTO sources(key, name, kind, unit, price_level, policy_status, created_at, created_by, updated_at)"
            " VALUES (?, 'ثبت دستی', 'manual', 'toman', 'unknown', 'approved', ?, 'system', ?)",
            (MANUAL_KEY, now, now)).lastrowid

    def own_seller(self, seller: str | None) -> bool:
        s = tn.norm(seller or "")
        return bool(s) and any(tn.norm(o) in s for o in self.cfg.market.own_sellers)

    def sources(self, include_manual: bool = True) -> list[dict]:
        with self.db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY kind='manual', name")]
            stats = {r["source_id"]: dict(r) for r in c.execute(
                "SELECT source_id, COUNT(*) AS n, SUM(match_state='matched') AS matched, MAX(last_seen_at) AS seen"
                " FROM external_products GROUP BY source_id")}
        out = []
        for r in rows:
            if r["kind"] == "manual" and not include_manual:
                continue
            r["start_urls"] = _j(r["start_urls"], [])
            r.update({"n_listings": 0, "n_matched": 0, "last_seen": None})
            st = stats.get(r["id"])
            if st:
                r.update({"n_listings": st["n"], "n_matched": st["matched"] or 0, "last_seen": st["seen"]})
            r["due_at"] = self._source_due_at(r)
            out.append(r)
        return out

    def get_source(self, source_id: int) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            return None
        r = dict(row)
        r["start_urls"] = _j(r["start_urls"], [])
        return r

    def _clean_source(self, data: dict, existing: dict | None) -> dict:
        data = _with_preset(data)

        def txt(k, maxlen=200):
            return str(data.get(k) or "").strip()[:maxlen]

        key = txt("key", 40).lower() if not existing else existing["key"]
        if not _KEY_RE.match(key) or key == MANUAL_KEY:
            raise ServiceError("شناسه منبع باید ۲ تا ۴۰ حرف کوچک انگلیسی، رقم، - یا _ باشد (مثلاً basalam).")
        name = txt("name")
        if not name:
            raise ServiceError("نام منبع را وارد کنید.")
        kind = txt("kind")
        if kind not in SOURCE_KINDS or kind == "manual":
            raise ServiceError("نوع منبع نامعتبر است.")
        unit = txt("unit")
        if unit not in prices.UNIT_LABELS:
            raise ServiceError("واحد قیمت منبع باید تومان یا ریال باشد.")
        level = txt("price_level")
        if level not in prices.LEVEL_LABELS:
            raise ServiceError("سطح قیمت منبع نامعتبر است.")
        parser = txt("parser") or "auto"
        if parser not in ("auto", "regex"):
            raise ServiceError("روش خواندن صفحه نامعتبر است.")
        regex = str(data.get("item_regex") or "").strip() or None
        if parser == "regex":
            try:
                extract.validate_regex(regex or "")
            except ValueError as exc:
                raise ServiceError(str(exc)) from None
        domain = None
        urls: list[str] = []
        if kind != "channel" or txt("domain"):
            try:
                domain = normalize_domain(txt("domain"))
            except FetchBlocked as exc:
                raise ServiceError(str(exc)) from None
            for line in str(data.get("start_urls") or "").splitlines():
                u = line.strip()
                if not u:
                    continue
                parts = urllib.parse.urlsplit(u)
                if parts.scheme not in ("http", "https") or not parts.hostname or not (
                        parts.hostname.lower() == domain or parts.hostname.lower().endswith("." + domain)
                        or parts.hostname.lower() == "www." + domain):
                    raise ServiceError(f"نشانی «{u}» با http(s) شروع نمی‌شود یا روی دامنه {domain} نیست.")
                if u not in urls:
                    urls.append(u)
        try:
            interval = int(tn.latin_digits(data.get("interval_hours") or 24))
            delay = int(tn.latin_digits(data.get("delay_seconds") or self.cfg.collector.min_delay_seconds))
            pages = int(tn.latin_digits(data.get("max_pages") or 10))
        except ValueError:
            raise ServiceError("فاصله پایش، مکث و تعداد صفحه باید عدد صحیح باشند.") from None
        if not 1 <= interval <= 24 * 30 or not 0 <= delay <= 600 or not 1 <= pages <= 200:
            raise ServiceError("فاصله پایش ۱ تا ۷۲۰ ساعت، مکث ۰ تا ۶۰۰ ثانیه و صفحه‌ها ۱ تا ۲۰۰ باشد.")
        return {"key": key, "name": name, "kind": kind, "domain": domain, "start_urls": json.dumps(urls),
                "unit": unit, "price_level": level, "parser": parser, "item_regex": regex,
                "interval_hours": interval, "delay_seconds": max(delay, self.cfg.collector.min_delay_seconds),
                "max_pages": pages, "note": txt("note", 1000) or None,
                "active": 1 if str(data.get("active", "1")) in ("1", "on", "true") else 0}

    def save_source(self, data: dict, user: str, source_id: int | None = None) -> int:
        existing = self.get_source(source_id) if source_id else None
        if source_id and (existing is None or existing["kind"] == "manual"):
            raise ServiceError("منبع پیدا نشد.")
        v = self._clean_source(data, existing)
        now = iso(now_utc())
        with self.db() as c, db.tx(c):
            if existing is None:
                if c.execute("SELECT 1 FROM sources WHERE key=?", (v["key"],)).fetchone():
                    raise ServiceError(f"منبعی با شناسه «{v['key']}» از قبل هست.")
                cols = list(v)
                sid = c.execute(
                    f"INSERT INTO sources({', '.join(cols)}, created_at, created_by, updated_at)"
                    f" VALUES ({', '.join('?' for _ in cols)}, ?, ?, ?)",
                    [v[k] for k in cols] + [now, user, now]).lastrowid
                self._audit(c, user, "source_create", {"source": v["key"], "domain": v["domain"]})
                return sid
            sets = dict(v)
            sets.pop("key")
            reset = existing["domain"] != v["domain"] and existing["policy_status"] == "approved"
            if reset:
                sets.update({"policy_status": "pending", "policy_note": "دامنه عوض شد؛ دوباره بررسی شود.",
                             "policy_by": user, "policy_at": now})
            sets["updated_at"] = now
            c.execute(f"UPDATE sources SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                      list(sets.values()) + [source_id])
            self._audit(c, user, "source_update", {"source": existing["key"], "policy_reset": reset})
            return source_id

    def set_policy(self, source_id: int, status: str, note: str, user: str) -> None:
        if status not in POLICY_LABELS:
            raise ServiceError("وضعیت مجوز نامعتبر است.")
        note = (note or "").strip()
        if status != "pending" and len(note) < 5:
            raise ServiceError("بنویسید چه چیزی بررسی شد (robots.txt، شرایط استفاده سایت، …)."
                               if self.cfg.collector.respect_robots else "دلیل یا توضیح تصمیم را بنویسید (دست‌کم ۵ حرف).")
        with self.db() as c, db.tx(c):
            src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if src is None or src["kind"] == "manual":
                raise ServiceError("منبع پیدا نشد.")
            if status == "approved" and not src["domain"]:
                raise ServiceError("برای منبعی که دامنه ندارد پایش خودکار مجاز نمی‌شود.")
            c.execute("UPDATE sources SET policy_status=?, policy_note=?, policy_by=?, policy_at=? WHERE id=?",
                      (status, note or None, user, iso(now_utc()), source_id))
            self._audit(c, user, "source_policy", {"source": src["key"], "status": status, "note": note})

    def _source_due_at(self, s: dict) -> datetime | None:
        if s["kind"] in ("manual", "channel") or not s["active"] or s["policy_status"] != "approved" or not s["domain"]:
            return None
        if not s["last_run_at"]:
            return now_utc()
        return parse_iso(s["last_run_at"]) + timedelta(hours=s["interval_hours"])

    def due_sources(self, now: datetime | None = None) -> list[dict]:
        now = now or now_utc()
        return [s for s in self.sources(include_manual=False)
                if s["due_at"] is not None and s["due_at"] <= now and s["start_urls"]]

    def _rate_at(self, c, at: str) -> dict | None:
        row = c.execute("SELECT id, value_toman FROM rates WHERE accepted=1 AND fetched_at<=?"
                        " ORDER BY fetched_at DESC, id DESC LIMIT 1", (at,)).fetchone()
        return dict(row) if row else None

    def _upsert_listing(self, c, source: dict, key: str, title: str, url, seller, brand, attrs: dict, now: str) -> tuple[int, bool]:
        row = c.execute("SELECT * FROM external_products WHERE source_id=? AND ext_key=?", (source["id"], key)).fetchone()
        own = 1 if self.own_seller(seller) else 0
        attrs_s = json.dumps(attrs, ensure_ascii=False, sort_keys=True)
        if row is None:
            eid = c.execute(
                "INSERT INTO external_products(source_id, ext_key, url, title, seller, brand, attrs, tier, is_own,"
                " match_state, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?, 'new', ?, ?)",
                (source["id"], key, url, title, seller, brand, attrs_s, attrs.get("tier") or "unknown", own, now,
                 now)).lastrowid
            return eid, True
        changed = row["title"] != title or row["attrs"] != attrs_s
        state = row["match_state"]
        if changed and state in ("new", "proposed", "unmatched"):
            state = "new"
        tier = row["tier"] if row["tier_by"] else (attrs.get("tier") or "unknown")
        c.execute("UPDATE external_products SET url=?, title=?, seller=?, brand=?, attrs=?, tier=?, is_own=MAX(is_own, ?),"
                  " match_state=?, last_seen_at=? WHERE id=?",
                  (url or row["url"], title, seller or row["seller"], brand or row["brand"], attrs_s, tier, own, state,
                   now, row["id"]))
        if changed and row["match_state"] == "matched":
            self._audit(c, "system", "listing_changed", {"listing": row["id"], "old": row["title"], "new": title})
        return row["id"], False

    def _observe(self, c, eid: int, snapshot_id: int, pp: prices.ParsedPrice, list_rial, level: str, min_qty,
                 entry: str, user: str, status: str, now: str, note: str | None = None,
                 pack_qty: int | None = None) -> tuple[int | None, str]:
        row = c.execute("SELECT title FROM external_products WHERE id=?", (eid,)).fetchone()
        title = row["title"] if row else ""
        if pack_qty is None:
            pack_qty = tn.pack_qty(title)
        pack_qty = max(1, int(pack_qty))
        if level == "unknown":
            level = tn.level_hint(title) or level
        pack_rial = pp.value_rial if pack_qty > 1 and pp.value_rial else None
        unit_rial = round(pp.value_rial / pack_qty) if pack_rial else pp.value_rial
        if list_rial and pack_qty > 1:
            list_rial = round(list_rial / pack_qty)
        last = c.execute("SELECT * FROM observations WHERE external_product_id=?"
                         " ORDER BY observed_at DESC, id DESC LIMIT 1", (eid,)).fetchone()
        if (entry == "crawl" and last is not None and last["kind"] == pp.kind
                and last["price_rial"] == unit_rial and last["availability"] == pp.availability
                and last["list_price_rial"] == list_rial):
            return None, "unchanged"
        m = self.cfg.market
        rate = self._rate_at(c, now)
        each = f" (قیمت هر عدد از بسته {fmt.fa(pack_qty)} عددی)" if pack_rial else ""
        if pp.kind == "price" and status in ("ok", "pending"):
            lo, hi = prices.usd_bounds_rial(m.min_price_usd, m.max_price_usd,
                                            rate["value_toman"] if rate else self.cfg.pricing.base_rate_toman)
            if not prices.in_range(unit_rial, lo, hi):
                status, note = "quarantined", (f"قیمت {fmt.money(unit_rial // 10)} تومان{each} بیرون از بازه مجاز "
                                               f"({fmt.money(lo // 10)} تا {fmt.money(hi // 10)} تومان، معادل "
                                               f"{fmt.fa(m.min_price_usd)} تا {fmt.fa(m.max_price_usd)} دلار) است؛ "
                                               "احتمالاً واحد (تومان/ریال)، تعداد بسته یا خواندن صفحه اشتباه است.")
            else:
                prev = c.execute("SELECT price_rial FROM observations WHERE external_product_id=? AND status='ok'"
                                 " AND kind='price' ORDER BY observed_at DESC, id DESC LIMIT 1", (eid,)).fetchone()
                j = prices.jump(unit_rial, prev["price_rial"] if prev else None)
                if j is not None and abs(j) > m.jump_quarantine:
                    status, note = "quarantined", (f"تغییر {fmt.pct(j, 0)} نسبت به قیمت قبلی همین آگهی؛ "
                                                   "پیش از استفاده بررسی شود.")
        try:
            oid = c.execute(
                "INSERT INTO observations(external_product_id, snapshot_id, observed_at, kind, availability, price_rial,"
                " list_price_rial, raw_price, unit, price_level, min_qty, rate_id, status, status_note, entry,"
                " created_by, created_at, pack_qty, pack_price_rial) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, snapshot_id, now, pp.kind, pp.availability, unit_rial, list_rial, pp.raw[:200], pp.unit,
                 level, min_qty, rate["id"] if rate else None, status, note, entry, user, now, pack_qty,
                 pack_rial)).lastrowid
        except db.sqlite3.IntegrityError:
            return None, "duplicate"
        return oid, status

    def _snapshot(self, c, source_id: int, kind: str, data: bytes, ext: str, user: str, url=None, http_status=None,
                  content_type=None, filename=None, run_id=None) -> int:
        sha, rel, size = self.store.put(data, ext)
        return c.execute(
            "INSERT INTO snapshots(source_id, kind, url, fetched_at, http_status, content_type, bytes, sha256, path,"
            " filename, run_id, parser_version, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, kind, url, iso(now_utc()), http_status, content_type, size, sha, rel, filename, run_id,
             extract.PARSER_VERSION if kind == "page" else None, user)).lastrowid

    @staticmethod
    def _new_stats(src: dict) -> dict:
        return {"source": src["key"], "pages": 0, "items": 0, "new_listings": 0, "observations": 0, "quarantined": 0,
                "unchanged": 0, "unparsed": 0, "errors": [], "healthy": False}

    def collect_source(self, source_id: int, user: str = "system", run_id: int | None = None) -> dict:
        src = self.get_source(source_id)
        if src is None or src["kind"] in ("manual", "channel"):
            raise ServiceError("این منبع پایش خودکار ندارد.")
        if src["policy_status"] == "blocked":
            raise ServiceError(f"پایش «{src['name']}» ممنوع شده است؛ برای خواندن دوباره، در صفحه منبع آن را «مجاز» کنید.")
        if src["policy_status"] != "approved":
            hint = ("اول robots.txt و شرایط استفاده را بررسی و در صفحه منبع تأیید کنید."
                    if self.cfg.collector.respect_robots else "اول در صفحه منبع آن را «مجاز» کنید.")
            raise ServiceError(f"پایش «{src['name']}» هنوز مجاز نشده است؛ {hint}")
        if not src["active"] or not src["start_urls"]:
            raise ServiceError(f"منبع «{src['name']}» غیرفعال است یا نشانی صفحه ندارد.")
        f = self.fetcher_factory()
        st = self._new_stats(src)
        drops: list[str] = []
        for url in src["start_urls"][: src["max_pages"]]:
            try:
                page = f.get(url, src["domain"], src["delay_seconds"])
            except FetchBlocked as exc:
                st["errors"].append(f"{url}: {exc}")
                continue
            st["pages"] += 1
            items, warnings = extract.extract(page.text(), page.url, src["parser"], src["item_regex"])
            self._store_page(src, page, items, warnings, user, run_id, st, drops, url)
        self._finish_run(src, st, drops, user)
        return st

    def _store_page(self, src: dict, page, items: list, warnings: list[str], user: str, run_id: int | None, st: dict,
                    drops: list[str], label: str, empty_note: str = "هیچ کالایی در این صفحه پیدا نشد.",
                    check_drop: bool = True, status: str = "ok", filename: str | None = None) -> int:
        now = iso(now_utc())
        with self.db() as c, db.tx(c):
            prev = c.execute("SELECT n_items FROM snapshots WHERE source_id=? AND kind='page' AND url=?"
                             " ORDER BY id DESC LIMIT 1", (src["id"], page.url)).fetchone() if check_drop else None
            drop = _drop_note(prev["n_items"] if prev else None, len(items))
            if drop:
                drops.append(f"{label}: {drop}")
                st["errors"].append(drops[-1])
            sid = self._snapshot(c, src["id"], "page", page.body, "json" if "json" in page.content_type else "html",
                                 user, url=page.url, http_status=page.status, content_type=page.content_type,
                                 filename=filename, run_id=run_id)
            notes = warnings + ([drop] if drop else [])
            msg = "؛ ".join(notes) or (None if items else empty_note)
            c.execute("UPDATE snapshots SET n_items=?, message=? WHERE id=?", (len(items), msg, sid))
            self._ingest(c, src, items, page.url, sid, user, now, st, status)
        return sid

    def _ingest(self, c, src: dict, items: list, page_url: str, sid: int, user: str, now: str, st: dict,
                status: str = "ok") -> None:
        for it in items:
            st["items"] += 1
            attrs = tn.parse_title(" ".join(x for x in (it.title, it.brand, it.part_no) if x))
            if it.group:
                attrs["group"] = str(it.group)[:120]
            if it.city:
                attrs["city"] = str(it.city)[:60]
            eid, new = self._upsert_listing(c, src, extract.ext_key(it, page_url), it.title[:300], it.url,
                                            it.seller, it.brand, attrs, now)
            st["new_listings"] += int(new)
            unit = extract.unit_from_currency(it.currency, src["unit"])
            pp = prices.parse_price(it.price, unit, it.availability if it.availability != "unknown" else None)
            if pp.kind is None:
                st["unparsed"] += 1
                continue
            lp = prices.parse_price(it.list_price, unit) if it.list_price else None
            list_rial = lp.value_rial if lp and lp.value_rial and pp.value_rial and lp.value_rial > pp.value_rial else None
            oid, got = self._observe(c, eid, sid, pp, list_rial, src["price_level"], None, "crawl", user, status, now)
            if oid is None:
                st["unchanged"] += 1
            else:
                st["observations"] += 1
                st["quarantined"] += int(got == "quarantined")
                st["pending"] = st.get("pending", 0) + int(got == "pending")

    def _finish_run(self, src: dict, st: dict, drops: list[str], user: str, found_other: bool = False) -> None:
        if st["pages"] and not st["items"] and not found_other:
            st["errors"].append(NO_ITEMS)
        ok = st["healthy"] = st["pages"] > 0 and (st["items"] > 0 or found_other)
        with self.db() as c, db.tx(c):
            c.execute("UPDATE sources SET last_run_at=?, last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,"
                      " failure_count=CASE WHEN ? THEN 0 ELSE failure_count+1 END, last_error=? WHERE id=?",
                      (iso(now_utc()), ok, iso(now_utc()), ok, "؛ ".join(st["errors"])[:1000] or None, src["id"]))

    def manual_observation(self, form: dict, user: str, role: str, upload: tuple[bytes, str] | None = None) -> int:
        seller = str(form.get("seller") or "").strip()[:120]
        if not seller:
            raise ServiceError("نام رقیب یا فروشنده را وارد کنید.")
        product_key = str(form.get("product_key") or "").strip() or None
        title = str(form.get("title") or "").strip()[:300]
        note = str(form.get("note") or "").strip()[:2000]
        unit = form.get("unit") or "toman"
        level = form.get("price_level") or "unknown"
        tier = form.get("tier") or "unknown"
        channel = form.get("channel") or "other"
        avail = form.get("availability") or "unknown"
        if unit not in prices.UNIT_LABELS or level not in prices.LEVEL_LABELS or tier not in tn.TIER_LABELS \
                or channel not in CHANNELS or avail not in prices.AVAILABILITY_LABELS:
            raise ServiceError("یکی از گزینه‌های فرم نامعتبر است.")
        min_qty = None
        if str(form.get("min_qty") or "").strip():
            q = tn.latin_digits(form.get("min_qty")).strip()
            if not q.isdigit() or int(q) < 1:
                raise ServiceError("حداقل تعداد باید عدد صحیح مثبت باشد.")
            min_qty = int(q)
        pack = None
        if str(form.get("pack_qty") or "").strip():
            q = tn.latin_digits(form.get("pack_qty")).strip()
            if not q.isdigit() or not 1 <= int(q) <= tn.PACK_MAX:
                raise ServiceError(f"«این قیمت برای چند عدد است» باید عددی بین ۱ و {fmt.fa(tn.PACK_MAX)} باشد.")
            pack = int(q)
        raw_price = str(form.get("price") or "").strip()
        pp = prices.parse_price(raw_price if raw_price else ("ناموجود" if avail == "out_of_stock" else ""), unit,
                                avail if avail != "unknown" else None)
        if pp.kind is None:
            raise ServiceError("قیمت را با رقم وارد کنید (مثلاً 3200000)، یا وضعیت را «ناموجود» بگذارید.")
        if upload is None and len(note) < 5:
            raise ServiceError("مدرک لازم است: فایل (عکس، PDF) پیوست کنید یا شرح منبع را بنویسید.")
        with self.db() as c, db.tx(c):
            prod = None
            if product_key:
                prod = c.execute("SELECT * FROM products WHERE key=?", (product_key,)).fetchone()
                if prod is None:
                    raise ServiceError("کالای انتخاب‌شده پیدا نشد.")
            if not title:
                title = f"{prod['name']} ({seller})" if prod else ""
            if not title:
                raise ServiceError("عنوان یا شرح کالای رقیب را بنویسید، یا کالای برنا را انتخاب کنید.")
            src_id = self._manual_source(c)
            src = dict(c.execute("SELECT * FROM sources WHERE id=?", (src_id,)).fetchone())
            if upload is not None:
                data, fname = upload
                ext = evidence.sniff_upload(data, fname or "")
                if ext is None:
                    raise ServiceError("فایل مدرک باید عکس (PNG، JPG، WEBP)، PDF یا متن (txt) باشد.")
                if len(data) > self.cfg.collector.max_upload_mb * 1024 * 1024:
                    raise ServiceError("حجم فایل مدرک بیش از سقف مجاز است.")
                sid = self._snapshot(c, src_id, "upload", data, ext, user, filename=(fname or "")[:120] or None,
                                     content_type=evidence.MIME[ext])
            else:
                body = f"{CHANNELS[channel]} | {seller}\n{note}".encode("utf-8")
                sid = self._snapshot(c, src_id, "note", body, "txt", user, content_type="text/plain")
            attrs = tn.parse_title(title)
            attrs["tier"] = tier if tier != "unknown" else attrs["tier"]
            attrs["channel"] = channel
            key = "manual:" + tn.norm(seller)[:80] + "|" + tn.norm(title)[:200]
            now = iso(now_utc())
            eid, _ = self._upsert_listing(c, src, key, title, None, seller, attrs.get("brand"), attrs, now)
            if tier != "unknown":
                c.execute("UPDATE external_products SET tier=?, tier_by=? WHERE id=?", (tier, user, eid))
            status = "ok" if role in ("approver", "admin") else "pending"
            oid, status = self._observe(c, eid, sid, pp, None, level, min_qty, "manual", user, status, now,
                                        note=None if status == "ok" else "ثبت فروش؛ منتظر تأیید", pack_qty=pack)
            if oid is None:
                raise ServiceError("همین مشاهده قبلاً ثبت شده است.")
            if prod is not None:
                self._link(c, eid, prod["key"], user, "approved" if status == "ok" else "proposed", "manual",
                           ["manual"], note or None)
            self._audit(c, user, "observation_manual", {"observation": oid, "seller": seller, "product": product_key,
                                                        "status": status})
            return oid

    def profiles(self, c=None) -> list[dict]:
        if c is None:
            with self.db() as c2:
                return self.profiles(c2)
        specs: dict[str, list[dict]] = {}
        for r in c.execute("SELECT product_key, attr, value FROM product_specs ORDER BY rowid"):
            specs.setdefault(r["product_key"], []).append(dict(r))
        return [catalog.product_profile(dict(p), specs.get(p["key"]))
                for p in c.execute("SELECT * FROM products WHERE active=1 ORDER BY row_no, key")]

    def _link(self, c, eid: int, product_key: str, user: str, status: str, verdict: str, reasons, note=None) -> int:
        now = iso(now_utc())
        if status == "approved":
            other = c.execute("SELECT product_key FROM matches WHERE external_product_id=? AND status='approved'"
                              " AND product_key!=?", (eid, product_key)).fetchone()
            if other:
                raise ServiceError(f"این آگهی قبلاً به کالای {other['product_key']} وصل شده است؛ اول آن تطبیق را لغو کنید.")
        relation = "equivalent"
        if verdict == "manual":
            prof = next((p for p in self.profiles(c) if p["key"] == product_key), None)
            attrs = _j(c.execute("SELECT attrs FROM external_products WHERE id=?", (eid,)).fetchone()["attrs"], {})
            if prof and prof.get("design") and attrs.get("design") and prof["design"] != attrs["design"]:
                relation = "substitute"
        else:
            row = c.execute("SELECT relation FROM matches WHERE external_product_id=? AND product_key=?",
                            (eid, product_key)).fetchone()
            relation = (row["relation"] if row else None) or relation
        c.execute(
            "INSERT INTO matches(external_product_id, product_key, status, verdict, relation, reasons, score, engine_version,"
            " proposed_at, decided_by, decided_at, note) VALUES (?,?,?,?,?,?,0,?,?,?,?,?)"
            " ON CONFLICT(external_product_id, product_key) DO UPDATE SET status=excluded.status,"
            " verdict=excluded.verdict, relation=excluded.relation, reasons=excluded.reasons,"
            " decided_by=excluded.decided_by, decided_at=excluded.decided_at, note=excluded.note",
            (eid, product_key, status, verdict, relation, json.dumps(reasons), matching.ENGINE_VERSION, now,
             user if status != "proposed" else None, now if status != "proposed" else None, note))
        self._refresh_state(c, eid)
        return c.execute("SELECT id FROM matches WHERE external_product_id=? AND product_key=?",
                         (eid, product_key)).fetchone()["id"]

    def _refresh_state(self, c, eid: int) -> str:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) AS n FROM matches WHERE external_product_id=? GROUP BY status", (eid,))}
        state = "matched" if counts.get("approved") else "proposed" if counts.get("proposed") else "unmatched"
        c.execute("UPDATE external_products SET match_state=? WHERE id=?", (state, eid))
        return state

    def match_pending(self, user: str = "system", limit: int = 2000) -> dict:
        st = {"listings": 0, "proposed": 0, "matched": 0, "unmatched": 0, "ambiguous": 0}
        with self.db() as c, db.tx(c):
            profiles = self.profiles(c)
            owners = matching.pn_owners(profiles)
            rows = c.execute("SELECT * FROM external_products WHERE match_state='new' ORDER BY id LIMIT ?",
                             (limit,)).fetchall()
            for r in rows:
                st["listings"] += 1
                attrs = _j(r["attrs"], {})
                if attrs.get("group") and self._follow_group(c, r, attrs["group"]):
                    st["matched"] += 1
                    continue
                verdicts = matching.match_listing(attrs, profiles, self.cfg.market.length_tolerance_mm, owners)
                human = {m["product_key"]: m for m in c.execute(
                    "SELECT * FROM matches WHERE external_product_id=? AND decided_by IS NOT NULL AND decided_by!='system'",
                    (r["id"],))}
                c.execute("DELETE FROM matches WHERE external_product_id=? AND (decided_by IS NULL OR decided_by='system')",
                          (r["id"],))
                alive = [v for v in verdicts if v.verdict != "reject"]
                ambiguous = (not human and len(alive) > AMBIGUOUS_LIMIT
                             and not any(v.verdict in ("strong", "candidate") for v in alive))
                now = iso(now_utc())
                for v in verdicts:
                    if v.product_key in human or (ambiguous and v.verdict != "reject"):
                        continue
                    status = "rejected" if v.verdict == "reject" else "proposed"
                    c.execute(
                        "INSERT INTO matches(external_product_id, product_key, status, verdict, relation, reasons, score,"
                        " engine_version, proposed_at, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], v.product_key, status, v.verdict, v.relation, json.dumps(v.reasons), v.score,
                         matching.ENGINE_VERSION, now, "system" if status == "rejected" else None,
                         now if status == "rejected" else None))
                state = self._refresh_state(c, r["id"])
                st["ambiguous"] += int(ambiguous)
                st[state] = st.get(state, 0) + 1
            if self.cfg.market.auto_approve_strong:
                st["auto_approved"] = self._approve_strong(c, "system", AUTO_NOTE)
                if st["auto_approved"] and rows:
                    states = [c.execute("SELECT match_state FROM external_products WHERE id=?", (r["id"],)).fetchone()[0]
                              for r in rows]
                    for k in ("proposed", "matched", "unmatched"):
                        st[k] = states.count(k)
            st["outliers"] = self._screen_outliers(c)
        return st

    def separate(self, row: dict) -> bool:
        return row.get("tier") in self.cfg.market.separate_tiers

    def split_tiers(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        return [r for r in rows if not self.separate(r)], [r for r in rows if self.separate(r)]

    def separate_label(self) -> str:
        return "، ".join(tn.TIER_LABELS[t] for t in self.cfg.market.separate_tiers)

    def _screen_outliers(self, c, keys=None) -> int:
        m = self.cfg.market
        groups: dict = {}
        for r in self.competitor_rows(c, now_utc()):
            o = r["obs"]
            if (not r["fresh"] or r["is_own"] or o["kind"] != "price" or o["availability"] == "out_of_stock"
                    or (keys is not None and r["product_key"] not in keys)):
                continue
            groups.setdefault((r["product_key"], r["relation"], self.separate(r), compare.obs_side(o)), []).append(r)
        n = 0
        for members in groups.values():
            if len(members) <= m.min_prices:
                continue
            for r in members:
                o = r["obs"]
                if o["status_by"]:
                    continue
                peers = sorted(x["obs"]["price_rial"] for x in members if x is not r)
                med = _median(peers)
                if not med:
                    continue
                ratio = Decimal(o["price_rial"]) / Decimal(med)
                if m.outlier_low <= ratio <= m.outlier_high:
                    continue
                note = (f"قیمت پرت: {fmt.fa(f'{ratio:.1f}'.replace('.', '٫'))} برابر میانه {fmt.fa(len(peers))} قیمت "
                        f"دیگر همین کالا ({fmt.money(med // 10)} تومان)؛ پیش از استفاده بررسی شود.")
                n += c.execute("UPDATE observations SET status='quarantined', status_note=? WHERE id=?"
                               " AND status='ok' AND status_by IS NULL", (note, o["id"])).rowcount
        return n

    def _follow_group(self, c, r, group: str) -> bool:
        if c.execute("SELECT 1 FROM matches WHERE external_product_id=? AND decided_by IS NOT NULL"
                     " AND decided_by!='system' LIMIT 1", (r["id"],)).fetchone():
            return False
        sib = c.execute(
            "SELECT m.product_key, m.decided_by FROM matches m JOIN external_products e ON e.id=m.external_product_id"
            " WHERE e.source_id=? AND e.id!=? AND m.status='approved' AND json_extract(e.attrs, '$.group')=?"
            " ORDER BY m.decided_at DESC, m.id DESC LIMIT 1", (r["source_id"], r["id"], group)).fetchone()
        if sib is None:
            return False
        c.execute("DELETE FROM matches WHERE external_product_id=? AND (decided_by IS NULL OR decided_by='system')",
                  (r["id"],))
        self._link(c, r["id"], sib["product_key"], sib["decided_by"] or "system", "approved", "manual", ["group"],
                   GROUP_NOTE)
        return True

    def _group_siblings(self, c, eid: int) -> list[int]:
        row = c.execute("SELECT source_id, json_extract(attrs, '$.group') AS g FROM external_products WHERE id=?",
                        (eid,)).fetchone()
        if row is None or not row["g"]:
            return []
        return [x["id"] for x in c.execute("SELECT id FROM external_products WHERE source_id=? AND id!=?"
                                           " AND json_extract(attrs, '$.group')=?", (row["source_id"], eid, row["g"]))]

    def _propagate_group(self, c, eid: int, product_key: str, user: str, action: str) -> int:
        n = 0
        now = iso(now_utc())
        for other in self._group_siblings(c, eid):
            if action == "approve":
                if c.execute("SELECT 1 FROM matches WHERE external_product_id=? AND status='approved'",
                             (other,)).fetchone():
                    continue
                mine = c.execute("SELECT verdict, reasons FROM matches WHERE external_product_id=? AND product_key=?",
                                 (other, product_key)).fetchone()
                keep = mine is not None and mine["verdict"] != "reject"
                self._link(c, other, product_key, user, "approved", mine["verdict"] if keep else "manual",
                           _j(mine["reasons"], []) if keep else ["group"], GROUP_NOTE)
                c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE"
                          " external_product_id=? AND status='proposed' AND product_key!=?",
                          (user, now, "تطبیق دیگری تأیید شد", other, product_key))
            elif action == "reject":
                if c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE"
                             " external_product_id=? AND product_key=? AND status='proposed'",
                             (user, now, "رد شد به‌تبعیت از آگهی هم‌گروه", other, product_key)).rowcount == 0:
                    continue
            else:
                if c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE"
                             " external_product_id=? AND product_key=? AND status='approved'",
                             (user, now, "لغو تطبیق به‌تبعیت از آگهی هم‌گروه", other, product_key)).rowcount == 0:
                    continue
            self._refresh_state(c, other)
            n += 1
        return n

    def rematch(self, user: str) -> dict:
        with self.db() as c, db.tx(c):
            c.execute("UPDATE external_products SET match_state='new' WHERE match_state IN ('proposed', 'unmatched')")
        return self.match_pending(user)

    def decide_match(self, match_id: int, accept: bool, user: str, note: str = "") -> None:
        with self.db() as c, db.tx(c):
            m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
            if m is None:
                raise ServiceError("پیشنهاد تطبیق پیدا نشد.")
            if accept:
                self._link(c, m["external_product_id"], m["product_key"], user, "approved", m["verdict"],
                           _j(m["reasons"], []), note or None)
                c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE"
                          " external_product_id=? AND status='proposed' AND id!=?",
                          (user, iso(now_utc()), "تطبیق دیگری تأیید شد", m["external_product_id"], match_id))
            else:
                c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE id=?",
                          (user, iso(now_utc()), note or None, match_id))
            self._refresh_state(c, m["external_product_id"])
            group = self._propagate_group(c, m["external_product_id"], m["product_key"], user,
                                          "approve" if accept else "reject")
            if accept:
                self._screen_outliers(c, {m["product_key"]})
            self._audit(c, user, "match_approve" if accept else "match_reject",
                        {"listing": m["external_product_id"], "product": m["product_key"], "note": note,
                         "group_listings": group})

    def manual_link(self, listing_id: int, product_key: str, user: str, note: str = "") -> None:
        with self.db() as c, db.tx(c):
            if c.execute("SELECT 1 FROM external_products WHERE id=?", (listing_id,)).fetchone() is None:
                raise ServiceError("آگهی پیدا نشد.")
            if c.execute("SELECT 1 FROM products WHERE key=? AND active=1", (product_key,)).fetchone() is None:
                raise ServiceError("کالای برنا پیدا نشد.")
            self._link(c, listing_id, product_key, user, "approved", "manual", ["manual"], note or None)
            c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE"
                      " external_product_id=? AND status='proposed'",
                      (user, iso(now_utc()), "تطبیق دستی ثبت شد", listing_id))
            self._refresh_state(c, listing_id)
            group = self._propagate_group(c, listing_id, product_key, user, "approve")
            self._screen_outliers(c, {product_key})
            self._audit(c, user, "match_manual", {"listing": listing_id, "product": product_key, "note": note,
                                                  "group_listings": group})

    def unlink(self, match_id: int, user: str, note: str = "") -> None:
        with self.db() as c, db.tx(c):
            m = c.execute("SELECT * FROM matches WHERE id=? AND status='approved'", (match_id,)).fetchone()
            if m is None:
                raise ServiceError("تطبیق تأییدشده پیدا نشد.")
            c.execute("UPDATE matches SET status='rejected', decided_by=?, decided_at=?, note=? WHERE id=?",
                      (user, iso(now_utc()), note or "لغو تطبیق", match_id))
            self._refresh_state(c, m["external_product_id"])
            group = self._propagate_group(c, m["external_product_id"], m["product_key"], user, "unlink")
            self._audit(c, user, "match_unlink", {"listing": m["external_product_id"], "product": m["product_key"],
                                                  "group_listings": group})

    def _approve_strong(self, c, user: str, note: str) -> int:
        n = 0
        rows = c.execute(
            "SELECT m.id, m.external_product_id, m.product_key FROM matches m"
            " JOIN external_products e ON e.id=m.external_product_id"
            " WHERE m.status='proposed' AND m.verdict='strong' AND e.match_state='proposed'"
            " AND (SELECT COUNT(*) FROM matches x WHERE x.external_product_id=m.external_product_id"
            "      AND x.status='proposed') = 1").fetchall()
        for r in rows:
            if c.execute("UPDATE matches SET status='approved', decided_by=?, decided_at=?, note=? WHERE id=?"
                         " AND status='proposed'", (user, iso(now_utc()), note, r["id"])).rowcount == 0:
                continue
            self._refresh_state(c, r["external_product_id"])
            self._propagate_group(c, r["external_product_id"], r["product_key"], user, "approve")
            n += 1
        if n:
            self._screen_outliers(c, {r["product_key"] for r in rows})
            self._audit(c, user, "match_auto_approve" if user == "system" else "match_bulk_approve", {"count": n})
        return n

    def strong_pending(self) -> int:
        with self.db() as c:
            return c.execute(
                "SELECT COUNT(*) FROM matches m JOIN external_products e ON e.id=m.external_product_id"
                " WHERE m.status='proposed' AND m.verdict='strong' AND e.match_state='proposed'"
                " AND (SELECT COUNT(*) FROM matches x WHERE x.external_product_id=m.external_product_id"
                "      AND x.status='proposed') = 1").fetchone()[0]

    def approve_strong(self, user: str, auto: bool = False) -> int:
        with self.db() as c, db.tx(c):
            return self._approve_strong(c, "system" if auto else user, AUTO_NOTE if auto else "تأیید گروهی تطبیق‌های قوی")

    def set_tier(self, listing_id: int, tier: str, user: str) -> None:
        if tier not in tn.TIER_LABELS:
            raise ServiceError("اصالت نامعتبر است.")
        with self.db() as c, db.tx(c):
            if c.execute("UPDATE external_products SET tier=?, tier_by=? WHERE id=?",
                         (tier, user, listing_id)).rowcount == 0:
                raise ServiceError("آگهی پیدا نشد.")
            self._audit(c, user, "listing_tier", {"listing": listing_id, "tier": tier})

    def set_own(self, listing_id: int, own: bool, user: str) -> None:
        with self.db() as c, db.tx(c):
            if c.execute("UPDATE external_products SET is_own=? WHERE id=?", (int(own), listing_id)).rowcount == 0:
                raise ServiceError("آگهی پیدا نشد.")
            self._audit(c, user, "listing_own", {"listing": listing_id, "own": own})

    def decide_observation(self, obs_id: int, accept: bool, user: str, note: str = "") -> None:
        with self.db() as c, db.tx(c):
            o = c.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
            if o is None or o["status"] not in ("pending", "quarantined"):
                raise ServiceError("این مشاهده منتظر بررسی نیست.")
            status = "ok" if accept else "rejected"
            c.execute("UPDATE observations SET status=?, status_by=?, status_at=?, status_note=? WHERE id=?",
                      (status, user, iso(now_utc()), (note or o["status_note"] or "")[:500] or None, obs_id))
            if accept:
                for m in c.execute("SELECT * FROM matches WHERE external_product_id=? AND status='proposed'"
                                   " AND verdict='manual'", (o["external_product_id"],)).fetchall():
                    self._link(c, o["external_product_id"], m["product_key"], user, "approved", "manual",
                               ["manual"], m["note"])
            self._audit(c, user, "observation_approve" if accept else "observation_reject",
                        {"observation": obs_id, "note": note})

    def specs(self, key: str) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM product_specs WHERE product_key=? ORDER BY attr, value",
                                               (key,))]

    def add_spec(self, key: str, attr: str, raw: str, user: str, note: str = "") -> None:
        if attr not in catalog.SPEC_ATTRS:
            raise ServiceError("ویژگی نامعتبر است.")
        try:
            value = catalog.spec_value(attr, raw)
        except ValueError as exc:
            raise ServiceError(str(exc)) from None
        with self.db() as c, db.tx(c):
            if c.execute("SELECT 1 FROM products WHERE key=?", (key,)).fetchone() is None:
                raise ServiceError("کالا پیدا نشد.")
            if attr not in catalog.MULTI_ATTRS:
                c.execute("DELETE FROM product_specs WHERE product_key=? AND attr=?", (key, attr))
            c.execute("INSERT OR REPLACE INTO product_specs(product_key, attr, value, note, updated_by, updated_at)"
                      " VALUES (?,?,?,?,?,?)", (key, attr, value, note.strip()[:300] or None, user, iso(now_utc())))
            self._audit(c, user, "spec_set", {"product": key, "attr": attr, "value": value})
        self.rematch(user)

    def remove_spec(self, key: str, attr: str, value: str, user: str) -> None:
        with self.db() as c, db.tx(c):
            if c.execute("DELETE FROM product_specs WHERE product_key=? AND attr=? AND value=?",
                         (key, attr, value)).rowcount == 0:
                raise ServiceError("این ویژگی پیدا نشد.")
            self._audit(c, user, "spec_remove", {"product": key, "attr": attr, "value": value})
        self.rematch(user)

    def _official(self, c) -> dict:
        pub = c.execute("SELECT * FROM publications ORDER BY id DESC LIMIT 1").fetchone()
        if pub is None:
            return {"publication": None, "prices": {}}
        return {"publication": dict(pub), "prices": {r["product_key"]: r["price_rial"] for r in c.execute(
            "SELECT product_key, price_rial FROM published_prices WHERE publication_id=?", (pub["id"],))}}

    def _latest_obs(self, c, as_of: str | None = None) -> dict[int, dict]:
        cond = "AND o.observed_at<=?" if as_of else ""
        args = (as_of,) if as_of else ()
        out = {}
        for r in c.execute(
                "SELECT o.* FROM observations o WHERE o.status='ok' " + cond +
                " ORDER BY o.external_product_id, o.observed_at, o.id", args):
            out[r["external_product_id"]] = dict(r)
        return out

    def competitor_rows(self, c, as_of: datetime | None = None, product_key: str | None = None) -> list[dict]:
        as_of = as_of or now_utc()
        latest = self._latest_obs(c, iso(as_of))
        stale = as_of - timedelta(days=self.cfg.market.stale_days)
        sql = ("SELECT m.id AS match_id, m.product_key, m.relation, m.verdict, m.decided_by, e.id AS listing_id,"
               " e.title, e.seller, e.url, e.tier, e.is_own, e.last_seen_at, e.attrs, s.id AS source_id, s.name AS source_name,"
               " s.kind AS source_kind FROM matches m JOIN external_products e ON e.id=m.external_product_id"
               " JOIN sources s ON s.id=e.source_id WHERE m.status='approved'")
        args: list = []
        if product_key:
            sql += " AND m.product_key=?"
            args.append(product_key)
        rows = []
        for r in c.execute(sql, args):
            o = latest.get(r["listing_id"])
            if o is None:
                continue
            d = dict(r)
            d["obs"] = o
            seen = parse_iso(r["last_seen_at"]) if r["last_seen_at"] else parse_iso(o["observed_at"])
            d["fresh_at"] = max(seen, parse_iso(o["observed_at"])) if seen <= as_of else parse_iso(o["observed_at"])
            d["fresh"] = d["fresh_at"] >= stale
            rows.append(d)
        return rows

    @staticmethod
    def stats(rows: list[dict]) -> dict:
        out = {}
        for level in ("wholesale", "retail", "unknown"):
            sel = [r for r in rows if r["obs"]["price_level"] == level]
            vals = sorted(r["obs"]["price_rial"] for r in sel if r["obs"]["kind"] == "price"
                          and r["obs"]["availability"] != "out_of_stock")
            out[level] = {"n": len(sel), "n_priced": len(vals), "min": vals[0] if vals else None,
                          "median": _median(vals), "max": vals[-1] if vals else None,
                          "oos": sum(1 for r in sel if r["obs"]["availability"] == "out_of_stock")}
        return out

    def overview(self) -> dict:
        now = now_utc()
        with self.db() as c:
            off = self._official(c)
            products = [dict(p) for p in c.execute("SELECT * FROM products WHERE active=1 ORDER BY row_no, key")]
            all_rows = self.competitor_rows(c, now)
            rows = [r for r in all_rows if r["fresh"]]
            pending = {r["product_key"]: r["n"] for r in c.execute(
                "SELECT m.product_key, COUNT(DISTINCT m.external_product_id) AS n FROM matches m"
                " WHERE m.status='proposed' GROUP BY m.product_key")}
            kpi = dict(c.execute(
                "SELECT (SELECT COUNT(*) FROM sources WHERE kind NOT IN ('manual') AND active=1) AS sources,"
                " (SELECT COUNT(*) FROM sources WHERE policy_status='approved' AND kind NOT IN ('manual','channel')"
                "   AND active=1) AS sources_ok,"
                " (SELECT COUNT(*) FROM external_products) AS listings,"
                " (SELECT COUNT(*) FROM external_products WHERE match_state='matched') AS matched,"
                " (SELECT COUNT(*) FROM external_products WHERE match_state='proposed') AS to_review,"
                " (SELECT COUNT(*) FROM observations WHERE status IN ('pending','quarantined')) AS obs_review,"
                " (SELECT MAX(started_at) FROM agent_runs WHERE agent='price_watcher') AS last_watch").fetchone())
            kpi["paused"] = db.meta_get(c, "agents_paused") == "1"
            kpi["unmatched"] = sum(1 for r in c.execute("SELECT attrs FROM external_products WHERE match_state='unmatched'")
                                   if _j(r["attrs"], {}).get("family") == "o2")
        table = []
        min_prices = self.cfg.market.min_prices
        for p in products:
            mine = [r for r in rows if r["product_key"] == p["key"]]
            comp = [r for r in mine if not r["is_own"]]
            main, sep = self.split_tiers([r for r in comp if r["relation"] == "equivalent"])
            st = self.stats(main)
            st_sep = self.stats(sep)
            st_sub = self.stats([r for r in comp if r["relation"] == "substitute"])
            official = off["prices"].get(p["key"])
            cmp = compare.compare(official, main, min_prices)
            sep_side = compare.side_stats(sep)
            sep_level = next((lv for lv in compare.SIDES if sep_side[lv]["median"]), None)
            cheapest = {s: min((r for r in comp if compare.priced(r) and compare.obs_side(r["obs"]) == s),
                               key=lambda r: r["obs"]["price_rial"], default=None) for s in compare.SIDES}
            table.append({"product": p, "key": p["key"], "name": p["name"], "official": official, "stats": st,
                          "stats_sub": st_sub, "cmp": cmp, "n_listings": len(comp),
                          "low_data": cmp["wholesale"]["low_data"] or cmp["retail"]["low_data"],
                          "stats_sep": st_sep, "sep_level": sep_level,
                          "sep_median": sep_side[sep_level]["median"] if sep_level else None,
                          "sep_n": sep_side[sep_level]["n_priced"] if sep_level else 0,
                          "cheapest": cheapest["wholesale"] or cheapest["retail"], "cheapest_side": cheapest,
                          "pending": pending.get(p["key"], 0),
                          "oos": sum(1 for r in comp if r["obs"]["availability"] == "out_of_stock")})
            table[-1]["has_data"] = bool(table[-1]["n_listings"] or table[-1]["pending"])
        priced_rows = [t for t in table if t["official"]]
        return {"now": now, "kpi": kpi, "table": table, "publication": off["publication"],
                "sep_label": self.separate_label(),
                "summary": {s: compare.summary(priced_rows, s) for s in compare.SIDES},
                "charts": {s: compare.chart(priced_rows, s) for s in compare.SIDES}}

    def product_market(self, key: str) -> dict | None:
        with self.db() as c:
            prod = c.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
            if prod is None:
                return None
            prof = next((p for p in self.profiles(c) if p["key"] == key), None) or catalog.product_profile(dict(prod))
            off = self._official(c)
            rows = self.competitor_rows(c, product_key=key)
            sep_tiers = tuple(self.cfg.market.separate_tiers)
            history = [dict(r) for r in c.execute(
                "SELECT o.external_product_id, o.observed_at, o.price_rial, o.kind, o.availability, o.price_level,"
                " o.pack_qty FROM observations o"
                " JOIN matches m ON m.external_product_id=o.external_product_id AND m.status='approved' AND m.product_key=?"
                " JOIN external_products e ON e.id=o.external_product_id WHERE o.status='ok' AND e.is_own=0"
                " AND m.relation='equivalent'"
                + (f" AND e.tier NOT IN ({','.join('?' for _ in sep_tiers)})" if sep_tiers else "")
                + " ORDER BY o.observed_at, o.id", (key, *sep_tiers))]
            official_series = [(parse_iso(r["published_at"]), r["price_rial"]) for r in c.execute(
                "SELECT p.published_at, pp.price_rial FROM publications p JOIN published_prices pp"
                " ON pp.publication_id=p.id WHERE pp.product_key=? ORDER BY p.published_at, p.id", (key,))]
            proposals = [dict(r) for r in c.execute(
                "SELECT m.*, e.title, e.seller, s.name AS source_name FROM matches m JOIN external_products e"
                " ON e.id=m.external_product_id JOIN sources s ON s.id=e.source_id WHERE m.product_key=?"
                " AND m.status='proposed' ORDER BY m.score DESC", (key,))]
            specs = self.specs(key)
            rate = self._rate_at(c, iso(now_utc()))
        series = {"wholesale": [], "retail": []}
        current = {"wholesale": {}, "retail": {}}
        for h in history:
            lane = compare.side(h["price_level"], h["pack_qty"])
            if h["kind"] == "price" and h["availability"] != "out_of_stock" and h["price_rial"]:
                current[lane][h["external_product_id"]] = h["price_rial"]
            else:
                current[lane].pop(h["external_product_id"], None)
            if current[lane]:
                series[lane].append((parse_iso(h["observed_at"]), statistics.median(current[lane].values())))
        for r in rows:
            r["attrs_d"] = _j(r["attrs"], {})
        main, sep = self.split_tiers([r for r in rows if r["fresh"] and not r["is_own"] and r["relation"] == "equivalent"])
        official = off["prices"].get(key)
        cmp = compare.compare(official, main, self.cfg.market.min_prices)
        return {"product": dict(prod), "profile": prof, "profile_rows": catalog.describe(prof),
                "official": official, "rows": rows, "stats_sep": compare.side_stats(sep),
                "sep_label": self.separate_label(), "cmp": cmp,
                "low_data": cmp["wholesale"]["low_data"] or cmp["retail"]["low_data"],
                "stats": self.stats(main), "series": series, "official_series": official_series,
                "proposals": proposals, "specs": specs, "rate": rate}

    def listings(self, state: str | None = None, source_id: int | None = None, limit: int = 1000) -> list[dict]:
        sql = ("SELECT e.*, s.name AS source_name, s.kind AS source_kind,"
               " (SELECT product_key FROM matches m WHERE m.external_product_id=e.id AND m.status='approved') AS product_key"
               " FROM external_products e JOIN sources s ON s.id=e.source_id WHERE 1=1")
        args: list = []
        if state in STATE_LABELS:
            sql += " AND e.match_state=?"
            args.append(state)
        if source_id:
            sql += " AND e.source_id=?"
            args.append(source_id)
        sql += " ORDER BY e.last_seen_at DESC, e.id DESC LIMIT ?"
        args.append(limit)
        with self.db() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
            latest = self._latest_obs(c)
        for r in rows:
            r["attrs_d"] = _j(r["attrs"], {})
            r["obs"] = latest.get(r["id"])
        return rows

    def listing(self, listing_id: int) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT e.*, s.name AS source_name, s.kind AS source_kind, s.unit AS source_unit"
                            " FROM external_products e JOIN sources s ON s.id=e.source_id WHERE e.id=?",
                            (listing_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["attrs_d"] = _j(d["attrs"], {})
            d["observations"] = [dict(r) for r in c.execute(
                "SELECT o.*, sn.kind AS ev_kind, sn.url AS ev_url, sn.filename AS ev_file"
                " FROM observations o JOIN snapshots sn"
                " ON sn.id=o.snapshot_id WHERE o.external_product_id=? ORDER BY o.observed_at DESC, o.id DESC",
                (listing_id,))]
            d["matches"] = [dict(r) for r in c.execute(
                "SELECT m.*, p.name AS product_name FROM matches m LEFT JOIN products p ON p.key=m.product_key"
                " WHERE m.external_product_id=? ORDER BY m.status='approved' DESC, m.status='proposed' DESC,"
                " m.score DESC", (listing_id,))]
        for m in d["matches"]:
            m["reasons_l"] = matching.reason_labels(_j(m["reasons"], []))
        return d

    def review_queue(self, limit: int = 200) -> list[dict]:
        with self.db() as c:
            profiles = {p["key"]: p for p in self.profiles(c)}
            names = {r["key"]: r["name"] for r in c.execute("SELECT key, name FROM products")}
            listings = [dict(r) for r in c.execute(
                "SELECT e.*, s.name AS source_name FROM external_products e JOIN sources s ON s.id=e.source_id"
                " WHERE e.match_state='proposed' ORDER BY e.id LIMIT ?", (limit,))]
            latest = self._latest_obs(c)
            out = []
            for e in listings:
                ms = [dict(m) for m in c.execute(
                    "SELECT * FROM matches WHERE external_product_id=? ORDER BY status='proposed' DESC, score DESC",
                    (e["id"],))]
                e["attrs_d"] = _j(e["attrs"], {})
                e["obs"] = latest.get(e["id"])
                e["proposals"] = []
                e["rejected"] = []
                for m in ms:
                    if m["verdict"] == "manual" and m["status"] == "proposed":
                        continue
                    m["reasons_l"] = matching.reason_labels(_j(m["reasons"], []))
                    m["product_name"] = names.get(m["product_key"], m["product_key"])
                    m["profile"] = profiles.get(m["product_key"])
                    (e["proposals"] if m["status"] == "proposed" else e["rejected"]).append(m)
                if not e["proposals"]:
                    continue
                e["best_rank"] = min((matching.VERDICT_RANK[m["verdict"]] for m in e["proposals"]), default=9)
                e["best_score"] = max((m["score"] for m in e["proposals"]), default=0)
                e["group_others"] = []
                out.append(e)
        firsts: dict = {}
        kept = []
        for e in out:
            g = e["attrs_d"].get("group")
            if g and (e["source_id"], g) in firsts:
                firsts[(e["source_id"], g)]["group_others"].append(e["seller"] or e["source_name"])
                continue
            if g:
                firsts[(e["source_id"], g)] = e
            kept.append(e)
        kept.sort(key=lambda e: (e["best_rank"], -e["best_score"], e["id"]))
        return kept

    def observations_to_review(self) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute(
                "SELECT o.*, e.title, e.seller, e.id AS listing_id, s.name AS source_name, sn.kind AS ev_kind,"
                " (SELECT price_rial FROM observations p WHERE p.external_product_id=o.external_product_id"
                "  AND p.status='ok' AND p.kind='price' AND p.id!=o.id ORDER BY p.observed_at DESC LIMIT 1) AS prev_rial,"
                " (SELECT m.product_key FROM matches m WHERE m.external_product_id=o.external_product_id"
                "  AND m.status IN ('approved','proposed') AND (m.status='approved' OR m.verdict='manual')"
                "  ORDER BY m.status='approved' DESC LIMIT 1) AS product_key"
                " FROM observations o JOIN external_products e ON e.id=o.external_product_id"
                " JOIN sources s ON s.id=e.source_id JOIN snapshots sn ON sn.id=o.snapshot_id"
                " WHERE o.status IN ('pending','quarantined') ORDER BY o.id")]

    def snapshot(self, snapshot_id: int) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT sn.*, s.name AS source_name FROM snapshots sn JOIN sources s ON s.id=sn.source_id"
                            " WHERE sn.id=?", (snapshot_id,)).fetchone()
        return dict(row) if row else None

    def snapshot_bytes(self, snap: dict) -> bytes:
        try:
            return self.store.read(snap["path"])
        except (OSError, ValueError):
            raise ServiceError("فایل مدرک پیدا نشد یا خراب است.") from None

    def snapshots(self, source_id: int, limit: int = 30) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM snapshots WHERE source_id=? ORDER BY id DESC LIMIT ?",
                                               (source_id, limit))]

    def test_parser(self, source_id: int, data: dict | None = None) -> dict:
        src = self.get_source(source_id)
        if src is None:
            raise ServiceError("منبع پیدا نشد.")
        data = _with_preset(data or {})
        parser = data.get("parser") or src["parser"]
        regex = data["item_regex"] if data.get("item_regex") is not None else src["item_regex"]
        snaps = [s for s in self.snapshots(source_id, 1) if s["kind"] == "page"]
        if not snaps:
            raise ServiceError("هنوز صفحه‌ای از این منبع ذخیره نشده؛ یک بار «اجرای پایش» را بزنید.")
        html = self.snapshot_bytes(snaps[0]).decode("utf-8", errors="replace")
        try:
            items, warnings = extract.extract(html, snaps[0]["url"] or "", parser, regex)
        except ValueError as exc:
            raise ServiceError(str(exc)) from None
        out = []
        for it in items[:50]:
            pp = prices.parse_price(it.price, extract.unit_from_currency(it.currency, src["unit"]))
            out.append({"title": it.title, "price": it.price, "rial": pp.value_rial, "kind": pp.kind,
                        "availability": it.availability, "url": it.url, "attrs": tn.parse_title(it.title)})
        return {"snapshot": snaps[0], "items": out, "count": len(items), "warnings": warnings}

    def _agent_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._agent_locks.setdefault(key, threading.Lock())

    def agent_running(self, key: str) -> bool:
        return self._agent_lock(key).locked()

    def paused(self) -> bool:
        with self.db() as c:
            return db.meta_get(c, "agents_paused") == "1"

    def set_paused(self, paused: bool, user: str) -> None:
        with self.db() as c, db.tx(c):
            db.meta_set(c, "agents_paused", "1" if paused else None)
            self._audit(c, user, "agents_pause" if paused else "agents_resume", {})

    def recover_interrupted(self) -> int:
        with self.db() as c, db.tx(c):
            return c.execute("UPDATE agent_runs SET status='failed', finished_at=?, message=? WHERE status='running'",
                             (iso(now_utc()), "اجرا نیمه‌کاره ماند (سامانه خاموش شد).")).rowcount

    def run_agent(self, key: str, trigger: str = "manual", user: str = "system") -> dict:
        from .agents import registry
        agent = registry().get(key)
        if agent is None:
            raise ServiceError("ایجنت پیدا نشد.")
        lock = self._agent_lock(key)
        if not lock.acquire(blocking=False):
            raise ServiceError(f"«{agent.label}» همین حالا در حال اجراست.")
        try:
            with self.db() as c, db.tx(c):
                run_id = c.execute("INSERT INTO agent_runs(agent, trigger, started_at, status, created_by)"
                                   " VALUES (?,?,?,'running',?)", (key, trigger, iso(now_utc()), user)).lastrowid
            status, message, stats = "ok", None, {}
            try:
                stats = agent.run(self, run_id, user) or {}
                status = stats.pop("_status", "ok")
                message = stats.pop("_message", None)
            except ServiceError as exc:
                status, message = "failed", str(exc)
            except Exception as exc:
                log.exception("agent %s failed", key)
                status, message = "failed", f"خطای داخلی: {exc.__class__.__name__}"
            with self.db() as c, db.tx(c):
                c.execute("UPDATE agent_runs SET status=?, finished_at=?, stats=?, message=? WHERE id=?",
                          (status, iso(now_utc()), json.dumps(stats, ensure_ascii=False, default=str), message, run_id))
            return {"run_id": run_id, "status": status, "message": message, "stats": stats}
        finally:
            lock.release()

    def scheduled_tick(self, now: datetime | None = None) -> list[dict]:
        from .agents import registry
        if not self.cfg.agents.enabled or self.paused():
            return []
        now = now or now_utc()
        done = []
        for key, agent in registry().items():
            try:
                if agent.due(self, now):
                    done.append({"agent": key, **self.run_agent(key, "schedule", "system")})
            except ServiceError as exc:
                log.info("agent %s skipped: %s", key, exc)
        return done

    def agent_runs(self, limit: int = 60, agent: str | None = None) -> list[dict]:
        with self.db() as c:
            sql = "SELECT * FROM agent_runs" + (" WHERE agent=?" if agent else "") + " ORDER BY id DESC LIMIT ?"
            rows = [dict(r) for r in c.execute(sql, ((agent, limit) if agent else (limit,)))]
        for r in rows:
            r["stats_d"] = _j(r["stats"], {})
        return rows

    def agents_overview(self) -> list[dict]:
        from .agents import registry
        now = now_utc()
        out = []
        for key, agent in registry().items():
            last = self.agent_runs(1, key)
            out.append({"key": key, "label": agent.label, "description": agent.description, "tools": agent.tools,
                        "level": agent.level, "last": last[0] if last else None, "next": agent.next_info(self, now),
                        "running": self._agent_lock(key).locked()})
        return out

    def build_report(self, user: str = "system", run_id: int | None = None, period_end: datetime | None = None) -> int:
        from . import report
        end = period_end or now_utc()
        data = report.build(self, end)
        return self.save_report("weekly", data, user, run_id)

    def save_report(self, kind: str, data: dict, user: str, run_id: int | None = None) -> int:
        now = iso(now_utc())
        with self.db() as c, db.tx(c):
            return c.execute("INSERT INTO reports(kind, created_at, period_start, period_end, run_id, data, created_by)"
                             " VALUES (?,?,?,?,?,?,?)",
                             (kind, now, data.get("period_start", now), data.get("period_end", now), run_id,
                              json.dumps(data, ensure_ascii=False, default=str), user)).lastrowid

    def reports(self, limit: int = 52, kind: str = "weekly") -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT id, kind, created_at, period_start, period_end, created_by"
                                               " FROM reports WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))]

    def report(self, report_id: int) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["data"] = _j(d["data"], {})
        return d

    def changes_since(self, at: str) -> dict:
        with self.db() as c:
            approved = c.execute("SELECT COUNT(*) FROM matches WHERE status='approved' AND decided_at>?",
                                 (at,)).fetchone()[0]
            reviewed = c.execute("SELECT COUNT(*) FROM observations WHERE status_by IS NOT NULL AND status_at>?",
                                 (at,)).fetchone()[0]
        return {"approved": approved, "reviewed": reviewed, "any": bool(approved or reviewed)}

    def last_report_at(self) -> datetime | None:
        with self.db() as c:
            row = c.execute("SELECT MAX(created_at) AS t FROM reports WHERE kind='weekly'").fetchone()
        return parse_iso(row["t"]) if row and row["t"] else None

    def report_due_at(self, now: datetime) -> datetime:
        t = now.astimezone(TEHRAN)
        days_back = (t.weekday() - self.cfg.agents.report_weekday) % 7
        slot = (t - timedelta(days=days_back)).replace(hour=self.cfg.agents.report_time.hour,
                                                       minute=self.cfg.agents.report_time.minute, second=0)
        if slot > t:
            slot -= timedelta(days=7)
        return slot
