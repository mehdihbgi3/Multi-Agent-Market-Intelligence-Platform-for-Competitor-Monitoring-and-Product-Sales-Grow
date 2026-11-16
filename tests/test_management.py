import json
from datetime import datetime, timedelta

import pytest

from borna_pricing import clock, config, db, management, viz
from borna_pricing.clock import TEHRAN, iso
from borna_pricing.market import scout
from borna_pricing.market.service import MarketService
from borna_pricing.market.sites import Business
from borna_pricing.sales.service import SalesService
from borna_pricing.service import Service
from borna_pricing.web import create_app

from conftest import FakeWeb, fake_fetcher_factory, write_settings

NOW = datetime(2026, 11, 8, 11, 0, tzinfo=TEHRAN)


@pytest.fixture
def env(tmp_path):
    cfg = config.load_config(write_settings(tmp_path, "[advisor]\nmin_margin = 0.2\n"))
    svc = Service(cfg)
    clock.set_fixed_now(NOW - timedelta(days=40))
    svc.init()
    for user, pw, role in (("admin", "admin-pass-1", "admin"), ("finance", "finance-pass", "approver"),
                           ("sales", "sales-pass-1", "viewer")):
        svc.create_user(user, pw, role, user, "test")
    clock.set_fixed_now(NOW)
    ms = MarketService(svc, fake_fetcher_factory(FakeWeb({})))
    sales = SalesService(svc, ms)
    ms.sales = sales
    app = create_app(cfg, svc, ms, sales)
    app.config["TESTING"] = True
    return app, svc, ms, sales


def login(client, user, pw):
    client.get("/login")
    with client.session_transaction() as s:
        tok = s.get("_csrf")
    assert client.post("/login", data={"username": user, "password": pw, "_csrf": tok}).status_code == 302


def text(r):
    return r.get_data(as_text=True)


def source(c, key, name, level):
    return c.execute("INSERT INTO sources(key, name, kind, domain, unit, price_level, policy_status, created_at,"
                     " created_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (key, name, "aggregator", key + ".ir", "toman", level, "approved", iso(NOW), "system",
                      iso(NOW))).lastrowid


def listing(c, src, key, seller, level, prices, tier="aftermarket", brand=None, ext=None):
    ext = ext or f"{src}-{key}-{seller}"
    eid = c.execute("INSERT INTO external_products(source_id, ext_key, url, title, seller, attrs, tier, match_state,"
                    " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (src, ext, f"https://example.ir/{ext}", f"سنسور اکسیژن {key}", seller,
                     json.dumps({"family": "o2", "brand": brand, "tier": tier}), tier, "matched",
                     iso(NOW - timedelta(days=60)), iso(NOW - timedelta(days=1)))).lastrowid
    c.execute("INSERT INTO matches(external_product_id, product_key, status, verdict, relation, engine_version,"
              " proposed_at, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (eid, key, "approved", "strong", "equivalent", "test", iso(NOW - timedelta(days=60)), "admin",
               iso(NOW - timedelta(days=60))))
    for when, price in prices:
        sid = c.execute("INSERT INTO snapshots(source_id, kind, url, fetched_at, bytes, sha256, path, created_by)"
                        " VALUES (?,?,?,?,?,?,?,?)", (src, "page", "https://x", iso(when), 1, "x", "x", "system")).lastrowid
        c.execute("INSERT INTO observations(external_product_id, snapshot_id, observed_at, kind, availability,"
                  " price_rial, unit, price_level, status, entry, created_by, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (eid, sid, iso(when), "price", "in_stock", int(price), "toman", level, "ok", "crawl", "system",
                   iso(when)))
    return eid


def market_data(svc):
    official = {r["product_key"]: r["price_rial"] for r in svc.publication()["rows"]}
    a, b, c3 = "FIB101-480", "FIB102-250", "FIB103-480"
    recent = NOW - timedelta(days=2)
    with svc.db() as c, db.tx(c):
        whole = source(c, "bazar", "بازار تهران", "wholesale")
        torob = source(c, "scout-torob", "ترب", "retail")
        listing(c, whole, a, "پخش الف", "wholesale", [(recent, official[a] * 0.9)])
        listing(c, whole, a, "پخش ب", "wholesale", [(recent, official[a] * 0.9)])
        listing(c, torob, a, "فروشگاه پ", "retail", [(recent, official[a] * 1.3)])
        listing(c, torob, a, "فروشگاه اصل", "retail", [(recent, official[a] * 2)], tier="genuine")
        listing(c, whole, b, "پخش الف", "wholesale", [(recent, official[b] * 1.01)], brand="Crouse")
        listing(c, whole, c3, "پخش ب", "wholesale", [(recent, official[c3] * 1.2)])
        listing(c, torob, c3, "فروشگاه ت", "retail", [(NOW - timedelta(days=50), official[c3] * 1.5),
                                                      (recent, official[c3] * 1.2)])
        own = listing(c, torob, a, "برنا سنسور", "retail", [(NOW - timedelta(days=50), official[a] * 1.3),
                                                           (recent, official[a] * 1.4)])
        c.execute("UPDATE external_products SET is_own=1 WHERE id=?", (own,))
    return official, (a, b, c3)


def test_fresh_install_dashboard_shows_empty_states_and_respects_roles(env):
    app, svc, ms, sales = env
    d = management.build(svc, ms, sales, 7)
    assert d["days"] == 90 and len(d["kpis"]) == 9 and [k["kind"] for k in d["leads"]["kinds"]] == [
        "seller", "supplier", "repair", "manufacturer"]
    assert d["market"]["lanes"]["wholesale"]["rows"] == [] and d["market"]["lanes"]["retail"]["rows"] == []
    assert not d["market"]["has_market"] and not d["sales"]["has_months"] and d["margins"] == [] and d["trends"] == []
    assert d["decisions"] == [] and d["drops"] == [] and d["insights"] == []
    c = app.test_client()
    login(c, "finance", "finance-pass")
    page = text(c.get("/management?days=30"))
    assert "داشبورد مدیریت" in page and "هنوز قیمت تأییدشده‌ای از رقبا" in page and "هنوز سفارشی ثبت نشده" in page
    assert "کاری منتظر تصمیم شما نیست" in page and "data-print" in page
    assert '/sales/leads?kind=seller' in page and '/sales/leads?kind=repair' in page and '/sales/leads?kind=supplier' in page
    assert 'href="/management?days=30" aria-current="true"' in page and ">مدیریت</a>" in page
    for days in (90, 180, 365):
        assert c.get(f"/management?days={days}").status_code == 200
    viewer = app.test_client()
    login(viewer, "sales", "sales-pass-1")
    assert viewer.get("/management").status_code == 403 and ">مدیریت</a>" not in text(viewer.get("/"))
    assert app.test_client().get("/management").status_code == 302


def test_borna_against_wholesale_and_retail_sellers(env):
    app, svc, ms, sales = env
    official, (a, b, c3) = market_data(svc)
    m = management.build(svc, ms, sales, 90)["market"]
    rows = {r["key"]: r for r in m["rows"]}
    assert rows[a]["w_n"] == 2 and rows[a]["r_n"] == 1 and rows[a]["gap_w"] == pytest.approx(1 / 0.9 - 1, abs=1e-3)
    assert rows[a]["cmp"]["retail"]["borna"] == official[a] == rows[a]["cmp"]["wholesale"]["borna"]
    assert rows[a]["gap_r"] == pytest.approx(1 / 1.3 - 1, abs=1e-3) and rows[a]["cheapest"]["level"] == "عمده"
    assert rows[b]["gap_w"] == pytest.approx(1 / 1.01 - 1, abs=1e-3) and rows[b]["gap_r"] is None
    assert rows[c3]["gap_w"] == pytest.approx(1 / 1.2 - 1, abs=1e-3)
    assert rows[c3]["gap_r"] == pytest.approx(1 / 1.2 - 1, abs=1e-3)
    w, r = m["summary"]["wholesale"], m["summary"]["retail"]
    assert (w["cheaper"], w["similar"], w["pricier"], w["n"]) == (1, 1, 1, 3)
    assert (r["cheaper"], r["similar"], r["pricier"], r["n"]) == (2, 0, 0, 2)
    lw, lr = m["lanes"]["wholesale"], m["lanes"]["retail"]
    assert [x["key"] for x in lw["rows"]] == [a, b, c3] and lw["axis"] == 0.2
    assert lw["rows"][0]["gap"] > 0 > lw["rows"][-1]["gap"] and all(0 < x["w"] <= 50 for x in lw["rows"])
    assert [x["key"] for x in lr["rows"]] == [c3, a] and lr["rows"][1]["borna"] == official[a]
    brands = {x["label"]: x for x in m["brands"]}
    assert brands["اصلی"]["gap"] == pytest.approx(-0.5, abs=1e-3) and "کروز" in brands
    assert all(0 < x["w"] <= 46 for x in m["brands"])
    sites = {x["label"]: x for x in m["sites"]}
    assert set(sites) == {"بازار تهران", "ترب"} and sites["ترب"]["n"] == 2
    sellers = {x["label"]: x for x in m["sellers"]}
    assert sellers["پخش ب"]["products"] == 2 and sellers["پخش ب"]["cheaper"] == 1 and sellers["پخش الف"]["cheaper"] == 1
    assert "فروشگاه ت" in sellers and "برنا سنسور" not in sellers
    assert m["n_prices"] == 7 and m["n_sellers"] == 5
    assert [(x["key"], x["side"]) for x in m["cheapest_all"]] == [(a, "تکی"), (b, "عمده"), (c3, "عمده"), (c3, "تکی")]
    assert [(x["key"], x["side"]) for x in m["priciest"]] == [(a, "عمده")]
    d = management.build(svc, ms, sales, 90)
    assert [x["key"] for x in d["drops"]] == [c3] and d["drops"][0]["change"] == pytest.approx(-0.2, abs=1e-3)
    assert any(t.startswith("عمده با عمده: قیمت هر عدد برنا در ۱ کالا ارزان‌تر") for t in d["insights"])
    assert any(t.startswith("تکی با تکی: قیمت هر عدد برنا در ۲ کالا ارزان‌تر") for t in d["insights"])
    assert not any("ثبت نشده" in t for t in d["insights"])
    assert any(t.startswith("بیشترین گرانی در عمده: FIB101-480") for t in d["insights"])
    assert [t["key"] for t in d["trends"]] == [a, c3, b]
    lanes = {t["key"]: {ln["side"]: ln for ln in t["lanes"]} for t in d["trends"]}
    assert lanes[a]["wholesale"]["chart"] and lanes[a]["retail"]["chart"]
    assert [x["name"] for x in lanes[a]["retail"]["legend"]] == ["قیمت هر عدد برنا (لیست رسمی)", "میانه تکی رقبا"]
    assert lanes[b]["retail"]["chart"] is None and lanes[c3]["retail"]["chart"]
    kpi = {k["label"]: k for k in d["kpis"]}
    assert kpi["عمده با عمده (قیمت هر عدد)"]["value"] == "هم‌قیمت"
    assert kpi["تکی با تکی"]["value"] == management.gap_words((rows[a]["gap_r"] + rows[c3]["gap_r"]) / 2)
    c = app.test_client()
    login(c, "admin", "admin-pass-1")
    page = text(c.get("/management"))
    assert "قیمت هر عدد برنا در برابر رقبا: عمده با عمده، تکی با تکی" in page and 'class="gchart"' in page
    assert page.count('class="gchart"') == 2 and "قیمت تکی برنا" not in page
    assert "برنا ارزان‌تر" in page and "برنا گران‌تر" in page and "اصلی" in page and "کروز" in page
    assert "رقیبانی که قیمت را تند پایین آوردند" in page and "فروشگاه ت" in page
    assert page.count('class="mchart wide-only"') == 6 and page.count('class="mchart narrow-only"') == 6


def test_sales_businesses_margins_and_decisions(env):
    app, svc, ms, sales = env
    official, (a, b, c3) = market_data(svc)
    sales.save_customer({"name": "بنکدار تهران", "kind": "wholesaler", "city": "تهران"}, "admin")
    sales.save_customer({"name": "پخش مشهد", "kind": "wholesaler", "city": "مشهد"}, "admin")
    for back, cust, days_ in ((5, "بنکدار تهران", "0"), (20, "پخش مشهد", "45"), (100, "بنکدار تهران", "0")):
        sales.create_order({"customer_name": cust, "order_date": (NOW - timedelta(days=back)).date().isoformat(),
                            "payment_days": days_}, [{"product_key": a, "qty": "100", "unit_price": "20000000"}], "admin")
    iid = sales.create_inquiry({"customer_name": "پخش مشهد", "product_key": a, "qty": "10"}, "admin")
    sales.close_inquiry(iid, "lost", "admin", "admin", {"lost_reason": "price"})
    sales.create_inquiry({"customer_name": "بنکدار تهران", "product_key": b, "qty": "10",
                          "follow_up_on": (NOW - timedelta(days=1)).date().isoformat()}, "admin")
    with svc.db() as c, db.tx(c):
        scout.upsert_businesses(c, [Business("تعمیرگاه یک", "repair", "تهران", None, None, None, "02166123456"),
                                    Business("تعمیرگاه دو", "repair", "کرج", None),
                                    Business("پخش سه", "supplier", "تهران", None)], "اوپن‌استریت‌مپ",
                                iso(NOW - timedelta(days=2)))
    sales.refresh_leads()
    sales.set_cost(a, "200", "admin")
    sales.set_cost(b, "4", "admin")
    d = management.build(svc, ms, sales, 30)
    s = d["sales"]
    assert s["total"] == 2 * 100 * 20_000_000 and s["orders"] == 2 and s["prev"] == 0 and s["change"] is None
    assert sum(m["total"] for m in s["months"]) == 3 * 100 * 20_000_000 and len(s["months"]) == 12
    assert [x["label"] for x in s["cities"]] == ["تهران", "مشهد"] and s["cities"][0]["w"] == 100
    assert d["reasons"] == [{"label": "قیمت بالاتر از رقیب", "n": 1, "w": 100.0}] and d["lost"] == 1
    kinds = {k["kind"]: k for k in d["leads"]["kinds"]}
    assert (kinds["repair"]["n"], kinds["repair"]["phone"], kinds["repair"]["new"]) == (2, 1, 2)
    assert kinds["supplier"]["n"] == 3 and kinds["seller"]["n"] == 3 and kinds["manufacturer"]["n"] == 0
    tehran = d["leads"]["cities"][0]
    assert tehran["label"] == "تهران" and [p["kind"] for p in tehran["parts"]] == ["repair", "supplier"]
    margins = {x["key"]: x for x in d["margins"]}
    assert margins[a]["margin"] < 0 and margins[a]["w"] == 0 and margins[a]["note"] == "زیر بهای تمام‌شده"
    assert margins[b]["margin"] > 0 and margins[b]["w"] == 100 and margins[b]["note"].startswith("بها ")
    texts = [x["text"] for x in d["decisions"]]
    assert any("استعلام موعد پیگیری دارد" in t for t in texts)
    kpi = {k["label"]: k for k in d["kpis"]}
    assert kpi["فروش ۳۰ روز"]["value"] == "۴۰۰ میلیون" and kpi["تبدیل استعلام"]["value"] == "۰٪"
    assert kpi["حاشیه سود (میانه)"]["tone"] in ("bad", "neutral")
    c = app.test_client()
    login(c, "admin", "admin-pass-1")
    page = text(c.get("/management?days=30"))
    assert "زیر بهای تمام‌شده" in page and "قیمت بالاتر از رقیب" in page and "فروش ماهانه" in page
    assert 'href="/sales/leads?city=%D8%AA%D9%87%D8%B1%D8%A7%D9%86"' in page


def test_time_lines_and_wording():
    t0 = datetime(2026, 10, 1, 10, 0, tzinfo=TEHRAN)
    assert viz.time_lines([], value_fmt=str, axis_fmt=str, unit="تومان") is None
    series = [{"name": "دلار", "color": "#2a78d6", "step": False,
               "points": [(t0, 260000), (t0 + timedelta(days=5), 265000), (t0 + timedelta(days=9), 270000)]},
              {"name": "لیست", "color": "#eb6834", "step": True,
               "points": [(t0, 260000), (t0 + timedelta(days=6), 268000)]}]
    ch = viz.time_lines(series, value_fmt=lambda v: str(int(v)), axis_fmt=viz.axis_toman, unit="تومان",
                        end=t0 + timedelta(days=10))
    svg = str(ch["svg"])
    assert svg.count('class="line"') == 2 and svg.count('class="end"') == 2 and svg.count('class="xdot"') == 2
    assert 'H902.0' in svg and "۲۷۰٬۰۰۰" in svg
    data = json.loads(str(ch["data"]))
    assert data["w"] == 920 and len(data["rows"]) == 5 and [v["n"] for v in data["rows"][0]["vals"]] == ["دلار", "لیست"]
    assert data["rows"][-1]["vals"][1]["v"] == "268000 تومان"
    small = viz.time_lines(series, value_fmt=str, axis_fmt=viz.axis_toman, unit="تومان", width=360, height=220,
                           compact=True)
    assert 'viewBox="0 0 360 220"' in str(small["svg"]) and "compact" in str(small["svg"])
    year = viz.time_lines(series, value_fmt=str, axis_fmt=viz.axis_toman, unit="تومان", start=t0 - timedelta(days=365),
                          end=t0 + timedelta(days=10))
    assert 3 <= str(year["svg"]).count('text-anchor="middle"') <= 7
    assert management.compact_toman(25_000_000_000) == "۲٫۵ میلیارد"
    assert management.compact_toman(30_000_000) == "۳ میلیون" and management.compact_toman(4_500_000) == "۴۵۰٬۰۰۰"
    assert management.gap_words(0.011) == "هم‌قیمت" and management.gap_words(0.05) == "۵٫۰٪ گران‌تر"
    assert management.gap_words(-0.125) == "۱۲٫۵٪ ارزان‌تر" and management.gap_words(None) == "—"
    assert management.fx_words(0.002) == "دلار هم‌اندازه نرخ لیست است"
    assert management.fx_words(0.031) == "دلار ۳٫۱٪ بالاتر از نرخ لیست" and management.fx_words(None) is None
