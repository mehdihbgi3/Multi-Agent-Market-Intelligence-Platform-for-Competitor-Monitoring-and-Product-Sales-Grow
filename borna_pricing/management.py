from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import fmt, jalali, viz
from .clock import iso, now_utc, parse_iso, tehran_date
from .market import compare, textnorm as tn
from .sales.service import LEAD_KINDS, LOST_REASONS

PERIODS = (30, 90, 180, 365)
BAND = 0.02
DROP = -0.10
KIND_ORDER = ("seller", "supplier", "repair", "manufacturer")
STACK_ORDER = ("seller", "repair", "supplier", "manufacturer")
KIND_COLORS = {"seller": "#2a78d6", "repair": "#eb6834", "supplier": "#1baf7a", "manufacturer": "#eda100"}
BRAND_FA = {"Crouse": "کروز", "Isaco": "ایساکو", "Saipa Yadak": "سایپا یدک", "Ezam": "عظام"}
LEVELS = compare.SIDE_LABELS
FX_COLORS = ("#1baf7a", "#17202b")


def _median(values):
    return statistics.median(values) if values else None


def _gap(mine, theirs):
    return mine / theirs - 1 if mine and theirs else None


def _level(row) -> str:
    return compare.obs_side(row["obs"])


def compact_toman(rial) -> str:
    if rial is None:
        return "—"
    toman = rial / 10
    if abs(toman) >= 1e9:
        return fmt.fa(f"{toman / 1e9:.1f}".rstrip("0").rstrip(".").replace(".", "٫")) + " میلیارد"
    if abs(toman) >= 1e6:
        return fmt.fa(f"{toman / 1e6:.1f}".rstrip("0").rstrip(".").replace(".", "٫")) + " میلیون"
    return fmt.money(round(toman))


def gap_words(gap) -> str:
    if gap is None:
        return "—"
    if abs(gap) <= BAND:
        return "هم‌قیمت"
    return f"{fmt.pct(abs(gap), 1, signed=False)} {'گران‌تر' if gap > 0 else 'ارزان‌تر'}"


def fx_words(gap) -> str | None:
    if gap is None:
        return None
    if abs(gap) < 0.005:
        return "دلار هم‌اندازه نرخ لیست است"
    return f"دلار {fmt.pct(abs(gap), 1, signed=False)} {'بالاتر' if gap > 0 else 'پایین‌تر'} از نرخ لیست"


def _nice_axis(values, floor: float = 0.1) -> float:
    top = max([abs(v) for v in values if v is not None] + [floor])
    for step in (0.1, 0.2, 0.3, 0.5, 0.75, 1.0):
        if top <= step:
            return step
    return 1.0


def _market(ms, c, now) -> dict:
    off = ms._official(c)
    official = off["prices"]
    products = [dict(p) for p in c.execute("SELECT key, name FROM products WHERE active=1 ORDER BY row_no, key")]
    names = {p["key"]: p["name"] for p in products}
    every = ms.competitor_rows(c, now)
    fresh = [r for r in every if r["fresh"] and not r["is_own"] and r["relation"] == "equivalent"]
    priced = [r for r in fresh if compare.priced(r)]
    main, _ = ms.split_tiers(priced)
    rows = []
    for p in products:
        mine = [r for r in main if r["product_key"] == p["key"]]
        cmp = compare.compare(official.get(p["key"]), mine, ms.cfg.market.min_prices)
        w, rt = cmp["wholesale"], cmp["retail"]
        cheapest = {}
        for s in compare.SIDES:
            best = min((r for r in mine if _level(r) == s), key=lambda r: r["obs"]["price_rial"], default=None)
            cheapest[s] = ({"seller": best["seller"] or best["source_name"], "price": best["obs"]["price_rial"],
                            "level": LEVELS[s]} if best else None)
        rows.append({"key": p["key"], "name": p["name"], "official": w["borna"], "n": len(mine), "cmp": cmp,
                     "w_median": w["median"], "w_n": w["n_priced"], "r_median": rt["median"], "r_n": rt["n_priced"],
                     "gap_w": w["gap"], "gap_r": rt["gap"], "w_min": w["min"], "w_max": w["max"], "r_min": rt["min"],
                     "r_max": rt["max"], "cheapest_side": cheapest,
                     "cheapest": cheapest["wholesale"] or cheapest["retail"]})
    summary = {s: compare.summary(rows, s) for s in compare.SIDES}
    lanes = {s: compare.chart(rows, s) for s in compare.SIDES}
    groups = _groups(ms, priced, {r["key"]: r["official"] for r in rows}, names)
    sellers = {tn.norm(r["seller"] or r["source_name"]) for r in priced}
    extremes = {"priciest": [], "cheapest_all": []}
    for r in rows:
        for s, lo, hi in (("wholesale", "w_min", "w_max"), ("retail", "r_min", "r_max")):
            mine = r["official"]
            if mine and r[hi] and mine > r[hi]:
                extremes["priciest"].append({"key": r["key"], "side": LEVELS[s], "borna": mine, "other": r[hi]})
            if mine and r[lo] and mine < r[lo]:
                extremes["cheapest_all"].append({"key": r["key"], "side": LEVELS[s], "borna": mine, "other": r[lo]})
    return {"official": official, "publication": off["publication"], "rows": rows, "summary": summary, "lanes": lanes,
            **groups, "n_prices": len(priced), "n_sellers": len(sellers), "n_main": len(main),
            "sep_label": ms.separate_label(), "stale_days": ms.cfg.market.stale_days, **extremes,
            "has_market": bool(priced)}


def _bars(items: list[dict], key: str = "gap") -> list[dict]:
    axis = _nice_axis([i[key] for i in items])
    for i in items:
        i["w"] = min(abs(i[key]), axis) / axis * 46 if i[key] is not None else 0
        i["beyond"] = i[key] is not None and abs(i[key]) > axis
    return items


def _groups(ms, priced, official, names) -> dict:
    brands: dict = {}
    sites: dict = {}
    sellers: dict = {}
    for r in priced:
        mine = official.get(r["product_key"])
        gap = _gap(mine, r["obs"]["price_rial"])
        if gap is None:
            continue
        attrs = json.loads(r["attrs"] or "{}")
        brand = attrs.get("brand")
        label = BRAND_FA.get(brand, brand) if brand else tn.TIER_LABELS.get(r["tier"] or "unknown", "نامشخص")
        b = brands.setdefault(label, {"label": label, "gaps": [], "sellers": set()})
        b["gaps"].append(gap)
        b["sellers"].add(tn.norm(r["seller"] or r["source_name"]))
        if not ms.separate(r):
            s = sites.setdefault(r["source_name"], {"label": r["source_name"], "gaps": [], "sellers": set()})
            s["gaps"].append(gap)
            s["sellers"].add(tn.norm(r["seller"] or r["source_name"]))
        name = (r["seller"] or r["source_name"]).strip()
        sl = sellers.setdefault(tn.norm(name), {"label": name, "gaps": [], "products": {}, "n": 0})
        sl["gaps"].append(gap)
        sl["n"] += 1
        best = sl["products"].get(r["product_key"])
        if best is None or r["obs"]["price_rial"] / mine < best:
            sl["products"][r["product_key"]] = r["obs"]["price_rial"] / mine

    def pack(d, limit):
        out = [{"label": v["label"], "gap": _median(v["gaps"]), "n": len(v["gaps"]), "sellers": len(v["sellers"])}
               for v in d.values()]
        out.sort(key=lambda x: (-x["n"], x["label"]))
        return _bars(out[:limit])

    seller_rows = []
    for v in sellers.values():
        cheaper = sum(1 for ratio in v["products"].values() if ratio < 1)
        seller_rows.append({"label": v["label"], "gap": _median(v["gaps"]), "n": v["n"], "products": len(v["products"]),
                            "cheaper": cheaper, "product_keys": sorted(v["products"])[:6],
                            "names": [names.get(k, k) for k in sorted(v["products"])[:6]]})
    seller_rows.sort(key=lambda x: (-x["products"], -x["n"], x["label"]))
    return {"brands": pack(brands, 8), "sites": pack(sites, 8), "sellers": _bars(seller_rows[:10])}


def _drops(c, now) -> list[dict]:
    ref_t = now - timedelta(days=30)
    names = {r["key"]: r["name"] for r in c.execute("SELECT key, name FROM products")}
    state: dict = {}
    for r in c.execute(
            "SELECT o.external_product_id AS id, o.observed_at, o.price_rial, o.kind, o.availability, e.seller,"
            " s.name AS source_name, m.product_key FROM observations o"
            " JOIN external_products e ON e.id=o.external_product_id JOIN sources s ON s.id=e.source_id"
            " JOIN matches m ON m.external_product_id=e.id AND m.status='approved'"
            " WHERE o.status='ok' AND e.is_own=0 ORDER BY o.observed_at, o.id"):
        if r["kind"] != "price" or r["availability"] == "out_of_stock" or not r["price_rial"]:
            continue
        d = state.setdefault(r["id"], {"ref": None, "last": None, "row": r})
        if parse_iso(r["observed_at"]) <= ref_t:
            d["ref"] = r["price_rial"]
        else:
            d["last"] = r["price_rial"]
            d["row"] = r
    out = []
    for d in state.values():
        if d["ref"] and d["last"]:
            change = d["last"] / d["ref"] - 1
            if change <= DROP:
                row = d["row"]
                out.append({"seller": row["seller"] or row["source_name"], "source": row["source_name"],
                            "key": row["product_key"], "name": names.get(row["product_key"], row["product_key"]),
                            "before": d["ref"], "after": d["last"], "change": change})
    out.sort(key=lambda x: x["change"])
    return out[:6]


def _price_series(c, key: str, start: datetime) -> list[tuple[datetime, int]]:
    pts, before = [], None
    for r in c.execute("SELECT p.published_at, pp.price_rial FROM publications p JOIN published_prices pp"
                       " ON pp.publication_id=p.id WHERE pp.product_key=? ORDER BY p.published_at, p.id", (key,)):
        t = parse_iso(r["published_at"])
        if t < start:
            before = r["price_rial"]
        else:
            pts.append((t, r["price_rial"]))
    return ([(start, before)] if before is not None else []) + pts


def _median_series(ms, c, key: str, start: datetime) -> dict:
    tiers = tuple(ms.cfg.market.separate_tiers)
    current = {lv: {} for lv in compare.SIDES}
    days = {lv: {} for lv in compare.SIDES}
    first = {lv: None for lv in compare.SIDES}
    sql = ("SELECT o.external_product_id AS id, o.observed_at, o.price_rial, o.kind, o.availability, o.price_level,"
           " o.pack_qty FROM observations o JOIN matches m ON m.external_product_id=o.external_product_id"
           " AND m.status='approved' AND m.product_key=? AND m.relation='equivalent'"
           " JOIN external_products e ON e.id=o.external_product_id WHERE o.status='ok' AND e.is_own=0"
           + (f" AND e.tier NOT IN ({','.join('?' for _ in tiers)})" if tiers else "")
           + " ORDER BY o.observed_at, o.id")
    for o in c.execute(sql, (key, *tiers)):
        lv = compare.side(o["price_level"], o["pack_qty"])
        if o["kind"] == "price" and o["availability"] != "out_of_stock" and o["price_rial"]:
            current[lv][o["id"]] = o["price_rial"]
        else:
            current[lv].pop(o["id"], None)
        t = parse_iso(o["observed_at"])
        value = _median(list(current[lv].values()))
        if t < start:
            first[lv] = value
        else:
            days[lv][tehran_date(t)] = (t, value)
    out = {}
    for lv in compare.SIDES:
        pts = ([(start, first[lv])] if first[lv] is not None else []) + [v for _, v in sorted(days[lv].items())]
        out[lv] = [(t, v) for t, v in pts if v is not None]
    return out


def _trends(ms, c, now, days, market) -> list[dict]:
    start = now - timedelta(days=days)
    top = sorted((r for r in market["rows"] if r["n"] and r["official"]), key=lambda r: (-r["n"], r["key"]))[:4]
    out = []
    kw = {"value_fmt": lambda v: fmt.money(round(v)), "axis_fmt": viz.axis_toman, "unit": "تومان", "start": start,
          "end": now, "compact": True}
    for r in top:
        med = _median_series(ms, c, r["key"], start)
        borna = _price_series(c, r["key"], start)
        lanes = []
        for s in compare.SIDES:
            drawn = [{**x, "points": [(t, v / 10) for t, v in x["points"]]}
                     for x in compare.lanes(s, borna, med[s])]
            lanes.append({"side": s, "title": compare.SIDE_TITLES[s], "legend": drawn, "has_market": bool(med[s]),
                          "chart": viz.time_lines(drawn, width=460, height=190, **kw) if med[s] else None,
                          "chart_n": viz.time_lines(drawn, width=360, height=200, **kw) if med[s] else None})
        out.append({"key": r["key"], "name": r["name"], "n": r["n"], "lanes": lanes})
    return out


def _fx(svc, st, now, days) -> dict:
    start = now - timedelta(days=days)
    acc = svc.accepted_rates(days)
    dollar = [(parse_iso(r["fetched_at"]), r["value_toman"]) for r in acc]
    pubs = sorted(svc.publications(1000), key=lambda p: (parse_iso(p["published_at"]), p["id"]))
    listed, before = [], None
    for p in pubs:
        t = parse_iso(p["published_at"])
        if t < start:
            before = p["rate_rial"] // 10
        else:
            listed.append((t, p["rate_rial"] // 10))
    listed = ([(start, before)] if before is not None else []) + listed
    series = [{"name": "دلار آزاد", "color": FX_COLORS[0], "step": False, "points": dollar},
              {"name": "نرخ لیست رسمی برنا", "color": FX_COLORS[1], "step": True, "points": listed}]
    drawn = [s for s in series if s["points"]]
    kw = {"value_fmt": lambda v: fmt.money(round(v)), "axis_fmt": viz.axis_toman, "unit": "تومان", "start": start,
          "end": now}
    chart = viz.time_lines(drawn, **kw)
    chart_n = viz.time_lines(drawn, width=360, height=230, compact=True, **kw)
    last = st["last_accepted"]
    week = [v for t, v in dollar if t <= now - timedelta(days=7)]
    recent = svc.accepted_rates(8)
    ref = week[-1] if week else (recent[0]["value_toman"] if recent else None)
    return {"chart": chart, "chart_n": chart_n, "dollar": last["value_toman"] if last else None,
            "dollar_at": last["fetched_at"] if last else None,
            "week_change": _gap(last["value_toman"], ref) if last and ref else None,
            "list_gap": float(st["change_vs_published"]) if st.get("change_vs_published") is not None else None,
            "n_rates": len(dollar), "n_pubs": len(listed)}


def _months(today: date) -> list[tuple[int, int]]:
    jy, jm, _ = jalali.to_jalali(today)
    out = []
    for i in range(11, -1, -1):
        y, m = jy, jm - i
        while m <= 0:
            y, m = y - 1, m + 12
        out.append((y, m))
    return out


def _sales(c, today: date, days: int) -> dict:
    d0 = today - timedelta(days=days - 1)
    p0 = d0 - timedelta(days=days)
    q = ("SELECT COUNT(DISTINCT o.id) AS n, COALESCE(SUM(l.qty * l.unit_price_rial), 0) AS s FROM orders o"
         " JOIN order_lines l ON l.order_id=o.id WHERE o.order_date>=? AND o.order_date<?")
    cur = c.execute(q, (d0.isoformat(), (today + timedelta(days=1)).isoformat())).fetchone()
    prev = c.execute(q, (p0.isoformat(), d0.isoformat())).fetchone()
    keys = _months(today)
    first = jalali.from_jalali(keys[0][0], keys[0][1], 1)
    buckets = {k: {"cash": 0, "credit": 0} for k in keys}
    for r in c.execute("SELECT o.order_date, o.payment_days, l.qty * l.unit_price_rial AS amt FROM orders o"
                       " JOIN order_lines l ON l.order_id=o.id WHERE o.order_date>=?", (first.isoformat(),)):
        y, m, _ = jalali.to_jalali(date.fromisoformat(r["order_date"]))
        if (y, m) in buckets:
            buckets[(y, m)]["credit" if r["payment_days"] else "cash"] += r["amt"]
    top = max([b["cash"] + b["credit"] for b in buckets.values()] + [1])
    months = []
    for (y, m), b in buckets.items():
        total = b["cash"] + b["credit"]
        months.append({"label": jalali.month_name(m), "year": y, "cash": b["cash"], "credit": b["credit"], "total": total,
                       "h_cash": b["cash"] / top * 150, "h_credit": b["credit"] / top * 150,
                       "cap": compact_toman(total) if total else ""})
    cities = [dict(r) for r in c.execute(
        "SELECT COALESCE(NULLIF(TRIM(cu.city), ''), 'نامشخص') AS label, SUM(l.qty * l.unit_price_rial) AS s,"
        " COUNT(DISTINCT o.customer_id) AS customers FROM orders o JOIN order_lines l ON l.order_id=o.id"
        " JOIN customers cu ON cu.id=o.customer_id WHERE o.order_date>=? AND o.order_date<? GROUP BY label"
        " ORDER BY s DESC, label LIMIT 8", (d0.isoformat(), (today + timedelta(days=1)).isoformat()))]
    top_c = max([x["s"] for x in cities] + [1])
    for x in cities:
        x["w"] = x["s"] / top_c * 100
        x["text"] = compact_toman(x["s"])
    return {"total": cur["s"], "orders": cur["n"], "prev": prev["s"], "change": _gap(cur["s"], prev["s"]),
            "months": months, "has_months": any(m["total"] for m in months), "cities": cities}


def _leads(c, now) -> dict:
    week = iso(now - timedelta(days=7))
    kinds = {k: {"kind": k, "label": LEAD_KINDS[k], "n": 0, "phone": 0, "new": 0, "color": KIND_COLORS[k]}
             for k in KIND_ORDER}
    cities: dict = {}
    for r in c.execute("SELECT l.kind, l.city, l.first_seen_at, (COALESCE(l.phone, '')!='' OR COALESCE(cu.phone, '')!='')"
                       " AS has_phone FROM leads l LEFT JOIN customers cu ON cu.id=l.customer_id"):
        k = kinds.get(r["kind"])
        if k is None:
            continue
        k["n"] += 1
        k["phone"] += int(bool(r["has_phone"]))
        k["new"] += int((r["first_seen_at"] or "") >= week)
        if r["city"]:
            ct = cities.setdefault(r["city"], {"label": r["city"], "total": 0, **{x: 0 for x in KIND_ORDER}})
            ct[r["kind"]] += 1
            ct["total"] += 1
    top = sorted(cities.values(), key=lambda x: (-x["total"], x["label"]))[:10]
    peak = max([x["total"] for x in top] + [1])
    for x in top:
        x["parts"] = [{"kind": k, "label": LEAD_KINDS[k], "n": x[k], "w": x[k] / peak * 100, "color": KIND_COLORS[k]}
                      for k in STACK_ORDER if x[k]]
    total = sum(k["n"] for k in kinds.values())
    return {"kinds": [kinds[k] for k in KIND_ORDER], "total": total, "new": sum(k["new"] for k in kinds.values()),
            "phone": sum(k["phone"] for k in kinds.values()), "cities": top,
            "legend": [{"label": LEAD_KINDS[k], "color": KIND_COLORS[k]} for k in STACK_ORDER]}


def _health(c, now) -> dict:
    def last(agent):
        row = c.execute("SELECT started_at, status FROM agent_runs WHERE agent=? ORDER BY id DESC LIMIT 1", (agent,)).fetchone()
        return dict(row) if row else None

    def one(sql, *args):
        return c.execute(sql, args).fetchone()[0]

    return {"scout": last("market_scout"), "watcher": last("price_watcher"),
            "fresh": one("SELECT COUNT(*) FROM observations WHERE status='ok' AND observed_at>=?",
                         iso(now - timedelta(days=7))),
            "broken": one("SELECT COUNT(*) FROM sources WHERE active=1 AND failure_count>0"),
            "to_review": one("SELECT COUNT(*) FROM external_products WHERE match_state='proposed'"),
            "obs_review": one("SELECT COUNT(*) FROM observations WHERE status IN ('pending','quarantined')")}


def _margins(sales, official, dollar_toman, min_margin) -> list[dict]:
    if not dollar_toman:
        return []
    out = []
    for key, row in sales.costs().items():
        price = official.get(key)
        try:
            cost = Decimal(str(row["cost_usd"]))
        except (InvalidOperation, KeyError, TypeError):
            continue
        if not price:
            continue
        margin = 1 - float(cost * dollar_toman * 10) / price
        low = min_margin is not None and margin < float(min_margin)
        note = ("زیر بهای تمام‌شده" if margin < 0 else "زیر کف سود" if low
                else f"بها {fmt.money(round(cost * dollar_toman))} تومان")
        out.append({"key": key, "margin": margin, "cost_toman": float(cost * dollar_toman), "price": price,
                    "low": low or margin < 0, "note": note})
    out.sort(key=lambda x: x["margin"])
    peak = max([x["margin"] for x in out] + [0.01])
    for x in out:
        x["w"] = max(x["margin"], 0) / peak * 100
    return out


def _decisions(st, health, advisor, followups) -> list[dict]:
    out = []
    if st["held"]:
        out.append({"text": f"نرخ تازه {fmt.money(st['held']['value_toman'])} تومان منتظر تصمیم است.",
                    "endpoint": "dashboard", "args": {}, "tone": "warn"})
    run = st["run"]
    if run and run["status"] == "draft":
        out.append({"text": "لیست تازه آماده انتشار است" + ("؛ تغییر بیش از سقف روزانه است و تأیید شما را لازم دارد."
                                                         if run["needs_approval"] else "."),
                    "endpoint": "dashboard", "args": {}, "tone": "warn" if run["needs_approval"] else "info"})
    if health["to_review"]:
        out.append({"text": f"{fmt.fa(health['to_review'])} تطبیق آگهی منتظر تأیید است.", "endpoint": "market_review",
                    "args": {}, "tone": "info"})
    if health["obs_review"]:
        out.append({"text": f"{fmt.fa(health['obs_review'])} قیمت رقیب منتظر بررسی است.",
                    "endpoint": "market_observations", "args": {}, "tone": "info"})
    if advisor and advisor.get("n_changes"):
        out.append({"text": f"مشاور قیمت برای {fmt.fa(advisor['n_changes'])} کالا تغییر قیمت پایه پیشنهاد داده است.",
                    "endpoint": "sales_advisor", "args": {}, "tone": "info"})
    if health["broken"]:
        out.append({"text": f"{fmt.fa(health['broken'])} منبع قیمت دیگر سالم نیست.", "endpoint": "market_sources",
                    "args": {}, "tone": "crit"})
    if followups["inquiries"] or followups["reorders"]:
        out.append({"text": f"{fmt.fa(len(followups['inquiries']))} استعلام موعد پیگیری دارد و "
                            f"{fmt.fa(len(followups['reorders']))} مشتری به زمان سفارش دوباره رسیده است.",
                    "endpoint": "sales_dashboard", "args": {}, "tone": "info"})
    return out


def _insights(market, drops) -> list[str]:
    out = []
    s = market["summary"]
    w, r = s["wholesale"], s["retail"]
    if w["n"]:
        out.append(f"عمده با عمده: قیمت هر عدد برنا در {fmt.fa(w['cheaper'])} کالا ارزان‌تر، در {fmt.fa(w['similar'])} "
                   f"کالا هم‌قیمت (کمتر از ۲٪ فاصله) و در {fmt.fa(w['pricier'])} کالا گران‌تر از میانه عمده‌فروش‌هاست؛ "
                   f"میانه: {gap_words(w['median'])}.")
    if r["n"]:
        out.append(f"تکی با تکی: قیمت هر عدد برنا در {fmt.fa(r['cheaper'])} کالا ارزان‌تر، در {fmt.fa(r['similar'])} "
                   f"کالا هم‌قیمت و در {fmt.fa(r['pricier'])} کالا گران‌تر از میانه قیمت تکی فروشنده‌هاست؛ میانه: "
                   f"{gap_words(r['median'])}.")
    for side, label in (("wholesale", "عمده"), ("retail", "تکی")):
        ranked = sorted((x for x in market["rows"] if x["cmp"][side]["gap"] is not None),
                        key=lambda x: x["cmp"][side]["gap"])
        if ranked and ranked[-1]["cmp"][side]["gap"] > BAND:
            top = ranked[-1]
            out.append(f"بیشترین گرانی در {label}: {top['key']} با {gap_words(top['cmp'][side]['gap'])} از میانه "
                       f"{fmt.fa(top['cmp'][side]['n_priced'])} قیمت {label}.")
        if ranked and ranked[0]["cmp"][side]["gap"] < -BAND:
            low = ranked[0]
            out.append(f"بیشترین ارزانی در {label}: {low['key']} با {gap_words(low['cmp'][side]['gap'])}؛ "
                       "جای بررسی افزایش قیمت دارد.")
    if drops:
        out.append(f"{fmt.fa(len(drops))} آگهی رقیب در ۳۰ روز گذشته بیش از ۱۰٪ ارزان‌تر شده است (بخش هشدارها).")
    if market["n_prices"]:
        out.append(f"این تحلیل بر پایه {fmt.fa(market['n_prices'])} قیمت تأییدشده از {fmt.fa(market['n_sellers'])} "
                   f"فروشنده در {fmt.fa(market['stale_days'])} روز اخیر است؛ همه قیمت‌ها برای یک عدد است.")
    return out


def build(svc, ms, sales, days: int) -> dict:
    days = days if days in PERIODS else 90
    now = now_utc()
    st = svc.state()
    with svc.db() as c:
        market = _market(ms, c, now)
        leads = _leads(c, now)
        health = _health(c, now)
        drops = _drops(c, now)
        trends = _trends(ms, c, now, days, market)
        sales_d = _sales(c, tehran_date(now), days)
    fx = _fx(svc, st, now, days)
    k = sales.kpis(days)
    reasons = [{"label": LOST_REASONS.get(code, code), "n": n} for code, n in k["reasons"]]
    peak = max([x["n"] for x in reasons] + [1])
    for x in reasons:
        x["w"] = x["n"] / peak * 100
    weekly = ms.reports(1, "weekly")
    advisor_rep = ms.reports(1, "advisor")
    advisor = ms.report(advisor_rep[0]["id"])["data"] if advisor_rep else None
    margins = _margins(sales, market["official"], fx["dollar"] or svc.cfg.pricing.base_rate_toman, ms.cfg.advisor.min_margin)
    w, rt = market["summary"]["wholesale"], market["summary"]["retail"]
    pub = st["publication"]
    margin_mid = _median([x["margin"] for x in margins])
    min_margin = ms.cfg.advisor.min_margin
    low_mid = margin_mid is not None and min_margin is not None and margin_mid < float(min_margin)
    kpis = [
        {"label": "دلار آزاد", "value": fmt.money(fx["dollar"]) if fx["dollar"] else "—", "unit": "تومان",
         "delta": fmt.pct(fx["week_change"], 1) if fx["week_change"] is not None else None, "delta_note": "در ۷ روز",
         "tone": "neutral", "endpoint": "rates", "args": {}},
        {"label": "لیست رسمی", "value": ("معتبر" if pub and pub.get("is_valid") else "منقضی") if pub else "—",
         "unit": "", "delta": fx_words(fx["list_gap"]),
         "delta_note": "", "tone": "neutral", "endpoint": "pricelist_page", "args": {}},
        {"label": "عمده با عمده (قیمت هر عدد)", "value": gap_words(w["median"]), "unit": "",
         "delta": f"ارزان‌تر در {fmt.fa(w['cheaper'])}، گران‌تر در {fmt.fa(w['pricier'])} کالا" if w["n"] else None,
         "delta_note": "", "tone": "neutral", "endpoint": None, "anchor": "analysis", "args": {}},
        {"label": "تکی با تکی", "value": gap_words(rt["median"]), "unit": "",
         "delta": f"ارزان‌تر در {fmt.fa(rt['cheaper'])}، گران‌تر در {fmt.fa(rt['pricier'])} کالا" if rt["n"] else None,
         "delta_note": "", "tone": "neutral", "endpoint": None, "anchor": "analysis", "args": {}},
        {"label": "حاشیه سود (میانه)", "value": fmt.pct(margin_mid, 1, signed=False) if margin_mid is not None else "—",
         "unit": "", "delta": (f"زیر کف سود {fmt.pct(min_margin, 0, signed=False)}" if low_mid else
                               f"{fmt.fa(len(margins))} کالا با بهای ثبت‌شده" if margins else "بهای تمام‌شده ثبت نشده"),
         "delta_note": f"؛ {fmt.fa(len(margins))} کالا" if low_mid else "",
         "tone": "bad" if low_mid else "neutral",
         "endpoint": "sales_advisor", "args": {}},
        {"label": f"فروش {fmt.fa(days)} روز", "value": compact_toman(sales_d["total"]), "unit": "تومان",
         "delta": fmt.pct(sales_d["change"], 1) if sales_d["change"] is not None else None,
         "delta_note": "نسبت به دوره قبل",
         "tone": "good" if (sales_d["change"] or 0) > 0 else ("bad" if (sales_d["change"] or 0) < 0 else "neutral"),
         "endpoint": "sales_dashboard", "args": {"days": days}},
        {"label": "تبدیل استعلام", "value": fmt.pct(k["conversion"], 0, signed=False) if k["conversion"] is not None else "—",
         "unit": "", "delta": f"{fmt.fa(k['open'])} استعلام باز", "delta_note": "", "tone": "neutral",
         "endpoint": "sales_inquiries", "args": {"status": "open"}},
        {"label": "کسب‌وکارها", "value": fmt.money(leads["total"]), "unit": "",
         "delta": f"{fmt.fa(leads['new'])} تازه در ۷ روز", "delta_note": "", "tone": "neutral",
         "endpoint": "sales_leads", "args": {}},
        {"label": "قیمت‌های تازه بازار", "value": fmt.money(health["fresh"]), "unit": "",
         "delta": "در ۷ روز اخیر", "delta_note": "", "tone": "neutral", "endpoint": "market_agents", "args": {}},
    ]
    quick = [{"label": "لیست قیمت رسمی (چاپ)", "endpoint": "pricelist_page", "args": {}},
             {"label": "لیست قیمت رسمی (Excel)", "endpoint": "pricelist_xlsx", "args": {}},
             {"label": "Excel کسب‌وکارها", "endpoint": "sales_leads_xlsx", "args": {}},
             {"label": "بازار و رقبا", "endpoint": "market_overview", "args": {}},
             {"label": "تاریخچه نرخ دلار", "endpoint": "rates", "args": {}}]
    if weekly:
        quick.insert(2, {"label": "آخرین گزارش هفتگی بازار (Excel)", "endpoint": "market_report_xlsx",
                         "args": {"report_id": weekly[0]["id"]}})
    if advisor_rep:
        quick.insert(3, {"label": "پیشنهاد مشاور قیمت (Excel)", "endpoint": "sales_advisor_xlsx",
                         "args": {"report_id": advisor_rep[0]["id"]}})
    return {"now": now, "days": days, "periods": PERIODS, "kpis": kpis, "leads": leads, "market": market,
            "insights": _insights(market, drops), "drops": drops, "trends": trends, "fx": fx, "sales": sales_d,
            "reasons": reasons, "lost": k["lost"], "margins": margins, "has_costs": bool(sales.costs()),
            "decisions": _decisions(st, health, advisor, sales.followups()), "quick": quick, "health": health,
            "fx_legend": [{"name": n, "color": col} for n, col in zip(("دلار آزاد", "نرخ لیست رسمی برنا"), FX_COLORS)]}
