"""Browser (Playwright) tests for the web UI.

Boots the real FastAPI server on a spare port against the existing DB and
drives the page with a headless Chromium browser to verify that the
leaderboard renders and that clicking a player (including the index fund,
which regressed to a stuck "Loading..." drawer) populates the detail drawer.

Run:  pytest tests/test_web_ui.py
Skips automatically if playwright/browser are unavailable.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    # Run against a throwaway copy of the DB so tests never touch the live one.
    tmpdir = tempfile.mkdtemp(prefix="upt_test_db_")
    live_db = PROJECT_ROOT / "data" / "portfolio.db"
    test_db = Path(tmpdir) / "portfolio.db"
    if live_db.exists():
        shutil.copy2(live_db, test_db)
        for suffix in ("-wal", "-shm"):
            side = live_db.with_name(live_db.name + suffix)
            if side.exists():
                shutil.copy2(side, test_db.with_name(test_db.name + suffix))
    env = {**os.environ, "DB_PATH": str(test_db)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    base_url = f"http://127.0.0.1:{port}"
    # Wait for the server to accept connections.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                pytest.fail(f"Server exited early:\n{out}")
            time.sleep(0.3)
    else:
        proc.terminate()
        pytest.fail("Server did not start in time")
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def page(server):
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as e:  # browser not installed
        pytest.skip(f"Chromium unavailable: {e}")
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.goto(server, wait_until="domcontentloaded")
    pg._collected_errors = errors  # type: ignore[attr-defined]
    yield pg
    browser.close()
    pw.stop()


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


def test_chart_hover_shows_full_timestamp(page):
    result = page.evaluate(
        """async () => {
            const { history } = await (await fetch('/api/portfolio-history')).json();
            const timestamps = [...new Set(Object.values(history).flat().map(point => point.time))].sort();
            const title = Chart.getChart('lbChart').options.plugins.tooltip.callbacks.title([{ dataIndex: 0 }]);
            return { expected: new Date(timestamps[0]).toLocaleString(), title };
        }"""
    )
    assert result["title"] == result["expected"]


def test_leaderboard_chart_ends_at_the_table_valuation(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const valuesByUsername = Object.fromEntries(lbData.map(row => [row.username, Number(row.total_value)]));
            return chart.data.datasets.map(dataset => ({
                username: dataset.label,
                chartValue: dataset.data.at(-1),
                tableValue: valuesByUsername[dataset.label],
            }));
        }"""
    )

    assert result
    assert all(row["chartValue"] == row["tableValue"] for row in result)


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
    ranges = page.eval_on_selector_all("[data-stock-range]", "buttons => buttons.map(button => button.dataset.stockRange)")
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
        payload = {"suggestions": [{"ticker": "AAPL", "company_name": "Apple Inc.", "instrument_type": "equity", "exchange": "NASDAQ", "category": None}] if query.lower() == "apple" else []}
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
        page.wait_for_function("() => document.querySelector('#instrument-suggestions').textContent.includes('No matching')")
        page.press("#stock-search-input", "Enter")
        assert page.evaluate("() => window.openedSuggestionTickers") == ["AAPL", "AAPL", "MSFT"]
    finally:
        page.unroute("**/api/instrument-suggestions**")
        page.evaluate("() => { if (window.originalOpenDrawerTicker) window.openDrawerTicker = window.originalOpenDrawerTicker; }")


def test_websocket_refreshes_only_affected_views(page):
    refreshes = page.evaluate(
        """() => {
            const original = {
                loadLeaderboard,
                loadActivity,
                renderDecisionBatchStatus,
            };
            const calls = { leaderboard: 0, activity: 0, decisionBatch: 0 };
            window.loadLeaderboard = () => calls.leaderboard++;
            window.loadActivity = () => calls.activity++;
            window.renderDecisionBatchStatus = () => calls.decisionBatch++;
            document.getElementById('view-leaderboard').style.display = 'flex';
            document.getElementById('view-activity').style.display = 'block';

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

    assert refreshes == {"leaderboard": 3, "activity": 3, "decisionBatch": 1}
