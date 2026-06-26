"""
Corporate Actions Service — detects and applies stock splits,
dividends, and other corporate actions to portfolio holdings.
"""

import logging
from datetime import datetime, timedelta

import yfinance as yf

from db.connection import get_db
from models.holding import Holding

logger = logging.getLogger(__name__)


def check_splits(ticker: str) -> list[dict]:
    """
    Check for any stock splits for a ticker in the last 30 days.
    Returns list of split events: [{date, ratio}]
    """
    try:
        t = yf.Ticker(ticker)
        splits = t.splits
        if splits is None or splits.empty:
            return []

        cutoff = datetime.now() - timedelta(days=30)
        recent = []
        for date, ratio in splits.items():
            if date.to_pydatetime() >= cutoff and ratio != 1.0:
                recent.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "ratio": float(ratio),
                })
        return recent
    except Exception as e:
        logger.debug(f"Failed to check splits for {ticker}: {e}")
        return []


def apply_split_to_holdings(ticker: str, ratio: float):
    """
    Adjust all users' holdings for a stock split.
    ratio > 1 = forward split (e.g., 10:1 → ratio=10 → multiply shares, divide cost)
    ratio < 1 = reverse split
    """
    holdings = Holding.all_for_user(0)  # placeholder
    with get_db() as conn:
        rows = conn.execute(
            "SELECT h.* FROM holdings h WHERE h.ticker = ? AND h.quantity > 0", (ticker.upper(),)
        ).fetchall()

    for row in rows:
        h = Holding(**dict(row))
        # Forward split: shares * ratio, cost / ratio
        # Reverse split: shares * ratio, cost / ratio
        new_qty = h.quantity * ratio
        new_avg = h.average_cost_per_share / ratio
        h.quantity = round(new_qty, 8)
        h.average_cost_per_share = round(new_avg, 4)
        h.upsert()

    # Record in corporate_actions table
    with get_db() as conn:
        conn.execute(
            """INSERT INTO corporate_actions (ticker, action_type, ratio, effective_date, applied_to_holdings)
               VALUES (?, ?, ?, DATE('now'), 1)""",
            (ticker.upper(), "split" if ratio > 1 else "reverse_split", ratio),
        )

    logger.info(f"Applied {ratio}:1 {'split' if ratio > 1 else 'reverse split'} for {ticker} across {len(rows)} holdings")


def scan_all_holdings_for_splits():
    """Scan all currently held tickers for recent splits and apply them."""
    with get_db() as conn:
        tickers = conn.execute(
            "SELECT DISTINCT ticker FROM holdings WHERE quantity > 0"
        ).fetchall()

    for row in tickers:
        ticker = row["ticker"]
        splits = check_splits(ticker)
        for split in splits:
            # Check if already applied
            with get_db() as conn:
                existing = conn.execute(
                    """SELECT id FROM corporate_actions
                       WHERE ticker = ? AND effective_date = ? AND applied_to_holdings = 1""",
                    (ticker.upper(), split["date"]),
                ).fetchone()
            if not existing:
                apply_split_to_holdings(ticker, split["ratio"])
