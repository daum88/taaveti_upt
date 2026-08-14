"""
Market data service — yfinance wrapper with rate limiting, batching,
news fetching, and market-status detection.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

from config import (
    YFINANCE_RATE_LIMIT_DELAY,
    YFINANCE_RETRY_COUNT,
)

logger = logging.getLogger(__name__)


# ── Market Status ────────────────────────────────────────

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
        df = yf.download(ticker_str, progress=False, auto_adjust=True, **download_args)
        if df is None or df.empty:
            return {}

        closes = df.get("Close", df)
        volumes = df.get("Volume", None)
        if isinstance(closes, pd.DataFrame):
            for t in tickers:
                if t in closes.columns:
                    col = closes[t].dropna()
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
            if len(col) >= 2 and len(tickers) == 1:
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


def fetch_current_prices(tickers: list[str]) -> dict[str, dict]:
    """
    Individual ticker price fetch with full volume data.
    Used for small lists (filtered stocks, watchlist). Use fetch_prices_batch for bulk.
    Returns dict: {ticker: {price, previous_close, change_percent, volume}}
    """
    results = {}
    for ticker in tickers:
        for attempt in range(YFINANCE_RETRY_COUNT):
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
                if attempt < YFINANCE_RETRY_COUNT - 1:
                    time.sleep(YFINANCE_RATE_LIMIT_DELAY * (2**attempt))
        time.sleep(YFINANCE_RATE_LIMIT_DELAY)
    return results


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


# ── News Fetching ────────────────────────────────────────


def fetch_news(ticker: str, lookback_hours: int = 3) -> list[dict]:
    """
    Fetch recent news headlines for a ticker via yfinance.
    Returns list of dicts with title, publisher, link, published_at.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    articles = []
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        for item in news:
            content = item.get("content", {})
            title = content.get("title", "")
            if not title:
                continue

            # pubDate is ISO 8601 string like "2026-06-24T10:00:00Z"
            pub_time_raw = content.get("pubDate")
            pub_time = None
            if pub_time_raw:
                try:
                    # Handle ISO 8601 format
                    pub_time = datetime.fromisoformat(pub_time_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    # Fallback: try as Unix timestamp
                    try:
                        pub_time = datetime.fromtimestamp(float(pub_time_raw), tz=UTC)
                    except (ValueError, TypeError, OSError):
                        pass

            if not pub_time:
                continue  # skip articles with unparseable dates

            provider = content.get("provider", {})
            canonical = content.get("canonicalUrl", {})

            articles.append(
                {
                    "title": title,
                    "publisher": provider.get("displayName", "Unknown"),
                    "link": canonical.get("url", ""),
                    "published_at": pub_time.isoformat(),
                }
            )
    except Exception as e:
        logger.debug(f"News fetch failed for {ticker}: {e}")

    # Filter by lookback
    if lookback_hours > 0:
        articles = [a for a in articles if a["published_at"] and datetime.fromisoformat(a["published_at"]) >= cutoff]

    return articles


# ── Company Info ──────────────────────────────────────────


def fetch_ticker_info(ticker: str) -> dict:
    """Fetch company name and sector for a ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        return {
            "company_name": info.get("longName") or info.get("shortName", ticker),
            "sector": info.get("sector", "Unknown"),
            "market_cap": info.get("marketCap"),
        }
    except Exception:
        return {"company_name": ticker, "sector": "Unknown", "market_cap": None}


def categorize_market_cap(market_cap) -> str:
    """Categorize a market cap value into size bucket."""
    if market_cap is None:
        return "large"
    if market_cap >= 200e9:
        return "mega"
    if market_cap >= 10e9:
        return "large"
    if market_cap >= 2e9:
        return "mid"
    if market_cap >= 300e6:
        return "small"
    return "micro"
