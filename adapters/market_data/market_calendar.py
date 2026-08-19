"""
Market-status detection external port.

Determines whether the NYSE regular session is open, using an exchange
calendar that accounts for US holidays, daylight saving time, and early
closes. A degraded fallback uses New York weekday regular hours when the
calendar is unavailable.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

logger = logging.getLogger(__name__)

NYSE_CALENDAR = xcals.get_calendar("XNYS")
NEW_YORK = ZoneInfo("America/New_York")


def is_market_open(now: datetime | None = None) -> bool:
    """Return whether the NYSE regular session is open at ``now``.

    The exchange calendar accounts for US holidays, daylight saving time, and
    early closes. If calendar evaluation is unavailable, the degraded fallback
    uses New York weekday regular hours but cannot identify exchange holidays
    or early closes.
    """
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("Market-status time must be timezone-aware")
    current_time = current_time.astimezone(UTC)
    try:
        return NYSE_CALENDAR.is_open_on_minute(current_time, ignore_breaks=True)
    except Exception as error:
        logger.warning("NYSE calendar unavailable; using weekday-hours fallback: %s", error)
        eastern_time = current_time.astimezone(NEW_YORK)
        if eastern_time.weekday() >= 5:
            return False
        session_start = eastern_time.replace(hour=9, minute=30, second=0, microsecond=0)
        session_end = eastern_time.replace(hour=16, minute=0, second=0, microsecond=0)
        return session_start <= eastern_time < session_end


def latest_completed_session(now: datetime | None = None) -> date | None:
    """Return the most recent NYSE session whose official close has passed.

    Quote providers publish end-of-day bars per symbol with varying lag, so a
    daily-bar download taken while the market is closed may still lack the
    latest session. Callers must treat quotes older than this date as stale.
    The degraded fallback uses New York weekday hours and cannot identify
    exchange holidays or early closes.
    """
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("Market-status time must be timezone-aware")
    current_time = current_time.astimezone(UTC)
    try:
        sessions = NYSE_CALENDAR.sessions_in_range((current_time - timedelta(days=21)).date(), current_time.date())
        for session in reversed(sessions):
            if NYSE_CALENDAR.session_close(session) <= current_time:
                return session.date()
        return None
    except Exception as error:
        logger.warning("NYSE calendar unavailable; using weekday-hours fallback: %s", error)
        eastern_time = current_time.astimezone(NEW_YORK)
        day = eastern_time.date()
        if eastern_time.weekday() < 5 and eastern_time.time() >= time(16, 0):
            return day
        day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day
