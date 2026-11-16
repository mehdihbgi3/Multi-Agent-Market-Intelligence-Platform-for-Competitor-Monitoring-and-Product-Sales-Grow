import io
import re
from datetime import datetime

import pytest
from openpyxl import load_workbook

from borna_pricing import clock, config, db
from borna_pricing.clock import TEHRAN
from borna_pricing.market import scout
from borna_pricing.market.service import MarketService
from borna_pricing.market.sites import Business
from borna_pricing.sales.service import SalesService
from borna_pricing.service import Service
from borna_pricing.web import create_app

from conftest import FIXTURES, FakeWeb, fake_fetcher_factory, write_settings

LIST_URL = "https://shop.example.ir/cat/o2"
ITEMLIST = (FIXTURES / "market" / "synthetic_itemlist.html").read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    cfg = config.load_config(write_settings(tmp_path, "[credit]\nannual_cost_of_money = 0.35\n"))
    svc = Service(cfg)
    clock.set_fixed_now(datetime(2026, 10, 31, 13, 30, tzinfo=TEHRAN))
    svc.init()
    svc.create_user("admin", "admin-pass-1", "admin", "مدیر سامانه", "test")
    svc.create_user("finance", "finance-pass", "approver", "مدیر مالی", "test")
    svc.create_user("sales", "sales-pass-1", "viewer", "کارشناس فروش", "test")
    ms = MarketService(svc, fake_fetcher_factory(FakeWeb({LIST_URL: (200, "text/html", ITEMLIST)})))
    sales = SalesService(svc, ms)
    app = create_app(cfg, svc, ms, sales)
    app.config["TESTING"] = True
    return app, svc, ms, sales


def login(client, user, pw):
    client.get("/login")
    with client.session_transaction() as s:
        tok = s.get("_csrf")
    r = client.post("/login", data={"username": user, "password": pw, "_csrf": tok})
    assert r.status_code == 302
    with client.session_transaction() as s:
        return s["_csrf"]


def text(r):
    return r.get_data(as_text=True)


def test_roles(env):
    app, *_ = env
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    for url in ("/sales", "/sales/inquiries", "/sales/inquiries/new", "/sales/customers", "/sales/customers/new",
                "/sales/orders", "/sales/orders/new", "/sales/credit", "/sales/leads"):
        assert c.get(url).status_code == 200, url
    assert c.get("/sales/advisor").status_code == 403
    assert c.get("/sales/notifications").status_code == 404 and "پیام بله" not in text(c.get("/sales"))
    assert c.post("/sales/orders/import", data={"_csrf": tok}).status_code == 403
    assert c.post("/sales/costs", data={"_csrf": tok, "product_key": "FIB101-480", "cost_usd": "5"}).status_code == 403
    assert "فروش" in text(c.get("/"))
    t = c.get("/sales/orders/template.xlsx")
    assert t.status_code == 200 and load_workbook(io.BytesIO(t.data)).sheetnames[0] == "سفارش‌ها"


def test_inquiry_flow_through_web(env):
    app, svc, ms, sales = env
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    r = c.post("/sales/inquiries/new", data={"_csrf": tok, "customer_name": "بنکدار الف", "customer_kind": "wholesaler",
                                             "product_key": "FIB201-400", "qty": "۱۰۰", "payment_days": "45",
                                             "channel": "phone", "unit": "rial"})
    assert r.status_code == 302
    iid = int(re.search(r"/sales/inquiries/(\d+)", r.headers["Location"]).group(1))
    page = text(c.get(f"/sales/inquiries/{iid}"))
    assert "بنکدار الف" in page and "چک ۴۵ روزه" in page and "۲٬۳۵۰٬۰۰۰" in page
    r = c.post(f"/sales/inquiries/{iid}/close", data={"_csrf": tok, "outcome": "lost", "lost_reason": "price",
                                                     "competitor": "کروز", "competitor_price": "2150000", "unit": "toman"},
               follow_redirects=True)
    assert "قیمت رقیب برای بررسی" in text(r)
    assert ms.observations_to_review()[0]["price_rial"] == 21_500_000
    dash = text(c.get("/sales"))
    assert "قیمت بالاتر از رقیب" in dash
    r = c.post("/sales/inquiries/new", data={"_csrf": tok, "customer_id": "1", "product_key": "FIB101-480", "qty": "50"})
    iid2 = int(re.search(r"/sales/inquiries/(\d+)", r.headers["Location"]).group(1))
    r = c.post(f"/sales/inquiries/{iid2}/close", data={"_csrf": tok, "outcome": "won", "create_order": "1"},
               follow_redirects=True)
    assert "سفارش هم ثبت شد" in text(r)
    assert "FIB101-480" in text(c.get("/sales/orders"))
    cust = text(c.get("/sales/customers/1"))
    assert "بنکدار الف" in cust and "از دست رفت" in cust


def test_orders_form_and_import(env, tmp_path):
    app, svc, ms, sales = env
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    r = c.post("/sales/orders/new", data={"_csrf": tok, "customer_name": "تعمیرگاه ج", "order_date": "1405/08/01",
                                          "payment_days": "0", "product_1": "FIB101-480", "qty_1": "10", "price_1": "",
                                          "product_2": "FIB102-250", "qty_2": "5", "price_2": "2000000", "unit": "toman"},
               follow_redirects=True)
    assert "ثبت شد" in text(r)
    o = sales.orders()[0]
    assert o["total_rial"] == 10 * 21_000_000 + 5 * 20_000_000
    fin = app.test_client()
    ftok = login(fin, "finance", "finance-pass")
    wb = load_workbook(io.BytesIO(fin.get("/sales/orders/template.xlsx").data))
    wb["سفارش‌ها"].append(["1405/07/10", "بنکدار د", "", "", "FIB201-400", 100, 23_000_000, 30, "X-9", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = fin.post("/sales/orders/import", data={"_csrf": ftok, "file": (buf, "orders.xlsx")},
                 content_type="multipart/form-data")
    page = text(r)
    assert r.status_code == 200 and "۱ ردیف آماده ورود" in page
    token = re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
    r = fin.post("/sales/orders/import/apply", data={"_csrf": ftok, "token": token}, follow_redirects=True)
    assert "۱ سفارش با ۱ ردیف وارد شد" in text(r)
    r = fin.post("/sales/orders/import/apply", data={"_csrf": ftok, "token": token}, follow_redirects=True)
    assert "منقضی" in text(r)


def test_credit_calculator_page(env):
    app, *_ = env
    c = app.test_client()
    login(c, "sales", "sales-pass-1")
    page = text(c.get("/sales/credit"))
    assert 'value="35"' in page
    page = text(c.get("/sales/credit?go=1&product_key=FIB101-480&qty=1&days=45&rate=35&dollar=30&offered=2200000"))
    assert "۲٬۱۸۰٬۰۰۰" in page and "۲٬۱۷۰٬۰۰۰" in page and "بیشتر از نقد" in page
    page = text(c.get("/sales/credit?go=1&cash=&days=45&rate="))
    assert "error-text" in page


def test_advisor_and_leads(env):
    app, svc, ms, sales = env
    sid = ms.save_source({"key": "shopx", "name": "نمونه", "kind": "shop", "domain": "shop.example.ir",
                          "start_urls": LIST_URL, "unit": "toman", "price_level": "wholesale", "parser": "auto",
                          "interval_hours": "24", "delay_seconds": "3", "max_pages": "5", "active": "1"}, "admin")
    ms.set_policy(sid, "approved", "robots.txt بررسی شد", "admin")
    ms.run_agent("price_watcher", "manual", "admin")
    ms.approve_strong("admin")
    fin = app.test_client()
    tok = login(fin, "finance", "finance-pass")
    assert "هنوز پیشنهادی ساخته نشده" in text(fin.get("/sales/advisor"))
    r = fin.post("/sales/advisor/run", data={"_csrf": tok}, follow_redirects=True)
    assert "مشاور قیمت اجرا شد" in text(r)
    page = text(fin.get("/sales/advisor"))
    assert "پیشنهادها" in page and "Excel" in page
    rid = ms.reports(kind="advisor")[0]["id"]
    x = fin.get(f"/sales/advisor/{rid}.xlsx")
    assert x.status_code == 200 and load_workbook(io.BytesIO(x.data)).active.title == "پیشنهاد قیمت پایه"
    assert fin.get(f"/market/reports/{rid}").status_code == 404
    r = fin.post("/sales/costs", data={"_csrf": tok, "product_key": "FIB101-480", "cost_usd": "۸٫۵"}, follow_redirects=True)
    assert "بهای تمام‌شده ثبت شد" in text(r) and "۸.۵ دلار" in text(r)
    sales_c = app.test_client()
    stok = login(sales_c, "sales", "sales-pass-1")
    r = sales_c.post("/sales/leads/refresh", data={"_csrf": stok}, follow_redirects=True)
    assert "۱ مشتری احتمالی تازه" in text(r) and "فروشگاه نمونه الف" in text(r)
    lead = sales.leads()[0]
    r = sales_c.post(f"/sales/leads/{lead['id']}/customer", data={"_csrf": stok})
    assert r.status_code == 302 and "/sales/customers/" in r.headers["Location"]
    admin = app.test_client()
    login(admin, "admin", "admin-pass-1")
    assert admin.get("/sales/notifications").status_code == 404 and "پیام بله" not in text(admin.get("/sales"))


def test_leads_phone_form_and_excel_download(env):
    app, svc, ms, sales = env
    sid = ms.save_source({"key": "shopx", "name": "نمونه", "kind": "shop", "domain": "shop.example.ir",
                          "start_urls": LIST_URL, "unit": "toman", "price_level": "wholesale", "parser": "auto",
                          "interval_hours": "24", "delay_seconds": "3", "max_pages": "5", "active": "1"}, "admin")
    ms.set_policy(sid, "approved", "robots.txt بررسی شد", "admin")
    ms.run_agent("price_watcher", "manual", "admin")
    ms.approve_strong("admin")
    sales.refresh_leads()
    lead = sales.leads()[0]
    r = app.test_client().get("/sales/leads.xlsx")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    page = text(c.get("/sales/leads?kind=seller"))
    assert "دریافت Excel" in page and "/sales/leads.xlsx?kind=seller" in page and 'name="phone" value=""' in page
    r = c.post(f"/sales/leads/{lead['id']}/status", data={"_csrf": tok, "status": "contacted", "phone": "0912-345-6789",
                                                         "next": "/sales/leads?kind=seller"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/sales/leads?kind=seller")
    page = text(c.get("/sales/leads?kind=seller"))
    assert 'href="tel:09123456789"' in page and "۰۹۱۲۳۴۵۶۷۸۹" in page and 'name="phone" value="09123456789"' in page
    r = c.post(f"/sales/leads/{lead['id']}/status", data={"_csrf": tok, "status": "contacted", "phone": "abc"},
               follow_redirects=True)
    assert "معتبر نیست" in text(r)
    x = c.get("/sales/leads.xlsx?kind=seller")
    assert x.status_code == 200 and x.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "Borna_Businesses_20261031.xlsx" in x.headers["Content-Disposition"]
    rows = list(load_workbook(io.BytesIO(x.data))["همه"].iter_rows(min_row=2, values_only=True))
    assert [(r[1], r[2], r[5]) for r in rows] == [("فروشگاه نمونه الف", "فروشنده", "09123456789")]
    empty = load_workbook(io.BytesIO(c.get("/sales/leads.xlsx?kind=repair").data))
    assert empty["همه"].max_row == 1 and empty.sheetnames == ["همه", "راهنما"]
    admin = app.test_client()
    login(admin, "admin", "admin-pass-1")
    audit = text(admin.get("/admin/audit"))
    assert "دریافت Excel کسب‌وکارها" in audit and "نوع: فروشنده" in audit


def test_long_lead_list_is_capped_and_searchable(env):
    app, svc, ms, sales = env
    shops = [Business(name=f"تعمیرگاه شماره {i}", kind="repair", city="تهران" if i % 2 else "کرج",
                      url=f"https://www.openstreetmap.org/node/{i}", phone=f"0912{i:07d}" if i == 7 else None)
             for i in range(1, 506)]
    with svc.db() as c, db.tx(c):
        scout.upsert_businesses(c, shops, "اوپن‌استریت‌مپ", "2026-10-31T10:00:00+00:00")
    c = app.test_client()
    login(c, "sales", "sales-pass-1")
    page = text(c.get("/sales/leads"))
    assert "۵۰۰ مورد از ۵۰۵ نشان داده شده است" in page and page.count('name="status"') == 500
    page = text(c.get("/sales/leads?city=کرج"))
    assert "نشان داده شده است" not in page and page.count('name="status"') == 252
    page = text(c.get("/sales/leads?q=۰۹۱۲۰۰۰۰۰۰۷"))
    assert page.count('name="status"') == 1 and "تعمیرگاه شماره 7" in page
    page = text(c.get("/sales/leads?q=شماره 50&kind=repair"))
    assert page.count('name="status"') == 7 and "q=%D8%B4%D9%85%D8%A7%D8%B1%D9%87+50" in page
    assert "با این فیلتر یا جست‌وجو چیزی پیدا نشد" in text(c.get("/sales/leads?q=100%25"))
    wb = load_workbook(io.BytesIO(c.get("/sales/leads.xlsx").data))
    assert wb["همه"].max_row == 506
    wb = load_workbook(io.BytesIO(c.get("/sales/leads.xlsx?q=شماره 50").data))
    assert wb["همه"].max_row == 8 and "فیلتر: جست‌وجو: شماره 50" in [x.value for x in wb["راهنما"]["A"]]


def test_phone_upload_and_phone_filter_on_the_page(env):
    app, svc, ms, sales = env
    with svc.db() as c, db.tx(c):
        scout.upsert_businesses(c, [Business(name="باطری سازی نمونه", kind="repair", city="ارومیه",
                                             url="https://divar.ir/v/x/gaz237G4")], "دیوار", "2026-10-31T10:00:00+00:00")
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    page = text(c.get("/sales/leads?phone=no"))
    assert "بارگذاری تلفن‌ها" in page and "آگهی دیوار" in page and "/sales/leads.xlsx?phone=no" in page
    assert 'value="no" selected' in page
    wb = load_workbook(io.BytesIO(c.get("/sales/leads.xlsx?phone=no").data))
    assert wb["همه"]["B2"].value == "باطری سازی نمونه" and wb["همه"]["M2"].value == "آگهی دیوار"
    wb["همه"]["F2"] = "۰۹۰۱ ۷۲۷ ۴۷۱۵"
    buf = io.BytesIO()
    wb.save(buf)
    r = c.post("/sales/leads/phones", data={"_csrf": tok, "file": (io.BytesIO(buf.getvalue()), "leads.xlsx")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "۱ تلفن تازه ثبت شد؛ ۰ ردیف تغییری نداشت." in text(r)
    assert 'href="tel:09017274715"' in text(c.get("/sales/leads?phone=yes"))
    assert "با این فیلتر یا جست‌وجو چیزی پیدا نشد" in text(c.get("/sales/leads?phone=no"))
    r = c.post("/sales/leads/phones", data={"_csrf": tok, "file": (io.BytesIO(b"x"), "leads.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "پسوند xlsx" in text(r)
    r = c.post("/sales/leads/phones", data={"_csrf": tok, "file": (io.BytesIO(b"x"), "leads.xlsx")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "فایل Excel باز نشد" in text(r)
    r = c.post("/sales/leads/phones", data={"file": (io.BytesIO(buf.getvalue()), "leads.xlsx")},
               content_type="multipart/form-data")
    assert r.status_code == 400
