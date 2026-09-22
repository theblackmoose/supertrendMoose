"""Indicators matching TradingView's implementations.

ATR and ADX use Wilder's smoothing (RMA); supertrend() reproduces
ta.supertrend()'s band-ratcheting logic and direction convention
(-1 = uptrend, +1 = downtrend).
"""
from __future__ import annotations

import os
from collections import OrderedDict
from datetime import date, timedelta

import numpy as np
import pandas as pd


def wilder(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, length: int = 14, tr: pd.Series | None = None) -> pd.Series:
    return wilder(true_range(df) if tr is None else tr, length)


def adx(df: pd.DataFrame, di_len: int = 14, adx_len: int = 14,
        tr: pd.Series | None = None) -> pd.DataFrame:
    """Returns a frame with plus_di, minus_di and adx columns."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )

    tr_smooth = wilder(true_range(df) if tr is None else tr, di_len).replace(0, np.nan)
    plus_di = 100 * wilder(plus_dm, di_len) / tr_smooth
    minus_di = 100 * wilder(minus_dm, di_len) / tr_smooth

    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return pd.DataFrame(
        {"plus_di": plus_di, "minus_di": minus_di, "adx": wilder(dx.fillna(0), adx_len)}
    )


def supertrend(df: pd.DataFrame, length: int = 10, mult: float = 3.0,
               tr: pd.Series | None = None) -> pd.DataFrame:
    """Returns a frame with the supertrend line and direction (-1 up, +1 down)."""
    a = atr(df, length, tr)
    hl2 = (df["high"] + df["low"]) / 2
    upper_basic = (hl2 + mult * a).to_numpy(dtype=float)
    lower_basic = (hl2 - mult * a).to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)

    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    direction = np.ones(n)
    line = np.full(n, np.nan)

    if not a.notna().any():
        return pd.DataFrame({"supertrend": line, "direction": np.nan}, index=df.index)

    start = int(a.notna().to_numpy().argmax())
    upper[start] = upper_basic[start]
    lower[start] = lower_basic[start]

    for i in range(start + 1, n):
        lower[i] = (
            lower_basic[i]
            if (lower_basic[i] > lower[i - 1] or close[i - 1] < lower[i - 1])
            else lower[i - 1]
        )
        upper[i] = (
            upper_basic[i]
            if (upper_basic[i] < upper[i - 1] or close[i - 1] > upper[i - 1])
            else upper[i - 1]
        )
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] > upper[i - 1] else 1
        else:
            direction[i] = 1 if close[i] < lower[i - 1] else -1
        line[i] = lower[i] if direction[i] == -1 else upper[i]

    direction[: start + 1] = np.nan
    return pd.DataFrame({"supertrend": line, "direction": direction}, index=df.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram, as TradingView's ta.macd().

    Display only: nothing in the scanner, backtest or tuning reads it, which is
    why compute_all() does not attach it. The EMAs are seeded from the first
    close rather than an SMA; the difference is gone well before the warm-up
    that the chart hides anyway, and the first slow + signal bars are blanked
    so an unsettled value is never drawn.
    """
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    line = fast_ema - slow_ema
    sig = line.ewm(span=signal, adjust=False).mean()
    out = pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})
    out.iloc[: min(len(out), slow + signal - 2)] = np.nan
    return out


def _market_closed(d) -> bool:
    """True on the NYSE full-day holidays that can fall on a Friday.

    Only Fridays matter here: they decide whether a week that ends early has
    already closed. Thanksgiving Friday is a trading day, and a Saturday New
    Year's Day does not close the Friday before it.
    """
    from dateutil.easter import easter

    if d == easter(d.year) - timedelta(days=2):          # Good Friday
        return True
    for month, day, since in ((1, 1, 0), (6, 19, 2022), (7, 4, 0), (12, 25, 0)):
        if d.year < since:
            continue
        fixed = date(d.year, month, day)
        if d == fixed:
            return True
        # Saturday holidays are observed on the Friday, except New Year's Day.
        if fixed.weekday() == 5 and (month, day) != (1, 1) and d == fixed - timedelta(days=1):
            return True
    return False


def trading_days_until(start: date, end: date) -> int:
    """Trading days after start up to and including end (weekends and the
    Friday holidays above excluded; other holidays may make it one high)."""
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5 and not _market_closed(d):
            n += 1
        d += timedelta(days=1)
    return n


def _week_finished(last_bar: date) -> bool:
    """Has the week containing last_bar closed, given it is the latest bar?"""
    friday = last_bar + timedelta(days=4 - last_bar.weekday())
    d = last_bar + timedelta(days=1)
    while d <= friday:
        if not _market_closed(d):
            return False
        d += timedelta(days=1)
    return True


def macd_weekly(close: pd.Series, fast: int = 12, slow: int = 26,
                signal: int = 9) -> pd.DataFrame:
    """Weekly MACD laid onto daily bars, like TradingView's "1 week" timeframe
    with "Wait for timeframe closes" ticked.

    Each week's value appears on that week's last trading day and holds until
    the next week closes. A week still in progress is left out, so the latest
    days show the last completed week and never a value that could change.
    week_end marks the bars a week closed on, so the chart can plot one point
    per week, as TradingView does. Display only, like macd().
    """
    cols = ["macd", "signal", "hist"]
    if close.empty:
        out = pd.DataFrame(columns=cols, index=close.index, dtype=float)
        out["week_end"] = pd.Series(dtype=bool)
        return out
    weeks = close.index.to_period("W-FRI")
    wclose = close.groupby(weeks).last()
    ends = pd.Series(close.index, index=close.index).groupby(weeks).last()
    if not _week_finished(close.index[-1].date()):
        wclose, ends = wclose.iloc[:-1], ends.iloc[:-1]

    wm = macd(wclose, fast, slow, signal)
    out = pd.DataFrame(np.nan, index=close.index, columns=cols)
    out.loc[ends.to_numpy(), cols] = wm[cols].to_numpy()
    out = out.ffill()
    out["week_end"] = out.index.isin(ends.to_numpy())
    return out


def compute_all(
    df: pd.DataFrame,
    atr_len: int = 10,
    atr_mult: float = 3.0,
    sma_len: int = 200,
    di_len: int = 14,
    adx_len: int = 14,
) -> pd.DataFrame:
    """Attach every indicator the scanner and the chart need."""
    out = df.copy()
    # True range was being recomputed three times per call - once for the ATR,
    # once inside supertrend, once inside ADX. It only depends on the bars.
    tr = true_range(out)
    st = supertrend(out, atr_len, atr_mult, tr=tr)
    dmi = adx(out, di_len, adx_len, tr=tr)
    out["supertrend"] = st["supertrend"]
    out["direction"] = st["direction"]
    out["adx"] = dmi["adx"]
    out["plus_di"] = dmi["plus_di"]
    out["minus_di"] = dmi["minus_di"]
    out["sma"] = out["close"].rolling(sma_len).mean()
    out["atr"] = atr(out, atr_len, tr)
    out["atr_pct"] = out["atr"] / out["close"] * 100
    out["avg_volume"] = out["volume"].rolling(20).mean()
    return out


# ── memoisation ───────────────────────────────────────────────────────
# compute_all is pure for a given frame and parameter set, and the dashboard
# asks for the same combinations repeatedly (chart, states and backtest all
# want the same indicators). Keyed on the data's own fingerprint so a new bar
# invalidates it automatically.
_CACHE: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
# Each entry is a full indicator frame - roughly 125 KB for five years of daily
# bars - so the ceiling is a memory budget, not just a count.
CACHE_MAX = int(os.getenv("INDICATOR_CACHE_MAX", "").strip() or "200")


def cache_clear() -> None:
    _CACHE.clear()


def cache_info() -> dict:
    return {"entries": len(_CACHE), "max": CACHE_MAX}


def compute_cached(
    key: str,
    df: pd.DataFrame,
    atr_len: int = 10,
    atr_mult: float = 3.0,
    sma_len: int = 200,
    di_len: int = 14,
    adx_len: int = 14,
) -> pd.DataFrame:
    """compute_all with memoisation. Treat the result as read-only."""
    if df.empty:
        return compute_all(df, atr_len, atr_mult, sma_len, di_len, adx_len)

    fingerprint = (key, atr_len, float(atr_mult), sma_len, di_len, adx_len,
                   len(df), int(df.index[-1].value), float(df["close"].iloc[-1]))
    hit = _CACHE.get(fingerprint)
    if hit is not None:
        _CACHE.move_to_end(fingerprint)
        return hit

    out = compute_all(df, atr_len, atr_mult, sma_len, di_len, adx_len)
    _CACHE[fingerprint] = out
    while len(_CACHE) > CACHE_MAX:
        _CACHE.popitem(last=False)
    return out
