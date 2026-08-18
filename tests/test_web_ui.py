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
    ]
    portfolio_history = {
        "2": [
            {"time": timestamp, "value": 10_000, "pnl": 0, "pnl_percent": 0},
            {"time": latest, "value": 10_050, "pnl": 50, "pnl_percent": 0.5},
        ],
        "1": [
            {"time": timestamp, "value": 10_000, "pnl": 0, "pnl_percent": 0},
            {"time": later, "value": 10_100, "pnl": 100, "pnl_percent": 1},
            {"time": latest, "value": 10_200, "pnl": 200, "pnl_percent": 2},
        ],
        "3": [
            {"time": timestamp, "value": 9_900, "pnl": -100, "pnl_percent": -1},
            {"time": later, "value": 10_400, "pnl": 400, "pnl_percent": 4},
            {"time": latest, "value": 9_800, "pnl": -200, "pnl_percent": -2},
        ],
    }
    portfolio_users = {"2": "Indexer", "3": "Running AI", "1": "Taavet"}

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

    watchlist = [
        {
            "ticker": "AAPL" if index == 0 else f"T{index:03d}",
            "company": "Apple Inc." if index == 0 else f"Test Instrument {index}",
            "company_name": "Apple Inc." if index == 0 else f"Test Instrument {index}",
            "instrument_type": "equity" if index % 2 == 0 else "etf",
            "sector": "Technology",
            "category": None,
            "price": 225 + index,
            "change_percent": 2.27,
            "volume": 10_000_000,
            "total": 75,
        }
        for index in range(75)
    ]

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

    decisions = [
        {
            "id": 21 - index,
            "time": latest,
            "decision": "BUY" if index % 3 == 0 else "HOLD",
            "ticker": "AAPL" if index % 3 == 0 else None,
            "allocation_percentage": 0.1 if index % 3 == 0 else None,
            "reasoning": f"Fixture reasoning {index}.",
            "response_status": "parsed",
            "execution_status": "executed" if index % 3 == 0 else "hold",
            "rejection": None,
            "provider": "test",
            "model_name": "fixture-model",
            "market_snapshot_at": latest,
        }
        for index in range(13)
    ]

    def response(url):
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/leaderboard":
            return leaderboard
        if path == "/api/portfolio-history":
            return {"history": portfolio_history, "users": portfolio_users}
        if path.startswith("/api/agent-detail/"):
            return detail(path.rsplit("/", 1)[1])
        if path == "/api/agents/running-ai/decisions":
            before_id = int(query.get("before_id", ["0"])[0] or 0)
            remaining = [row for row in decisions if not before_id or row["id"] < before_id]
            return remaining[:10]
        if path == "/api/watchlist":
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["50"])[0])
            return watchlist[offset : offset + limit]
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
    context = browser.new_context(has_touch=True)
    page = context.new_page()
    errors = []
    requests = []
    request_urls = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def fulfill_api(route):
        request_urls.append(route.request.url)
        requests.append(urlparse(route.request.url).path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(browser_api(route.request.url)))

    page.route("**/api/**", fulfill_api)
    page.goto(server, wait_until="domcontentloaded")
    page._api_requests = requests  # type: ignore[attr-defined]
    page._api_request_urls = request_urls  # type: ignore[attr-defined]
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
    assert "/api/watchlist" not in page._api_requests
    assert not any(path.startswith("/api/agent-detail/") for path in page._api_requests)
    assert page.text_content("#popular-list") == "Open Markets to load instruments."


def test_markets_navigation_loads_the_watchlist_only_when_opened(page):
    initial_watchlist_requests = page._api_requests.count("/api/watchlist")
    try:
        page.click("#nav-markets")
        page.wait_for_selector("#popular-list .pop-row", timeout=15000)
        first_load_requests = page._api_requests.count("/api/watchlist")
        page.click("#nav-lb")
        page.click("#nav-markets")
        page.wait_for_timeout(100)
        second_open_requests = page._api_requests.count("/api/watchlist")
        state = {
            "marketsVisible": page.locator("#view-markets").is_visible(),
            "leaderboardHidden": not page.locator("#view-leaderboard").is_visible(),
            "marketsNavigationActive": page.locator("#nav-markets").evaluate(
                "element => element.classList.contains('active')"
            ),
        }
    finally:
        page.click("#nav-lb")

    assert initial_watchlist_requests == 0
    assert first_load_requests == 1
    assert second_open_requests == 1
    assert state == {
        "marketsVisible": True,
        "leaderboardHidden": True,
        "marketsNavigationActive": True,
    }


def test_markets_loads_the_catalogue_in_pages_when_the_list_end_is_reached(page):
    try:
        page.click("#nav-markets")
        page.wait_for_function("() => document.querySelectorAll('#popular-list .pop-row').length === 50")
        page.locator("#market-load-sentinel").scroll_into_view_if_needed()
        page.wait_for_function("() => document.querySelectorAll('#popular-list .pop-row').length === 75")

        requests = [urlparse(url) for url in page._api_request_urls if urlparse(url).path == "/api/watchlist"]

        assert [(request.query, parse_qs(request.query)) for request in requests] == [
            ("limit=50&offset=0", {"limit": ["50"], "offset": ["0"]}),
            ("limit=50&offset=50", {"limit": ["50"], "offset": ["50"]}),
        ]
        assert page.text_content("#market-catalogue-status") == "Showing 75 of 75 active instruments."
    finally:
        page.click("#nav-lb")


def test_main_page_prioritizes_chart_and_table_over_automation_controls(page):
    hierarchy = page.evaluate(
        """() => {
            const main = document.querySelector('.lb-main');
            const automation = document.getElementById('automation-panel');
            return {
                childIds: [...main.children].map(child => child.id),
                legacyPanels: [
                    Boolean(document.getElementById('decision-panel')),
                    Boolean(document.getElementById('refresh-panel')),
                ],
                title: document.getElementById('automation-title').textContent,
                taskTitles: [...automation.querySelectorAll('.automation-task-title')]
                    .map(title => title.textContent),
                controlsInPanel: [
                    'batch-decision-btn',
                    'funnel-refresh-btn',
                    'batch-decision-msg',
                    'funnel-refresh-msg',
                ].every(id => automation.contains(document.getElementById(id))),
            };
        }"""
    )

    assert hierarchy == {
        "childIds": ["kpis", "portfolio-chart-panel", "lb-table", "automation-panel"],
        "legacyPanels": [False, False],
        "title": "Automation",
        "taskTitles": ["AI decisions", "Scheduled market & news refresh"],
        "controlsInPanel": True,
    }


def test_automation_panel_stacks_before_the_sidebar_makes_the_main_column_narrow(page):
    viewport = page.viewport_size
    try:
        page.set_viewport_size({"width": 800, "height": 900})
        layout = page.evaluate(
            """() => {
                const panel = document.getElementById('automation-panel');
                const [decisions, refresh] = panel.querySelectorAll('.automation-task');
                const decisionDashboard = decisions.querySelector('.decision-dashboard');
                return {
                    refreshBelowDecisions: refresh.getBoundingClientRect().top > decisions.getBoundingClientRect().top,
                    panelFits: panel.scrollWidth <= panel.clientWidth,
                    decisionDashboardColumns: getComputedStyle(decisionDashboard).gridTemplateColumns.split(' ').length,
                };
            }"""
        )
    finally:
        page.set_viewport_size(viewport)

    assert layout == {
        "refreshBelowDecisions": True,
        "panelFits": True,
        "decisionDashboardColumns": 1,
    }


def test_activity_navigation_replaces_the_leaderboard(page):
    page.click("#nav-act")
    try:
        page.wait_for_selector("#view-activity:not([hidden])")
        page.wait_for_selector("#act-body tr")
        assert page.locator("#view-activity").is_visible()
        assert not page.locator("#view-leaderboard").is_visible()
        assert page.locator("#nav-act").evaluate("element => element.classList.contains('active')")
    finally:
        page.click("#nav-lb")

    assert page.locator("#view-leaderboard").is_visible()
    assert not page.locator("#view-activity").is_visible()


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


def test_portfolio_chart_hides_native_legend_and_exposes_a_ranked_direct_focus_legend(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            return {
                leaderboardOrder: lbData.map(player => player.user_id),
                legendDisplayed: chart.options.plugins.legend.display,
                selectorOptions: [...document.getElementById('lb-chart-player').options]
                    .map(option => ({value: option.value, text: option.text})),
                directLegend: [...document.querySelectorAll('#lb-chart-legend button')].map(button => ({
                    value: button.dataset.lbChartPlayer,
                    text: button.textContent,
                    pressed: button.getAttribute('aria-pressed'),
                    colorIndex: button.querySelector('.portfolio-chart-legend-swatch')?.dataset.chartColor || null,
                })),
                datasetIds: chart.data.datasets.map(dataset => dataset.portfolioUserId),
                datasetColorIndexes: chart.data.datasets.map(dataset => dataset.portfolioColorIndex),
            };
        }"""
    )

    assert result == {
        "leaderboardOrder": [3, 1, 2],
        "legendDisplayed": False,
        "selectorOptions": [
            {"value": "", "text": "All players"},
            {"value": "1", "text": "#1 Taavet"},
            {"value": "2", "text": "#2 Indexer"},
            {"value": "3", "text": "#3 Running AI"},
        ],
        "directLegend": [
            {"value": "", "text": "All players", "pressed": "true", "colorIndex": None},
            {"value": "1", "text": "#1 Taavet", "pressed": "false", "colorIndex": "0"},
            {"value": "2", "text": "#2 Indexer", "pressed": "false", "colorIndex": "1"},
            {"value": "3", "text": "#3 Running AI", "pressed": "false", "colorIndex": "2"},
        ],
        "datasetIds": ["1", "2", "3"],
        "datasetColorIndexes": [0, 1, 2],
    }


def test_portfolio_chart_ranked_legend_focuses_one_player_and_restores_all_players(page):
    try:
        page.click('#lb-chart-legend button[data-lb-chart-player="2"]')
        focused = page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                return {
                    visibleIds: chart.data.datasets
                        .filter((_, index) => chart.isDatasetVisible(index))
                        .map(dataset => dataset.portfolioUserId),
                    selectorValue: document.getElementById('lb-chart-player').value,
                    pressedValues: [...document.querySelectorAll('#lb-chart-legend button')]
                        .filter(button => button.getAttribute('aria-pressed') === 'true')
                        .map(button => button.dataset.lbChartPlayer),
                    announcement: document.getElementById('lb-chart-announcements').textContent,
                };
            }"""
        )
        page.click('#lb-chart-legend button[data-lb-chart-player=""]')
        restored = page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                return {
                    visibleIds: chart.data.datasets
                        .filter((_, index) => chart.isDatasetVisible(index))
                        .map(dataset => dataset.portfolioUserId),
                    selectorValue: document.getElementById('lb-chart-player').value,
                    pressedValues: [...document.querySelectorAll('#lb-chart-legend button')]
                        .filter(button => button.getAttribute('aria-pressed') === 'true')
                        .map(button => button.dataset.lbChartPlayer),
                    announcement: document.getElementById('lb-chart-announcements').textContent,
                };
            }"""
        )
    finally:
        page.select_option("#lb-chart-player", "")

    assert focused == {
        "visibleIds": ["2"],
        "selectorValue": "2",
        "pressedValues": ["2"],
        "announcement": "Showing Indexer only.",
    }
    assert restored == {
        "visibleIds": ["1", "2", "3"],
        "selectorValue": "",
        "pressedValues": [""],
        "announcement": "Showing all players.",
    }


def test_portfolio_chart_hover_emphasizes_the_nearest_line_and_restores_all_lines(page):
    page.wait_for_function("() => Chart.getChart('lbChart')?.data.datasets.length === 3")
    canvas = page.locator("#lbChart")
    box = canvas.bounding_box()
    assert box is not None
    target = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const datasetIndex = chart.data.datasets.findIndex(dataset => dataset.portfolioUserId === '3');
            const firstDataIndex = chart.data.datasets[datasetIndex].data.findIndex(point =>
                point.x === Date.parse('2026-07-28T12:00:00+00:00'));
            const laterDataIndex = chart.data.datasets[datasetIndex].data.findIndex(point =>
                point.x === Date.parse('2026-07-29T12:00:00+00:00'));
            const points = chart.getDatasetMeta(datasetIndex).data;
            const x = (points[firstDataIndex].x + points[laterDataIndex].x) / 2;
            const point = chart.getDatasetMeta(datasetIndex).dataset.interpolate({x}, 'x');
            return {x: point.x, y: point.y, width: chart.width, height: chart.height};
        }"""
    )

    page.mouse.move(
        box["x"] + target["x"] / target["width"] * box["width"],
        box["y"] + target["y"] / target["height"] * box["height"],
    )
    page.wait_for_function(
        """() => {
            const datasets = Chart.getChart('lbChart').data.datasets;
            const highlighted = datasets.find(dataset => dataset.portfolioUserId === '3');
            return highlighted.borderWidth === 4
                && datasets.filter(dataset => dataset !== highlighted).every(dataset => dataset.borderWidth === 1);
        }"""
    )
    highlighted = page.evaluate(
        """() => Chart.getChart('lbChart').data.datasets.map(dataset => ({
            playerId: dataset.portfolioUserId,
            borderWidth: dataset.borderWidth,
            borderColor: dataset.borderColor,
        }))"""
    )

    page.mouse.move(box["x"] + box["width"] + 20, box["y"] + 20)
    page.wait_for_function(
        """() => Chart.getChart('lbChart').data.datasets
            .every(dataset => dataset.borderWidth === 2 && /^#[0-9a-f]{6}$/i.test(dataset.borderColor))"""
    )
    restored = page.evaluate(
        """() => Chart.getChart('lbChart').data.datasets.map(dataset => ({
            playerId: dataset.portfolioUserId,
            borderWidth: dataset.borderWidth,
            borderColor: dataset.borderColor,
        }))"""
    )

    assert next(dataset for dataset in highlighted if dataset["playerId"] == "3") == {
        "playerId": "3",
        "borderWidth": 4,
        "borderColor": "#9a6700",
    }
    assert all(dataset["borderWidth"] == 1 for dataset in highlighted if dataset["playerId"] != "3")
    assert all(dataset["borderWidth"] == 2 for dataset in restored)
    assert (
        page.text_content("#lb-chart-hint")
        == "Hover a line to highlight it · Ctrl/⌘ + scroll or pinch to zoom · drag to pan"
    )


def test_portfolio_chart_uses_unique_colour_matched_legend_entries_for_a_large_league(page):
    result = page.evaluate(
        """async () => {
            const originalFetch = window.fetch;
            const at = '2026-08-01T12:00:00+00:00';
            const rankings = Array.from({length: 12}, (_, index) => ({
                user_id: 100 + index,
                username: `player-${index + 1}`,
                display_name: `Player ${index + 1}`,
                user_type: 'llm_agent',
                decision_architecture: 'single_model',
                rank: index + 1,
                total_value: 10_000 + index,
                holdings_value: 0,
                cash_balance: 10_000 + index,
                pnl_percent: index / 10,
            })).reverse();
            const orderedRankings = [...rankings].reverse();
            const portfolio = {
                history: Object.fromEntries(orderedRankings.map(player => [String(player.user_id), [{
                    time: at,
                    value: player.total_value,
                    pnl: player.total_value - 10_000,
                    pnl_percent: player.pnl_percent,
                }]])),
                users: Object.fromEntries(orderedRankings.map(player => [String(player.user_id), player.display_name])),
            };
            const response = body => new Response(JSON.stringify(body), {
                status: 200,
                headers: {'Content-Type': 'application/json'},
            });
            let chartState;
            window.fetch = url => {
                if (String(url) === '/api/leaderboard') return Promise.resolve(response(rankings));
                if (String(url) === '/api/portfolio-history') return Promise.resolve(response(portfolio));
                return originalFetch(url);
            };
            try {
                await refreshLeaderboard();
                const chart = Chart.getChart('lbChart');
                const datasetIndexes = Object.fromEntries(chart.data.datasets.map(dataset => [
                    dataset.portfolioUserId,
                    dataset.portfolioColorIndex,
                ]));
                const legend = [...document.querySelectorAll('#lb-chart-legend button')].map(button => ({
                    playerId: button.dataset.lbChartPlayer,
                    label: button.textContent,
                    colorIndex: button.querySelector('.portfolio-chart-legend-swatch')?.dataset.chartColor || null,
                }));
                chartState = {
                    legendLabels: legend.map(entry => entry.label),
                    uniqueDatasetColours: new Set(Object.values(datasetIndexes)).size,
                    datasetCount: chart.data.datasets.length,
                    legendColoursMatch: legend.slice(1).every(entry =>
                        Number(entry.colorIndex) === datasetIndexes[entry.playerId]),
                };
            } finally {
                window.fetch = originalFetch;
                await refreshLeaderboard();
            }
            return chartState;
        }"""
    )

    assert result == {
        "legendLabels": ["All players", *[f"#{rank} Player {rank}" for rank in range(1, 13)]],
        "uniqueDatasetColours": 12,
        "datasetCount": 12,
        "legendColoursMatch": True,
    }


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


def test_portfolio_chart_player_focus_fits_visible_y_axis_and_can_restore_all_players(page):
    try:
        page.select_option("#lb-chart-player", "2")
        focused = page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                const visible = chart.data.datasets.filter((_, index) => chart.isDatasetVisible(index));
                return {
                    visibleIds: visible.map(dataset => dataset.portfolioUserId),
                    focusedValues: visible.flatMap(dataset => dataset.data.map(point => point.y).filter(Number.isFinite)),
                    allValues: chart.data.datasets.flatMap(dataset => dataset.data.map(point => point.y).filter(Number.isFinite)),
                    axisMin: chart.scales.y.min,
                    axisMax: chart.scales.y.max,
                    summary: document.getElementById('lb-chart-summary').textContent,
                    canvasLabel: document.getElementById('lbChart').getAttribute('aria-label'),
                };
            }"""
        )

        page.select_option("#lb-chart-player", "")
        restored = page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                const values = chart.data.datasets.flatMap(dataset => dataset.data.map(point => point.y).filter(Number.isFinite));
                return {
                    visibleIds: chart.data.datasets
                        .filter((_, index) => chart.isDatasetVisible(index))
                        .map(dataset => dataset.portfolioUserId),
                    axisMin: chart.scales.y.min,
                    axisMax: chart.scales.y.max,
                    valueMin: Math.min(...values),
                    valueMax: Math.max(...values),
                    summaryHidden: document.getElementById('lb-chart-summary').hidden,
                };
            }"""
        )
    finally:
        page.select_option("#lb-chart-player", "")

    assert focused["visibleIds"] == ["2"]
    assert focused["summary"] == "Indexer: $10,100.00 · +1.00% · #2"
    assert focused["canvasLabel"] == "Portfolio value chart for Indexer"
    assert focused["axisMin"] < min(focused["focusedValues"])
    assert focused["axisMax"] > max(focused["focusedValues"])
    assert focused["axisMin"] > min(focused["allValues"])
    assert focused["axisMax"] < max(focused["allValues"])
    assert restored["visibleIds"] == ["1", "2", "3"]
    assert restored["axisMin"] < restored["valueMin"]
    assert restored["axisMax"] > restored["valueMax"]
    assert restored["summaryHidden"] is True


def test_portfolio_chart_tooltip_value_ranks_rows_and_marks_carried_values(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const tooltip = chart.options.plugins.tooltip;
            const at = Date.parse('2026-07-29T12:00:00+00:00');
            const initialAt = Date.parse('2026-07-28T12:00:00+00:00');
            const contextsAt = time => chart.data.datasets.map((dataset, datasetIndex) => {
                const raw = dataset.data.find(point => point.x === time);
                return {dataset, datasetIndex, raw, parsed: {x: raw.x, y: raw.y}};
            });
            const rows = contextsAt(at).sort(tooltip.itemSort);
            const initialRows = contextsAt(initialAt).sort(tooltip.itemSort);
            return {
                expectedTitle: new Date(at).toLocaleString(),
                title: tooltip.callbacks.title(rows),
                labels: rows.map(tooltip.callbacks.label),
                labelColors: rows.map(tooltip.callbacks.labelTextColor),
                initialOrder: initialRows.map(row => row.dataset.label),
                observedAt: new Date(initialAt).toLocaleString(),
            };
        }"""
    )

    assert result["title"] == result["expectedTitle"]
    assert result["labels"] == [
        "#1 Running AI: $10,400.00 (+$500.00 +5.05%)",
        "#2 Taavet: $10,100.00 (+$100.00 +1.00%)",
        f"#3 Indexer: $10,000.00 · as of {result['observedAt']}",
    ]
    assert result["labelColors"] == ["#1a7f37", "#1a7f37", "#1f2328"]
    assert result["initialOrder"] == ["Taavet", "Indexer", "Running AI"]


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
        """async () => {
            const chart = Chart.getChart('lbChart');
            chart.zoom(1.5);
            syncLbChartZoomState();
            const before = {chart, min: chart.scales.x.min, max: chart.scales.x.max};
            handleWebSocketMessage({type: 'LEADERBOARD_UPDATE'});
            await leaderboardRefreshInFlight;
            const result = {
                sameInstance: Chart.getChart('lbChart') === before.chart,
                min: chart.scales.x.min,
                max: chart.scales.x.max,
                beforeMin: before.min,
                beforeMax: before.max,
            };
            document.getElementById('lb-chart-reset').click();
            return result;
        }"""
    )

    assert result["sameInstance"] is True
    assert result["min"] == pytest.approx(result["beforeMin"])
    assert result["max"] == pytest.approx(result["beforeMax"])


def test_leaderboard_chart_zoom_pan_and_reset(page):
    result = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const reset = document.getElementById('lb-chart-reset');
            const configured = Boolean(chart.options.plugins.zoom?.zoom.wheel.enabled)
                && Boolean(chart.options.plugins.zoom?.zoom.pinch.enabled)
                && Boolean(chart.options.plugins.zoom?.pan.enabled);
            const wheelModifier = chart.options.plugins.zoom?.zoom.wheel.modifierKey;
            const initiallyDisabled = reset.disabled;
            chart.zoom(1.5);
            syncLbChartZoomState();
            const zoomed = {min: chart.scales.x.min, max: chart.scales.x.max};
            chart.pan({x: 40});
            syncLbChartZoomState();
            const panned = Math.abs(chart.scales.x.min - zoomed.min) > 1
                || Math.abs(chart.scales.x.max - zoomed.max) > 1;
            const enabledAfterNavigation = !reset.disabled && chart.isZoomedOrPanned();
            reset.click();
            return {
                configured,
                wheelModifier,
                initiallyDisabled,
                panned,
                enabledAfterNavigation,
                disabledAfterReset: reset.disabled,
                zoomedAfterReset: chart.isZoomedOrPanned(),
            };
        }"""
    )

    assert result == {
        "configured": True,
        "wheelModifier": "ctrl",
        "initiallyDisabled": True,
        "panned": True,
        "enabledAfterNavigation": True,
        "disabledAfterReset": True,
        "zoomedAfterReset": False,
    }


def test_portfolio_chart_preserves_focus_range_and_colours_during_websocket_refresh(page):
    result = page.evaluate(
        """async () => {
            const originalFetch = window.fetch;
            const [rankings, portfolio] = await Promise.all([
                originalFetch('/api/leaderboard').then(response => response.json()),
                originalFetch('/api/portfolio-history').then(response => response.json()),
            ]);
            const player = document.getElementById('lb-chart-player');
            const allRange = document.querySelector('[data-lb-chart-range="ALL"]');
            const thirtyDayRange = document.querySelector('[data-lb-chart-range="30D"]');
            player.value = '2';
            player.dispatchEvent(new Event('change', {bubbles: true}));
            thirtyDayRange.click();
            const before = Chart.getChart('lbChart');
            const beforeColors = Object.fromEntries(
                before.data.datasets.map(dataset => [dataset.portfolioUserId, dataset.borderColor]),
            );
            const legendColors = () => Object.fromEntries(
                [...document.querySelectorAll('#lb-chart-legend button[data-lb-chart-player]')]
                    .filter(button => button.dataset.lbChartPlayer)
                    .map(button => [
                        button.dataset.lbChartPlayer,
                        button.querySelector('.portfolio-chart-legend-swatch').dataset.chartColor,
                    ]),
            );
            const beforeLegendColors = legendColors();
            const response = body => new Response(JSON.stringify(body), {
                status: 200,
                headers: {'Content-Type': 'application/json'},
            });
            window.fetch = (url, options) => {
                if (String(url) === '/api/leaderboard') return Promise.resolve(response([...rankings].reverse()));
                if (String(url) === '/api/portfolio-history') {
                    return Promise.resolve(response({
                        history: Object.fromEntries([...Object.entries(portfolio.history)].reverse()),
                        users: Object.fromEntries([...Object.entries(portfolio.users)].reverse()),
                    }));
                }
                return originalFetch(url, options);
            };
            let result;
            try {
                handleWebSocketMessage({type: 'LEADERBOARD_UPDATE'});
                await leaderboardRefreshInFlight;
                const chart = Chart.getChart('lbChart');
                result = {
                    sameInstance: chart === before,
                    beforeColors,
                    afterColors: Object.fromEntries(
                        chart.data.datasets.map(dataset => [dataset.portfolioUserId, dataset.borderColor]),
                    ),
                    beforeLegendColors,
                    afterLegendColors: legendColors(),
                    activeLegendIds: [...document.querySelectorAll('#lb-chart-legend button[aria-pressed="true"]')]
                        .map(button => button.dataset.lbChartPlayer),
                    playerValue: player.value,
                    selectedRange: document.querySelector('[data-lb-chart-range][aria-pressed="true"]').dataset.lbChartRange,
                    visibleIds: chart.data.datasets
                        .filter((_, index) => chart.isDatasetVisible(index))
                        .map(dataset => dataset.portfolioUserId),
                    datasetIds: chart.data.datasets.map(dataset => dataset.portfolioUserId),
                };
            } finally {
                window.fetch = originalFetch;
                player.value = '';
                player.dispatchEvent(new Event('change', {bubbles: true}));
                allRange.click();
                await refreshLeaderboard();
            }
            return result;
        }"""
    )

    assert result["sameInstance"] is True
    assert result["beforeColors"] == result["afterColors"]
    assert result["beforeLegendColors"] == result["afterLegendColors"]
    assert result["activeLegendIds"] == ["2"]
    assert result["playerValue"] == "2"
    assert result["selectedRange"] == "30D"
    assert result["visibleIds"] == ["2"]
    assert result["datasetIds"] == ["1", "2", "3"]


def test_portfolio_chart_resets_focus_when_the_selected_player_disappears(page):
    result = page.evaluate(
        """async () => {
            const originalFetch = window.fetch;
            const [rankings, portfolio] = await Promise.all([
                originalFetch('/api/leaderboard').then(response => response.json()),
                originalFetch('/api/portfolio-history').then(response => response.json()),
            ]);
            const player = document.getElementById('lb-chart-player');
            player.value = '2';
            player.dispatchEvent(new Event('change', {bubbles: true}));
            const response = body => new Response(JSON.stringify(body), {
                status: 200,
                headers: {'Content-Type': 'application/json'},
            });
            window.fetch = (url, options) => {
                if (String(url) === '/api/leaderboard') {
                    return Promise.resolve(response(rankings.filter(ranking => ranking.user_id !== 2)));
                }
                if (String(url) === '/api/portfolio-history') {
                    return Promise.resolve(response({
                        history: Object.fromEntries(
                            Object.entries(portfolio.history).filter(([playerId]) => playerId !== '2'),
                        ),
                        users: Object.fromEntries(
                            Object.entries(portfolio.users).filter(([playerId]) => playerId !== '2'),
                        ),
                    }));
                }
                return originalFetch(url, options);
            };
            let result;
            try {
                await refreshLeaderboard();
                const chart = Chart.getChart('lbChart');
                result = {
                    playerValue: player.value,
                    optionValues: [...player.options].map(option => option.value),
                    visibleIds: chart.data.datasets
                        .filter((_, index) => chart.isDatasetVisible(index))
                        .map(dataset => dataset.portfolioUserId),
                    summaryHidden: document.getElementById('lb-chart-summary').hidden,
                    canvasLabel: document.getElementById('lbChart').getAttribute('aria-label'),
                    announcement: document.getElementById('lb-chart-announcements').textContent,
                };
            } finally {
                window.fetch = originalFetch;
                await refreshLeaderboard();
            }
            return result;
        }"""
    )

    assert result == {
        "playerValue": "",
        "optionValues": ["", "1", "3"],
        "visibleIds": ["1", "3"],
        "summaryHidden": True,
        "canvasLabel": "Portfolio value comparison chart for all players",
        "announcement": "The selected player is no longer available. Showing all players.",
    }


def test_portfolio_chart_has_loading_empty_error_and_retry_states(page):
    page.evaluate(
        """() => {
            const originalFetch = window.fetch;
            window.portfolioHistoryMode = 'pending';
            window.fetch = async (url, options) => {
                if (String(url) !== '/api/portfolio-history') return originalFetch(url, options);
                if (window.portfolioHistoryMode === 'empty') {
                    return new Response(JSON.stringify({history: {}, users: {}}), {
                        status: 200,
                        headers: {'Content-Type': 'application/json'},
                    });
                }
                if (window.portfolioHistoryMode === 'error') {
                    return new Response(JSON.stringify({error: 'History is unavailable'}), {
                        status: 503,
                        headers: {'Content-Type': 'application/json'},
                    });
                }
                if (window.portfolioHistoryMode === 'pending') {
                    return new Promise(resolve => {
                        window.resolvePortfolioHistory = () => resolve(originalFetch(url, options));
                    });
                }
                return originalFetch(url, options);
            };
            window.restorePortfolioHistoryFetch = async () => {
                window.portfolioHistoryMode = 'normal';
                window.resolvePortfolioHistory?.();
                await window.pendingPortfolioChartRefresh?.catch(() => {});
                window.fetch = originalFetch;
                delete window.portfolioHistoryMode;
                delete window.resolvePortfolioHistory;
                delete window.pendingPortfolioChartRefresh;
                if (!Chart.getChart('lbChart')) await refreshLeaderboard();
            };
        }"""
    )
    try:
        page.evaluate("() => { window.pendingPortfolioChartRefresh = refreshLeaderboard(); }")
        page.wait_for_function(
            "() => document.getElementById('lb-chart-status').textContent === 'Refreshing portfolio history…'"
        )
        loading = page.evaluate(
            """() => ({
                busy: document.getElementById('lbChart').getAttribute('aria-busy'),
                playerDisabled: document.getElementById('lb-chart-player').disabled,
                rangesDisabled: [...document.querySelectorAll('[data-lb-chart-range]')].every(button => button.disabled),
            })"""
        )

        page.evaluate(
            """async () => {
                window.portfolioHistoryMode = 'normal';
                window.resolvePortfolioHistory();
                await window.pendingPortfolioChartRefresh;
            }"""
        )
        page.wait_for_function("() => !document.getElementById('lb-chart-status').textContent")

        empty = page.evaluate(
            """async () => {
                window.portfolioHistoryMode = 'empty';
                await refreshLeaderboard();
                return {
                    chartExists: Boolean(Chart.getChart('lbChart')),
                    status: document.getElementById('lb-chart-status').textContent,
                    state: document.getElementById('lb-chart-status').dataset.state,
                    retryHidden: document.getElementById('lb-chart-retry').hidden,
                    playerDisabled: document.getElementById('lb-chart-player').disabled,
                    rangesDisabled: [...document.querySelectorAll('[data-lb-chart-range]')].every(button => button.disabled),
                    resetDisabled: document.getElementById('lb-chart-reset').disabled,
                };
            }"""
        )

        error = page.evaluate(
            """async () => {
                window.portfolioHistoryMode = 'error';
                await refreshLeaderboard();
                return {
                    status: document.getElementById('lb-chart-status').textContent,
                    state: document.getElementById('lb-chart-status').dataset.state,
                    retryVisible: !document.getElementById('lb-chart-retry').hidden,
                    playerDisabled: document.getElementById('lb-chart-player').disabled,
                    rangesDisabled: [...document.querySelectorAll('[data-lb-chart-range]')].every(button => button.disabled),
                    resetDisabled: document.getElementById('lb-chart-reset').disabled,
                };
            }"""
        )

        page.evaluate("() => { window.portfolioHistoryMode = 'normal'; }")
        page.click("#lb-chart-retry")
        page.wait_for_function(
            """() => Boolean(Chart.getChart('lbChart'))
                && !document.getElementById('lb-chart-status').textContent
                && document.getElementById('lb-chart-retry').hidden"""
        )
        recovered = page.evaluate(
            """() => ({
                busy: document.getElementById('lbChart').getAttribute('aria-busy'),
                playerDisabled: document.getElementById('lb-chart-player').disabled,
                rangesDisabled: [...document.querySelectorAll('[data-lb-chart-range]')].every(button => button.disabled),
            })"""
        )
    finally:
        page.evaluate("() => window.restorePortfolioHistoryFetch?.()")

    assert loading == {"busy": "true", "playerDisabled": False, "rangesDisabled": False}
    assert empty == {
        "chartExists": False,
        "status": "No portfolio history is available yet. Check back after the first valuation.",
        "state": "empty",
        "retryHidden": True,
        "playerDisabled": True,
        "rangesDisabled": True,
        "resetDisabled": True,
    }
    assert error == {
        "status": "Couldn’t load portfolio history. Please try again.",
        "state": "error",
        "retryVisible": True,
        "playerDisabled": True,
        "rangesDisabled": True,
        "resetDisabled": True,
    }
    assert recovered == {"busy": "false", "playerDisabled": False, "rangesDisabled": False}


def test_portfolio_chart_controls_work_by_keyboard_and_announce_changes(page):
    try:
        assert not page.locator("#lb-chart-player").is_disabled()
        page.focus("#lb-chart-player")
        assert page.evaluate("() => document.activeElement.id") == "lb-chart-player"
        page.select_option("#lb-chart-player", "1")
        focused = page.evaluate(
            """() => ({
                visibleDatasets: Chart.getChart('lbChart').data.datasets
                    .filter((_, index) => Chart.getChart('lbChart').isDatasetVisible(index)).length,
                announcement: document.getElementById('lb-chart-announcements').textContent,
            })"""
        )

        page.focus('[data-lb-chart-range="7D"]')
        page.keyboard.press("Space")
        ranged = page.evaluate(
            """() => ({
                pressed: document.querySelector('[data-lb-chart-range="7D"]').getAttribute('aria-pressed'),
                announcement: document.getElementById('lb-chart-announcements').textContent,
            })"""
        )

        page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                chart.zoom(1.5);
                syncLbChartZoomState();
            }"""
        )
        page.wait_for_function("() => !document.getElementById('lb-chart-reset').disabled")
        page.focus("#lb-chart-reset")
        page.keyboard.press("Enter")
        reset = page.evaluate(
            """() => ({
                disabled: document.getElementById('lb-chart-reset').disabled,
                announcement: document.getElementById('lb-chart-announcements').textContent,
            })"""
        )
    finally:
        page.select_option("#lb-chart-player", "")
        page.click('[data-lb-chart-range="ALL"]')

    assert focused == {"visibleDatasets": 1, "announcement": "Showing Taavet only."}
    assert ranged == {"pressed": "true", "announcement": "Showing the last 7 days."}
    assert reset == {"disabled": True, "announcement": "View reset to the last 7 days."}


def test_portfolio_chart_tooltip_uses_touch_events_and_high_contrast_colours(page):
    tooltip = page.evaluate(
        """() => {
            const chart = Chart.getChart('lbChart');
            const options = chart.options.plugins.tooltip;
            return {
                events: chart.options.events,
                interactionAxis: chart.options.interaction.axis,
                backgroundColor: options.backgroundColor,
                titleColor: options.titleColor,
                bodyColor: options.bodyColor,
                borderColor: options.borderColor,
                borderWidth: options.borderWidth,
                activeTransitionDuration: chart.options.transitions.active.animation.duration,
            };
        }"""
    )

    assert tooltip == {
        "events": ["mousemove", "mouseout", "click", "touchstart", "touchmove"],
        "interactionAxis": "x",
        "backgroundColor": "#ffffff",
        "titleColor": "#1f2328",
        "bodyColor": "#1f2328",
        "borderColor": "#d0d7de",
        "borderWidth": 1,
        "activeTransitionDuration": 0,
    }


def test_portfolio_chart_tooltip_is_contained_after_tap_at_mobile_width(page):
    viewport = page.viewport_size
    try:
        page.set_viewport_size({"width": 390, "height": 844})
        canvas = page.locator("#lbChart")
        canvas.scroll_into_view_if_needed()
        page.wait_for_timeout(100)
        point = page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                const point = chart.data.datasets[0].data[0];
                return {
                    x: chart.scales.x.getPixelForValue(point.x),
                    y: chart.scales.y.getPixelForValue(point.y),
                    width: chart.width,
                    height: chart.height,
                };
            }"""
        )
        box = canvas.bounding_box()
        assert box is not None
        page.touchscreen.tap(
            box["x"] + point["x"] * box["width"] / point["width"],
            box["y"] + point["y"] * box["height"] / point["height"],
        )
        page.wait_for_function("() => Chart.getChart('lbChart').tooltip.opacity > 0")
        tooltip = page.evaluate(
            """() => {
                const chart = Chart.getChart('lbChart');
                const tooltip = chart.tooltip;
                return {
                    activeElements: tooltip.getActiveElements().length,
                    left: tooltip.x,
                    top: tooltip.y,
                    right: tooltip.x + tooltip.width,
                    bottom: tooltip.y + tooltip.height,
                    chartWidth: chart.width,
                    chartHeight: chart.height,
                };
            }"""
        )
    finally:
        page.set_viewport_size(viewport)

    assert tooltip["activeElements"] > 0
    assert 0 <= tooltip["left"] <= tooltip["right"] <= tooltip["chartWidth"], tooltip
    assert 0 <= tooltip["top"] <= tooltip["bottom"] <= tooltip["chartHeight"], tooltip


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
    """Clicking a watchlist stock opens the stock detail drawer with a price chart."""
    try:
        page.click("#nav-markets")
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
    finally:
        page.click("#nav-lb")


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
        page.click("#nav-markets")
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
        page.evaluate("() => closeDrawer()")
        page.click("#nav-lb")
        page.evaluate(
            "() => { if (window.originalOpenDrawerTicker) window.openDrawerTicker = window.originalOpenDrawerTicker; }"
        )


def test_websocket_refreshes_only_affected_views(page):
    refreshes = page.evaluate(
        """() => {
            const original = {
                applyLeaderboardUpdate,
                refreshLeaderboard,
                loadActivity,
                renderDecisionBatchStatus,
            };
            const calls = { liveLeaderboard: 0, leaderboardRefresh: 0, activity: 0, decisionBatch: 0 };
            window.applyLeaderboardUpdate = () => calls.liveLeaderboard++;
            window.refreshLeaderboard = () => calls.leaderboardRefresh++;
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
                { type: 'LEADERBOARD_UPDATE', data: [] },
                { type: 'PORTFOLIO_RESET' },
                { type: 'GATEKEEPER_ALERT', status: 'EXECUTED' },
            ]) handleWebSocketMessage(message);

            Object.assign(window, original);
            return calls;
        }"""
    )

    assert refreshes == {"liveLeaderboard": 1, "leaderboardRefresh": 0, "activity": 3, "decisionBatch": 1}


def test_agent_drawer_lists_decision_history_with_load_more(page):
    _first_username(page)
    _open_and_assert_drawer(page, "running")
    page.wait_for_selector("#decision-history .decision-item", timeout=15000)
    assert len(page.query_selector_all("#decision-history .decision-item")) == 10

    first = page.query_selector("#decision-history .decision-item").text_content()
    assert "BUY" in first
    assert "AAPL" in first
    assert "Executed" in first
    assert "fixture-model" in first
    assert "No trade" in page.query_selector_all("#decision-history .decision-item")[1].text_content()

    page.click("#decision-history .decision-item .decision-reason summary")
    assert "Fixture reasoning 0." in page.text_content("#decision-history")

    page.click(".load-more-btn")
    page.wait_for_function(
        "() => document.querySelectorAll('#decision-history .decision-item').length === 13",
        timeout=8000,
    )
    assert page.query_selector(".load-more-btn") is None
    assert page._collected_errors == [], f"JS errors: {page._collected_errors}"
    page.click("#drawer .close")
