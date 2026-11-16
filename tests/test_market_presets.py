import time
from datetime import datetime

import pytest

from borna_pricing import clock, config
from borna_pricing.clock import TEHRAN
from borna_pricing.market import extract, presets
from borna_pricing.market.service import NO_ITEMS, MarketService, _drop_note
from borna_pricing.service import Service, ServiceError

from conftest import FIXTURES, FakeWeb, fake_fetcher_factory, write_settings

MARKET = FIXTURES / "market"
PS_URL = "https://shop.example.ir/fa/12-o2-sensor"
PS_JSON = (MARKET / "synthetic_json_wrapped.json").read_text(encoding="utf-8")
PS_HTML = (MARKET / "synthetic_prestashop.html").read_text(encoding="utf-8")
WOO_URL = "https://parts.example.ir/product-category/o2-sensor/"
WOO_HTML = (MARKET / "synthetic_woocommerce.html").read_text(encoding="utf-8")
LIST_URL = "https://shop.example.ir/cat/o2"
ITEMLIST = (MARKET / "synthetic_itemlist.html").read_text(encoding="utf-8")
PRODUCT = (MARKET / "synthetic_product.html").read_text(encoding="utf-8")
OG = (MARKET / "synthetic_og.html").read_text(encoding="utf-8")
CARDS = (MARKET / "synthetic_cards.html").read_text(encoding="utf-8")
PRESTASHOP_REGEX = r'data-id-product="(?P<id>\d+)"(?:(?!</article>).)*?<h3[^>]*>\s*<a\s+href="(?P<url>[^"]+)"[^>]*>(?P<title>[^<]+)</a>(?:(?!</article>).)*?class="price[^"]*"[^>]*>(?P<price>[^<]+)<'
PS_ITEMS = [
    ("1201", "https://shop.example.ir/fa/o2/1201-siemens-gray-50.html", "سنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت",
     "2,450,000 تومان"),
    ("1202", "https://shop.example.ir/fa/o2/1202-sagem-green-35.html", "سنسور اکسیژن ساژم سوکت سبز سیم سبز 35 سانت",
     "2,380,000 تومان"),
    ("1203", "https://shop.example.ir/fa/o2/1203-me17-black.html", "سنسور اکسیژن ME17 بوش سوکت مشکی", "0 تومان"),
]
WOO_ITEMS = [
    ("401", "https://parts.example.ir/product/o2-siemens-gray-50/", "سنسور اکسیژن زیمنس سوکت طوسی سیم 50 سانت",
     "2,450,000 تومان"),
    ("402", "https://parts.example.ir/product/o2-sagem-green-35/", "سنسور اکسیژن ساژم سوکت سبز سیم 35 سانت",
     "2,650,000 تومان"),
    ("403", "https://parts.example.ir/product/o2-me17-black/", "سنسور اکسیژن ME17 بوش سوکت مشکی", "3,100,000 تومان"),
]
PS_WARNING = "داده ساخت‌یافته پیدا نشد؛ قالب «پرستاشاپ» به‌کار رفت."
WOO_WARNING = "داده ساخت‌یافته پیدا نشد؛ قالب «ووکامرس» به‌کار رفت."
SOURCE = {"key": "shopx", "name": "فروشگاه نمونه", "kind": "shop", "domain": "shop.example.ir",
          "start_urls": LIST_URL, "unit": "toman", "price_level": "retail", "parser": "auto", "interval_hours": "24",
          "delay_seconds": "3", "max_pages": "5", "active": "1"}


def rows(items):
    return [(i.ext_id, i.url, i.title, i.price) for i in items]


@pytest.fixture
def env(tmp_path):
    made = []

    def make(extra=""):
        root = tmp_path / f"env{len(made)}"
        root.mkdir()
        cfg = config.load_config(write_settings(root, extra))
        svc = Service(cfg)
        clock.set_fixed_now(datetime(2026, 10, 31, 13, 30, tzinfo=TEHRAN))
        svc.init()
        web = FakeWeb({LIST_URL: (200, "text/html; charset=utf-8", ITEMLIST)})
        ms = MarketService(svc, fake_fetcher_factory(web))
        made.append(ms)
        return svc, ms, web
    return make


def approved(ms, **over):
    sid = ms.save_source({**SOURCE, **over}, "admin")
    ms.set_policy(sid, "approved", "robots.txt و شرایط استفاده بررسی شد", "admin")
    return sid


def serve(web, body, url=LIST_URL):
    web.pages[url] = (200, "text/html; charset=utf-8", body)


def test_preset_registry():
    assert list(presets.PRESETS) == ["prestashop", "woocommerce"]
    assert {k: (p.label, p.verified) for k, p in presets.PRESETS.items()} == {
        "prestashop": ("پرستاشاپ", True), "woocommerce": ("ووکامرس", False)}
    assert presets.PRESETS["prestashop"].regex == PRESTASHOP_REGEX
    woo = presets.PRESETS["woocommerce"].regex
    assert "(?:(?!</li>).)*?" in woo and "</article>" not in woo
    for p in presets.PRESETS.values():
        assert extract.validate_regex(p.regex).groupindex.keys() >= {"id", "url", "title", "price"}


def test_prestashop_preset_reads_json_and_plain_html():
    rx = presets.PRESETS["prestashop"].regex
    assert PS_JSON.lstrip().startswith("{") and PS_HTML.lstrip().startswith("<!doctype")
    assert rows(extract.from_regex(extract.unwrap_json_html(PS_JSON), PS_URL, rx)) == PS_ITEMS
    assert rows(extract.from_regex(PS_HTML, PS_URL, rx)) == PS_ITEMS
    assert extract.from_regex(WOO_HTML, WOO_URL, rx) == []


def test_woocommerce_preset_takes_the_sale_price():
    rx = presets.PRESETS["woocommerce"].regex
    items = extract.from_regex(WOO_HTML, WOO_URL, rx)
    assert rows(items) == WOO_ITEMS
    assert WOO_HTML.count("woocommerce-Price-amount") == 6 and "2,900,000" not in items[1].price
    assert extract.from_regex(PS_HTML, PS_URL, rx) == []


def test_auto_falls_back_to_presets_and_warns():
    items, warnings = extract.extract(PS_HTML, PS_URL)
    assert rows(items) == PS_ITEMS and warnings == [PS_WARNING]
    items, warnings = extract.extract(PS_JSON, PS_URL + "?from-xhr")
    assert rows(items) == PS_ITEMS and warnings == [PS_WARNING]
    items, warnings = extract.extract(WOO_HTML, WOO_URL)
    assert rows(items) == WOO_ITEMS and warnings == [WOO_WARNING]


def test_auto_finds_nothing_on_unknown_markup():
    assert extract.extract(CARDS, "https://shop.example.ir/list") == ([], [])


def test_structured_data_wins_over_presets():
    items, warnings = extract.extract(ITEMLIST, LIST_URL)
    assert len(items) == 4 and warnings == ["یک بلوک JSON-LD خراب بود و نادیده گرفته شد."]
    cards = PS_HTML[PS_HTML.index("<section id=\"products\">"):PS_HTML.index("<footer")]
    mixed = ITEMLIST.replace("</body>", cards + "</body>")
    assert mixed != ITEMLIST and extract.extract(mixed, LIST_URL) == (items, warnings)
    one, w1 = extract.extract(PRODUCT, "https://s.example.ir/p/1")
    assert len(one) == 1 and not any("قالب" in w for w in w1)
    og, w2 = extract.extract(OG.replace("</body>", WOO_HTML[WOO_HTML.index("<ul class=\"products"):]),
                             "https://shop.example.ir/item/77")
    assert len(og) == 1 and og[0].price == "2,850,000" and w2 == []


def test_presets_stay_fast_on_large_junk_html():
    block = (
        '<li class="menu-item menu-item-{i}"><a href="https://shop.example.ir/c/{i}">دسته {i}</a></li>'
        '<li class="product type-product post-{i} status-publish instock"><a href="https://shop.example.ir/p/{i}"'
        ' class="woocommerce-LoopProduct-link"><h2 class="woocommerce-loop-product__title">کالای {i}</h2></a>'
        '<span class="woocommerce-Price-amount amount"><bdi>{i},000</bdi></span></li>'
        '<article class="product-miniature" data-id-product="{i}"><div class="thumbnail-container">'
        '<a href="https://shop.example.ir/p/{i}">کالا</a></div><h3 class="product-title">بدون پیوند</h3>'
        '<span class="price">۱۲۳</span></article>'
        '<div class="card"><span class="price">تماس بگیرید</span><p>متن نمونه سنسور اکسیژن خودرو {i}</p></div>\n'
    )
    parts, size, i = ["<html><body>"], 0, 0
    while size < 2_000_000:
        parts.append(block.format(i=i))
        size += len(parts[-1])
        i += 1
    junk = "".join(parts) + "</body></html>"
    assert len(junk) >= 2_000_000
    start = time.perf_counter()
    assert extract.extract(junk, "https://shop.example.ir/c") == ([], [])
    for p in presets.PRESETS.values():
        assert extract.from_regex(junk, "https://shop.example.ir/c", p.regex) == []
    assert time.perf_counter() - start < 5


def test_save_with_preset_sets_parser_and_regex(env):
    _, ms, _ = env()
    sid = ms.save_source({**SOURCE, "item_regex": "", "preset": "prestashop"}, "admin")
    src = ms.get_source(sid)
    assert (src["parser"], src["item_regex"]) == ("regex", PRESTASHOP_REGEX)
    ms.save_source({**SOURCE, "parser": "auto", "item_regex": "x", "preset": "woocommerce"}, "admin", sid)
    src = ms.get_source(sid)
    assert (src["parser"], src["item_regex"]) == ("regex", presets.PRESETS["woocommerce"].regex)
    ms.save_source({**SOURCE, "item_regex": "", "preset": ""}, "admin", sid)
    assert (ms.get_source(sid)["parser"], ms.get_source(sid)["item_regex"]) == ("auto", None)
    with pytest.raises(ServiceError, match="قالب آماده"):
        ms.save_source({**SOURCE, "preset": "magento"}, "admin", sid)
    with pytest.raises(ServiceError, match="قالب آماده"):
        ms.save_source({**SOURCE, "key": "other", "preset": "nope"}, "admin")
    assert [s["key"] for s in ms.sources(include_manual=False)] == ["shopx"]
    assert ms.get_source(sid)["parser"] == "auto"


def test_snapshot_and_parser_test_report_the_preset(env):
    _, ms, web = env()
    serve(web, PS_HTML, PS_URL)
    sid = approved(ms, start_urls=PS_URL)
    st = ms.collect_source(sid)
    assert (st["pages"], st["items"], st["healthy"], st["errors"]) == (1, 3, True, [])
    snap = ms.snapshots(sid)[0]
    assert snap["n_items"] == 3 and snap["message"] == PS_WARNING and snap["parser_version"] == "1.2"
    out = ms.test_parser(sid)
    assert out["count"] == 3 and out["warnings"] == [PS_WARNING]
    assert ms.test_parser(sid, {"preset": "prestashop"})["count"] == 3
    assert ms.test_parser(sid, {"preset": "woocommerce"})["count"] == 0
    with pytest.raises(ServiceError, match="قالب آماده"):
        ms.test_parser(sid, {"preset": "nope"})


def test_zero_items_is_a_failure_and_a_good_run_resets(env):
    _, ms, web = env()
    serve(web, CARDS)
    sid = approved(ms)
    st = ms.collect_source(sid)
    assert (st["pages"], st["items"], st["healthy"]) == (1, 0, False) and st["errors"] == [NO_ITEMS]
    src = ms.get_source(sid)
    assert src["failure_count"] == 1 and src["last_error"] == NO_ITEMS and src["last_success_at"] is None
    res = ms.run_agent("price_watcher", "manual", "finance")
    assert res["status"] == "failed" and res["message"] == "از هیچ منبعی کالایی خوانده نشد؛ خطاها را ببینید."
    assert res["stats"]["errors"] == [f"فروشگاه نمونه: {NO_ITEMS}"]
    assert ms.get_source(sid)["failure_count"] == 2
    serve(web, ITEMLIST)
    res = ms.run_agent("price_watcher", "manual", "finance")
    assert res["status"] == "ok"
    src = ms.get_source(sid)
    assert src["failure_count"] == 0 and src["last_error"] is None and src["last_success_at"]


def test_sudden_drop_warns_without_counting_as_failure(env):
    _, ms, web = env()
    sid = approved(ms)
    assert ms.collect_source(sid)["items"] == 4
    serve(web, PRODUCT)
    st = ms.collect_source(sid)
    drop = "افت ناگهانی تعداد کالا (از ۴ به ۱)؛ ممکن است ساختار سایت عوض شده باشد."
    assert st["healthy"] and st["items"] == 1 and st["errors"] == [f"{LIST_URL}: {drop}"]
    src = ms.get_source(sid)
    assert src["failure_count"] == 0 and drop in src["last_error"] and src["last_success_at"]
    assert ms.snapshots(sid)[0]["message"] == drop
    assert "alert" not in st
    res = ms.run_agent("price_watcher", "manual", "finance")
    assert res["status"] == "ok" and ms.get_source(sid)["last_error"] is None
    assert _drop_note(4, 2) is None and _drop_note(3, 0) is None and _drop_note(None, 0) is None
    assert _drop_note(10, 5) is None and "از ۱۰ به ۴" in _drop_note(10, 4)


def test_source_health_breaks_and_recovers_without_messages(env):
    _, ms, web = env("[bale]\nenabled = true\napi_base = https://bot.example.ir\ntoken = T\nchat_id = 42\n")
    sid = approved(ms)
    assert ms.collect_source(sid)["healthy"]
    serve(web, CARDS)
    st = ms.collect_source(sid)
    assert not st["healthy"] and "alert" not in st and st["errors"][-1] == NO_ITEMS
    for _ in range(2):
        ms.collect_source(sid)
    assert ms.get_source(sid)["failure_count"] == 3
    serve(web, ITEMLIST)
    assert ms.collect_source(sid)["healthy"] and ms.get_source(sid)["failure_count"] == 0
