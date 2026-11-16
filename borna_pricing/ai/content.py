from __future__ import annotations

import json
import re
from collections import Counter

from .. import db, fmt
from ..clock import iso, now_utc
from ..market import catalog, textnorm as tn
from ..service import ServiceError
from .gateway import ModelError

PROMPT_V = "listing-1"
FORBIDDEN = re.compile(r"(?<!\w)(اصل|اصلی|اورجینال|اوریجینال|original|genuine|فابریک|شرکتی)(?!\w)", re.I)
STOP = {"سنسور", "اکسیژن", "برای", "مناسب", "با", "و", "از", "به", "در", "سیم", "سوکت", "طرح", "ماشین", "خودرو", "عدد"}
INSTRUCTIONS = (
    "برای فروشگاه اینترنتی یک قطعه یدکی، عنوان و توضیح فارسی بنویس. فقط از واقعیت‌های داده‌شده استفاده کن؛ هیچ "
    "ویژگی، خودرو، گارانتی یا عددی اضافه نکن. کالا ساخت برنا سنسور و «طرح» برند دیگر است؛ هرگز ننویس «اصل»، "
    "«اصلی»، «اورجینال»، «فابریک» یا «شرکتی» و درباره اصل بودن یا نبودن کالا هیچ جمله‌ای ننویس؛ آن جمله را "
    "خودمان اضافه می‌کنیم. عنوان حداکثر ۱۲۰ نویسه و توضیح حداکثر ۹۰۰ نویسه. خروجی: "
    '{"title": "...", "body": "..."}')


def facts(product: dict, prof: dict, moq) -> list[tuple[str, str]]:
    out = []
    if prof.get("ecu"):
        out.append(("سیستم انژکتور (ECU)", catalog.display("ecu", prof["ecu"])))
    if prof.get("generation"):
        out.append(("نسل", "نسل جدید"))
    if prof.get("socket_color"):
        out.append(("رنگ سوکت", catalog.display("socket_color", prof["socket_color"])))
    if prof.get("wire_color"):
        out.append(("رنگ سیم", catalog.display("wire_color", prof["wire_color"])))
    if prof.get("length_mm"):
        out.append(("طول سیم", f"{fmt.fa(round(prof['length_mm'] / 10))} سانتی‌متر"))
    elif prof.get("length_class"):
        out.append(("طول سیم", catalog.display("length_class", prof["length_class"])))
    if prof.get("wire_count"):
        out.append(("تعداد سیم", fmt.fa(prof["wire_count"])))
    if prof.get("position"):
        out.append(("موقعیت نصب", catalog.display("position", prof["position"])))
    if prof.get("part_nos"):
        out.append(("پارت‌نامبر معادل", "، ".join(prof["part_nos"])))
    if prof.get("vehicles"):
        out.append(("خودروهای سازگار", catalog.display("vehicles", prof["vehicles"])))
    if moq:
        out.append(("حداقل سفارش عمده", f"{fmt.fa(moq)} عدد"))
    out.append(("کد کالا", product["key"]))
    return out


def competitor_keywords(titles: list[str], own_title: str, limit: int = 8) -> list[str]:
    own = set(tn.tokens_text(own_title).split())
    count = Counter()
    for t in titles:
        for w in set(tn.tokens_text(t).split()):
            if len(w) > 1 and w not in STOP and w not in own and not FORBIDDEN.fullmatch(w) and not w.isdigit():
                count[w] += 1
    return [w for w, n in count.most_common(limit) if n >= 2]


def disclaimer(prof: dict) -> str:
    design = catalog.display("design", prof["design"]) if prof.get("design") else ""
    if design:
        return f"این سنسور طرح {design} و ساخت برنا سنسور است، نه قطعه اصلی {design}."
    return "این سنسور ساخت برنا سنسور است."


def rule_draft(product: dict, prof: dict) -> tuple[str, str, list[str]]:
    design = catalog.display("design", prof["design"]) if prof.get("design") else ""
    parts = ["سنسور اکسیژن"]
    if prof.get("ecu"):
        parts.append(catalog.display("ecu", prof["ecu"]))
    if design:
        parts.append(f"طرح {design}")
    if prof.get("socket_color"):
        parts.append(f"سوکت {catalog.display('socket_color', prof['socket_color'])}")
    if prof.get("length_mm"):
        parts.append(f"سیم {fmt.fa(round(prof['length_mm'] / 10))} سانت")
    if prof.get("wire_count") == 5:
        parts.append("پنج سیم")
    if prof.get("vehicles"):
        parts.append(catalog.display("vehicles", prof["vehicles"][:2]))
    title = " ".join(parts)
    if prof.get("part_nos"):
        title += f" | {prof['part_nos'][0]}"
    lines = [f"{label}: {value}" for label, value in facts(product, prof, product.get("moq"))]
    flags = [f"{label} نامشخص" for attr, label in (("ecu", "ECU"), ("socket_color", "رنگ سوکت"),
                                                    ("length_mm", "طول سیم"), ("vehicles", "خودروی سازگار"))
             if not prof.get(attr)]
    return title[:120], "\n".join(lines), flags


def generate(ms, gateway, key: str, user: str, use_model: bool = True) -> int:
    with ms.db() as c:
        product = c.execute("SELECT * FROM products WHERE key=? AND active=1", (key,)).fetchone()
        if product is None:
            raise ServiceError("کالا پیدا نشد.")
        titles = [r["title"] for r in c.execute(
            "SELECT e.title FROM matches m JOIN external_products e ON e.id=m.external_product_id"
            " WHERE m.product_key=? AND m.status='approved' AND e.is_own=0", (key,))]
    product = dict(product)
    prof = next((p for p in ms.profiles() if p["key"] == key), None) or catalog.product_profile(product)
    title, body, flags = rule_draft(product, prof)
    keywords = competitor_keywords(titles, title)
    source, model = "rules", None
    if use_model and gateway is not None and gateway.ready:
        data_text = "\n".join([f"عنوان پیشنهادی ساده: {title}"]
                              + [f"{a}: {b}" for a, b in facts(product, prof, product.get("moq"))]
                              + ([f"کلیدواژه‌های پرتکرار آگهی رقبا: {'، '.join(keywords)}"] if keywords else []))
        try:
            res = gateway.complete_json("listing", PROMPT_V, INSTRUCTIONS, data_text, user, _validate)
            if FORBIDDEN.search(res["title"] + " " + res["body"]):
                flags.append("متن مدل ادعای «اصل» بودن داشت و کنار گذاشته شد؛ متن قاعده‌ای جایش آمد.")
            else:
                title, body, source, model = res["title"], res["body"], "model", ms.cfg.llm.model
        except ModelError as exc:
            flags.append(f"مدل جواب نداد: {exc}")
    if keywords:
        flags.append("کلیدواژه‌های رقبا: " + "، ".join(keywords))
    body = body.rstrip() + "\n" + disclaimer(prof)
    with ms.db() as c, db.tx(c):
        did = c.execute("INSERT INTO ai_drafts(kind, product_key, title, body, source, model, prompt_version, flags,"
                        " status, created_at, created_by) VALUES ('listing',?,?,?,?,?,?,?, 'draft', ?, ?)",
                        (key, title, body, source, model, PROMPT_V, json.dumps(flags, ensure_ascii=False),
                         iso(now_utc()), user)).lastrowid
        db.audit(c, iso(now_utc()), user, "draft_create", {"product": key, "draft": did, "source": source})
    return did


def _validate(data: dict) -> dict:
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        raise ModelError("مدل عنوان یا توضیح نداد.")
    return {"title": title[:120], "body": body[:900]}


def drafts(svc, key: str | None = None, limit: int = 200) -> list[dict]:
    with svc.db() as c:
        sql = "SELECT d.*, p.name AS product_name FROM ai_drafts d LEFT JOIN products p ON p.key=d.product_key"
        args: tuple = ()
        if key:
            sql += " WHERE d.product_key=?"
            args = (key,)
        rows = [dict(r) for r in c.execute(sql + " ORDER BY d.id DESC LIMIT ?", (*args, limit))]
    for r in rows:
        r["flags_l"] = json.loads(r["flags"] or "[]")
    return rows


def draft(svc, draft_id: int) -> dict | None:
    with svc.db() as c:
        row = c.execute("SELECT d.*, p.name AS product_name FROM ai_drafts d LEFT JOIN products p"
                        " ON p.key=d.product_key WHERE d.id=?", (draft_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["flags_l"] = json.loads(d["flags"] or "[]")
    return d


def decide(svc, draft_id: int, approve: bool, user: str) -> None:
    with svc.db() as c, db.tx(c):
        d = c.execute("SELECT * FROM ai_drafts WHERE id=? AND status='draft'", (draft_id,)).fetchone()
        if d is None:
            raise ServiceError("پیش‌نویس منتظر تصمیم پیدا نشد.")
        if approve:
            c.execute("UPDATE ai_drafts SET status='discarded', decided_by=?, decided_at=? WHERE product_key=?"
                      " AND status='approved'", (user, iso(now_utc()), d["product_key"]))
        c.execute("UPDATE ai_drafts SET status=?, decided_by=?, decided_at=? WHERE id=?",
                  ("approved" if approve else "discarded", user, iso(now_utc()), draft_id))
        db.audit(c, iso(now_utc()), user, "draft_approve" if approve else "draft_discard",
                 {"product": d["product_key"], "draft": draft_id})
