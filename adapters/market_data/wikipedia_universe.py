"""Investable-universe lookup via Wikipedia constituent tables.

This is a true external port: it scrapes the S&P 500 (and Nasdaq-100 fallback)
constituent tables and degrades to a hardcoded large-cap list when both scrapes
fail. Callers receive a clean, sorted, size-bounded ticker list and never see
the scraping mechanics.
"""

import logging
from io import StringIO

import pandas as pd
import requests

from config import NASDAQ100_WIKI_URL, SP500_WIKI_URL, WATCHLIST_SIZE

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _scrape_wiki_table(url: str, table_index: int = 0) -> pd.DataFrame | None:
    """Scrape a Wikipedia table with proper headers."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
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


def _fallback_tickers() -> list[str]:
    """Hardcoded top 100 US stocks as last-resort fallback."""
    return [
        "AAPL",
        "MSFT",
        "GOOGL",
        "AMZN",
        "NVDA",
        "META",
        "TSLA",
        "BRK-B",
        "UNH",
        "JNJ",
        "V",
        "XOM",
        "WMT",
        "JPM",
        "MA",
        "PG",
        "LLY",
        "HD",
        "CVX",
        "ABBV",
        "MRK",
        "PEP",
        "KO",
        "AVGO",
        "COST",
        "TMO",
        "MCD",
        "CSCO",
        "ABT",
        "DHR",
        "NFLX",
        "ADBE",
        "CRM",
        "DIS",
        "AMD",
        "INTC",
        "QCOM",
        "TXN",
        "AMGN",
        "INTU",
        "VZ",
        "CMCSA",
        "NKE",
        "PM",
        "IBM",
        "HON",
        "RTX",
        "LOW",
        "GE",
        "CAT",
        "AMAT",
        "UBER",
        "NOW",
        "SPGI",
        "ISRG",
        "GS",
        "AXP",
        "UNP",
        "PFE",
        "MS",
        "BKNG",
        "ELV",
        "SYK",
        "BLK",
        "TJX",
        "LRCX",
        "MDT",
        "PLD",
        "ADP",
        "DE",
        "MMC",
        "C",
        "CB",
        "BSX",
        "ADI",
        "CI",
        "FI",
        "ETN",
        "LMT",
        "SCHW",
        "TMUS",
        "GILD",
        "MO",
        "SO",
        "DUK",
        "ICE",
        "MU",
        "KLAC",
        "SHW",
        "ZTS",
        "WM",
        "CMG",
        "ANET",
        "CDNS",
        "SNPS",
        "REGN",
        "ITW",
        "PH",
        "AON",
        "CL",
    ]
