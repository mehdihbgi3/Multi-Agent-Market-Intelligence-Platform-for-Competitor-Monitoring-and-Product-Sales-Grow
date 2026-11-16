from __future__ import annotations

from datetime import timedelta

from ..clock import iso
from ..market.agents import Agent


class ReasonAnalyst(Agent):
    key = "reason_analyst"
    label = "تحلیلگر دلیل نخریدن"
    description = ("استعلام‌های ازدست‌رفته‌ای را که دلیلشان «سایر» یا خالی مانده می‌خواند و از روی یادداشت فروشنده، "
                   "دلیل نزدیک‌تر را پیشنهاد می‌دهد. اگر مدل زبانی وصل باشد از آن کمک می‌گیرد، وگرنه با کلیدواژه. "
                   "پیشنهاد فقط با تأیید انسان اعمال می‌شود.")
    tools = ("خواندن یادداشت استعلام‌ها (بدون نام و تلفن مشتری)", "ثبت پیشنهاد دلیل")
    level = "L1"

    def _pending(self, ms) -> int:
        with ms.db() as c:
            return c.execute(
                "SELECT COUNT(*) FROM inquiries i WHERE i.status='lost' AND (i.lost_reason IS NULL OR i.lost_reason='other')"
                " AND (COALESCE(i.outcome_note, '') || COALESCE(i.note, '')) != ''"
                " AND NOT EXISTS (SELECT 1 FROM ai_suggestions s WHERE s.kind='lost_reason' AND s.subject_id=i.id)"
            ).fetchone()[0]

    def due(self, ms, now):
        if not self._pending(ms):
            return False
        with ms.db() as c:
            return c.execute("SELECT 1 FROM agent_runs WHERE agent=? AND started_at>=? AND status!='failed' LIMIT 1",
                             (self.key, iso(now - timedelta(hours=12)))).fetchone() is None

    def next_info(self, ms, now):
        n = self._pending(ms)
        return f"{n} استعلام منتظر پیشنهاد دلیل" if n else "استعلامی منتظر نیست."

    def run(self, ms, run_id, user):
        from . import reasons
        st = reasons.run(ms.svc, getattr(ms, "ai", None), user)
        if not st["checked"]:
            st["_status"], st["_message"] = "skipped", "استعلام ازدست‌رفته بی‌دلیلی نبود."
        return st


class ContentWriter(Agent):
    key = "content_writer"
    label = "نویسنده متن آگهی"
    description = ("برای کالاهای برنا که متن آگهی تأییدشده ندارند، پیش‌نویس عنوان و توضیح فروشگاه اینترنتی می‌سازد: "
                   "فقط از مشخصات فنی ثبت‌شده و با ذکر «طرح» بودن کالا. اگر مدل زبانی وصل باشد متن را روان‌تر می‌کند. "
                   "هیچ آگهی‌ای را منتشر نمی‌کند؛ انتشار دستی و پس از تأیید است.")
    tools = ("خواندن کاتالوگ فنی", "خواندن عنوان آگهی‌های تطبیق‌شده رقبا", "ثبت پیش‌نویس")
    level = "L1"

    def due(self, ms, now):
        return False

    def next_info(self, ms, now):
        return "فقط با دکمه «اجرا»"

    def run(self, ms, run_id, user):
        from . import content
        with ms.db() as c:
            keys = [r["key"] for r in c.execute(
                "SELECT key FROM products p WHERE active=1 AND NOT EXISTS (SELECT 1 FROM ai_drafts d"
                " WHERE d.product_key=p.key AND d.status IN ('draft', 'approved')) ORDER BY row_no, key")]
        made = 0
        for key in keys:
            content.generate(ms, getattr(ms, "ai", None), key, user)
            made += 1
        if not made:
            return {"_status": "skipped", "_message": "همه کالاها پیش‌نویس یا متن تأییدشده دارند.", "drafts": 0}
        return {"drafts": made}


AI_AGENTS = {a.key: a for a in (ReasonAnalyst(), ContentWriter())}
