"""Price ingest via yfinance.

yfinance is an unofficial scraper, so this layer assumes it will fail
sometimes: small batches, a pause between them, bounded retries, and
incremental top-ups so a failure never costs the whole history.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, select

from . import market
from .config import settings
from .db import Price, get_session

log = logging.getLogger("data")

REQUIRED = ["open", "high", "low", "close", "volume"]

_cache_ready: bool | None = None


def ensure_cache() -> bool:
    """Point yfinance at a writable cache directory.

    The container runs with a read-only root filesystem, so yfinance cannot
    use its default location under HOME. Without a writable cache it cannot
    persist Yahoo's cookie and crumb, every request comes back as HTML rather
    than JSON, and yfinance misreports the result as "possibly delisted".
    """
    global _cache_ready
    if _cache_ready is not None:
        return _cache_ready

    import yfinance as yf

    path = Path(os.getenv("YF_CACHE_DIR", "/tmp/yf-cache"))
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.touch()
        probe.unlink()
        yf.set_tz_cache_location(str(path))
        log.info("yfinance cache at %s", path)
        _cache_ready = True
    except OSError as e:
        log.error(
            "yfinance cache directory %s is not writable (%s). Yahoo requests "
            "will very likely fail. Set YF_CACHE_DIR to a writable path, or "
            "check that the container has a tmpfs mounted at /tmp.", path, e
        )
        _cache_ready = False
    return _cache_ready


def _normalise(df: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        levels = df.columns.get_level_values(-1)
        if ticker in set(levels):
            df = df.xs(ticker, axis=1, level=-1)
        else:
            df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    if not all(c in df.columns for c in REQUIRED):
        return None
    df = df[REQUIRED].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df[~df.index.duplicated(keep="last")]


def download(tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """Download a batch. Returns only the tickers that came back clean."""
    import yfinance as yf

    ensure_cache()

    out: dict[str, pd.DataFrame] = {}
    for attempt in range(1, settings.fetch_retries + 1):
        missing = [t for t in tickers if t not in out]
        if not missing:
            break
        try:
            raw = yf.download(
                missing, start=start, end=end + timedelta(days=1), interval="1d",
                auto_adjust=True, progress=False, threads=False, group_by="ticker",
            )
        except Exception as e:  # noqa: BLE001 - yfinance raises a variety of things
            log.warning("batch failed (attempt %d): %s", attempt, e)
            time.sleep(settings.fetch_pause_sec * attempt * 2)
            continue

        for t in missing:
            try:
                sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) and t in raw.columns.get_level_values(0) else raw
                norm = _normalise(sub.copy(), t)
                # A session still trading is not a finished daily bar.
                norm = market.drop_unfinished(norm)
                if norm is not None and len(norm):
                    out[t] = norm
            except Exception as e:  # noqa: BLE001
                log.warning("%s parse failed: %s", t, e)
        if len(out) < len(tickers):
            time.sleep(settings.fetch_pause_sec * attempt)
    return out


def store(ticker: str, df: pd.DataFrame) -> int:
    """Replace the stored rows for the dates present in df."""
    if df is None or df.empty:
        return 0
    with get_session() as s:
        s.execute(
            delete(Price).where(Price.ticker == ticker, Price.d >= df.index.min().date())
        )
        s.add_all(
            Price(
                ticker=ticker, d=idx.date(), open=float(r.open), high=float(r.high),
                low=float(r.low), close=float(r.close), volume=float(r.volume),
            )
            for idx, r in df.iterrows()
        )
        s.commit()
    invalidate(ticker)
    return len(df)


# Loading dominated the cost of a full scan: building one ORM object per bar,
# then a dict per bar, then a DataFrame - roughly 20ms a ticker, 39 times over.
# Read the whole history once per ticker with pandas, keep it, and slice.
_frames: dict[str, pd.DataFrame] = {}


def invalidate(ticker: str | None = None) -> None:
    """Drop cached bars. Called whenever prices are written."""
    if ticker is None:
        _frames.clear()
    else:
        _frames.pop(ticker, None)


def _full_frame(ticker: str) -> pd.DataFrame:
    cached = _frames.get(ticker)
    if cached is not None:
        return cached

    from sqlalchemy import text

    with get_session() as s:
        df = pd.read_sql(
            text("SELECT d, open, high, low, close, volume FROM prices "
                 "WHERE ticker = :t ORDER BY d"),
            s.connection(), params={"t": ticker},
        )
    if df.empty:
        df = pd.DataFrame(columns=["d", *REQUIRED])
    df["d"] = pd.to_datetime(df["d"])
    df = df.set_index("d")
    _frames[ticker] = df
    return df


def load(ticker: str, years: int | None = None) -> pd.DataFrame:
    """Load stored bars as an OHLCV frame indexed by date."""
    df = _full_frame(ticker)
    if df.empty:
        return pd.DataFrame(columns=REQUIRED)
    cutoff = pd.Timestamp(
        date.today() - timedelta(days=int((years or settings.history_years) * 365.25) + 400)
    )
    return df[df.index >= cutoff] if df.index[0] < cutoff else df


def last_stored_date(ticker: str) -> date | None:
    with get_session() as s:
        return s.scalar(select(Price.d).where(Price.ticker == ticker).order_by(Price.d.desc()).limit(1))


def refresh(tickers: list[str], full: bool = False) -> tuple[int, list[str]]:
    """Top up each ticker to today. Returns (rows written, failed tickers)."""
    # The later of the local and New York dates, so a zone behind or ahead of
    # New York never cuts off the latest finished session.
    today = max(date.today(), market.market_today())
    written, failed = 0, []
    batch = settings.fetch_batch_size

    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
        # Fetch from the oldest start date needed across the chunk
        starts = []
        for t in chunk:
            last = None if full else last_stored_date(t)
            starts.append(
                (last - timedelta(days=5)) if last
                else today - timedelta(days=int(settings.history_years * 365.25) + 400)
            )
        got = download(chunk, min(starts), today)
        for t in chunk:
            if t in got:
                written += store(t, got[t])
            else:
                failed.append(t)
        time.sleep(settings.fetch_pause_sec)

    if failed and not written:
        log.error(
            "Every ticker failed (%d of %d) and no rows were written. This is "
            "almost never a delisting. Usual causes, in order: the yfinance "
            "cache is not writable (see the message above), Yahoo is rate-"
            "limiting this IP, the installed yfinance is too old for Yahoo's "
            "current API, or the container has no outbound network access. "
            "Check with: docker compose exec moose python -c "
            "\"import yfinance,sys; print(yfinance.__version__); "
            "print(yfinance.download('AAPL', period='5d'))\"",
            len(failed), len(tickers),
        )
    elif failed:
        log.warning("refresh complete: %d rows, %d of %d tickers failed: %s",
                    written, len(failed), len(tickers), ", ".join(failed[:10]))
    else:
        log.info("refresh complete: %d rows, all %d tickers ok", written, len(tickers))
    return written, failed


def fetch_next_earnings(ticker: str) -> date | None:
    """Best effort next-earnings lookup. Returns None rather than raising.

    yfinance logs a missing earnings calendar at ERROR level, which is wrong
    for anything that legitimately has none. Quietened for the duration of
    the call so a real problem stays visible.
    """
    import yfinance as yf

    ensure_cache()
    yf_log = logging.getLogger("yfinance")
    previous = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=16)
        if ed is None or ed.empty:
            return None
        idx = pd.DatetimeIndex(ed.index).tz_localize(None).normalize()
        future = sorted(d for d in idx if d.date() >= date.today())
        return future[0].date() if future else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        yf_log.setLevel(previous)


def fetch_earnings_history(ticker: str) -> list[date]:
    """Every report date yfinance knows about, past and upcoming."""
    import yfinance as yf

    ensure_cache()
    yf_log = logging.getLogger("yfinance")
    previous = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=80)
        if ed is None or ed.empty:
            return []
        idx = pd.DatetimeIndex(ed.index).tz_localize(None).normalize()
        return sorted({d.date() for d in idx})
    except Exception:  # noqa: BLE001
        return []
    finally:
        yf_log.setLevel(previous)


def store_earnings(ticker: str, dates: list[date]) -> int:
    from sqlalchemy import select as _select

    if not dates:
        return 0
    from .db import EarningsDate

    with get_session() as s:
        known = {
            d for (d,) in s.execute(
                _select(EarningsDate.d).where(EarningsDate.ticker == ticker)
            ).all()
        }
        added = [d for d in dates if d not in known]
        s.add_all(EarningsDate(ticker=ticker, d=d) for d in added)
        s.commit()
        return len(added)


# A report date Yahoo stops listing is treated as moved, and removed, only when
# Yahoo lists another report within this many days of it. Quarterly reports
# sit about 91 days apart, so a nearby replacement means the estimate changed.
EARNINGS_MOVE_WINDOW = 45


def superseded_earnings(stored, fetched) -> list[date]:
    """Stored report dates that a fresh fetch has replaced with a nearby date.

    A date with no nearby replacement is kept: a gap in Yahoo's list is more
    likely a glitch than a report that never happened, and keeping it only
    means a few extra blocked days.
    """
    fresh = set(fetched)
    if not fresh:
        return []
    window = timedelta(days=EARNINGS_MOVE_WINDOW)
    return sorted(
        d for d in set(stored)
        if d not in fresh and any(abs(f - d) <= window for f in fresh)
    )


def prune_earnings(ticker: str, fetched: list[date]) -> list[date]:
    """Delete stored report dates that a fresh fetch has moved. Returns them."""
    from .db import EarningsDate

    stale = superseded_earnings(load_earnings(ticker), fetched)
    if stale:
        with get_session() as s:
            s.query(EarningsDate).filter(
                EarningsDate.ticker == ticker, EarningsDate.d.in_(stale)
            ).delete(synchronize_session=False)
            s.commit()
    return stale


def load_earnings(ticker: str) -> list[date]:
    from sqlalchemy import select as _select

    from .db import EarningsDate

    with get_session() as s:
        return [
            d for (d,) in s.execute(
                _select(EarningsDate.d).where(EarningsDate.ticker == ticker)
                .order_by(EarningsDate.d)
            ).all()
        ]


def fetch_company_name(ticker: str) -> str:
    """Look up a display name for a ticker added outside the seeded list."""
    from .watchlist import NAMES

    if ticker in NAMES:
        return NAMES[ticker]

    import yfinance as yf

    ensure_cache()
    yf_log = logging.getLogger("yfinance")
    previous = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        info = yf.Ticker(ticker).get_info() or {}
        return str(info.get("longName") or info.get("shortName") or "")
    except Exception:  # noqa: BLE001
        return ""
    finally:
        yf_log.setLevel(previous)
