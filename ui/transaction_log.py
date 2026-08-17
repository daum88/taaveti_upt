"""
Transaction History Viewer — scrollable log of all past executions.
"""

from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from application.portfolio_queries import PortfolioNotFound, PortfolioQueries

console = Console()


def show_transaction_history(portfolios: PortfolioQueries) -> None:
    """Display paginated transaction history for browsing."""
    all_transactions = portfolios.recent_transactions(limit=200)
    if not all_transactions:
        console.print("[dim]No transactions recorded yet.[/dim]")
        Prompt.ask("[dim]Press Enter to return[/dim]")
        return

    page_size = 20
    page = 0
    total_pages = (len(all_transactions) + page_size - 1) // page_size

    while True:
        start = page * page_size
        end = min(start + page_size, len(all_transactions))
        page_transactions = all_transactions[start:end]

        table = Table(
            title=f"📜 TRANSACTION HISTORY (page {page + 1}/{total_pages})",
            box=box.SIMPLE,
            expand=True,
            title_style="bold blue",
            header_style="bold cyan",
        )

        table.add_column("#", justify="right", style="dim", width=5)
        table.add_column("Date/Time", style="dim", width=20)
        table.add_column("Trader", style="bold", width=12)
        table.add_column("Act", width=5)
        table.add_column("Ticker", style="bold yellow", width=8)
        table.add_column("Qty", justify="right", width=10)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Total", justify="right", width=12)
        table.add_column("Reasoning", style="italic", width=40)

        for index, transaction in enumerate(page_transactions):
            display_index = start + index + 1
            action_color = {"BUY": "green", "DIVIDEND": "blue", "DIVIDEND_REVERSAL": "yellow"}.get(
                transaction["transaction_type"], "red"
            )
            reasoning = (transaction.get("llm_reasoning") or "")[:60]
            market_note = " [dim](closed)[/dim]" if transaction.get("market_closed") else ""

            table.add_row(
                str(display_index),
                transaction["executed_at"] or "—",
                (transaction.get("username") or f"ID:{transaction['user_id']}").title(),
                f"[{action_color}]{transaction['transaction_type']}[/{action_color}]",
                transaction["ticker"],
                f"{transaction['quantity']:.4f}",
                f"${transaction['price_per_share']:.2f}",
                f"${transaction['total_value']:,.2f}",
                reasoning + market_note,
            )

        console.print(table)
        console.print(f"\n[dim]Showing {start + 1}-{end} of {len(all_transactions)} transactions[/dim]")
        command = Prompt.ask(
            "[bold]Navigate:[/bold] [yellow]n[/yellow]=next, [yellow]p[/yellow]=prev, [yellow]q[/yellow]=quit",
            choices=["n", "p", "q"],
            default="q",
        )

        if command == "n" and page < total_pages - 1:
            page += 1
        elif command == "p" and page > 0:
            page -= 1
        elif command == "q":
            break


def show_user_transaction_history(portfolios: PortfolioQueries, username: str = "taavet") -> None:
    """Show transaction history for a specific user."""
    try:
        transactions = portfolios.user_trades(username, limit=50)
    except PortfolioNotFound:
        console.print(f"[red]User '{username}' not found.[/red]")
        return

    table = Table(
        title=f"📜 Transactions — {username.title()}",
        box=box.SIMPLE,
        title_style="bold blue",
        header_style="bold cyan",
    )

    table.add_column("Date/Time", style="dim", width=20)
    table.add_column("Act", width=5)
    table.add_column("Ticker", style="bold yellow", width=8)
    table.add_column("Qty", justify="right", width=10)
    table.add_column("Price", justify="right", width=10)
    table.add_column("Total", justify="right", width=12)
    table.add_column("Balance", justify="right", width=12)
    table.add_column("Reasoning", style="italic", width=36)

    for transaction in transactions:
        action_color = {"BUY": "green", "DIVIDEND": "blue", "DIVIDEND_REVERSAL": "yellow"}.get(
            transaction.transaction_type, "red"
        )
        reasoning = (transaction.llm_reasoning or "")[:55]
        market_note = " [dim](closed)[/dim]" if transaction.market_closed else ""

        table.add_row(
            transaction.executed_at or "—",
            f"[{action_color}]{transaction.transaction_type}[/{action_color}]",
            transaction.ticker,
            f"{transaction.quantity:.4f}",
            f"${transaction.price_per_share:.2f}",
            f"${transaction.total_value:,.2f}",
            f"${transaction.cash_balance_after:,.2f}" if transaction.cash_balance_after else "—",
            reasoning + market_note,
        )

    console.print(table)
    Prompt.ask("[dim]Press Enter to return[/dim]")
