"""
CLI Entry Point for the Polymarket Copy-Trading Bot.

Usage:
    copy-trader setup          # Derive API keys from your private key
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


@cli.command()
@click.option("--env-file", default=".env", help="Path to .env file")
@click.option(
    "--private-key",
    default=None,
    help="Private key (hex). If not provided, reads from PRIVATE_KEY in .env",
)
def setup(env_file: str, private_key: str | None) -> None:
    """Derive Polymarket CLOB API credentials from your wallet private key.

    This signs a message with your key and registers it with Polymarket's
    auth service. The returned api_key, api_secret, and api_passphrase are
    written to your .env file so the bot can trade on your account.

    You only need to run this once per wallet.
    """
    import os
    from pathlib import Path

    env_path = Path(env_file)

    # Resolve private key
    pk = private_key
    if not pk:
        if env_path.exists():
            from dotenv import load_dotenv
            load_dotenv(env_path)
        pk = os.environ.get("PRIVATE_KEY", "")

    if not pk:
        console.print("[red]No private key found.[/red]")
        console.print("Provide via --private-key or set PRIVATE_KEY in your .env file.")
        sys.exit(1)

    if not pk.startswith("0x"):
        pk = "0x" + pk

    console.print("[bold]Polymarket API Key Setup[/bold]\n")
    console.print("This will sign a message with your private key and register")
    console.print("it with Polymarket to obtain CLOB API credentials.\n")

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        console.print("[red]py-clob-client not installed.[/red]")
        console.print("Run: pip install py-clob-client")
        sys.exit(1)

    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
        )
        wallet_address = client.get_address()
        console.print(f"Wallet: [cyan]{wallet_address}[/cyan]")
        console.print("Deriving API key...\n")

        creds = client.derive_api_key()

        if not creds or "apiKey" not in creds:
            console.print(f"[red]Failed to derive credentials: {creds}[/red]")
            sys.exit(1)

        api_key = creds["apiKey"]
        api_secret = creds["secret"]
        api_passphrase = creds["passphrase"]

        console.print(f"  API Key:      [green]{api_key[:16]}...[/green]")
        console.print(f"  API Secret:   [green]{api_secret[:16]}...[/green]")
        console.print(f"  Passphrase:   [green]{api_passphrase[:16]}...[/green]\n")

        # Write to .env file
        import re

        if env_path.exists():
            content = env_path.read_text()
        else:
            example = env_path.parent / ".env.example"
            content = example.read_text() if example.exists() else ""

        updates = {
            "POLYMARKET_API_KEY": api_key,
            "POLYMARKET_API_SECRET": api_secret,
            "POLYMARKET_API_PASSPHRASE": api_passphrase,
        }
        for key, value in updates.items():
            pattern = rf"^{key}=.*$"
            replacement = f"{key}={value}"
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            else:
                content += f"\n{key}={value}\n"

        env_path.write_text(content)
        console.print(f"Credentials saved to [bold]{env_path}[/bold]")
        console.print("\nYou can now run the bot:")
        console.print("  python -m src.main run --dry-run")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


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
