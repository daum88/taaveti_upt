"""
Market-status detection external port.

Determines whether the NYSE regular session is open, using an exchange
calendar that accounts for US holidays, daylight saving time, and early
closes. A degraded fallback uses New York weekday regular hours when the
calendar is unavailable.
"""

import logging
from datetime import UTC, datetime
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
