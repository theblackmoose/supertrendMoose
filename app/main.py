"""FastAPI app: JSON API for the dashboard plus the nightly scheduler."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import numpy as np
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import backtest, data, earnings, market, notify, scanner, splits, tuning
from .config import settings
from .db import Position, Price, ScanRun, Signal, WatchItem, get_session, init_db
from .indicators import compute_cached, macd as macd_calc, macd_weekly, trading_days_until

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
# yfinance logs every failed ticker on every retry, which buries the summary.
logging.getLogger("yfinance").setLevel(
    getattr(logging, os.getenv("YFINANCE_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
)

log = logging.getLogger("main")

STATIC_DIR = os.getenv(
    "STATIC_DIR", str(Path(__file__).resolve().parent.parent / "static")
)
# TZ is the user's own zone: it sets log times and the weekly earnings
# refresh. The nightly scan follows New York instead (app/market.py), so a
# user anywhere gets their scan after every US close without adjusting it.
USER_TZ = market.zone(os.getenv("TZ", "").strip() or "UTC")
SCHEDULER_TZ = USER_TZ.key
scheduler = BackgroundScheduler(timezone=USER_TZ)
SCHEDULE = market.build_schedule(settings, USER_TZ)


def auth(x_auth_token: str | None = Header(default=None)) -> None:
    """No-op when AUTH_TOKEN is unset (LAN-only deployments).

    compare_digest takes the same time however much of the token matches, so
    response timing cannot be used to guess it a character at a time.
    """
    if settings.auth_token and not hmac.compare_digest(
        (x_auth_token or "").encode(), settings.auth_token.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Auth-Token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(scanner.run_scan, SCHEDULE.trigger, id="nightly_scan",
                      max_instances=1, coalesce=True, misfire_grace_time=3600)
    # misfire_grace_time=None: run it however late it is, so a scan missed
    # while the machine slept still happens on wake.
    scheduler.add_job(_catch_up, IntervalTrigger(minutes=CATCH_UP_EVERY),
                      id="catch_up", max_instances=1, coalesce=True,
                      misfire_grace_time=None,
                      next_run_time=datetime.now(USER_TZ) + timedelta(seconds=20))
    # Also run late rather than not at all, for a machine asleep on Sunday
    # morning: otherwise the blackout dates would wait another week.
    scheduler.add_job(scanner.refresh_earnings,
                      CronTrigger(day_of_week="sun", hour=6, timezone=USER_TZ),
                      id="earnings_refresh", max_instances=1, coalesce=True,
                      misfire_grace_time=None)
    scheduler.start()
    nxt = scheduler.get_job("nightly_scan").next_run_time
    # Reported in the trigger's own zone, which is New York for the default.
    nxt = nxt.astimezone(USER_TZ) if nxt else None
    log.info("scan scheduled %s; next %s (%s New York)", SCHEDULE.description,
             f"{nxt:%a %d %b %H:%M} {SCHEDULER_TZ}" if nxt else "none",
             f"{nxt.astimezone(market.NY):%a %H:%M}" if nxt else "-")
    if SCHEDULE.note:
        log.info("schedule: %s", SCHEDULE.note)
    if SCHEDULE.warning:
        log.warning("schedule: %s", SCHEDULE.warning)

    if settings.scan_on_startup:
        scheduler.add_job(_startup_scan, id="startup_scan")
    yield
    scheduler.shutdown(wait=False)


# A machine that sleeps through the scan time gets no scan: the container is
# frozen, and when it wakes the scheduler sees a fire time an hour or a day
# in the past. This looks for missed sessions instead of relying on the
# machine being awake at one moment, and covers restarts too.
CATCH_UP_EVERY = 15          # minutes between checks
CATCH_UP_RETRY = timedelta(minutes=60)   # after a failed attempt
_last_catch_up: datetime | None = None


def _catch_up() -> None:
    """Scan if the stored prices are behind the last finished US session."""
    global _last_catch_up
    try:
        if scanner.scan_status()["running"]:
            return
        latest = _latest_stored_date()
        if latest is None:
            return                      # a cold start is _startup_scan's job
        behind = market.sessions_behind(latest)
        if not behind:
            return
        # A session that has only just closed belongs to the nightly scan,
        # not to catch-up. Without this, the first 15-minute tick after the
        # close + settle window scans (and alerts) early, then the nightly
        # scan fires on schedule and sends the same alert a second time.
        if datetime.now(UTC) < _scheduled_scan_due(market.last_completed_session()):
            return
        # Yahoo being down must not turn this into a retry loop.
        now = datetime.now(UTC)
        if _last_catch_up and now - _last_catch_up < CATCH_UP_RETRY:
            return
        _last_catch_up = now
        log.info("prices are %d session(s) behind (stored to %s) - scanning now",
                 behind, latest)
        scanner.run_scan(refresh_prices=True, notify_on=True)
    except Exception as e:  # noqa: BLE001 - a background job must not die
        log.error("catch-up scan failed: %s", e)


CATCH_UP_GRACE = timedelta(minutes=20)    # let the nightly scan run first


def _scheduled_scan_due(session: date) -> datetime:
    """When the nightly scan for `session` should have finished by.

    Uses the real trigger, so it follows SCAN_AFTER_CLOSE_MINUTES or a fixed
    SCAN_TIME alike. Capped at a day after the close so a SCAN_DAYS setting
    that skips this session cannot stall catch-up indefinitely.
    """
    close = datetime.combine(session, market.CLOSE, market.NY)
    fire = SCHEDULE.trigger.get_next_fire_time(None, close)
    cap = close + timedelta(hours=24)
    due = min(fire, cap) if fire else cap
    return (due + CATCH_UP_GRACE).astimezone(UTC)


def _latest_stored_date() -> date | None:
    with get_session() as s:
        return s.scalar(select(Price.d).order_by(Price.d.desc()).limit(1))


def _startup_scan() -> None:
    """Seed on a cold start, and top up if the stored data has gone stale."""
    try:
        with get_session() as s:
            item = s.scalar(select(WatchItem).limit(1))
        if not item:
            return

        latest = _latest_stored_date()
        if latest is None:
            log.info("cold start: fetching history, this takes a few minutes")
            scanner.run_scan(refresh_prices=True, notify_on=False)
            scanner.refresh_earnings()
            return

        # Prices can survive a rebuild while the earnings table does not, and
        # the weekly refresh may be days away. An inert blackout is a silent
        # change of strategy, so top it up now.
        coverage = scanner.earnings_coverage()
        if not coverage["blackout_active"]:
            log.info("no earnings dates stored - the blackout is inert, refreshing")
            scanner.refresh_earnings()

        # Catching up on missed sessions is _catch_up's job, and it runs
        # within moments of start-up, so there is nothing to do here.
        log.info("stored data is current to %s", latest)
    except Exception as e:  # noqa: BLE001
        log.error("startup scan failed: %s", e)


app = FastAPI(title="SupertrendMoose", version="1.0.0", lifespan=lifespan)


# ───────────────────────────── models ─────────────────────────────
# Yahoo symbols: letters and digits, plus . - = (BRK-B, GC=F) and a leading ^
# for indices (^GSPC). Anything else is refused before it reaches Yahoo, the
# database or the dashboard.
TICKER_PATTERN = r"^\^?[A-Za-z0-9][A-Za-z0-9.=\-]{0,14}$"
Ticker = Annotated[str, Field(pattern=TICKER_PATTERN)]


class WatchIn(BaseModel):
    ticker: Ticker
    name: str = Field("", max_length=120)
    sector: str = Field("", max_length=40)
    profile: str = "full"
    enabled: bool = True
    atr_len: int | None = Field(None, ge=2, le=50)
    atr_mult: float | None = Field(None, ge=0.5, le=8)
    adx_min: float | None = Field(None, ge=0, le=60)


class PositionIn(BaseModel):
    ticker: Ticker
    entry_date: date
    entry_price: float
    quantity: float | None = Field(None, gt=0)
    entry_fee: float = Field(0.0, ge=0, le=100_000)
    note: str = Field("", max_length=500)


class PositionClose(BaseModel):
    exit_date: date
    exit_price: float
    exit_fee: float = Field(0.0, ge=0, le=100_000)


class WatchPatch(BaseModel):
    sector: str | None = Field(None, max_length=40)
    profile: str | None = None
    enabled: bool | None = None
    atr_len: int | None = Field(None, ge=2, le=50)
    atr_mult: float | None = Field(None, ge=0.5, le=8)
    adx_min: float | None = Field(None, ge=0, le=60)


# ───────────────────────────── routes ─────────────────────────────
@app.get("/api/diagnose", dependencies=[Depends(auth)])
def diagnose() -> dict:
    """One-shot check of the data path. Use this when a scan returns nothing."""
    import yfinance as yf

    result: dict = {"yfinance_version": yf.__version__,
                    "cache_writable": data.ensure_cache(),
                    "cache_dir": os.getenv("YF_CACHE_DIR", "/tmp/yf-cache")}
    try:
        df = yf.download("AAPL", period="5d", interval="1d",
                         progress=False, threads=False, auto_adjust=True)
        result["probe_ticker"] = "AAPL"
        result["probe_rows"] = 0 if df is None else len(df)
        result["probe_ok"] = bool(df is not None and len(df))
    except Exception as e:  # noqa: BLE001
        result["probe_ok"] = False
        result["probe_error"] = f"{type(e).__name__}: {e}"

    if not result.get("probe_ok"):
        result["hint"] = (
            "Yahoo returned nothing. Check, in order: cache_writable above; "
            "whether Yahoo is rate-limiting this IP (wait ~15 min); whether "
            "yfinance needs upgrading; whether the container has outbound "
            "network access."
        )
    with get_session() as s:
        result["stored_tickers"] = len({
            r for (r,) in s.execute(select(Price.ticker).distinct()).all()
        })
    latest = _latest_stored_date()
    result["data_through"] = latest.isoformat() if latest else None
    result["today_local"] = date.today().isoformat()
    result["timezone"] = SCHEDULER_TZ
    return result


@app.get("/api/health")
def health() -> dict:
    with get_session() as s:
        last = s.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))
        n = len(s.execute(select(WatchItem).where(WatchItem.enabled.is_(True))).scalars().all())
    latest = _latest_stored_date()
    scan = scanner.scan_status()
    cov = scanner.earnings_coverage()
    return {
        "status": "ok",
        "scanning": scan["running"],
        "scan_note": scan["note"],
        "blackout_active": cov["blackout_active"],
        "earnings_coverage": f'{cov["with_history"]}/{cov["eligible"]}',
        "watchlist": n,
        "assets": ASSET_VERSION,
        "data_through": latest.isoformat() if latest else None,
        # Prices silently stopping updating looked like a quiet market before;
        # now the dashboard can say how many sessions are missing.
        "sessions_behind": market.sessions_behind(latest),
        "timezone": SCHEDULER_TZ,
        "last_scan": last.finished.isoformat() if last and last.finished else None,
        "last_scan_failed": last.failed if last else None,
        "last_scan_tickers": last.tickers if last else None,
        "last_scan_buys": last.buys if last else None,
        "scheduled": SCHEDULE.description,
        "schedule_mode": SCHEDULE.mode,
        "schedule_warning": SCHEDULE.warning or None,
        "next_scan": _next_scan(),
        "default_commission": settings.default_commission,
    }


def _next_scan() -> str | None:
    """Next nightly scan as an ISO time with its UTC offset, for any browser."""
    job = scheduler.get_job("nightly_scan") if scheduler.running else None
    if not job or not job.next_run_time:
        return None
    return job.next_run_time.astimezone(USER_TZ).isoformat()


@app.get("/api/states", dependencies=[Depends(auth)])
def states(
    atr_len: int | None = Query(None, ge=2, le=50),
    atr_mult: float | None = Query(None, ge=0.5, le=8),
    adx_min: float | None = Query(None, ge=0, le=60),
) -> list[dict]:
    """Current state of every watchlist ticker, ranked for the rail.

    Accepts the same overrides as the chart so the dashboard's controls change
    these figures as well, rather than only the drawn lines.
    """
    return scanner.current_states(atr_len, atr_mult, adx_min)


@app.get("/api/chart/{ticker}", dependencies=[Depends(auth)])
def chart(
    ticker: str,
    atr_len: int | None = Query(None),
    atr_mult: float | None = Query(None),
    adx_min: float | None = Query(None),
    bars: int = Query(500, ge=60, le=5000),
) -> dict:
    """OHLC plus every indicator series, ready for Lightweight Charts."""
    ticker = ticker.upper()
    with get_session() as s:
        item = s.get(WatchItem, ticker)
    if item is None:
        raise HTTPException(404, f"{ticker} is not on the watchlist")

    # Same depth the backtest loads, so both slice from an identical series.
    df = data.load(ticker, years=25)
    if df.empty:
        raise HTTPException(404, f"No price history stored for {ticker}. Run a scan first.")

    ind = compute_cached(
        ticker, df,
        atr_len or item.atr_len or settings.atr_len,
        atr_mult or item.atr_mult or settings.atr_mult,
        settings.sma_len, settings.di_len, settings.adx_len,
    ).tail(bars)

    thresh = adx_min if adx_min is not None else (
        item.adx_min if item.adx_min is not None else settings.adx_min
    )

    # Built column-wise with zip rather than row-wise with iterrows: for 1300
    # bars across five series, iterrows was the single largest cost in the
    # whole request, far outweighing the indicator maths itself.
    times = ind.index.strftime("%Y-%m-%d").tolist()
    o_arr = ind["open"].to_numpy(dtype=float).round(4)
    h_arr = ind["high"].to_numpy(dtype=float).round(4)
    l_arr = ind["low"].to_numpy(dtype=float).round(4)
    c_arr = ind["close"].to_numpy(dtype=float).round(4)
    v_arr = ind["volume"].to_numpy(dtype=float)
    dir_arr = ind["direction"].to_numpy(dtype=float)

    def series(col: str, keep: "np.ndarray | None" = None) -> list[dict]:
        vals = ind[col].to_numpy(dtype=float)
        mask = ~np.isnan(vals)
        if keep is not None:
            mask &= keep
        return [{"time": t, "value": round(float(v), 4)}
                for t, v in zip(np.array(times)[mask].tolist(), vals[mask].round(4).tolist())]

    candles = [{"time": t, "open": a, "high": b, "low": c_, "close": d_}
               for t, a, b, c_, d_ in zip(times, o_arr.tolist(), h_arr.tolist(),
                                          l_arr.tolist(), c_arr.tolist())]
    up_bar = c_arr >= o_arr
    volume = [{"time": t, "value": v, "color": "#2bb6a366" if u else "#e5544b66"}
              for t, v, u in zip(times, v_arr.tolist(), up_bar.tolist())]

    # Markers for filtered vs rejected flips across the visible window
    markers = []
    rising = backtest.rising_series(ind["adx"])
    # The markers must reflect every condition the backtest applies, or the
    # chart shows signals as taken that the simulation refused.
    earnings = data.load_earnings(ticker)
    blackout = backtest.blackout_mask(ind.index, earnings)
    market = backtest.market_ok_series(data.load(backtest.MARKET_TICKER, years=25), ind.index)
    rising_arr = rising.to_numpy(dtype=bool)
    sma_arr = ind["sma"].to_numpy(dtype=float)
    adx_arr = ind["adx"].to_numpy(dtype=float)
    # Only the bars where direction actually changed matter - typically a few
    # dozen out of thousands.
    flips = np.nonzero(dir_arr[1:] != dir_arr[:-1])[0] + 1
    for i in flips:
        prev_d, cur_d = dir_arr[i - 1], dir_arr[i]
        if np.isnan(prev_d) or np.isnan(cur_d):
            continue
        if prev_d == 1 and cur_d == -1:
            above = bool(not np.isnan(sma_arr[i]) and c_arr[i] > sma_arr[i])
            adx_ok = bool(not np.isnan(adx_arr[i]) and adx_arr[i] > thresh)
            # Same rule the backtest uses - never a second copy of it.
            ok = backtest.passes(item.profile, above, adx_ok,
                                 bool(rising_arr[i]), bool(market[i]))
            # The earnings blackout is a separate rule that applies whatever
            # the filter profile is - including "none". Labelling it "filtered"
            # made the Unfiltered profile look like it was filtering.
            blocked_by_earnings = bool(blackout[i]) and ok
            if blackout[i]:
                ok = False
            if blocked_by_earnings:
                color, text = "#9a86d4", "earnings"
            elif ok:
                color, text = "#26a69a", "BUY"
            else:
                color, text = "#5c6b7a", "filtered"
            markers.append({
                "time": times[i], "position": "belowBar",
                "color": color, "shape": "arrowUp", "text": text,
            })
        elif prev_d == -1 and cur_d == 1:
            markers.append({
                "time": times[i], "position": "aboveBar",
                "color": "#ef5350", "shape": "arrowDown", "text": "SELL",
            })

    override = WatchItem(
        ticker=item.ticker, name=item.name, sector=item.sector, profile=item.profile,
        enabled=item.enabled,
        atr_len=atr_len or item.atr_len, atr_mult=atr_mult or item.atr_mult,
        adx_min=thresh, next_earnings=item.next_earnings,
    )
    state = scanner.evaluate(override, df)

    # Leading bars where the 200-day average and ADX do not exist yet. The
    # backtest drops them, so the chart should not offer them as a window
    # either - at Max that made the two tabs appear to start ~200 bars apart.
    usable = (~np.isnan(dir_arr)) & (~np.isnan(sma_arr)) & (~np.isnan(adx_arr))
    warmup = int(np.argmax(usable)) if usable.any() else len(dir_arr)

    # Report dates for the chart's "E" badges. Each goes on the first bar on or
    # after the report date, so a weekend or holiday date still lands on a bar.
    first_bar, last_bar = ind.index[0].date(), ind.index[-1].date()
    report_bars = {}
    for d in earnings:
        if first_bar <= d <= last_bar:
            pos = int(ind.index.searchsorted(pd.Timestamp(d)))
            report_bars.setdefault(times[pos], d.isoformat())
    earnings_marks = [{"time": t, "date": d} for t, d in sorted(report_bars.items())]
    # Next report after the latest bar. The watchlist's next_earnings comes
    # from the latest refresh and is what the readout shows, so it wins; the
    # stored dates are the fallback, since an estimate Yahoo later moved can
    # linger there.
    nxt = None
    if item.next_earnings and item.next_earnings > last_bar:
        nxt = item.next_earnings
    else:
        future = [d for d in earnings if d > last_bar]
        nxt = min(future) if future else None
    upcoming = None
    if nxt:
        upcoming = {"date": nxt.isoformat(),
                    "trading_days": trading_days_until(last_bar, nxt)}

    return {
        "ticker": ticker, "name": item.name or ticker,
        "warmup_bars": warmup,
        "profile": item.profile, "sector": item.sector,
        "adx_min": thresh,
        "state": state,
        "earnings_known": len(earnings),
        "candles": candles, "volume": volume, "markers": markers,
        "supertrend_up": series("supertrend", dir_arr == -1),
        "supertrend_down": series("supertrend", dir_arr == 1),
        "sma": series("sma"),
        "adx": series("adx"),
        "next_earnings": item.next_earnings.isoformat() if item.next_earnings else None,
        "earnings": earnings_marks,
        "upcoming_earnings": upcoming,
    }


@app.get("/api/macd/{ticker}", dependencies=[Depends(auth)])
def macd_series(
    ticker: str,
    fast: int = Query(12, ge=2, le=100),
    slow: int = Query(26, ge=3, le=200),
    signal: int = Query(9, ge=2, le=100),
    bars: int = Query(500, ge=60, le=5000),
    timeframe: str = Query("D", pattern="^[DW]$"),
) -> dict:
    """Optional MACD pane for the chart. Display only - no signal uses it.

    timeframe D works on the daily bars. W works on weekly closes, completed
    weeks only, with each week's value placed on its last trading day - the
    same as TradingView's 1 week timeframe with "Wait for timeframe closes".

    Kept apart from /api/chart so the main chart payload is unchanged and the
    extra series are only sent when the pane is open. Computed over the full
    stored history, then trimmed to the same bars the chart shows, so the
    values match whatever window is loaded.
    """
    if fast >= slow:
        raise HTTPException(422, "fast length must be shorter than slow length")
    ticker = ticker.upper()
    with get_session() as s:
        if s.get(WatchItem, ticker) is None:
            raise HTTPException(404, f"{ticker} is not on the watchlist")

    df = data.load(ticker, years=25)
    if df.empty:
        raise HTTPException(404, f"No price history stored for {ticker}. Run a scan first.")

    calc = macd_weekly if timeframe == "W" else macd_calc
    full = calc(df["close"], fast, slow, signal)
    m = full.tail(bars)
    # For weekly, the last bar a week's value first appeared on - so the pane
    # can say which week it is showing.
    as_of = None
    if timeframe == "W":
        steps = full["macd"].dropna()
        steps = steps[steps.ne(steps.shift())]
        if len(steps):
            as_of = steps.index[-1].strftime("%Y-%m-%d")
    times = m.index.strftime("%Y-%m-%d").tolist()

    def col(name: str) -> list:
        # None for warm-up bars; the dashboard turns those into gaps.
        vals = m[name].to_numpy(dtype=float).round(4)
        return [None if np.isnan(v) else float(v) for v in vals.tolist()]

    return {
        "ticker": ticker, "fast": fast, "slow": slow, "signal": signal,
        "timeframe": timeframe, "as_of": as_of,
        "time": times, "macd": col("macd"), "signal_line": col("signal"),
        "hist": col("hist"),
        # Weekly only: the bars a week closed on, one plotted point per week.
        "week_end": m["week_end"].astype(bool).tolist() if timeframe == "W" else None,
    }


@app.get("/api/backtest/{ticker}", dependencies=[Depends(auth)])
def backtest_ticker(
    ticker: str,
    years: float = Query(5, ge=0.25, le=20),
    bars: int | None = Query(None, ge=60, le=5000),
    atr_len: int | None = Query(None),
    atr_mult: float | None = Query(None),
    adx_min: float | None = Query(None),
) -> dict:
    """Compare every filter profile on one ticker, against buy-and-hold."""
    ticker = ticker.upper()
    with get_session() as s:
        item = s.get(WatchItem, ticker)
    if item is None:
        raise HTTPException(404, f"{ticker} is not on the watchlist")

    # Load everything stored so the SMA is warm before the window starts.
    df = data.load(ticker, years=25)
    if df.empty:
        raise HTTPException(404, f"No price history stored for {ticker}. Run a scan first.")

    result = backtest.run(
        df,
        atr_len or item.atr_len,
        atr_mult or item.atr_mult,
        adx_min if adx_min is not None else item.adx_min,
        years,
        bars,
        earnings=data.load_earnings(ticker),
        market=data.load(backtest.MARKET_TICKER, years=25),
    )
    if "error" in result:
        raise HTTPException(422, result["error"])
    result["ticker"] = ticker
    result["name"] = item.name or ticker
    result["current_profile"] = item.profile
    return result


@app.get("/api/backtest", dependencies=[Depends(auth)])
def backtest_watchlist(years: float = Query(5, ge=0.25, le=20),
                       bars: int | None = Query(None, ge=60, le=5000)) -> dict:
    """Pooled backtest across the enabled watchlist. Slower - runs every ticker."""
    with get_session() as s:
        items = s.execute(select(WatchItem).where(WatchItem.enabled.is_(True))).scalars().all()
        rows = [(i.ticker, i.sector, i.profile, i.atr_len, i.atr_mult, i.adx_min) for i in items]

    _market_df = data.load(backtest.MARKET_TICKER, years=25)
    pooled: dict[str, list] = {p: [] for p in backtest.PROFILES}
    per_ticker, skipped = [], []

    for ticker, sector, profile, a_len, a_mult, a_min in rows:
        df = data.load(ticker, years=25)
        if df.empty:
            skipped.append(ticker)
            continue
        res = backtest.run(df, a_len, a_mult, a_min, years, bars,
                           earnings=data.load_earnings(ticker), market=_market_df)
        if "error" in res:
            skipped.append(ticker)
            continue
        for p in backtest.PROFILES:
            pooled[p].extend(res["profiles"][p]["trades"])
        per_ticker.append({
            "ticker": ticker, "sector": sector, "assigned": profile,
            "best": res["verdict"]["profile"],
            "expectancy": {p: res["profiles"][p]["metrics"]["expectancy"] for p in backtest.PROFILES},
            "trades": {p: res["profiles"][p]["metrics"]["trades"] for p in backtest.PROFILES},
        })

    return {
        "years": years,
        "tested": len(per_ticker),
        "skipped": skipped,
        "pooled": {p: {"label": backtest.LABELS[p], "metrics": backtest.metrics(pooled[p])}
                   for p in backtest.PROFILES},
        "per_ticker": sorted(per_ticker, key=lambda r: r["ticker"]),
        "mismatched": [r["ticker"] for r in per_ticker if r["best"] and r["best"] != r["assigned"]],
    }


@app.get("/api/tune/{ticker}", dependencies=[Depends(auth)])
def tune_ticker(
    ticker: str,
    profile: str | None = Query(None, pattern="^(full|adx_only|adx_rising|regime|none)$"),
    bars: int | None = Query(None, ge=400, le=5000),
) -> dict:
    """Walk-forward ATR search: fitted on the earlier window, scored on the later one."""
    ticker = ticker.upper()
    with get_session() as s:
        item = s.get(WatchItem, ticker)
    if item is None:
        raise HTTPException(404, f"{ticker} is not on the watchlist")

    df = data.load(ticker, years=25)
    if df.empty:
        raise HTTPException(404, f"No price history stored for {ticker}. Run a scan first.")

    result = tuning.tune(
        df, profile or item.profile,
        item.adx_min if item.adx_min is not None else None, bars,
        earnings=data.load_earnings(ticker),
        market=data.load(backtest.MARKET_TICKER, years=25),
        key=ticker,
    )
    if "error" in result:
        raise HTTPException(422, result["error"])
    result["ticker"] = ticker
    result["name"] = item.name or ticker
    result["current"] = {"atr_len": item.atr_len or settings.atr_len,
                         "atr_mult": item.atr_mult or settings.atr_mult}
    return result


@app.post("/api/tune/{ticker}/apply", dependencies=[Depends(auth)])
def apply_tuning(ticker: str, atr_len: int = Query(..., ge=2, le=50),
                 atr_mult: float = Query(..., ge=0.5, le=8)) -> dict:
    """Save tuned ATR settings against one ticker."""
    with get_session() as s:
        item = s.get(WatchItem, ticker.upper())
        if not item:
            raise HTTPException(404, f"{ticker.upper()} is not on the watchlist")
        item.atr_len, item.atr_mult = atr_len, atr_mult
        s.commit()
        return {"ticker": item.ticker, "atr_len": atr_len, "atr_mult": atr_mult}


@app.get("/api/export/grid.csv", dependencies=[Depends(auth)])
def export_grid(years: float = Query(5, ge=1, le=20),
                bars: int | None = Query(None, ge=400, le=5000),
                tickers: str | None = Query(None)) -> StreamingResponse:
    """Every ticker x every filter x every ATR setting, as CSV.

    One row per combination, with the fitting and test windows reported
    separately. Streamed rather than assembled in memory: 39 tickers is
    roughly 3,900 rows.

    The window must match what the Tune tab shows, or the CSV cannot be
    reconciled with the screen. `bars` wins if given; otherwise `years` is
    converted at 252 trading days a year, the same basis the Range control
    uses.
    """
    window_bars = bars if bars is not None else int(round(252 * years))
    with get_session() as s:
        items = s.execute(
            select(WatchItem).where(WatchItem.enabled.is_(True)).order_by(WatchItem.ticker)
        ).scalars().all()
        rows = [(i.ticker, i.name, i.sector, i.adx_min) for i in items]

    if tickers:
        wanted = {t.strip().upper() for t in tickers.split(",") if t.strip()}
        rows = [r for r in rows if r[0] in wanted]

    market = data.load(backtest.MARKET_TICKER, years=25)

    columns = [
        "ticker", "name", "sector", "profile", "profile_label",
        "atr_len", "atr_mult", "adx_min", "cost_pct_per_side", "window_bars",
        "fit_start", "fit_end", "fit_trades", "fit_expectancy",
        "test_start", "test_end", "test_trades", "test_expectancy",
        "test_win_rate", "test_return", "test_max_dd",
        "is_fit_winner", "is_default", "ticker_verdict_held_up",
        "ticker_underpowered", "earnings_dates_known",
    ]

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        yield buf.getvalue()

        for ticker, name, sector, adx_override in rows:
            df = data.load(ticker, years=25)
            if df.empty:
                continue
            earnings = data.load_earnings(ticker)
            # Profiles share indicators for a given ATR pair, so keeping the
            # ticker on the outside keeps those cache entries hot.
            for profile in backtest.PROFILES:
                result = tuning.tune(
                    df, profile, adx_override, window_bars,
                    earnings=earnings, market=market, key=ticker,
                )
                if "error" in result:
                    continue
                chosen = result["chosen"]
                fit_from, fit_to = result["in_sample"].split(" to ")
                test_from, test_to = result["out_sample"].split(" to ")

                buf = io.StringIO()
                writer = csv.writer(buf)
                for r in result["results"]:
                    writer.writerow([
                        ticker, name, sector, profile, backtest.LABELS.get(profile, profile),
                        r["atr_len"], r["atr_mult"],
                        adx_override if adx_override is not None else settings.adx_min,
                        settings.cost_pct_per_side, window_bars,
                        fit_from, fit_to, r["is_trades"], r["is_expectancy"],
                        test_from, test_to, r["oos_trades"], r["oos_expectancy"],
                        r["oos_win_rate"], r["oos_return"], r["oos_max_dd"],
                        int(r["atr_len"] == chosen["atr_len"]
                            and r["atr_mult"] == chosen["atr_mult"]),
                        int(r["atr_len"] == settings.atr_len
                            and r["atr_mult"] == settings.atr_mult),
                        int(result["held_up"]), int(result["underpowered"]),
                        len(earnings),
                    ])
                yield buf.getvalue()

    stamp = date.today().isoformat()
    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="supertrendmoose-grid-{stamp}.csv"'},
    )


@app.get("/api/signals", dependencies=[Depends(auth)])
def signals(days: int = Query(60, ge=1, le=730), passed_only: bool = False) -> list[dict]:
    cutoff = date.fromordinal(date.today().toordinal() - days)
    with get_session() as s:
        q = select(Signal).where(Signal.d >= cutoff).order_by(Signal.d.desc(), Signal.ticker)
        if passed_only:
            q = q.where(Signal.passed.is_(True))
        rows = s.execute(q).scalars().all()
        return [
            {"ticker": r.ticker, "date": r.d.isoformat(), "kind": r.kind, "passed": r.passed,
             "reason": r.reason, "close": r.close, "stop": r.stop, "adx": round(r.adx, 1),
             "atr_pct": round(r.atr_pct, 2), "above_sma": r.above_sma}
            for r in rows
        ]


@app.get("/api/watchlist", dependencies=[Depends(auth)])
def get_watchlist() -> list[dict]:
    with get_session() as s:
        rows = s.execute(select(WatchItem).order_by(WatchItem.sector, WatchItem.ticker)).scalars().all()
        return [
            {"ticker": r.ticker, "name": r.name, "sector": r.sector,
             "profile": r.profile, "enabled": r.enabled,
             "atr_len": r.atr_len, "atr_mult": r.atr_mult, "adx_min": r.adx_min,
             "next_earnings": r.next_earnings.isoformat() if r.next_earnings else None}
            for r in rows
        ]


@app.post("/api/watchlist", dependencies=[Depends(auth)])
def add_watch(item: WatchIn) -> dict:
    t = item.ticker.upper().strip()
    if item.profile not in set(backtest.PROFILES):
        raise HTTPException(400, f"profile must be one of {', '.join(backtest.PROFILES)}")
    with get_session() as s:
        if s.get(WatchItem, t):
            raise HTTPException(409, f"{t} is already on the watchlist")
        s.add(WatchItem(ticker=t, name=item.name or data.fetch_company_name(t),
                        sector=item.sector, profile=item.profile,
                        enabled=item.enabled, atr_len=item.atr_len,
                        atr_mult=item.atr_mult, adx_min=item.adx_min))
        s.commit()
    written, failed = data.refresh([t])
    if t in failed:
        raise HTTPException(502, f"Added {t}, but no price data came back. Check the symbol.")
    return {"ticker": t, "bars": written}


@app.patch("/api/watchlist/{ticker}", dependencies=[Depends(auth)])
def patch_watch(ticker: str, patch: WatchPatch) -> dict:
    with get_session() as s:
        item = s.get(WatchItem, ticker.upper())
        if not item:
            raise HTTPException(404, f"{ticker.upper()} is not on the watchlist")
        for k, v in patch.model_dump(exclude_unset=True).items():
            setattr(item, k, v)
        s.commit()
        return {"ticker": item.ticker, "profile": item.profile, "enabled": item.enabled}


@app.delete("/api/watchlist/{ticker}", dependencies=[Depends(auth)])
def delete_watch(ticker: str) -> dict:
    with get_session() as s:
        item = s.get(WatchItem, ticker.upper())
        if not item:
            raise HTTPException(404, f"{ticker.upper()} is not on the watchlist")
        s.delete(item)
        s.commit()
    return {"deleted": ticker.upper()}


@app.post("/api/watchlist/profile", dependencies=[Depends(auth)])
def set_all_profiles(value: str = Query(..., pattern="^(full|adx_only|adx_rising|regime|none)$")) -> dict:
    """Apply one filter profile to every watchlist ticker."""
    with get_session() as s:
        items = s.execute(select(WatchItem)).scalars().all()
        for item in items:
            item.profile = value
        s.commit()
        return {"profile": value, "updated": len(items)}


def _net_return_pct(p: Position) -> float | None:
    """A closed trade's return after both commissions, on what it cost."""
    if p.exit_date is None or p.exit_price is None:
        return None
    qty = p.quantity or 1.0        # without a quantity, one share is the same %
    cost = qty * p.entry_price + (p.entry_fee or 0.0)
    if cost <= 0:
        return None
    net = qty * p.exit_price - (p.exit_fee or 0.0) - cost
    return round(net / cost * 100, 2)


def _position_json(p: Position) -> dict:
    return {
        "id": p.id, "ticker": p.ticker,
        "entry_date": p.entry_date.isoformat(), "entry_price": p.entry_price,
        "quantity": p.quantity,
        "exit_date": p.exit_date.isoformat() if p.exit_date else None,
        "exit_price": p.exit_price,
        "entry_fee": round(p.entry_fee or 0.0, 2),
        "exit_fee": round(p.exit_fee or 0.0, 2),
        "return_pct": round(p.return_pct, 2) if p.return_pct is not None else None,
        # After commission, which is what the trade really made.
        "net_return_pct": _net_return_pct(p),
        "hold_days": (p.exit_date - p.entry_date).days if p.exit_date else None,
        "open": p.is_open, "note": p.note,
    }


@app.get("/api/positions", dependencies=[Depends(auth)])
def list_positions(ticker: str | None = Query(None),
                   include_closed: bool = Query(True)) -> dict:
    """Trades you recorded, plus how they compare with the backtest."""
    with get_session() as s:
        q = select(Position).order_by(Position.entry_date.desc())
        if ticker:
            q = q.where(Position.ticker == ticker.upper())
        if not include_closed:
            q = q.where(Position.exit_date.is_(None))
        rows = s.execute(q).scalars().all()
        items = [_position_json(p) for p in rows]

    # Oldest sale first: drawdown walks the trades in order, so feeding them
    # newest-first reported no drawdown at all. Returns are net of the
    # commission actually paid, to match the simulated rows, which carry
    # COST_PCT_PER_SIDE, and the Earnings tab.
    # A split makes a held position's recorded price wrong; flag it rather
    # than reporting figures that are quietly out by the split factor.
    open_items = [p for p in items if p["open"]]
    if open_items:
        by_ticker: dict[str, dict] = {}
        for p in open_items:
            if p["ticker"] not in by_ticker:
                df = data.load(p["ticker"])
                by_ticker[p["ticker"]] = ({ts.date(): float(c) for ts, c in df["close"].items()}
                                          if not df.empty else {})
            p["split_suspect"] = splits.detect(
                p["entry_price"], date.fromisoformat(p["entry_date"]),
                p["quantity"], by_ticker[p["ticker"]])

    closed_items = sorted((p for p in items if p["net_return_pct"] is not None),
                          key=lambda p: (p["exit_date"], p["id"]))
    realised = backtest.metrics(
        [{"ret_pct": p["net_return_pct"], "hold_days": p["hold_days"] or 0}
         for p in closed_items]
    )
    return {"positions": items, "open": sum(1 for p in items if p["open"]),
            "realised": realised}


@app.post("/api/positions", dependencies=[Depends(auth)])
def add_position(item: PositionIn) -> dict:
    ticker = item.ticker.upper().strip()
    if item.entry_price <= 0:
        raise HTTPException(400, "entry_price must be positive")
    with get_session() as s:
        if not s.get(WatchItem, ticker):
            raise HTTPException(404, f"{ticker} is not on the watchlist")
        # More than one open lot per ticker is allowed: buying more of
        # something you already hold is a normal thing to do. They are tracked
        # separately and sold oldest first.
        row = Position(ticker=ticker, entry_date=item.entry_date,
                       entry_price=item.entry_price, quantity=item.quantity,
                       entry_fee=item.entry_fee, note=item.note)
        s.add(row)
        s.commit()
        return _position_json(row)


@app.post("/api/positions/{position_id}/close", dependencies=[Depends(auth)])
def close_position(position_id: int, body: PositionClose) -> dict:
    with get_session() as s:
        row = s.get(Position, position_id)
        if not row:
            raise HTTPException(404, f"No position with id {position_id}")
        if not row.is_open:
            raise HTTPException(409, "That position is already closed")
        if body.exit_date < row.entry_date:
            raise HTTPException(400, "exit_date cannot precede entry_date")
        if body.exit_price <= 0:
            raise HTTPException(400, "exit_price must be positive")
        row.exit_date, row.exit_price = body.exit_date, body.exit_price
        row.exit_fee = body.exit_fee
        s.commit()
        return _position_json(row)


class SellIn(BaseModel):
    ticker: Ticker
    exit_date: date
    exit_price: float
    exit_fee: float = Field(0.0, ge=0, le=100_000)
    quantity: float | None = Field(None, gt=0)   # None sells the lot(s) whole


@app.post("/api/positions/sell", dependencies=[Depends(auth)])
def sell_position(body: SellIn) -> dict:
    """Sell some or all of what you hold in a ticker, oldest lot first.

    Selling part of a lot splits it: the sold shares become a closed trade and
    the rest stays open, with the purchase commission shared between them in
    proportion. The sale commission is shared the same way.
    """
    ticker = body.ticker.upper().strip()
    if body.exit_price <= 0:
        raise HTTPException(400, "exit_price must be positive")
    with get_session() as s:
        lots = s.execute(
            select(Position)
            .where(Position.ticker == ticker, Position.exit_date.is_(None))
            .order_by(Position.entry_date, Position.id)
        ).scalars().all()
        if not lots:
            raise HTTPException(404, f"You have no open position in {ticker}")
        if any(l.entry_date > body.exit_date for l in lots if l.quantity is None):
            raise HTTPException(400, "exit_date cannot precede entry_date")

        held = sum(l.quantity or 0.0 for l in lots)
        want = body.quantity
        if want is None:
            want = held if held else None
        elif not held:
            raise HTTPException(400,
                                "That position has no quantity recorded, so it can only be sold whole")
        elif want > held + 1e-9:
            raise HTTPException(400, f"You hold {held:g}, which is less than {want:g}")

        sold, remaining = [], want
        total_qty = want or 0.0
        for lot in lots:
            if remaining is not None and remaining <= 1e-9:
                break
            if lot.entry_date > body.exit_date:
                raise HTTPException(400, "exit_date cannot precede entry_date")
            qty = lot.quantity
            take = qty if (remaining is None or qty is None) else min(qty, remaining)
            # The sale's commission is split over the shares it covers.
            fee_share = (body.exit_fee * (take / total_qty)
                         if total_qty and take is not None else body.exit_fee)
            if qty is None or abs(take - qty) < 1e-9:
                lot.exit_date, lot.exit_price = body.exit_date, body.exit_price
                lot.exit_fee = round(fee_share, 2)
                sold.append(lot)
            else:
                share = take / qty
                part = Position(
                    ticker=lot.ticker, entry_date=lot.entry_date,
                    entry_price=lot.entry_price, quantity=take,
                    entry_fee=round((lot.entry_fee or 0.0) * share, 2),
                    exit_date=body.exit_date, exit_price=body.exit_price,
                    exit_fee=round(fee_share, 2), note=lot.note)
                lot.quantity = qty - take
                lot.entry_fee = round((lot.entry_fee or 0.0) - (part.entry_fee or 0.0), 2)
                s.add(part)
                sold.append(part)
            if remaining is not None and take is not None:
                remaining -= take
        s.commit()
        closed = [_position_json(x) for x in sold]
        left = sum(l.quantity or 0.0 for l in s.execute(
            select(Position).where(Position.ticker == ticker,
                                   Position.exit_date.is_(None))).scalars().all())
    return {"closed": closed, "still_held": round(left, 6)}


@app.get("/api/earnings", dependencies=[Depends(auth)])
def earnings_report(ticker: str | None = Query(None)) -> dict:
    """What the recorded trades earned, after commission.

    Closed trades only, across every ticker: a trade has no result until it
    has both a buy and a sell.
    """
    with get_session() as s:
        q = select(Position)
        if ticker:
            q = q.where(Position.ticker == ticker.upper())
        rows = s.execute(q).scalars().all()

    # Daily closes for the tickers involved, so open positions can be valued
    # every day rather than only when they are sold.
    closes: dict[str, dict] = {}
    for t in {p.ticker for p in rows}:
        df = data.load(t)
        if not df.empty:
            closes[t] = {ts.date(): float(c) for ts, c in df["close"].items()}

    report = earnings.report(rows, closes, settings.starting_cash or None)
    report["open_positions"] = sum(1 for p in rows if p.exit_date is None)
    return report


class SplitIn(BaseModel):
    factor: int = Field(..., ge=2, le=100)
    reverse: bool = False


@app.post("/api/positions/{position_id}/split", dependencies=[Depends(auth)])
def apply_split(position_id: int, body: SplitIn) -> dict:
    """Restate a purchase in post-split shares.

    The money you put in does not change: the price per share is divided by
    the factor and the share count multiplied by it (the other way round for
    a reverse split).
    """
    with get_session() as s:
        row = s.get(Position, position_id)
        if not row:
            raise HTTPException(404, f"No position with id {position_id}")
        if not row.is_open:
            raise HTTPException(409, "A closed trade is already consistent: both its prices "
                                     "are from before the split")
        row.entry_price, row.quantity = splits.apply_factor(
            row.entry_price, row.quantity, body.factor, body.reverse)
        s.commit()
        return _position_json(row)


@app.post("/api/positions/{position_id}/reopen", dependencies=[Depends(auth)])
def reopen_position(position_id: int) -> dict:
    """Undo a sale: the trade goes back to being held.

    For a sale entered by mistake. The purchase is untouched, so the shares
    are held again at the price and date they were bought.
    """
    with get_session() as s:
        row = s.get(Position, position_id)
        if not row:
            raise HTTPException(404, f"No position with id {position_id}")
        if row.is_open:
            raise HTTPException(409, "That position is already open")
        row.exit_date = row.exit_price = None
        row.exit_fee = 0.0
        s.commit()
        return _position_json(row)


@app.get("/api/export/trades.csv", dependencies=[Depends(auth)])
def export_trades(ticker: str | None = Query(None)) -> StreamingResponse:
    """Every recorded order as a row, for a spreadsheet or an accountant.

    One row per buy and per sell, in the same order the Earnings tab lists
    them, with the figures that tab shows. Prices and amounts are plain
    numbers in whatever currency you traded in, with no conversion or tax
    treatment applied: that differs by country, and this is the raw record.
    """
    report = earnings_report(ticker)
    rows = report["orders"]
    trades = {t["id"]: t for t in report["trades"]}
    opens = {t["id"]: t for t in report["open_trades"]}

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "trade_id", "status", "date", "ticker", "side", "quantity", "price",
            "value", "commission", "hold_days", "trade_net", "trade_net_pct",
            "trade_gross", "trade_gross_pct", "entry_date", "entry_price",
            "exit_date", "exit_price", "mark_price", "note",
        ])
        for o in rows:
            t = trades.get(o["trade_id"]) or opens.get(o["trade_id"]) or {}
            writer.writerow([
                o["trade_id"], "open" if o.get("open") else "closed", o["date"],
                o["ticker"], o["side"], o["quantity"], o["price"], o["value"],
                o["commission"], o["hold_days"],
                t.get("net"), t.get("net_pct"), t.get("gross"), t.get("gross_pct"),
                t.get("entry_date"), t.get("entry_price"),
                t.get("exit_date"), t.get("exit_price"), t.get("mark"), t.get("note", ""),
            ])
        yield buf.getvalue()

    stamp = date.today().isoformat()
    name = f"supertrendmoose-trades-{ticker.upper()}-{stamp}" if ticker \
        else f"supertrendmoose-trades-{stamp}"
    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@app.delete("/api/positions/{position_id}", dependencies=[Depends(auth)])
def delete_position(position_id: int) -> dict:
    with get_session() as s:
        row = s.get(Position, position_id)
        if not row:
            raise HTTPException(404, f"No position with id {position_id}")
        s.delete(row)
        s.commit()
    return {"deleted": position_id}


@app.post("/api/refresh-earnings", dependencies=[Depends(auth)])
def manual_refresh_earnings() -> dict:
    """Fetch report dates now, rather than waiting for the weekly job."""
    return scanner.refresh_earnings()


@app.get("/api/earnings-coverage", dependencies=[Depends(auth)])
def earnings_coverage() -> dict:
    """Whether the earnings blackout has any dates to act on."""
    return scanner.earnings_coverage()


@app.post("/api/scan", dependencies=[Depends(auth)])
def manual_scan(refresh_prices: bool = True, send_alerts: bool = False,
                resend: bool = False) -> dict:
    """send_alerts sends signals not yet alerted; resend also repeats ones that were."""
    result = scanner.run_scan(refresh_prices=refresh_prices, notify_on=send_alerts,
                              resend=resend)
    result["buys"] = [b["ticker"] for b in result["buys"]]
    result["exits"] = [e["ticker"] for e in result["exits"]]
    return result


@app.post("/api/test-notification", dependencies=[Depends(auth)])
def test_notification() -> dict:
    return notify.send(
        "SupertrendMoose test",
        f"Notifications are working. Sent {datetime.now():%Y-%m-%d %H:%M}.",
    )


@app.get("/api/runs", dependencies=[Depends(auth)])
def runs(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    with get_session() as s:
        rows = s.execute(select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)).scalars().all()
        return [
            {"started": r.started.isoformat(),
             "finished": r.finished.isoformat() if r.finished else None,
             "tickers": r.tickers, "failed": r.failed, "buys": r.buys,
             "exits": r.exits, "note": r.note}
            for r in rows
        ]


def _asset_version() -> str:
    """Short fingerprint of the front-end assets.

    Without this the browser happily serves a cached app.js after a rebuild,
    so code changes appear not to have taken effect. The value only changes
    when the files do, so normal caching still works.
    """
    digest = hashlib.sha256()
    for name in ("app.js", "style.css", "index.html"):
        path = Path(STATIC_DIR) / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()


@app.get("/")
def index() -> HTMLResponse:
    html = (Path(STATIC_DIR) / "index.html").read_text()
    html = html.replace("/static/app.js", f"/static/app.js?v={ASSET_VERSION}")
    html = html.replace("/static/style.css", f"/static/style.css?v={ASSET_VERSION}")
    # The page itself must never be cached, or it would keep pointing at the
    # old version string.
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
