"""Quote plausibility gate.

Provider glitches (stale quote fields, undelivered price adjustments) can produce
quotes wildly disconnected from the last known price — e.g. fast_info mixing a
pre-split last_price with a post-split previous_close, which once surfaced as a
phantom +179% move that agents traded on.

Quotes moving more than ``Settings.quote_anomaly_threshold`` from their reference
(latest persisted snapshot, falling back to the latest cached daily close) are
verified against recently detected corporate actions: a split whose ratio matches
the implied move confirms the transition and is recorded exactly once (pre-split
holdings and cached history adjusted); anything unexplained is quarantined instead
of poisoning price snapshots, the volatility filter, and agent decisions.

Quarantine is fail-closed: when corporate-action lookup degrades, the suspect
quote is dropped for the cycle rather than trusted. A genuine move beyond the
threshold in a large-cap universe is rare and surfaces loudly in the logs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from adapters.market_data.yfinance_corporate_actions import CorporateActions, fetch_recent_actions
from adapters.sqlite.funnel import FunnelStore
from adapters.sqlite.market_features import MarketFeatureStore
from services.corporate_actions import record_split
from settings import Settings, load_settings

logger = logging.getLogger(__name__)

_RATIO_TOLERANCE = 0.15


def quarantine_suspect_quotes(
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    settings: Settings | None = None,
    action_fetcher: Callable[..., CorporateActions] | None = None,
) -> dict[str, dict]:
    """Return the quotes that pass the plausibility gate, recording confirmed splits."""
    configuration = settings or load_settings()
    fetcher = action_fetcher or fetch_recent_actions
    priced = {ticker.upper(): dict(quote) for ticker, quote in quotes.items() if quote.get("price")}
    if not priced:
        return {}
    references = _reference_prices(priced.keys())
    accepted: dict[str, dict] = {}
    for ticker, quote in priced.items():
        reference = references.get(ticker)
        price = float(quote["price"])
        if reference is None or reference <= 0:
            accepted[ticker] = quote
            continue
        move = (price - reference) / reference
        if abs(move) <= configuration.quote_anomaly_threshold:
            accepted[ticker] = quote
            continue
        if _confirm_split(ticker, reference, price, settings=configuration, action_fetcher=fetcher):
            accepted[ticker] = quote
        else:
            logger.warning(
                "Quarantining %s quote %.4f: %+.1f%% vs last known %.4f with no matching corporate action",
                ticker,
                price,
                move * 100,
                reference,
            )
    return accepted


def _reference_prices(tickers: Iterable[str]) -> dict[str, float]:
    ordered = sorted({ticker.upper() for ticker in tickers})
    references = FunnelStore().latest_prices(ordered)
    missing = [ticker for ticker in ordered if ticker not in references]
    if missing:
        for ticker, close in MarketFeatureStore().latest_closes(missing).items():
            references.setdefault(ticker, close)
    return references


def _confirm_split(
    ticker: str,
    reference: float,
    price: float,
    *,
    settings: Settings,
    action_fetcher: Callable[..., CorporateActions],
) -> bool:
    since = datetime.now() - timedelta(days=settings.corporate_actions_lookback_days)
    actions = action_fetcher(ticker, since=since)
    implied_ratio = reference / price
    for split in actions.splits:
        if abs(implied_ratio - split.ratio) / split.ratio <= _RATIO_TOLERANCE:
            record_split(ticker, split.ratio, split.effective_date)
            logger.info(
                "Confirmed %s split %.4g:1 effective %s explains %+.1f%% quote move; quote accepted",
                ticker,
                split.ratio,
                split.effective_date,
                (price - reference) / reference * 100,
            )
            return True
    return False
