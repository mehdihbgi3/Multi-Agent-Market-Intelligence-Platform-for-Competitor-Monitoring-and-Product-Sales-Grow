from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from .. import fmt
from ..clock import iso
from . import compare

NEAR = Decimal("0.05")


def _chg(new, old):
    if not new or not old:
        return None
    return Decimal(new) / Decimal(old) - 1


def _rate_toman(c, at: str):
    row = c.execute("SELECT value_toman FROM rates WHERE accepted=1 AND fetched_at<=? ORDER BY fetched_at DESC, id DESC"
                    " LIMIT 1", (at,)).fetchone()
    return row["value_toman"] if row else None


def _gap_text(name: str, key: str, s: str, x: dict) -> str:
    where = "از میانه قیمت هر عدد عمده‌فروش‌های هم‌طرح" if s == "wholesale" else "از میانه قیمت تکی رقبای هم‌طرح"
    mine = "قیمت هر عدد برنا (لیست رسمی)"
    side_word = "ارزان‌تر" if x["gap"] < 0 else "گران‌تر"
    return (f"{name} ({key}): {mine} {fmt.pct_plain_words(abs(Decimal(str(x['gap']))))} {side_word} {where} است "
            f"({fmt.fa(x['n_priced'])} قیمت{'؛ کم‌داده، با احتیاط بخوانید' if x['low_data'] else ''}).")


def build(ms, end: datetime) -> dict:
    start = end - timedelta(days=7)
    with ms.db() as c:
        off = ms._official(c)
        products = [dict(p) for p in c.execute("SELECT * FROM products WHERE active=1 ORDER BY row_no, key")]
        all_now = ms.competitor_rows(c, end)
        all_old = ms.competitor_rows(c, start)
        now_rows = [r for r in all_now if r["fresh"] and not r["is_own"]]
        old_rows = [r for r in all_old if r["fresh"] and not r["is_own"]]
        fx_end, fx_start = _rate_toman(c, iso(end)), _rate_toman(c, iso(start))
        new_listings = c.execute("SELECT COUNT(*) FROM external_products WHERE first_seen_at>? AND first_seen_at<=?",
                                 (iso(start), iso(end))).fetchone()[0]
        to_review = c.execute("SELECT COUNT(*) FROM external_products WHERE match_state='proposed'").fetchone()[0]
        obs_review = c.execute("SELECT COUNT(*) FROM observations WHERE status IN ('pending','quarantined')").fetchone()[0]
        failing = [dict(r) for r in c.execute(
            "SELECT name, failure_count, last_error FROM sources WHERE failure_count>0 AND active=1 ORDER BY name")]
        new_biz = [dict(r) for r in c.execute(
            "SELECT name, kind, city, address, phone, sources, example_url FROM leads WHERE first_seen_at>? AND"
            " first_seen_at<=?"
            " ORDER BY kind, city, name", (iso(start), iso(end)))]
        biz_totals = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) AS n FROM leads GROUP BY kind")}
    fx_chg = _chg(fx_end, fx_start)
    rows, insights = [], []
    min_prices = ms.cfg.market.min_prices
    for p in products:
        eq_all = [r for r in now_rows if r["product_key"] == p["key"] and r["relation"] == "equivalent"]
        eq_now, sep_now = ms.split_tiers(eq_all)
        eq_old = ms.split_tiers([r for r in old_rows if r["product_key"] == p["key"] and r["relation"] == "equivalent"])[0]
        sub_now = [r for r in now_rows if r["product_key"] == p["key"] and r["relation"] == "substitute"]
        official = off["prices"].get(p["key"])
        cmp = compare.compare(official, eq_now, min_prices)
        old = compare.side_stats(eq_old)
        for side in compare.SIDES:
            x = cmp[side]
            x["median_old"] = old[side]["median"]
            chg = _chg(x["median"], x["median_old"])
            x["change"] = float(chg) if chg is not None else None
        st_sep, st_sub = compare.side_stats(sep_now), compare.side_stats(sub_now)
        priced = [r for r in eq_all + sub_now if compare.priced(r)]
        cheapest = {}
        for side in compare.SIDES:
            best = min((r for r in priced if compare.obs_side(r["obs"]) == side),
                       key=lambda r: r["obs"]["price_rial"], default=None)
            cheapest[side] = ({"seller": best["seller"] or best["source_name"], "price": best["obs"]["price_rial"],
                               "level": best["obs"]["price_level"], "tier": best["tier"], "relation": best["relation"]}
                              if best else None)
        oos = sum(1 for r in eq_all + sub_now if r["obs"]["availability"] == "out_of_stock")
        row = {"key": p["key"], "name": p["name"], "official": official, "cmp": cmp,
               "n": len(eq_now), "n_sub": len(sub_now), "n_sep": len(sep_now), "oos": oos,
               "low_data": cmp["wholesale"]["low_data"] or cmp["retail"]["low_data"],
               "sep_median": st_sep["wholesale"]["median"] or st_sep["retail"]["median"],
               "substitute_median": st_sub["wholesale"]["median"] or st_sub["retail"]["median"],
               "cheapest_side": cheapest, "cheapest": cheapest["wholesale"] or cheapest["retail"]}
        rows.append(row)
        for side in compare.SIDES:
            x = cmp[side]
            if x["gap"] is not None and abs(Decimal(str(x["gap"]))) > NEAR:
                insights.append({"kind": "position", "side": side, "key": p["key"],
                                 "text": _gap_text(p["name"], p["key"], side, x)})
        lag = next((cmp[sd]["change"] for sd in compare.SIDES if cmp[sd]["change"] is not None), None)
        if fx_chg is not None and lag is not None and abs(fx_chg) >= Decimal("0.02"):
            if abs(Decimal(str(lag))) < abs(fx_chg) / 2:
                insights.append({"kind": "lag", "key": p["key"],
                                 "text": f"{p['name']}: دلار این هفته {fmt.pct_words(fx_chg)} شد ولی میانه قیمت رقبا "
                                         f"{fmt.pct_words(Decimal(str(lag)))}؛ رقبا هنوز با دلار هم‌قدم نشده‌اند."})
        if oos:
            insights.append({"kind": "oos", "key": p["key"],
                             "text": f"{p['name']}: {fmt.fa(oos)} آگهی رقیب ناموجود است؛ فرصت پیشنهاد به مشتریان آن‌ها."})
    if new_listings:
        insights.append({"kind": "new", "key": None, "text": f"{fmt.fa(new_listings)} آگهی تازه در این هفته پیدا شد."})
    if to_review or obs_review:
        insights.append({"kind": "review", "key": None,
                         "text": f"{fmt.fa(to_review)} آگهی منتظر تأیید تطبیق و {fmt.fa(obs_review)} قیمت منتظر بررسی است."})
    if new_biz:
        from ..sales.service import LEAD_KINDS
        kinds = Counter(b["kind"] for b in new_biz)
        parts = [f"{fmt.fa(kinds[k])} {label}" for k, label in LEAD_KINDS.items() if kinds.get(k)]
        cities = Counter(b["city"] for b in new_biz if b["city"]).most_common(5)
        where = "؛ بیشترین در " + "، ".join(f"{name} ({fmt.fa(n)})" for name, n in cities) if cities else ""
        insights.append({"kind": "discovery", "key": None,
                         "text": f"کشف بازار: این هفته {'، '.join(parts)} تازه پیدا شد{where}."})
        for b in new_biz:
            b["sources"] = json.loads(b["sources"] or "[]")
    for s in failing:
        insights.append({"kind": "source", "key": None,
                         "text": f"منبع «{s['name']}» {fmt.fa(s['failure_count'])} بار پشت سر هم خوانده نشد: "
                                 f"{(s['last_error'] or '')[:160]}"})
    return {"version": 3, "period_start": iso(start), "period_end": iso(end), "fx_start": fx_start, "fx_end": fx_end,
            "fx_change": str(fx_chg) if fx_chg is not None else None, "rows": rows, "insights": insights,
            "new_listings": new_listings, "to_review": to_review, "obs_review": obs_review,
            "discovery": {"new": new_biz[:500], "n_new": len(new_biz), "totals": biz_totals},
            "separate_label": ms.separate_label(), "min_prices": min_prices,
            "publication_id": off["publication"]["id"] if off["publication"] else None}


def _old_row(r: dict) -> dict:
    level = r.get("level")
    w_med, r_med = r.get("wholesale_median"), r.get("retail_median")
    if r_med is None and level == "unknown":
        r_med = r.get("median")
    out = {}
    for side, med in (("wholesale", w_med), ("retail", r_med)):
        mine = r.get("official")
        same = level is not None and compare.side(level) == side
        g = _chg(mine, med)
        out[side] = {"borna": mine, "median": med, "median_old": r.get("median_old") if same else None,
                     "n_priced": r.get("n_priced", 0) if same else (1 if med else 0), "n": r.get("n_priced", 0) if same else 0,
                     "min": None, "max": None, "oos": 0, "gap": float(g) if g is not None else None,
                     "change": float(Decimal(r["competitor_change"])) if same and r.get("competitor_change") else None,
                     "low_data": bool(r.get("low_data")) and same, "label": compare.SIDE_LABELS[side]}
    ch = r.get("cheapest")
    side_of = compare.side(ch["level"]) if ch else None
    return {**r, "cmp": out, "cheapest_side": {s: (ch if side_of == s else None) for s in compare.SIDES}}


def _with_official(r: dict) -> dict:
    cmp = {}
    for side, x in r["cmp"].items():
        x = dict(x)
        x["borna"] = r.get("official")
        g = _chg(r.get("official"), x.get("median"))
        x["gap"] = float(g) if g is not None else None
        cmp[side] = x
    return {**r, "cmp": cmp}


def normalize(data: dict) -> dict:
    version = data.get("version", 1)
    if version >= 3:
        return data
    out = dict(data)
    rows = [_old_row(r) for r in data.get("rows", [])] if version < 2 else data.get("rows", [])
    out["rows"] = [_with_official(r) for r in rows]
    found = []
    for r in out["rows"]:
        for side in compare.SIDES:
            x = r["cmp"][side]
            if x.get("gap") is not None and x.get("n_priced") and abs(Decimal(str(x["gap"]))) > NEAR:
                found.append({"kind": "position", "side": side, "key": r["key"],
                              "text": _gap_text(r["name"], r["key"], side, x)})
    out["insights"] = found + [i for i in data.get("insights", []) if i.get("kind") not in ("no_retail", "position")]
    return out


def charts(data: dict) -> dict:
    items = [{"key": r["key"], "name": r["name"], "cmp": r["cmp"]} for r in data["rows"]]
    return {s: compare.chart(items, s) for s in compare.SIDES}, {s: compare.summary(items, s) for s in compare.SIDES}


def _pct(x):
    return float(x) if x is not None else None


def xlsx(rep: dict, company: str) -> bytes:
    from .. import xlsx_style as xs

    data = normalize(rep["data"])
    wb = xs.book()
    sep = data.get("separate_label") or "اصلی"
    head = ["کد", "نام کالا", "قیمت هر عدد برنا، لیست رسمی (ریال)",
            "عمده: میانه هر عدد رقبا (ریال)", "عمده: فاصله برنا", "عمده: تعداد قیمت", "عمده: تغییر میانه در هفته",
            "تکی: میانه رقبا (ریال)", "تکی: فاصله برنا", "تکی: تعداد قیمت", "تکی: تغییر میانه در هفته", "کم‌داده", "میانه جایگزین‌ها (ریال)", f"میانه {sep} (ریال)",
            "آگهی هم‌طرح", "آگهی جایگزین", f"آگهی {sep}", "ناموجود",
            "ارزان‌ترین عمده‌فروش", "قیمت ارزان‌ترین عمده (ریال)", "ارزان‌ترین تکی", "قیمت ارزان‌ترین تکی (ریال)"]
    body = []
    for r in data["rows"]:
        w, t = r["cmp"]["wholesale"], r["cmp"]["retail"]
        cw, ct = (r.get("cheapest_side") or {}).get("wholesale") or {}, (r.get("cheapest_side") or {}).get("retail") or {}
        body.append([r["key"], r["name"], r.get("official") or w["borna"], w["median"], _pct(w["gap"]),
                     w["n_priced"] or None, _pct(w.get("change")), t["median"], _pct(t["gap"]), t["n_priced"] or None,
                     _pct(t.get("change")), "بله" if r.get("low_data") else None, r.get("substitute_median"),
                     r.get("sep_median"), r["n"], r["n_sub"], r.get("n_sep", 0), r["oos"],
                     cw.get("seller"), cw.get("price"), ct.get("seller"), ct.get("price")])
    money = {j: xs.MONEY for j in (2, 3, 7, 12, 13, 19, 21)}
    pct = {j: xs.PCT for j in (4, 6, 8, 10)}
    formats = {**money, **pct}
    widths = xs.fit_widths(head, body, formats, {1: 40}, 8, 40)
    t_ws = xs.sheet(wb, "کالاها", first=True)
    top = xs.title_rows(t_ws, [(f"گزارش هفتگی بازار — {company}", "title"),
                              (f"از {fmt.jdatetime(data['period_start'])} تا {fmt.jdatetime(data['period_end'])}", "sub"),
                              ("همه قیمت‌ها برای یک عدد است. قیمت برنا قیمت هر عدد در لیست رسمی است (حداقل سفارش "
                               "۱۰۰ عدد). «عمده» آن را با میانه قیمت هر عدد عمده‌فروش‌ها می‌سنجد (قیمت بسته‌ها بر "
                               "تعداد تقسیم شده) و «تکی» با میانه قیمت تکی رقبا. فاصله مثبت یعنی برنا گران‌تر.", "note")],
                        len(head), widths) + 1
    xs.table(t_ws, top, head, body, formats=formats, widths=widths, bold_cols=(2,), freeze_cols=2)
    xs.page(t_ws, landscape=True)

    s_ws = xs.sheet(wb, "خلاصه")
    lines = [(f"گزارش هفتگی بازار — {company}", "title"),
             (f"از {fmt.jdatetime(data['period_start'])} تا {fmt.jdatetime(data['period_end'])}", "sub")]
    if data.get("fx_end"):
        lines.append((f"دلار آزاد: {fmt.money(data['fx_start']) if data.get('fx_start') else '—'} ← "
                      f"{fmt.money(data['fx_end'])} تومان"
                      + (f" ({fmt.pct(Decimal(data['fx_change']))})" if data.get("fx_change") else ""), "note"))
    s_rows = [[i, ins["text"]] for i, ins in enumerate(data["insights"], start=1)] or [[None, "نکته‌ای نیست."]]
    s_w = [7, 110]
    top = xs.title_rows(s_ws, lines, 2, s_w) + 1
    xs.table(s_ws, top, ["ردیف", "نکته‌های این هفته"], s_rows, widths=s_w, center_cols=(0,), autofilter=False)
    xs.page(s_ws)
    wb.move_sheet(s_ws, offset=-1)
    wb.active = 0

    found = (data.get("discovery") or {}).get("new") or []
    if found:
        from ..sales.leads_xlsx import link_cell
        from ..sales.service import LEAD_KINDS
        b = xs.sheet(wb, "کسب‌وکارهای تازه")
        b_head = ["نام", "نوع", "شهر", "نشانی", "تلفن", "منبع", "لینک"]
        b_rows = [[r["name"], LEAD_KINDS.get(r["kind"], r["kind"]), r["city"], r.get("address"), r.get("phone"),
                   "، ".join(r["sources"]), r["example_url"]] for r in found]
        b_w = xs.fit_widths(b_head, b_rows, {}, {3: 40, 6: 18}, 8, 40)
        res = xs.table(b, 1, b_head, b_rows, widths=b_w, formats={4: xs.TEXT})
        for i in range(len(b_rows)):
            link_cell(b.cell(row=res["top"] + 1 + i, column=7), b_rows[i][6])
        xs.page(b, landscape=True)
    return xs.save(wb)
