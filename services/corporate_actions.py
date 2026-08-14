"""Detect and apply stock splits and cash dividends to portfolio holdings."""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import yfinance as yf

from adapters.sqlite.connection import get_db, transaction
from config import CORPORATE_ACTIONS_LOOKBACK_DAYS
from db.money import dec, from_e8, q, to_e8
from models.holding import Holding

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
    ratio_d = dec(ratio)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM holdings WHERE ticker = ? AND quantity_e8 > 0", (ticker.upper(),)
        ).fetchall()

    affected = 0
    for row in rows:
        holding = Holding.get_by_user_and_ticker(row["user_id"], ticker)
        if not holding:
            continue
        holding.quantity = q(holding.quantity * ratio_d)
        holding.average_cost_per_share = q(holding.average_cost_per_share / ratio_d)
        holding.upsert()
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
    logger.info("Applied %s:1 %s for %s across %s holdings", ratio, action_type, ticker, affected)
    return affected


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


def _entitled_balances(conn, ticker: str, cutoff: str):
    return conn.execute(
        """SELECT user_id,
               SUM(CASE transaction_type WHEN 'BUY' THEN quantity_e8 WHEN 'SELL' THEN -quantity_e8 ELSE 0 END) AS quantity_e8
        FROM transactions
        WHERE ticker = ? AND transaction_type IN ('BUY', 'SELL')
          AND datetime(executed_at) < datetime(?)
        GROUP BY user_id
        HAVING SUM(CASE transaction_type WHEN 'BUY' THEN quantity_e8 WHEN 'SELL' THEN -quantity_e8 ELSE 0 END) > 0""",
        (ticker.upper(), cutoff),
    ).fetchall()


def apply_dividend_to_entitled_accounts(ticker: str, amount_per_share: Decimal, ex_date: date | str) -> Decimal:
    """Atomically credit accounts with their net shares immediately before ex-date UTC."""
    amount = dec(amount_per_share)
    effective_date, cutoff = _ex_date_cutoff(ex_date)
    ticker = ticker.upper()
    with transaction() as conn:
        claim = conn.execute(
            """INSERT INTO corporate_actions
                (ticker, action_type, amount_per_share_e8, total_paid_e8, effective_date, applied_to_holdings)
            VALUES (?, 'dividend', ?, 0, ?, 0)
            ON CONFLICT(ticker, action_type, effective_date) DO NOTHING""",
            (ticker, to_e8(amount), effective_date.isoformat()),
        )
        if claim.rowcount == 0:
            return Decimal(0)

        total_paid_e8 = 0
        holders = 0
        for balance in _entitled_balances(conn, ticker, cutoff):
            payout_e8 = to_e8(q(from_e8(balance["quantity_e8"]) * amount))
            if payout_e8 <= 0:
                continue
            account = conn.execute(
                "SELECT id, cash_balance_e8 FROM accounts WHERE user_id = ?", (balance["user_id"],)
            ).fetchone()
            if account is None:
                continue
            cash_before_e8 = account["cash_balance_e8"]
            cash_after_e8 = cash_before_e8 + payout_e8
            conn.execute(
                "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (cash_after_e8, account["id"]),
            )
            conn.execute(
                """INSERT INTO transactions
                (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
                 cash_balance_before_e8, cash_balance_after_e8, llm_reasoning, realized_pnl_e8, executed_at)
                VALUES (?, ?, 'DIVIDEND', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    balance["user_id"],
                    ticker,
                    balance["quantity_e8"],
                    to_e8(amount),
                    payout_e8,
                    cash_before_e8,
                    cash_after_e8,
                    f"Cash dividend ${amount}/share (ex-date {effective_date.isoformat()})",
                    payout_e8,
                    datetime.now(UTC).isoformat(),
                ),
            )
            total_paid_e8 += payout_e8
            holders += 1
        conn.execute(
            """UPDATE corporate_actions SET total_paid_e8 = ?, applied_to_holdings = 1
            WHERE ticker = ? AND action_type = 'dividend' AND effective_date = ?""",
            (total_paid_e8, ticker, effective_date.isoformat()),
        )
    total = from_e8(total_paid_e8)
    logger.info("Paid $%s/share dividend for %s to %s holders (total $%s)", amount, ticker, holders, total)
    return total


def apply_dividend_to_holdings(ticker: str, amount_per_share, effective_date: str) -> Decimal:
    """Compatibility alias for historical ex-date entitlement processing."""
    return apply_dividend_to_entitled_accounts(ticker, dec(amount_per_share), effective_date)


def reverse_erroneous_dividend(original_transaction_id: int) -> bool:
    """Reverse one erroneous dividend with an immutable, idempotent audit entry."""
    with transaction() as conn:
        original = conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND transaction_type = 'DIVIDEND'", (original_transaction_id,)
        ).fetchone()
        if original is None:
            raise ValueError(f"Dividend transaction {original_transaction_id} does not exist")
        if conn.execute(
            "SELECT 1 FROM dividend_reversals WHERE original_transaction_id = ?", (original_transaction_id,)
        ).fetchone():
            return False
        account = conn.execute(
            "SELECT id, cash_balance_e8 FROM accounts WHERE user_id = ?", (original["user_id"],)
        ).fetchone()
        amount_e8 = original["total_value_e8"]
        if account is None or account["cash_balance_e8"] < amount_e8:
            raise ValueError(f"Account cannot fund reversal of dividend transaction {original_transaction_id}")
        cash_after_e8 = account["cash_balance_e8"] - amount_e8
        conn.execute(
            "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (cash_after_e8, account["id"]),
        )
        reversal = conn.execute(
            """INSERT INTO transactions
            (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
             cash_balance_before_e8, cash_balance_after_e8, llm_reasoning, realized_pnl_e8, executed_at)
            VALUES (?, ?, 'DIVIDEND_REVERSAL', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                original["user_id"],
                original["ticker"],
                original["quantity_e8"],
                original["price_per_share_e8"],
                -amount_e8,
                account["cash_balance_e8"],
                cash_after_e8,
                f"Reversal of erroneous dividend transaction #{original_transaction_id}",
                -amount_e8,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO dividend_reversals (original_transaction_id, reversal_transaction_id) VALUES (?, ?)",
            (original_transaction_id, reversal.lastrowid),
        )
    return True


def _held_tickers() -> list[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0").fetchall()
    return [row["ticker"] for row in rows]


def _dividend_candidate_tickers() -> list[str]:
    cutoff = _lookback_cutoff().replace(tzinfo=UTC).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0
            UNION SELECT DISTINCT ticker FROM transactions
            WHERE transaction_type IN ('BUY', 'SELL') AND datetime(executed_at) >= datetime(?)""",
            (cutoff,),
        ).fetchall()
    return [row["ticker"] for row in rows]


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
