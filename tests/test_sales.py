import json
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook

from borna_pricing import clock, config, db
from borna_pricing.clock import TEHRAN
from borna_pricing.market import scout
from borna_pricing.market import textnorm as tn
from borna_pricing.market.service import MarketService
from borna_pricing.market.sites import Business
from borna_pricing.sales import advisor, credit, orders_xlsx
from borna_pricing.sales.service import SalesService, parse_date
from borna_pricing.service import Service, ServiceError

from conftest import FIXTURES, FakeWeb, fake_fetcher_factory, write_settings

LIST_URL = "https://shop.example.ir/cat/o2"
ITEMLIST = (FIXTURES / "market" / "synthetic_itemlist.html").read_text(encoding="utf-8")


def at(y, m, d, hh=10, mm=0):
    clock.set_fixed_now(datetime(y, m, d, hh, mm, tzinfo=TEHRAN))


@pytest.fixture
def env(tmp_path):
    def make(extra=""):
        cfg = config.load_config(write_settings(tmp_path, extra))
        svc = Service(cfg)
        at(2026, 10, 31, 13, 30)
        svc.init()
        web = FakeWeb({LIST_URL: (200, "text/html; charset=utf-8", ITEMLIST)})
        ms = MarketService(svc, fake_fetcher_factory(web))
        sales = SalesService(svc, ms)
        ms.sales = sales
        return svc, ms, sales
    return make


def test_dates_and_customers(env):
    _, _, sales = env()
    assert parse_date("۱۴۰۵/۰۸/۱۰") == date(2026, 11, 1)
    assert parse_date("2026-11-01") == date(2026, 11, 1)
    with pytest.raises(ServiceError):
        parse_date("1405/13/01")
    cid = sales.save_customer({"name": "بنکدار  الف", "kind": "wholesaler", "city": "تهران", "phone": "۰۹۱۲ ۱۲۳ ۴۵۶۷"}, "sales")
    with pytest.raises(ServiceError, match="از قبل"):
        sales.save_customer({"name": "بنکدار الف"}, "sales")
    c = sales.customer(cid)
    assert c["name"] == "بنکدار الف" and c["phone"] == "09121234567" and c["reorder"] is None
    sales.save_customer({"name": "بنکدار الف", "kind": "retailer", "active": "0"}, "admin", cid)
    assert sales.customer(cid)["kind"] == "retailer" and not sales.customer(cid)["active"]
    with pytest.raises(ServiceError):
        sales.save_customer({"name": "x"}, "sales")


def test_inquiry_won_creates_order(env):
    _, _, sales = env()
    iid = sales.create_inquiry({"customer_name": "بنکدار الف", "product_key": "FIB101-480", "qty": "۲۰۰",
                                "payment_days": "45", "channel": "phone"}, "sales")
    inq = sales.inquiry(iid)
    assert inq["quoted_price_rial"] == 21_000_000 and inq["status"] == "open" and inq["follow_up_on"] == "2026-11-02"
    out = sales.close_inquiry(iid, "won", "sales", "viewer")
    o = sales.orders()[0]
    assert out["order_id"] == o["id"] and o["total_rial"] == 200 * 21_000_000 and o["payment_days"] == 45
    with pytest.raises(ServiceError):
        sales.close_inquiry(iid, "lost", "sales", "viewer")


def test_inquiry_lost_with_competitor_price_feeds_market(env):
    _, ms, sales = env()
    iid = sales.create_inquiry({"customer_name": "بنکدار ب", "product_key": "FIB201-400", "qty": "100"}, "sales")
    with pytest.raises(ServiceError, match="نام رقیب"):
        sales.close_inquiry(iid, "lost", "sales", "viewer", {"lost_reason": "price", "competitor_price": "2100000"})
    out = sales.close_inquiry(iid, "lost", "sales", "viewer", {"lost_reason": "price", "competitor": "کروز",
                                                             "competitor_price": "۲٬۱۰۰٬۰۰۰", "unit": "toman",
                                                             "note": "گفت کروز ارزان‌تر داد"})
    pend = ms.observations_to_review()
    assert [p["id"] for p in pend] == [out["observation_id"]]
    assert pend[0]["price_rial"] == 21_000_000 and pend[0]["min_qty"] == 100 and pend[0]["product_key"] == "FIB201-400"
    k = sales.kpis()
    assert k["lost"] == 1 and k["reasons"] == [("price", 1)]
    assert k["price_gap"] == pytest.approx(23.5 / 21 - 1)
    with pytest.raises(ServiceError):
        sales.close_inquiry(sales.create_inquiry({"customer_name": "x y", "product_key": "FIB201-400", "qty": "1"},
                                                 "sales"), "lost", "sales", "viewer", {"lost_reason": "bogus"})


def test_orders_import_template_and_dedup(env, tmp_path):
    svc, _, sales = env()
    data = orders_xlsx.template(svc.products(include_inactive=False))
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["سفارش‌ها", "راهنما", "کالاها"]
    ws = wb["سفارش‌ها"]
    ws.append(["۱۴۰۵/۰۷/۰۱", "بنکدار الف", "بنکدار", "تهران", "FIB101-480", 100, 20_500_000, 45, "F-1", "تلفن", ""])
    ws.append(["1405/07/01", "بنکدار الف", "", "", "FIB103-480", 50, 20_500_000, 45, "F-1", "", ""])
    ws.append(["1405/07/20", "بنکدار الف", "", "", "FIB101-480", 100, 21_000_000, 0, "F-2", "", ""])
    ws.append(["1405/08/09", "بنکدار الف", "", "", "FIB101-480", 100, 21_000_000, 0, "F-3", "", ""])
    ws.append(["1405/08/01", "تعمیرگاه ج", "تعمیرگاه", "شیراز", "NOPE", 1, 1, 0, "", "", ""])
    ws.append(["1405/08/01", "تعمیرگاه ج", "", "", "FIB201-400", "abc", 1, 0, "", "", ""])
    path = tmp_path / "orders.xlsx"
    wb.save(path)
    rows, errors = orders_xlsx.read(path, {p["key"] for p in svc.products()})
    assert len(rows) == 4 and len(errors) == 2 and "NOPE" in errors[0] and "سطر 7" in errors[1]
    res = sales.import_orders(rows, "admin")
    assert res == {"orders": 3, "lines": 4, "skipped": []}
    again = sales.import_orders(rows, "admin")
    assert again["orders"] == 0 and len(again["skipped"]) == 3
    cust = sales.customers()[0]
    assert cust["n_orders"] == 3 and cust["kind"] == "wholesaler"


def test_reorder_reminders_and_followups(env):
    _, _, sales = env()
    for d, ref in (("1405/06/01", "a"), ("1405/07/01", "b"), ("1405/07/30", "c")):
        sales.create_order({"customer_name": "بنکدار الف", "order_date": d, "ref": ref},
                           [{"product_key": "FIB101-480", "qty": "100", "unit_price": "21000000"}], "sales")
    at(2026, 11, 20)
    f = sales.followups()
    assert len(f["reorders"]) == 1 and f["reorders"][0]["typical_days"] == 30
    assert f["reorders"][0]["products"] == ["FIB101-480"]
    iid = sales.create_inquiry({"customer_name": "بنکدار الف", "product_key": "FIB102-250", "qty": "10",
                                "follow_up_on": "1405/08/29"}, "sales")
    f = sales.followups()
    assert f["reorders"] == [] and [i["id"] for i in f["inquiries"]] == [iid]
    sales.set_follow_up(iid, "1405/09/10", "sales")
    assert sales.followups()["inquiries"] == []


def test_kpis(env):
    _, _, sales = env()
    a = sales.create_inquiry({"customer_name": "الف", "product_key": "FIB101-480", "qty": "10"}, "s")
    b = sales.create_inquiry({"customer_name": "الف", "product_key": "FIB101-480", "qty": "10"}, "s")
    c = sales.create_inquiry({"customer_name": "ب ب", "product_key": "FIB201-400", "qty": "5", "payment_days": "60"}, "s")
    sales.close_inquiry(a, "won", "s", "viewer")
    sales.close_inquiry(b, "lost", "s", "viewer", {"lost_reason": "payment"})
    sales.close_inquiry(c, "won", "s", "viewer")
    k = sales.kpis()
    assert (k["won"], k["lost"], k["open"]) == (2, 1, 0) and k["conversion"] == pytest.approx(2 / 3)
    assert k["reasons"] == [("payment", 1)] and k["by_product"][0]["key"] == "FIB101-480"
    assert k["cash_rial"] == 10 * 21_000_000 and k["credit_rial"] == 5 * 23_500_000


def test_check_calculator():
    q = credit.quote(21_000_000, 45, Decimal("0.35"), Decimal("0.30"), offered_rial=22_000_000)
    assert q.min_check_rial == 21_800_000 and q.dollar_safe_rial == 21_700_000
    assert q.offered_pv_rial == 21_200_892 and float(q.offered_vs_cash) == pytest.approx(0.00957, abs=1e-4)
    assert float(q.offered_dollar_loss) == pytest.approx(0.01427, abs=1e-4)
    cash = credit.quote(21_000_000, 0, Decimal("0.35"))
    assert cash.min_check_rial == 21_000_000 and cash.dollar_factor is None
    with pytest.raises(ValueError):
        credit.quote(0, 30, Decimal("0.3"))


def test_settings_for_credit_and_advisor(tmp_path):
    cfg = config.load_config(write_settings(tmp_path))
    assert cfg.credit.annual_cost_of_money is None and cfg.credit.check_days == (30, 45, 60, 90)
    assert cfg.advisor.target_band == Decimal("0.05") and cfg.advisor.min_margin is None
    assert not hasattr(cfg, "bale")
    cfg = config.load_config(write_settings(tmp_path, "[credit]\nannual_cost_of_money = 0.35\ncheck_days = 60, 30\n"
                                                      "[bale]\nenabled = true\napi_base = ftp://x\n"
                                                      "[agents]\ndigest_time = 09:30\n"))
    assert cfg.credit.annual_cost_of_money == Decimal("0.35") and cfg.credit.check_days == (30, 60)


def approve_market(ms):
    sid = ms.save_source({"key": "shopx", "name": "نمونه", "kind": "shop", "domain": "shop.example.ir",
                          "start_urls": LIST_URL, "unit": "toman", "price_level": "wholesale", "parser": "auto",
                          "interval_hours": "24", "delay_seconds": "3", "max_pages": "5", "active": "1"}, "admin")
    ms.set_policy(sid, "approved", "robots.txt بررسی شد", "admin")
    ms.run_agent("price_watcher", "manual", "admin")
    ms.approve_strong("admin")


def test_pricing_advisor(env):
    svc, ms, sales = env("[advisor]\nmin_prices = 1\nmin_margin = 0.2\n")
    approve_market(ms)
    for seller, price in (("الف", "2300000"), ("ب", "2350000")):
        ms.manual_observation({"seller": seller, "price": price, "unit": "toman", "price_level": "wholesale",
                               "product_key": "FIB201-400", "note": "تلفنی"}, "admin", "admin")
    sales.set_cost("FIB101-480", "۸٫۵", "admin")
    assert {i["key"]: i for i in advisor.proposals(ms, sales)["items"]}["FIB101-480"]["median"] is None
    genuine = next(e for e in ms.listings() if "OZA632-SZ2" in e["title"])
    ms.set_tier(genuine["id"], "aftermarket", "admin")
    data = advisor.proposals(ms, sales)
    items = {i["key"]: i for i in data["items"]}
    fib101 = items["FIB101-480"]
    assert fib101["median"] == 34_000_000 and fib101["action"] == "raise"
    assert fib101["new_base"] == 32_500_000
    fib201 = items["FIB201-400"]
    assert fib201["median"] == 23_250_000 and fib201["action"] == "keep" and fib201["new_base"] is None
    assert items["FIB102-250"]["action"] == "insufficient"
    sales.set_cost("FIB204-480", "10", "admin")
    items = {i["key"]: i for i in advisor.proposals(ms, sales)["items"]}
    assert items["FIB204-480"]["floor"] == 32_500_000 and items["FIB204-480"]["action"] in ("raise", "floor")
    wb = load_workbook(BytesIO(advisor.xlsx(data, "برنا")))
    assert wb.active.cell(row=4, column=1).value == "کد"
    with pytest.raises(ServiceError):
        sales.set_cost("FIB101-480", "-2", "admin")
    sales.set_cost("FIB101-480", "", "admin")
    assert "FIB101-480" not in sales.costs()


def test_leads_from_listings(env):
    svc, ms, sales = env()
    approve_market(ms)
    with ms.db() as c, db.tx(c):
        scout.upsert_businesses(c, [Business(name="-", kind="repair", city=None, url="https://www.openstreetmap.org/node/9"),
                                    Business(name="...", kind="repair", city=None, url=None)],
                                "اوپن‌استریت‌مپ", "2026-10-31T10:00:00+00:00")
        c.execute("UPDATE leads SET note='بررسی شود' WHERE name='...'")
    res = sales.refresh_leads()
    assert res["new"] == 1 and res["removed"] == 1 and [r["name"] for r in sales.leads(kind="repair")] == ["..."]
    with ms.db() as c, db.tx(c):
        c.execute("DELETE FROM leads WHERE name='...'")
    lead = sales.leads()[0]
    assert lead["name"] == "فروشگاه نمونه الف" and lead["n_listings"] == 1
    sales.set_lead_status(lead["id"], "contacted", "sales", "تماس گرفتم")
    cid = sales.lead_to_customer(lead["id"], "sales")
    assert sales.customer(cid)["source"] == "lead" and sales.leads()[0]["status"] == "customer"
    assert sales.lead_to_customer(lead["id"], "sales") == cid
    with pytest.raises(ServiceError):
        sales.set_lead_status(lead["id"], "customer", "sales")


def test_phone_numbers_are_read_in_iranian_form():
    assert tn.phones("+98 21 6612 3456") == "02166123456"
    assert tn.phones("+98-912-345-6789;۰۹۱۲ ۳۴۵ ۶۷۸۹") == "09123456789"
    assert tn.phones("(021) 6612-3456 - 0912 345 6789") == "02166123456، 09123456789"
    assert tn.phones("021-66123456 داخلی 12") == "02166123456"
    assert tn.phones("0098 21 66123456 یا ۰۹۱۲۰۰۰۱۱۱۱") == "02166123456، 09120001111"
    assert tn.phones("+1 555 123 4567") == "+15551234567"
    assert tn.phones("021 1, 021 2, 0911 1111111, 0912 2222222, 0913 3333333, 0914 4444444") == (
        "09111111111، 09122222222، 09133333333")
    assert tn.phones("1525") is None and tn.phones("") is None and tn.phones(None) is None


def test_lead_phones_and_excel_list(env):
    svc, ms, sales = env()
    approve_market(ms)
    sales.refresh_leads()
    with ms.db() as c, db.tx(c):
        scout.upsert_businesses(c, [
            Business(name="تعمیرگاه برق نمونه", kind="repair", city="تهران", url="https://www.openstreetmap.org/node/7",
                     address="خیابان نمونه ۱۲", phone="02166123456، 09121112233"),
            Business(name="پخش قطعات نمونه", kind="supplier", city="شیراز", url="https://divar.ir/v/sample")],
            "اوپن‌استریت‌مپ", "2026-10-31T10:00:00+00:00")
    seller = next(r for r in sales.leads() if r["kind"] == "seller")
    with pytest.raises(ServiceError, match="معتبر نیست"):
        sales.set_lead_status(seller["id"], "contacted", "sales", "", "12")
    sales.set_lead_status(seller["id"], "contacted", "sales", "تماس گرفتم", "۰۹۱۲ ۳۴۵ ۶۷۸۹")
    sales.set_lead_status(seller["id"], "contacted", "sales", "", "")
    sales.set_lead_status(seller["id"], "contacted", "sales", "", "09123456789")
    assert next(r for r in sales.leads() if r["id"] == seller["id"])["phones_l"] == ["09123456789"]
    with svc.db() as c:
        changes = [json.loads(r["details"]).get("phone") for r in c.execute(
            "SELECT details FROM audit WHERE action='lead_status' ORDER BY id")]
    assert changes == [True, None, None]
    data, n = sales.leads_workbook(None, None, None, "sales")
    wb = load_workbook(BytesIO(data))
    assert n == 3 and wb.sheetnames == ["همه", "فروشندگان", "تأمین‌کنندگان", "تعمیرگاه‌ها", "راهنما"]
    ws = wb["همه"]
    assert ws.sheet_view.rightToLeft and ws.freeze_panes == "E2" and ws.auto_filter.ref == "A1:P4"
    head = [x.value for x in ws[1]]
    assert head[:8] == ["ردیف", "نام", "نوع", "شهر", "نشانی", "تلفن", "وضعیت", "یادداشت"] and len(head) == 16
    assert head[15] == "شناسه (تغییر ندهید)"
    assert ws.print_area == "'همه'!$A$1:$H$4" and ws.page_setup.orientation == "landscape"
    assert [r[1] for r in ws.iter_rows(min_row=2, values_only=True)] == [
        "فروشگاه نمونه الف", "پخش قطعات نمونه", "تعمیرگاه برق نمونه"]
    rows = {r[1].value: r for r in ws.iter_rows(min_row=2)}
    shop = [x.value for x in rows["فروشگاه نمونه الف"]]
    assert shop[5] == "09123456789" and rows["فروشگاه نمونه الف"][5].number_format == "@"
    assert shop[6:16] == ["تماس گرفته شد", "تماس گرفتم", "نمونه", 1, 3_400_000, 3_400_000, "سایت فروشگاه",
                          "1405/08/09", "1405/08/09", seller["id"]]
    assert rows["فروشگاه نمونه الف"][12].hyperlink.target == "https://shop.example.ir/p/a-100"
    repair = [x.value for x in rows["تعمیرگاه برق نمونه"]]
    assert repair[2:9] == ["تعمیرگاه", "تهران", "خیابان نمونه ۱۲", "02166123456، 09121112233", "تازه", None,
                           "اوپن‌استریت‌مپ"]
    assert repair[12] == "روی نقشه" and rows["تعمیرگاه برق نمونه"][12].hyperlink.target == (
        "https://www.openstreetmap.org/node/7")
    assert rows["پخش قطعات نمونه"][12].value == "آگهی دیوار"
    assert rows["تعمیرگاه برق نمونه"][2].fill.fgColor.rgb.endswith("FCE2D7") and rows["تعمیرگاه برق نمونه"][2].font.b
    assert rows["فروشگاه نمونه الف"][2].fill.fgColor.rgb.endswith("DCE8F8")
    assert rows["پخش قطعات نمونه"][5].value is None and rows["پخش قطعات نمونه"][10].value is None
    assert [r[1] for r in wb["تعمیرگاه‌ها"].iter_rows(min_row=2, values_only=True)] == ["تعمیرگاه برق نمونه"]
    assert wb["تعمیرگاه‌ها"].sheet_properties.tabColor.rgb.endswith("EB6834") and wb["تعمیرگاه‌ها"].freeze_panes == "E2"
    guide = [x.value for x in wb["راهنما"]["A"] if x.value]
    assert "فیلتر: همه" in guide and "همه: ۳؛ با تلفن: ۲؛ با نشانی: ۱" in guide
    assert "رنگ ستون «نوع»: آبی فروشنده، سبزآبی عمده‌فروش / تأمین‌کننده، نارنجی تعمیرگاه" in guide
    assert any("دیوار" in g and "اطلاعات تماس" in g for g in guide) and any("بارگذاری تلفن‌ها" in g for g in guide)
    data, n = sales.leads_workbook("contacted", "seller", None, "sales")
    wb = load_workbook(BytesIO(data))
    assert n == 1 and wb.sheetnames == ["همه", "راهنما"]
    assert "فیلتر: وضعیت: تماس گرفته شد، نوع: فروشنده" in [x.value for x in wb["راهنما"]["A"]]
    data, n = sales.leads_workbook(None, None, "اصفهان", "sales")
    assert n == 0 and load_workbook(BytesIO(data))["همه"].max_row == 1
    with svc.db() as c:
        audits = [json.loads(r["details"]) for r in c.execute(
            "SELECT details FROM audit WHERE action='leads_export' ORDER BY id")]
    assert audits == [{"count": 3, "filter": "همه"}, {"count": 1, "filter": "وضعیت: تماس گرفته شد، نوع: فروشنده"},
                      {"count": 0, "filter": "شهر: اصفهان"}]
    lead = next(r for r in sales.leads() if r["kind"] == "repair")
    cid = sales.lead_to_customer(lead["id"], "sales")
    cust = sales.customer(cid)
    assert cust["phone"] == "02166123456" and "تلفن‌ها: 02166123456، 09121112233" in cust["note"]
    sales.save_customer({"name": cust["name"], "kind": cust["kind"], "city": cust["city"], "phone": "021 5555 4444"},
                        "sales", cid)
    assert next(r for r in sales.leads() if r["id"] == lead["id"])["phones_l"] == [
        "02155554444", "02166123456", "09121112233"]


def test_phones_come_back_from_the_excel_file(env):
    svc, ms, sales = env()
    approve_market(ms)
    sales.refresh_leads()
    with ms.db() as c, db.tx(c):
        scout.upsert_businesses(c, [
            Business(name="تعمیرگاه برق نمونه", kind="repair", city="تهران", url="https://www.openstreetmap.org/node/7",
                     phone="02166123456"),
            Business(name="باطری سازی نمونه", kind="repair", city="ارومیه", url="https://divar.ir/v/x/gaz237G4"),
            Business(name="پخش قطعات نمونه", kind="supplier", city="شیراز", url="https://divar.ir/v/sample")],
            "دیوار", "2026-10-31T10:00:00+00:00")
    assert {r["name"] for r in sales.leads(phone="no")} == {"فروشگاه نمونه الف", "باطری سازی نمونه", "پخش قطعات نمونه"}
    assert [r["name"] for r in sales.leads(phone="yes")] == ["تعمیرگاه برق نمونه"]
    data, _ = sales.leads_workbook(None, None, None, "sales", phone="no")
    assert "فیلتر: تلفن: ندارد" in [x.value for x in load_workbook(BytesIO(data))["راهنما"]["A"]]
    wb = load_workbook(BytesIO(sales.leads_workbook(None, None, None, "sales")[0]))

    def row(sheet, name):
        return next(r for r in wb[sheet].iter_rows(min_row=2) if r[1].value == name)

    row("همه", "باطری سازی نمونه")[5].value = "۰۹۰۱ ۷۲۷ ۴۷۱۵"
    row("همه", "پخش قطعات نمونه")[5].value = 7136261234
    row("تعمیرگاه‌ها", "تعمیرگاه برق نمونه")[5].value = "0912 111 2233"
    row("فروشندگان", "فروشگاه نمونه الف")[5].value = "تماس نگرفتیم"
    wb["همه"].append([None, "ردیف ساختگی", None, None, None, "09350000000"] + [None] * 9 + [9999])
    wb["همه"].append([None, "بی‌شناسه", None, None, None, "09351111111"])
    buf = BytesIO()
    wb.save(buf)
    res = sales.import_lead_phones(buf.getvalue(), "leads.xlsx", "sales")
    assert (res["changed"], res["same"], res["unknown"]) == (3, 0, 1)
    assert res["errors"] == ["برگه «همه» سطر 7: شناسه ندارد.", "برگه «فروشندگان» سطر 2: «تماس نگرفتیم» شماره تلفن نیست."]
    phones = {r["name"]: r["phone"] for r in sales.leads()}
    assert phones == {"فروشگاه نمونه الف": None, "تعمیرگاه برق نمونه": "02166123456، 09121112233",
                      "باطری سازی نمونه": "09017274715", "پخش قطعات نمونه": "07136261234"}
    again = sales.import_lead_phones(buf.getvalue(), "leads.xlsx", "sales")
    assert (again["changed"], again["same"]) == (0, 3)
    with svc.db() as c:
        audit = [json.loads(r["details"]) for r in c.execute(
            "SELECT details FROM audit WHERE action='leads_phones_import' ORDER BY id")]
    assert audit[0] == {"file": "leads.xlsx", "count": 4, "changed": 3}
    with pytest.raises(ServiceError, match="انتخاب نشده"):
        sales.import_lead_phones(b"", "x.xlsx", "sales")
    with pytest.raises(ServiceError, match="باز نشد"):
        sales.import_lead_phones(b"not an excel file", "x.xlsx", "sales")
    other = BytesIO()
    orders_xlsx_bytes = orders_xlsx.template([{"key": "FIB101-480", "name": "نمونه"}])
    other.write(orders_xlsx_bytes)
    with pytest.raises(ServiceError, match="شناسه"):
        sales.import_lead_phones(other.getvalue(), "orders.xlsx", "sales")


def test_sales_agents(env):
    svc, ms, sales = env()
    approve_market(ms)
    at(2026, 11, 1, 9, 0)
    reg = __import__("borna_pricing.market.agents", fromlist=["x"]).registry()
    assert "sales_assistant" not in reg
    due = {k: a.due(ms, clock.now_utc()) for k, a in reg.items()}
    assert due["lead_finder"] and not due["pricing_advisor"]
    at(2026, 11, 1, 9, 45)
    done = {d["agent"]: d for d in ms.scheduled_tick()}
    assert done["lead_finder"]["stats"]["new"] == 1
    assert done["market_analyst"]["status"] == "ok" and done["pricing_advisor"]["status"] == "ok"
    assert set(done["market_analyst"]["stats"]) == {"report_id"}
    assert ms.report(done["pricing_advisor"]["stats"]["report_id"])["kind"] == "advisor"
    assert "pricing_advisor" not in {d["agent"] for d in ms.scheduled_tick()}
    at(2026, 11, 1, 10, 30)
    ms.build_report("admin")
    done = {d["agent"]: d for d in ms.scheduled_tick()}
    assert done["pricing_advisor"]["status"] == "ok"
    assert {r["kind"] for r in ms.reports()} == {"weekly"} and len(ms.reports(kind="advisor")) == 2
    ms2 = MarketService(svc)
    assert ms2.run_agent("lead_finder")["status"] == "skipped"
