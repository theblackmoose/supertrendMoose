"""Scan engine.

For each watchlist ticker: refresh prices, compute indicators, detect a
Supertrend flip on the most recent bar, apply that ticker's filter
profile, persist the signal and (for passing signals) notify.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select

from . import data, market, notify, splits
from .config import settings
from .db import Meta, Position, ScanRun, Signal, WatchItem, get_session
from .indicators import compute_all, compute_cached
from .backtest import ADX_RISE_BARS, MARKET_TICKER
from .watchlist import ADX_ONLY, ADX_RISING, FULL, NONE, REGIME

log = logging.getLogger("scanner")

# Set while a scan is in flight so the dashboard can say so. The first scan
# fetches five years for the whole watchlist and takes minutes; without this
# the rail just sits empty and looks broken.
_scan_state: dict = {"running": False, "started": None, "note": ""}


# Silence used to mean both "nothing to trade" and "nothing ran". These
# alerts say which. At most one a day, so an outage that lasts a week does
# not send seven identical messages.
FAILURE_ALERT_MIN = 3          # or a tenth of the watchlist, whichever is larger
_last_failure_alert: date | None = None


def _alert_if_broken(watched: int, failed: list[str], states: list[dict]) -> bool:
    """Tell the user when a scan did not really work.

    Two cases worth waking someone for: most tickers failed to fetch, and the
    stored prices are still behind the last finished US session, which means
    today's scan changed nothing.

    Returns True when the scan was degraded, whether or not an alert went out
    this time, so the caller can suppress a heartbeat that would otherwise
    claim everything is fine.
    """
    global _last_failure_alert
    lines, title = [], ""
    threshold = max(FAILURE_ALERT_MIN, round(watched / 10)) if watched else FAILURE_ALERT_MIN

    if not states:
        title = "scan found no data at all"
        lines.append(f"No ticker could be evaluated out of {watched}.")
    elif len(failed) >= threshold:
        title = f"{len(failed)} of {watched} tickers failed"
        lines.append("Price download failed for: " + ", ".join(sorted(failed)[:15])
                     + ("…" if len(failed) > 15 else ""))

    latest = max((date.fromisoformat(st["date"]) if isinstance(st["date"], str) else st["date"]
                  for st in states), default=None)
    behind = market.sessions_behind(latest)
    if behind and behind >= 2:
        title = title or f"prices are {behind} sessions behind"
        lines.append(f"Stored prices end {latest}, which is {behind} sessions ago. "
                     "Signals are being computed on stale data.")

    if not title:
        _last_failure_alert = None        # healthy again
        return False
    today = date.today()
    if _last_failure_alert == today:
        return True                       # still broken, just already said so
    _last_failure_alert = today
    lines.append("Open /api/diagnose or the container log for the cause.")
    log.warning("scan problem: %s", title)
    notify.send(f"SupertrendMoose: {title}", "\n".join(lines))
    return True


def scan_status() -> dict:
    return dict(_scan_state)

# ── attention score ───────────────────────────────────────────────────
# Ranks how much a ticker deserves looking at RIGHT NOW. It is not a
# prediction and not a measure of edge - it answers "where should I look
# first this morning", which is a question the data can actually support.
#
# Three components, each scored 0-100, then weighted. Every component is
# shown on the row so the ranking can be read rather than trusted.
FRESHNESS_WEIGHT = 0.40   # a young trend leaves more of the move ahead of you
RISK_WEIGHT = 0.30        # distance to stop sets position size
STRENGTH_WEIGHT = 0.30    # ADX says whether the trend is real

FRESH_FADE_BARS = 60      # a trend this old scores zero for freshness
RISK_FLOOR_PCT = 2.0      # at or below this distance to stop, full marks
RISK_CEILING_PCT = 12.0   # at this distance, zero
RISK_MIN = -100.0         # and it keeps falling past there, rather than flooring
STRENGTH_SPAN = 25.0      # ADX this far above the threshold scores full marks


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_components(bars_in_trend: int, risk_pct: float,
                     adx: float, adx_min: float) -> dict:
    """The three 0-100 sub-scores and their weighted total."""
    freshness = _clamp(100.0 * (1.0 - (max(bars_in_trend, 1) - 1) / FRESH_FADE_BARS))

    # Risk keeps falling past the ceiling instead of flooring at zero. Clamping
    # made 12% and 21% score identically, so a stop twice as far away looked
    # no worse - and a distant stop is not merely "not good", it is actively
    # bad: it forces a tiny position and risks a large loss if hit.
    risk = _clamp(100.0 * (RISK_CEILING_PCT - risk_pct)
                  / (RISK_CEILING_PCT - RISK_FLOOR_PCT), low=RISK_MIN)

    strength = _clamp(100.0 * (adx - adx_min) / STRENGTH_SPAN)
    total = _clamp(freshness * FRESHNESS_WEIGHT + risk * RISK_WEIGHT
                   + strength * STRENGTH_WEIGHT)
    return {
        "freshness": round(freshness), "risk": round(risk),
        "strength": round(strength), "total": round(total),
    }


def bars_since_flip(direction) -> int:
    """How many bars the current Supertrend direction has held."""
    values = direction.to_numpy()
    if not len(values):
        return 0
    current = values[-1]
    count = 0
    for value in values[::-1]:
        if value != current:
            break
        count += 1
    return count


_market_cache: tuple[date, bool] | None = None


def market_regime_ok() -> bool:
    """Is the market index above its own long-term average right now?

    Cached for the day: every ticker in a scan asks the same question, and the
    answer only changes when new bars arrive.
    """
    global _market_cache
    today = date.today()
    if _market_cache and _market_cache[0] == today:
        return _market_cache[1]

    market = data.load(MARKET_TICKER)
    if market.empty or len(market) < settings.sma_len:
        # No market data is not the same as a bearish market; pass rather
        # than silently blocking every entry.
        result = True
    else:
        sma = market["close"].rolling(settings.sma_len).mean().iloc[-1]
        result = bool(pd.notna(sma) and market["close"].iloc[-1] > sma)
    _market_cache = (today, result)
    return result


def _combine(rows: list[Position]) -> dict:
    """Several lots of one ticker seen as the holding they add up to.

    Buying more of something you hold leaves two lots. The dashboard shows
    one holding: the shares add up, the entry price is the average you paid
    weighted by size, and it is held from the first purchase.
    """
    rows = sorted(rows, key=lambda r: (r.entry_date, r.id or 0))
    qty = sum(r.quantity or 0.0 for r in rows)
    if qty:
        entry = sum((r.quantity or 0.0) * r.entry_price for r in rows) / qty
    else:
        entry = rows[0].entry_price
    return {
        "id": rows[0].id,
        "entry_date": rows[0].entry_date.isoformat(),
        "entry_price": round(entry, 4),
        "quantity": qty or None,
        "entry_fee": round(sum(r.entry_fee or 0.0 for r in rows), 2),
        "lots": len(rows),
        "note": rows[0].note,
    }


def open_positions() -> dict[str, dict]:
    """Every open holding in one query, keyed by ticker.

    Asking per ticker meant 39 round trips for one dashboard refresh.
    """
    with get_session() as s:
        rows = s.execute(
            select(Position).where(Position.exit_date.is_(None))
            .order_by(Position.entry_date)
        ).scalars().all()
    by_ticker: dict[str, list] = {}
    for row in rows:
        by_ticker.setdefault(row.ticker, []).append(row)
    return {t: _combine(rs) for t, rs in by_ticker.items()}


def open_position(ticker: str) -> dict | None:
    """What you currently hold in this ticker, all lots together."""
    with get_session() as s:
        rows = s.execute(
            select(Position)
            .where(Position.ticker == ticker, Position.exit_date.is_(None))
            .order_by(Position.entry_date)
        ).scalars().all()
        return _combine(rows) if rows else None


def evaluate(item: WatchItem, df: pd.DataFrame,
             market_ok: bool | None = None,
             positions: dict[str, dict] | None = None) -> dict | None:
    """Compute the current state of one ticker. None if there isn't enough data."""
    atr_len = item.atr_len or settings.atr_len
    atr_mult = item.atr_mult or settings.atr_mult
    adx_min = item.adx_min if item.adx_min is not None else settings.adx_min

    if len(df) < max(settings.sma_len, 60) + 5:
        return None
    if market_ok is None:
        market_ok = market_regime_ok()

    ind = compute_cached(item.ticker, df, atr_len, atr_mult,
                         settings.sma_len, settings.di_len, settings.adx_len)
    ind = ind.dropna(subset=["direction"])
    if len(ind) < 2:
        return None

    last, prev = ind.iloc[-1], ind.iloc[-2]
    d = ind.index[-1].date()
    held = bars_since_flip(ind["direction"])

    flip_up = last.direction == -1 and prev.direction == 1
    flip_dn = last.direction == 1 and prev.direction == -1

    above_sma = bool(pd.notna(last.sma) and last.close > last.sma)
    adx_ok = bool(pd.notna(last.adx) and last.adx > adx_min)
    adx_rising = bool(
        len(ind) > ADX_RISE_BARS
        and pd.notna(last.adx)
        and last.adx > ind["adx"].iloc[-1 - ADX_RISE_BARS]
    )

    # Filter profile
    if item.profile == FULL:
        passed, why = (above_sma and adx_ok), ""
        if not above_sma:
            why = "below SMA200"
        elif not adx_ok:
            why = f"ADX {last.adx:.0f} < {adx_min:.0f}"
    elif item.profile == ADX_ONLY:
        passed = adx_ok
        why = "" if passed else f"ADX {last.adx:.0f} < {adx_min:.0f}"
    elif item.profile == ADX_RISING:
        passed = adx_rising
        why = "" if passed else f"ADX {last.adx:.0f} not rising"
    elif item.profile == REGIME:
        passed = market_ok
        why = "" if passed else f"{MARKET_TICKER} below its 200-day average"
    else:  # NONE
        passed, why = True, ""

    # Earnings blackout: block new entries, never exits
    blackout = False
    if item.next_earnings:
        days = (item.next_earnings - d).days
        if 0 <= days <= 5:
            blackout, passed, why = True, False, f"earnings in {days}d"

    # Liquidity / volatility suitability
    suitable = (
        pd.notna(last.avg_volume) and last.avg_volume >= settings.min_avg_volume
        and settings.min_atr_pct <= last.atr_pct <= settings.max_atr_pct
    )
    if flip_up and not suitable and passed:
        passed, why = False, "outside liquidity/ATR screen"

    risk_pct = ((float(last.close) / float(last.supertrend) - 1) * 100
                if pd.notna(last.supertrend) and last.supertrend else 0.0)
    # Only an uptrend the filter has cleared is worth scoring; everything
    # else is not actionable, so it ranks below anything that is.
    scored = bool(last.direction == -1 and passed)
    parts = (score_components(held, risk_pct, float(last.adx), adx_min)
             if scored else None)
    if scored:
        score_note = ""
    elif last.direction != -1:
        score_note = "downtrend"
    else:
        score_note = why or "filtered out"

    return {
        "ticker": item.ticker, "name": item.name or item.ticker,
        "sector": item.sector, "profile": item.profile,
        "bars_in_trend": held, "risk_pct": round(risk_pct, 1),
        "adx_rising": adx_rising, "market_ok": market_ok,
        "position": _position_state(item.ticker, float(last.close),
                                    float(last.supertrend) if pd.notna(last.supertrend) else None,
                                    d, positions, df),
        "score": parts["total"] if parts else None, "score_parts": parts,
        "score_note": score_note,
        "date": d, "close": float(last.close), "stop": float(last.supertrend),
        "adx": float(last.adx), "atr_pct": float(last.atr_pct),
        "avg_volume": float(last.avg_volume) if pd.notna(last.avg_volume) else 0.0,
        "sma": float(last.sma) if pd.notna(last.sma) else None,
        "above_sma": above_sma, "adx_ok": adx_ok, "blackout": blackout,
        "trend": "up" if last.direction == -1 else "down",
        "flip_up": bool(flip_up), "flip_dn": bool(flip_dn),
        "passed": bool(passed), "reason": why,
        "next_earnings": item.next_earnings.isoformat() if item.next_earnings else None,
        "tradeable": bool(last.direction == -1 and passed),
    }


def _split_suspect(pos: dict, df: pd.DataFrame | None) -> dict | None:
    """Does this purchase look like it predates a split?"""
    if df is None or df.empty:
        return None
    entry_date = date.fromisoformat(pos["entry_date"])
    window = df.loc[: pd.Timestamp(entry_date)].tail(splits.LOOKBACK_DAYS + 1)
    closes = {ts.date(): float(c) for ts, c in window["close"].items()}
    return splits.detect(pos["entry_price"], entry_date, pos["quantity"], closes)


def _position_state(ticker: str, close: float, stop: float | None, asof: date,
                    positions: dict[str, dict] | None = None,
                    df: pd.DataFrame | None = None) -> dict | None:
    """Live figures for a held position: P&L, and what is actually at risk."""
    pos = positions.get(ticker) if positions is not None else open_position(ticker)
    if not pos:
        return None

    entry = pos["entry_price"]
    qty = pos["quantity"]
    pnl_pct = (close / entry - 1) * 100 if entry else 0.0
    held_days = max((asof - date.fromisoformat(pos["entry_date"])).days, 0)

    # Risk is measured from the CURRENT stop, not the entry. Once the stop
    # trails above your entry the trade can no longer lose money, and that is
    # the thing worth seeing.
    risk_per_share = (entry - stop) if stop is not None else None
    out = {
        **pos,
        "pnl_pct": round(pnl_pct, 2),
        "held_days": held_days,
        "stop": round(stop, 2) if stop is not None else None,
        "risk_locked_out": bool(stop is not None and stop >= entry),
        # A split rewrites the stored history but not what you typed, so the
        # dashboard says so rather than showing figures that are out by the
        # split factor. Only the few bars around the purchase are needed.
        "split_suspect": _split_suspect(pos, df),
    }
    if qty:
        out["pnl_value"] = round((close - entry) * qty, 2)
        out["market_value"] = round(close * qty, 2)
        out["risk_value"] = round(max(risk_per_share or 0.0, 0.0) * qty, 2)
    return out


def current_states(atr_len: int | None = None, atr_mult: float | None = None,
                   adx_min: float | None = None) -> list[dict]:
    """State of every enabled ticker, for the dashboard rail.

    The overrides let the dashboard's ATR and ADX controls drive these figures
    too. Without them the chart would redraw on a new setting while the stop,
    risk, verdict and attention score below it still reflected the stored
    values - the same numbers, silently describing different settings.
    """
    with get_session() as s:
        items = s.execute(select(WatchItem).where(WatchItem.enabled.is_(True))).scalars().all()
        items = [
            WatchItem(ticker=i.ticker, name=i.name, sector=i.sector, profile=i.profile,
                      enabled=i.enabled,
                      atr_len=atr_len or i.atr_len,
                      atr_mult=atr_mult or i.atr_mult,
                      adx_min=adx_min if adx_min is not None else i.adx_min,
                      next_earnings=i.next_earnings)
            for i in items
        ]
    # Resolved once for the whole sweep rather than per ticker.
    held = open_positions()
    market = market_regime_ok()

    out = []
    for item in items:
        df = data.load(item.ticker)
        st = evaluate(item, df, market_ok=market, positions=held)
        if st:
            out.append(st)
    # Fired today first, then by attention score, then unscored rows by ADX.
    order = {"up": 0, "down": 1}
    out.sort(key=lambda x: (
        not x["flip_up"],
        order[x["trend"]],
        -(x["score"] if x["score"] is not None else -1),
        -x["adx"],
    ))
    return out


def run_scan(refresh_prices: bool = True, notify_on: bool = True,
             resend: bool = False) -> dict:
    """Full nightly scan. Returns a summary dict."""
    started = datetime.utcnow()
    _scan_state.update(running=True, started=started.isoformat(),
                       note="fetching prices" if refresh_prices else "evaluating")
    with get_session() as s:
        run = ScanRun(started=started)
        s.add(run)
        s.commit()
        run_id = run.id
        items = s.execute(select(WatchItem).where(WatchItem.enabled.is_(True))).scalars().all()
        tickers = [i.ticker for i in items]
        snapshot = [
            WatchItem(ticker=i.ticker, name=i.name, sector=i.sector, profile=i.profile,
                      enabled=i.enabled, atr_len=i.atr_len, atr_mult=i.atr_mult,
                      adx_min=i.adx_min, next_earnings=i.next_earnings)
            for i in items
        ]

    failed: list[str] = []
    if refresh_prices and tickers:
        _, failed = data.refresh(tickers)

    buys, exits, blocked, states = [], [], [], []
    for item in snapshot:
        df = data.load(item.ticker)
        st = evaluate(item, df)
        if not st:
            continue
        states.append(st)

        if st["flip_up"]:
            _record(item.ticker, st, "BUY")
            if st["passed"]:
                buys.append(st)
            else:
                # A signal fired and was rejected. Worth naming in the alert:
                # otherwise a ticker's absence from the buy list looks like
                # nothing happened, rather than a screen doing its job.
                blocked.append(st)
        elif st["flip_dn"]:
            _record(item.ticker, st, "EXIT")
            exits.append(st)

    # A rescan of the same bars must not repeat an alert. That happens more
    # than it sounds: a manual scan, or catch-up retrying hourly while Yahoo
    # has not yet filled in the newest bar, both re-evaluate a bar that has
    # already been alerted on.
    # resend is for asking again on purpose, from the API.
    new_buys = [b for b in buys
                if resend or not _already_notified(b["ticker"], b["date"], "BUY")]
    new_exits = [e for e in exits
                 if resend or not _already_notified(e["ticker"], e["date"], "EXIT")]

    sent = {}
    if notify_on and (new_buys or new_exits):
        title, body = notify.format_signals(new_buys, new_exits, blocked=blocked,
                                            scanned=len(states))
        sent = notify.send(f"SupertrendMoose: {title}", body,
                           urgent=bool(notify.held_exits(new_exits)))
        _mark_notified([(b["ticker"], b["date"], "BUY") for b in new_buys]
                       + [(e["ticker"], e["date"], "EXIT") for e in new_exits])

    with get_session() as s:
        run = s.get(ScanRun, run_id)
        run.finished = datetime.utcnow()
        run.tickers = len(states)
        run.failed = len(failed)
        run.buys = len(buys)
        run.exits = len(exits)
        run.note = ("fetch failed: " + ",".join(failed[:20])) if failed else ""
        s.commit()

    _scan_state.update(running=False, note="")
    log.info("scan done: %d tickers, %d buys, %d exits, %d failed",
             len(states), len(buys), len(exits), len(failed))
    degraded = False
    if notify_on:
        degraded = _alert_if_broken(len(snapshot), failed, states)

    # A quiet day and a broken notification channel look identical from the
    # outside. The heartbeat makes them distinguishable: something arrives on
    # every scan, so silence means the alerting is broken rather than calm.
    # Suppressed on a degraded scan, where the alert above has already gone out
    # and "no signals" would be a misleading thing to say.
    # Once per bar, for the same reason as the alerts above.
    latest_bar = max((st["date"] for st in states), default=None)
    if notify_on and settings.notify_heartbeat and not (buys or exits) and not degraded \
            and latest_bar and _heartbeat_due(latest_bar):
        hb_title, hb_body = notify.format_heartbeat(states, failed)
        sent = notify.send(f"SupertrendMoose: {hb_title}", hb_body)
        if any(sent.values()):
            _set_heartbeat(latest_bar)

    return {
        "tickers": len(states), "buys": buys, "exits": exits,
        "failed": failed, "notified": sent,
        "duration_sec": (datetime.utcnow() - started).total_seconds(),
    }


def _record(ticker: str, st: dict, kind: str) -> None:
    with get_session() as s:
        existing = s.scalar(
            select(Signal).where(Signal.ticker == ticker, Signal.d == st["date"], Signal.kind == kind)
        )
        if existing:
            return
        s.add(Signal(
            ticker=ticker, d=st["date"], kind=kind, passed=st["passed"], reason=st["reason"],
            close=st["close"], stop=st["stop"], adx=st["adx"], atr_pct=st["atr_pct"],
            above_sma=st["above_sma"],
        ))
        s.commit()


def _already_notified(ticker: str, d: date, kind: str) -> bool:
    with get_session() as s:
        return bool(s.scalar(
            select(Signal.notified).where(Signal.ticker == ticker, Signal.d == d,
                                          Signal.kind == kind)
        ))


def _mark_notified(keys: list[tuple[str, date, str]]) -> None:
    """Mark exactly the signals that were alerted: (ticker, bar date, kind)."""
    with get_session() as s:
        for ticker, d, kind in keys:
            row = s.scalar(select(Signal).where(Signal.ticker == ticker, Signal.d == d,
                                                Signal.kind == kind))
            if row:
                row.notified = True
        s.commit()


HEARTBEAT_KEY = "heartbeat_bar"


def _heartbeat_due(bar: date) -> bool:
    with get_session() as s:
        row = s.get(Meta, HEARTBEAT_KEY)
        return not row or row.value != bar.isoformat()


def _set_heartbeat(bar: date) -> None:
    with get_session() as s:
        row = s.get(Meta, HEARTBEAT_KEY)
        if row:
            row.value = bar.isoformat()
        else:
            s.add(Meta(key=HEARTBEAT_KEY, value=bar.isoformat()))
        s.commit()


# Funds and indices never report earnings; asking wastes a request per ticker.
NO_EARNINGS_SECTORS = {"ETF", "Index", "Commodity"}


def refresh_earnings() -> dict:
    """Refresh next-earnings dates and report history for the watchlist.

    Without this the earnings blackout silently does nothing: no stored report
    dates means no bars to block, so both the scanner and the backtest quietly
    stop applying a rule you think is protecting you.
    """
    updated = 0
    skipped = 0
    pruned = 0
    failed: list[str] = []
    with get_session() as s:
        items = s.execute(select(WatchItem)).scalars().all()
        for item in items:
            if (item.sector or "") in NO_EARNINGS_SECTORS:
                skipped += 1
                continue
            try:
                history = data.fetch_earnings_history(item.ticker)
                stale_next = False
                if history:
                    data.store_earnings(item.ticker, history)
                    # Estimates Yahoo has since moved, so neither the backtest
                    # nor the chart keeps a report day that did not happen.
                    moved = data.prune_earnings(item.ticker, history)
                    if moved:
                        pruned += len(moved)
                        log.info("earnings for %s moved: dropped %s", item.ticker,
                                 ", ".join(d.isoformat() for d in moved))
                    upcoming = [d for d in history if d >= date.today()]
                    nxt = upcoming[0] if upcoming else None
                    # A company that reported earlier than its estimate leaves
                    # the old estimate here, and the live blackout would keep
                    # blocking entries until that date passed.
                    stale_next = bool(item.next_earnings and nxt is None
                                      and data.superseded_earnings([item.next_earnings], history))
                else:
                    nxt = data.fetch_next_earnings(item.ticker)
            except Exception as e:  # noqa: BLE001
                # One bad ticker must not abandon the other 38.
                log.warning("earnings fetch failed for %s: %s", item.ticker, e)
                failed.append(item.ticker)
                continue
            if nxt and nxt != item.next_earnings:
                item.next_earnings = nxt
                updated += 1
            elif stale_next:
                item.next_earnings = None
                updated += 1
        s.commit()
    result = earnings_coverage()
    result.update(updated=updated, skipped=skipped, failed=failed, pruned=pruned)
    log.info("earnings refresh: %d updated, %d skipped, %d failed, %d moved dates "
             "dropped, %d/%d tickers now have report dates",
             updated, skipped, len(failed), pruned,
             result["with_history"], result["eligible"])
    return result


def earnings_coverage() -> dict:
    """How many tickers actually have report dates stored.

    Zero means the blackout is inert, which is worth knowing loudly.
    """
    from .db import EarningsDate

    with get_session() as s:
        items = s.execute(select(WatchItem).where(WatchItem.enabled.is_(True))).scalars().all()
        eligible = [i.ticker for i in items
                    if (i.sector or "") not in NO_EARNINGS_SECTORS]
        with_next = sum(1 for i in items if i.next_earnings)
        tickers = {t for (t,) in s.execute(select(EarningsDate.ticker).distinct())}
    return {
        "eligible": len(eligible),
        "with_history": len(tickers & set(eligible)),
        "with_next_date": with_next,
        "blackout_active": bool(tickers & set(eligible)),
    }
