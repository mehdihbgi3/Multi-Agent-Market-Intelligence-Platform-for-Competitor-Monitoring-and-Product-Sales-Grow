import io
import json
import os
import quopri
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest
from openpyxl import load_workbook

from borna_pricing import __main__ as cli
from borna_pricing import clock, config, db
from borna_pricing.clock import TEHRAN
from borna_pricing.config import BrowserCfg
from borna_pricing.market import agents, prices, saved, scout, sites
from borna_pricing.market import report as market_report
from borna_pricing.market.browser import Browser, BrowserUnavailable
from borna_pricing.market.fetcher import Fetcher, FetchBlocked, Page, Response
from borna_pricing.market.service import GROUP_NOTE, MarketService
from borna_pricing.sales.service import SalesService
from borna_pricing.service import Service, ServiceError
from borna_pricing.web import create_app

from conftest import FIXTURES, write_settings

SCOUT = FIXTURES / "market" / "scout"
S = sites.SITES
Q = scout.BASE_QUERY
HTML = "text/html; charset=utf-8"
TOROB_PRODUCT = "https://torob.com/p/11111111-aaaa-4bbb-8ccc-000000000002/%D8%A8%D9%88%D8%B4/"
TOROB_GROUP = "11111111-aaaa-4bbb-8ccc-000000000002"
DK_PRODUCT = ("https://www.digikala.com/product/dkp-90000001/"
              "%D8%B3%D9%86%D8%B3%D9%88%D8%B1-%D8%A7%DA%A9%D8%B3%DB%8C%DA%98%D9%86-%D9%86%D9%85%D9%88%D9%86%D9%87/")
DK_PRODUCT_2 = "https://www.digikala.com/product/dkp-90000002/%D8%B3%D9%86%D8%B3%D9%88%D8%B1-%D8%A8%D9%88%D8%B4/"
JS_SHELL = '<!doctype html><html><head><title>دیوار</title></head><body><div id="app"></div></body></html>'
REPAIRS = {"برق خودرو و انژکتور نمونه", "مکانیکی سیار خودروهای ایرانی", "دیاگ و تنظیم موتور تخصصی"}


def fx(name):
    return (SCOUT / name).read_text(encoding="utf-8")


def at(y, m, d, hh=10, mm=0):
    clock.set_fixed_now(datetime(y, m, d, hh, mm, tzinfo=TEHRAN))


def prefix(site, repair=False):
    return site.repair_urls("", "iran")[0] if repair else site.search_urls("", 1, "iran")[0]


def find(pages, url):
    if url in pages:
        return pages[url]
    keys = [k for k in pages if k.endswith(("=", "~")) and url.startswith(k)]
    return pages[max(keys, key=len)] if keys else None


class Web:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append(url)
        body = find(self.pages, url)
        if body is None:
            return Response(404, {"content-type": "text/plain"}, b"not found")
        status, ctype, body = body if isinstance(body, tuple) else (200, HTML, body)
        return Response(status, {"content-type": ctype}, body.encode("utf-8"))


class FakeBrowser:
    def __init__(self, pages, calls, delays):
        self.pages = pages
        self.calls = calls
        self.delays = delays

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def render(self, url, domain, ready=None, collect=None, scrolls=None, delay=None):
        self.calls.append(url)
        self.delays.append(delay)
        body = find(self.pages, url)
        if body is None:
            raise FetchBlocked("سایت پاسخ HTTP 404 داد.")
        if isinstance(body, int):
            raise FetchBlocked(f"سایت پاسخ HTTP {body} داد.", status=body)
        return Page(url, 200, "text/html", body.encode("utf-8"), "utf-8")


class NoBrowser:
    made = 0

    def __init__(self):
        NoBrowser.made += 1

    def __enter__(self):
        raise BrowserUnavailable("مرورگر Edge یا Chrome روی این رایانه پیدا نشد یا باز نشد.")

    def __exit__(self, *exc):
        return False


def web_pages():
    return {prefix(S["torob"]): fx("torob_search.html"), TOROB_PRODUCT: fx("torob_product.html"),
            prefix(S["emalls"]): fx("emalls_search.html"), prefix(S["divar"]): JS_SHELL,
            prefix(S["divar"], True): fx("divar_services.html")}


def browser_pages():
    return {prefix(S["digikala"]): fx("digikala_search.html"), DK_PRODUCT: fx("digikala_product.html"),
            prefix(S["basalam"]): fx("basalam_search.html"), prefix(S["divar"]): fx("divar_parts.html")}


def settings(**over):
    sc = {"enabled": "true", "queries_per_site": "1", "pages_per_query": "1", "product_pages": "1",
          "delay_seconds": "1", "osm": "false", **over}
    return "[scout]\n" + "".join(f"{k} = {v}\n" for k, v in sc.items()) + "\n[browser]\nenabled = true\n"


class Env:
    def __init__(self, tmp_path, browser_factory=None, **over):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.cfg = config.load_config(write_settings(tmp_path, settings(**over), scout=True))
        self.svc = Service(self.cfg)
        at(2026, 11, 5)
        self.svc.init()
        self.web = Web(web_pages())
        self.bpages = browser_pages()
        self.bcalls = []
        self.bdelays = []
        self.sleeps = []
        self.clock = [0.0]
        c = self.cfg.collector

        def fetchers():
            return Fetcher(c.user_agent, max_bytes=c.max_bytes, min_delay=0, resolver=lambda h, p: ["93.184.216.34"],
                           transport=self.web, sleep=self.sleep, clock=lambda: self.clock[0],
                           respect_robots=c.respect_robots)
        self.ms = MarketService(self.svc, fetchers,
                                browser_factory=browser_factory or (lambda: FakeBrowser(self.bpages, self.bcalls,
                                                                                       self.bdelays)))
        self.ms.sales = SalesService(self.svc, self.ms)

    def sleep(self, seconds):
        self.sleeps.append((self.web.calls[-1] if self.web.calls else "", round(seconds)))
        self.clock[0] += seconds

    def run(self, user="admin"):
        return self.ms.run_agent("market_scout", "manual", user)

    def listings(self, site_key):
        with self.ms.db() as c:
            return [dict(r) for r in c.execute(
                "SELECT e.* FROM external_products e JOIN sources s ON s.id=e.source_id WHERE s.key=? ORDER BY e.id",
                (scout.SOURCE_PREFIX + site_key,))]

    def source(self, site_key):
        with self.ms.db() as c:
            return dict(c.execute("SELECT * FROM sources WHERE key=?", (scout.SOURCE_PREFIX + site_key,)).fetchone())


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_torob_search_and_product_pages():
    r = S["torob"].parse_search(fx("torob_search.html"), prefix(S["torob"]))
    assert (r.cards, r.skipped) == (4, 1)
    assert [(i.seller, i.price, i.group) for i in r.items] == [
        ("یدکی نمونه الف", "۲٫۴۵۰٫۰۰۰ تومان", "11111111-aaaa-4bbb-8ccc-000000000001"),
        ("پخش قطعات نمونه", "ناموجود", "11111111-aaaa-4bbb-8ccc-000000000004")]
    assert r.items[0].ext_id == "11111111-aaaa-4bbb-8ccc-000000000001|یدکی نمونه الف"
    assert r.product_urls == [TOROB_PRODUCT]
    p = S["torob"].parse_product(fx("torob_product.html"), TOROB_PRODUCT + "?utm=1")
    got = {i.seller: (i.price, i.availability, i.city, i.group, i.url) for i in p.items}
    assert got == {"یدکی نمونه ب": ("39000000 ریال", "in_stock", "شیراز", TOROB_GROUP, TOROB_PRODUCT),
                   "فروشگاه نمونه ج": ("52000000 ریال", "in_stock", "تبریز", TOROB_GROUP, TOROB_PRODUCT),
                   "قطعه نمونه د": (None, "out_of_stock", None, TOROB_GROUP, TOROB_PRODUCT),
                   "باسلام": ("45000000 ریال", "in_stock", None, TOROB_GROUP, TOROB_PRODUCT)}
    assert S["torob"].parse_product(fx("torob_product.html"), "https://torob.com/search/?query=x").items == []
    assert S["torob"].parse_product("<html></html>", TOROB_PRODUCT).items == []


def test_emalls_digikala_basalam_and_divar_pages():
    r = S["emalls"].parse_search(fx("emalls_search.html"), prefix(S["emalls"]))
    assert (r.cards, r.skipped) == (4, 1)
    assert [(i.ext_id, i.seller) for i in r.items] == [("em800000003-700000003", "باسلام"),
                                                       ("em0-700000001", "یدکی نمونه الف"),
                                                       ("em800000002-700000002", "چین یدک نمونه")]
    assert [i.url for i in r.items] == [prefix(S["emalls"]), "https://yadaki-nemone.ir/",
                                        "https://emalls.ir/%D9%85%D8%B4%D8%AE%D8%B5%D8%A7%D8%AA_%D8%B3%D9%86%D8%B3%D9%88%D8%B1"
                                        "-%D8%A7%DA%A9%D8%B3%DB%8C%DA%98%D9%86-%D9%84%DB%8C%D9%81%D8%A7%D9%86~id~800000002"]
    assert not sites.on_platform("https://yadaki-nemone.ir/") and sites.on_platform("https://statics.basalam.com/x.jpg")
    r = S["digikala"].parse_search(fx("digikala_search.html"), prefix(S["digikala"]))
    assert (r.cards, r.skipped, r.items) == (4, 1, [])
    assert r.product_urls == [DK_PRODUCT, DK_PRODUCT_2]
    html = fx("digikala_product.html")
    r = S["digikala"].parse_product(html, DK_PRODUCT)
    assert [(i.ext_id, i.seller, i.price, i.availability) for i in r.items] == [
        ("dkp-90000001|AB12C", "فروشگاه نمونه الف", "۴,۳۰۰,۰۰۰ تومان", "unknown"),
        ("dkp-90000001|XY34Z", "یدک نمونه ب", "۴,۵۰۰,۰۰۰ تومان", "unknown"),
        ("dkp-90000001|QW56E", "قطعه نمونه ج", None, "out_of_stock")]
    assert {i.group for i in r.items} == {"dkp-90000001"}
    alone = S["digikala"].parse_product(html.replace("فروشندگان این کالا", ""), DK_PRODUCT)
    assert [(i.ext_id, i.price) for i in alone.items] == [("dkp-90000001|AB12C", "۴,۳۰۰,۰۰۰ تومان")]
    assert S["digikala"].parse_product(html, "https://www.digikala.com/search/?q=x").items == []
    r = S["basalam"].parse_search(fx("basalam_search.html"), prefix(S["basalam"]))
    assert (r.cards, r.skipped) == (5, 1)
    assert [(i.ext_id, i.seller, i.city, i.price, i.list_price) for i in r.items] == [
        ("bsl-50000001", "یدکی نمونه اصفهان", "اصفهان", "1٬850٬000 تومان", None),
        ("bsl-50000002", "قطعه نمونه مشهد", "مشهد", "1٬800٬000 تومان", "2٬000٬000 تومان"),
        ("bsl-50000003", "لوازم نمونه کرج", "کرج", "950٬000 تومان", None)]
    assert r.items[0].url == "https://basalam.com/yadaki-nemone/product/50000001"
    r = S["divar"].parse_search(fx("divar_parts.html"), prefix(S["divar"]))
    assert [(i.ext_id, i.price, i.city, i.seller) for i in r.items] == [
        ("dvr-AAAA1111", "۱,۲۰۰,۰۰۰ تومان", "شیراز", None), ("dvr-BBBB2222", "توافقی", "تهران", None),
        ("dvr-CCCC3333", "۸۵۰,۰۰۰ تومان", "کرج", None)]
    assert r.items[1].url == "https://divar.ir/v/%D8%B2%DB%8C%D9%85%D9%86%D8%B3/BBBB2222"
    r = S["divar"].parse_repairs(fx("divar_services.html"), prefix(S["divar"], True))
    assert (r.cards, r.skipped) == (6, 3)
    assert [(b.name, b.kind, b.city) for b in r.businesses] == [
        ("برق خودرو و انژکتور نمونه", "repair", "اصفهان"), ("مکانیکی سیار خودروهای ایرانی", "repair", "کرج"),
        ("دیاگ و تنظیم موتور تخصصی", "repair", "تهران")]
    assert S["divar"].parse_search(JS_SHELL, prefix(S["divar"])).cards == 0


def test_search_addresses_groups_and_prices():
    assert S["torob"].search_urls("سنسور اکسیژن پژو ۲۰۶", 2, "iran") == [
        "https://torob.com/search/?query=%D8%B3%D9%86%D8%B3%D9%88%D8%B1%20%D8%A7%DA%A9%D8%B3%DB%8C%DA%98%D9%86"
        "%20%D9%BE%DA%98%D9%88%20%DB%B2%DB%B0%DB%B6"]
    em = S["emalls"].search_urls("سنسور  اکسیژن", 3, "iran")
    assert len(em) == 3 and em[0].startswith("https://emalls.ir/%D9%84%DB%8C%D8%B3%D8%AA-%D9%82%DB%8C%D9%85%D8%AA~skey~")
    assert em[0].endswith("%D8%B3%D9%86%D8%B3%D9%88%D8%B1%20%D8%A7%DA%A9%D8%B3%DB%8C%DA%98%D9%86")
    assert em[2].endswith("~page~3")
    assert S["divar"].search_urls("x", 1, "tehran") == ["https://divar.ir/s/tehran/car-accessories?q=x"]
    assert S["divar"].repair_urls("دیاگ", "iran")[0].startswith("https://divar.ir/s/iran/vehicles-services?q=%D8%AF")
    assert S["basalam"].search_urls("a b", 1, "iran") == ["https://basalam.com/s?q=a%20b"]
    assert S["digikala"].group_of(DK_PRODUCT) == "dkp-90000001" and S["digikala"].group_of("https://x/") is None
    assert S["torob"].group_of(TOROB_PRODUCT) == TOROB_GROUP and S["emalls"].group_of(TOROB_PRODUCT) is None
    assert prices.parse_price("۵٫۹۱۲٫۰۰۰ تومان", "toman").value_rial == 59_120_000
    assert prices.parse_price("۲٫۵ میلیون تومان", "toman").value_rial == 25_000_000
    assert prices.parse_price("۱٫۲۵ تومان", "toman").kind is None


def test_queries_come_from_borna_products_and_rotate(env):
    qs = scout.product_queries([{"part_nos": ["OZA608-U2", "0258006028"]}, {"part_nos": ["OZA608-U2"]}],
                               ("سنسور  لامبدا", "OZA608-U2"))
    assert qs[0] == Q and "سنسور اکسیژن پراید" in qs and "سنسور اکسیژن ME17" in qs
    assert qs[-3:] == ["OZA608-U2", "0258006028", "سنسور لامبدا"]
    assert len(qs) == len(set(qs))
    real = scout.product_queries(env.ms.profiles())
    assert len(real) > 26 and real[0] == Q
    assert scout.peek(env.ms, "torob", ["a", "b", "c"], 2) == ["a", "b"]
    assert scout.peek(env.ms, "torob", ["a", "b", "c"], 2) == ["a", "b"]
    scout.advance(env.ms, "torob", 2, 3)
    assert scout.peek(env.ms, "torob", ["a", "b", "c"], 2) == ["c", "a"]
    scout.advance(env.ms, "torob", 1, 3)
    assert scout.peek(env.ms, "torob", ["a", "b", "c"], 2) == ["a", "b"]
    scout.advance(env.ms, "torob", 0, 3)
    assert scout.peek(env.ms, "torob", ["a", "b", "c"], 1) == ["a"]
    assert scout.peek(env.ms, "emalls", ["a", "b", "c"], 5) == ["a", "b", "c"]
    assert scout.peek(env.ms, "torob", [], 3) == []


def test_scout_finds_prices_sellers_and_repair_shops(env):
    res = env.run()
    assert res["status"] == "ok" and res["message"] is None
    st = res["stats"]
    want = {"torob": (2, 6, 0), "emalls": (1, 3, 0), "digikala": (2, 3, 0), "basalam": (1, 3, 0), "divar": (2, 3, 3)}
    assert {k: (v["pages"], v["items"], v["businesses"]) for k, v in st["sites"].items()} == want
    assert all(v["healthy"] for v in st["sites"].values())
    assert (st["pages"], st["items"], st["observations"], st["new_listings"]) == (8, 18, 18, 18)
    assert (st["businesses"], st["new_businesses"], st["n_errors"], st["errors"]) == (3, 3, 0, [])
    assert st["leads"] == {"sellers": 12, "new": 12, "removed": 0} and st["matching"]["listings"] == 18
    assert not [u for u in env.web.calls if u.endswith("/robots.txt")]
    assert not [u for u in env.web.calls if "digikala" in u or "basalam" in u]
    assert not [u for u in env.bcalls if "torob" in u or "emalls" in u]
    assert [u for u in env.bcalls if "divar" in u] == [S["divar"].search_urls(Q, 1, "iran")[0]]
    with env.ms.db() as c:
        srcs = [dict(r) for r in c.execute("SELECT * FROM sources WHERE kind!='manual' ORDER BY id")]
        assert [s["key"] for s in srcs] == ["scout-torob", "scout-emalls", "scout-digikala", "scout-basalam",
                                            "scout-divar"]
        assert {(s["policy_status"], s["policy_by"], s["created_by"], s["failure_count"], s["start_urls"])
                for s in srcs} == {("approved", "system", "system", 0, "[]")}
        assert all(s["policy_note"] == scout.POLICY_NOTE and s["last_success_at"] for s in srcs)
        assert c.execute("SELECT COUNT(*) FROM audit WHERE action='source_create' AND username='system'").fetchone()[0] == 5
        obs = {r["seller"]: dict(r) for r in c.execute(
            "SELECT e.seller, o.kind, o.price_rial, o.availability FROM observations o"
            " JOIN external_products e ON e.id=o.external_product_id JOIN sources s ON s.id=e.source_id"
            " WHERE s.key='scout-torob'")}
    assert obs["یدکی نمونه الف"]["price_rial"] == 24_500_000 and obs["یدکی نمونه ب"]["price_rial"] == 39_000_000
    assert obs["قطعه نمونه د"]["availability"] == "out_of_stock"
    group = [e for e in env.listings("torob") if json.loads(e["attrs"]).get("group") == TOROB_GROUP]
    assert len(group) == 4
    assert {json.loads(e["attrs"]).get("city") for e in env.listings("basalam")} == {"اصفهان", "مشهد", "کرج"}
    sales = env.ms.sales
    leads = {r["name"]: r for r in sales.leads()}
    assert len(leads) == 15 and "باسلام" not in leads
    assert {n for n, r in leads.items() if r["kind"] == "repair"} == REPAIRS
    assert leads["پخش قطعات نمونه"]["kind"] == "supplier"
    assert leads["یدکی نمونه الف"]["sources_l"] == ["ایمالز", "ترب"] and leads["یدکی نمونه الف"]["n_listings"] == 2
    assert (leads["یدکی نمونه الف"]["price_min_rial"], leads["یدکی نمونه الف"]["price_max_rial"]) == (23_500_000, 24_500_000)
    assert leads["یدکی نمونه ب"]["city"] == "شیراز" and leads["لوازم نمونه کرج"]["city"] == "کرج"
    assert leads["مکانیکی سیار خودروهای ایرانی"]["sources_l"] == ["دیوار"]
    assert leads["مکانیکی سیار خودروهای ایرانی"]["example_url"].endswith("/RRRR0002")
    assert leads["یدکی نمونه الف"]["example_url"] == "https://yadaki-nemone.ir/"
    assert leads["چین یدک نمونه"]["example_url"].startswith("https://emalls.ir/") and "~id~800000002" in leads[
        "چین یدک نمونه"]["example_url"]
    assert all(r["example_url"] for r in leads.values())
    assert sales.lead_cities()[:2] == [("اصفهان", 2), ("کرج", 2)]
    assert {r["name"] for r in sales.leads(kind="repair", city="کرج")} == {"مکانیکی سیار خودروهای ایرانی"}
    review = {e["seller"]: e for e in env.ms.review_queue()}
    assert sorted(review["یدکی نمونه ب"]["group_others"]) == sorted(["فروشگاه نمونه ج", "قطعه نمونه د", "باسلام"])
    assert not {"فروشگاه نمونه ج", "قطعه نمونه د"} & set(review)


def test_second_run_rotates_words_and_does_not_duplicate(env):
    env.run()
    first_calls, first_browser = len(env.web.calls), len(env.bcalls)
    env.bpages[DK_PRODUCT_2] = fx("digikala_product.html")
    at(2026, 11, 6, 11)
    res = env.run()
    st = res["stats"]
    assert res["status"] == "ok"
    assert {k: v["new_listings"] for k, v in st["sites"].items()} == {"torob": 0, "emalls": 0, "digikala": 3,
                                                                      "basalam": 0, "divar": 0}
    assert (st["new_businesses"], st["businesses"]) == (0, 3)
    assert DK_PRODUCT_2 in env.bcalls[first_browser:] and DK_PRODUCT not in env.bcalls[first_browser:]
    empty = fx("torob_product.html").split('"offers":[')[0] + '"offers":[]}}</script></head></html>'
    env.web.pages[TOROB_PRODUCT] = env.bpages[TOROB_PRODUCT] = empty
    at(2026, 11, 7, 11)
    res = env.run()
    assert res["status"] == "ok" and res["stats"]["sites"]["torob"]["items"] == 2 and not res["stats"]["errors"]
    second = S["torob"].search_urls(scout.product_queries(env.ms.profiles())[1], 1, "iran")[0]
    assert second in env.web.calls[first_calls:]
    assert S["divar"].repair_urls(env.cfg.scout.repair_keywords[1], "iran")[0] in env.web.calls[first_calls:]
    with env.ms.db() as c:
        assert c.execute("SELECT COUNT(*) FROM sources WHERE created_by='system'").fetchone()[0] == 5
        assert c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 15
        assert db.meta_get(c, "scout_cursor:torob") == "3" and db.meta_get(c, "scout_cursor:divar_repair") == "3"


def test_products_never_seen_are_read_first(env):
    env.run()
    src = env.source("torob")
    other = "https://torob.com/p/11111111-aaaa-4bbb-8ccc-000000000009/x/"
    ordered = scout._order_products(env.ms, src, S["torob"], [TOROB_PRODUCT, other, TOROB_PRODUCT], 5)
    assert ordered == [other, TOROB_PRODUCT]
    assert scout._order_products(env.ms, src, S["torob"], [TOROB_PRODUCT, other], 1) == [other]
    assert scout._order_products(env.ms, src, S["torob"], [other], 0) == []


def test_blocked_scout_source_is_skipped(env):
    env.run()
    for key in S:
        if key != "torob":
            env.ms.set_policy(env.source(key)["id"], "blocked", "تصمیم مدیریت برنا", "admin")
    at(2026, 11, 6, 11)
    calls = len(env.web.calls)
    res = env.run()
    assert set(res["stats"]["sites"]["divar"]) == {"skipped"} and res["stats"]["sites"]["torob"]["healthy"]
    assert all("emalls" not in u and "divar" not in u for u in env.web.calls[calls:])
    env.ms.set_policy(env.source("torob")["id"], "blocked", "تصمیم مدیریت برنا", "admin")
    res = env.run()
    assert res["status"] == "skipped" and "ممنوع" in res["message"]
    with pytest.raises(ServiceError, match="ممنوع"):
        env.ms.collect_source(env.source("torob")["id"], "admin")


def test_missing_browser_leaves_other_sites_working(tmp_path):
    NoBrowser.made = 0
    e = Env(tmp_path, browser_factory=NoBrowser)
    res = e.run()
    sites_st = res["stats"]["sites"]
    assert res["status"] == "partial" and NoBrowser.made == 1
    assert sites_st["torob"]["healthy"] and sites_st["emalls"]["healthy"]
    assert not sites_st["digikala"]["healthy"] and not sites_st["basalam"]["healthy"]
    assert sites_st["divar"]["healthy"] and sites_st["divar"]["items"] == 0 and sites_st["divar"]["businesses"] == 3
    assert any("Edge یا Chrome" in x for x in res["stats"]["errors"])
    assert any(x.startswith("دیوار") and "بدون مرورگر نامرئی چیزی نداشت" in x for x in res["stats"]["errors"])
    assert e.source("digikala")["failure_count"] == 1 and "Edge یا Chrome" in e.source("digikala")["last_error"]


def test_light_failure_without_http_answer_falls_back_to_browser(tmp_path):
    e = Env(tmp_path)
    e.web.pages[prefix(S["emalls"])] = (200, "application/octet-stream", "x")
    e.bpages[prefix(S["emalls"])] = fx("emalls_search.html")
    e.web.pages[prefix(S["torob"])] = (404, HTML, "not found")
    res = e.run()
    st = res["stats"]["sites"]
    assert st["emalls"]["items"] == 3 and st["emalls"]["healthy"]
    assert [u for u in e.bcalls if "emalls" in u] == [S["emalls"].search_urls(Q, 1, "iran")[0]]
    assert not st["torob"]["healthy"] and not st["torob"]["blocked"] and res["status"] == "partial"
    assert not [u for u in e.bcalls if "torob" in u]
    err = next(x for x in res["stats"]["errors"] if x.startswith("ترب"))
    assert "404" in err and "مرورگر" not in err
    f = Env(tmp_path / "b")
    f.web.pages[prefix(S["emalls"])] = (200, "application/octet-stream", "x")
    res = f.run()
    err = next(x for x in res["stats"]["errors"] if x.startswith("ایمالز"))
    assert "صفحه وب نیست" in err and "مرورگر نامرئی هم نتوانست" in err


def test_blocking_answer_stops_the_site_until_the_next_run(tmp_path):
    e = Env(tmp_path, queries_per_site="3", product_pages="3")
    e.web.pages[prefix(S["torob"])] = (490, HTML, "too many")
    e.bpages[prefix(S["basalam"])] = 403
    res = e.run()
    st = res["stats"]["sites"]
    assert st["torob"]["blocked"] and not st["torob"]["healthy"] and st["torob"]["pages"] == 0
    assert len([u for u in e.web.calls if "torob" in u]) == 1 and not [u for u in e.bcalls if "torob" in u]
    assert st["basalam"]["blocked"] and len([u for u in e.bcalls if "basalam" in u]) == 1
    assert not st["emalls"]["blocked"] and st["emalls"]["healthy"]
    err = next(x for x in res["stats"]["errors"] if x.startswith("ترب"))
    assert "490" in err and "تا اجرای بعد متوقف شد" in err
    with e.ms.db() as c:
        assert db.meta_get(c, "scout_cursor:torob") is None and db.meta_get(c, "scout_cursor:emalls") == "3"
    first = [u for u in e.web.calls if "torob.com/search" in u][0]
    e.web.pages[prefix(S["torob"])] = fx("torob_search.html")
    at(2026, 11, 6, 11)
    res = e.run()
    assert [u for u in e.web.calls if "torob.com/search" in u][1] == first
    assert res["stats"]["sites"]["torob"]["healthy"] and not res["stats"]["sites"]["torob"]["blocked"]
    with e.ms.db() as c:
        assert db.meta_get(c, "scout_cursor:torob") == "3"


def test_torob_is_read_slower_and_with_fewer_words(tmp_path):
    e = Env(tmp_path, queries_per_site="5")
    assert e.cfg.scout.delay_for("torob") == 15 and e.cfg.scout.queries_for("torob") == 3
    assert e.cfg.scout.delay_for("emalls") == 1 and e.cfg.scout.queries_for("emalls") == 5
    e.run()
    torob = [u for u in e.web.calls if "torob.com/search" in u]
    emalls = [u for u in e.web.calls if "emalls" in u]
    assert len(torob) == 3 and len(emalls) == 5
    assert {s for u, s in e.sleeps if "torob" in u} == {15}
    assert {s for u, s in e.sleeps if "emalls" in u} == {1}
    assert set(e.bdelays) == {1}
    custom = Env(tmp_path / "c", site_delay_seconds="torob: 30، divar: 8", site_queries="torob: 1")
    assert custom.cfg.scout.delay_for("torob") == 30 and custom.cfg.scout.delay_for("divar") == 8
    assert custom.cfg.scout.queries_for("torob") == 1
    for bad in ("amazon: 3", "torob 3", "torob: 0"):
        with pytest.raises(config.ConfigError):
            Env(tmp_path / "d", site_queries=bad)


def test_scout_switched_off_or_crashing_site(tmp_path, monkeypatch):
    e = Env(tmp_path / "a", enabled="false")
    res = e.run()
    assert res["status"] == "failed" and "خاموش" in res["message"]
    a = agents.AGENTS["market_scout"]
    assert not a.due(e.ms, clock.now_utc()) and "خاموش" in a.next_info(e.ms, clock.now_utc())
    e = Env(tmp_path / "b", sites="torob")

    def boom(html, url):
        raise RuntimeError("bad markup")
    monkeypatch.setattr(S["torob"], "parse_search", boom)
    res = e.run()
    assert res["status"] == "failed" and list(res["stats"]["sites"]) == ["torob"]
    assert any("خطای داخلی هنگام خواندن ترب: RuntimeError" in x for x in res["stats"]["errors"])


def test_scout_agent_schedule_and_registry(env):
    a = agents.AGENTS["market_scout"]
    analyst = agents.AGENTS["market_analyst"]
    assert list(agents.AGENTS)[:2] == ["price_watcher", "market_scout"] and "market_scout" in agents.registry()
    now = clock.now_utc()
    assert a.due(env.ms, now) and a.next_info(env.ms, now) == "نوبت بعد: همین حالا"
    assert not analyst.due(env.ms, now)
    env.run()
    assert analyst.due(env.ms, now)
    assert not a.due(env.ms, now) and not a.due(env.ms, now + timedelta(hours=23))
    assert a.due(env.ms, now + timedelta(hours=25)) and a.next_info(env.ms, now).startswith("نوبت بعد: ")
    assert agents.AGENTS["price_watcher"].next_info(env.ms, now) == "هیچ منبع مجاز و فعالی با نشانی صفحه نیست."
    assert agents.AGENTS["price_watcher"].run(env.ms, None, "system")["_status"] == "skipped"


def test_group_decisions_cover_the_same_product_in_other_shops(env):
    env.run()
    group = [e for e in env.listings("torob") if json.loads(e["attrs"]).get("group") == TOROB_GROUP]
    head, sibs = group[0], group[1:]
    main = next(m for m in env.ms.listing(head["id"])["matches"] if m["product_key"] == "FIB201-400")
    env.ms.decide_match(main["id"], True, "finance")
    for e in group:
        lst = env.ms.listing(e["id"])
        assert lst["match_state"] == "matched"
        approved = [m for m in lst["matches"] if m["status"] == "approved"]
        assert [(m["product_key"], m["relation"]) for m in approved] == [("FIB201-400", "equivalent")]
        assert next(m for m in lst["matches"] if m["product_key"] == "FIB109-400")["status"] == "rejected"
    assert all(m["note"] == GROUP_NOTE for e in sibs for m in env.ms.listing(e["id"])["matches"]
               if m["status"] == "approved")
    assert not [e for e in env.ms.review_queue() if e["id"] in {x["id"] for x in group}]
    one = next(m for m in env.ms.listing(sibs[0]["id"])["matches"] if m["status"] == "approved")
    env.ms.unlink(one["id"], "finance")
    assert {env.ms.listing(e["id"])["match_state"] for e in group} == {"unmatched"}
    env.ms.manual_link(sibs[1]["id"], "FIB201-400", "finance", "همان کالا")
    assert {env.ms.listing(e["id"])["match_state"] for e in group} == {"matched"}
    with env.ms.db() as c:
        audit = json.loads(c.execute("SELECT details FROM audit WHERE action='match_manual' ORDER BY id DESC LIMIT 1")
                           .fetchone()[0])
    assert audit["group_listings"] == 3
    env.web.pages[TOROB_PRODUCT] = fx("torob_product.html").replace(
        '"offers":[', '"offers":[{"@type":"Offer","price":"47000000","priceCurrency":"IRR","name":"فروشگاه تازه نمونه",'
                      '"availability":"http://schema.org/InStock"},')
    at(2026, 11, 6, 11)
    env.run()
    fresh = next(e for e in env.listings("torob") if e["seller"] == "فروشگاه تازه نمونه")
    m = next(m for m in env.ms.listing(fresh["id"])["matches"] if m["status"] == "approved")
    assert (m["product_key"], m["verdict"], json.loads(m["reasons"]), m["decided_by"]) == (
        "FIB201-400", "manual", ["group"], "finance")


def test_outlier_prices_wait_for_review_before_counting(env):
    extra = ('{"@type":"Offer","price":"41000000","priceCurrency":"IRR","name":"فروشگاه پنجم",'
             '"availability":"http://schema.org/InStock"},{"@type":"Offer","price":"150000000","priceCurrency":"IRR",'
             '"name":"فروشگاه گران","availability":"http://schema.org/InStock"},')
    env.web.pages[TOROB_PRODUCT] = fx("torob_product.html").replace('"offers":[', '"offers":[' + extra)
    env.run()
    group = [e for e in env.listings("torob") if json.loads(e["attrs"]).get("group") == TOROB_GROUP]
    assert len(group) == 6
    main = next(m for m in env.ms.listing(group[0]["id"])["matches"] if m["product_key"] == "FIB201-400")
    env.ms.decide_match(main["id"], True, "finance")

    def latest():
        with env.ms.db() as c:
            return {r["seller"]: dict(r) for r in c.execute(
                "SELECT e.seller, o.* FROM observations o JOIN external_products e ON e.id=o.external_product_id"
                " WHERE json_extract(e.attrs, '$.group')=? ORDER BY o.id", (TOROB_GROUP,))}

    obs = latest()
    assert obs["فروشگاه گران"]["status"] == "quarantined" and "قیمت پرت" in obs["فروشگاه گران"]["status_note"]
    assert {obs[s]["status"] for s in ("یدکی نمونه ب", "فروشگاه نمونه ج", "باسلام", "فروشگاه پنجم")} == {"ok"}
    row = {r["product"]["key"]: r for r in env.ms.overview()["table"]}["FIB201-400"]
    assert row["stats"]["retail"]["median"] == 43_000_000 and row["stats"]["retail"]["n_priced"] == 4
    assert not row["low_data"]
    review = next(o for o in env.ms.observations_to_review() if o["id"] == obs["فروشگاه گران"]["id"])
    assert review["product_key"] == "FIB201-400"
    env.ms.decide_observation(review["id"], True, "finance", "قیمت درست است")
    env.ms.match_pending()
    assert latest()["فروشگاه گران"]["status"] == "ok"
    row = {r["product"]["key"]: r for r in env.ms.overview()["table"]}["FIB201-400"]
    assert row["stats"]["retail"]["median"] == 45_000_000 and row["stats"]["retail"]["n_priced"] == 5


def test_bulk_strong_approval_reaches_the_whole_group(env):
    env.run()
    group = [e for e in env.listings("torob") if json.loads(e["attrs"]).get("group") == TOROB_GROUP]
    with env.ms.db() as c, db.tx(c):
        c.execute("DELETE FROM matches WHERE external_product_id IN (%s) AND product_key!='FIB201-400'"
                  % ",".join(str(e["id"]) for e in group))
        c.execute("UPDATE matches SET verdict='strong' WHERE external_product_id=?", (group[0]["id"],))
    assert env.ms.approve_strong("finance") == 1
    assert {env.ms.listing(e["id"])["match_state"] for e in group} == {"matched"}


def test_weekly_report_lists_new_businesses(env):
    env.run()
    rid = env.ms.build_report("admin")
    rep = env.ms.report(rid)
    d = rep["data"]["discovery"]
    assert d["n_new"] == 15 and d["totals"] == {"repair": 3, "seller": 11, "supplier": 1}
    assert {b["name"] for b in d["new"] if b["kind"] == "repair"} == REPAIRS
    text = next(i["text"] for i in rep["data"]["insights"] if i["kind"] == "discovery")
    assert "۱۱ فروشنده" in text and "۱ عمده‌فروش / تأمین‌کننده" in text and "۳ تعمیرگاه" in text
    assert "اصفهان (۲)" in text and "کرج (۲)" in text
    wb = load_workbook(io.BytesIO(market_report.xlsx(rep, "برنا سنسور")))
    sheet = wb["کسب‌وکارهای تازه"]
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 15 and ("مکانیکی سیار خودروهای ایرانی", "تعمیرگاه", "کرج", None, None, "دیوار") in [
        r[:6] for r in rows]
    at(2026, 11, 20)
    later = env.ms.report(env.ms.build_report("admin"))
    assert later["data"]["discovery"]["n_new"] == 0
    assert "کسب‌وکارهای تازه" not in load_workbook(io.BytesIO(market_report.xlsx(later, "برنا سنسور"))).sheetnames


def saved_page(name, url=None):
    body = fx(name)
    return ((f"<!DOCTYPE html>\n<!-- saved from url=({len(url):04d}){url} -->\n" if url else "") + body).encode("utf-8")


def mhtml(name, url):
    body = quopri.encodestring(fx(name).encode("utf-8")).decode("ascii")
    boundary = "----MultipartBoundary--sample----"
    return (f"From: <Saved by Blink>\r\nSnapshot-Content-Location: {url}\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: multipart/related;\r\n\ttype=\"text/html\";\r\n\tboundary=\"{boundary}\"\r\n\r\n"
            f"--{boundary}\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n"
            f"Content-Location: {url}\r\n\r\n{body}\r\n--{boundary}--\r\n").encode("ascii")


def test_saved_pages_are_read_like_the_scout(env):
    ms = env.ms
    r = saved.import_page(ms, saved_page("torob_search.html", S["torob"].search_urls(Q, 1, "iran")[0]), "t.html",
                          "auto", "", "finance", "approver")
    assert (r["site"], r["kind"], r["items"], r["observations"], r["product_urls"]) == ("torob", "search", 2, 2, 1)
    assert "کالا چند فروشنده دارد" in saved.summary(r) and "ترب" in saved.summary(r)
    r = saved.import_page(ms, saved_page("torob_product.html", TOROB_PRODUCT), "p.html", "auto", "", "finance",
                          "approver")
    assert (r["kind"], r["items"], r["new_listings"]) == ("product", 4, 4)
    r = saved.import_page(ms, mhtml("digikala_product.html", DK_PRODUCT), "d.mhtml", "auto", "", "finance", "approver")
    assert (r["site"], r["kind"], r["items"]) == ("digikala", "product", 3)
    url = S["divar"].repair_urls("برق خودرو", "iran")[0]
    r = saved.import_page(ms, saved_page("divar_services.html", url), "s.html", "auto", "", "finance", "approver")
    assert (r["kind"], r["businesses"], r["new_businesses"], r["items"]) == ("repair", 3, 3, 0)
    r = saved.import_page(ms, saved_page("divar_services.html"), "s.html", "divar_repair", "", "finance", "approver")
    assert (r["kind"], r["new_businesses"]) == ("repair", 0)
    with pytest.raises(ServiceError, match="شناخته نشد"):
        saved.import_page(ms, saved_page("basalam_search.html"), "b.html", "auto", "", "sales", "viewer")
    r = saved.import_page(ms, saved_page("basalam_search.html"), "b.html", "basalam", "", "sales", "viewer")
    assert (r["items"], r["pending"], r["observations"]) == (3, 3, 3) and "منتظر تأیید" in saved.summary(r)
    with ms.db() as c:
        st = {row["status"] for row in c.execute(
            "SELECT o.status FROM observations o JOIN external_products e ON e.id=o.external_product_id"
            " JOIN sources s ON s.id=e.source_id WHERE s.key='scout-basalam'")}
        snap = dict(c.execute("SELECT * FROM snapshots WHERE filename='b.html'").fetchone())
        audit = c.execute("SELECT COUNT(*) FROM audit WHERE action='page_upload'").fetchone()[0]
    assert st == {"pending"} and snap["kind"] == "page" and snap["url"] == "https://basalam.com/" and audit == 6
    assert {o["seller"] for o in ms.observations_to_review() if o["source_name"] == "باسلام"} >= {"یدکی نمونه اصفهان"}
    assert "یدکی نمونه اصفهان" in {r["name"] for r in ms.sales.leads()}
    with pytest.raises(ServiceError, match="مال ترب نیست"):
        saved.import_page(ms, saved_page("emalls_search.html"), "e.html", "torob", "https://emalls.ir/x", "a", "admin")
    with pytest.raises(ServiceError, match="صفحه وب نیست"):
        saved.import_page(ms, b"just some text", "x.txt", "auto", "", "a", "admin")
    with pytest.raises(ServiceError, match="کالا یا تعمیرگاهی پیدا نشد"):
        saved.import_page(ms, JS_SHELL.encode("utf-8"), "x.html", "divar", "", "a", "admin")
    with pytest.raises(ServiceError, match="http"):
        saved.import_page(ms, saved_page("torob_search.html"), "x.html", "torob", "ftp://torob.com/x", "a", "admin")
    with pytest.raises(ServiceError, match="فایلی انتخاب نشده"):
        saved.import_page(ms, b"", "", "auto", "", "a", "admin")
    with pytest.raises(ServiceError, match="آگهی تکی دیوار"):
        saved.import_page(ms, saved_page("divar_parts.html", "https://divar.ir/v/x/AAAA1111"), "v.html", "auto", "",
                          "a", "admin")
    ms.set_policy(scout.ensure_source(ms, S["emalls"])["id"], "blocked", "تصمیم مدیریت برنا", "admin")
    r = saved.import_page(ms, saved_page("emalls_search.html"), "e.html", "emalls", "", "finance", "approver")
    assert r["items"] == 3


def login(client, user, pw):
    client.get("/login")
    with client.session_transaction() as s:
        tok = s.get("_csrf")
    r = client.post("/login", data={"username": user, "password": pw, "_csrf": tok})
    assert r.status_code == 302
    with client.session_transaction() as s:
        return s["_csrf"]


def page(r):
    return r.get_data(as_text=True)


def test_web_pages_for_scout_sources_and_businesses(env):
    env.svc.create_user("admin", "admin-pass-1", "admin", "مدیر سامانه", "test")
    env.svc.create_user("sales", "sales-pass-1", "viewer", "کارشناس فروش", "test")
    app = create_app(env.cfg, env.svc, env.ms, env.ms.sales)
    app.config["TESTING"] = True
    admin = app.test_client()
    tok = login(admin, "admin", "admin-pass-1")
    assert "کاشف بازار هر روز خودش" in page(admin.get("/market"))
    r = admin.post("/market/agents/market_scout/run", data={"_csrf": tok}, follow_redirects=True)
    assert "کاشف بازار در پس‌زمینه شروع شد" in page(r)
    for t in [t for t in threading.enumerate() if t.name == "borna-market_scout"]:
        t.join(30)
    run = env.ms.agent_runs(1, "market_scout")[0]
    assert run["status"] == "ok" and run["created_by"] == "admin"
    lock = env.ms._agent_lock("market_scout")
    lock.acquire()
    try:
        r = admin.post("/market/agents/market_scout/run", data={"_csrf": tok}, follow_redirects=True)
        assert "همین حالا در حال اجراست" in page(r)
    finally:
        lock.release()
    text = page(admin.get("/market/sources"))
    assert text.count(">کاشف بازار<") == 5 and "robots.txt" not in text and "کپچا" not in text
    text = page(admin.get(f"/market/sources/{env.source('divar')['id']}"))
    assert "این منبع را کاشف بازار ساخته" in text and "robots.txt رعایت نمی‌شود" in text
    sales = app.test_client()
    stok = login(sales, "sales", "sales-pass-1")
    text = page(sales.get("/sales/leads?kind=repair"))
    assert all(n in text for n in REPAIRS) and "یدکی نمونه الف" not in text
    text = page(sales.get("/sales/leads?city=کرج"))
    assert "لوازم نمونه کرج" in text and "مکانیکی سیار خودروهای ایرانی" in text and "یدکی نمونه اصفهان" not in text
    assert "اصفهان (۲)" in text
    lead = next(r for r in env.ms.sales.leads(kind="repair") if r["city"] == "کرج")
    r = sales.post(f"/sales/leads/{lead['id']}/customer", data={"_csrf": stok})
    assert r.status_code == 302
    cid = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    cust = env.ms.sales.customer(cid)
    assert (cust["kind"], cust["city"], cust["source"]) == ("repair_shop", "کرج", "lead")
    rid = env.ms.build_report("admin")
    assert "کشف بازار: کسب‌وکارهای تازه این هفته" in page(admin.get(f"/market/reports/{rid}"))
    assert "تطبیق تازه تأیید شده" not in page(admin.get(f"/market/reports/{rid}"))
    group = [e for e in env.listings("torob") if json.loads(e["attrs"]).get("group") == TOROB_GROUP]
    main = next(m for m in env.ms.listing(group[0]["id"])["matches"] if m["product_key"] == "FIB201-400")
    at(2026, 11, 5, 11)
    env.ms.decide_match(main["id"], True, "admin")
    text = page(admin.get(f"/market/reports/{rid}"))
    assert "۴ تطبیق تازه تأیید شده" in text and "ساخت گزارش همین حالا" in text
    assert "آخرین گزارش (شماره" in page(admin.get("/market/reports"))
    text = page(admin.get("/market/agents"))
    assert "ترب: صفحه: ۲" in text and "کسب‌وکارها:" in text and "موفق" in text
    assert page(sales.get("/market/upload")).count('name="file"') == 1
    r = sales.post("/market/upload", data={"_csrf": stok, "site": "auto", "url": "",
                                           "file": (io.BytesIO(saved_page("torob_product.html", TOROB_PRODUCT)), "p.html")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "صفحه کالا ترب خوانده شد" in page(r)
    cheaper = fx("basalam_search.html").replace("1٬850٬000", "1٬790٬000").encode("utf-8")
    r = sales.post("/market/upload", data={"_csrf": stok, "site": "basalam", "url": "",
                                           "file": (io.BytesIO(cheaper), "b.html")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "۱ قیمت منتظر تأیید مدیر مالی است" in page(r)
    lst = next(e for e in env.listings("basalam") if e["seller"] == "یدکی نمونه اصفهان")
    assert "صفحه بارگذاری‌شده" in page(admin.get(f"/market/listings/{lst['id']}"))
    r = sales.post("/market/upload", data={"_csrf": stok, "site": "auto", "url": "",
                                           "file": (io.BytesIO(b"plain"), "x.txt")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "صفحه وب نیست" in page(r)


def test_background_run_refused_when_scout_is_off(tmp_path):
    e = Env(tmp_path, enabled="false")
    e.svc.create_user("admin", "admin-pass-1", "admin", "مدیر سامانه", "test")
    app = create_app(e.cfg, e.svc, e.ms, e.ms.sales)
    app.config["TESTING"] = True
    c = app.test_client()
    tok = login(c, "admin", "admin-pass-1")
    r = c.post("/market/agents/market_scout/run", data={"_csrf": tok}, follow_redirects=True)
    assert "در تنظیمات خاموش است" in page(r) and e.ms.agent_runs(5, "market_scout") == []
    assert "کاشف بازار هر روز خودش" not in page(c.get("/market"))


def test_scout_and_browser_settings(tmp_path):
    cfg = config.load_config(write_settings(tmp_path, scout=True))
    assert cfg.collector.respect_robots is False and cfg.collector.max_bytes == 6_000_000
    sc = cfg.scout
    assert sc.enabled and sc.sites == ("torob", "emalls", "digikala", "basalam", "divar")
    assert (sc.interval_hours, sc.queries_per_site, sc.pages_per_query, sc.product_pages, sc.delay_seconds) == (24, 6, 2,
                                                                                                                 15, 5)
    assert sc.divar_region == "iran" and sc.repair_keywords[0] == "برق خودرو" and sc.extra_keywords == ()
    br = cfg.browser
    assert br.enabled and br.channel == "msedge" and br.executable_path == "" and br.max_scrolls == 6
    cfg = config.load_config(write_settings(tmp_path, "[scout]\nsites = Divar، torob divar\nextra_keywords = a، b\n"
                                                      "divar_region = Tehran\n[browser]\nchannel = Chrome\n"
                                                      "executable_path = tools/chrome.exe\n", scout=True))
    assert cfg.scout.sites == ("divar", "torob") and cfg.scout.extra_keywords == ("a", "b")
    assert cfg.scout.divar_region == "tehran" and cfg.browser.channel == "chrome"
    assert cfg.browser.executable_path == str((tmp_path / "tools" / "chrome.exe").resolve())
    for bad in ("[scout]\nsites = torob, amazon\n", "[scout]\ndivar_region = تهران\n", "[browser]\nchannel = firefox\n",
                "[scout]\ndelay_seconds = 0\n", "[browser]\nuser_agent = a\n  b\n"):
        with pytest.raises(config.ConfigError):
            config.load_config(write_settings(tmp_path, bad, scout=True))
    sc = config.load_config(write_settings(tmp_path, scout=True)).scout
    assert sc.osm and sc.osm_days == 7 and sc.osm_urls == ("https://overpass-api.de/api/interpreter",)
    for bad in ("[scout]\nosm_urls = http://overpass-api.de/api/interpreter\n", "[scout]\nosm_days = 0\n",
                "[scout]\nosm_urls = ftp://x.org/a\n"):
        with pytest.raises(config.ConfigError):
            config.load_config(write_settings(tmp_path, bad, scout=True))
    cfg = config.load_config(write_settings(tmp_path, "[collector]\nrespect_robots = true\n"))
    assert cfg.collector.respect_robots and not cfg.scout.enabled and not cfg.browser.enabled
    m = cfg.market
    assert (m.separate_tiers, m.min_prices, float(m.outlier_high), float(m.outlier_low)) == (("genuine",), 3, 2.5, 0.4)
    m = config.load_config(write_settings(tmp_path, "[market]\nseparate_tiers = genuine، OEM genuine\nmin_prices = 5\n")).market
    assert m.separate_tiers == ("genuine", "oem") and m.min_prices == 5
    assert config.load_config(write_settings(tmp_path, "[market]\nseparate_tiers =\n")).market.separate_tiers == ()
    for bad in ("[market]\nseparate_tiers = fake\n", "[market]\nmin_prices = 0\n", "[market]\noutlier_high = 1\n",
                "[market]\noutlier_low = 1\n"):
        with pytest.raises(config.ConfigError):
            config.load_config(write_settings(tmp_path, bad))


def test_upgrade_from_v4_keeps_leads(tmp_path, monkeypatch, capsys):
    path = write_settings(tmp_path)
    cfg = config.load_config(path)
    at(2026, 11, 5)
    con = db.connect(cfg.paths.database)
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    for v in (2, 3, 4):
        for stmt in db.MIGRATIONS[v]:
            con.execute(stmt)
    con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    con.execute("INSERT INTO leads(name, name_norm, sources, n_listings, first_seen_at, last_seen_at) VALUES"
                " ('فروشگاه قدیمی', 'فروشگاه قدیمی', '[\"ترب\"]', 2, '2026-10-01T10:00:00+00:00',"
                " '2026-10-01T10:00:00+00:00')")
    for key in ("scout-osm", "scout-torob"):
        con.execute("INSERT INTO sources(key, name, kind, unit, price_level, created_at, created_by, updated_at,"
                    " last_success_at) VALUES (?, ?, 'aggregator', 'toman', 'unknown', '2026-11-01T10:00:00+00:00',"
                    " 'system', '2026-11-01T10:00:00+00:00', '2026-11-04T10:00:00+00:00')", (key, key))
    con.close()
    monkeypatch.setenv("BORNA_SETTINGS", str(path))
    assert cli.main(["init", "--password", "admin-pass-1"]) == 0
    assert "پایگاه‌داده به نسخه 9 به‌روز شد" in capsys.readouterr().out
    assert len(list((cfg.paths.database.parent / "backups").glob("pre_v9_*.sqlite3"))) == 1
    svc = Service(cfg)
    assert svc.ensure_schema() == []
    with svc.db() as c:
        assert dict(c.execute("SELECT key, last_success_at FROM sources").fetchall()) == {
            "scout-osm": None, "scout-torob": "2026-11-04T10:00:00+00:00"}
        row = dict(c.execute("SELECT * FROM leads").fetchone())
        assert (row["name"], row["kind"], row["city"], row["price_min_rial"], row["n_listings"], row["address"],
                row["phone"]) == ("فروشگاه قدیمی", "seller", None, None, 2, None, None)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE leads SET kind='robot'")


def osm_pages(e, base=None, shops=None, places=None):
    base = base or e.cfg.scout.osm_urls[0]
    e.web.pages[sites.OSM.request_url(base, sites.OSM.shops_query())] = shops or (200, "application/json",
                                                                                 fx("osm_shops.json"))
    e.web.pages[sites.OSM.request_url(base, sites.OSM.places_query())] = places or (200, "application/json",
                                                                                   fx("osm_places.json"))


def test_map_reader_keeps_named_shops_with_their_city():
    r = sites.OSM.parse_map(fx("osm_shops.json"), fx("osm_places.json"))
    assert (r.cards, r.skipped) == (11, 5) and "-" not in [b.name for b in r.businesses]
    got = [(b.name, b.kind, b.city, b.address, b.url.rsplit("/", 2)[-2] + "/" + b.url.rsplit("/", 1)[-1])
           for b in r.businesses]
    assert got == [("برق خودرو نمونه یک", "repair", "تهران", "خیابان نمونه ۱۲", "node/1001"),
                   ("تعمیرگاه نمونه دو", "repair", "کرج", None, "way/2002"),
                   ("پخش لوازم یدکی نمونه", "seller", "شیراز", "شیراز، بلوار نمونه، پلاک ۵", "node/1003"),
                   ("لوازم یدکی نمونه سه", "seller", "تهران", None, "node/1004"),
                   ("مکانیکی جاده نمونه", "repair", None, None, "node/1008"),
                   ("برق خودرو نمونه یک", "repair", "تبریز", None, "node/1010")]
    assert r.businesses[0].key == "برق خودرو نمونه یک @ تهران" and r.businesses[4].key.endswith("@ osm:node/1008")
    assert [b.phone for b in r.businesses] == ["02166123456", None, "07136261234، 09171112233", None, None, None]
    alone = sites.OSM.parse_map(fx("osm_shops.json"), None)
    assert [b.city for b in alone.businesses] == [None, "Karaj", None, None, None, None, "تبریز"]
    with pytest.raises(ValueError, match="جواب درستی نداد"):
        sites.overpass_elements('{"elements": [], "remark": "runtime error: Query timed out"}')
    with pytest.raises(ValueError, match="JSON"):
        sites.overpass_elements("<html>busy</html>")
    url = sites.OSM.request_url("https://overpass-api.de/api/interpreter", sites.OSM.shops_query())
    assert url.startswith("https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D") and " " not in url


def test_scout_reads_the_map_once_a_week(tmp_path):
    e = Env(tmp_path, osm="true", sites="divar")
    osm_pages(e)
    res = e.run()
    st = res["stats"]
    osm = st["sites"]["osm"]
    assert res["status"] == "ok" and osm["healthy"] and not osm["blocked"]
    assert (osm["pages"], osm["repairs"], osm["parts"], osm["new_businesses"]) == (2, 4, 2, 6)
    leads = {(r["name"], r["city"]): r for r in e.ms.sales.leads()}
    supplier = leads[("پخش لوازم یدکی نمونه", "شیراز")]
    assert supplier["kind"] == "supplier" and supplier["address"] == "شیراز، بلوار نمونه، پلاک ۵"
    assert supplier["example_url"] == "https://www.openstreetmap.org/node/1003"
    assert supplier["sources_l"] == ["اوپن‌استریت‌مپ"]
    assert supplier["phone"] == "07136261234، 09171112233" and supplier["phones_l"] == ["07136261234", "09171112233"]
    tehran = leads[("برق خودرو نمونه یک", "تهران")]
    assert tehran["phones_l"] == ["02166123456"]
    e.ms.sales.set_lead_status(tehran["id"], "contacted", "sales", "", "۰۹۱۲ ۰۰۰ ۱۱۱۱")
    assert leads[("لوازم یدکی نمونه سه", "تهران")]["kind"] == "seller"
    assert leads[("تعمیرگاه نمونه دو", "کرج")]["kind"] == "repair"
    assert {("برق خودرو نمونه یک", "تهران"), ("برق خودرو نمونه یک", "تبریز"), ("مکانیکی جاده نمونه", None)} <= set(leads)
    src = e.source("osm")
    assert (src["created_by"], src["policy_status"], src["failure_count"]) == ("system", "approved", 0)
    with e.ms.db() as c:
        snap = dict(c.execute("SELECT * FROM snapshots WHERE source_id=? ORDER BY id DESC LIMIT 1",
                              (src["id"],)).fetchone())
    assert snap["content_type"] == "application/json" and "۴ تعمیرگاه و ۲ فروشگاه لوازم یدکی" in snap["message"]
    assert snap["path"].endswith(".json.gz")
    calls = len([u for u in e.web.calls if "overpass" in u])
    assert calls == 2
    at(2026, 11, 8)
    res = e.run()
    assert "نوبت بعد" in res["stats"]["sites"]["osm"]["skipped"]
    assert len([u for u in e.web.calls if "overpass" in u]) == calls
    at(2026, 11, 13)
    res = e.run()
    osm = res["stats"]["sites"]["osm"]
    assert osm["healthy"] and osm["new_businesses"] == 0 and len([u for u in e.web.calls if "overpass" in u]) == 4
    assert len(e.ms.sales.leads(kind="repair")) == 4 + 3
    again = {(r["name"], r["city"]): r for r in e.ms.sales.leads()}
    assert again[("برق خودرو نمونه یک", "تهران")]["phone"] == "09120001111"
    assert again[("پخش لوازم یدکی نمونه", "شیراز")]["phone"] == "07136261234، 09171112233"


def test_map_service_falls_back_to_the_next_address(tmp_path):
    e = Env(tmp_path, osm="true", sites="divar",
            osm_urls="https://overpass-api.de/api/interpreter, https://overpass.example.org/api/interpreter")
    first, second = e.cfg.scout.osm_urls
    osm_pages(e, first, shops=(504, "text/html", "busy"),
              places=(200, "application/json", '{"elements": [], "remark": "runtime error: Query timed out"}'))
    osm_pages(e, second)
    res = e.run()
    osm = res["stats"]["sites"]["osm"]
    assert osm["healthy"] and (osm["repairs"], osm["parts"]) == (4, 2) and res["status"] == "ok"
    f = Env(tmp_path / "b", osm="true", sites="divar")
    osm_pages(f, places=(504, "text/html", "busy"))
    res = f.run()
    osm = res["stats"]["sites"]["osm"]
    assert osm["healthy"] and res["status"] == "partial"
    assert any("فهرست شهرها خوانده نشد" in x for x in res["stats"]["errors"])
    assert {r["city"] for r in f.ms.sales.leads() if "اوپن‌استریت‌مپ" in r["sources_l"]} == {None, "Karaj", "تبریز"}
    g = Env(tmp_path / "c", osm="true", sites="divar")
    res = g.run()
    osm = res["stats"]["sites"]["osm"]
    assert not osm["healthy"] and res["status"] == "partial"
    assert any(x.startswith("اوپن‌استریت‌مپ") and "404" in x for x in res["stats"]["errors"])
    assert g.source("osm")["failure_count"] == 1
    h = Env(tmp_path / "d", sites="divar")
    h.run()
    assert not [u for u in h.web.calls if "overpass" in u]
    k = Env(tmp_path / "e", osm="true", sites="")
    osm_pages(k)
    assert k.run()["stats"]["sites"]["osm"]["healthy"]


def test_map_shops_on_the_web_pages_and_in_the_report(tmp_path):
    e = Env(tmp_path, osm="true", sites="divar")
    osm_pages(e)
    e.run()
    e.svc.create_user("admin", "admin-pass-1", "admin", "مدیر سامانه", "test")
    app = create_app(e.cfg, e.svc, e.ms, e.ms.sales)
    app.config["TESTING"] = True
    c = app.test_client()
    tok = login(c, "admin", "admin-pass-1")
    text = page(c.get("/sales/leads?city=شیراز"))
    assert "پخش لوازم یدکی نمونه" in text and "شیراز، بلوار نمونه، پلاک ۵" in text and "روی نقشه" in text
    assert "openstreetmap.org/node/1003" in text
    assert 'href="tel:07136261234"' in text and "۰۹۱۷۱۱۱۲۲۳۳" in text
    text = page(c.get("/market/sources"))
    assert "اوپن‌استریت‌مپ" in text and ">نقشه<" in text
    text = page(c.get(f"/market/sources/{e.source('osm')['id']}"))
    assert "نقشه آزاد اوپن‌استریت‌مپ" in text and "osm = false" in text
    text = page(c.get("/market/agents"))
    assert "اوپن‌استریت‌مپ: صفحه: ۲، تعمیرگاه: ۴، فروشگاه لوازم یدکی: ۲، کسب‌وکار تازه: ۶ — سالم" in text
    lead = next(r for r in e.ms.sales.leads() if r["name"] == "پخش لوازم یدکی نمونه")
    r = c.post(f"/sales/leads/{lead['id']}/customer", data={"_csrf": tok})
    cust = e.ms.sales.customer(int(r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]))
    assert cust["kind"] == "wholesaler" and cust["city"] == "شیراز" and "بلوار نمونه" in cust["note"]
    assert cust["phone"] == "07136261234" and "تلفن‌ها: 07136261234، 09171112233" in cust["note"]
    rep = e.ms.report(e.ms.build_report("admin"))
    assert "روی نقشه" in page(c.get(f"/market/reports/{rep['id']}"))
    sheet = load_workbook(io.BytesIO(market_report.xlsx(rep, "برنا سنسور")))["کسب‌وکارهای تازه"]
    head = [x.value for x in sheet[1]]
    assert head == ["نام", "نوع", "شهر", "نشانی", "تلفن", "منبع", "لینک"]
    rows = {r[0]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    assert rows["پخش لوازم یدکی نمونه"][3:5] == ("شیراز، بلوار نمونه، پلاک ۵", "07136261234، 09171112233")
    assert "۰۷۱۳۶۲۶۱۲۳۴" in page(c.get(f"/market/reports/{rep['id']}"))


def test_command_line_has_scout():
    args = cli.build_parser().parse_args(["scout", "--user", "operator"])
    assert args.func is cli.cmd_scout and args.user == "operator"
    assert cli.build_parser().parse_args(["scout"]).user == "cli"


class Req:
    def __init__(self, url, rtype="document", nav=True, main=True):
        self.url = url
        self.resource_type = rtype
        self._nav = nav
        self.frame = self
        self.page = self
        self.main_frame = self if main else None

    def is_navigation_request(self):
        return self._nav


class Route:
    def __init__(self, req):
        self.request = req
        self.done = None

    def abort(self):
        self.done = "abort"

    def continue_(self):
        self.done = "continue"

    def fulfill(self, status, content_type, body):
        self.done = ("fulfill", status, body)


def browser_cfg(**over):
    base = {"enabled": True, "channel": "chromium", "executable_path": "", "user_agent": "", "timeout_seconds": 30,
            "max_scrolls": 2, "scroll_pause_ms": 100, "max_mb": 12}
    return BrowserCfg(**{**base, **over})


def test_browser_route_guard():
    ips = {"www.digikala.com": ["93.184.216.34"], "cdn.example.com": ["93.184.216.35"], "intranet.local": ["10.0.0.5"]}
    b = Browser(browser_cfg(), resolver=lambda h, p: ips.get(h, []))
    b._domain = "digikala.com"

    def go(*a, **kw):
        r = Route(Req(*a, **kw))
        b._route(r)
        return r.done
    assert go("https://www.digikala.com/search/?q=x") == "continue"
    assert go("https://cdn.example.com/app.js", "script", False, False) == "continue"
    assert go("https://cdn.example.com/page", "document") == "abort"
    assert go("https://www.digikala.com/a.png", "image", False, False) == "abort"
    assert go("https://intranet.local/x.js", "script", False, False) == "abort"
    assert go("https://unknown.example.org/x.js", "script", False, False) == "abort"
    assert go("https://www.digikala.com:8443/x", "fetch", False, False) == "abort"
    assert go("https://user:pw@www.digikala.com/x", "fetch", False, False) == "abort"
    assert go("ftp://www.digikala.com/x", "fetch", False, False) == "abort"
    assert go("data:text/plain,hi", "fetch", False, False) == "continue"
    served = Browser(browser_cfg(), transport=lambda url: (200, "text/html", "<p>ok</p>") if "ok" in url else None)
    served._domain = "digikala.com"
    r = Route(Req("https://www.digikala.com/ok"))
    served._route(r)
    assert r.done == ("fulfill", 200, b"<p>ok</p>")
    r = Route(Req("https://www.digikala.com/missing"))
    served._route(r)
    assert r.done == "abort"


def test_browser_refuses_when_switched_off_or_closed():
    with pytest.raises(BrowserUnavailable, match="خاموش"):
        Browser(browser_cfg(enabled=False)).__enter__()
    with pytest.raises(BrowserUnavailable, match="باز نیست"):
        Browser(browser_cfg()).render("https://www.digikala.com/", "digikala.com")
    b = Browser(browser_cfg(executable_path="/nowhere/chrome"))
    assert b._launch_options() == [{"executable_path": "/nowhere/chrome"}]
    assert Browser(browser_cfg(channel="msedge"))._launch_options() == [{"channel": "msedge"}, {"channel": "chrome"}, {}]


DK_SHELL = """<!doctype html><html lang="fa"><head><meta charset="utf-8"><title>جست‌وجو | دیجی‌کالا</title>
<script src="/app.js"></script></head><body><img src="/logo.png"><div id="list" style="min-height:3000px"></div></body></html>"""
DK_JS = """
var n = 0;
function add() {
  n += 1;
  var a = document.createElement('a');
  a.href = '/product/dkp-' + (70000000 + n) + '/x/?variant_id=' + n;
  a.innerHTML = '<div data-testid="product-card"><h3>سنسور اکسیژن ' + n + '</h3></div>';
  document.getElementById('list').appendChild(a);
}
document.addEventListener('DOMContentLoaded', function () {
  setTimeout(function () { add(); add(); }, 50);
  window.addEventListener('wheel', function () { add(); });
});
"""
BSL_SHELL = """<!doctype html><html lang="fa"><head><meta charset="utf-8"><title>باسلام</title>
<script src="/list.js"></script></head><body><div id="v" style="min-height:3000px"></div></body></html>"""
BSL_JS = """
var start = 0;
function show() {
  var html = '';
  for (var i = start; i < start + 2; i++) {
    html += '<a data-component="search-items" href="/booth-' + i + '/product/' + (900 + i) + '"><h2>سنسور اکسیژن '
      + i + '</h2><div><span class="bi-vendor"></span><span>شهر' + i + '<span>|</span><div title="غرفه ' + i
      + '">غرفه ' + i + '</div></span></div><span>1٬000٬000<span class="bi-toman"></span></span></a>';
  }
  document.getElementById('v').innerHTML = html;
}
document.addEventListener('DOMContentLoaded', function () {
  show();
  window.addEventListener('wheel', function () { start += 2; show(); });
});
"""


@pytest.mark.skipif(not os.environ.get("BORNA_BROWSER_PATH"),
                    reason="آزمون مرورگر واقعی فقط وقتی BORNA_BROWSER_PATH داده شود اجرا می‌شود.")
def test_real_browser_renders_scrolls_and_collects_virtual_lists():
    pages = {"https://www.digikala.com/search/?q=x": (200, "text/html; charset=utf-8", DK_SHELL),
             "https://www.digikala.com/app.js": (200, "application/javascript", DK_JS),
             "https://basalam.com/s?q=x": (200, "text/html; charset=utf-8", BSL_SHELL),
             "https://basalam.com/list.js": (200, "application/javascript", BSL_JS)}
    seen = []

    def transport(url):
        seen.append(url)
        return pages.get(url) or ((404, "text/plain", "not found") if url.endswith("/missing") else None)
    cfg = browser_cfg(executable_path=os.environ["BORNA_BROWSER_PATH"], max_scrolls=3, scroll_pause_ms=300)
    with Browser(cfg, min_delay=0, transport=transport) as b:
        got = b.render("https://www.digikala.com/search/?q=x", "digikala.com", ready=S["digikala"].ready)
        parsed = S["digikala"].parse_search(got.text(), got.url)
        assert parsed.cards == 5 and len(parsed.product_urls) == 5
        got = b.render("https://basalam.com/s?q=x", "basalam.com", ready=S["basalam"].ready, collect=S["basalam"].collect)
        parsed = S["basalam"].parse_search(got.text(), got.url)
        assert [i.ext_id for i in parsed.items] == [f"bsl-{900 + i}" for i in range(8)]
        assert parsed.items[3].seller == "غرفه 3" and parsed.items[3].city == "شهر3"
        assert "<title>باسلام</title>" in got.text()
        with pytest.raises(FetchBlocked, match="بیرون از دامنه"):
            b.render("https://evil.example.com/", "basalam.com")
        with pytest.raises(FetchBlocked, match="404"):
            b.render("https://basalam.com/missing", "basalam.com")
        assert b.pages == 2
    assert not [u for u in seen if u.endswith(".png")]


def test_site_check_reads_one_page_per_site_and_saves_nothing(env):
    from borna_pricing.market import sitecheck

    def counts():
        with env.ms.db() as c:
            return tuple(c.execute("SELECT (SELECT COUNT(*) FROM external_products), (SELECT COUNT(*) FROM observations),"
                                   " (SELECT COUNT(*) FROM snapshots), (SELECT COUNT(*) FROM leads)").fetchone())

    before = counts()
    env.bpages[prefix(S["basalam"])] = 403
    rep = sitecheck.run(env.ms, ["torob", "emalls", "digikala", "basalam", "divar"])
    res = {r["key"]: r for r in rep["results"]}
    assert list(res) == ["rate", "torob", "emalls", "digikala", "basalam", "divar"]
    assert res["rate"]["status"] == "ok" and res["rate"]["sample"][0]["price"].endswith("تومان")
    assert res["torob"]["status"] == "ok" and res["torob"]["items"] > 0 and res["torob"]["priced"] > 0
    assert not res["torob"]["browser"] and res["torob"]["http"] == 200 and res["torob"]["sample"]
    assert res["digikala"]["status"] == "ok" and res["digikala"]["browser"] and res["digikala"]["products"] > 0
    assert "صفحه خود هر کالا" in res["digikala"]["message"]
    assert res["basalam"]["status"] == "blocked" and "403" in res["basalam"]["message"]
    assert res["divar"]["status"] == "ok" and res["divar"]["browser"]
    assert rep["ok"] == 5 and rep["total"] == 6 and counts() == before
    assert sitecheck.last(env.ms)["results"][1]["key"] == "torob"
    text = "\n".join(sitecheck.lines(rep))
    assert "ترب: سالم" in text and "باسلام: مسدود شد" in text and "با مرورگر نامرئی" in text


def test_site_check_page_and_command(env, monkeypatch, capsys, tmp_path):
    from borna_pricing.market import sitecheck
    app = create_app(env.cfg, env.svc, env.ms, env.ms.sales)
    app.config["TESTING"] = True
    env.svc.create_user("admin", "admin-pass-1", "admin", "مدیر", "test")
    env.svc.create_user("finance", "finance-pass", "approver", "مالی", "test")
    c = app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        tok = s["_csrf"]
    c.post("/login", data={"username": "finance", "password": "finance-pass", "_csrf": tok})
    assert c.get("/market/site-check").status_code == 403
    a = app.test_client()
    a.get("/login")
    with a.session_transaction() as s:
        tok = s["_csrf"]
    a.post("/login", data={"username": "admin", "password": "admin-pass-1", "_csrf": tok})
    with a.session_transaction() as s:
        tok = s["_csrf"]
    assert "هنوز بررسی‌ای انجام نشده" in a.get("/market/site-check").get_data(as_text=True)
    started = []
    monkeypatch.setattr(sitecheck, "start", lambda ms, user: started.append(user) or True)
    r = a.post("/market/site-check", data={"_csrf": tok}, follow_redirects=True)
    assert started == ["admin"] and "در پس‌زمینه شروع شد" in r.get_data(as_text=True)
    sitecheck.run(env.ms, ["torob"])
    page = a.get("/market/site-check").get_data(as_text=True)
    assert "آخرین بررسی" in page and "ترب" in page and "سالم" in page and "درخواست ساده" in page
    report = sitecheck.last(env.ms)
    monkeypatch.setattr(sitecheck, "run", lambda ms, keys, query: {**report, "query": query})
    monkeypatch.setenv("BORNA_SETTINGS", str(tmp_path / "settings.ini"))
    assert cli.main(["check-sites", "--site", "torob", "--query", "لامبدا"]) == 0
    out = capsys.readouterr().out
    assert "جست‌وجوی آزمون: «لامبدا»" in out and "ترب: سالم" in out
    saved_files = list((tmp_path / "data").glob("site_check_*.txt"))
    assert len(saved_files) == 1 and "ترب: سالم" in saved_files[0].read_text(encoding="utf-8")
