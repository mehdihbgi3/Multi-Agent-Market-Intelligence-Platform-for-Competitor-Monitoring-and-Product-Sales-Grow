from __future__ import annotations

import re

from .. import fmt
from ..market import matching, textnorm as tn
from .content import FORBIDDEN
from .gateway import ModelError, redact

PROMPT_V = "reply-1"
MAX_OPTIONS = 3
INSTRUCTIONS = (
    "پیش‌نویس جواب کوتاه و مؤدبانه فروشنده یک شرکت قطعه یدکی به پرسش مشتری را بازنویسی کن. فقط از گزینه‌ها و "
    "قیمت‌های داده‌شده استفاده کن و قیمت‌ها را دقیقاً همان‌طور بنویس. هیچ کالا، قیمت، تخفیف یا قولی اضافه نکن و "
    "ننویس «اصل» یا «اورجینال». خروجی: {\"reply\": \"...\"}")
_BIGNUM = re.compile(r"\d[\d٬,]{5,}")


def candidates(question: str, profiles: list[dict], tolerance_mm: int = 30) -> tuple[list, dict]:
    attrs = tn.parse_title("سنسور اکسیژن " + question)
    res = [v for v in matching.match_listing(attrs, profiles, tolerance_mm) if v.verdict != "reject"]
    if res:
        res = [v for v in res if v.score >= res[0].score - 10]
    return res[:MAX_OPTIONS], attrs


def rule_reply(options: list[dict], attrs: dict) -> str:
    if not options:
        return ("سلام، وقت بخیر. برای اینکه سنسور درست را پیشنهاد بدهیم، لطفاً پارت‌نامبر روی سنسور فعلی، مدل خودرو و "
                "نوع سیستم انژکتور (زیمنس، ساژم یا ME17) را بفرمایید. اگر عکس سوکت و سیم سنسور قبلی را بفرستید، "
                "سریع‌تر جواب می‌دهیم.")
    lines = ["سلام، وقت بخیر. با توجه به مشخصاتی که فرمودید این گزینه‌ها را داریم:"]
    for i, o in enumerate(options, start=1):
        price = f"قیمت عمده هر عدد {fmt.money(o['price_rial'] // 10)} تومان" if o.get("price_rial") else "قیمت را استعلام می‌کنیم"
        moq = f"، حداقل {fmt.fa(o['moq'])} عدد" if o.get("moq") else ""
        lines.append(f"{fmt.fa(i)}. {o['name']} (کد {o['key']}) — {price}{moq}")
    ask = []
    if not attrs.get("socket_color"):
        ask.append("رنگ سوکت")
    if not attrs.get("length_mm") and not attrs.get("length_class"):
        ask.append("طول سیم")
    if not attrs.get("part_nos"):
        ask.append("پارت‌نامبر روی سنسور فعلی")
    if ask:
        lines.append(f"برای انتخاب دقیق، لطفاً {'، '.join(ask)} را هم بفرمایید.")
    lines.append("قیمت‌ها طبق لیست رسمی امروز است.")
    return "\n".join(lines)


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[٬,]", "", m) for m in _BIGNUM.findall(tn.latin_digits(text))}


def draft(ms, gateway, question: str, user: str, use_model: bool = True) -> dict:
    q = redact(question or "").strip()[:1500]
    if len(q) < 3:
        return {"error": "متن پرسش مشتری را وارد کنید."}
    profiles = ms.profiles()
    verdicts, attrs = candidates(q, profiles, ms.cfg.market.length_tolerance_mm)
    with ms.db() as c:
        official = ms._official(c)["prices"]
        products = {r["key"]: dict(r) for r in c.execute("SELECT key, name, moq FROM products WHERE active=1")}
    options = [{"key": v.product_key, "name": products[v.product_key]["name"], "moq": products[v.product_key]["moq"],
                "price_rial": official.get(v.product_key), "verdict": v.verdict}
               for v in verdicts if v.product_key in products]
    text = rule_reply(options, attrs)
    out = {"question": q, "options": options, "attrs": attrs, "text": text, "source": "rules", "notes": []}
    if use_model and gateway is not None and gateway.ready and options:
        try:
            res = gateway.complete_json("reply", PROMPT_V, INSTRUCTIONS,
                                        f"پرسش مشتری: {q}\n\nپیش‌نویس با گزینه‌ها و قیمت‌ها:\n{text}", user,
                                        lambda d: {"reply": str(d.get("reply") or "").strip()[:1500]})
            new = res["reply"]
            if not new:
                out["notes"].append("مدل متنی برنگرداند.")
            elif FORBIDDEN.search(new):
                out["notes"].append("متن مدل ادعای «اصل» بودن داشت و کنار گذاشته شد.")
            elif not _numbers(new) <= _numbers(text):
                out["notes"].append("متن مدل عددی داشت که در قیمت‌های رسمی نبود و کنار گذاشته شد.")
            else:
                out["text"], out["source"] = new, "model"
        except ModelError as exc:
            out["notes"].append(f"مدل جواب نداد: {exc}")
    return out
