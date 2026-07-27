"""
Corporate Actions Service — detects and applies stock splits and cash
dividends to portfolio holdings.

Splits adjust share quantity and cost basis in place. Cash dividends credit
each holder's account by (shares * amount_per_share) and are recorded in the
``corporate_actions`` ledger. Each action is applied at most once, keyed by
(ticker, action_type, effective_date).
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import yfinance as yf

from config import CORPORATE_ACTIONS_LOOKBACK_DAYS
from db.connection import get_db
from db.money import to_e8, dec, q
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction

logger = logging.getLogger(__name__)


def _lookback_cutoff() -> datetime:
    return datetime.now() - timedelta(days=CORPORATE_ACTIONS_LOOKBACK_DAYS)


def _already_applied(ticker: str, action_type: str, effective_date: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """SELECT id FROM corporate_actions
               WHERE ticker = ? AND action_type = ? AND effective_date = ?
                 AND applied_to_holdings = 1""",
            (ticker.upper(), action_type, effective_date),
        ).fetchone()
    return row is not None


# ══════════════════════════════════════════════════════════
#  Splits
# ══════════════════════════════════════════════════════════

def check_splits(ticker: str) -> list[dict]:
    """Recent stock splits (last CORPORATE_ACTIONS_LOOKBACK_DAYS days).

    Returns list of {date, ratio}.
    """
    try:
        splits = yf.Ticker(ticker).splits
        if splits is None or splits.empty:
            return []
        cutoff = _lookback_cutoff()
        return [
            {"date": date.strftime("%Y-%m-%d"), "ratio": float(ratio)}
            for date, ratio in splits.items()
            if date.to_pydatetime().replace(tzinfo=None) >= cutoff and ratio != 1.0
        ]
    except Exception as e:
        logger.debug(f"Failed to check splits for {ticker}: {e}")
        return []


def apply_split_to_holdings(ticker: str, ratio: float, effective_date: str) -> int:
    """Adjust all holders' quantity/cost-basis for a split. Returns holders affected.

    Forward split (ratio > 1): shares * ratio, cost / ratio.
    Reverse split (ratio < 1): same maths.
    """
    ratio_d = dec(ratio)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM holdings WHERE ticker = ? AND quantity_e8 > 0",
            (ticker.upper(),),
        ).fetchall()

    affected = 0
    for row in rows:
        h = Holding.get_by_user_and_ticker(row["user_id"], ticker)
        if not h:
            continue
        h.quantity = q(h.quantity * ratio_d)
        h.average_cost_per_share = q(h.average_cost_per_share / ratio_d)
        h.upsert()
        affected += 1

    action_type = "split" if ratio > 1 else "reverse_split"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO corporate_actions
                   (ticker, action_type, ratio, effective_date, applied_to_holdings)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(ticker, action_type, effective_date)
               DO UPDATE SET applied_to_holdings = 1, ratio = excluded.ratio""",
            (ticker.upper(), action_type, float(ratio), effective_date),
        )

    logger.info(f"Applied {ratio}:1 {action_type} for {ticker} across {affected} holdings")
    return affected


# ══════════════════════════════════════════════════════════
#  Dividends
# ══════════════════════════════════════════════════════════

def check_dividends(ticker: str) -> list[dict]:
    """Recent cash dividends (last CORPORATE_ACTIONS_LOOKBACK_DAYS days).

    Returns list of {date, amount} where amount is cash per share.
    """
    try:
        divs = yf.Ticker(ticker).dividends
        if divs is None or divs.empty:
            return []
        cutoff = _lookback_cutoff()
        return [
            {"date": date.strftime("%Y-%m-%d"), "amount": float(amount)}
            for date, amount in divs.items()
            if date.to_pydatetime().replace(tzinfo=None) >= cutoff and amount > 0
        ]
    except Exception as e:
        logger.debug(f"Failed to check dividends for {ticker}: {e}")
        return []


def apply_dividend_to_holdings(ticker: str, amount_per_share, effective_date: str) -> Decimal:
    """Credit every holder's cash account by (shares * amount_per_share).

    Returns total cash distributed across all holders.
    """
    amount = dec(amount_per_share)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM holdings WHERE ticker = ? AND quantity_e8 > 0",
            (ticker.upper(),),
        ).fetchall()

    total_paid = Decimal(0)
    holders = 0
    for row in rows:
        h = Holding.get_by_user_and_ticker(row["user_id"], ticker)
        if not h or h.quantity <= 0:
            continue
        account = Account.get_by_user_id(row["user_id"])
        if not account:
            continue
        payout = q(h.quantity * amount)
        if payout <= 0:
            continue
        cash_before = account.cash_balance
        account.credit(payout)
        Transaction.create(
            user_id=row["user_id"],
            ticker=ticker,
            transaction_type="DIVIDEND",
            quantity=h.quantity,
            price_per_share=amount,
            total_value=payout,
            cash_balance_before=cash_before,
            cash_balance_after=account.cash_balance,
            llm_reasoning=f"Cash dividend ${amount}/share (ex-date {effective_date})",
            realized_pnl=payout,
        )
        total_paid += payout
        holders += 1

    total_paid = q(total_paid)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO corporate_actions
                   (ticker, action_type, amount_per_share_e8, total_paid_e8,
                    effective_date, applied_to_holdings)
               VALUES (?, 'dividend', ?, ?, ?, 1)
               ON CONFLICT(ticker, action_type, effective_date)
               DO UPDATE SET applied_to_holdings = 1,
                             amount_per_share_e8 = excluded.amount_per_share_e8,
                             total_paid_e8 = excluded.total_paid_e8""",
            (ticker.upper(), to_e8(amount), to_e8(total_paid), effective_date),
        )

    logger.info(
        f"Paid ${amount}/share dividend for {ticker} to {holders} holders "
        f"(total ${total_paid})"
    )
    return total_paid


# ══════════════════════════════════════════════════════════
#  Scanners
# ══════════════════════════════════════════════════════════

def _held_tickers() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0"
        ).fetchall()
    return [r["ticker"] for r in rows]


def scan_all_holdings_for_splits() -> int:
    """Detect and apply recent splits across all held tickers. Returns count applied."""
    applied = 0
    for ticker in _held_tickers():
        for split in check_splits(ticker):
            if not _already_applied(ticker, "split", split["date"]) and \
               not _already_applied(ticker, "reverse_split", split["date"]):
                apply_split_to_holdings(ticker, split["ratio"], split["date"])
                applied += 1
    return applied


def scan_all_holdings_for_dividends() -> int:
    """Detect and pay recent dividends across all held tickers. Returns count applied."""
    applied = 0
    for ticker in _held_tickers():
        for div in check_dividends(ticker):
            if not _already_applied(ticker, "dividend", div["date"]):
                apply_dividend_to_holdings(ticker, div["amount"], div["date"])
                applied += 1
    return applied


def scan_all_corporate_actions() -> dict:
    """Scan held tickers for splits and dividends and apply them.

    Returns {"splits": n, "dividends": m}.
    """
    return {
        "splits": scan_all_holdings_for_splits(),
        "dividends": scan_all_holdings_for_dividends(),
    }
