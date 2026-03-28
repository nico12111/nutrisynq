"""
CLI Entry Point for the Polymarket Copy-Trading Bot.

Usage:
    copy-trader run [--config config.json] [--dry-run]
    copy-trader status
    copy-trader check-wallets
"""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """Polymarket Copy-Trading Bot - Monitor wallets and copy trades."""
    pass


@cli.command()
@click.option("--config", default="config.json", help="Path to config.json")
@click.option("--dry-run", is_flag=True, default=False, help="Force dry-run mode (overrides .env)")
def run(config: str, dry_run: bool) -> None:
    """Start the copy-trading bot."""
    from src.bot import run_bot
    from src.config.settings import load_config

    app_config = load_config(config)
    if dry_run:
        app_config.env.dry_run = True

    console.print("[bold]Polymarket Copy-Trading Bot[/bold]")
    console.print(f"Config: {config}")
    console.print(f"Dry Run: {app_config.is_dry_run}\n")

    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        console.print("\n[yellow]Bot stopped by user.[/yellow]")
        sys.exit(0)


@cli.command(name="check-wallets")
@click.option("--config", default="config.json", help="Path to config.json")
def check_wallets(config: str) -> None:
    """Verify wallet configurations and check on-chain activity."""
    from src.config.settings import load_config

    app_config = load_config(config)

    console.print("[bold]Wallet Configuration Check[/bold]\n")

    for w in app_config.wallets:
        status = "[green]ACTIVE[/green]" if w.enabled else "[red]DISABLED[/red]"
        console.print(f"  {status} {w.label or 'unnamed'}: {w.address}")
        console.print(f"    Buys: {'Yes' if w.copy_buys else 'No'} | Sells: {'Yes' if w.copy_sells else 'No'}")
        console.print(f"    Max Risk: ${w.max_risk_usd:,.0f}")
        console.print()

    console.print(f"Total active wallets: {len(app_config.active_wallets)}")


@cli.command()
@click.option("--config", default="config.json", help="Path to config.json")
def status(config: str) -> None:
    """Show current bot configuration and risk parameters."""
    from rich.table import Table

    from src.config.settings import load_config

    app_config = load_config(config)

    table = Table(title="Bot Configuration")
    table.add_column("Parameter", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Mode", "DRY RUN" if app_config.is_dry_run else "LIVE")
    table.add_row("Trade Mode", app_config.trading.mode)
    table.add_row("Fixed Amount", f"${app_config.trading.fixed_amount_usd}")
    table.add_row("Percent of Balance", f"{app_config.trading.percent_of_balance}%")
    table.add_row("Max Slippage", f"{app_config.trading.max_slippage_percent}%")
    table.add_row("Max Parallel Positions", str(app_config.trading.max_parallel_positions))
    table.add_row("Max Exposure/Market", f"${app_config.risk.max_exposure_per_market_usd}")
    table.add_row("Max Exposure/Wallet", f"${app_config.risk.max_exposure_per_wallet_usd}")
    table.add_row("Max Total Exposure", f"${app_config.risk.max_total_exposure_usd}")
    table.add_row("Max Daily Loss", f"${app_config.risk.max_daily_loss_usd}")
    table.add_row("Poll Interval", f"{app_config.monitoring.poll_interval_seconds}s")
    table.add_row("Block Confirmations", str(app_config.monitoring.block_confirmations))

    console.print(table)


if __name__ == "__main__":
    cli()
