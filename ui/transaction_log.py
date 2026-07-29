"""
Transaction History Viewer — scrollable log of all past executions.
"""

from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from models.transaction import Transaction
from models.user import User

console = Console()


def show_transaction_history():
    """Display paginated transaction history for browsing."""
    all_txns = Transaction.recent_with_usernames(limit=200)
    if not all_txns:
        console.print("[dim]No transactions recorded yet.[/dim]")
        Prompt.ask("[dim]Press Enter to return[/dim]")
        return

    page_size = 20
    page = 0
    total_pages = (len(all_txns) + page_size - 1) // page_size

    while True:
        start = page * page_size
        end = min(start + page_size, len(all_txns))
        page_txns = all_txns[start:end]

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

        for i, t in enumerate(page_txns):
            idx = start + i + 1
            action_color = {"BUY": "green", "DIVIDEND": "blue", "DIVIDEND_REVERSAL": "yellow"}.get(t["transaction_type"], "red")
            reasoning = (t.get("llm_reasoning") or "")[:60]
            market_note = " [dim](closed)[/dim]" if t.get("market_closed") else ""

            table.add_row(
                str(idx),
                t["executed_at"] or "—",
                (t.get("username") or f"ID:{t['user_id']}").title(),
                f"[{action_color}]{t['transaction_type']}[/{action_color}]",
                t["ticker"],
                f"{t['quantity']:.4f}",
                f"${t['price_per_share']:.2f}",
                f"${t['total_value']:,.2f}",
                reasoning + market_note,
            )

        console.print(table)
        console.print(f"\n[dim]Showing {start + 1}-{end} of {len(all_txns)} transactions[/dim]")
        cmd = Prompt.ask(
            "[bold]Navigate:[/bold] [yellow]n[/yellow]=next, [yellow]p[/yellow]=prev, [yellow]q[/yellow]=quit",
            choices=["n", "p", "q"],
            default="q",
        )

        if cmd == "n" and page < total_pages - 1:
            page += 1
        elif cmd == "p" and page > 0:
            page -= 1
        elif cmd == "q":
            break


def show_user_transaction_history(username: str = "taavet"):
    """Show transaction history for a specific user."""
    user = User.get_by_username(username)
    if not user:
        console.print(f"[red]User '{username}' not found.[/red]")
        return

    txns = Transaction.recent_for_user(user.id, limit=50)

    table = Table(
        title=f"📜 Transactions — {user.username.title()}",
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

    for t in txns:
        action_color = {"BUY": "green", "DIVIDEND": "blue", "DIVIDEND_REVERSAL": "yellow"}.get(t.transaction_type, "red")
        reasoning = (t.llm_reasoning or "")[:55]
        market_note = " [dim](closed)[/dim]" if t.market_closed else ""

        table.add_row(
            t.executed_at or "—",
            f"[{action_color}]{t.transaction_type}[/{action_color}]",
            t.ticker,
            f"{t.quantity:.4f}",
            f"${t.price_per_share:.2f}",
            f"${t.total_value:,.2f}",
            f"${t.cash_balance_after:,.2f}" if t.cash_balance_after else "—",
            reasoning + market_note,
        )

    console.print(table)
    Prompt.ask("[dim]Press Enter to return[/dim]")
