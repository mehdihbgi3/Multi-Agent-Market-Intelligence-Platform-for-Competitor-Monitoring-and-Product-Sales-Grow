from __future__ import annotations

from decimal import Decimal

from .. import engine, fmt
from ..clock import iso, now_utc

ACTIONS = {"lower": "کاهش قیمت پایه", "raise": "افزایش قیمت پایه", "keep": "بدون تغییر",
           "floor": "زیر کف قیمت؛ افزایش لازم", "insufficient": "داده عمده کافی نیست"}


def proposals(ms, sales) -> dict:
    cfg = ms.cfg
    band = cfg.advisor.target_band
    params = engine.PricingParams.from_config(cfg.pricing)
    with ms.db() as c:
        pub = c.execute("SELECT * FROM publications ORDER BY id DESC LIMIT 1").fetchone()
        products = [dict(p) for p in c.execute("SELECT * FROM products WHERE active=1 ORDER BY row_no, key")]
        rows = [r for r in ms.competitor_rows(c) if r["fresh"] and not r["is_own"] and r["relation"] == "equivalent"
                and not ms.separate(r)]
        rate = ms._rate_at(c, iso(now_utc()))
    official = sales.official_prices()
    costs = sales.costs()
    rate_toman = rate["value_toman"] if rate else cfg.pricing.base_rate_toman
    out = []
    for p in products:
        price = official.get(p["key"])
        if not price or pub is None:
            continue
        st = ms.stats([r for r in rows if r["product_key"] == p["key"]])["wholesale"]
        alpha = Decimal(p["alpha"]) if p["alpha"] else params.alpha_default
        factor = engine.exact_price(p["base_price_rial"], alpha, pub["rate_rial"], params.base_rate_rial) / Decimal(p["base_price_rial"])
        floor = None
        cost = costs.get(p["key"])
        if cost and cfg.advisor.min_margin is not None:
            floor = int(Decimal(cost["cost_usd"]) * rate_toman * 10 / (1 - cfg.advisor.min_margin))
        item = {"key": p["key"], "name": p["name"], "official": price, "base": p["base_price_rial"],
                "median": st["median"], "n": st["n_priced"], "min": st["min"], "position": None, "action": "insufficient",
                "target": None, "new_base": None, "floor": floor, "cost_usd": cost["cost_usd"] if cost else None,
                "reason": ""}
        target = None
        if st["median"] and st["n_priced"] >= cfg.advisor.min_prices:
            med = Decimal(st["median"])
            item["position"] = Decimal(price) / med - 1
            hi, lo = med * (1 + band), med * (1 - band)
            if Decimal(price) > hi:
                target, item["action"] = hi, "lower"
                item["reason"] = (f"قیمت رسمی {fmt.pct_plain_words(item['position'])} بالاتر از میانه عمده "
                                  f"{fmt.fa(st['n_priced'])} رقیب هم‌طرح است؛ پیشنهاد: حداکثر "
                                  f"{fmt.pct_plain_words(band)} بالاتر از میانه.")
            elif Decimal(price) < lo:
                target, item["action"] = lo, "raise"
                item["reason"] = (f"قیمت رسمی {fmt.pct_plain_words(-item['position'])} پایین‌تر از میانه عمده "
                                  f"{fmt.fa(st['n_priced'])} رقیب هم‌طرح است؛ جای افزایش هست.")
            else:
                item["action"] = "keep"
                item["reason"] = f"در محدوده ±{fmt.pct_plain(band)} میانه عمده رقبا."
        else:
            item["reason"] = (f"برای پیشنهاد دست‌کم {fmt.fa(cfg.advisor.min_prices)} قیمت عمده تازه و تأییدشده از "
                              "رقبای هم‌طرح لازم است.")
        if floor:
            if Decimal(price) < floor and (target is None or target < floor):
                target, item["action"] = Decimal(floor), "floor"
                item["reason"] = (f"قیمت رسمی زیر کف قیمت ({fmt.money(floor // 10)} تومان با حاشیه "
                                  f"{fmt.pct_plain(cfg.advisor.min_margin)}) است.")
            elif target is not None and target < floor:
                target = Decimal(floor)
                item["reason"] += f" پیشنهاد به کف قیمت ({fmt.money(floor // 10)} تومان) محدود شد."
        if target is not None:
            new_base = engine.round_price(target / factor, cfg.pricing.round_step_rial)
            if new_base == p["base_price_rial"]:
                item["action"] = "keep"
                item["reason"] += " تغییر کمتر از یک پله گرد کردن است."
            else:
                item["target"] = int(target)
                item["new_base"] = new_base
        out.append(item)
    return {"created_at": iso(now_utc()), "rate_toman": rate_toman, "band": str(band), "items": out,
            "n_changes": sum(1 for x in out if x["new_base"])}


def xlsx(data: dict, company: str) -> bytes:
    from .. import xlsx_style as xs

    wb = xs.book()
    ws = xs.sheet(wb, "پیشنهاد قیمت پایه", first=True)
    head = ["کد", "نام کالا", "قیمت پایه فعلی (ریال)", "قیمت پایه پیشنهادی (ریال)", "تغییر", "قیمت رسمی امروز (ریال)",
            "میانه عمده رقبا، هر عدد (ریال)", "تعداد قیمت رقبا", "کف قیمت (ریال)", "پیشنهاد", "دلیل"]
    body = []
    for it in data["items"]:
        change = (it["new_base"] / it["base"] - 1) if it["new_base"] else None
        body.append([it["key"], it["name"], it["base"], it["new_base"], float(change) if change is not None else None,
                     it["official"], it["median"], it["n"], it["floor"], ACTIONS[it["action"]], it["reason"]])
    formats = {2: xs.MONEY, 3: xs.MONEY, 4: xs.PCT, 5: xs.MONEY, 6: xs.MONEY, 8: xs.MONEY}
    widths = xs.fit_widths(head, body, formats, {1: 40, 10: 60}, 8, 40)
    top = xs.title_rows(ws, [(f"پیشنهاد قیمت پایه — {company} — {fmt.jdatetime(data['created_at'])}", "title"),
                             ("این فایل فقط پیشنهاد است. اگر تأیید شد، قیمت پایه را در فایل اصلی کالاها عوض کنید و از "
                              "«کالاها ← پیش‌نمایش تغییرها» وارد کنید تا از مسیر تأیید عادی بگذرد. مقایسه فقط با قیمت "
                              "هر عدد عمده‌فروش‌هاست (قیمت بسته‌ها بر تعداد تقسیم شده).", "note")],
                       len(head), widths) + 1
    xs.table(ws, top, head, body, formats=formats, widths=widths, freeze_cols=2)
    xs.page(ws, landscape=True)
    return xs.save(wb)
