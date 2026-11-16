from __future__ import annotations

import statistics

from .. import fmt

SIDES = ("wholesale", "retail")
SIDE_LABELS = {"wholesale": "عمده", "retail": "تکی"}
SIDE_TITLES = {"wholesale": "عمده با عمده", "retail": "تکی با تکی"}
BAND = 0.02
AXIS_STEPS = (0.1, 0.2, 0.3, 0.5, 0.75, 1.0)
PARTS = (("cheaper", "برنا ارزان‌تر"), ("similar", "هم‌قیمت"), ("pricier", "برنا گران‌تر"))
BULK_PACK = 10


def side(level: str | None, pack_qty=1) -> str:
    return "wholesale" if level == "wholesale" or (pack_qty or 1) >= BULK_PACK else "retail"


def obs_side(o) -> str:
    return side(o["price_level"], o["pack_qty"] if "pack_qty" in o.keys() else 1)


def priced(r: dict) -> bool:
    o = r["obs"]
    return o["kind"] == "price" and o["availability"] != "out_of_stock" and bool(o["price_rial"])


def median(values):
    return int(statistics.median(values)) if values else None


def gap(mine, theirs):
    return mine / theirs - 1 if mine and theirs else None


def values(rows: list[dict], s: str) -> list[int]:
    return sorted(r["obs"]["price_rial"] for r in rows if priced(r) and obs_side(r["obs"]) == s)


def side_stats(rows: list[dict]) -> dict:
    out = {}
    for s in SIDES:
        sel = [r for r in rows if obs_side(r["obs"]) == s]
        vals = values(sel, s)
        out[s] = {"n": len(sel), "n_priced": len(vals), "min": vals[0] if vals else None, "median": median(vals),
                  "max": vals[-1] if vals else None,
                  "oos": sum(1 for r in sel if r["obs"]["availability"] == "out_of_stock")}
    return out


def compare(official, comp_rows: list[dict], min_prices: int) -> dict:
    st = side_stats(comp_rows)
    out = {}
    for s in SIDES:
        x = dict(st[s])
        x["borna"] = official
        x["gap"] = gap(official, x["median"])
        x["low_data"] = 0 < x["n_priced"] < min_prices
        x["label"] = SIDE_LABELS[s]
        out[s] = x
    return out


def summary(items: list[dict], s: str) -> dict:
    gaps = [i["cmp"][s]["gap"] for i in items if i["cmp"][s]["gap"] is not None]
    out = {"side": s, "label": SIDE_LABELS[s], "title": SIDE_TITLES[s], "n": len(gaps),
           "cheaper": sum(1 for g in gaps if g < -BAND), "similar": sum(1 for g in gaps if abs(g) <= BAND),
           "pricier": sum(1 for g in gaps if g > BAND),
           "median": statistics.median(gaps) if gaps else None,
           "no_borna": sum(1 for i in items if i["cmp"][s]["median"] and not i["cmp"][s]["borna"]),
           "no_market": sum(1 for i in items if i["cmp"][s]["borna"] and not i["cmp"][s]["median"])}
    total = max(1, len(gaps))
    out["parts"] = [{"key": k, "label": label, "n": out[k], "pct": out[k] / total * 100} for k, label in PARTS]
    return out


def nice_axis(gaps, floor: float = 0.1) -> float:
    top = max([abs(g) for g in gaps if g is not None] + [floor])
    return next((step for step in AXIS_STEPS if top <= step), 1.0)


def chart(items: list[dict], s: str) -> dict:
    rows = [i for i in items if i["cmp"][s]["gap"] is not None]
    rows.sort(key=lambda i: (-i["cmp"][s]["gap"], i["key"]))
    axis = nice_axis([i["cmp"][s]["gap"] for i in rows])
    bars = []
    for i in rows:
        c = i["cmp"][s]
        bars.append({"key": i["key"], "name": i["name"], "gap": c["gap"], "borna": c["borna"], "median": c["median"],
                     "n": c["n_priced"], "low_data": c["low_data"], "min": c["min"], "max": c["max"],
                     "w": min(abs(c["gap"]), axis) / axis * 50, "beyond": abs(c["gap"]) > axis})
    return {"side": s, "label": SIDE_LABELS[s], "title": SIDE_TITLES[s], "axis": axis, "band": BAND / axis * 50,
            "rows": bars, "ticks": [(tick_label(v), pos) for v, pos in
                                    ((-axis, 0), (-axis / 2, 25), (0, 50), (axis / 2, 75), (axis, 100))]}


def tick_label(v: float) -> str:
    if abs(v) < 1e-9:
        return "۰"
    return ("+" if v > 0 else "−") + fmt.fa(str(round(abs(v) * 100))) + "٪"


COLORS = {"borna": "#17202b", "wholesale": "#2a78d6", "retail": "#eb6834"}
SERIES_NAMES = {"wholesale": ("قیمت هر عدد برنا (لیست رسمی)", "میانه عمده رقبا"),
                "retail": ("قیمت هر عدد برنا (لیست رسمی)", "میانه تکی رقبا")}


def lanes(s: str, borna_points, market_points) -> list[dict]:
    mine, theirs = SERIES_NAMES[s]
    out = [{"name": mine, "color": COLORS["borna"], "step": True, "points": list(borna_points)},
           {"name": theirs, "color": COLORS[s], "step": False, "points": list(market_points)}]
    return [x for x in out if x["points"]]
