from __future__ import annotations

from .. import fmt
from ..market import textnorm as tn
from ..service import ServiceError
from .service import CUSTOMER_KINDS, SALES_CHANNELS, parse_date, parse_int

SHEET = "سفارش‌ها"
COLUMNS = [
    ("date", "تاریخ (شمسی)", True),
    ("customer", "مشتری", True),
    ("kind", "نوع مشتری", False),
    ("city", "شهر", False),
    ("product_key", "کد کالا", True),
    ("qty", "تعداد", True),
    ("price", "قیمت واحد (ریال)", True),
    ("days", "مهلت پرداخت (روز)", False),
    ("ref", "شماره فاکتور", False),
    ("channel", "کانال", False),
    ("note", "توضیح", False),
]
HELP = [
    "هر ردیف یک کالا از یک سفارش است. ردیف‌هایی که تاریخ، مشتری و شماره فاکتورشان یکی است یک سفارش می‌شوند.",
    "تاریخ را شمسی بنویسید، مثل ۱۴۰۵/۰۸/۱۰. قیمت واحد به ریال است.",
    "کد کالا همان کد سامانه است، مثل FIB101-480 (فهرست در برگه «کالاها»).",
    "مهلت پرداخت برای فروش نقد ۰ است و برای چک تعداد روز تا سررسید (مثلاً ۴۵).",
    "نوع مشتری و کانال اختیاری‌اند؛ مقدارهای مجاز در برگه «کالاها» زیر فهرست کالاها آمده است.",
    "مشتری تازه خودکار ساخته می‌شود. سفارشی که شماره فاکتورش برای همان مشتری قبلاً ثبت شده، دوباره ثبت نمی‌شود.",
]


def template(products: list[dict]) -> bytes:
    from .. import xlsx_style as xs

    wb = xs.book()
    ws = xs.sheet(wb, SHEET, first=True)
    head = [label + (" *" if required else "") for _, label, required in COLUMNS]
    widths = xs.fit_widths(head, [], {}, {1: 26, 10: 30}, 12, 30)
    xs.table(ws, 1, head, [], widths=widths, autofilter=False)
    xs.page(ws, landscape=True)
    xs.help_sheet(wb, "راهنما", [("راهنمای پر کردن فایل سفارش‌ها", "title"), ("", "note")]
                  + [(f"{fmt.fa(i)}. {line}", "note") for i, line in enumerate(HELP, start=1)])
    k = xs.sheet(wb, "کالاها")
    rows = [[p["key"], p["name"]] for p in products]
    res = xs.table(k, 1, ["کد کالا", "نام"], rows, fixed={1: 60}, autofilter=False)
    extra = [["نوع مشتری مجاز", "، ".join(CUSTOMER_KINDS.values())], ["کانال مجاز", "، ".join(SALES_CHANNELS.values())]]
    xs.table(k, res["last"] + 2, ["فهرست", "مقدارهای مجاز"], extra, widths=res["widths"], autofilter=False)
    k.freeze_panes = "A2"
    k.print_title_rows = "1:1"
    xs.page(k)
    wb.active = 0
    return xs.save(wb)


def _label_code(value, mapping: dict) -> str | None:
    s = tn.norm(value or "")
    if not s:
        return None
    for code, label in mapping.items():
        if s in (code, tn.norm(label)) or tn.norm(label).startswith(s):
            return code
    return "other"


def read(path, product_keys: set[str]) -> tuple[list[dict], list[str]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise ServiceError(f"فایل Excel باز نشد: {exc}") from None
    ws = wb[SHEET] if SHEET in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ServiceError("فایل خالی است.")
    header = [tn.norm(str(x or "")).replace("*", "").strip() for x in rows[0]]
    col = {}
    for field, label, _ in COLUMNS:
        want = tn.norm(label)
        for j, h in enumerate(header):
            if h and (h == want or h.startswith(want.split(" (")[0])) and j not in col.values():
                col[field] = j
                break
    missing = [label for field, label, req in COLUMNS if req and field not in col]
    if missing:
        raise ServiceError("ستون‌های لازم پیدا نشد: " + "، ".join(missing))
    out, errors = [], []
    for n, raw in enumerate(rows[1:], start=2):
        vals = {f: (raw[j] if j < len(raw) else None) for f, j in col.items()}
        if all(v in (None, "") for v in vals.values()):
            continue
        try:
            d = vals["date"]
            when = d.date() if hasattr(d, "date") else parse_date(str(d or ""))
            if when is None:
                raise ServiceError("تاریخ خالی است.")
            key = str(vals["product_key"] or "").strip()
            if key not in product_keys:
                raise ServiceError(f"کد کالای «{key}» در سامانه نیست.")
            customer = str(vals["customer"] or "").strip()
            if not customer:
                raise ServiceError("نام مشتری خالی است.")
            item = {"row": n, "date": when, "customer": customer, "product_key": key,
                    "qty": parse_int(str(vals["qty"] if vals["qty"] is not None else ""), "تعداد", 1, 1_000_000),
                    "price": parse_int(str(vals["price"] if vals["price"] is not None else ""), "قیمت واحد", 1),
                    "days": parse_int(str(vals.get("days") if vals.get("days") is not None else ""), "مهلت پرداخت", 0,
                                      365, default=0),
                    "kind": _label_code(vals.get("kind"), CUSTOMER_KINDS) or "wholesaler",
                    "channel": _label_code(vals.get("channel"), SALES_CHANNELS) or "other",
                    "city": str(vals.get("city") or "").strip() or None,
                    "ref": str(vals.get("ref") or "").strip() or None,
                    "note": str(vals.get("note") or "").strip() or None}
            out.append(item)
        except ServiceError as exc:
            errors.append(f"سطر {n}: {exc}")
    if not out and not errors:
        errors.append("هیچ ردیف سفارشی در فایل نیست.")
    return out, errors
