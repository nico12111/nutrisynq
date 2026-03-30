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
        asyncio.run(run_bot(app_config))
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
        console.print("Deriving API credentials...\n")

        # create_or_derive_api_creds() is the official recommended method.
        # It creates new credentials if none exist, or derives existing ones.
        creds = client.create_or_derive_api_creds()

        if not creds or not creds.api_key:
            console.print(f"[red]Failed to derive credentials: {creds}[/red]")
            sys.exit(1)

        api_key = creds.api_key
        api_secret = creds.api_secret
        api_passphrase = creds.api_passphrase

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
        console.print()
        console.print("[bold]Next steps:[/bold]")
        console.print("1. Set FUNDER_ADDRESS in .env (your proxy wallet from polymarket.com/settings)")
        console.print("2. Set SIGNATURE_TYPE in .env (1 = email/Google login, 2 = otherwise)")
        console.print("3. Run: python -m src.main run --dry-run")

    except Exception as e:
        import traceback
        console.print(f"[red]Error: {e}[/red]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
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


@cli.command(name="test-auth")
@click.option("--config", default="config.json", help="Path to config.json")
def test_auth(config: str) -> None:
    """Test Polymarket CLOB API authentication and order signing.

    Verifies that the private key, API credentials, signature_type,
    and funder address are all correctly configured.
    """
    from src.config.settings import load_config

    app_config = load_config(config)
    env = app_config.env

    console.print("[bold]Polymarket Auth Test[/bold]\n")

    # Show config (redacted)
    console.print(f"  API URL:        {env.polymarket_api_url}")
    console.print(f"  API Key:        {env.polymarket_api_key[:16]}..." if env.polymarket_api_key else "  API Key:        [red]MISSING[/red]")
    console.print(f"  API Secret:     {env.polymarket_api_secret[:8]}..." if env.polymarket_api_secret else "  API Secret:     [red]MISSING[/red]")
    console.print(f"  API Passphrase: {env.polymarket_api_passphrase[:8]}..." if env.polymarket_api_passphrase else "  API Passphrase: [red]MISSING[/red]")
    console.print(f"  Private Key:    {env.private_key[:10]}..." if env.private_key else "  Private Key:    [red]MISSING[/red]")
    console.print(f"  Funder:         {env.funder_address}" if env.funder_address else "  Funder:         [red]MISSING[/red]")
    console.print(f"  Signature Type: {env.signature_type}")
    console.print()

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        console.print("[red]py-clob-client not installed.[/red]")
        sys.exit(1)

    # Step 1: Create client and check derived address
    console.print("[bold]Step 1: Check wallet address[/bold]")
    try:
        basic_client = ClobClient(
            host=env.polymarket_api_url,
            chain_id=137,
            key=env.private_key,
        )
        derived_addr = basic_client.get_address()
        console.print(f"  Derived EOA address: [cyan]{derived_addr}[/cyan]")
        if env.funder_address:
            if derived_addr.lower() == env.funder_address.lower():
                console.print("  [yellow]WARNING: Funder == EOA. If you use email login, funder should be your PROXY wallet, not EOA.[/yellow]")
            else:
                console.print(f"  Funder (proxy):      [cyan]{env.funder_address}[/cyan]")
                console.print("  [green]OK - EOA and funder are different (expected for POLY_PROXY)[/green]")
    except Exception as e:
        console.print(f"  [red]Failed: {e}[/red]")
        sys.exit(1)

    # Step 2: Create authenticated client
    console.print("\n[bold]Step 2: Test API authentication[/bold]")
    try:
        creds = ApiCreds(
            api_key=env.polymarket_api_key,
            api_secret=env.polymarket_api_secret,
            api_passphrase=env.polymarket_api_passphrase,
        )
        client = ClobClient(
            host=env.polymarket_api_url,
            chain_id=137,
            key=env.private_key,
            creds=creds,
            signature_type=env.signature_type,
            funder=env.funder_address or None,
        )

        # Test basic connectivity
        ok = client.get_ok()
        console.print(f"  get_ok(): [green]{ok}[/green]")

        # Test authenticated endpoint
        api_keys = client.get_api_keys()
        console.print(f"  get_api_keys(): [green]OK - {len(api_keys) if isinstance(api_keys, list) else 'returned'}[/green]")
    except Exception as e:
        console.print(f"  [red]API auth failed: {e}[/red]")
        console.print("  [yellow]Hint: Re-run 'python -m src.main setup' to regenerate credentials.[/yellow]")
        sys.exit(1)

    # Step 3: Test order signing (dry - don't actually post)
    console.print("\n[bold]Step 3: Test order signing[/bold]")
    try:
        from py_clob_client.clob_types import MarketOrderArgs

        # Find an active market with tokens
        console.print("  Fetching active markets...")
        import requests
        test_token_id = None
        question = ""

        # Try multiple API queries to find a market with tokens
        for query_params in [
            {"limit": "10", "active": "true", "closed": "false"},
            {"limit": "10"},
        ]:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params=query_params,
                timeout=10,
            )
            if resp.status_code == 200:
                for m in resp.json():
                    tokens = m.get("tokens", [])
                    if tokens and tokens[0].get("token_id"):
                        test_token_id = tokens[0]["token_id"]
                        question = m.get("question", "")[:60]
                        break
            if test_token_id:
                break

        if test_token_id:
                console.print(f"  Test market: '{question}'")
                console.print(f"  Test token:  {test_token_id[:30]}...")

                # Try to create (sign) a market order without posting
                order_args = MarketOrderArgs(
                    token_id=test_token_id,
                    amount=0.01,
                    side="BUY",
                )
                signed_order = client.create_market_order(order_args)
                console.print(f"  create_market_order(): [green]OK - order signed successfully[/green]")

                # Now try to actually post (tiny amount, will likely fail due to min size, but not signature)
                console.print("\n[bold]Step 4: Test order posting (tiny $0.01 order)[/bold]")
                try:
                    from py_clob_client.clob_types import OrderType
                    response = client.post_order(signed_order, OrderType.FOK)
                    console.print(f"  post_order(): [green]{response}[/green]")
                except Exception as post_err:
                    err_str = str(post_err)
                    if "invalid signature" in err_str.lower():
                        console.print(f"  [red]SIGNATURE ERROR: {err_str}[/red]")
                        console.print()
                        console.print("[bold yellow]Mögliche Ursachen:[/bold yellow]")
                        console.print("  1. Falscher signature_type - probier SIGNATURE_TYPE=0 in .env")
                        console.print("  2. Falscher funder_address - prüfe auf polymarket.com/settings")
                        console.print("  3. API Credentials passen nicht zum Private Key")
                        console.print("     → Lösung: python -m src.main setup --private-key <dein_key>")
                        console.print("  4. Private Key ist von einem anderen Account")
                    elif "minimum" in err_str.lower() or "size" in err_str.lower():
                        console.print(f"  [green]Order rejected for size (not signature) - SIGNING WORKS![/green]")
                        console.print(f"  Error detail: {err_str}")
                    else:
                        console.print(f"  [yellow]Order rejected: {err_str}[/yellow]")
        else:
            console.print("  [yellow]No active market with tokens found[/yellow]")
    except Exception as e:
        console.print(f"  [red]Order signing failed: {e}[/red]")
        import traceback
        console.print(f"  [dim]{traceback.format_exc()}[/dim]")

    console.print()


if __name__ == "__main__":
    cli()
