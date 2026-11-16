from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..market import matching, prices, textnorm as tn
from .gateway import ModelError

PROMPT_V = "price-list-1"
INSTRUCTIONS = (
    "تو لیست قیمت لوازم یدکی خودرو را که از یک کانال پیام‌رسان کپی شده می‌خوانی. برای هر کالا یک مورد بساز: "
    "title (نام کالا همان‌طور که در متن است)، price (عدد قیمت همان‌طور که در متن آمده، بدون واحد؛ اگر قیمت ندارد "
    "خالی) و available (اگر نوشته ناموجود false، اگر موجود true، وگرنه null). خروجی دقیقاً به این شکل: "
    '{"items": [{"title": "...", "price": "...", "available": null}]}')
_NUM = re.compile(r"(?<![\w\-./])(?:\d{1,3}(?:[,٬.\s]\d{3})+(?!\d)|\d+(?:[.٫/]\d+)?)(?![\w\-]|\s*(?:سانت|سانتی|cm|mm|میلی|سیم)(?!\w))")
_NOT_PRICE_WORDS = re.compile(r"ناموجود|موجود\s?نیست|تماس|استعلام|توافقی")
_TRAIL = re.compile(r"[\s:：\-–—=…\.،,|]+$")
_THOUSAND_HINT = re.compile(r"هزار\s*تومان|به\s*هزار|ارقام.{0,12}هزار|\(هزار")


def suggest_multiplier(text: str) -> int:
    return 1000 if _THOUSAND_HINT.search(tn.norm(text)) else 1


def _to_rial(num_text: str, unit: str, multiplier: int) -> int | None:
    s = num_text.strip()
    if re.fullmatch(r"\d{1,3}(?:[,٬.\s]\d{3})+", s):
        s = re.sub(r"[,٬.\s]", "", s)
    else:
        s = s.replace("٫", ".").replace("/", ".")
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    if v < 100_000:
        v *= multiplier
    if v <= 0 or v != v.to_integral_value():
        return None
    v = int(v)
    return v * 10 if unit == "toman" else v


def _row(title: str, price_text: str, rial: int | None, availability: str, source: str) -> dict:
    attrs = tn.parse_title(title)
    return {"title": title[:300], "price_text": price_text, "price_rial": rial,
            "kind": "price" if rial else "quote_only", "availability": availability, "attrs": attrs,
            "source": source, "product_key": None, "verdict": None, "include": attrs["family"] == "o2"}


def parse_rules(text: str, unit: str = "toman", multiplier: int = 1) -> list[dict]:
    rows = []
    for raw in (text or "").splitlines():
        line = tn.latin_digits(raw).strip()
        if len(line) < 4:
            continue
        nums = [m for m in _NUM.finditer(line)
                if not (re.fullmatch(r"\d{9,}", m.group(0)) or re.fullmatch(r"0\d{5,}", m.group(0)))]
        avail = prices.detect_availability(line)
        if nums:
            tail = _NOT_PRICE_WORDS.search(line, nums[-1].end())
            if tail:
                nums = []
        if not nums:
            if avail == "out_of_stock" or re.search(r"تماس|استعلام", line):
                title = _TRAIL.sub("", re.sub(r"ناموجود|موجود\s?نیست|تماس\s?بگیرید|استعلام|توافقی", "", line)).strip()
                if len(title) >= 3:
                    rows.append(_row(title, "", None, avail, "rules"))
            continue
        last = nums[-1]
        title = _TRAIL.sub("", (line[: last.start()] + " " + line[last.end():]).strip())
        title = re.sub(r"(?<!\w)(?:تومان|تومن|ریال|ت)\s*$", "", title).strip()
        title = _TRAIL.sub("", title)
        if len(title) < 3 or not re.search(r"[^\d\s]", title):
            continue
        rows.append(_row(title, last.group(0), _to_rial(last.group(0), unit, multiplier), avail, "rules"))
    return rows


def _validate(data: dict) -> dict:
    items = data.get("items")
    if not isinstance(items, list):
        raise ModelError("پاسخ مدل فهرست items نداشت.")
    out = []
    for it in items[:200]:
        if not isinstance(it, dict) or not str(it.get("title") or "").strip():
            continue
        price = str(it.get("price") or "").strip()
        if price and not re.fullmatch(r"[\d۰-۹٠-٩,٬.\s/٫]+", price):
            continue
        avail = it.get("available")
        out.append({"title": str(it["title"]).strip()[:300], "price": price,
                    "available": avail if avail in (True, False, None) else None})
    return {"items": out}


def parse_model(gateway, text: str, unit: str, multiplier: int, user: str) -> list[dict]:
    data = gateway.complete_json("price_list", PROMPT_V, INSTRUCTIONS, text, user, _validate)
    source_text = tn.latin_digits(text)
    rows = []
    for it in data["items"]:
        price = tn.latin_digits(it["price"])
        if price and price.replace(" ", "") not in source_text.replace(" ", ""):
            continue
        avail = {True: "in_stock", False: "out_of_stock"}.get(it["available"], "unknown")
        rows.append(_row(it["title"], price, _to_rial(price, unit, multiplier) if price else None, avail, "model"))
    return rows


def attach_matches(rows: list[dict], profiles: list[dict], tolerance_mm: int = 30) -> list[dict]:
    owners = matching.pn_owners(profiles)
    for r in rows:
        res = [v for v in matching.match_listing(r["attrs"], profiles, tolerance_mm, owners) if v.verdict != "reject"]
        if res and res[0].verdict in ("strong", "candidate") and (len(res) == 1 or res[1].verdict not in ("strong", "candidate")):
            r["product_key"], r["verdict"] = res[0].product_key, res[0].verdict
        elif res:
            r["verdict"] = "review"
            r["options"] = [v.product_key for v in res[:5]]
    return rows
