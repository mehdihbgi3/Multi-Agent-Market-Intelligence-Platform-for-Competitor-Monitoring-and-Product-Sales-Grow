from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

ONE = Decimal(1)
DAYS = Decimal(365)


@dataclass(frozen=True)
class CheckQuote:
    cash_rial: int
    days: int
    annual_rate: Decimal
    annual_dollar: Decimal | None
    rate_factor: Decimal
    min_check_rial: int
    dollar_factor: Decimal | None
    dollar_safe_rial: int | None
    offered_rial: int | None
    offered_pv_rial: int | None
    offered_vs_cash: Decimal | None
    offered_dollar_loss: Decimal | None


def _grow(rate: Decimal, days: int) -> Decimal:
    return (ONE + rate) ** (Decimal(days) / DAYS)


def _ceil(x: Decimal, step: int = 10_000) -> int:
    return int((x / step).to_integral_value(rounding=ROUND_CEILING) * step)


def quote(cash_rial: int, days: int, annual_rate: Decimal, annual_dollar: Decimal | None = None,
          offered_rial: int | None = None) -> CheckQuote:
    if cash_rial <= 0 or days < 0:
        raise ValueError("cash and days must be positive")
    f = _grow(annual_rate, days)
    df = _grow(annual_dollar, days) if annual_dollar is not None else None
    pv = vs = loss = None
    if offered_rial:
        pv_d = Decimal(offered_rial) / f
        pv = int(pv_d)
        vs = pv_d / Decimal(cash_rial) - ONE
        if df is not None:
            loss = Decimal(offered_rial) / (Decimal(cash_rial) * df) - ONE
    return CheckQuote(cash_rial=cash_rial, days=days, annual_rate=annual_rate, annual_dollar=annual_dollar,
                      rate_factor=f, min_check_rial=_ceil(Decimal(cash_rial) * f),
                      dollar_factor=df, dollar_safe_rial=_ceil(Decimal(cash_rial) * df) if df is not None else None,
                      offered_rial=offered_rial, offered_pv_rial=pv, offered_vs_cash=vs, offered_dollar_loss=loss)
