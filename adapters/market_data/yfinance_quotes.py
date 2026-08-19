"""
Quote-snapshot external port.

yfinance wrapper with rate limiting that returns current price snapshots,
either batched in one download call for many tickers or per-ticker with full
volume detail for small lists.
"""

import logging
import time

import pandas as pd
import yfinance as yf

from adapters.market_data.market_calendar import is_market_open, latest_completed_session
from settings import Settings, load_settings

logger = logging.getLogger(__name__)


def _is_session_gap(closes: pd.Series, expected_session) -> bool:
    """True when the newest daily bar is not the latest completed NYSE session.

    Providers finalize end-of-day bars per symbol with varying lag; trusting a
    lagging (or already forming next-day) bar values portfolios at prices the
    market never traded as a same-day close.
    """
    return expected_session is not None and (closes.empty or closes.index[-1].date() != expected_session)


# ── Price Fetching ───────────────────────────────────────


def fetch_prices_batch(tickers: list[str]) -> dict[str, dict]:
    """
    Ultra-fast batch price fetch for many tickers using yf.download.
    Fetches all tickers in ONE API call (~2-3s for 200 tickers).
    Returns dict: {ticker: {price, previous_close, change_percent}}
    """
    if not tickers:
        return {}
    results = {}
    try:
        ticker_str = " ".join(tickers)
        market_open = is_market_open()
        download_args = {"period": "2d", "interval": "1m"} if market_open else {"period": "5d"}
        expected_session = None if market_open else latest_completed_session()
        df = yf.download(ticker_str, progress=False, auto_adjust=True, **download_args)
        if df is None or df.empty:
            return {}

        closes = df.get("Close", df)
        volumes = df.get("Volume", None)
        if isinstance(closes, pd.DataFrame):
            for t in tickers:
                if t in closes.columns:
                    col = closes[t].dropna()
                    if _is_session_gap(col, expected_session):
                        logger.info("Skipping %s: daily bars not finalized for session %s", t, expected_session)
                        continue
                    if len(col) >= 2:
                        price = float(col.iloc[-1])
                        previous_session = col[col.index.date < col.index[-1].date()] if market_open else col.iloc[:-1]
                        prev = float(previous_session.iloc[-1]) if not previous_session.empty else float(col.iloc[-2])
                        change = (price - prev) / prev * 100 if prev > 0 else 0.0
                        vol = None
                        if volumes is not None and isinstance(volumes, pd.DataFrame) and t in volumes.columns:
                            vcol = volumes[t].dropna()
                            if len(vcol) > 0:
                                vol = int(vcol.iloc[-1])
                        results[t] = {
                            "price": round(price, 4),
                            "previous_close": round(prev, 4),
                            "change_percent": round(change, 4),
                            "volume": vol,
                        }
        else:
            col = closes.dropna()
            if _is_session_gap(col, expected_session):
                logger.info("Skipping %s: daily bars not finalized for session %s", tickers[0], expected_session)
            elif len(col) >= 2 and len(tickers) == 1:
                price = float(col.iloc[-1])
                previous_session = col[col.index.date < col.index[-1].date()] if market_open else col.iloc[:-1]
                prev = float(previous_session.iloc[-1]) if not previous_session.empty else float(col.iloc[-2])
                change = (price - prev) / prev * 100 if prev > 0 else 0.0
                results[tickers[0]] = {
                    "price": round(price, 4),
                    "previous_close": round(prev, 4),
                    "change_percent": round(change, 4),
                    "volume": None,
                }
    except Exception as e:
        logger.warning(f"Batch price fetch failed: {e} — falling back to individual")

    logger.info(f"Batch-fetched prices for {len(results)}/{len(tickers)} tickers in one call")
    return results


def fetch_current_prices(tickers: list[str], *, settings: Settings | None = None) -> dict[str, dict]:
    """
    Individual ticker price fetch with full volume data.
    Used for small lists (filtered stocks, watchlist). Use fetch_prices_batch for bulk.
    Returns dict: {ticker: {price, previous_close, change_percent, volume}}
    """
    configuration = settings or load_settings()
    results = {}
    for ticker in tickers:
        for attempt in range(configuration.yfinance_retry_count):
            try:
                t = yf.Ticker(ticker)
                info = t.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "regular_market_previous_close", None)
                prev_close = getattr(info, "regular_market_previous_close", None)
                if price is None:
                    info_dict = t.info or {}
                    price = info_dict.get("regularMarketPrice") or info_dict.get("currentPrice")
                    prev_close = info_dict.get("regularMarketPreviousClose") or info_dict.get("previousClose")
                if price and price > 0:
                    change = ((price - prev_close) / prev_close * 100) if prev_close and prev_close > 0 else 0.0
                    results[ticker.upper()] = {
                        "price": round(price, 4),
                        "previous_close": round(prev_close, 4) if prev_close else None,
                        "change_percent": round(change, 4),
                        "volume": getattr(info, "last_volume", None),
                    }
                break
            except Exception:
                if attempt < configuration.yfinance_retry_count - 1:
                    time.sleep(configuration.yfinance_rate_limit_delay * (2**attempt))
        time.sleep(configuration.yfinance_rate_limit_delay)
    return results
