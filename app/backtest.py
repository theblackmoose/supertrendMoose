"""Backtest engine.

Runs the same Supertrend signals through each filter profile so you can
see what the filter actually costs or earns on a given ticker.

Fills are at the NEXT bar's open after a signal, never the signal bar's
close, so results reflect a fill you could realistically have got.
Buy-and-hold over the identical window is included as a benchmark: a
strategy that loses to simply owning the stock is worth knowing about.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from .config import settings
from .indicators import compute_all

# Ordered most restrictive first. Measured across the exported grid, total
# trades taken were: full 3,550 < adx_rising 3,781 < adx_only 5,268 <
# regime 10,768 < none 13,316. Note that "rising" filters harder than the
# level test, which is not the obvious guess.
PROFILES = ("full", "adx_rising", "adx_only", "regime", "none")
LABELS = {
    "full": "200-day avg + trend strength",
    "adx_rising": "Trend strength rising",
    "adx_only": "Trend strength only",
    "regime": "Market regime (SPY)",
    "none": "Unfiltered",
}

# The market-regime profile ignores the individual stock entirely and asks one
# question: is the broad market in an uptrend? The per-stock filters hurt
# because they lag the stock itself; a market gate is a different hypothesis,
# namely that you should stand aside in broad downturns rather than judging
# each name on its own recent behaviour.
MARKET_TICKER = "SPY"

# ADX is low by construction at trend inception: it measures how established
# a trend is, and a trend that just started is not established. Demanding
# "ADX > threshold" on the flip bar therefore fights the entry it is meant to
# qualify. "Rising over the last few bars" asks whether direction is building
# instead of whether it has already built.
ADX_RISE_BARS = 5


def rising_series(adx: "pd.Series") -> "pd.Series":
    """True where ADX is above its own value ADX_RISE_BARS bars earlier."""
    return adx > adx.shift(ADX_RISE_BARS)


def passes(profile: str, above_sma: bool, adx_ok: bool, adx_rising: bool,
           market_ok: bool = True) -> bool:
    """The one definition of each filter profile.

    The chart markers and the backtest must agree, so both call this. A second
    copy of these rules elsewhere is how the chart ended up labelling filtered
    signals as taken.
    """
    if profile == "full":
        return above_sma and adx_ok
    if profile == "adx_only":
        return adx_ok
    if profile == "adx_rising":
        return adx_rising
    if profile == "regime":
        return market_ok
    return True


def blackout_mask(index: pd.DatetimeIndex, earnings: list, days: int = 5) -> "np.ndarray":
    """True on bars within `days` before a report date.

    The live scanner blocks entries in this window because a Supertrend stop
    cannot protect against an overnight gap. The backtest has to apply the same
    rule or it is measuring a different strategy from the one you run.
    """
    mask = np.zeros(len(index), dtype=bool)
    if not earnings:
        return mask
    idx = pd.DatetimeIndex(index)
    for d in earnings:
        lo = pd.Timestamp(d) - pd.Timedelta(days=days)
        mask |= (idx >= lo) & (idx <= pd.Timestamp(d))
    return mask


def market_ok_series(market: pd.DataFrame | None, index: pd.DatetimeIndex,
                     sma_len: int | None = None) -> "np.ndarray":
    """True on bars where the market index closed above its own long-term average.

    Missing market data means the gate passes rather than silently blocking
    every trade, which would look like a broken profile instead of absent data.
    """
    if market is None or market.empty:
        return np.ones(len(index), dtype=bool)
    length = sma_len or settings.sma_len
    above = market["close"] > market["close"].rolling(length).mean()
    aligned = above.reindex(pd.DatetimeIndex(index).union(above.index)).ffill()
    return aligned.reindex(pd.DatetimeIndex(index)).fillna(True).to_numpy(dtype=bool)


def simulate(ind: pd.DataFrame, profile: str, adx_min: float,
             cost_pct: float | None = None,
             blackout: "np.ndarray | None" = None,
             market: "np.ndarray | None" = None) -> list[dict]:
    """Long-only walk-forward. Returns a list of closed trades.

    cost_pct is charged on each side, so it covers commission and expected
    slippage together. Twenty trades at 0.05% a side is 2% of drag, which
    matters a great deal when per-trade expectancy is 1-3%.
    """
    cost = (settings.cost_pct_per_side if cost_pct is None else cost_pct) / 100.0
    o = ind["open"].to_numpy(dtype=float)
    c = ind["close"].to_numpy(dtype=float)
    d = ind["direction"].to_numpy(dtype=float)
    sma = ind["sma"].to_numpy(dtype=float)
    ax = ind["adx"].to_numpy(dtype=float)
    rising = np.full(len(ax), False)
    if len(ax) > ADX_RISE_BARS:
        rising[ADX_RISE_BARS:] = ax[ADX_RISE_BARS:] > ax[:-ADX_RISE_BARS]
    idx = ind.index
    n = len(ind)

    trades: list[dict] = []
    in_pos, entry_i = False, 0

    for i in range(1, n - 1):
        if math.isnan(d[i]) or math.isnan(d[i - 1]):
            continue
        flip_up = d[i] == -1 and d[i - 1] == 1
        flip_dn = d[i] == 1 and d[i - 1] == -1

        if not in_pos and flip_up:
            if blackout is not None and blackout[i]:
                continue
            above = bool(not math.isnan(sma[i]) and c[i] > sma[i])
            adx_ok = bool(not math.isnan(ax[i]) and ax[i] > adx_min)
            mkt = True if market is None else bool(market[i])
            if not passes(profile, above, adx_ok, bool(rising[i]), mkt):
                continue
            in_pos, entry_i = True, i + 1
        elif in_pos and flip_dn:
            x = i + 1
            buy = o[entry_i] * (1 + cost)
            sell = o[x] * (1 - cost)
            trades.append({
                "entry_date": idx[entry_i].strftime("%Y-%m-%d"),
                "exit_date": idx[x].strftime("%Y-%m-%d"),
                "entry": round(buy, 2), "exit": round(sell, 2),
                "ret_pct": round((sell / buy - 1) * 100, 2),
                "hold_days": int((idx[x] - idx[entry_i]).days),
                "open": False,
            })
            in_pos = False

    if in_pos and entry_i < n:
        buy = o[entry_i] * (1 + cost)
        mark = c[-1] * (1 - cost)   # marked as if closed, so costs are symmetric
        trades.append({
            "entry_date": idx[entry_i].strftime("%Y-%m-%d"),
            "exit_date": idx[-1].strftime("%Y-%m-%d"),
            "entry": round(buy, 2), "exit": round(mark, 2),
            "ret_pct": round((mark / buy - 1) * 100, 2),
            "hold_days": int((idx[-1] - idx[entry_i]).days),
            "open": True,
        })
    return trades


def metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "expectancy": None, "profit_factor": None,
                "max_dd": None, "total_return": 0.0, "avg_hold": None,
                "avg_win": None, "avg_loss": None, "best": None, "worst": None}

    r = np.array([t["ret_pct"] for t in trades], dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    gross_win, gross_loss = wins.sum(), abs(losses.sum())

    equity = np.cumprod(1 + r / 100)
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity / peak - 1) * 100).min())

    return {
        "trades": len(r),
        "win_rate": round(len(wins) / len(r) * 100, 1),
        "expectancy": round(float(r.mean()), 2),
        "profit_factor": (round(float(gross_win / gross_loss), 2) if gross_loss > 0 else None),
        "max_dd": round(max_dd, 1),
        "total_return": round(float((equity[-1] - 1) * 100), 1),
        "avg_hold": round(float(np.mean([t["hold_days"] for t in trades])), 0),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "best": round(float(r.max()), 1),
        "worst": round(float(r.min()), 1),
    }


def equity_curve(trades: list[dict], start: date, end: date) -> list[dict]:
    """Step curve: value changes only when a trade closes. Indexed to 100."""
    pts = [{"time": start.strftime("%Y-%m-%d"), "value": 100.0}]
    value = 100.0
    for t in trades:
        value *= 1 + t["ret_pct"] / 100
        pts.append({"time": t["exit_date"], "value": round(value, 2)})
    if pts[-1]["time"] != end.strftime("%Y-%m-%d"):
        pts.append({"time": end.strftime("%Y-%m-%d"), "value": round(value, 2)})
    # Lightweight Charts requires strictly ascending, unique timestamps
    seen, clean = set(), []
    for p in pts:
        if p["time"] in seen:
            clean[-1] = p
        else:
            seen.add(p["time"])
            clean.append(p)
    return clean


def buy_and_hold(ind: pd.DataFrame, cost_pct: float | None = None) -> tuple[list[dict], dict]:
    cost = (settings.cost_pct_per_side if cost_pct is None else cost_pct) / 100.0
    close = ind["close"]
    base = float(close.iloc[0]) * (1 + cost)
    # Weekly sampling keeps the payload small without changing the shape
    sampled = close.iloc[:: max(1, len(close) // 400)]
    curve = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v) / base * 100, 2)}
        for idx, v in sampled.items()
    ]
    if curve[-1]["time"] != close.index[-1].strftime("%Y-%m-%d"):
        curve.append({"time": close.index[-1].strftime("%Y-%m-%d"),
                      "value": round(float(close.iloc[-1]) / base * 100, 2)})

    running_max = close.cummax()
    return curve, {
        "total_return": round((float(close.iloc[-1]) * (1 - cost) / base - 1) * 100, 1),
        "max_dd": round(float(((close / running_max - 1) * 100).min()), 1),
    }


def verdict(results: dict[str, dict], bh: dict) -> dict:
    """Pick a recommended profile, and be honest about weak evidence."""
    scored = [(k, v["metrics"]) for k, v in results.items() if v["metrics"]["trades"] > 0]
    if not scored:
        return {"profile": None, "text": "Not enough history to test.", "confidence": "none"}

    best_key, best = max(scored, key=lambda kv: kv[1]["expectancy"] or -999)
    n = best["trades"]
    confidence = "low" if n < 10 else ("moderate" if n < 25 else "reasonable")

    parts = [f"{LABELS[best_key]} has the highest average return per trade "
             f"at {best['expectancy']:.2f}% over {n} trades."]
    if confidence == "low":
        parts.append(f"Only {n} trades — treat this as a hint, not evidence.")

    if best["total_return"] is not None and best["total_return"] < bh["total_return"]:
        parts.append(
            f"Note that buy-and-hold returned {bh['total_return']:.0f}% against "
            f"{best['total_return']:.0f}% for this method, though with a "
            f"{abs(bh['max_dd']):.0f}% drawdown versus {abs(best['max_dd']):.0f}%."
        )
    return {"profile": best_key, "text": " ".join(parts), "confidence": confidence}


def run(
    df: pd.DataFrame,
    atr_len: int | None = None,
    atr_mult: float | None = None,
    adx_min: float | None = None,
    years: float | None = None,
    bars: int | None = None,
    earnings: list | None = None,
    market: pd.DataFrame | None = None,
) -> dict:
    """Backtest every profile on one ticker's stored history.

    Indicators are computed over the FULL history and only then sliced to the
    requested window, so the 200-day SMA is fully warmed up at the first bar
    of the window. Slicing first and dropping the NaN warm-up afterwards
    silently extends the window by roughly 60 trading days, which is what
    made the backtest disagree with the chart.

    `bars` takes precedence over `years`: it is what the chart uses, so
    passing it makes the two cover exactly the same period.
    """
    atr_len = atr_len or settings.atr_len
    atr_mult = atr_mult or settings.atr_mult
    adx_min = settings.adx_min if adx_min is None else adx_min

    ind = compute_all(df, atr_len, atr_mult, settings.sma_len, settings.di_len, settings.adx_len)
    ind = ind.dropna(subset=["direction", "sma", "adx"])

    if bars:
        ind = ind.tail(int(bars))
    elif years:
        cutoff = ind.index.max() - pd.Timedelta(days=int(years * 365.25))
        ind = ind[ind.index > cutoff]

    if len(ind) < 60:
        return {"error": "Not enough history after indicator warm-up. Needs about 260 bars."}

    start, end = ind.index[0].date(), ind.index[-1].date()
    bh_curve, bh_stats = buy_and_hold(ind)
    cost_pct = settings.cost_pct_per_side

    mask = blackout_mask(ind.index, earnings or [])
    mkt = market_ok_series(market, ind.index)
    results = {}
    for p in PROFILES:
        trades = simulate(ind, p, adx_min, blackout=mask, market=mkt)
        results[p] = {
            "label": LABELS[p],
            "metrics": metrics(trades),
            "equity": equity_curve(trades, start, end),
            "trades": trades[-40:],
        }

    return {
        "start": start.isoformat(), "end": end.isoformat(), "bars": len(ind),
        "params": {"atr_len": atr_len, "atr_mult": atr_mult, "adx_min": adx_min,
                   "cost_pct_per_side": cost_pct,
                   "earnings_blocked": int(mask.sum()),
                   "earnings_known": len(earnings or []),
                   "market_ok_pct": round(float(mkt.mean()) * 100, 1)},
        "profiles": results,
        "buy_hold": {"label": "Buy and hold", "equity": bh_curve, **bh_stats},
        "verdict": verdict(results, bh_stats),
    }
