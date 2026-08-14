"""Corporate-action lookup via yfinance.

This true-external adapter hides yfinance's split/dividend series, provider
failures, and date normalization behind one immutable result for a ticker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockSplit:
    effective_date: str
    ratio: float


@dataclass(frozen=True)
class CashDividend:
    ex_date: str
    amount_per_share: float


@dataclass(frozen=True)
class CorporateActions:
    splits: tuple[StockSplit, ...]
    dividends: tuple[CashDividend, ...]


def fetch_recent_actions(ticker: str, *, since: datetime) -> CorporateActions:
    """Return valid split and dividend events at or after ``since``.

    Provider and payload failures degrade to an empty result. Dates are
    normalized to ISO calendar dates, so callers do not depend on pandas or
    yfinance index types.
    """
    try:
        instrument = yf.Ticker(ticker)
        return CorporateActions(
            splits=_splits(instrument.splits, since),
            dividends=_dividends(instrument.dividends, since),
        )
    except Exception as error:
        logger.debug("Failed to fetch corporate actions for %s: %s", ticker, error)
        return CorporateActions((), ())


def _splits(series, since: datetime) -> tuple[StockSplit, ...]:
    if series is None or series.empty:
        return ()
    return tuple(
        StockSplit(day.strftime("%Y-%m-%d"), float(ratio))
        for day, ratio in series.items()
        if _is_on_or_after(day, since) and ratio != 1.0
    )


def _dividends(series, since: datetime) -> tuple[CashDividend, ...]:
    if series is None or series.empty:
        return ()
    return tuple(
        CashDividend(day.strftime("%Y-%m-%d"), float(amount))
        for day, amount in series.items()
        if _is_on_or_after(day, since) and amount > 0
    )


def _is_on_or_after(day, since: datetime) -> bool:
    return day.to_pydatetime().replace(tzinfo=None) >= since
