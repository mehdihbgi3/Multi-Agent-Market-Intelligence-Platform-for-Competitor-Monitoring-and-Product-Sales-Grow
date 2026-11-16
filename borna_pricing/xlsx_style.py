from __future__ import annotations

import math
from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

FONT = "Tahoma"
SIZE = 10
INK = "17202B"
MUTED = "4A5665"
FAINT = "7D8895"
HEAD_FILL = PatternFill("solid", fgColor=INK)
SUB_FILL = PatternFill("solid", fgColor="E6EBF0")
ZEBRA = PatternFill("solid", fgColor="F3F6F8")
EDGE = Side(style="thin", color="9AA6B2")
BORDER = Border(left=EDGE, right=EDGE, top=EDGE, bottom=EDGE)
MONEY = "#,##0"
PCT = "+0.0%;-0.0%;0.0%"
TEXT = "@"
LINE_PT = 15.0
PAD_PT = 5.0
SIZES = {"title": 14, "sub": 10.5, "note": 10, "small": 9}
COLORS = {"title": INK, "sub": INK, "note": MUTED, "small": FAINT}


def book() -> Workbook:
    return Workbook()


def sheet(wb: Workbook, title: str, *, first: bool = False, tab: str | None = None):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title[:31]
    ws.sheet_view.rightToLeft = True
    ws.sheet_view.showGridLines = True
    if tab:
        ws.sheet_properties.tabColor = tab
    return ws


def _al(horizontal="right", wrap=True, vertical="center") -> Alignment:
    return Alignment(horizontal=horizontal, vertical=vertical, wrap_text=wrap, readingOrder=2)


def shown(value, number_format: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        if number_format == PCT:
            return f"{value * 100:+.1f}%"
        if number_format == MONEY:
            return f"{round(value):,}"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:16]
    return str(value)


def text_width(text: str, size: float = SIZE, bold: bool = False) -> float:
    if not text:
        return 0.0
    w = 0.0
    for ch in text:
        if ch in " ‌":
            w += 0.55
        elif ch.isdigit() or ch in ",.+-%/:":
            w += 1.0
        elif "؀" <= ch <= "ۿ" or "ﭐ" <= ch <= "﻿":
            w += 1.05
        elif ch.isupper():
            w += 1.25
        else:
            w += 1.0
    return w * (size / SIZE) * (1.08 if bold else 1.0)


def lines_needed(text: str, width: float, size: float = SIZE, bold: bool = False) -> int:
    if not text:
        return 1
    usable = max(width - 1.6, 1.0)
    total = 0
    for part in str(text).split("\n"):
        words = part.split(" ")
        line = 0.0
        n = 1
        for word in words:
            ww = text_width(word, size, bold)
            if ww > usable:
                if line:
                    n += 1
                n += math.ceil(ww / usable) - 1
                line = ww % usable
                continue
            step = ww + (text_width(" ", size) if line else 0)
            if line + step > usable:
                n += 1
                line = ww
            else:
                line += step
        total += n
    return total


def height_for(lines: int, size: float = SIZE) -> float:
    return round(lines * LINE_PT * (size / SIZE) + PAD_PT, 1)


def title_rows(ws, lines: list[tuple[str, str]], ncols: int, widths: list[float], start: int = 1) -> int:
    total = sum(widths[:ncols]) or 60
    r = start
    for text, kind in lines:
        if text:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=max(ncols, 1))
            c = ws.cell(row=r, column=1, value=text)
            size = SIZES.get(kind, SIZE)
            bold = kind in ("title", "sub")
            c.font = Font(name=FONT, size=size, bold=bold, color=COLORS.get(kind, INK))
            c.alignment = _al("right", True, "center")
            ws.row_dimensions[r].height = height_for(lines_needed(text, total, size, bold), size)
        r += 1
    return r


def fit_widths(headers: list[str], rows: list[list], formats: dict[int, str], fixed: dict[int, float],
               min_w: float, max_w: float) -> list[float]:
    out = []
    for j, h in enumerate(headers):
        if j in fixed:
            out.append(fixed[j])
            continue
        longest_word = max((text_width(w, SIZE, True) for w in str(h).split()), default=4)
        data = max((text_width(shown(r[j], formats.get(j))) for r in rows if j < len(r)), default=0)
        whole_head = text_width(str(h), SIZE, True)
        want = max(longest_word, min(data, max_w), min(whole_head, 18)) + 2.2
        out.append(round(min(max(want, min_w), max_w), 1))
    return out


def table(ws, top: int, headers: list[str], rows: list[list], *, formats: dict[int, str] | None = None,
          widths: list[float] | None = None, fixed: dict[int, float] | None = None, min_w: float = 7,
          max_w: float = 48, bold_cols: tuple = (), center_cols: tuple = (), zebra: bool = True,
          fills: dict[tuple[int, int], PatternFill] | None = None, links: dict[tuple[int, int], str] | None = None,
          freeze_cols: int = 0, autofilter: bool = True) -> dict:
    formats = formats or {}
    fills = fills or {}
    widths = widths or fit_widths(headers, rows, formats, fixed or {}, min_w, max_w)
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    head_lines = 1
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=top, column=j, value=h)
        c.font = Font(name=FONT, size=SIZE, bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
        c.alignment = _al("center", True, "center")
        c.border = BORDER
        head_lines = max(head_lines, lines_needed(str(h), widths[j - 1], SIZE, True))
    ws.row_dimensions[top].height = height_for(head_lines)
    for i, row in enumerate(rows):
        r = top + 1 + i
        n_lines = 1
        for j in range(len(headers)):
            v = row[j] if j < len(row) else None
            c = ws.cell(row=r, column=j + 1, value=v)
            nf = formats.get(j)
            if nf:
                c.number_format = nf
            numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
            c.font = Font(name=FONT, size=SIZE, bold=j in bold_cols)
            c.border = BORDER
            if (i, j) in fills:
                c.fill = fills[(i, j)]
            elif zebra and i % 2:
                c.fill = ZEBRA
            if numeric:
                c.alignment = _al("center" if j in center_cols else "left", False, "center")
            else:
                c.alignment = _al("center" if j in center_cols else "right", True, "center")
                n_lines = max(n_lines, lines_needed(shown(v), widths[j], SIZE, j in bold_cols))
        ws.row_dimensions[r].height = height_for(n_lines)
    last = top + len(rows)
    if autofilter and rows:
        ws.auto_filter.ref = f"A{top}:{get_column_letter(len(headers))}{last}"
    ws.freeze_panes = ws.cell(row=top + 1, column=freeze_cols + 1)
    ws.print_title_rows = f"{top}:{top}"
    return {"top": top, "last": last, "widths": widths}


def note_rows(ws, start: int, lines: list[tuple[str, str]], ncols: int, widths: list[float]) -> int:
    return title_rows(ws, lines, ncols, widths, start)


def help_sheet(wb: Workbook, title: str, lines: list[tuple[str, str]], width: float = 100) -> object:
    ws = sheet(wb, title)
    ws.column_dimensions["A"].width = width
    r = 1
    for text, kind in lines:
        if text:
            c = ws.cell(row=r, column=1, value=text)
            size = SIZES.get(kind, SIZE)
            bold = kind in ("title", "sub")
            c.font = Font(name=FONT, size=size, bold=bold, color=COLORS.get(kind, INK))
            c.alignment = _al("right", True, "center")
            ws.row_dimensions[r].height = height_for(lines_needed(text, width, size, bold), size)
        r += 1
    page(ws, landscape=False)
    return ws


def page(ws, landscape: bool = False) -> None:
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5)


def save(wb: Workbook) -> bytes:
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
