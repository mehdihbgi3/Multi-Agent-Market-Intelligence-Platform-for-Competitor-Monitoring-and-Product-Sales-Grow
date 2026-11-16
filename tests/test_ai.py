import json
import urllib.error
from datetime import datetime

import pytest

from borna_pricing import clock, config
from borna_pricing.ai import content, price_list, reasons, reply
from borna_pricing.ai.gateway import ModelError, ModelGateway, parse_json, redact
from borna_pricing.clock import TEHRAN
from borna_pricing.market.service import MarketService
from borna_pricing.sales.service import SalesService
from borna_pricing.service import Service

from conftest import FakeWeb, fake_fetcher_factory, write_settings

LLM_ON = "[llm]\nenabled = true\nbase_url = http://127.0.0.1:11434/v1\nmodel = qwen-test\napi_key = KEY\n"
PRICE_TEXT = """لیست قیمت امروز (به هزار تومان)
سنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت ...... 2450
سنسور اکسیژن ساژم سوکت سبز سیم سبز 35 سانت: ۲٬۳۸۰
سنسور اکسیژن ME17 بوش سوکت مشکی — ناموجود
بکس سنسور اکسیژن 450
"""


class FakeModel:
    def __init__(self, *contents, fail_first_400=False):
        self.contents = list(contents)
        self.calls = []
        self.fail_first_400 = fail_first_400

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload))
        if self.fail_first_400 and len(self.calls) == 1:
            raise urllib.error.HTTPError(url, 400, "bad", {}, None)
        c = self.contents.pop(0) if self.contents else "{}"
        return 200, json.dumps({"choices": [{"message": {"content": c}}]})


@pytest.fixture
def env(tmp_path):
    made = []

    def make(extra=""):
        d = tmp_path / f"env{len(made)}"
        d.mkdir()
        made.append(d)
        cfg = config.load_config(write_settings(d, extra))
        svc = Service(cfg)
        clock.set_fixed_now(datetime(2026, 10, 31, 13, 30, tzinfo=TEHRAN))
        svc.init()
        ms = MarketService(svc, fake_fetcher_factory(FakeWeb()))
        ms.sales = SalesService(svc, ms)
        return svc, ms
    return make


def test_redaction_and_json_parsing():
    assert redact("شماره 09121234567 یا +98 912 123 4567 یا a.b@x.ir") == "شماره [تلفن] یا [تلفن] یا [ایمیل]"
    assert redact("تلفن ۰۷۱-۳۷۷۴۲۶۲۷") == "تلفن [تلفن]"
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('متن اضافه {"a": 2} پایان') == {"a": 2}
    with pytest.raises(ModelError):
        parse_json("نه")


def test_gateway_off_and_incomplete(env):
    svc, _ = env()
    with pytest.raises(ModelError, match="خاموش"):
        ModelGateway(svc).complete_json("t", "v", "x", "y")
    svc2, _ = env("[llm]\nenabled = true\n")
    gw = ModelGateway(svc2)
    assert not gw.ready
    with pytest.raises(ModelError, match="خالی"):
        gw.complete_json("t", "v", "x", "y")


def test_gateway_call_log_budget_and_fallback(env):
    svc, _ = env(LLM_ON + "daily_call_limit = 2\n")
    fake = FakeModel('{"ok": true}', '{"ok": true}', fail_first_400=True)
    gw = ModelGateway(svc, fake)
    assert gw.complete_json("test", "v1", "دستور", "تماس 09121234567", "admin") == {"ok": True}
    url, headers, payload = fake.calls[-1]
    assert url == "http://127.0.0.1:11434/v1/chat/completions" and headers["Authorization"] == "Bearer KEY"
    assert "response_format" not in payload and "response_format" in fake.calls[0][2]
    user_msg = payload["messages"][1]["content"]
    assert user_msg.startswith("<<<DATA") and "09121234567" not in user_msg and "[تلفن]" in user_msg
    assert "غیرقابل اعتماد" in payload["messages"][0]["content"] and payload["temperature"] == 0
    gw.complete_json("test", "v1", "دستور", "x", "admin")
    with pytest.raises(ModelError, match="سقف"):
        gw.complete_json("test", "v1", "دستور", "x", "admin")
    assert [r["status"] for r in gw.recent()] == ["blocked", "ok", "ok"]
    gw2 = ModelGateway(svc, FakeModel("نه JSON"))
    svc_cfg_calls = gw2.calls_today()
    assert svc_cfg_calls == 2


def test_gateway_errors_are_logged(env):
    svc, _ = env(LLM_ON)
    with pytest.raises(ModelError, match="JSON"):
        ModelGateway(svc, FakeModel("نه JSON")).complete_json("t", "v", "x", "y")

    def down(url, headers, payload, timeout):
        raise OSError("refused")
    with pytest.raises(ModelError, match="اتصال"):
        ModelGateway(svc, down).complete_json("t", "v", "x", "y")
    assert [r["status"] for r in ModelGateway(svc).recent()] == ["error", "error"]


def test_price_list_rules(env):
    _, ms = env()
    assert price_list.suggest_multiplier(PRICE_TEXT) == 1000
    rows = price_list.attach_matches(price_list.parse_rules(PRICE_TEXT, "toman", 1000), ms.profiles())
    by = {r["title"]: r for r in rows}
    a = by["سنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت"]
    assert a["price_rial"] == 24_500_000 and a["product_key"] == "FIB101-480" and a["include"]
    b = by["سنسور اکسیژن ساژم سوکت سبز سیم سبز 35 سانت"]
    assert b["price_rial"] == 23_800_000 and b["product_key"] == "FIB107-350"
    c = by["سنسور اکسیژن ME17 بوش سوکت مشکی"]
    assert c["price_rial"] is None and c["availability"] == "out_of_stock"
    d = by["بکس سنسور اکسیژن"]
    assert not d["include"]
    assert len(rows) == 4
    assert price_list.parse_rules("سنسور اکسیژن زیمنس 2,450,000", "toman", 1)[0]["price_rial"] == 24_500_000
    assert price_list.parse_rules("سنسور اکسیژن L90 سیم زرد 2,300,000 تومان", "toman", 1000)[0]["price_rial"] == 23_000_000
    for line in ("سنسور اکسیژن پژو 405 — ناموجود", "سنسور اکسیژن بوش 0258006027 تماس بگیرید",
                 "سنسور اکسیژن زیمنس سیم 50 سانت استعلام"):
        assert price_list.parse_rules(line, "toman", 1000)[0]["price_rial"] is None, line


def test_price_list_model_drops_invented_prices(env):
    svc, ms = env(LLM_ON)
    fake = FakeModel(json.dumps({"items": [
        {"title": "سنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت", "price": "2450", "available": None},
        {"title": "سنسور ساختگی", "price": "9999", "available": True},
        {"title": "<b>bad</b>", "price": "abc"}]}))
    rows = price_list.parse_model(ModelGateway(svc, fake), PRICE_TEXT, "toman", 1000, "admin")
    assert [r["title"] for r in rows] == ["سنسور اکسیژن زیمنس سوکت طوسی NTK سیم 50 سانت"]
    assert rows[0]["source"] == "model" and rows[0]["price_rial"] == 24_500_000


def lost_inquiry(sales, note, reason="other"):
    iid = sales.create_inquiry({"customer_name": "بنکدار الف", "product_key": "FIB101-480", "qty": "10"}, "s")
    sales.close_inquiry(iid, "lost", "s", "viewer", {"lost_reason": reason, "note": note})
    return iid


def test_lost_reason_suggestions(env):
    svc, ms = env()
    assert reasons.suggest_rules("گفت چک ۹۰ روزه می‌خواهد") == "payment"
    assert reasons.suggest_rules("از رقیب ارزون‌تر خرید") == "price"
    assert reasons.suggest_rules("بدون توضیح") is None
    a = lost_inquiry(ms.sales, "مشتری گفت قیمت گرونه")
    b = lost_inquiry(ms.sales, "بدون توضیح")
    lost_inquiry(ms.sales, "چک ۹۰ روزه می‌خواست", "payment")
    res = reasons.run(svc, None)
    assert res["checked"] == 2 and res["suggested"] == 1
    pend = reasons.pending(svc)
    assert set(pend) == {a} and pend[a]["value"] == "price" and pend[a]["source"] == "rules"
    reasons.decide(svc, ms.sales, pend[a]["id"], True, "finance")
    assert ms.sales.inquiry(a)["lost_reason"] == "price" and reasons.pending(svc) == {}
    assert reasons.run(svc, None)["checked"] == 1
    svc2, ms2 = env(LLM_ON)
    c = lost_inquiry(ms2.sales, "یه چیزی گفت که نفهمیدم")
    gw = ModelGateway(svc2, FakeModel('{"reason": "quality", "confidence": 0.7}'))
    assert reasons.run(svc2, gw)["model"] == 1
    assert reasons.pending(svc2)[c]["value"] == "quality" and reasons.pending(svc2)[c]["confidence"] == "0.7"


def test_listing_drafts_and_guardrail(env):
    svc, ms = env()
    did = content.generate(ms, None, "FIB101-480", "finance")
    d = content.draft(svc, did)
    assert d["source"] == "rules" and "زیمنس" in d["title"] and "طرح NTK" in d["title"] and "OZA632-SZ2" in d["title"]
    assert d["body"].endswith("این سنسور طرح NTK و ساخت برنا سنسور است، نه قطعه اصلی NTK.")
    assert "طول سیم: ۴۸ سانتی‌متر" in d["body"] and "خودروی سازگار نامشخص" in d["flags_l"]
    content.decide(svc, did, True, "finance")
    did2 = content.generate(ms, None, "FIB101-480", "finance")
    content.decide(svc, did2, True, "finance")
    assert content.draft(svc, did)["status"] == "discarded" and content.draft(svc, did2)["status"] == "approved"
    svc2, ms2 = env(LLM_ON)
    ok = ModelGateway(svc2, FakeModel('{"title": "سنسور اکسیژن زیمنس طرح NTK سوکت طوسی", "body": "متن روان"}'))
    d2 = content.draft(svc2, content.generate(ms2, ok, "FIB101-480", "finance"))
    assert d2["source"] == "model" and d2["body"].startswith("متن روان") and d2["body"].endswith("نه قطعه اصلی NTK.")
    bad = ModelGateway(svc2, FakeModel('{"title": "سنسور اکسیژن اورجینال", "body": "کاملاً اصل"}'))
    d3 = content.draft(svc2, content.generate(ms2, bad, "FIB101-480", "finance"))
    assert d3["source"] == "rules" and any("اصل" in f for f in d3["flags_l"])
    res = ms2.run_agent("content_writer", "manual", "finance")
    assert res["status"] == "ok" and res["stats"]["drafts"] == 26


def test_customer_reply_drafts(env):
    svc, ms = env(LLM_ON)
    q = "سلام سنسور اکسیژن زیمنس سوکت طوسی سیم ۵۰ سانت برای ۴۰۵ دارید؟ شماره من 09121234567"
    out = reply.draft(ms, None, q, "sales")
    assert "09121234567" not in out["question"] and out["options"][0]["key"] == "FIB101-480"
    assert "۲٬۱۰۰٬۰۰۰ تومان" in out["text"] and out["source"] == "rules"
    invented = ModelGateway(svc, FakeModel('{"reply": "قیمت ۱٬۹۰۰٬۰۰۰ تومان است"}'))
    out = reply.draft(ms, invented, q, "sales")
    assert out["source"] == "rules" and any("عددی" in n for n in out["notes"])
    faithful = ModelGateway(svc, FakeModel('{"reply": "سلام، FIB101-480 داریم؛ هر عدد ۲٬۱۰۰٬۰۰۰ تومان."}'))
    out = reply.draft(ms, faithful, q, "sales")
    assert out["source"] == "model" and "۲٬۱۰۰٬۰۰۰" in out["text"]
    none = reply.draft(ms, None, "یک سنسور می‌خواهم", "sales")
    assert none["options"] == [] and "پارت‌نامبر" in none["text"]
    assert reply.draft(ms, None, "", "sales")["error"]


def test_ai_agents_registered(env):
    svc, ms = env()
    from borna_pricing.market.agents import registry
    assert {"reason_analyst", "content_writer"} <= set(registry())
    lost_inquiry(ms.sales, "گفت گران است")
    done = {d["agent"]: d for d in ms.scheduled_tick()}
    assert done["reason_analyst"]["stats"]["suggested"] == 1
    assert "reason_analyst" not in {d["agent"] for d in ms.scheduled_tick()}
