from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from borna_pricing import clock, config, db, pricelist, xlsx_style as xs
from borna_pricing.clock import TEHRAN
from borna_pricing.market import report, scout
from borna_pricing.market.service import MarketService
from borna_pricing.market.sites import Business
from borna_pricing.sales import advisor, leads_xlsx, orders_xlsx
from borna_pricing.sales.service import SalesService
from borna_pricing.service import Service

from conftest import FakeWeb, fake_fetcher_factory, write_settings

LONG_SELLER = "فروشگاه بزرگ قطعات یدکی و لوازم برقی خودروی ایرانی و خارجی برادران محمدی و شرکا"
LONG_TAIL = "با گارانتی شش ماهه و ارسال رایگان به سراسر کشور، مناسب تعمیرگاه‌ها و فروشگاه‌های لوازم یدکی خودرو"


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("xlsx")
    cfg = config.load_config(write_settings(tmp, "[advisor]\nmin_prices = 1\n"))
    svc = Service(cfg)
    clock.set_fixed_now(datetime(2026, 11, 8, 11, 0, tzinfo=TEHRAN))
    svc.init()
    ms = MarketService(svc, fake_fetcher_factory(FakeWeb({})))
    sales = SalesService(svc, ms)
    ms.sales = sales
    products = svc.products(include_inactive=False)
    keys = [p["key"] for p in products]
    for i, prod in enumerate(products[:8]):
        key = prod["key"]
        for j, level in enumerate(("wholesale", "retail", "retail")):
            ms.manual_observation({"seller": f"{LONG_SELLER} {i}{j}", "title": f"{prod['name']} {LONG_TAIL} {j}",
                                   "price": str(2_000_000 + 37_000 * i + 11_000 * j), "unit": "toman",
                                   "price_level": level, "product_key": key, "note": "تماس تلفنی با فروشنده"},
                                  "admin", "admin")
    with ms.db() as c, db.tx(c):
        scout.upsert_businesses(c, [
            Business(name=f"{LONG_SELLER} شعبه {n}", kind=kind, city="تهران",
                     url=f"https://www.openstreetmap.org/node/{n}", phone="02166123456",
                     address="تهران، خیابان امام خمینی، بین چهارراه حسن‌آباد و میدان امام، پاساژ بزرگ قطعات، طبقه دوم")
            for n, kind in enumerate(("repair", "supplier", "seller", "repair"))], "نقشه", "2026-11-01T10:00:00+00:00")
    rid = ms.build_report("admin")
    sales.set_cost(keys[0], "9.5", "admin")
    pub = svc.publication(None)
    return {
        "pricelist": pricelist.build_xlsx(pub, cfg),
        "weekly": report.xlsx(ms.report(rid), cfg.web.company_name),
        "advisor": advisor.xlsx(advisor.proposals(ms, sales), cfg.web.company_name),
        "orders": orders_xlsx.template(svc.products(include_inactive=False)),
        "leads": leads_xlsx.build(sales.leads(), cfg.web.company_name, {}),
    }


def merged_width(ws, rng) -> float:
    return sum(ws.column_dimensions[get_column_letter(j)].width or 8.43
               for j in range(rng.min_col, rng.max_col + 1))


def check_sheet(ws) -> list[str]:
    problems = []
    if ws.sheet_view.showGridLines is False:
        problems.append(f"{ws.title}: gridlines hidden")
    ranges = list(ws.merged_cells.ranges)
    for a in range(len(ranges)):
        for b in range(a + 1, len(ranges)):
            if not (ranges[a].max_row < ranges[b].min_row or ranges[b].max_row < ranges[a].min_row
                    or ranges[a].max_col < ranges[b].min_col or ranges[b].max_col < ranges[a].min_col):
                problems.append(f"{ws.title}: merged {ranges[a]} overlaps {ranges[b]}")
    starts = {(r.min_row, r.min_col): r for r in ranges}
    covered = {(row, col) for r in ranges for row in range(r.min_row, r.max_row + 1)
               for col in range(r.min_col, r.max_col + 1)} - set(starts)
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None or (cell.row, cell.column) in covered:
                continue
            rng = starts.get((cell.row, cell.column))
            width = merged_width(ws, rng) if rng else (ws.column_dimensions[cell.column_letter].width or 8.43)
            text = xs.shown(cell.value, cell.number_format)
            size = (cell.font.sz or 11) if cell.font else 11
            bold = bool(cell.font and cell.font.b)
            numeric = isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
            where = f"{ws.title}!{cell.coordinate}"
            if numeric:
                if xs.text_width(text, size, bold) > width - 1.0:
                    problems.append(f"{where}: number {text} wider than column {width}")
                continue
            if not (cell.alignment and cell.alignment.wrap_text):
                if xs.text_width(text, size, bold) > width - 1.0:
                    problems.append(f"{where}: text spills out ({len(text)} chars, width {width})")
                continue
            need = xs.lines_needed(text, width, size, bold)
            height = ws.row_dimensions[cell.row].height or 15
            if height < need * 12.0 * size / 10:
                problems.append(f"{where}: {need} lines need more than row height {height}")
    return problems


@pytest.mark.parametrize("name", ["pricelist", "weekly", "advisor", "orders", "leads"])
def test_excel_layout_has_grid_and_no_overflow(files, name):
    wb = load_workbook(BytesIO(files[name]))
    problems = [p for ws in wb.worksheets for p in check_sheet(ws)]
    assert problems == []
    for ws in wb.worksheets:
        assert ws.sheet_view.rightToLeft
        head = next((r for r in ws.iter_rows() if r[0].fill and r[0].fill.fgColor.rgb in ("0017202B", "FF17202B")), None)
        if head is None:
            continue
        top = head[0].row
        ncols = len([c for c in head if c.value is not None])
        for row in ws.iter_rows(min_row=top, max_row=min(ws.max_row, top + 40)):
            if all(c.value is None for c in row[:ncols]) and row[0].row > top:
                break
            for cell in row[:ncols]:
                assert cell.border.left.style == "thin" and cell.border.bottom.style == "thin", cell.coordinate


def test_title_rows_are_merged_across_the_table(files):
    for name in ("pricelist", "weekly", "advisor"):
        wb = load_workbook(BytesIO(files[name]))
        ws = wb.worksheets[0] if name != "weekly" else wb["کالاها"]
        head = next(r for r in ws.iter_rows() if r[0].value in ("ردیف", "کد"))
        ncols = len([c for c in head if c.value is not None])
        first = {str(m) for m in ws.merged_cells.ranges}
        assert f"A1:{get_column_letter(ncols)}1" in first, name
        assert ws.cell(row=1, column=1).alignment.wrap_text


def test_text_width_and_line_estimates():
    assert xs.text_width("") == 0 and xs.lines_needed("", 10) == 1
    assert xs.lines_needed("کلمه " * 40, 20) > xs.lines_needed("کلمه " * 10, 20) > 1
    assert xs.lines_needed("abc\nabc", 30) == 2
    assert xs.shown(12_345_678, xs.MONEY) == "12,345,678" and xs.shown(-0.125, xs.PCT) == "-12.5%"
