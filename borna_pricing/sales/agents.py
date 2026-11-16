from __future__ import annotations

from datetime import datetime, timedelta

from ..clock import iso, parse_iso
from ..market.agents import Agent


def _ran_since(ms, key: str, since: datetime) -> bool:
    with ms.db() as c:
        return c.execute("SELECT 1 FROM agent_runs WHERE agent=? AND started_at>=? AND status!='failed' LIMIT 1",
                         (key, iso(since))).fetchone() is not None


class PricingAdvisor(Agent):
    key = "pricing_advisor"
    label = "مشاور قیمت"
    description = ("بعد از هر گزارش هفتگی، قیمت رسمی هر کالا را با میانه قیمت عمده رقبای هم‌طرح و کف قیمت (اگر بهای "
                   "تمام‌شده ثبت شده باشد) مقایسه می‌کند و تغییر قیمت پایه را پیشنهاد می‌دهد. هیچ قیمتی را عوض "
                   "نمی‌کند؛ پیشنهاد فقط از مسیر Excel کالاها و تأیید عادی اعمال می‌شود.")
    tools = ("خواندن قیمت‌های تأییدشده رقبا", "خواندن بهای تمام‌شده", "ساخت پیشنهاد")
    level = "L2"

    def _last(self, ms, kind):
        with ms.db() as c:
            row = c.execute("SELECT MAX(created_at) AS t FROM reports WHERE kind=?", (kind,)).fetchone()
        return parse_iso(row["t"]) if row and row["t"] else None

    def due(self, ms, now):
        if getattr(ms, "sales", None) is None:
            return False
        weekly = self._last(ms, "weekly")
        mine = self._last(ms, "advisor")
        return weekly is not None and (mine is None or mine < weekly)

    def next_info(self, ms, now):
        return "بعد از گزارش هفتگی بعدی"

    def run(self, ms, run_id, user):
        if getattr(ms, "sales", None) is None:
            return {"_status": "skipped", "_message": "بخش فروش راه‌اندازی نشده است."}
        from . import advisor
        data = advisor.proposals(ms, ms.sales)
        rid = ms.save_report("advisor", data, user, run_id)
        return {"report_id": rid, "changes": data["n_changes"], "products": len(data["items"])}


class LeadFinder(Agent):
    key = "lead_finder"
    label = "یابنده مشتری عمده"
    description = ("فروشندگانی را که در منبع‌های پایش‌شده سنسور اکسیژن می‌فروشند، به‌عنوان خریدار احتمالی عمده "
                   "فهرست می‌کند تا فروش با آن‌ها تماس بگیرد. فقط از آگهی‌های عمومی همان منبع‌های مجاز استفاده "
                   "می‌کند و هیچ پیامی نمی‌فرستد.")
    tools = ("خواندن آگهی‌های جمع‌شده", "ثبت مشتری احتمالی")
    level = "L1"

    def due(self, ms, now):
        if getattr(ms, "sales", None) is None:
            return False
        with ms.db() as c:
            row = c.execute("SELECT MAX(first_seen_at) AS t FROM external_products e JOIN sources s ON s.id=e.source_id"
                            " WHERE s.kind!='manual'").fetchone()
        if not row or not row["t"]:
            return False
        return not _ran_since(ms, self.key, parse_iso(row["t"])) and not _ran_since(ms, self.key, now - timedelta(hours=6))

    def next_info(self, ms, now):
        return "بعد از پیدا شدن آگهی تازه"

    def run(self, ms, run_id, user):
        if getattr(ms, "sales", None) is None:
            return {"_status": "skipped", "_message": "بخش فروش راه‌اندازی نشده است."}
        return ms.sales.refresh_leads()


SALES_AGENTS = {a.key: a for a in (PricingAdvisor(), LeadFinder())}
RETIRED_AGENTS = {"sales_assistant": "دستیار فروش (حذف‌شده)"}
