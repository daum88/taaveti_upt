"""
Manual Trade Executor — CLI interface for Taavet (human user)
to execute BUY/SELL orders through the same execution engine.
"""

import logging
from decimal import Decimal
from uuid import uuid4

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from adapters.market_data.yfinance_quotes import fetch_current_prices
from application.trading import Trading, TradingError
from domain.trading import ConfirmOrder
from models.account import Account
from models.holding import Holding
from models.user import User
from services.leaderboard import compute_portfolio_snapshot
from settings import Settings

logger = logging.getLogger(__name__)
console = Console()


def _show_taavet_status(settings: Settings):
    """Display Taavet's current portfolio status."""
    taavet = User.get_by_username("taavet")
    if not taavet:
        console.print("[red]Taavet user not found![/red]")
        return None

    snap = compute_portfolio_snapshot(taavet.id, settings=settings)
    if not snap:
        return None

    console.print()
    console.print(
        Panel.fit(
            f"[bold]Cash:[/bold] ${snap['cash_balance']:,.2f}  |  "
            f"[bold]Holdings:[/bold] ${snap['holdings_value']:,.2f}  |  "
            f"[bold]Total:[/bold] [green]${snap['total_value']:,.2f}[/green]  |  "
            f"[bold]P&L:[/bold] [{'green' if snap['pnl_total'] >= 0 else 'red'}]${snap['pnl_total']:+,.2f}[/{'green' if snap['pnl_total'] >= 0 else 'red'}]",
            title="👤 Taavet — Portfolio Status",
            border_style="cyan",
        )
    )

    # Show holdings
    if snap["holdings"]:
        table = Table(title="Current Holdings", box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Ticker", style="bold yellow")
        table.add_column("Shares")
        table.add_column("Avg Cost")
        table.add_column("Current Price")
        table.add_column("Market Value", justify="right")
        table.add_column("P&L", justify="right")

        for h in snap["holdings"]:
            pnl_color = "green" if h["pnl"] >= 0 else "red"
            table.add_row(
                h["ticker"],
                f"{h['quantity']:.4f}",
                f"${h['average_cost']:.2f}",
                f"${h['current_price']:.2f}",
                f"${h['market_value']:,.2f}",
                f"[{pnl_color}]${h['pnl']:+,.2f}[/{pnl_color}]",
            )
        console.print(table)
    else:
        console.print("[dim]No current holdings.[/dim]")

    console.print()
    return taavet


def run_manual_trade(settings: Settings) -> None:
    """Interactive CLI loop for Taavet to place manual trades."""
    trading = Trading(settings=settings)
    taavet = _show_taavet_status(settings)
    if not taavet:
        return

    # Choose action
    console.print("[bold]Trade Actions:[/bold]")
    console.print("  [green]BUY[/green] — Purchase shares")
    console.print("  [red]SELL[/red] — Sell shares from holdings")
    console.print("  [dim]cancel[/dim] — Return to dashboard")
    console.print()

    action = Prompt.ask("Action", choices=["BUY", "SELL", "cancel"], default="cancel")
    if action == "cancel":
        return

    ticker = Prompt.ask("Ticker symbol").strip().upper()
    if not ticker:
        console.print("[red]Ticker required.[/red]")
        return

    # Fetch current price
    with console.status(f"[bold yellow]Fetching price for {ticker}...[/bold yellow]"):
        prices = fetch_current_prices([ticker], settings=settings)

    if ticker not in prices or not prices[ticker].get("price"):
        console.print(f"[red]Could not fetch price for {ticker}. Check ticker symbol.[/red]")
        return

    current_price = prices[ticker]["price"]
    prev_close = prices[ticker].get("previous_close")
    change_pct = prices[ticker].get("change_percent", 0)

    console.print(
        f"\n[bold]{ticker}[/bold] — Current: [green]${current_price:.2f}[/green] | Prev Close: ${prev_close:.2f} | Change: [{'green' if change_pct >= 0 else 'red'}]{change_pct:+.2f}%[/{'green' if change_pct >= 0 else 'red'}]"
    )

    account = Account.get_by_user_id(taavet.id)
    snap = compute_portfolio_snapshot(taavet.id, settings=settings)
    total_value = snap["total_value"] if snap else account.cash_balance

    if action == "BUY":
        # Show buying power
        max_shares = account.cash_balance / current_price if current_price > 0 else 0
        console.print(f"Buying power: [green]${account.cash_balance:,.2f}[/green] (max {max_shares:.2f} shares)")

        # Option 1: specify dollar amount
        use_dollars = Confirm.ask("Specify amount in dollars?", default=True)

        if use_dollars:
            amount_str = Prompt.ask("Amount ($)", default=f"{min(account.cash_balance, 1000):.2f}")
            try:
                amount = float(amount_str)
            except ValueError:
                console.print("[red]Invalid amount.[/red]")
                return

            if amount > account.cash_balance:
                console.print(f"[yellow]Amount exceeds cash. Down-sizing to ${account.cash_balance:,.2f}[/yellow]")
                amount = account.cash_balance

            allocation = amount / total_value if total_value > 0 else 0
        else:
            allocation_str = Prompt.ask("Allocation (% of portfolio)", default="10")
            try:
                allocation = float(allocation_str) / 100.0
            except ValueError:
                console.print("[red]Invalid percentage.[/red]")
                return

        console.print(f"\n[bold]BUY {ticker}[/bold] — Allocating {allocation * 100:.1f}% of portfolio")
        if not Confirm.ask("Confirm trade?", default=True):
            console.print("[dim]Trade cancelled.[/dim]")
            return

        try:
            result = trading.execute(
                ConfirmOrder(taavet.username, ticker, "BUY", Decimal(str(allocation)) * total_value, str(uuid4()))
            )
            order = result.order
            console.print(
                f"[bold green]✓ BUY executed:[/bold green] {order.quantity:.4f} shares of {ticker} @ ${order.price:.2f} = ${order.total:,.2f}"
            )
        except TradingError as error:
            console.print(f"[red]✗ Trade rejected: {error}[/red]")

    elif action == "SELL":
        holding = Holding.get_by_user_and_ticker(taavet.id, ticker)
        if not holding or holding.quantity <= 0:
            console.print(f"[red]You don't own any {ticker} shares to sell.[/red]")
            return

        console.print(
            f"You hold: [bold]{holding.quantity:.4f} shares[/bold] @ avg ${holding.average_cost_per_share:.2f}"
        )
        console.print(f"Current value: [green]${holding.quantity * current_price:,.2f}[/green]")

        use_dollars = Confirm.ask("Specify amount in dollars?", default=False)
        if use_dollars:
            amount_str = Prompt.ask("Amount ($)")
            try:
                amount = float(amount_str)
            except ValueError:
                console.print("[red]Invalid amount.[/red]")
                return
            allocation = amount / total_value if total_value > 0 else 0
        else:
            # Sell all by default
            sell_all = Confirm.ask("Sell all shares?", default=True)
            if sell_all:
                allocation = (holding.quantity * current_price) / total_value if total_value > 0 else 0
            else:
                allocation_str = Prompt.ask("Allocation (% of portfolio)", default="10")
                try:
                    allocation = float(allocation_str) / 100.0
                except ValueError:
                    console.print("[red]Invalid percentage.[/red]")
                    return

        if not Confirm.ask("Confirm sell?", default=True):
            console.print("[dim]Trade cancelled.[/dim]")
            return

        try:
            result = trading.execute(
                ConfirmOrder(taavet.username, ticker, "SELL", Decimal(str(allocation)) * total_value, str(uuid4()))
            )
            order = result.order
            console.print(
                f"[bold red]✓ SELL executed:[/bold red] {order.quantity:.4f} shares of {ticker} @ ${order.price:.2f} = ${order.total:,.2f}"
            )
        except TradingError as error:
            console.print(f"[red]✗ Trade rejected: {error}[/red]")
