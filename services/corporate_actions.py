"""Detect and apply stock splits and cash dividends to portfolio holdings."""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import yfinance as yf

from adapters.sqlite.corporate_actions import CorporateActionStore
from config import CORPORATE_ACTIONS_LOOKBACK_DAYS
from db.money import dec

logger = logging.getLogger(__name__)

_store = CorporateActionStore()


def _lookback_cutoff() -> datetime:
    return datetime.now() - timedelta(days=CORPORATE_ACTIONS_LOOKBACK_DAYS)


def _already_applied(ticker: str, action_type: str, effective_date: str) -> bool:
    return _store.already_applied(ticker, action_type, effective_date)


def check_splits(ticker: str) -> list[dict]:
    try:
        splits = yf.Ticker(ticker).splits
        if splits is None or splits.empty:
            return []
        cutoff = _lookback_cutoff()
        return [
            {"date": day.strftime("%Y-%m-%d"), "ratio": float(ratio)}
            for day, ratio in splits.items()
            if day.to_pydatetime().replace(tzinfo=None) >= cutoff and ratio != 1.0
        ]
    except Exception as error:
        logger.debug("Failed to check splits for %s: %s", ticker, error)
        return []


def apply_split_to_holdings(ticker: str, ratio: float, effective_date: str) -> int:
    result = _store.apply_split(ticker, dec(ratio), effective_date)
    if result.applied:
        action_type = "split" if ratio > 1 else "reverse_split"
        logger.info("Applied %s:1 %s for %s across %s holdings", ratio, action_type, ticker, result.affected_holdings)
    return result.affected_holdings


def check_dividends(ticker: str) -> list[dict]:
    try:
        dividends = yf.Ticker(ticker).dividends
        if dividends is None or dividends.empty:
            return []
        cutoff = _lookback_cutoff()
        return [
            {"date": day.strftime("%Y-%m-%d"), "amount": float(amount)}
            for day, amount in dividends.items()
            if day.to_pydatetime().replace(tzinfo=None) >= cutoff and amount > 0
        ]
    except Exception as error:
        logger.debug("Failed to check dividends for %s: %s", ticker, error)
        return []


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


def _dividend_candidate_tickers() -> list[str]:
    cutoff = _lookback_cutoff().replace(tzinfo=UTC).isoformat()
    return _store.dividend_candidate_tickers(cutoff)


def scan_all_holdings_for_splits() -> int:
    applied = 0
    for ticker in _held_tickers():
        for split in check_splits(ticker):
            if not _already_applied(ticker, "split", split["date"]) and not _already_applied(
                ticker, "reverse_split", split["date"]
            ):
                apply_split_to_holdings(ticker, split["ratio"], split["date"])
                applied += 1
    return applied


def scan_all_holdings_for_dividends() -> int:
    applied = 0
    for ticker in _dividend_candidate_tickers():
        for dividend in check_dividends(ticker):
            if not _already_applied(ticker, "dividend", dividend["date"]):
                apply_dividend_to_entitled_accounts(ticker, dividend["amount"], dividend["date"])
                applied += 1
    return applied


def scan_all_corporate_actions() -> dict:
    return {"splits": scan_all_holdings_for_splits(), "dividends": scan_all_holdings_for_dividends()}
