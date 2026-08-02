"""
Rich terminal dashboard — polished multi-panel live UI.
Auto-refreshes every N seconds. Keyboard via select.poll().
"""

import select
import sys
import time
from datetime import datetime

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from config import DASHBOARD_REFRESH_SECONDS, STARTING_BALANCE
from models.transaction import Transaction
from models.user import User
from services.leaderboard import compute_portfolio_snapshot, get_leaderboard
from services.scheduler import get_scheduler_status, trigger_manual_cycle

console = Console()


def _type_icon(user_type: str, verbose: bool = False) -> str:
    """Return the display icon (and optional label) for a user type."""
    if user_type == "llm_agent":
        return "🤖 AI Agent" if verbose else "🤖"
    if user_type == "index_fund":
        return "📊 Index Fund" if verbose else "📊"
    return "👤 Manual Trader" if verbose else "👤"


# ── Non-blocking key input ──────────────────────────────


def _get_key(timeout: float = 0.2) -> str | None:
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            r, _, _ = select.select([sys.stdin], [], [], timeout)
            return sys.stdin.read(1) if r else None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return None


# ── Spark + bar helpers ─────────────────────────────────


def _pnl_bar(pnl_pct: float, width: int = 10) -> str:
    if pnl_pct >= 0:
        w = min(int(pnl_pct / 3 * width), width) if pnl_pct <= 30 else width
        return f"[green]{'█' * w}[/green][dim]{'░' * (width - w)}[/dim]"
    else:
        w = min(int(abs(pnl_pct) / 3 * width), width) if abs(pnl_pct) <= 30 else width
        return f"[red]{'█' * w}[/red][dim]{'░' * (width - w)}[/dim]"


def _spark(values: list[float], width: int = 12) -> str:
    if not values or len(values) < 2:
        return "—"
    mn, mx = min(values), max(values)
    rng = mx - mn or 0.001
    chars = "▁▂▃▄▅▆▇█"
    return "".join(chars[min(int((v - mn) / rng * (len(chars) - 1)), len(chars) - 1)] for v in values)


# ── Leaderboard ─────────────────────────────────────────


def build_leaderboard_table(rankings: list[dict]) -> Table:
    table = Table(
        title="🏆  LEADERBOARD",
        box=box.HEAVY_EDGE,
        expand=True,
        title_style="bold yellow",
        header_style="bold white",
        border_style="grey50",
    )
    table.add_column("#", justify="center", style="bold", width=3)
    table.add_column("Trader", style="bold", width=10)
    table.add_column("Portfolio", justify="right", style="bold green", width=12)
    table.add_column("P&L", justify="right", width=14)
    table.add_column("Positions", justify="left", width=34)

    for r in rankings:
        rank_icon = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r["rank"], f" {r['rank']}")
        pnl_style = "bold green" if r["pnl_total"] >= 0 else "bold red"
        type_icon = _type_icon(r["user_type"])
        pnl_str = f"[{pnl_style}]${r['pnl_total']:+,.2f}[/{pnl_style}] [dim]({r['pnl_percent']:+.1f}%)[/dim]"

        pos_parts = []
        for h in r.get("holdings", [])[:3]:
            pc = "green" if h["pnl"] >= 0 else "red"
            pos_parts.append(f"{h['ticker']} [{pc}]{h['pnl_percent']:+.1f}%[/{pc}]")
        pos_str = "  ".join(pos_parts) if pos_parts else "[dim]cash only[/dim]"
        if len(r.get("holdings", [])) > 3:
            pos_str += f"  [dim]+{len(r['holdings']) - 3}[/dim]"

        table.add_row(rank_icon, f"{type_icon} {r['username'].title()}", f"${r['total_value']:,.2f}", pnl_str, pos_str)
    return table


# ── Account cards ───────────────────────────────────────


def build_account_cards(rankings: list[dict]) -> Panel:
    cards = []
    for r in rankings:
        pnl_color = "green" if r["pnl_total"] >= 0 else "red"
        type_icon = "🤖 AI" if r["user_type"] == "llm_agent" else ("📊 Idx" if r["user_type"] == "index_fund" else "👤 You")

        holdings_lines = ""
        for h in r.get("holdings", []):
            pc = "green" if h["pnl"] >= 0 else "red"
            holdings_lines += f"  {h['ticker']} {h['quantity']:.1f}×${h['current_price']:.0f} [{pc}]{h['pnl_percent']:+.1f}%[/{pc}]\n"
        if not holdings_lines:
            holdings_lines = "  [dim]all cash[/dim]\n"

        content = f"[dim]{type_icon}[/dim]\n[bold green]${r['total_value']:,.2f}[/bold green]\nCash ${r['cash_balance']:,.0f}  Invested ${r['holdings_value']:,.0f}\nP&L [{pnl_color}]${r['pnl_total']:+,.2f}[/{pnl_color}]\n\n{holdings_lines}"

        cards.append(
            Panel(
                content.rstrip(),
                title=f"[bold]{r['username'].title()}[/bold]",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    return Panel(
        Columns(cards, equal=True, expand=True),
        title="💼  PORTFOLIOS",
        border_style="cyan",
        box=box.HEAVY_EDGE,
    )


# ── Transaction log ─────────────────────────────────────


def build_transaction_log() -> Panel:
    txns = Transaction.recent_with_usernames(limit=10)
    table = Table(box=box.SIMPLE, expand=True, header_style="bold cyan", show_lines=False)
    table.add_column("Time", style="dim", width=8)
    table.add_column("Trader", style="bold", width=7)
    table.add_column("Act", width=4)
    table.add_column("Ticker", style="bold yellow", width=6)
    table.add_column("Amount", justify="right", width=12)
    table.add_column("Why", style="italic", width=34)

    for t in txns:
        action_color = {"BUY": "green", "DIVIDEND": "blue"}.get(t["transaction_type"], "red")
        action_icon = {"BUY": "🟢", "DIVIDEND": "💰"}.get(t["transaction_type"], "🔴")
        time_str = (t.get("executed_at") or "")[-8:] if t.get("executed_at") else "—"
        reasoning = (t.get("llm_reasoning") or "")[:52]
        if t.get("market_closed"):
            reasoning += " [dim]●closed[/dim]"
        amt = f"${t['total_value']:,.2f} ({t['quantity']:.2f}×${t['price_per_share']:.2f})"

        table.add_row(
            time_str,
            (t.get("username") or "").title()[:7],
            f"[{action_color}]{action_icon} {t['transaction_type'][:3]}.[/{action_color}]",
            t["ticker"],
            amt,
            reasoning,
        )

    if not txns:
        table.add_row("—", "—", "—", "—", "—", "[dim]No trades yet. Press [bold]f[/bold] to run a cycle.[/dim]")

    return Panel(table, title="📜  RECENT ACTIVITY", border_style="blue", box=box.HEAVY_EDGE)


# ── Status bar ──────────────────────────────────────────


def build_status_bar(scheduler_status: dict) -> Panel:
    text = Text()

    # Provider badge
    from config import LLM_PROVIDER

    text.append(f" 🧠 {LLM_PROVIDER.upper()} ", style="bold white on #333333")
    text.append("  ")

    # Scheduler status
    if scheduler_status.get("running"):
        text.append("● LIVE ", style="bold green")
    else:
        text.append("○ IDLE ", style="bold red")

    if scheduler_status.get("in_progress"):
        text.append("⏳ processing… ", style="bold yellow")
    else:
        text.append("✓ ready ", style="dim")

    # Last run
    if scheduler_status.get("last_run"):
        try:
            dt = datetime.fromisoformat(scheduler_status["last_run"])
            text.append(f"│ Last: {dt.strftime('%H:%M:%S')} ", style="dim")
        except Exception:
            pass

    # Stats
    last = scheduler_status.get("last_result") or {}
    if last.get("stocks_processed"):
        text.append(f"│ {last['stocks_processed']} stocks → {last.get('trades_executed', 0)} trades ", style="dim")

    # Next cycle countdown
    if scheduler_status.get("next_run"):
        remaining = scheduler_status["next_run"] - time.time()
        if remaining > 0:
            mins, secs = int(remaining // 60), int(remaining % 60)
            text.append(f"│ Next cycle: {mins}m {secs}s ", style="dim")

    # Clock
    now = datetime.now()
    text.append(f"│ {now.strftime('%H:%M:%S')} ", style="dim")

    return Panel(text, box=box.MINIMAL, padding=(0, 1))


# ── News ticker ─────────────────────────────────────────


def build_news_ticker() -> Panel:
    from db.connection import get_db

    with get_db() as conn:
        rows = conn.execute("SELECT t.ticker AS ticker, n.title AS title, n.publisher AS publisher, MAX(n.published_at) AS published_at  FROM news_items n JOIN news_item_tickers t ON t.news_item_id = n.id  GROUP BY t.ticker ORDER BY published_at DESC LIMIT 6").fetchall()

    if not rows:
        return Panel("[dim]No news headlines yet. Run a funnel cycle.[/dim]", title="📰  MARKET PULSE", border_style="magenta", box=box.HEAVY_EDGE)

    ticker_text = Text()
    for r in rows:
        pub = r["publisher"][:12] if r["publisher"] else "—"
        ticker_text.append("◆ ", style="magenta")
        ticker_text.append(f"[{r['ticker']}] ", style="bold yellow")
        ticker_text.append(f"{r['title'][:72]}", style="white")
        ticker_text.append(f"  [dim]({pub})[/dim]\n")

    return Panel(ticker_text, title="📰  MARKET PULSE", border_style="magenta", box=box.HEAVY_EDGE)


# ── Dashboard assembly ──────────────────────────────────


def make_dashboard() -> Layout:
    rankings = get_leaderboard()
    sched = get_scheduler_status()

    layout = Layout()
    layout.split(
        Layout(name="top", ratio=2),
        Layout(name="mid", ratio=1),
        Layout(name="bottom", ratio=1),
        Layout(name="status", size=1),
    )

    layout["top"].split_row(
        Layout(name="leaderboard", ratio=2),
        Layout(name="news", ratio=1),
    )

    layout["mid"].split_row(
        Layout(name="accounts", ratio=1),
    )

    layout["bottom"].split_row(
        Layout(name="transactions", ratio=1),
    )

    layout["leaderboard"].update(build_leaderboard_table(rankings))
    layout["news"].update(build_news_ticker())
    layout["accounts"].update(build_account_cards(rankings))
    layout["transactions"].update(build_transaction_log())
    layout["status"].update(build_status_bar(sched))

    return layout


# ── Interactive handlers ────────────────────────────────


def _trade():
    console.clear()
    from ui.trade_executor import run_manual_trade

    run_manual_trade()
    Prompt.ask("[dim]↵ Enter to return[/dim]")


def _history():
    console.clear()
    from ui.transaction_log import show_transaction_history

    show_transaction_history()


def _account():
    users = User.all()
    console.clear()
    console.print("[bold]Select a trader:[/bold]")
    for i, u in enumerate(users):
        icon = _type_icon(u.user_type)
        console.print(f"  [{i + 1}] {icon} {u.username.title()}")
    choice = Prompt.ask("Choice", default="1")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(users):
            snap = compute_portfolio_snapshot(users[idx].id)
            _show_account(snap)
    except (ValueError, IndexError):
        pass
    Prompt.ask("[dim]↵ Enter to return[/dim]")


def _show_account(snap: dict):
    console.clear()
    pnl_color = "green" if snap["pnl_total"] >= 0 else "red"
    type_icon = _type_icon(snap["user_type"], verbose=True)

    console.print(
        Panel(
            f"[bold]{snap['username'].title()}[/bold] — {type_icon}\n"
            f"Total: [bold green]${snap['total_value']:,.2f}[/bold green]  │  "
            f"Cash: ${snap['cash_balance']:,.2f}  │  "
            f"Invested: ${snap['holdings_value']:,.2f}\n"
            f"P&L: [{pnl_color}]${snap['pnl_total']:+,.2f} ({snap['pnl_percent']:+.2f}%)[/{pnl_color}]  "
            f"vs starting ${STARTING_BALANCE:,.2f}",
            border_style="cyan",
        )
    )

    if snap["holdings"]:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Ticker", style="bold yellow")
        table.add_column("Shares", justify="right")
        table.add_column("Avg Cost", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("Return", justify="right")

        for h in snap["holdings"]:
            pc = "green" if h["pnl"] >= 0 else "red"
            table.add_row(
                h["ticker"],
                f"{h['quantity']:.4f}",
                f"${h['average_cost']:.2f}",
                f"${h['current_price']:.2f}",
                f"${h['market_value']:,.2f}",
                f"[{pc}]${h['pnl']:+,.2f}[/{pc}]",
                f"[{pc}]{h['pnl_percent']:+.2f}%[/{pc}]",
            )
        console.print(table)
    else:
        console.print("[dim]No active positions.[/dim]")


def _force():
    console.clear()
    console.print("[bold yellow]⚡ Triggering funnel cycle…[/bold yellow]")
    ok = trigger_manual_cycle()
    if ok:
        console.print("[green]✓ Cycle launched! Agents are analyzing the market…[/green]")
    else:
        console.print("[yellow]⚠ Cycle already in progress.[/yellow]")
    Prompt.ask("[dim]↵ Enter to return[/dim]")


# ── Main loop ──────────────────────────────────────────


def run_dashboard():
    # Banner
    console.clear()
    console.print(
        "\n[bold cyan]╔══════════════════════════════════════════════╗[/bold cyan]\n"
        "[bold cyan]║[/bold cyan]   [bold yellow]📈  STOCK PORTFOLIO SIMULATOR  📉[/bold yellow]              [bold cyan]║[/bold cyan]\n"
        "[bold cyan]║[/bold cyan]   [dim]AI-Powered Multi-Agent Paper Trading[/dim]           [bold cyan]║[/bold cyan]\n"
        "[bold cyan]╚══════════════════════════════════════════════╝[/bold cyan]\n"
    )
    time.sleep(0.8)

    console.clear()
    last_render = 0.0

    try:
        layout = make_dashboard()
        console.print(layout)
        console.print()
        console.print("[dim]Press [/dim][bold cyan]r[/bold cyan][dim] refresh  [/dim][bold cyan]t[/bold cyan][dim] trade  [/dim][bold cyan]f[/bold cyan][dim] force cycle  [/dim][bold cyan]a[/bold cyan][dim] accounts  [/dim][bold cyan]h[/bold cyan][dim] history  [/dim][bold cyan]q[/bold cyan][dim] quit[/dim]")
        last_render = time.time()
    except Exception as e:
        console.print(f"[red]Render error: {e}[/red]")

    while True:
        now = time.time()

        if now - last_render >= DASHBOARD_REFRESH_SECONDS:
            try:
                console.clear()
                layout = make_dashboard()
                console.print(layout)
                console.print()
                console.print("[dim]Press [/dim][bold cyan]r[/bold cyan][dim] refresh  [/dim][bold cyan]t[/bold cyan][dim] trade  [/dim][bold cyan]f[/bold cyan][dim] force cycle  [/dim][bold cyan]a[/bold cyan][dim] accounts  [/dim][bold cyan]h[/bold cyan][dim] history  [/dim][bold cyan]q[/bold cyan][dim] quit[/dim]")
                last_render = now
            except Exception:
                pass

        key = _get_key(0.3)
        if not key:
            continue

        key = key.lower()
        if key in ("q", "\x03"):
            console.clear()
            console.print("[bold]Shutting down…[/bold]")
            break
        elif key == "r":
            last_render = 0
        elif key == "t":
            _trade()
            last_render = 0
        elif key == "f":
            _force()
            last_render = 0
        elif key == "a":
            _account()
            last_render = 0
        elif key == "h":
            _history()
            last_render = 0

    console.clear()
