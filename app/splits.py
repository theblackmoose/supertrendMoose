"""Spotting a stock split against a position you recorded.

Yahoo's history is split-adjusted backwards: after a 4-for-1 split every
older close is divided by four. The price you typed when you bought is not,
so the two drift apart by exactly the split factor and every dollar figure
for that holding is wrong until it is corrected.

That is what this finds: compare what you paid with what the stored history
now says that day closed at. A ratio near a whole number is a split; near its
reciprocal, a reverse split. Ordinary price moves do not land on 2.0, 4.0 or
10.0 within a few percent on the very day you bought.
"""
from __future__ import annotations

from datetime import date, timedelta

# Ratios worth claiming. Anything else is left alone rather than guessed at.
FACTORS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 30)
TOLERANCE = 0.04          # 4%: a split is exact, so this only absorbs the
                          # difference between your fill and that day's close
LOOKBACK_DAYS = 7         # a purchase dated on a holiday still finds a close


def _close_near(closes: dict, day: date) -> tuple[date, float] | None:
    """The stored close on that day, or the last one before it."""
    for back in range(LOOKBACK_DAYS + 1):
        d = day - timedelta(days=back)
        if d in closes:
            return d, closes[d]
    return None


def detect(entry_price: float, entry_date: date, quantity: float | None,
           closes: dict) -> dict | None:
    """A split between the purchase and now, or None.

    Returns what to change the record to: after a 4-for-1 split you paid a
    quarter as much per share and hold four times as many.
    """
    if not entry_price or not closes:
        return None
    found = _close_near(closes, entry_date)
    if not found:
        return None
    day, close = found
    if close <= 0:
        return None

    ratio = entry_price / close
    for f in FACTORS:
        if abs(ratio - f) <= f * TOLERANCE:
            return {
                "factor": f, "reverse": False, "ratio": round(ratio, 3),
                "close_on_day": round(close, 4), "close_date": day.isoformat(),
                "suggested_entry": round(entry_price / f, 4),
                "suggested_quantity": round(quantity * f, 6) if quantity else None,
                "note": f"{f}-for-1 split",
            }
        if abs(ratio - 1 / f) <= (1 / f) * TOLERANCE:
            return {
                "factor": f, "reverse": True, "ratio": round(ratio, 3),
                "close_on_day": round(close, 4), "close_date": day.isoformat(),
                "suggested_entry": round(entry_price * f, 4),
                "suggested_quantity": round(quantity / f, 6) if quantity else None,
                "note": f"1-for-{f} reverse split",
            }
    return None


def apply_factor(entry_price: float, quantity: float | None,
                 factor: int, reverse: bool) -> tuple[float, float | None]:
    """The corrected purchase: same money, restated in post-split shares."""
    if reverse:
        return round(entry_price * factor, 4), (round(quantity / factor, 6) if quantity else None)
    return round(entry_price / factor, 4), (round(quantity * factor, 6) if quantity else None)
