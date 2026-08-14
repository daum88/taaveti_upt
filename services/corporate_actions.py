"""Detect and apply stock splits and cash dividends to portfolio holdings."""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from adapters.market_data.yfinance_corporate_actions import fetch_recent_actions
from adapters.sqlite.corporate_actions import CorporateActionStore
from db.money import dec
from settings import Settings, load_settings

logger = logging.getLogger(__name__)

_store = CorporateActionStore()


def _lookback_cutoff(settings: Settings) -> datetime:
    return datetime.now() - timedelta(days=settings.corporate_actions_lookback_days)


def _already_applied(ticker: str, action_type: str, effective_date: str) -> bool:
    return _store.already_applied(ticker, action_type, effective_date)


def check_splits(ticker: str, *, settings: Settings | None = None) -> list[dict]:
    configuration = settings or load_settings()
    actions = fetch_recent_actions(ticker, since=_lookback_cutoff(configuration))
    return [{"date": split.effective_date, "ratio": split.ratio} for split in actions.splits]


def apply_split_to_holdings(ticker: str, ratio: float, effective_date: str) -> int:
    result = _store.apply_split(ticker, dec(ratio), effective_date)
    if result.applied:
        action_type = "split" if ratio > 1 else "reverse_split"
        logger.info("Applied %s:1 %s for %s across %s holdings", ratio, action_type, ticker, result.affected_holdings)
    return result.affected_holdings


def check_dividends(ticker: str, *, settings: Settings | None = None) -> list[dict]:
    configuration = settings or load_settings()
    actions = fetch_recent_actions(ticker, since=_lookback_cutoff(configuration))
    return [{"date": dividend.ex_date, "amount": dividend.amount_per_share} for dividend in actions.dividends]


def _ex_date_cutoff(ex_date: date | str) -> tuple[date, str]:
    parsed = date.fromisoformat(ex_date) if isinstance(ex_date, str) else ex_date
    return parsed, datetime.combine(parsed, time.min, UTC).isoformat()


def apply_dividend_to_entitled_accounts(ticker: str, amount_per_share: Decimal, ex_date: date | str) -> Decimal:
    """Atomically credit accounts with their net shares immediately before ex-date UTC."""
    amount = dec(amount_per_share)
    effective_date, cutoff = _ex_date_cutoff(ex_date)
    result = _store.apply_dividend(
        ticker,
        amount,
        effective_date.isoformat(),
        cutoff,
        datetime.now(UTC).isoformat(),
    )
    if result.applied:
        logger.info(
            "Paid $%s/share dividend for %s to %s holders (total $%s)",
            amount,
            ticker,
            result.holder_count,
            result.total_paid,
        )
    return result.total_paid


def apply_dividend_to_holdings(ticker: str, amount_per_share, effective_date: str) -> Decimal:
    """Compatibility alias for historical ex-date entitlement processing."""
    return apply_dividend_to_entitled_accounts(ticker, dec(amount_per_share), effective_date)


def reverse_erroneous_dividend(original_transaction_id: int) -> bool:
    """Reverse one erroneous dividend with an immutable, idempotent audit entry."""
    return _store.reverse_dividend(original_transaction_id, datetime.now(UTC).isoformat())


def _held_tickers() -> list[str]:
    return _store.held_tickers()


def _dividend_candidate_tickers(settings: Settings) -> list[str]:
    cutoff = _lookback_cutoff(settings).replace(tzinfo=UTC).isoformat()
    return _store.dividend_candidate_tickers(cutoff)


def scan_all_holdings_for_splits(*, settings: Settings | None = None) -> int:
    configuration = settings or load_settings()
    applied = 0
    for ticker in _held_tickers():
        for split in check_splits(ticker, settings=configuration):
            if not _already_applied(ticker, "split", split["date"]) and not _already_applied(
                ticker, "reverse_split", split["date"]
            ):
                apply_split_to_holdings(ticker, split["ratio"], split["date"])
                applied += 1
    return applied


def scan_all_holdings_for_dividends(*, settings: Settings | None = None) -> int:
    configuration = settings or load_settings()
    applied = 0
    for ticker in _dividend_candidate_tickers(configuration):
        for dividend in check_dividends(ticker, settings=configuration):
            if not _already_applied(ticker, "dividend", dividend["date"]):
                apply_dividend_to_entitled_accounts(ticker, dividend["amount"], dividend["date"])
                applied += 1
    return applied


def scan_all_corporate_actions(*, settings: Settings | None = None) -> dict:
    configuration = settings or load_settings()
    return {
        "splits": scan_all_holdings_for_splits(settings=configuration),
        "dividends": scan_all_holdings_for_dividends(settings=configuration),
    }
