from __future__ import annotations

from . import fmt, jalali, xlsx_style as xs
from .clock import TEHRAN, parse_iso


def filename(pub: dict) -> str:
    t = parse_iso(pub["published_at"]).astimezone(TEHRAN)
    y, m, d = jalali.to_jalali(t.date())
    return f"Borna_PriceList_{y}{m:02d}{d:02d}_{t:%H%M}_no{pub['id']}.xlsx"


def build_xlsx(pub: dict, cfg) -> bytes:
    wb = xs.book()
    ws = xs.sheet(wb, "لیست قیمت", first=True)
    head = ["ردیف", "نام محصول", "پارت‌نامبر", "حداقل سفارش", "قیمت هر عدد (ریال)", "قیمت هر عدد (تومان)"]
    body = [[row["row_no"], row["name"], row["part_no"] or None, row["moq"], row["price_rial"], row["price_rial"] // 10]
            for row in pub["rows"]]
    formats = {4: xs.MONEY, 5: xs.MONEY}
    widths = xs.fit_widths(head, body, formats, {0: 7}, 9, 52)
    pub_t = parse_iso(pub["published_at"])
    vu = pub.get("valid_until_eff") or parse_iso(pub["valid_until"])
    lines = [(f"{cfg.web.company_name} — {cfg.web.pricelist_title}", "title"),
             (f"تاریخ: {fmt.jdate_long(pub_t)} — ساعت {fmt.hm(pub_t)}    (لیست شماره {fmt.fa(pub['id'])})", "sub"),
             (f"اعتبار: {cfg.web.validity_text} ({fmt.weekday(vu)} {fmt.jdate(vu)} ساعت {fmt.hm(vu)})", "note")]
    if cfg.web.show_rate_on_pricelist:
        lines.append((f"مبنای نرخ دلار: {fmt.money(pub['rate_rial'] // 10)} تومان", "note"))
    top = xs.title_rows(ws, lines, len(head), widths) + 1
    res = xs.table(ws, top, head, body, formats=formats, widths=widths, bold_cols=(4,), center_cols=(0, 2, 3),
                   autofilter=False)
    notes = []
    if cfg.web.footer_note:
        notes.append((cfg.web.footer_note, "note"))
    notes.append(("قیمت‌ها برای یک عدد است، بر پایه نرخ روز دلار محاسبه شده و فقط در مدت اعتبار بالا معتبر است.",
                  "small"))
    xs.note_rows(ws, res["last"] + 2, notes, len(head), widths)
    xs.page(ws)
    return xs.save(wb)
