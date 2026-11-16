import re
from datetime import datetime

import pytest

from borna_pricing import clock, config
from borna_pricing.ai import reasons
from borna_pricing.clock import TEHRAN
from borna_pricing.market.service import MarketService
from borna_pricing.sales.service import SalesService
from borna_pricing.service import Service
from borna_pricing.web import create_app

from conftest import FakeWeb, fake_fetcher_factory, write_settings

PRICE_TEXT = "لیست قیمت (به هزار تومان)\nسنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت ...... 2450\nبکس سنسور اکسیژن 450\n"


@pytest.fixture
def env(tmp_path):
    cfg = config.load_config(write_settings(tmp_path))
    svc = Service(cfg)
    clock.set_fixed_now(datetime(2026, 10, 31, 13, 30, tzinfo=TEHRAN))
    svc.init()
    svc.create_user("admin", "admin-pass-1", "admin", "مدیر سامانه", "test")
    svc.create_user("finance", "finance-pass", "approver", "مدیر مالی", "test")
    svc.create_user("sales", "sales-pass-1", "viewer", "کارشناس فروش", "test")
    ms = MarketService(svc, fake_fetcher_factory(FakeWeb()))
    sales = SalesService(svc, ms)
    app = create_app(cfg, svc, ms, sales)
    app.config["TESTING"] = True
    return app, svc, ms, sales


def login(client, user, pw):
    client.get("/login")
    with client.session_transaction() as s:
        tok = s.get("_csrf")
    assert client.post("/login", data={"username": user, "password": pw, "_csrf": tok}).status_code == 302
    with client.session_transaction() as s:
        return s["_csrf"]


def text(r):
    return r.get_data(as_text=True)


def test_price_list_page_parse_and_confirm(env):
    app, svc, ms, _ = env
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    assert c.get("/market/content").status_code == 403 and c.get("/market/ai").status_code == 403
    r = c.post("/market/price-list", data={"_csrf": tok, "action": "parse", "seller": "کانال الف", "channel": "telegram",
                                           "price_level": "wholesale", "unit": "toman", "multiplier": "auto",
                                           "tier": "aftermarket", "text": PRICE_TEXT})
    page = text(r)
    assert "۲ ردیف خوانده شد" in page and "۲٬۴۵۰٬۰۰۰" in page and "سنسور اکسیژن نیست" in page
    assert 'name="include_0" value="1" checked' in page and 'name="include_1" value="1" checked' not in page
    assert re.search(r'<option value="FIB101-480" selected>', page)
    form = {"_csrf": tok, "action": "confirm", "n": "2", "seller": "کانال الف", "channel": "telegram",
            "price_level": "wholesale", "tier": "aftermarket", "text": PRICE_TEXT,
            "include_0": "1", "title_0": "سنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت", "price_rial_0": "24500000",
            "availability_0": "unknown", "product_0": "FIB101-480",
            "title_1": "بکس سنسور اکسیژن", "price_rial_1": "4500000", "availability_1": "unknown", "product_1": ""}
    r = c.post("/market/price-list", data=form, follow_redirects=True)
    assert "۱ قیمت ثبت شد" in text(r) and "بعد از تأیید" in text(r)
    pend = ms.observations_to_review()
    assert len(pend) == 1 and pend[0]["price_rial"] == 24_500_000 and pend[0]["product_key"] == "FIB101-480"
    assert ms.snapshot_bytes(ms.snapshot(pend[0]["snapshot_id"])).decode("utf-8") == PRICE_TEXT


def test_reply_page(env):
    app, *_ = env
    c = app.test_client()
    tok = login(c, "sales", "sales-pass-1")
    r = c.post("/sales/reply", data={"_csrf": tok, "question": "سنسور اکسیژن زیمنس سوکت طوسی سیم ۵۰ سانت دارید؟ 09121234567"})
    page = text(r)
    assert "FIB101-480" in page and "۲٬۱۰۰٬۰۰۰ تومان" in page and page.count("09121234567") == 1


def test_content_pages(env):
    app, svc, ms, _ = env
    c = app.test_client()
    tok = login(c, "finance", "finance-pass")
    assert "FIB101-480" in text(c.get("/market/content"))
    r = c.post("/market/content/generate", data={"_csrf": tok, "product_key": "FIB101-480"})
    assert r.status_code == 302
    did = int(r.headers["Location"].rsplit("/", 1)[1])
    page = text(c.get(f"/market/content/{did}"))
    assert "طرح NTK" in page and "نه قطعه اصلی NTK" in page
    r = c.post(f"/market/content/{did}/decide", data={"_csrf": tok, "decision": "approve"}, follow_redirects=True)
    assert "متن تأیید شد" in text(r)
    assert "تأییدشده" in text(c.get("/market/content"))


def test_reason_suggestion_on_inquiry_page(env):
    app, svc, ms, sales = env
    iid = sales.create_inquiry({"customer_name": "بنکدار الف", "product_key": "FIB101-480", "qty": "10"}, "sales")
    sales.close_inquiry(iid, "lost", "sales", "viewer", {"lost_reason": "other", "note": "گفت گرونه"})
    reasons.run(svc, None)
    c = app.test_client()
    tok = login(c, "finance", "finance-pass")
    page = text(c.get(f"/sales/inquiries/{iid}"))
    assert "پیشنهاد قاعده کلیدواژه" in page and "قیمت بالاتر از رقیب" in page
    sid = reasons.pending(svc)[iid]["id"]
    r = c.post(f"/sales/suggestions/{sid}/decide", data={"_csrf": tok, "decision": "apply", "next": f"/sales/inquiries/{iid}"},
               follow_redirects=True)
    assert "دلیل پیشنهادی ثبت شد" in text(r) and sales.inquiry(iid)["lost_reason"] == "price"


def test_ai_admin_page(env):
    app, *_ = env
    c = app.test_client()
    tok = login(c, "admin", "admin-pass-1")
    page = text(c.get("/market/ai"))
    assert "خاموش" in page and "سازگار با OpenAI" in page
    r = c.post("/market/ai", data={"_csrf": tok}, follow_redirects=True)
    assert "آزمون ناموفق" in text(r)
    agents = text(c.get("/market/agents"))
    assert "نویسنده متن آگهی" in agents and "تحلیلگر دلیل نخریدن" in agents and "دستیار فروش" not in agents
