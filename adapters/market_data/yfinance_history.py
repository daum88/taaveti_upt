"""Price-history (OHLCV) lookup via yfinance.

This is a true external port: it pulls a ticker's open/high/low/close/volume
bars from yfinance for a calendar range or an intraday sampling interval,
rounds prices, drops incomplete rows, and formats a stable date key. Callers
receive clean, date-keyed OHLCV dicts and never see the provider's payload
shape or error modes.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_ohlcv(ticker: str, days: int = 14, interval: str | None = None) -> list[dict]:
    """Fetch OHLCV data for the requested calendar range and sampling interval."""
    try:
        t = yf.Ticker(ticker)
        end = datetime.now()
        start = end - timedelta(days=days + 2)  # extra buffer for weekends
        history_args = (
            {"period": "1d", "interval": interval}
            if interval
            else {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")}
        )
        df = t.history(**history_args)
        if df.empty:
            return []
        timestamp_format = "%Y-%m-%dT%H:%M" if interval else "%Y-%m-%d"
        records = []
        for idx, row in df.iterrows():
            if row[["Open", "High", "Low", "Close"]].isna().any():
                continue
            records.append(
                {
                    "date": idx.strftime(timestamp_format),
                    "open": round(row["Open"], 4),
                    "high": round(row["High"], 4),
                    "low": round(row["Low"], 4),
                    "close": round(row["Close"], 4),
                    "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
                }
            )
        return records
    except Exception as e:
        logger.debug(f"Failed to fetch OHLCV for {ticker}: {e}")
        return []


def fetch_ohlcv_batch(tickers: list[str], days: int = 14) -> dict[str, list[dict]]:
    """
    Batch OHLCV fetch for many tickers via a single yf.download call per chunk.
    Returns a dict mapping ticker -> list of date-keyed OHLCV dicts.
    """
    result: dict[str, list[dict]] = {}
    if not tickers:
        return result

    end = datetime.now()
    start = end - timedelta(days=days + 2)  # buffer for weekends

    def _records_from_df(df) -> list[dict]:
        records = []
        for idx, row in df.iterrows():
            if row[["Open", "High", "Low", "Close"]].isna().any():
                continue
            records.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": round(row["Open"], 4),
                    "high": round(row["High"], 4),
                    "low": round(row["Low"], 4),
                    "close": round(row["Close"], 4),
                    "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
                }
            )
        return records

    try:
        df = yf.download(
            tickers,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
    except Exception as e:
        logger.debug(f"Batch OHLCV download failed: {e}")
        return result

    if df is None or df.empty:
        return result

    if len(tickers) == 1:
        result[tickers[0]] = _records_from_df(df)
        return result

    for ticker in tickers:
        if ticker not in df.columns.get_level_values(0):
            continue
        sub = df[ticker].dropna(how="all")
        if not sub.empty:
            result[ticker] = _records_from_df(sub)
    return result
