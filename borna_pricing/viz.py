from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from markupsafe import Markup, escape

from . import fmt
from .charts import _nice_step, _tick_label, _time_ticks


def axis_toman(v: float) -> str:
    return fmt.money(round(v))


def _value_at(points: list[tuple[datetime, float]], t: datetime):
    best = None
    for pt, v in points:
        if pt > t:
            break
        best = v
    return best


def time_lines(series: list[dict], *, value_fmt, axis_fmt, unit: str, start: datetime | None = None,
               end: datetime | None = None, width: int = 920, height: int = 260, compact: bool = False) -> dict | None:
    pts = [(t, float(v)) for s in series for t, v in s["points"]]
    if not pts:
        return None
    t0 = min([t for t, _ in pts] + ([start] if start else []))
    t1 = max([t for t, _ in pts] + ([end] if end else []))
    if t1 <= t0:
        t0, t1 = t0 - timedelta(hours=12), t1 + timedelta(hours=12)
    ys = [v for _, v in pts]
    lo, hi = min(ys), max(ys)
    pad = max((hi - lo) * 0.12, abs(hi) * 0.01, 1)
    lo, hi = lo - pad, hi + pad
    step = _nice_step(hi - lo, 3 if compact else 4)
    lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step
    pad_l, pad_r, pad_t, pad_b = (76, 14, 12, 30) if compact else (88, 18, 16, 32)
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    span = (t1 - t0).total_seconds()

    def X(t):
        return pad_l + (t - t0).total_seconds() / span * pw

    def Y(v):
        return pad_t + (hi - v) / (hi - lo) * ph

    o = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg mchart-svg{" compact" if compact else ""}" role="img" '
         f'direction="ltr" aria-label="{escape(", ".join(s["name"] for s in series))}">']
    v = lo
    while v <= hi + step / 1000:
        y = Y(v)
        o.append(f'<line x1="{pad_l}" x2="{pad_l + pw}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        o.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{escape(axis_fmt(v))}</text>')
        v += step
    ticks, kind = _time_ticks(t0, t1)
    if compact and len(ticks) > 4:
        ticks = ticks[::2]
    o.append(f'<line x1="{pad_l}" x2="{pad_l + pw}" y1="{pad_t + ph}" y2="{pad_t + ph}" class="axis"/>')
    for t in ticks:
        x = X(t)
        if x < pad_l + 18 or x > pad_l + pw - 18:
            continue
        o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{pad_t + ph}" y2="{pad_t + ph + 5}" class="axis"/>')
        o.append(f'<text x="{x:.1f}" y="{pad_t + ph + 20}" class="tick" text-anchor="middle">'
                 f'{escape(_tick_label(t, kind))}</text>')
    for s in series:
        p = [(X(t), Y(float(v))) for t, v in s["points"]]
        if s.get("step"):
            d = [f"M{p[0][0]:.1f},{p[0][1]:.1f}"]
            for (_, _), (x1, y1) in zip(p, p[1:]):
                d.append(f"H{x1:.1f}V{y1:.1f}")
            d.append(f"H{X(t1):.1f}")
            last = (X(t1), p[-1][1])
        else:
            d = [f"M{p[0][0]:.1f},{p[0][1]:.1f}"] + [f"L{x:.1f},{y:.1f}" for x, y in p[1:]]
            last = p[-1]
        o.append(f'<path d="{" ".join(d)}" class="line" style="stroke:{s["color"]}"/>')
        o.append(f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="4" class="end" style="fill:{s["color"]}"/>')
    o.append(f'<line class="xhair" x1="0" x2="0" y1="{pad_t}" y2="{pad_t + ph}" visibility="hidden"/>')
    for s in series:
        o.append(f'<circle class="xdot" r="4.5" cx="0" cy="0" visibility="hidden" style="fill:{s["color"]}"/>')
    o.append(f'<rect class="hit" x="{pad_l}" y="{pad_t}" width="{pw}" height="{ph}"/>')
    o.append("</svg>")
    times = sorted({t for t, _ in pts if t0 <= t <= t1} | {t1})
    rows = []
    for t in times:
        vals = []
        for i, s in enumerate(series):
            val = _value_at(s["points"], t)
            if val is not None:
                vals.append({"i": i, "n": s["name"], "v": f"{value_fmt(val)} {unit}", "y": round(Y(float(val)), 1),
                             "c": s["color"]})
        if vals:
            rows.append({"x": round(X(t), 1), "t": fmt.jdatetime(t), "vals": vals})
    data = {"w": width, "h": height, "rows": rows}
    return {"svg": Markup("".join(o)),
            "data": Markup(json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))}
