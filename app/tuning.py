"""Walk-forward parameter tuning.

Optimising parameters on the same data you then judge them by is how people
build strategies that look excellent and trade badly. So this splits the
history: settings are chosen on the earlier portion and scored ONLY on the
later portion, which the search never saw.

The out-of-sample column is the only one worth acting on. The in-sample
column is shown so you can see how much of the improvement evaporated —
a large gap between the two is the signal that the fit was noise.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import backtest
from .config import settings
from .indicators import compute_cached

log = logging.getLogger("tuning")

# Deliberately coarse. A fine grid finds better in-sample numbers and worse
# out-of-sample ones - there is simply more noise to fit.
ATR_LENGTHS = (7, 10, 14, 21)
ATR_MULTS = (2.0, 2.5, 3.0, 3.5, 4.0)

MIN_IS_TRADES = 8       # below this, an in-sample result is not worth fitting to
SPLIT = 0.6             # fraction of the window used to choose settings


def _slice_metrics(ind: pd.DataFrame, profile: str, adx_min: float,
                   blackout, market) -> dict:
    return backtest.metrics(
        backtest.simulate(ind, profile, adx_min, blackout=blackout, market=market)
    )


def _evaluate(df: pd.DataFrame, atr_len: int, atr_mult: float,
              profile: str, adx_min: float, split_at: pd.Timestamp,
              earnings: list | None = None,
              market: pd.DataFrame | None = None,
              key: str = "") -> tuple[dict, dict]:
    """Metrics for one parameter pair, in-sample and out-of-sample.

    Indicators are computed over the whole frame and sliced afterwards, so the
    out-of-sample run starts with fully warmed indicators rather than a fresh
    200-bar blind spot.

    The earnings blackout and the market gate must be applied here too. Without
    them the tuner silently measured a different strategy from the backtest -
    the market-regime profile in particular degenerated into Unfiltered,
    because an absent market array makes that gate pass everything.
    """
    ind = compute_cached(key or "tune", df, atr_len, atr_mult, settings.sma_len,
                         settings.di_len, settings.adx_len)
    ind = ind.dropna(subset=["direction", "sma", "adx"])

    blackout = backtest.blackout_mask(ind.index, earnings or [])
    mkt = backtest.market_ok_series(market, ind.index)

    is_sel = ind.index <= split_at
    oos_sel = ~is_sel
    return (
        _slice_metrics(ind[is_sel], profile, adx_min, blackout[is_sel], mkt[is_sel]),
        _slice_metrics(ind[oos_sel], profile, adx_min, blackout[oos_sel], mkt[oos_sel]),
    )


def tune(
    df: pd.DataFrame,
    profile: str = "none",
    adx_min: float | None = None,
    bars: int | None = None,
    earnings: list | None = None,
    market: pd.DataFrame | None = None,
    key: str = "",
) -> dict:
    """Search ATR settings in-sample, report how they did out-of-sample."""
    adx_min = settings.adx_min if adx_min is None else adx_min

    full = compute_cached(key or "tune", df, settings.atr_len, settings.atr_mult,
                          settings.sma_len, settings.di_len, settings.adx_len)
    full = full.dropna(subset=["direction", "sma", "adx"])
    if bars:
        full = full.tail(int(bars))
    if len(full) < 400:
        return {"error": "Need at least 400 bars after warm-up to split a window."}

    window = df[df.index >= full.index[0]]
    split_at = full.index[int(len(full) * SPLIT)]

    results = []
    for atr_len in ATR_LENGTHS:
        for atr_mult in ATR_MULTS:
            is_m, oos_m = _evaluate(window, atr_len, atr_mult, profile, adx_min,
                                    split_at, earnings, market, key)
            results.append({
                "atr_len": atr_len, "atr_mult": atr_mult,
                "is_trades": is_m["trades"], "is_expectancy": is_m["expectancy"],
                "oos_trades": oos_m["trades"], "oos_expectancy": oos_m["expectancy"],
                "oos_return": oos_m["total_return"], "oos_max_dd": oos_m["max_dd"],
                "oos_win_rate": oos_m["win_rate"],
            })

    # Choose on in-sample only, and only where there were enough trades to mean
    # anything. The out-of-sample numbers play no part in the selection.
    eligible = [r for r in results if r["is_trades"] >= MIN_IS_TRADES
                and r["is_expectancy"] is not None]
    underpowered = not eligible
    if underpowered:
        eligible = [r for r in results if r["is_expectancy"] is not None]
    if not eligible:
        return {"error": "No parameter combination produced any trades."}

    chosen = max(eligible, key=lambda r: r["is_expectancy"])

    baseline = next(
        (r for r in results
         if r["atr_len"] == settings.atr_len and r["atr_mult"] == settings.atr_mult),
        None,
    )

    corr, corr_n = fit_test_correlation(results)
    verdict, held_up = _verdict(chosen, baseline, underpowered)
    if underpowered:
        held_up = False
    return {
        "profile": profile,
        "profile_label": backtest.LABELS.get(profile, profile),
        "split_date": split_at.strftime("%Y-%m-%d"),
        "in_sample": f"{full.index[0]:%Y-%m-%d} to {split_at:%Y-%m-%d}",
        "out_sample": f"{split_at:%Y-%m-%d} to {full.index[-1]:%Y-%m-%d}",
        "bars": len(full),
        "grid_size": len(results),
        "chosen": chosen,
        "baseline": baseline,
        "held_up": held_up,
        "underpowered": underpowered,
        "fit_test_r": None if corr is None else round(corr, 3),
        "fit_test_n": corr_n,
        "fit_test_note": correlation_note(corr),
        "verdict": verdict,
        "results": sorted(results, key=lambda r: -(r["is_expectancy"] or -999)),
    }


def fit_test_correlation(results: list[dict]) -> tuple[float | None, int]:
    """How well the fitting window predicted the test window, for this ticker.

    This is the number that decides whether tuning is worth doing at all. Near
    zero means the fitting window carries no information about the test window,
    so whichever setting "won" the fit was chosen by noise. Measured across all
    39 tickers this sits around 0.03; showing it per ticker keeps the tab
    honest about itself instead of relying on an offline analysis.
    """
    pairs = [(r["is_expectancy"], r["oos_expectancy"]) for r in results
             if r["is_expectancy"] is not None and r["oos_expectancy"] is not None]
    if len(pairs) < 4:
        return None, len(pairs)

    xs = np.array([p[0] for p in pairs], dtype=float)
    ys = np.array([p[1] for p in pairs], dtype=float)
    if xs.std() == 0 or ys.std() == 0:
        return None, len(pairs)
    return float(np.corrcoef(xs, ys)[0, 1]), len(pairs)


def correlation_note(r: float | None) -> str:
    """Plain reading of the fit-to-test correlation.

    Sign matters and abs() would hide it: a negative correlation means the
    settings that looked best in the fitting window tended to do WORSE on the
    test window, which is worse than no information at all.
    """
    if r is None:
        return "not enough varying rows to measure"
    if r <= -0.5:
        return "strongly inverted — the fit winner tended to do worse, not better"
    if r <= -0.2:
        return "inverted — fitting well predicted testing badly"
    if r < 0.2:
        return "no predictive relationship — the fit winner was chosen by noise"
    if r < 0.5:
        return "weak relationship, not enough to act on"
    return "some predictive relationship, unusual for this data"


def _verdict(chosen: dict, baseline: dict | None,
             underpowered: bool = False) -> tuple[str, bool]:
    ie, oe = chosen["is_expectancy"], chosen["oos_expectancy"]
    parts = []

    if underpowered:
        parts.append(
            f"No setting produced {MIN_IS_TRADES} trades in the fitting window, "
            "so there was never enough here to tune on. Read the rest as "
            "illustration, not a recommendation."
        )

    parts.append(
        f"ATR {chosen['atr_len']} / {chosen['atr_mult']:g} was best in the fitting "
        f"window at {ie:+.2f}% per trade over {chosen['is_trades']} trades."
    )

    if oe is None or chosen["oos_trades"] == 0:
        parts.append("It produced no trades out-of-sample, so there is nothing to confirm it.")
        return " ".join(parts), False

    parts.append(
        f"On the held-back test window it managed {oe:+.2f}% "
        f"over {chosen['oos_trades']} trades."
    )

    held = oe > 0
    if oe <= 0 < ie:
        parts.append("The edge did not survive the split, which means it was noise.")
    elif ie and oe < ie * 0.5:
        parts.append("Less than half the in-sample edge carried over; treat it as fragile.")

    if baseline and baseline["oos_expectancy"] is not None:
        delta = oe - baseline["oos_expectancy"]
        parts.append(
            f"Your current default of ATR {settings.atr_len}/{settings.atr_mult:g} "
            f"scored {baseline['oos_expectancy']:+.2f}% over the same test window, so "
            + ("tuning would have gained %.2f%% per trade."
               % delta if delta > 0 else
               "tuning would have LOST %.2f%% per trade. Keep the default."
               % abs(delta))
        )
        held = held and delta > 0

    if chosen["oos_trades"] < 8:
        parts.append(
            f"Only {chosen['oos_trades']} trades in the test window either way, "
            "which is too few to conclude much."
        )
        held = False

    parts.append(
        "Verdict: the tuned setting is worth applying."
        if held else
        "Verdict: nothing here beat your current settings on data it had not seen."
    )
    return " ".join(parts), held
