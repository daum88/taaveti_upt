"""
Market data service — yfinance wrapper with rate limiting, batching,
news fetching, and market-status detection.
"""

import time
import logging
from io import StringIO
from typing import Optional
from datetime import datetime, timedelta, timezone

import yfinance as yf
import pandas as pd
import requests
from bs4 import BeautifulSoup

from config import (
    SP500_WIKI_URL,
    NASDAQ100_WIKI_URL,
    YFINANCE_RATE_LIMIT_DELAY,
    YFINANCE_BATCH_DELAY,
    YFINANCE_RETRY_COUNT,
    YFINANCE_REQUEST_TIMEOUT,
    WATCHLIST_SIZE,
)

logger = logging.getLogger(__name__)


# ── Watchlist Ingestion ──────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _scrape_wiki_table(url: str, table_index: int = 0) -> pd.DataFrame | None:
    """Scrape a Wikipedia table with proper headers."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        if table_index < len(tables):
            return tables[table_index]
    except Exception as e:
        logger.debug(f"Failed to scrape {url}: {e}")
    return None


def fetch_sp500_tickers() -> list[str]:
    """
    Scrape current S&P 500 constituents from Wikipedia.
    Returns clean ticker list (sorted, up to WATCHLIST_SIZE).
    """
    tickers = []

    # Try S&P 500
    df = _scrape_wiki_table(SP500_WIKI_URL, table_index=0)
    if df is not None and "Symbol" in df.columns:
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        logger.info(f"Scraped {len(tickers)} S&P 500 tickers from Wikipedia")

    # Fallback: Nasdaq-100
    if not tickers:
        logger.info("S&P 500 scrape failed, trying Nasdaq-100...")
        df = _scrape_wiki_table(NASDAQ100_WIKI_URL, table_index=3)
        if df is not None and "Ticker" in df.columns:
            tickers = df["Ticker"].tolist()
            logger.info(f"Scraped {len(tickers)} Nasdaq-100 tickers")

    # Last resort: hardcoded top 100
    if not tickers:
        logger.warning("All scrapes failed. Using hardcoded fallback list of top 100 tickers.")
        tickers = _fallback_tickers()

    # Clean and limit
    tickers = [t.strip().upper() for t in tickers if isinstance(t, str) and t.strip()]
    return sorted(tickers)[:WATCHLIST_SIZE]


# ── Market Status ────────────────────────────────────────

def is_market_open() -> bool:
    """
    Check if major US markets are currently open.
    Uses yfinance on SPY as a proxy.
    """
    try:
        spy = yf.Ticker("SPY")
        info = spy.fast_info
        # fast_info has 'regular_market_time' — if within last 5 min, likely open
        market_time = getattr(info, "regular_market_time", None)
        if market_time:
            mt = datetime.fromtimestamp(market_time, tz=timezone.utc)
            return (datetime.now(timezone.utc) - mt).total_seconds() < 3600  # within 1 hour
    except Exception:
        pass

    # Fallback: check weekday + time
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    # US market hours (ET): 9:30 AM – 4:00 PM = 13:30 – 20:00 UTC
    market_open_utc = now.replace(hour=13, minute=30, second=0)
    market_close_utc = now.replace(hour=20, minute=0, second=0)
    return market_open_utc <= now <= market_close_utc


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
        df = yf.download(ticker_str, period="5d", progress=False, auto_adjust=True)
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
                        prev = float(col.iloc[-2])
                        change = (price - prev) / prev * 100 if prev > 0 else 0.0
                        vol = None
                        if volumes is not None and isinstance(volumes, pd.DataFrame) and t in volumes.columns:
                            vcol = volumes[t].dropna()
                            if len(vcol) > 0:
                                vol = int(vcol.iloc[-1])
                        results[t] = {"price": round(price, 4), "previous_close": round(prev, 4), "change_percent": round(change, 4), "volume": vol}
        else:
            col = closes.dropna()
            if len(col) >= 2 and len(tickers) == 1:
                price = float(col.iloc[-1])
                prev = float(col.iloc[-2])
                change = (price - prev) / prev * 100 if prev > 0 else 0.0
                results[tickers[0]] = {"price": round(price, 4), "previous_close": round(prev, 4), "change_percent": round(change, 4), "volume": None}
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
            except Exception as e:
                if attempt < YFINANCE_RETRY_COUNT - 1:
                    time.sleep(YFINANCE_RATE_LIMIT_DELAY * (2 ** attempt))
        time.sleep(YFINANCE_RATE_LIMIT_DELAY)
    return results


def fetch_ohlcv(ticker: str, days: int = 14) -> list[dict]:
    """
    Fetch OHLCV data for a ticker for the last N days.
    Returns list of date-str-keyed dicts.
    """
    try:
        t = yf.Ticker(ticker)
        end = datetime.now()
        start = end - timedelta(days=days + 2)  # extra buffer for weekends
        df = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if df.empty:
            return []
        records = []
        for idx, row in df.iterrows():
            records.append({
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(row["Open"], 4),
                "high": round(row["High"], 4),
                "low": round(row["Low"], 4),
                "close": round(row["Close"], 4),
                "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
            })
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
            if pd.isna(row.get("Close")):
                continue
            records.append({
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(row["Open"], 4),
                "high": round(row["High"], 4),
                "low": round(row["Low"], 4),
                "close": round(row["Close"], 4),
                "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
            })
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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
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
                        pub_time = datetime.fromtimestamp(float(pub_time_raw), tz=timezone.utc)
                    except (ValueError, TypeError, OSError):
                        pass

            if not pub_time:
                continue  # skip articles with unparseable dates

            provider = content.get("provider", {})
            canonical = content.get("canonicalUrl", {})

            articles.append({
                "title": title,
                "publisher": provider.get("displayName", "Unknown"),
                "link": canonical.get("url", ""),
                "published_at": pub_time.isoformat(),
            })
    except Exception as e:
        logger.debug(f"News fetch failed for {ticker}: {e}")

    # Filter by lookback
    if lookback_hours > 0:
        articles = [
            a for a in articles
            if a["published_at"] and datetime.fromisoformat(a["published_at"]) >= cutoff
        ]

    return articles


# ── Company Info ──────────────────────────────────────────

def _fallback_tickers() -> list[str]:
    """Hardcoded top 100 US stocks as last-resort fallback."""
    return [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "UNH", "JNJ",
        "V", "XOM", "WMT", "JPM", "MA", "PG", "LLY", "HD", "CVX", "ABBV",
        "MRK", "PEP", "KO", "AVGO", "COST", "TMO", "MCD", "CSCO", "ABT", "DHR",
        "NFLX", "ADBE", "CRM", "DIS", "AMD", "INTC", "QCOM", "TXN", "AMGN", "INTU",
        "VZ", "CMCSA", "NKE", "PM", "IBM", "HON", "RTX", "LOW", "GE", "CAT",
        "AMAT", "UBER", "NOW", "SPGI", "ISRG", "GS", "AXP", "UNP", "PFE", "MS",
        "BKNG", "ELV", "SYK", "BLK", "TJX", "LRCX", "MDT", "PLD", "ADP", "DE",
        "MMC", "C", "CB", "BSX", "ADI", "CI", "FI", "ETN", "LMT", "SCHW",
        "TMUS", "GILD", "MO", "SO", "DUK", "ICE", "MU", "KLAC", "SHW", "ZTS",
        "WM", "CMG", "ANET", "CDNS", "SNPS", "REGN", "ITW", "PH", "AON", "CL",
    ]


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
