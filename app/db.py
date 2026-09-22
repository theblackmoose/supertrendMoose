"""Database models. SQLite by default; set DATABASE_URL for Postgres."""
from __future__ import annotations

import logging

from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Float, Integer, String, UniqueConstraint, create_engine, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import settings


class Base(DeclarativeBase):
    pass


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("ticker", "d", name="uq_price"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    d: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class WatchItem(Base):
    __tablename__ = "watchlist"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    sector: Mapped[str] = mapped_column(String(64), default="")
    profile: Mapped[str] = mapped_column(String(16), default="full")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # per-ticker overrides; null means fall back to global settings
    atr_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atr_mult: Mapped[float | None] = mapped_column(Float, nullable=True)
    adx_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_earnings: Mapped[date | None] = mapped_column(Date, nullable=True)


class Signal(Base):
    """One row per Supertrend flip, whether or not the filter passed it."""
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("ticker", "d", "kind", name="uq_signal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    d: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(8))          # BUY | EXIT
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(128), default="")
    close: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)            # supertrend line = initial stop
    adx: Mapped[float] = mapped_column(Float)
    atr_pct: Mapped[float] = mapped_column(Float)
    above_sma: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Position(Base):
    """A trade you actually took.

    Kept separate from Signal: signals are what the scanner saw, positions are
    what you did about them. Comparing the two is the point.
    """
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    entry_date: Mapped[date] = mapped_column(Date)
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Commission actually paid on each order, so the Earnings tab reports what
    # the account did rather than what the prices alone suggest.
    entry_fee: Mapped[float] = mapped_column(Float, default=0.0)
    exit_fee: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(String(256), default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def is_open(self) -> bool:
        return self.exit_date is None

    @property
    def return_pct(self) -> float | None:
        if self.exit_price is None or not self.entry_price:
            return None
        return (self.exit_price / self.entry_price - 1) * 100


class EarningsDate(Base):
    """Historical and upcoming report dates, so the backtest can honour the
    same blackout the live scanner applies."""
    __tablename__ = "earnings_dates"
    __table_args__ = (UniqueConstraint("ticker", "d", name="uq_earnings"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    d: Mapped[date] = mapped_column(Date, index=True)


class Meta(Base):
    """Small key/value notes about the database itself, such as which
    version of the seed list it has already been offered."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), default="")


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tickers: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    buys: Mapped[int] = mapped_column(Integer, default=0)
    exits: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(512), default="")


engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


def get_session() -> Session:
    return Session(engine)


def _ensure_column(table: str, column: str, ddl: str) -> None:
    """Additive migration for databases created before this column existed."""
    from sqlalchemy import text

    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
        else:
            cols = [
                r[0] for r in conn.execute(
                    text("SELECT column_name FROM information_schema.columns "
                         "WHERE table_name = :t"), {"t": table})
            ]
        if cols and column not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


log = logging.getLogger("db")


def init_db() -> None:
    """Create tables, migrate, and seed the watchlist on first run."""
    Base.metadata.create_all(engine)
    _ensure_column("watchlist", "name", "VARCHAR(128) DEFAULT ''")
    # Positions recorded before commission was tracked read as zero fees.
    _ensure_column("positions", "entry_fee", "FLOAT DEFAULT 0")
    _ensure_column("positions", "exit_fee", "FLOAT DEFAULT 0")

    from .watchlist import DEFAULT_WATCHLIST, NAMES, SEED_ADDITIONS, SEED_VERSION

    with get_session() as s:
        fresh = s.scalar(select(WatchItem).limit(1)) is None
        if fresh:
            from .config import settings as _cfg

            s.add_all(
                WatchItem(ticker=w["ticker"], name=NAMES.get(w["ticker"], ""),
                          sector=w["sector"],
                          profile=_cfg.default_profile or w["profile"])
                for w in DEFAULT_WATCHLIST
            )
        else:
            # Backfill names for rows that predate the column.
            for item in s.execute(select(WatchItem)).scalars().all():
                if not item.name and item.ticker in NAMES:
                    item.name = NAMES[item.ticker]
        s.commit()
        _apply_seed_additions(s, fresh)


def _apply_seed_additions(s, fresh: bool) -> None:
    """Offer newly seeded tickers to an install that predates them.

    Only tickers added since this database was last updated, so one you
    deleted on purpose is never resurrected.
    """
    from .watchlist import DEFAULT_WATCHLIST, NAMES, SEED_ADDITIONS, SEED_VERSION

    row = s.get(Meta, "seed_version")
    seen = int(row.value) if row else 1
    if not fresh and seen < SEED_VERSION:
        by_ticker = {w["ticker"]: w for w in DEFAULT_WATCHLIST}
        added = []
        for version in range(seen + 1, SEED_VERSION + 1):
            for ticker in SEED_ADDITIONS.get(version, ()):
                if s.get(WatchItem, ticker) or ticker not in by_ticker:
                    continue
                w = by_ticker[ticker]
                s.add(WatchItem(ticker=ticker, name=NAMES.get(ticker, ""),
                                sector=w["sector"], profile=w["profile"]))
                added.append(ticker)
        if added:
            log.info("added newly seeded tickers: %s", ", ".join(added))
    if row:
        row.value = str(SEED_VERSION)
    else:
        s.add(Meta(key="seed_version", value=str(SEED_VERSION)))
    s.commit()
