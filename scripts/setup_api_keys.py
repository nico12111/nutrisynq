#!/usr/bin/env python3
"""
Polymarket API Key Setup Script.

Derives CLOB API credentials (api_key, api_secret, api_passphrase) from your
Polygon wallet private key using the official py-clob-client SDK.

How it works:
1. You provide your Polygon wallet private key
2. The script uses ClobClient.derive_api_key() which:
   - Signs a message with your private key (EIP-712)
   - Sends the signature to Polymarket's auth endpoint
   - Polymarket verifies ownership and returns API credentials
3. The credentials are written to your .env file

These credentials are bound to your wallet address and allow:
- Reading order books and market data
- Placing and cancelling orders
- Querying your balances and positions

Prerequisites:
- pip install py-clob-client
- A Polygon wallet with some USDC.e for trading

Usage:
    python scripts/setup_api_keys.py
    python scripts/setup_api_keys.py --env-file .env
    python scripts/setup_api_keys.py --private-key 0x...
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def derive_credentials(private_key: str, host: str, chain_id: int) -> dict:
    """
    Derive Polymarket CLOB API credentials from a private key.

    Uses the official py-clob-client SDK:
    https://github.com/Polymarket/py-clob-client

    The derive_api_key() method:
    1. Creates an EIP-712 typed signature using your private key
    2. POSTs to {host}/auth/derive-api-key with the signature
    3. Returns { apiKey, secret, passphrase }

    These credentials authenticate all subsequent CLOB API calls.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("ERROR: py-clob-client is not installed.")
        print("Install it with: pip install py-clob-client")
        sys.exit(1)

    # Initialize client without credentials (we're deriving them)
    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=private_key,
    )

    print(f"Deriving API key for wallet: {client.get_address()}")
    print(f"CLOB host: {host}")
    print(f"Chain ID: {chain_id}")
    print()

    # derive_api_key() signs a message and calls the auth endpoint
    creds = client.derive_api_key()

    if not creds or "apiKey" not in creds:
        print("ERROR: Failed to derive API credentials.")
        print(f"Response: {creds}")
        sys.exit(1)

    return {
        "api_key": creds["apiKey"],
        "api_secret": creds["secret"],
        "api_passphrase": creds["passphrase"],
    }


def update_env_file(env_path: Path, credentials: dict) -> None:
    """Update or create the .env file with API credentials."""
    env_vars = {
        "POLYMARKET_API_KEY": credentials["api_key"],
        "POLYMARKET_API_SECRET": credentials["api_secret"],
        "POLYMARKET_API_PASSPHRASE": credentials["api_passphrase"],
    }

    if env_path.exists():
        content = env_path.read_text()
    else:
        # Copy from .env.example if it exists
        example_path = env_path.parent / ".env.example"
        if example_path.exists():
            content = example_path.read_text()
        else:
            content = ""

    # Update each variable in the .env content
    for key, value in env_vars.items():
        pattern = rf"^{key}=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        else:
            content += f"\n{key}={value}\n"

    env_path.write_text(content)
    print(f"Credentials written to {env_path}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Derive Polymarket CLOB API credentials")
    parser.add_argument(
        "--private-key",
        help="Polygon wallet private key (hex). If not provided, reads from PRIVATE_KEY env var or .env file.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--host",
        default="https://clob.polymarket.com",
        help="Polymarket CLOB API host",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=137,
        help="Chain ID (default: 137 for Polygon)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing to .env",
    )

    args = parser.parse_args()
    env_path = Path(args.env_file)

    # Resolve private key
    private_key = args.private_key

    if not private_key:
        # Try loading from existing .env
        if env_path.exists():
            from dotenv import load_dotenv
            load_dotenv(env_path)
        private_key = os.environ.get("PRIVATE_KEY", "")

    if not private_key:
        print("ERROR: No private key provided.")
        print()
        print("Provide it via one of:")
        print("  1. --private-key 0xYOUR_KEY")
        print("  2. PRIVATE_KEY in your .env file")
        print("  3. PRIVATE_KEY environment variable")
        sys.exit(1)

    # Ensure 0x prefix
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print("=" * 60)
    print("Polymarket API Key Derivation")
    print("=" * 60)
    print()
    print("This will sign a message with your private key and")
    print("register it with Polymarket's authentication service.")
    print()

    # Derive credentials
    credentials = derive_credentials(private_key, args.host, args.chain_id)

    print()
    print("Credentials derived successfully!")
    print(f"  API Key:      {credentials['api_key'][:12]}...")
    print(f"  API Secret:   {credentials['api_secret'][:12]}...")
    print(f"  Passphrase:   {credentials['api_passphrase'][:12]}...")
    print()

    if args.dry_run:
        print("[DRY RUN] Would write to:", env_path)
        print()
        print("Add these to your .env manually:")
        print(f"  POLYMARKET_API_KEY={credentials['api_key']}")
        print(f"  POLYMARKET_API_SECRET={credentials['api_secret']}")
        print(f"  POLYMARKET_API_PASSPHRASE={credentials['api_passphrase']}")
    else:
        update_env_file(env_path, credentials)
        print()
        print("Done! Your bot can now trade on your Polymarket account.")
        print("Start with dry-run mode first: python -m src.main run --dry-run")


if __name__ == "__main__":
    main()
