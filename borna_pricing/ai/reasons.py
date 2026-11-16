from __future__ import annotations

import re

from .. import db
from ..clock import iso, now_utc
from ..market import textnorm as tn
from ..sales.service import LOST_REASONS
from .gateway import ModelError

PROMPT_V = "lost-reason-1"
RULES = [
    ("price", r"گران|گرون|قیمت|ارزان|ارزون|تخفیف"),
    ("payment", r"چک|نقد|اعتبار|مدت\s?دار|قسط"),
    ("availability", r"موجود|نداشت|دیر|تحویل|ارسال|انبار"),
    ("quality", r"کیفیت|اصل|اورجینال|برند|خراب|مرجوع|گارانتی|ضمانت"),
    ("moq", r"حداقل|تعداد\s?کم|کم\s?می\s?خواست"),
    ("fitment", r"نخورد|مناسب\s?نبود|جور\s?نبود|سوکت|طول\s?سیم|مدل\s?ماشین"),
    ("no_response", r"جواب\s?نداد|پیگیری\s?نکرد|خبری\s?نشد|منصرف|بی\s?جواب"),
]
INSTRUCTIONS = ("یادداشت فروشنده درباره استعلامی است که به فروش نرسید. نزدیک‌ترین دلیل را فقط از این کدها انتخاب کن: "
                + "، ".join(f"{k} ({v})" for k, v in LOST_REASONS.items())
                + '. خروجی: {"reason": "کد", "confidence": عدد بین 0 و 1}')


def suggest_rules(note: str) -> str | None:
    t = tn.norm(note)
    for code, pat in RULES:
        if re.search(pat, t):
            return code
    return None


def _validate(data: dict) -> dict:
    code = str(data.get("reason") or "")
    if code not in LOST_REASONS:
        raise ModelError("مدل کد دلیل نامعتبر داد.")
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    return {"reason": code, "confidence": conf}


def run(svc, gateway, user: str = "system", limit: int = 50) -> dict:
    with svc.db() as c:
        rows = c.execute(
            "SELECT i.id, COALESCE(i.outcome_note, '') || ' ' || COALESCE(i.note, '') AS text FROM inquiries i"
            " WHERE i.status='lost' AND (i.lost_reason IS NULL OR i.lost_reason='other')"
            " AND NOT EXISTS (SELECT 1 FROM ai_suggestions s WHERE s.kind='lost_reason' AND s.subject_id=i.id)"
            " ORDER BY i.id LIMIT ?", (limit,)).fetchall()
    out = {"checked": len(rows), "suggested": 0, "model": 0, "errors": 0}
    for r in rows:
        text = r["text"].strip()
        if len(text) < 3:
            continue
        code, source, conf = None, "rules", None
        if gateway is not None and gateway.ready:
            try:
                res = gateway.complete_json("lost_reason", PROMPT_V, INSTRUCTIONS, text, user, _validate)
                code, source, conf = res["reason"], "model", str(round(res["confidence"], 2))
                out["model"] += 1
            except ModelError:
                out["errors"] += 1
        if code is None:
            code = suggest_rules(text)
        if code is None or code == "other":
            continue
        with svc.db() as c, db.tx(c):
            c.execute("INSERT OR IGNORE INTO ai_suggestions(kind, subject_id, value, source, confidence, status, created_at)"
                      " VALUES ('lost_reason', ?, ?, ?, ?, 'pending', ?)", (r["id"], code, source, conf, iso(now_utc())))
        out["suggested"] += 1
    return out


def pending(svc) -> dict[int, dict]:
    with svc.db() as c:
        return {r["subject_id"]: dict(r) for r in c.execute(
            "SELECT * FROM ai_suggestions WHERE kind='lost_reason' AND status='pending'")}


def decide(svc, sales, suggestion_id: int, apply: bool, user: str) -> None:
    from ..service import ServiceError
    with svc.db() as c:
        s = c.execute("SELECT * FROM ai_suggestions WHERE id=? AND status='pending'", (suggestion_id,)).fetchone()
    if s is None:
        raise ServiceError("پیشنهاد پیدا نشد.")
    if apply:
        sales.set_lost_reason(s["subject_id"], s["value"], user)
    with svc.db() as c, db.tx(c):
        c.execute("UPDATE ai_suggestions SET status=?, decided_by=?, decided_at=? WHERE id=?",
                  ("applied" if apply else "dismissed", user, iso(now_utc()), suggestion_id))
