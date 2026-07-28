"""Browser (Playwright) tests for the web UI.

Boots the real FastAPI server on a spare port against the existing DB and
drives the page with a headless Chromium browser to verify that the
leaderboard renders and that clicking a player (including the index fund,
which regressed to a stuck "Loading..." drawer) populates the detail drawer.

Run:  pytest tests/test_web_ui.py
Skips automatically if playwright/browser are unavailable.
"""

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
    pg.goto(server, wait_until="networkidle")
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


def test_no_page_errors_on_load(page):
    assert page._collected_errors == [], f"JS errors on load: {page._collected_errors}"


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
