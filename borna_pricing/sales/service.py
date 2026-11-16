from __future__ import annotations

import json
import re
import statistics
import urllib.parse
from datetime import date, datetime, timedelta

from .. import db, jalali
from ..clock import iso, now_utc, tehran_date
from ..market import textnorm as tn
from ..market.sites import on_platform
from ..service import ServiceError

CUSTOMER_KINDS = {"wholesaler": "بنکدار / عمده‌فروش", "retailer": "فروشگاه لوازم یدکی", "repair_shop": "تعمیرگاه",
                  "fleet": "ناوگان", "oem": "قطعه‌ساز / خودروساز", "other": "سایر"}
SALES_CHANNELS = {"phone": "تلفن", "visit": "حضوری", "whatsapp": "واتس‌اپ", "bale": "بله", "telegram": "تلگرام",
                  "eitaa": "ایتا", "basalam": "باسلام", "website": "سایت", "other": "سایر"}
LOST_REASONS = {"price": "قیمت بالاتر از رقیب", "payment": "شرایط پرداخت (نقد یا چک)",
                "availability": "موجود نبود یا دیر می‌رسید", "quality": "کیفیت، اصالت یا برند",
                "moq": "حداقل سفارش", "fitment": "کالای مناسب خودروی مشتری را نداشتیم",
                "no_response": "مشتری پیگیری نکرد", "other": "سایر"}
INQUIRY_STATUS = {"open": "باز", "won": "به فروش رسید", "lost": "از دست رفت"}
LEAD_STATUS = {"new": "تازه", "contacted": "تماس گرفته شد", "customer": "مشتری شد", "ignored": "مناسب نیست"}
LEAD_KINDS = {"seller": "فروشنده", "supplier": "عمده‌فروش / تأمین‌کننده", "repair": "تعمیرگاه", "manufacturer": "سازنده"}
LEAD_TO_CUSTOMER = {"seller": "retailer", "supplier": "wholesaler", "repair": "repair_shop", "manufacturer": "oem"}
SUPPLIER_WORDS = ("پخش", "عمده", "بنکدار", "بنکداری", "تامین", "تأمین", "توزیع", "واردات", "واردکننده", "وارد کننده",
                  "بازرگانی", "نمایندگی")
PLATFORM_NAMES = ("باسلام", "دیجی کالا", "دیجیکالا", "ترب", "ایمالز", "دیوار", "اسنپ شاپ", "تپسی شاپ", "شیپور", "ترب پی")
REORDER_WINDOW_DAYS = 3
PHONE_FILTERS = {"yes": "دارد", "no": "ندارد"}
LINK_LABELS = (("openstreetmap.org", "روی نقشه"), ("divar.ir", "آگهی دیوار"), ("torob.com", "صفحه ترب"),
               ("emalls.ir", "صفحه ایمالز"), ("digikala.com", "صفحه دیجی‌کالا"), ("basalam.com", "صفحه باسلام"))


def parse_date(text, default: date | None = None) -> date | None:
    s = tn.latin_digits(text or "").strip()
    if not s:
        return default
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            raise ServiceError(f"تاریخ «{text}» معتبر نیست.") from None
    try:
        return jalali.parse_jalali(s)
    except ValueError:
        raise ServiceError(f"تاریخ «{text}» معتبر نیست؛ به شکل ۱۴۰۵/۰۸/۱۰ بنویسید.") from None


def parse_int(text, label: str, lo: int = 0, hi: int | None = None, default: int | None = None) -> int | None:
    s = tn.latin_digits(text if text is not None else "").replace(",", "").replace("٬", "").strip()
    if not s:
        if default is None:
            raise ServiceError(f"{label} را وارد کنید.")
        return default
    if not s.isdigit():
        raise ServiceError(f"{label} باید عدد صحیح باشد، نه «{text}».")
    v = int(s)
    if v < lo or (hi is not None and v > hi):
        raise ServiceError(f"{label} باید بین {lo} و {hi} باشد." if hi is not None else f"{label} باید دست‌کم {lo} باشد.")
    return v


def parse_rial(text, unit: str, label: str = "قیمت", required: bool = True) -> int | None:
    s = tn.latin_digits(text if text is not None else "").replace(",", "").replace("٬", "").replace(" ", "").strip()
    if not s:
        if required:
            raise ServiceError(f"{label} را وارد کنید.")
        return None
    if not s.isdigit() or int(s) <= 0:
        raise ServiceError(f"{label} باید عدد صحیح مثبت باشد، نه «{text}».")
    return int(s) * (10 if unit == "toman" else 1)


def _today() -> date:
    return tehran_date(now_utc())


def link_label(url: str | None) -> str:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    for domain, label in LINK_LABELS:
        if host == domain or host.endswith("." + domain):
            return label
    return "سایت فروشگاه" if host else ""


def _url_rank(url: str | None) -> int:
    if not url:
        return 0
    if re.search(r"~skey~|/search\b|/s/|[?&]q(?:uery)?=", url):
        return 1
    return 2 if on_platform(url) else 3


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SalesService:

    def __init__(self, svc, market=None):
        self.svc = svc
        self.cfg = svc.cfg
        self.market = market

    def db(self):
        return self.svc.db()

    @staticmethod
    def _audit(c, user, action, details):
        db.audit(c, iso(now_utc()), user, action, details)

    def official_prices(self, c=None) -> dict[str, int]:
        if c is None:
            with self.db() as c2:
                return self.official_prices(c2)
        pub = c.execute("SELECT id FROM publications ORDER BY id DESC LIMIT 1").fetchone()
        if pub is None:
            return {}
        return {r["product_key"]: r["price_rial"] for r in c.execute(
            "SELECT product_key, price_rial FROM published_prices WHERE publication_id=?", (pub["id"],))}

    def _product(self, c, key: str):
        row = c.execute("SELECT * FROM products WHERE key=? AND active=1", (key or "",)).fetchone()
        if row is None:
            raise ServiceError("کالای برنا را از فهرست انتخاب کنید.")
        return row

    def _clean_customer(self, data: dict) -> dict:
        name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()[:120]
        if len(name) < 2:
            raise ServiceError("نام مشتری را وارد کنید.")
        kind = data.get("kind") or "wholesaler"
        if kind not in CUSTOMER_KINDS:
            raise ServiceError("نوع مشتری نامعتبر است.")
        channel = data.get("channel") or None
        if channel and channel not in SALES_CHANNELS:
            raise ServiceError("راه ارتباط نامعتبر است.")
        phone = re.sub(r"[^\d+]", "", tn.latin_digits(data.get("phone") or ""))[:20] or None
        return {"name": name, "name_norm": tn.norm(name), "kind": kind,
                "city": str(data.get("city") or "").strip()[:60] or None, "phone": phone, "channel": channel,
                "note": str(data.get("note") or "").strip()[:500] or None}

    def save_customer(self, data: dict, user: str, customer_id: int | None = None, source: str = "manual") -> int:
        v = self._clean_customer(data)
        now = iso(now_utc())
        with self.db() as c, db.tx(c):
            other = c.execute("SELECT id FROM customers WHERE name_norm=?", (v["name_norm"],)).fetchone()
            if other and other["id"] != customer_id:
                raise ServiceError(f"مشتری «{v['name']}» از قبل ثبت شده است.")
            if customer_id is None:
                cid = c.execute("INSERT INTO customers(name, name_norm, kind, city, phone, channel, source, note,"
                                " created_at, created_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (v["name"], v["name_norm"], v["kind"], v["city"], v["phone"], v["channel"], source,
                                 v["note"], now, user, now)).lastrowid
                self._audit(c, user, "customer_create", {"customer": v["name"]})
                return cid
            active = 0 if str(data.get("active", "1")) == "0" else 1
            if c.execute("UPDATE customers SET name=?, name_norm=?, kind=?, city=?, phone=?, channel=?, note=?,"
                         " active=?, updated_at=? WHERE id=?",
                         (v["name"], v["name_norm"], v["kind"], v["city"], v["phone"], v["channel"], v["note"], active,
                          now, customer_id)).rowcount == 0:
                raise ServiceError("مشتری پیدا نشد.")
            self._audit(c, user, "customer_update", {"customer": v["name"]})
            return customer_id

    def _customer_id(self, c, data: dict, user: str) -> int:
        cid = str(data.get("customer_id") or "").strip()
        if cid.isdigit():
            row = c.execute("SELECT id FROM customers WHERE id=?", (int(cid),)).fetchone()
            if row is None:
                raise ServiceError("مشتری پیدا نشد.")
            return row["id"]
        name = str(data.get("customer_name") or "").strip()
        if not name:
            raise ServiceError("مشتری را انتخاب کنید یا نام مشتری تازه را بنویسید.")
        row = c.execute("SELECT id FROM customers WHERE name_norm=?", (tn.norm(name),)).fetchone()
        if row:
            return row["id"]
        v = self._clean_customer({"name": name, "kind": data.get("customer_kind") or "wholesaler",
                                  "city": data.get("customer_city"), "channel": data.get("channel")})
        now = iso(now_utc())
        cid = c.execute("INSERT INTO customers(name, name_norm, kind, city, phone, channel, source, note, created_at,"
                        " created_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (v["name"], v["name_norm"], v["kind"], v["city"], None, v["channel"], "manual", None, now, user,
                         now)).lastrowid
        self._audit(c, user, "customer_create", {"customer": v["name"]})
        return cid

    def customers(self, include_inactive: bool = True) -> list[dict]:
        with self.db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT cu.*, (SELECT COUNT(*) FROM orders o WHERE o.customer_id=cu.id) AS n_orders,"
                " (SELECT MAX(order_date) FROM orders o WHERE o.customer_id=cu.id) AS last_order,"
                " (SELECT COALESCE(SUM(l.qty * l.unit_price_rial), 0) FROM orders o JOIN order_lines l ON l.order_id=o.id"
                "  WHERE o.customer_id=cu.id) AS total_rial,"
                " (SELECT COUNT(*) FROM inquiries i WHERE i.customer_id=cu.id AND i.status='open') AS open_inquiries"
                " FROM customers cu" + ("" if include_inactive else " WHERE cu.active=1") + " ORDER BY cu.name")]
        return rows

    def customer(self, customer_id: int) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["inquiries"] = self._inquiries(c, "WHERE i.customer_id=?", (customer_id,))
            d["orders"] = self._orders(c, "WHERE o.customer_id=?", (customer_id,))
        d["reorder"] = self._reorder_for(d["orders"])
        return d

    def _inquiries(self, c, where: str = "", args=(), limit: int = 1000) -> list[dict]:
        return [dict(r) for r in c.execute(
            "SELECT i.*, cu.name AS customer_name, p.name AS product_name FROM inquiries i"
            " JOIN customers cu ON cu.id=i.customer_id LEFT JOIN products p ON p.key=i.product_key "
            + where + " ORDER BY i.id DESC LIMIT ?", (*args, limit))]

    def inquiries(self, status: str | None = None, limit: int = 500) -> list[dict]:
        with self.db() as c:
            if status in INQUIRY_STATUS:
                return self._inquiries(c, "WHERE i.status=?", (status,), limit)
            return self._inquiries(c, limit=limit)

    def inquiry(self, inquiry_id: int) -> dict | None:
        with self.db() as c:
            rows = self._inquiries(c, "WHERE i.id=?", (inquiry_id,))
        return rows[0] if rows else None

    def create_inquiry(self, data: dict, user: str) -> int:
        qty = parse_int(data.get("qty"), "تعداد", 1, 1_000_000)
        days = parse_int(data.get("payment_days"), "مهلت پرداخت", 0, 365, default=0)
        channel = data.get("channel") or "phone"
        if channel not in SALES_CHANNELS:
            raise ServiceError("راه ارتباط نامعتبر است.")
        follow = parse_date(data.get("follow_up_on"), _today() + timedelta(days=2))
        note = str(data.get("note") or "").strip()[:1000] or None
        with self.db() as c, db.tx(c):
            prod = self._product(c, data.get("product_key"))
            quoted = parse_rial(data.get("quoted_price"), data.get("unit") or "rial", "قیمت پیشنهادی", required=False)
            if quoted is None:
                quoted = self.official_prices(c).get(prod["key"])
            cid = self._customer_id(c, data, user)
            iid = c.execute(
                "INSERT INTO inquiries(customer_id, product_key, qty, quoted_price_rial, payment_days, channel, status,"
                " note, follow_up_on, created_at, created_by) VALUES (?,?,?,?,?,?, 'open', ?,?,?,?)",
                (cid, prod["key"], qty, quoted, days, channel, note, follow.isoformat(), iso(now_utc()), user)).lastrowid
            self._audit(c, user, "inquiry_create", {"inquiry": iid, "product": prod["key"], "qty": qty})
            return iid

    def set_follow_up(self, inquiry_id: int, text: str, user: str) -> None:
        d = parse_date(text)
        if d is None:
            raise ServiceError("تاریخ پیگیری را وارد کنید.")
        with self.db() as c, db.tx(c):
            if c.execute("UPDATE inquiries SET follow_up_on=? WHERE id=? AND status='open'",
                         (d.isoformat(), inquiry_id)).rowcount == 0:
                raise ServiceError("استعلام باز پیدا نشد.")
            self._audit(c, user, "inquiry_follow_up", {"inquiry": inquiry_id, "date": jalali.jalali_str(d)})

    def close_inquiry(self, inquiry_id: int, outcome: str, user: str, role: str, data: dict | None = None) -> dict:
        data = data or {}
        if outcome not in ("won", "lost"):
            raise ServiceError("نتیجه استعلام نامعتبر است.")
        note = str(data.get("note") or "").strip()[:1000] or None
        out = {"order_id": None, "observation_id": None}
        with self.db() as c, db.tx(c):
            inq = c.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
            if inq is None or inq["status"] != "open":
                raise ServiceError("استعلام باز پیدا نشد.")
            now = iso(now_utc())
            reason = competitor = None
            comp_rial = None
            if outcome == "lost":
                reason = data.get("lost_reason") or "other"
                if reason not in LOST_REASONS:
                    raise ServiceError("دلیل از دست رفتن را از فهرست انتخاب کنید.")
                competitor = str(data.get("competitor") or "").strip()[:120] or None
                comp_rial = parse_rial(data.get("competitor_price"), data.get("unit") or "toman", "قیمت رقیب",
                                       required=False)
                if comp_rial is not None and not competitor:
                    raise ServiceError("برای قیمت رقیب، نام رقیب را هم بنویسید.")
            if outcome == "won" and str(data.get("create_order", "1")) == "1" and inq["quoted_price_rial"]:
                oid = c.execute("INSERT INTO orders(customer_id, order_date, payment_days, channel, ref, note, created_at,"
                                " created_by) VALUES (?,?,?,?,?,?,?,?)",
                                (inq["customer_id"], _today().isoformat(), inq["payment_days"], inq["channel"], None,
                                 f"از استعلام شماره {inquiry_id}", now, user)).lastrowid
                c.execute("INSERT INTO order_lines(order_id, line_no, product_key, qty, unit_price_rial) VALUES (?,1,?,?,?)",
                          (oid, inq["product_key"], inq["qty"], inq["quoted_price_rial"]))
                out["order_id"] = oid
            c.execute("UPDATE inquiries SET status=?, lost_reason=?, reason_by=?, outcome_note=?, competitor=?,"
                      " competitor_price_rial=?, order_id=?, closed_at=?, closed_by=? WHERE id=?",
                      (outcome, reason, user if reason else None, note, competitor, comp_rial, out["order_id"], now,
                       user, inquiry_id))
            self._audit(c, user, "inquiry_won" if outcome == "won" else "inquiry_lost",
                        {"inquiry": inquiry_id, "reason": reason, "competitor": competitor})
            cust = c.execute("SELECT name FROM customers WHERE id=?", (inq["customer_id"],)).fetchone()["name"]
        if outcome == "lost" and comp_rial is not None and self.market is not None:
            obs = self.market.manual_observation(
                {"seller": competitor, "product_key": inq["product_key"], "price": str(comp_rial), "unit": "rial",
                 "price_level": "wholesale", "min_qty": str(inq["qty"]), "pack_qty": "1", "channel": "phone",
                 "note": f"از استعلام شماره {inquiry_id} مشتری {cust}" + (f": {note}" if note else "")},
                user, role)
            with self.db() as c, db.tx(c):
                c.execute("UPDATE inquiries SET observation_id=? WHERE id=?", (obs, inquiry_id))
            out["observation_id"] = obs
        return out

    def set_lost_reason(self, inquiry_id: int, reason: str, user: str) -> None:
        if reason not in LOST_REASONS:
            raise ServiceError("دلیل نامعتبر است.")
        with self.db() as c, db.tx(c):
            if c.execute("UPDATE inquiries SET lost_reason=?, reason_by=? WHERE id=? AND status='lost'",
                         (reason, user, inquiry_id)).rowcount == 0:
                raise ServiceError("استعلام ازدست‌رفته پیدا نشد.")
            self._audit(c, user, "inquiry_reason", {"inquiry": inquiry_id, "reason": reason})

    def _orders(self, c, where: str = "", args=(), limit: int = 5000) -> list[dict]:
        orders = [dict(r) for r in c.execute(
            "SELECT o.*, cu.name AS customer_name FROM orders o JOIN customers cu ON cu.id=o.customer_id "
            + where + " ORDER BY o.order_date DESC, o.id DESC LIMIT ?", (*args, limit))]
        for o in orders:
            o["lines"] = [dict(r) for r in c.execute(
                "SELECT l.*, p.name AS product_name FROM order_lines l LEFT JOIN products p ON p.key=l.product_key"
                " WHERE l.order_id=? ORDER BY l.line_no", (o["id"],))]
            o["total_rial"] = sum(x["qty"] * x["unit_price_rial"] for x in o["lines"])
        return orders

    def orders(self, limit: int = 300) -> list[dict]:
        with self.db() as c:
            return self._orders(c, limit=limit)

    def create_order(self, data: dict, lines: list[dict], user: str) -> int:
        when = parse_date(data.get("order_date"), _today())
        days = parse_int(data.get("payment_days"), "مهلت پرداخت", 0, 365, default=0)
        channel = data.get("channel") or "phone"
        if channel not in SALES_CHANNELS:
            raise ServiceError("راه ارتباط نامعتبر است.")
        ref = str(data.get("ref") or "").strip()[:60] or None
        clean = []
        with self.db() as c, db.tx(c):
            for ln in lines:
                if not str(ln.get("product_key") or "").strip() and not str(ln.get("qty") or "").strip():
                    continue
                prod = self._product(c, ln.get("product_key"))
                qty = parse_int(ln.get("qty"), f"تعداد {prod['key']}", 1, 1_000_000)
                price = parse_rial(ln.get("unit_price"), data.get("unit") or "rial", f"قیمت واحد {prod['key']}",
                                   required=False) or self.official_prices(c).get(prod["key"])
                if not price:
                    raise ServiceError(f"قیمت واحد {prod['key']} را وارد کنید.")
                clean.append((prod["key"], qty, price))
            if not clean:
                raise ServiceError("دست‌کم یک ردیف کالا لازم است.")
            cid = self._customer_id(c, data, user)
            if ref and c.execute("SELECT 1 FROM orders WHERE customer_id=? AND ref=?", (cid, ref)).fetchone():
                raise ServiceError(f"سفارش با شماره «{ref}» برای این مشتری قبلاً ثبت شده است.")
            oid = c.execute("INSERT INTO orders(customer_id, order_date, payment_days, channel, ref, note, created_at,"
                            " created_by) VALUES (?,?,?,?,?,?,?,?)",
                            (cid, when.isoformat(), days, channel, ref, str(data.get("note") or "").strip()[:500] or None,
                             iso(now_utc()), user)).lastrowid
            for i, (key, qty, price) in enumerate(clean, start=1):
                c.execute("INSERT INTO order_lines(order_id, line_no, product_key, qty, unit_price_rial) VALUES (?,?,?,?,?)",
                          (oid, i, key, qty, price))
            self._audit(c, user, "order_create", {"order": oid, "lines": len(clean)})
            return oid

    def import_orders(self, rows: list[dict], user: str) -> dict:
        groups: dict[tuple, dict] = {}
        for r in rows:
            key = (r["date"], tn.norm(r["customer"]), r.get("ref") or f"row{r['row']}")
            g = groups.setdefault(key, {"data": {"order_date": r["date"].isoformat(), "customer_name": r["customer"],
                                                 "customer_kind": r.get("kind") or "wholesaler",
                                                 "customer_city": r.get("city"), "payment_days": str(r.get("days") or 0),
                                                 "channel": r.get("channel") or "other", "ref": r.get("ref"),
                                                 "note": r.get("note")}, "lines": []})
            g["lines"].append({"product_key": r["product_key"], "qty": str(r["qty"]), "unit_price": str(r["price"])})
        out = {"orders": 0, "lines": 0, "skipped": []}
        for g in groups.values():
            try:
                self.create_order(g["data"], g["lines"], user)
                out["orders"] += 1
                out["lines"] += len(g["lines"])
            except ServiceError as exc:
                out["skipped"].append(f"{g['data']['customer_name']} {g['data'].get('ref') or ''}: {exc}")
        return out

    @staticmethod
    def _reorder_for(orders: list[dict]) -> dict | None:
        dates = sorted({date.fromisoformat(o["order_date"]) for o in orders})
        if len(dates) < 2:
            return None
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if not gaps:
            return None
        typical = int(statistics.median(gaps))
        due = dates[-1] + timedelta(days=typical)
        return {"typical_days": typical, "last": dates[-1].isoformat(), "due": due.isoformat(),
                "days_left": (due - _today()).days}

    def followups(self) -> dict:
        today = _today()
        with self.db() as c:
            due = self._inquiries(c, "WHERE i.status='open' AND i.follow_up_on<=?", (today.isoformat(),))
            customers = [dict(r) for r in c.execute("SELECT * FROM customers WHERE active=1")]
            open_by = {r["customer_id"] for r in c.execute("SELECT customer_id FROM inquiries WHERE status='open'")}
            reorders = []
            for cu in customers:
                orders = self._orders(c, "WHERE o.customer_id=?", (cu["id"],))
                ro = self._reorder_for(orders)
                if ro and ro["days_left"] <= REORDER_WINDOW_DAYS and cu["id"] not in open_by:
                    last = orders[0]
                    reorders.append({"customer": cu, **ro,
                                     "products": sorted({ln["product_key"] for ln in last["lines"]})})
        reorders.sort(key=lambda r: r["days_left"])
        return {"today": today.isoformat(), "inquiries": due, "reorders": reorders}

    def kpis(self, days: int = 90) -> dict:
        since = iso(now_utc() - timedelta(days=days))
        since_d = (_today() - timedelta(days=days)).isoformat()
        with self.db() as c:
            closed = [dict(r) for r in c.execute(
                "SELECT status, lost_reason, quoted_price_rial, competitor_price_rial, created_at, closed_at, product_key"
                " FROM inquiries WHERE status IN ('won','lost') AND closed_at>=?", (since,))]
            n_open = c.execute("SELECT COUNT(*) FROM inquiries WHERE status='open'").fetchone()[0]
            lines = [dict(r) for r in c.execute(
                "SELECT o.order_date, o.customer_id, o.payment_days, cu.name AS customer_name, l.product_key, l.qty,"
                " l.unit_price_rial, p.name AS product_name FROM orders o JOIN order_lines l ON l.order_id=o.id"
                " JOIN customers cu ON cu.id=o.customer_id LEFT JOIN products p ON p.key=l.product_key"
                " WHERE o.order_date>=?", (since_d,))]
        won = sum(1 for r in closed if r["status"] == "won")
        lost = [r for r in closed if r["status"] == "lost"]
        reasons = {}
        for r in lost:
            reasons[r["lost_reason"] or "other"] = reasons.get(r["lost_reason"] or "other", 0) + 1
        gaps = [r["quoted_price_rial"] / r["competitor_price_rial"] - 1 for r in lost
                if r["quoted_price_rial"] and r["competitor_price_rial"]]
        close_days = [(datetime.fromisoformat(r["closed_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds()
                      / 86400 for r in closed if r["closed_at"]]
        by_product: dict[str, dict] = {}
        by_customer: dict[str, dict] = {}
        by_month: dict[str, int] = {}
        cash = credit = 0
        for ln in lines:
            amount = ln["qty"] * ln["unit_price_rial"]
            p = by_product.setdefault(ln["product_key"], {"key": ln["product_key"], "name": ln["product_name"], "qty": 0,
                                                         "rial": 0})
            p["qty"] += ln["qty"]
            p["rial"] += amount
            cu = by_customer.setdefault(ln["customer_name"], {"name": ln["customer_name"], "id": ln["customer_id"],
                                                              "rial": 0, "qty": 0})
            cu["rial"] += amount
            cu["qty"] += ln["qty"]
            jy, jm, _ = jalali.to_jalali(date.fromisoformat(ln["order_date"]))
            label = f"{jy}/{jm:02d}"
            by_month[label] = by_month.get(label, 0) + amount
            if ln["payment_days"]:
                credit += amount
            else:
                cash += amount
        return {"days": days, "closed": len(closed), "won": won, "lost": len(lost), "open": n_open,
                "conversion": (won / len(closed)) if closed else None,
                "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
                "price_gap": statistics.median(gaps) if gaps else None, "n_price_gap": len(gaps),
                "close_days": statistics.median(close_days) if close_days else None,
                "by_product": sorted(by_product.values(), key=lambda x: -x["rial"]),
                "by_customer": sorted(by_customer.values(), key=lambda x: -x["rial"])[:20],
                "by_month": sorted(by_month.items()), "cash_rial": cash, "credit_rial": credit}

    def costs(self) -> dict[str, dict]:
        with self.db() as c:
            return {r["product_key"]: dict(r) for r in c.execute("SELECT * FROM product_costs")}

    def set_cost(self, key: str, raw: str, user: str, note: str = "") -> None:
        from decimal import Decimal, InvalidOperation
        s = tn.latin_digits(raw or "").replace("٫", ".").replace(",", "").strip()
        with self.db() as c, db.tx(c):
            self._product(c, key)
            if not s:
                c.execute("DELETE FROM product_costs WHERE product_key=?", (key,))
                self._audit(c, user, "cost_remove", {"product": key})
                return
            try:
                v = Decimal(s)
            except InvalidOperation:
                raise ServiceError("بهای تمام‌شده را به دلار و با رقم وارد کنید، مثلاً 4.2") from None
            if not v.is_finite() or v <= 0 or v > 10000:
                raise ServiceError("بهای تمام‌شده باید عددی مثبت (دلار برای هر عدد) باشد.")
            c.execute("INSERT INTO product_costs(product_key, cost_usd, note, updated_by, updated_at) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(product_key) DO UPDATE SET cost_usd=excluded.cost_usd, note=excluded.note,"
                      " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                      (key, str(v.normalize()), note.strip()[:200] or None, user, iso(now_utc())))
            self._audit(c, user, "cost_set", {"product": key})

    def refresh_leads(self) -> dict:
        own = [tn.norm(o) for o in self.cfg.market.own_sellers]
        platforms = {tn.norm(p) for p in PLATFORM_NAMES}
        with self.db() as c, db.tx(c):
            rows = c.execute(
                "SELECT e.seller, e.url, e.last_seen_at, e.first_seen_at, e.attrs, s.name AS source_name,"
                " (SELECT o.price_rial FROM observations o WHERE o.external_product_id=e.id AND o.status='ok'"
                "  AND o.kind='price' ORDER BY o.observed_at DESC, o.id DESC LIMIT 1) AS price"
                " FROM external_products e JOIN sources s ON s.id=e.source_id"
                " WHERE e.seller IS NOT NULL AND e.is_own=0 AND s.kind!='manual'").fetchall()
            found: dict[str, dict] = {}
            for r in rows:
                attrs = json.loads(r["attrs"] or "{}")
                if attrs.get("family") != "o2":
                    continue
                key = tn.norm(r["seller"])
                if not key or key in platforms or any(o and o in key for o in own):
                    continue
                f = found.setdefault(key, {"name": r["seller"].strip()[:120], "sources": set(), "n": 0, "url": r["url"],
                                           "first": r["first_seen_at"], "last": r["last_seen_at"], "cities": {},
                                           "prices": []})
                f["sources"].add(r["source_name"])
                if _url_rank(r["url"]) > _url_rank(f["url"]):
                    f["url"] = r["url"]
                f["n"] += 1
                f["first"] = min(f["first"], r["first_seen_at"])
                f["last"] = max(f["last"], r["last_seen_at"])
                if attrs.get("city"):
                    f["cities"][attrs["city"]] = f["cities"].get(attrs["city"], 0) + 1
                if r["price"]:
                    f["prices"].append(r["price"])
            new = 0
            for key, f in found.items():
                kind = "supplier" if any(w in key for w in SUPPLIER_WORDS) else "seller"
                city = max(f["cities"], key=f["cities"].get) if f["cities"] else None
                lo, hi = (min(f["prices"]), max(f["prices"])) if f["prices"] else (None, None)
                row = c.execute("SELECT id, kind, sources, example_url FROM leads WHERE name_norm=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO leads(name, name_norm, sources, n_listings, example_url, kind, city,"
                              " price_min_rial, price_max_rial, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (f["name"], key, json.dumps(sorted(f["sources"]), ensure_ascii=False), f["n"], f["url"],
                               kind, city, lo, hi, f["first"], f["last"]))
                    new += 1
                else:
                    keep = row["kind"] if row["kind"] in ("repair", "manufacturer") else kind
                    srcs = sorted(set(json.loads(row["sources"] or "[]")) | f["sources"])
                    url = f["url"] if _url_rank(f["url"]) > _url_rank(row["example_url"]) else row["example_url"]
                    c.execute("UPDATE leads SET sources=?, n_listings=?, example_url=?, last_seen_at=?, kind=?,"
                              " city=COALESCE(?, city), price_min_rial=?, price_max_rial=? WHERE id=?",
                              (json.dumps(srcs, ensure_ascii=False), f["n"], url, f["last"], keep, city, lo, hi,
                               row["id"]))
            junk = [r["id"] for r in c.execute("SELECT id, name FROM leads WHERE status='new' AND customer_id IS NULL"
                                               " AND COALESCE(phone, '')='' AND COALESCE(note, '')=''")
                    if not any(ch.isalpha() for ch in r["name"] or "")]
            c.executemany("DELETE FROM leads WHERE id=?", [(i,) for i in junk])
        return {"sellers": len(found), "new": new, "removed": len(junk)}

    def leads(self, status: str | None = None, kind: str | None = None, city: str | None = None,
              q: str | None = None, phone: str | None = None) -> list[dict]:
        where, args = [], []
        if phone in PHONE_FILTERS:
            none = "COALESCE(l.phone, '')='' AND COALESCE(cu.phone, '')=''"
            where.append(none if phone == "no" else f"NOT ({none})")
        if status in LEAD_STATUS:
            where.append("l.status=?")
            args.append(status)
        if kind in LEAD_KINDS:
            where.append("l.kind=?")
            args.append(kind)
        if city:
            where.append("l.city=?")
            args.append(city)
        words = tn.norm(q or "")[:60]
        if words:
            digits = re.sub(r"\D", "", words)
            cond = "l.name_norm LIKE ? ESCAPE '\\'"
            args.append(f"%{_like(words)}%")
            if len(digits) >= 4:
                cond = f"({cond} OR REPLACE(l.phone, ' ', '') LIKE ? OR cu.phone LIKE ?)"
                args += [f"%{digits}%", f"%{digits}%"]
            where.append(cond)
        with self.db() as c:
            sql = ("SELECT l.*, cu.phone AS customer_phone FROM leads l LEFT JOIN customers cu ON cu.id=l.customer_id"
                   + (" WHERE " + " AND ".join(where) if where else "")
                   + " ORDER BY l.status='new' DESC, l.n_listings DESC, l.name")
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["sources_l"] = json.loads(r["sources"] or "[]")
            both = tn.phones("، ".join(x for x in (r["customer_phone"], r["phone"]) if x))
            r["phones_l"] = both.split("، ") if both else []
        return rows

    def leads_workbook(self, status: str | None, kind: str | None, city: str | None, user: str,
                       q: str | None = None, phone: str | None = None) -> tuple[bytes, int]:
        from . import leads_xlsx
        rows = self.leads(status, kind, city, q, phone)
        shown = {"status": LEAD_STATUS.get(status), "kind": LEAD_KINDS.get(kind), "city": city or None,
                 "q": (q or "").strip()[:60] or None, "phone": PHONE_FILTERS.get(phone)}
        data = leads_xlsx.build(rows, self.cfg.web.company_name, shown)
        with self.db() as c, db.tx(c):
            self._audit(c, user, "leads_export", {"count": len(rows), "filter": leads_xlsx.filter_text(shown)})
        return data, len(rows)

    def import_lead_phones(self, data: bytes, filename: str, user: str) -> dict:
        from . import leads_xlsx
        found, errors = leads_xlsx.read_phones(data)
        out = {"rows": len(found), "changed": 0, "same": 0, "unknown": 0, "errors": errors}
        with self.db() as c, db.tx(c):
            for lead_id, values in found.items():
                row = c.execute("SELECT l.phone, cu.phone AS customer_phone FROM leads l LEFT JOIN customers cu"
                                " ON cu.id=l.customer_id WHERE l.id=?", (lead_id,)).fetchone()
                if row is None:
                    out["unknown"] += 1
                    continue
                shown = tn.phones("، ".join(x for x in (row["customer_phone"], row["phone"]) if x))
                phone = tn.phones("، ".join(values))
                if phone is None or phone in (shown, row["phone"]):
                    out["same"] += 1
                else:
                    c.execute("UPDATE leads SET phone=?, updated_by=? WHERE id=?", (phone, user, lead_id))
                    out["changed"] += 1
            self._audit(c, user, "leads_phones_import", {"file": filename[:120], "count": len(found),
                                                         "changed": out["changed"]})
        return out

    def lead_cities(self) -> list[tuple[str, int]]:
        with self.db() as c:
            return [(r["city"], r["n"]) for r in c.execute(
                "SELECT city, COUNT(*) AS n FROM leads WHERE city IS NOT NULL GROUP BY city ORDER BY n DESC, city")]

    def set_lead_status(self, lead_id: int, status: str, user: str, note: str = "", phone: str = "") -> None:
        if status not in LEAD_STATUS or status == "customer":
            raise ServiceError("وضعیت نامعتبر است.")
        typed = str(phone or "").strip()
        clean = tn.phones(typed) if typed else None
        if typed and clean is None:
            raise ServiceError(f"شماره تلفن «{typed[:40]}» معتبر نیست؛ مثلاً ۰۹۱۲۳۴۵۶۷۸۹ یا ۰۲۱۶۶۱۲۳۴۵۶ بنویسید.")
        with self.db() as c, db.tx(c):
            row = c.execute("SELECT phone FROM leads WHERE id=?", (lead_id,)).fetchone()
            if row is None:
                raise ServiceError("مشتری احتمالی پیدا نشد.")
            c.execute("UPDATE leads SET status=?, note=COALESCE(?, note), phone=COALESCE(?, phone), updated_by=?"
                      " WHERE id=?", (status, note.strip()[:300] or None, clean, user, lead_id))
            details = {"lead": lead_id, "status": status}
            if clean and clean != row["phone"]:
                details["phone"] = True
            self._audit(c, user, "lead_status", details)

    def lead_to_customer(self, lead_id: int, user: str, kind: str | None = None) -> int:
        with self.db() as c:
            lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if lead is None:
            raise ServiceError("مشتری احتمالی پیدا نشد.")
        kind = kind or LEAD_TO_CUSTOMER.get(lead["kind"], "retailer")
        if lead["customer_id"]:
            return lead["customer_id"]
        with self.db() as c:
            existing = c.execute("SELECT id FROM customers WHERE name_norm=?", (lead["name_norm"],)).fetchone()
        numbers = (lead["phone"] or "").split("، ")
        cid = existing["id"] if existing else self.save_customer(
            {"name": lead["name"], "kind": kind, "channel": "other", "city": lead["city"] or "", "phone": numbers[0],
             "note": f"از {'، '.join(json.loads(lead['sources'] or '[]'))}"
                     + (f"؛ نشانی: {lead['address']}" if lead["address"] else "")
                     + (f"؛ تلفن‌ها: {lead['phone']}" if len(numbers) > 1 else "")}, user, source="lead")
        with self.db() as c, db.tx(c):
            c.execute("UPDATE leads SET status='customer', customer_id=?, updated_by=? WHERE id=?", (cid, user, lead_id))
            self._audit(c, user, "lead_customer", {"lead": lead_id, "customer": cid})
        return cid
