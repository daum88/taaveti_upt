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

from application.portfolio_queries import PortfolioNotFound, PortfolioQueries
from application.trading import Trading, TradingError
from domain.trading import ConfirmOrder, PreviewOrder
from settings import Settings

logger = logging.getLogger(__name__)
console = Console()


def _show_taavet_status(portfolios: PortfolioQueries) -> dict[str, object] | None:
    """Display Taavet's current portfolio status."""
    try:
        snapshot = portfolios.portfolio("taavet")
    except PortfolioNotFound:
        console.print("[red]Taavet user not found![/red]")
        return None

    console.print()
    console.print(
        Panel.fit(
            f"[bold]Cash:[/bold] ${snapshot['cash_balance']:,.2f}  |  "
            f"[bold]Holdings:[/bold] ${snapshot['holdings_value']:,.2f}  |  "
            f"[bold]Total:[/bold] [green]${snapshot['total_value']:,.2f}[/green]  |  "
            f"[bold]P&L:[/bold] [{'green' if snapshot['pnl_total'] >= 0 else 'red'}]${snapshot['pnl_total']:+,.2f}[/{'green' if snapshot['pnl_total'] >= 0 else 'red'}]",
            title="👤 Taavet — Portfolio Status",
            border_style="cyan",
        )
    )

    # Show holdings
    holdings = snapshot["holdings"]
    if holdings:
        table = Table(title="Current Holdings", box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Ticker", style="bold yellow")
        table.add_column("Shares")
        table.add_column("Avg Cost")
        table.add_column("Current Price")
        table.add_column("Market Value", justify="right")
        table.add_column("P&L", justify="right")

        for holding in holdings:
            pnl_color = "green" if holding["pnl"] >= 0 else "red"
            table.add_row(
                holding["ticker"],
                f"{holding['quantity']:.4f}",
                f"${holding['average_cost']:.2f}",
                f"${holding['current_price']:.2f}",
                f"${holding['market_value']:,.2f}",
                f"[{pnl_color}]${holding['pnl']:+,.2f}[/{pnl_color}]",
            )
        console.print(table)
    else:
        console.print("[dim]No current holdings.[/dim]")

    console.print()
    return snapshot


def _request_amount(action: str, ticker: str, snapshot: dict[str, object]) -> Decimal | None:
    """Collect a dollar amount without bypassing Trading's quote and validation path."""
    total_value = Decimal(str(snapshot["total_value"]))

    if action == "BUY":
        cash_balance = Decimal(str(snapshot["cash_balance"]))
        console.print(f"Buying power: [green]${cash_balance:,.2f}[/green]")
        use_dollars = Confirm.ask("Specify amount in dollars?", default=True)
        if use_dollars:
            amount_str = Prompt.ask("Amount ($)", default=f"{min(cash_balance, Decimal(1000)):.2f}")
            try:
                return Decimal(amount_str)
            except ArithmeticError:
                console.print("[red]Invalid amount.[/red]")
                return None

        allocation_str = Prompt.ask("Allocation (% of portfolio)", default="10")
        try:
            return Decimal(allocation_str) / 100 * total_value
        except ArithmeticError:
            console.print("[red]Invalid percentage.[/red]")
            return None

    holding = next((item for item in snapshot["holdings"] if item["ticker"] == ticker), None)
    if holding is None or holding["quantity"] <= 0:
        console.print(f"[red]You don't own any {ticker} shares to sell.[/red]")
        return None

    console.print(f"You hold: [bold]{holding['quantity']:.4f} shares[/bold] @ avg ${holding['average_cost']:.2f}")
    console.print(f"Current value: [green]${holding['market_value']:,.2f}[/green]")
    use_dollars = Confirm.ask("Specify amount in dollars?", default=False)
    if use_dollars:
        amount_str = Prompt.ask("Amount ($)")
        try:
            return Decimal(amount_str)
        except ArithmeticError:
            console.print("[red]Invalid amount.[/red]")
            return None

    # Sell all by default
    if Confirm.ask("Sell all shares?", default=True):
        return Decimal(str(holding["market_value"]))

    allocation_str = Prompt.ask("Allocation (% of portfolio)", default="10")
    try:
        return Decimal(allocation_str) / 100 * total_value
    except ArithmeticError:
        console.print("[red]Invalid percentage.[/red]")
        return None


def _show_preview(action: str, preview) -> None:
    """Show the non-binding Trading preview before asking for confirmation."""
    console.print(
        f"\n[bold]{action} {preview.instrument.ticker}[/bold] — "
        f"Current: [green]${preview.quote.price:.2f}[/green] | "
        f"Estimated: {preview.estimated_quantity:.4f} shares for ${preview.estimated_executable_amount:,.2f} | "
        f"Fee: ${preview.fee:.2f}"
    )
    for warning in preview.warnings:
        console.print(f"[yellow]⚠ {warning.message}[/yellow]")


def run_manual_trade(settings: Settings, portfolios: PortfolioQueries) -> None:
    """Interactive CLI loop for Taavet to place manual trades."""
    trading = Trading(settings=settings)
    snapshot = _show_taavet_status(portfolios)
    if snapshot is None:
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

    amount = _request_amount(action, ticker, snapshot)
    if amount is None:
        return

    try:
        preview = trading.preview(PreviewOrder("taavet", ticker, action, amount))
    except TradingError as error:
        console.print(f"[red]✗ Trade rejected: {error}[/red]")
        return

    _show_preview(action, preview)
    if not Confirm.ask("Confirm trade?", default=True):
        console.print("[dim]Trade cancelled.[/dim]")
        return

    try:
        result = trading.execute(ConfirmOrder("taavet", ticker, action, amount, str(uuid4())))
        order = result.order
        color = "green" if action == "BUY" else "red"
        console.print(
            f"[bold {color}]✓ {action} executed:[/bold {color}] "
            f"{order.quantity:.4f} shares of {ticker} @ ${order.price:.2f} = ${order.total:,.2f}"
        )
    except TradingError as error:
        console.print(f"[red]✗ Trade rejected: {error}[/red]")
