#!/usr/bin/env python3
"""Debug script to check Polymarket account configuration."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    except ImportError:
        print("ERROR: pip install py-clob-client")
        sys.exit(1)

    pk = os.getenv("PRIVATE_KEY", "")
    funder = os.getenv("FUNDER_ADDRESS", "")
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")

    print("=" * 60)
    print("Polymarket Account Debug")
    print("=" * 60)

    # Init without creds first
    client_basic = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
    )
    derived_address = client_basic.get_address()
    print(f"\nDerived wallet from private key: {derived_address}")
    print(f"Funder address from .env:        {funder}")
    print(f"Signature type:                  {sig_type}")
    print(f"API Key set:                     {'Yes' if api_key else 'No'}")
    print()

    if not api_key:
        print("ERROR: No API key. Run: python3 -m src.main setup")
        sys.exit(1)

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )

    # Test all signature types
    for st in [0, 1, 2]:
        label = {0: "EOA", 1: "POLY_PROXY (Magic Link)", 2: "GNOSIS_SAFE"}[st]
        try:
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                creds=creds,
                signature_type=st,
                funder=funder if funder else None,
            )
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=st,
            )
            result = client.get_balance_allowance(params)
            balance = float(result.get("balance", 0)) / 1e6
            allowance = float(result.get("allowance", 0)) / 1e6
            print(f"  Signature Type {st} ({label}):")
            print(f"    Balance:   ${balance:.2f}")
            print(f"    Allowance: ${allowance:.2f}")
            if balance > 0:
                print(f"    >>> FOUND YOUR BALANCE! Use SIGNATURE_TYPE={st}")
        except Exception as e:
            print(f"  Signature Type {st} ({label}): Error - {e}")
        print()

    # Also try without funder
    print("--- Testing WITHOUT funder address ---")
    for st in [0, 1, 2]:
        label = {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE"}[st]
        try:
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                creds=creds,
                signature_type=st,
            )
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=st,
            )
            result = client.get_balance_allowance(params)
            balance = float(result.get("balance", 0)) / 1e6
            allowance = float(result.get("allowance", 0)) / 1e6
            print(f"  Sig Type {st} ({label}): Balance=${balance:.2f}, Allowance=${allowance:.2f}")
            if balance > 0:
                print(f"    >>> FOUND YOUR BALANCE! Use SIGNATURE_TYPE={st} without FUNDER_ADDRESS")
        except Exception as e:
            print(f"  Sig Type {st} ({label}): Error - {e}")

    print()
    print("If all balances are $0, check:")
    print("  1. Did the deposit finish? Check on polymarket.com")
    print("  2. Is the FUNDER_ADDRESS correct? (polymarket.com/settings)")
    print("  3. Try the address shown on your Polymarket profile page")


if __name__ == "__main__":
    main()
