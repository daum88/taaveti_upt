"""Deterministic Playwright coverage for the browser application.

The browser receives fixture responses at the HTTP boundary.  It therefore
runs by default without the local portfolio database, a running FastAPI
server, market-data providers, or LLM credentials.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "ui" / "web"


@pytest.fixture(scope="module")
def browser_api():
    timestamp = "2026-07-28T12:00:00+00:00"
    later = "2026-07-29T12:00:00+00:00"
    latest = "2026-07-31T12:00:00+00:00"
    pnl_history = [
        {"time": timestamp, "pnl": 0, "pnl_pct": 0},
        {"time": later, "pnl": 100, "pnl_pct": 1},
        {"time": latest, "pnl": 250, "pnl_pct": 2.5},
    ]
    leaderboard = [
        {
            "user_id": 1,
            "username": "taavet",
            "display_name": "Taavet",
            "user_type": "human",
            "decision_architecture": "single_model",
            "rank": 1,
            "total_value": 10_250,
            "holdings_value": 2_250,
            "cash_balance": 8_000,
            "pnl_percent": 2.5,
        },
        {
            "user_id": 2,
            "username": "indexer",
            "display_name": "Indexer",
            "user_type": "index_fund",
            "decision_architecture": "single_model",
            "rank": 2,
            "total_value": 10_100,
            "holdings_value": 2_100,
            "cash_balance": 8_000,
            "pnl_percent": 1,
        },
        {
            "user_id": 3,
            "username": "running-ai",
            "display_name": "Running AI",
            "user_type": "llm_agent",
            "decision_architecture": "single_model",
            "rank": 3,
            "total_value": 9_900,
            "holdings_value": 1_900,
            "cash_balance": 8_000,
            "pnl_percent": -1,
        },
    ]

    def detail(username):
        entry = next((row for row in leaderboard if row["username"] == username), leaderboard[0])
        return {
            "username": entry["username"],
            "display_name": entry["display_name"],
            "user_type": entry["user_type"],
            "decision_architecture": entry["decision_architecture"],
            "model_roster": {"provider": "test", "model": "fixture"},
            "strategy": {
                "label": "Balanced investor",
                "summary": "A measured approach.",
                "config": {
                    "sell_gain_pct": 12,
                    "sell_loss_pct": -8,
                    "min_move_pct": 1.5,
                    "max_positions": 7,
                    "max_allocation": 0.15,
                    "max_volatility_pct": 8,
                    "cash_reserve_pct": 5,
                    "prefer_dips": False,
                },
            },
            "portfolio": {
                "cash_balance": entry["cash_balance"],
                "holdings_value": entry["holdings_value"],
                "realized_pnl": 100,
                "holdings_count": 1,
                "total_value": entry["total_value"],
                "pnl_percent": entry["pnl_percent"],
                "holdings": [
                    {
                        "ticker": "AAPL",
                        "quantity": 10,
                        "average_cost": 200,
                        "current_price": 225,
                        "market_value": 2_250,
                        "pnl": 250,
                        "pnl_percent": 12.5,
                        "opened_at": timestamp,
                    }
                ],
            },
            "trades": [
                {"action": "BUY", "ticker": "AAPL", "quantity": 10, "price": 200, "total": 2_000, "time": timestamp}
            ],
            "sectors": {"Technology": 2_250},
            "stats": {
                "dividend_income": 0,
                "total_trades": 1,
                "buys": 1,
                "sells": 0,
                "win_rate": 0,
                "largest_trade": 2_000,
            },
            "analyses": [],
            "committee_steps": [],
            "no_trade_decision": None,
            "pnl_history": pnl_history,
        }

    stock = {
        "ticker": "AAPL",
        "company": "Apple Inc.",
        "sector": "Technology",
        "instrument_type": "equity",
        "exchange": "NASDAQ",
        "issuer": None,
        "category": None,
        "price": 225,
        "previous_close": 220,
        "change_percent": 2.27,
        "volume": 10_000_000,
        "chart_range": "1M",
        "ohlcv": [{"date": timestamp, "open": 200, "high": 205, "low": 199, "close": 202, "volume": 100}],
        "news": [{"title": "Apple fixture headline", "publisher": "Fixture News", "published_at": timestamp}],
        "research": {},
        "recent_trades": [
            {
                "transaction_type": "BUY",
                "username": "taavet",
                "quantity": 10,
                "price_per_share": 200,
                "executed_at": timestamp,
            }
        ],
        "holders": [
            {
                "username": "taavet",
                "display_name": "Taavet",
                "user_type": "human",
                "quantity": 10,
                "avg_cost": 200,
                "pnl_percent": 12.5,
            }
        ],
    }
    week = {
        "days": [
            {
                "weekday": "Tue",
                "date": "2026-07-28",
                "state": "not_due",
                "is_today": True,
                "due_at": None,
                "run_count": 0,
            }
        ],
        "latest_batch": {"status": "idle", "counts": {}, "agents": {}},
        "current_batch": None,
        "timezone": "UTC",
        "ai_account_count": 1,
    }

    def response(url):
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/leaderboard":
            return leaderboard
        if path == "/api/portfolio-history":
            return {
                "history": {
                    str(row["user_id"]): [
                        {"time": point["time"], "value": row["total_value"] - 250 + point["pnl"], "pnl": point["pnl"]}
                        for point in pnl_history
                    ]
                    for row in leaderboard
                },
                "users": {str(row["user_id"]): row["display_name"] for row in leaderboard},
            }
        if path.startswith("/api/agent-detail/"):
            return detail(path.rsplit("/", 1)[1])
        if path == "/api/watchlist":
            return [
                {
                    "ticker": "AAPL",
                    "company": "Apple Inc.",
                    "company_name": "Apple Inc.",
                    "instrument_type": "equity",
                    "sector": "Technology",
                    "category": None,
                    "price": 225,
                    "change_percent": 2.27,
                    "volume": 10_000_000,
                    "total": 1,
                }
            ]
        if path == "/api/instrument-suggestions":
            suggestion = query.get("query", [""])[0].lower()
            return {
                "suggestions": [
                    {
                        "ticker": "AAPL",
                        "company_name": "Apple Inc.",
                        "instrument_type": "equity",
                        "exchange": "NASDAQ",
                        "category": None,
                    }
                ]
                if suggestion == "apple"
                else []
            }
        if path.startswith("/api/stock/"):
            return {**stock, "chart_range": query.get("chart_range", ["1M"])[0]}
        if path == "/api/transactions":
            return [
                {
                    "username": "taavet",
                    "transaction_type": "BUY",
                    "ticker": "AAPL",
                    "quantity": 10,
                    "price_per_share": 200,
                    "total_value": 2_000,
                    "executed_at": timestamp,
                    "execution_quote_source": "fixture",
                    "execution_market_state": "last_close",
                    "execution_quote_captured_at": timestamp,
                }
            ]
        if path == "/api/decision-batches/week":
            return week
        if path == "/api/decision-batches":
            return {"status": "queued", "counts": {}, "agents": {}}
        if path == "/api/cycle/status":
            return {
                "running": True,
                "last_run": timestamp,
                "next_run": later,
                "in_progress": False,
                "last_result": {"stocks_processed": 1, "error": None},
            }
        if path == "/api/cycle":
            return {"ok": True, "message": "Cycle triggered"}
        if path == "/api/cycle/check":
            return {
                "triggered": False,
                "scheduler": {
                    "running": True,
                    "last_run": timestamp,
                    "next_run": later,
                    "in_progress": False,
                    "last_result": {"stocks_processed": 1, "error": None},
                },
            }
        return {}

    return response


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(WEB_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            if process.poll() is not None:
                pytest.fail("Static browser-test server exited early")
            time.sleep(0.1)
    else:
        process.terminate()
        pytest.fail("Static browser-test server did not start in time")
    yield base_url
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture(scope="module")
def page(server, browser_api):
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch()
    except Exception as error:
        pytest.skip(f"Chromium unavailable: {error}")
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def fulfill_api(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(browser_api(route.request.url)))

    page.route("**/api/**", fulfill_api)
    page.goto(server, wait_until="domcontentloaded")
    page._collected_errors = errors  # type: ignore[attr-defined]
    yield page
    context.close()
    browser.close()
    playwright.stop()


def _first_username(page):
    page.wait_for_selector("#lb-body tr td.rank", timeout=15000)
    return page.eval_on_selector_all(
        "#lb-body tr",
        "rows => rows.map(r => r.querySelector('.name-cell')?.textContent?.trim().slice(0,20))",
    )


def test_leaderboard_renders_rows(page):
    page.wait_for_selector("#lb-body tr td.rank", timeout=15000)
    rows = page.query_selector_all("#lb-body tr")
    assert len(rows) > 0
    # KPI header populated
    assert page.query_selector("#kpis .kpi") is not None
    # Invested + Cash columns present in header
    headers = page.eval_on_selector_all("#lb-table thead th", "ths => ths.map(t => t.textContent.trim())")
    assert "Invested" in headers
    assert "Cash" in headers
    # Portfolio-value chart canvas exists
    assert page.query_selector("#lbChart") is not None
    # Popular Stocks sidebar renders rows
    page.wait_for_selector("#popular-list .pop-row", timeout=15000)
    assert len(page.query_selector_all("#popular-list .pop-row")) > 0


def test_leaderboard_chart_spaces_points_by_elapsed_time(page):
    result = page.evaluate(
        """() => {
            const scale = Chart.getChart('lbChart').scales.x;
            const start = Date.parse('2026-07-28T12:00:00+00:00');
            const oneDayLater = start + 86_400_000;
            const threeDaysLater = start + 259_200_000;
            const firstInterval = scale.getPixelForValue(oneDayLater) - scale.getPixelForValue(start);
            const thirdInterval = scale.getPixelForValue(threeDaysLater) - scale.getPixelForValue(oneDayLater);
            return { type: scale.type, ratio: firstInterval / thirdInterval };
        }"""
    )

    assert result["type"] == "linear"
    assert result["ratio"] == pytest.approx(0.5)


def test_leaderboard_chart_keeps_all_accounts_in_the_visible_y_range(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const values = chart.data.datasets.flatMap(dataset => dataset.data.map(point => point.y).filter(Number.isFinite));
            return {
                datasetCount: chart.data.datasets.length,
                visibleDatasetCount: chart.data.datasets.filter((_, index) => chart.isDatasetVisible(index)).length,
                renderedSeriesCount: chart.data.datasets.filter((_, index) => chart.getDatasetMeta(index).data.some(point => !point.skip)).length,
                valueMin: Math.min(...values),
                valueMax: Math.max(...values),
                axisMin: chart.scales.y.min,
                axisMax: chart.scales.y.max,
            };
        }"""
    )

    assert result["datasetCount"] == len(page.evaluate("() => lbData"))
    assert result["visibleDatasetCount"] == result["datasetCount"]
    assert result["renderedSeriesCount"] == result["datasetCount"]
    assert result["axisMin"] < result["valueMin"]
    assert result["axisMax"] > result["valueMax"]


def test_chart_hover_shows_full_timestamp(page):
    result = page.evaluate(
        """async () => {
            const { history } = await (await fetch('/api/portfolio-history')).json();
            const timestamps = [...new Set(Object.values(history).flat().map(point => point.time))].sort();
            const title = Chart.getChart('lbChart').options.plugins.tooltip.callbacks.title([{ parsed: { x: new Date(timestamps[0]).getTime() } }]);
            return { expected: new Date(timestamps[0]).toLocaleString(), title };
        }"""
    )
    assert result["title"] == result["expected"]


def test_leaderboard_chart_ends_at_the_table_valuation(page):
    page.wait_for_function("() => Chart.getChart('lbChart')?.data.datasets.length === lbData.length")
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const valuesByUsername = Object.fromEntries(lbData.flatMap(row => [
                [row.username, Number(row.total_value)],
                [row.display_name, Number(row.total_value)],
            ]));
            const values = chart.data.datasets.map(dataset => ({
                username: dataset.label,
                chartValue: dataset.data.at(-1).y,
                tableValue: valuesByUsername[dataset.label],
            }));
            return {values, mismatches: values.filter(row => row.chartValue !== row.tableValue)};
        }"""
    )

    assert result["values"]
    assert result["mismatches"] == []


def test_unchanged_websocket_messages_preserve_chart_instance_and_zoom(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            chart.zoom(1.5);
            const before = { chart, min: chart.scales.x.min, max: chart.scales.x.max };
            handleWebSocketMessage({ type: 'ACCOUNT_STATE_UPDATE' });
            handleWebSocketMessage({ type: 'TRANSACTION_UPDATE', data: [] });
            return {
                sameInstance: Chart.getChart('lbChart') === before.chart,
                min: chart.scales.x.min,
                max: chart.scales.x.max,
                beforeMin: before.min,
                beforeMax: before.max,
            };
        }"""
    )

    assert result["sameInstance"] is True
    assert result["min"] == result["beforeMin"]
    assert result["max"] == result["beforeMax"]


def test_leaderboard_chart_zoom_and_reset(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const reset = document.getElementById('lb-chart-reset');
            const configured = Boolean(chart.options.plugins.zoom?.zoom.wheel.enabled)
                && Boolean(chart.options.plugins.zoom?.zoom.pinch.enabled)
                && Boolean(chart.options.plugins.zoom?.pan.enabled);
            const initiallyDisabled = reset.disabled;
            chart.zoom(1.5);
            syncLbChartZoomState();
            const enabledAfterZoom = !reset.disabled && chart.isZoomedOrPanned();
            reset.click();
            return { configured, initiallyDisabled, enabledAfterZoom, disabledAfterReset: reset.disabled, zoomedAfterReset: chart.isZoomedOrPanned() };
        }"""
    )

    assert result == {
        "configured": True,
        "initiallyDisabled": True,
        "enabledAfterZoom": True,
        "disabledAfterReset": True,
        "zoomedAfterReset": False,
    }


def test_no_page_errors_on_load(page):
    assert page._collected_errors == [], f"JS errors on load: {page._collected_errors}"


def test_transaction_types_have_correct_visual_direction(page):
    assert page.evaluate("() => ['BUY', 'DIVIDEND', 'SELL', 'DIVIDEND_REVERSAL'].map(transactionClass)") == [
        "pos",
        "pos",
        "neg",
        "neg",
    ]


def _open_and_assert_drawer(page, username):
    # Click the leaderboard row whose name-cell starts with username.
    page.eval_on_selector_all(
        "#lb-body tr",
        """(rows, name) => {
            const row = rows.find(r => (r.querySelector('.name-cell')?.textContent || '').toLowerCase().includes(name));
            if (row) row.click();
        }""",
        username,
    )
    page.wait_for_selector("#drawer.open", timeout=5000)
    # d-sub must leave the "Loading..." state and show a dollar value.
    page.wait_for_function(
        "() => { const s = document.getElementById('d-sub'); return s && /\\$/.test(s.textContent); }",
        timeout=8000,
    )
    # Portfolio tab must not be stuck on the Loading spinner.
    portfolio_html = page.inner_html("#tab-portfolio")
    assert "Loading" not in portfolio_html, f"{username} drawer stuck loading"
    assert "Failed to load" not in portfolio_html, f"{username} drawer failed: {portfolio_html[:200]}"
    # Stat cards should be present.
    assert page.query_selector("#tab-portfolio .stat") is not None


def test_ai_strategy_uses_plain_language_decision_criteria(page):
    html = page.evaluate(
        """() => strategyHtml({strategy: {
            label: 'Balanced investor',
            summary: 'A measured approach.',
            config: {
                sell_gain_pct: 12,
                sell_loss_pct: -8,
                min_move_pct: 1.5,
                max_positions: 7,
                max_allocation: 0.15,
                max_volatility_pct: 8,
                cash_reserve_pct: 5,
                prefer_dips: false,
            },
        }})"""
    )

    assert "How this AI makes decisions" in html
    assert "When it sells" in html
    assert "Sell a holding after it gains more than 12%." in html
    assert "Look for a clear price move of at least 1.5% before buying." in html
    assert "Put no more than 15% of the portfolio into one investment." in html
    assert "TP%" not in html
    assert "SL%" not in html
    assert "Max pos" not in html


def test_committee_no_trade_reason_is_shown_as_text(page):
    content = page.evaluate(
        """() => {
            renderPortfolio({
                decision_architecture: 'multi_model',
                user_type: 'llm_agent',
                strategy: {},
                committee_steps: [],
                no_trade_decision: {
                    decision: 'HOLD',
                    execution_status: 'hold',
                    reasoning: '<b>Wait for a lower-volatility setup.</b>',
                },
                portfolio: {cash_balance: 10000, holdings_value: 0, realized_pnl: 0, holdings_count: 0, total_value: 10000, holdings: []},
                stats: {win_rate: 0, total_trades: 0, largest_trade: 0},
                sectors: {},
            });
            const decision = document.querySelector('.committee-decision');
            const outcome = document.getElementById('committee-no-trade-outcome');
            const reason = document.getElementById('committee-no-trade-reason');
            return {decisionLabel: decision.querySelector('.committee-decision-eyebrow').textContent, outcome: outcome.textContent, text: reason.textContent, html: reason.innerHTML};
        }"""
    )

    assert content["decisionLabel"] == "Today’s committee decision"
    assert content["outcome"] == "The chair chose to hold rather than place a trade."
    assert content["text"] == "<b>Wait for a lower-volatility setup.</b>"
    assert "&lt;b&gt;" in content["html"]


def test_open_index_fund_drawer(page):
    """Regression: clicking the index fund used to open an empty stuck drawer."""
    names = [n.lower() for n in _first_username(page) if n]
    if not any("indexer" in n for n in names):
        pytest.skip("No 'indexer' player in current DB")
    _open_and_assert_drawer(page, "indexer")
    # Index fund is NOT human -> no Trade tab.
    trade_btn = page.query_selector("#tab-btn-trade")
    assert trade_btn is not None
    assert not trade_btn.is_visible(), "Index fund must not expose the Trade tab"
    page.click("#drawer .close")


def test_open_first_player_drawer(page):
    names = [n for n in _first_username(page) if n]
    assert names, "No players on leaderboard"
    target = names[0].split()[0].lower()
    _open_and_assert_drawer(page, target)


def test_human_has_trade_tab(page):
    """Human accounts must expose the Trade tab; non-humans must not."""
    names = [n.lower() for n in _first_username(page) if n]
    if not any("taavet" in n for n in names):
        pytest.skip("No human 'taavet' in current DB")
    _open_and_assert_drawer(page, "taavet")
    trade_btn = page.query_selector("#tab-btn-trade")
    assert trade_btn is not None and trade_btn.is_visible(), "Human must expose the Trade tab"
    page.click('.tabs button[data-tab="trade"]')
    assert page.is_visible("#tab-trade")
    assert page.query_selector("#trade-submit") is not None
    page.click("#drawer .close")


def test_leaderboard_refresh_preserves_manual_trade_draft(page):
    names = [n.lower() for n in _first_username(page) if n]
    if not any("taavet" in n for n in names):
        pytest.skip("No human 'taavet' in current DB")
    _open_and_assert_drawer(page, "taavet")
    page.click('.tabs button[data-tab="trade"]')
    page.fill("#trade-ticker", "AAPL")
    page.fill("#trade-amount", "100")
    page.click("#seg-sell")
    page.focus("#trade-amount")

    state = page.evaluate(
        """async () => {
            const form = document.getElementById('tab-trade').firstElementChild;
            handleWebSocketMessage({type: 'LEADERBOARD_UPDATE'});
            await leaderboardRefreshInFlight;
            return {
                sameForm: document.getElementById('tab-trade').firstElementChild === form,
                ticker: document.getElementById('trade-ticker').value,
                amount: document.getElementById('trade-amount').value,
                action: tradeAction,
                focused: document.activeElement.id,
            };
        }"""
    )

    assert state == {
        "sameForm": True,
        "ticker": "AAPL",
        "amount": "100",
        "action": "SELL",
        "focused": "trade-amount",
    }
    page.click("#drawer .close")


def test_trade_requires_review_and_cancel_has_no_execution_side_effect(page):
    names = [n.lower() for n in _first_username(page) if n]
    if not any("taavet" in n for n in names):
        pytest.skip("No human 'taavet' in current DB")
    _open_and_assert_drawer(page, "taavet")
    page.click('.tabs button[data-tab="trade"]')
    page.evaluate(
        """() => {
            window.tradeRequests = [];
            const originalFetch = window.fetch;
            window.fetch = async (url, options) => {
                window.tradeRequests.push(String(url));
                if (String(url) === '/api/trade/preview') {
                    return new Response(JSON.stringify({
                        instrument: { ticker: 'AAPL', company: 'Apple', instrument_type: 'equity' },
                        quote: { price: 100 }, action: 'BUY', requested_amount: 100,
                        estimated_executable_amount: 100, estimated_quantity: 1, fee: 1,
                        estimated_cash_after: 9899, estimated_holding_quantity: 1,
                        estimated_holding_weight: .01, warnings: []
                    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
                }
                return originalFetch(url, options);
            };
        }"""
    )
    page.fill("#trade-ticker", "AAPL")
    page.fill("#trade-amount", "100")
    page.click("#trade-submit")
    page.wait_for_selector("#trade-confirm-modal.open")
    assert "/api/trade" not in page.evaluate("() => window.tradeRequests")
    page.click("#trade-confirm-modal .decision-btn")
    assert not page.is_visible("#trade-confirm-modal")
    assert "/api/trade" not in page.evaluate("() => window.tradeRequests")
    page.click("#drawer .close")


def test_trade_retry_reuses_the_original_client_order_id(page):
    names = [name.lower() for name in _first_username(page) if name]
    if not any("taavet" in name for name in names):
        pytest.skip("No human 'taavet' in current DB")
    _open_and_assert_drawer(page, "taavet")
    page.click('.tabs button[data-tab="trade"]')
    page.evaluate(
        """() => {
            window.tradeRetryRequests = [];
            const originalFetch = window.fetch;
            window.fetch = async (url, options = {}) => {
                const path = String(url);
                if (path === '/api/trade/preview') {
                    return new Response(JSON.stringify({
                        instrument: { ticker: 'AAPL', company: 'Apple', instrument_type: 'equity' },
                        quote: { price: 100 }, action: 'BUY', requested_amount: 100,
                        estimated_executable_amount: 100, estimated_quantity: 1, fee: 1,
                        estimated_cash_after: 9899, estimated_holding_quantity: 1,
                        estimated_holding_weight: .01, warnings: []
                    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
                }
                if (path === '/api/trade') {
                    window.tradeRetryRequests.push(JSON.parse(options.body));
                    if (window.tradeRetryRequests.length === 1) throw new TypeError('connection lost');
                    return new Response(JSON.stringify({
                        ok: true,
                        transaction: { action: 'BUY', quantity: 1, ticker: 'AAPL', price: 100, total: 100, fee: 1 }
                    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
                }
                return originalFetch(url, options);
            };
            window.restoreTradeRetryFetch = () => { window.fetch = originalFetch; };
        }"""
    )
    try:
        page.fill("#trade-ticker", "AAPL")
        page.fill("#trade-amount", "100")
        page.click("#trade-submit")
        page.wait_for_selector("#trade-confirm-modal.open")
        page.click("#trade-confirm-submit")
        page.wait_for_function("() => document.getElementById('trade-confirm-submit').textContent.startsWith('Retry')")
        page.click("#trade-confirm-submit")
        page.wait_for_function("() => window.tradeRetryRequests.length === 2")
        requests = page.evaluate("() => window.tradeRetryRequests")
    finally:
        page.evaluate("() => window.restoreTradeRetryFetch?.()")
        page.click("#drawer .close")

    assert requests[0]["client_order_id"] == requests[1]["client_order_id"]
    assert requests[0]["client_order_id"]


def test_scheduled_news_refresh_status_is_visible_on_dashboard(page):
    status = page.evaluate(
        """() => {
            renderFunnelStatus({
                running: true,
                last_run: '2026-08-04T09:00:00+00:00',
                next_run: '2026-08-04T12:00:00+00:00',
                in_progress: false,
                last_result: {stocks_processed: 3, error: null},
            });
            return {
                title: document.getElementById('refresh-title').textContent,
                message: document.getElementById('funnel-refresh-msg').textContent,
                times: document.getElementById('funnel-refresh-times').textContent,
            };
        }"""
    )

    assert status["title"] == "Scheduled market & news refresh"
    assert status["message"] == "Refresh complete"
    assert "Last run:" in status["times"]
    assert "Next scheduled:" in status["times"]


def test_scheduled_news_refresh_can_be_triggered_manually(page):
    status = page.evaluate(
        """async () => {
            const originalFetch = window.fetch;
            const requests = [];
            window.fetch = async (url, options) => {
                requests.push({url: String(url), method: options?.method || 'GET'});
                return new Response(JSON.stringify({
                    in_progress: false,
                    last_run: '2026-08-04T09:00:00+00:00',
                    next_run: '2026-08-04T12:00:00+00:00',
                    last_result: {stocks_processed: 3, error: null},
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
            };
            try {
                await triggerManualRefresh();
                return {
                    requests,
                    disabled: document.getElementById('funnel-refresh-btn').disabled,
                };
            } finally {
                window.fetch = originalFetch;
            }
        }"""
    )

    assert {"url": "/api/cycle", "method": "POST"} in status["requests"]
    assert {"url": "/api/cycle/status", "method": "GET"} in status["requests"]
    assert status["disabled"] is False


def test_stock_drawer_opens(page):
    """Clicking a popular stock opens the stock detail drawer with a price chart."""
    page.wait_for_selector("#popular-list .pop-row", timeout=15000)
    page.click("#popular-list .pop-row:first-child")
    page.wait_for_selector("#stock-drawer.open", timeout=5000)
    page.wait_for_function(
        "() => { const s = document.getElementById('s-sub'); return s && /\\$/.test(s.textContent); }",
        timeout=8000,
    )
    body = page.inner_html("#stock-body")
    assert "Failed to load" not in body
    assert "Holders" in body and "News" in body
    ranges = page.eval_on_selector_all(
        "[data-stock-range]", "buttons => buttons.map(button => button.dataset.stockRange)"
    )
    assert ranges == ["1D", "1W", "1M", "3M", "6M", "1Y"]
    assert page.query_selector("[data-stock-range].active").get_attribute("data-stock-range") == "1M"
    page.click("#stock-drawer .close")


def test_drawer_tabs_switch(page):
    names = [n for n in _first_username(page) if n]
    target = names[0].split()[0].lower()
    _open_and_assert_drawer(page, target)
    page.click('.tabs button[data-tab="history"]')
    assert page.is_visible("#tab-history")
    page.click('.tabs button[data-tab="performance"]')
    assert page.is_visible("#tab-performance")
    page.wait_for_timeout(500)
    assert "Failed to load" not in page.inner_html("#drawer")
    assert page._collected_errors == [], f"JS errors: {page._collected_errors}"


def test_decision_indicator_tracks_running_llm_and_websocket_updates(page):
    result = page.evaluate(
        """() => {
            const account = (username, user_type, rank) => ({
                username, user_type, rank, total_value: 1000, holdings_value: 500,
                cash_balance: 500, pnl_percent: 0,
            });
            lbData = [
                account('running-ai', 'llm_agent', 1),
                account('queued-ai', 'llm_agent', 2),
                account('completed-ai', 'llm_agent', 3),
                account('human', 'human', 4),
                account('index', 'index_fund', 5),
            ];
            sortKey = 'rank';
            sortDir = 1;
            renderDecisionBatchStatus({
                status: 'running', counts: {}, agents: {
                    'running-ai': {status: 'running'},
                    'queued-ai': {status: 'queued'},
                    'completed-ai': {status: 'completed'},
                },
            });
            const initialIndicators = [...document.querySelectorAll('.ai-decision-indicator')]
                .map(indicator => indicator.closest('.name-cell').textContent.trim());
            const initialLabel = document.querySelector('.ai-decision-indicator')?.getAttribute('aria-label');

            const originalFetch = window.fetch;
            const fetches = [];
            window.fetch = (...args) => { fetches.push(args[0]); return originalFetch(...args); };
            handleWebSocketMessage({
                type: 'DECISION_BATCH_UPDATED', data: {
                    status: 'running', counts: {}, agents: {
                        'running-ai': {status: 'completed'},
                        'queued-ai': {status: 'running'},
                        'completed-ai': {status: 'completed'},
                    },
                },
            });
            const updatedIndicators = [...document.querySelectorAll('.ai-decision-indicator')]
                .map(indicator => indicator.closest('.name-cell').textContent.trim());
            window.fetch = originalFetch;
            return {initialIndicators, initialLabel, updatedIndicators, fetches};
        }"""
    )

    assert result == {
        "initialIndicators": ["RUrunning-aiAI"],
        "initialLabel": "AI is analyzing",
        "updatedIndicators": ["QUqueued-aiAI"],
        "fetches": [],
    }


def test_instrument_suggestions_support_company_search_selection_and_direct_tickers(page):
    def fulfill_suggestions(route):
        query = route.request.url.split("query=", 1)[1].split("&", 1)[0]
        payload = {
            "suggestions": [
                {
                    "ticker": "AAPL",
                    "company_name": "Apple Inc.",
                    "instrument_type": "equity",
                    "exchange": "NASDAQ",
                    "category": None,
                }
            ]
            if query.lower() == "apple"
            else []
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/instrument-suggestions**", fulfill_suggestions)
    try:
        page.evaluate("() => { closeDrawer(); closeStockDrawer(); }")
        page.evaluate("""() => {
            window.openedSuggestionTickers = [];
            window.originalOpenDrawerTicker = window.openDrawerTicker;
            window.openDrawerTicker = ticker => window.openedSuggestionTickers.push(ticker);
        }""")
        page.fill("#stock-search-input", "Apple")
        page.wait_for_selector("#instrument-suggestions [role=option]")
        assert "AAPL" in page.text_content("#instrument-suggestions")
        assert "Apple Inc." in page.text_content("#instrument-suggestions")
        page.press("#stock-search-input", "ArrowDown")
        page.press("#stock-search-input", "Enter")
        assert page.evaluate("() => window.openedSuggestionTickers") == ["AAPL"]

        page.fill("#stock-search-input", "Apple")
        page.wait_for_selector("#instrument-suggestions [role=option]")
        page.click("#instrument-suggestion-0")
        assert page.evaluate("() => window.openedSuggestionTickers") == ["AAPL", "AAPL"]

        page.fill("#stock-search-input", "MSFT")
        page.wait_for_function(
            "() => document.querySelector('#instrument-suggestions').textContent.includes('No matching')"
        )
        page.press("#stock-search-input", "Enter")
        assert page.evaluate("() => window.openedSuggestionTickers") == ["AAPL", "AAPL", "MSFT"]
    finally:
        page.unroute("**/api/instrument-suggestions**")
        page.evaluate(
            "() => { if (window.originalOpenDrawerTicker) window.openDrawerTicker = window.originalOpenDrawerTicker; }"
        )


def test_websocket_refreshes_only_affected_views(page):
    refreshes = page.evaluate(
        """() => {
            const original = {
                refreshLeaderboard,
                loadActivity,
                renderDecisionBatchStatus,
            };
            const calls = { leaderboard: 0, activity: 0, decisionBatch: 0 };
            window.refreshLeaderboard = () => calls.leaderboard++;
            window.loadActivity = () => calls.activity++;
            window.renderDecisionBatchStatus = () => calls.decisionBatch++;
            document.getElementById('view-leaderboard').hidden = false;
            document.getElementById('view-activity').hidden = false;

            for (const message of [
                { type: 'ACCOUNT_STATE_UPDATE' },
                { type: 'NEWS_UPDATE' },
                { type: 'DECISION_BATCH_UPDATED', data: {} },
                { type: 'GATEKEEPER_ALERT', status: 'REJECTED' },
                { type: 'TRANSACTION_UPDATE' },
                { type: 'LEADERBOARD_UPDATE' },
                { type: 'PORTFOLIO_RESET' },
                { type: 'GATEKEEPER_ALERT', status: 'EXECUTED' },
            ]) handleWebSocketMessage(message);

            Object.assign(window, original);
            return calls;
        }"""
    )

    assert refreshes == {"leaderboard": 1, "activity": 3, "decisionBatch": 1}
