from __future__ import annotations

import urllib.parse
from io import BytesIO

from .. import fmt
from ..clock import now_utc
from ..market import textnorm as tn
from ..service import ServiceError
from .service import LEAD_KINDS, LEAD_STATUS, link_label

COLUMNS = [("ردیف", 6), ("نام", 30), ("نوع", 18), ("شهر", 12), ("نشانی", 40), ("تلفن", 26), ("وضعیت", 13),
           ("یادداشت", 26), ("منبع", 22), ("تعداد آگهی", 9), ("کمترین قیمت (تومان)", 14), ("بیشترین قیمت (تومان)", 14),
           ("لینک", 17), ("اولین بار دیده‌شده", 13), ("آخرین بار دیده‌شده", 13), ("شناسه (تغییر ندهید)", 11)]
ID_HEADER = "شناسه"
PHONE_HEADER = "تلفن"
PRINTED = "H"
FROZEN = "E2"
KIND_SHEETS = {"seller": "فروشندگان", "supplier": "تأمین‌کنندگان", "repair": "تعمیرگاه‌ها", "manufacturer": "سازندگان"}
KIND_COLORS = {"seller": "DCE8F8", "supplier": "D5F1E6", "repair": "FCE2D7", "manufacturer": "FCEFCC"}
TAB_COLORS = {"seller": "2A78D6", "supplier": "1BAF7A", "repair": "EB6834", "manufacturer": "EDA100"}
COLOR_NAMES = {"seller": "آبی", "supplier": "سبزآبی", "repair": "نارنجی", "manufacturer": "زرد"}
FILTERS = (("status", "وضعیت"), ("kind", "نوع"), ("city", "شهر"), ("phone", "تلفن"), ("q", "جست‌وجو"))
MAX_LINKS = 60_000
MAX_BYTES = 20 * 1024 * 1024
HELP = [
    "برگه «همه» همه کسب‌وکارها را به ترتیب نوع دارد: اول فروشندگان، بعد تأمین‌کنندگان، بعد تعمیرگاه‌ها. هر نوع برگه "
    "جدای خودش را هم دارد (زبانه‌های پایین پنجره، به همان رنگ).",
    "ستون‌های «ردیف»، «نام»، «نوع» و «شهر» ثابت‌اند و وقتی فایل را به چپ و راست می‌برید دیده می‌مانند. با فیلتر سر "
    "ستون‌ها می‌توانید بر پایه شهر، وضعیت یا داشتن تلفن جدا کنید.",
    "روی نوشته ستون «لینک» کلیک کنید تا آگهی، صفحه کالا، سایت فروشگاه یا جای آن روی نقشه باز شود.",
    "تلفن از سه جا می‌آید: نقشه اوپن‌استریت‌مپ، پرونده مشتری، و شماره‌ای که تیم فروش ثبت می‌کند. سامانه شماره "
    "آگهی‌دهندگان دیوار و فروشندگان ترب، ایمالز، دیجی‌کالا و باسلام را خودکار برنمی‌دارد، چون این سایت‌ها شماره را "
    "فقط بعد از زدن «اطلاعات تماس» و جدا برای هر کسب‌وکار نشان می‌دهند.",
    "ثبت تلفن با همین فایل: لینک ردیف را باز کنید، «اطلاعات تماس» را بزنید و شماره را در ستون «تلفن» همان ردیف بنویسید. "
    "بعد فایل را در صفحه «کسب‌وکارها و مشتریان احتمالی» با «بارگذاری تلفن‌ها» به سامانه بدهید تا در صفحه و فایل‌های "
    "بعدی بیاید. ستون «شناسه» را تغییر ندهید؛ مرتب کردن یا پاک کردن ردیف‌ها اشکالی ندارد.",
    "نشانی و تلفن‌های نقشه را داوطلب‌ها ثبت کرده‌اند؛ پیش از مراجعه یک بار تلفنی تأیید کنید.",
    "قیمت‌ها به تومان و از آخرین قیمت آگهی‌های همان فروشنده است. تاریخ‌ها شمسی است.",
    "در چاپ، ستون‌های «ردیف» تا «یادداشت» افقی و هم‌عرض کاغذ چاپ می‌شود و سطر عنوان در هر صفحه تکرار می‌شود.",
]


def filter_text(shown: dict) -> str:
    return "، ".join(f"{label}: {shown[k]}" for k, label in FILTERS if shown.get(k)) or "همه"


def _toman(rial):
    return None if rial is None else round(rial / 10)


def _day(value):
    return tn.latin_digits(fmt.jdate(value)) if value else None


def _values(i: int, r: dict) -> list:
    return [i, r["name"], LEAD_KINDS.get(r["kind"], r["kind"]), r["city"], r["address"],
            "، ".join(r["phones_l"]) or None, LEAD_STATUS.get(r["status"], r["status"]), r["note"],
            "، ".join(r["sources_l"]) or None, r["n_listings"], _toman(r["price_min_rial"]),
            _toman(r["price_max_rial"]), r["example_url"], _day(r["first_seen_at"]), _day(r["last_seen_at"]), r["id"]]


def link_cell(cell, url: str | None, allowed: bool = True) -> None:
    from openpyxl.styles import Alignment, Font

    if not url:
        cell.value = None
        return
    target = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    if allowed and len(target) <= 2000:
        cell.value = link_label(url) or "باز کردن"
        cell.hyperlink = target
        cell.font = Font(color="0563C1", underline="single")
    else:
        cell.value = url
        cell.alignment = Alignment(wrap_text=True, vertical="center")


WRAP_WIDTHS = {1: 32, 4: 42, 7: 28}


def _sheet(wb, title: str, rows: list[dict], kind: str | None = None, first: bool = False):
    from openpyxl.styles import PatternFill

    from .. import xlsx_style as xs

    ws = xs.sheet(wb, title, first=first, tab=TAB_COLORS[kind] if kind else None)
    head = [label for label, _ in COLUMNS]
    urls = []
    body = []
    for i, r in enumerate(rows, start=1):
        vals = _values(i, r)
        urls.append(vals[12])
        vals[12] = (link_label(vals[12]) or "باز کردن") if vals[12] else None
        body.append(vals)
    fixed = {j: w for j, w in WRAP_WIDTHS.items()}
    widths = xs.fit_widths(head, body, {10: xs.MONEY, 11: xs.MONEY}, fixed, 7, 30)
    fills = {(i, 2): PatternFill("solid", fgColor=KIND_COLORS[r["kind"]]) for i, r in enumerate(rows)
             if r["kind"] in KIND_COLORS}
    res = xs.table(ws, 1, head, body, formats={5: xs.TEXT, 10: xs.MONEY, 11: xs.MONEY}, widths=widths,
                   bold_cols=(1, 2), center_cols=(0, 9), fills=fills, freeze_cols=4)
    for i, url in enumerate(urls):
        if url:
            link_cell(ws.cell(row=res["top"] + 1 + i, column=13), url, i < MAX_LINKS)
    ws.freeze_panes = FROZEN
    xs.page(ws, landscape=True)
    ws.print_title_rows = "1:1"
    ws.print_area = f"A1:{PRINTED}{len(rows) + 1}"
    return ws


def build(rows: list[dict], company: str, shown: dict) -> bytes:
    from .. import xlsx_style as xs

    order = list(KIND_SHEETS)
    grouped = sorted(rows, key=lambda r: order.index(r["kind"]) if r["kind"] in order else len(order))
    kinds = [k for k in order if any(r["kind"] == k for r in rows)]
    wb = xs.book()
    _sheet(wb, "همه", grouped, first=True)
    if len(kinds) > 1:
        for k in kinds:
            _sheet(wb, KIND_SHEETS[k], [r for r in rows if r["kind"] == k], k)
    counts = [f"{LEAD_KINDS[k]}: {fmt.fa(sum(1 for r in rows if r['kind'] == k))}" for k in kinds]
    colors = "، ".join(f"{COLOR_NAMES[k]} {LEAD_KINDS[k]}" for k in kinds)
    lines = [(f"فهرست کسب‌وکارها و مشتریان احتمالی — {company}", "title"),
             (f"تهیه‌شده در {fmt.jdatetime(now_utc())}", "note"), (f"فیلتر: {filter_text(shown)}", "note"), ("", "note"),
             (f"همه: {fmt.fa(len(rows))}؛ با تلفن: {fmt.fa(sum(1 for r in rows if r['phones_l']))}؛ "
              f"با نشانی: {fmt.fa(sum(1 for r in rows if r['address']))}", "sub"),
             ("؛ ".join(counts), "note"), (f"رنگ ستون «نوع»: {colors}" if colors else "", "note"), ("", "note"),
             ("راهنما", "sub")] + [(f"{fmt.fa(i)}. {line}", "note") for i, line in enumerate(HELP, start=1)]
    xs.help_sheet(wb, "راهنما", lines)
    wb.active = 0
    return xs.save(wb)


def _int(value) -> int | None:
    text = tn.latin_digits(str(value if value is not None else "")).strip()
    try:
        n = int(float(text))
    except ValueError:
        return None
    return n if n > 0 else None


def _phone_text(value) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        digits = str(int(value))
        return "0" + digits if len(digits) == 10 and not digits.startswith("0") else digits
    return str(value).strip()


def read_phones(data: bytes) -> tuple[dict[int, list[str]], list[str]]:
    from openpyxl import load_workbook

    if not data:
        raise ServiceError("فایلی انتخاب نشده است.")
    if len(data) > MAX_BYTES:
        raise ServiceError("حجم فایل بیش از ۲۰ مگابایت است.")
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ServiceError("فایل Excel باز نشد؛ همان فایلی را بدهید که «دریافت Excel» ساخته است.") from None
    found: dict[int, list[str]] = {}
    errors: list[str] = []
    sheets = 0
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            names = [tn.norm(str(x or "")) for x in (next(rows, None) or ())]
            id_col = next((j for j, h in enumerate(names) if h.startswith(ID_HEADER)), None)
            ph_col = next((j for j, h in enumerate(names) if h == PHONE_HEADER), None)
            if id_col is None or ph_col is None:
                continue
            sheets += 1
            for n, row in enumerate(rows, start=2):
                raw = row[ph_col] if ph_col < len(row) else None
                if raw is None or not str(raw).strip():
                    continue
                lead_id = _int(row[id_col] if id_col < len(row) else None)
                text = _phone_text(raw)
                phone = tn.phones(text)
                if lead_id is None:
                    errors.append(f"برگه «{ws.title}» سطر {n}: شناسه ندارد.")
                elif phone is None:
                    errors.append(f"برگه «{ws.title}» سطر {n}: «{text[:30]}» شماره تلفن نیست.")
                elif phone not in found.setdefault(lead_id, []):
                    found[lead_id].append(phone)
    finally:
        wb.close()
    if not sheets:
        raise ServiceError("ستون‌های «شناسه» و «تلفن» در فایل نیست؛ همان فایلی را بدهید که «دریافت Excel» ساخته است.")
    return found, errors
