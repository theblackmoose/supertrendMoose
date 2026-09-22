"""US market timing: when a session is finished, and when to scan.

Everything here works in New York time, because that is where the market
keeps its hours. The user's time zone (TZ) only decides how times are shown
and logged. Scheduling against New York means daylight saving on either side
never moves a scan to before the close.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("market")

NY = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
# Yahoo's daily bar can still move for a few minutes after the close.
SETTLE = timedelta(minutes=15)

# The schedule before the scan followed the market: 07:30 Tue-Sat local.
LEGACY_DEFAULT = (7, 30, "tue-sat")
MIN_DELAY, MAX_DELAY = 15, 600


def zone(name: str) -> ZoneInfo:
    """The named IANA zone, or UTC with an error logged if it is not one."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        log.error("TZ=%r is not a time zone name (e.g. Australia/Melbourne); using UTC", name)
        return ZoneInfo("UTC")


def now_ny() -> datetime:
    return datetime.now(NY)


def market_today(now: datetime | None = None) -> date:
    """The current date in New York."""
    return (now or now_ny()).astimezone(NY).date()


def session_complete(d: date, now: datetime | None = None) -> bool:
    """Has the session dated d closed and settled?"""
    now = (now or now_ny()).astimezone(NY)
    return now >= datetime.combine(d, CLOSE, NY) + SETTLE


def drop_unfinished(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Remove a bar for a session that has not closed yet.

    Yahoo returns today's bar while the market is open, with a price that is
    still moving. Stored, it would produce signals from a half-finished day.
    """
    if df is None or df.empty:
        return df
    keep = [session_complete(ts.date(), now) for ts in df.index]
    if all(keep):
        return df
    return df[keep]


def last_completed_session(now: datetime | None = None) -> date:
    """The most recent US session that has closed and settled.

    Weekends and the full-day holidays in _market_closed are skipped, so this
    is what the stored prices should reach.
    """
    from .indicators import _market_closed

    d = market_today(now)
    for _ in range(14):
        if d.weekday() < 5 and not _market_closed(d) and session_complete(d, now):
            return d
        d -= timedelta(days=1)
    return d


def sessions_behind(latest: date | None, now: datetime | None = None) -> int | None:
    """How many finished sessions the stored data is missing."""
    from .indicators import trading_days_until

    if latest is None:
        return None
    expected = last_completed_session(now)
    return trading_days_until(latest, expected) if latest < expected else 0


class Schedule:
    """The nightly scan trigger and a plain description of it."""

    def __init__(self, trigger: CronTrigger, description: str, mode: str,
                 warning: str = "", note: str = ""):
        self.trigger = trigger
        self.description = description
        self.mode = mode
        self.warning = warning   # a real problem with the chosen schedule
        self.note = note         # something worth knowing, not a problem


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def market_schedule(delay_min: int) -> Schedule:
    """Mon-Fri, delay_min minutes after the 16:00 New York close."""
    if not MIN_DELAY <= delay_min <= MAX_DELAY:
        clamped = min(max(delay_min, MIN_DELAY), MAX_DELAY)
        log.warning("SCAN_AFTER_CLOSE_MINUTES=%s is outside %d-%d; using %d",
                    delay_min, MIN_DELAY, MAX_DELAY, clamped)
        delay_min = clamped
    total = CLOSE.hour * 60 + CLOSE.minute + delay_min
    days = "mon-fri"
    if total >= 24 * 60:          # past midnight New York: the next day
        total -= 24 * 60
        days = "tue-sat"
    hour, minute = divmod(total, 60)
    trigger = CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=NY)
    desc = (f"{delay_min} min after each US close "
            f"({days} {hour:02d}:{minute:02d} New York time)")
    return Schedule(trigger, desc, "market")


def fixed_schedule(hour: int, minute: int, days: str, tz: ZoneInfo) -> Schedule:
    """A fixed local time, checked against the close for the next year."""
    trigger = CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=tz)
    desc = f"{days} {hour:02d}:{minute:02d} {tz.key} (fixed time)"
    early = first_early_run(trigger)
    warning = ""
    if early:
        ny = early.astimezone(NY)
        warning = (f"the fixed scan time can run before the US close has settled, "
                   f"e.g. {early:%a %d %b %Y %H:%M} {tz.key} = {ny:%H:%M} New York. "
                   f"Remove SCAN_TIME to scan after every close automatically.")
    return Schedule(trigger, desc, "fixed", warning)


def first_early_run(trigger: CronTrigger, start: datetime | None = None,
                    days: int = 400) -> datetime | None:
    """First run in the coming period that lands on a US weekday between
    the open and the settled close, when that day's bar is still moving."""
    now = start or datetime.now(NY)
    end = now + timedelta(days=days)
    prev = None
    run = trigger.get_next_fire_time(None, now)
    while run is not None and run < end:
        ny = run.astimezone(NY)
        settled = (datetime.combine(ny.date(), CLOSE, NY) + SETTLE).time()
        if ny.weekday() < 5 and OPEN <= ny.time() < settled:
            return run
        prev = run
        run = trigger.get_next_fire_time(prev, run + timedelta(seconds=1))
    return None


def build_schedule(settings, tz: ZoneInfo) -> Schedule:
    """Pick the scan schedule from the settings.

    SCAN_TIME (HH:MM, local) gives a fixed time. The older SCAN_HOUR and
    SCAN_MINUTE still work the same way, except that their old default of
    07:30 Tue-Sat, which ran before the close for part of every year, is
    moved to the market schedule.
    """
    days = settings.scan_days or LEGACY_DEFAULT[2]
    if settings.scan_time:
        parsed = _parse_hhmm(settings.scan_time)
        if parsed:
            return fixed_schedule(*parsed, days, tz)
        log.error("SCAN_TIME=%r is not HH:MM; scanning after each close instead",
                  settings.scan_time)
    elif settings.scan_hour_raw or settings.scan_minute_raw:
        try:
            hour = int(settings.scan_hour_raw or LEGACY_DEFAULT[0])
            minute = int(settings.scan_minute_raw or LEGACY_DEFAULT[1])
        except ValueError:
            log.error("SCAN_HOUR/SCAN_MINUTE are not numbers; scanning after each close")
        else:
            if (hour, minute, days) != LEGACY_DEFAULT and 0 <= hour <= 23 and 0 <= minute <= 59:
                return fixed_schedule(hour, minute, days, tz)
            if (hour, minute, days) == LEGACY_DEFAULT:
                sched = market_schedule(settings.scan_after_close_min)
                sched.note = ("SCAN_HOUR=7, SCAN_MINUTE=30 and SCAN_DAYS=tue-sat are the "
                                 "old default, which ran before the US close from November "
                                 "to March. Scanning after each close instead; those lines "
                                 "can be removed from .env.")
                return sched
            log.error("SCAN_HOUR/SCAN_MINUTE out of range; scanning after each close")
    return market_schedule(settings.scan_after_close_min)
