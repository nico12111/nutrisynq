#!/usr/bin/env python3
"""Debug script to check Polymarket account configuration."""

import os
import sys

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
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")

    print("=" * 60)
    print("Polymarket Account Debug")
    print("=" * 60)

    client_basic = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
    )
    derived_address = client_basic.get_address()
    print(f"\nSigning address (from private key): {derived_address}")
    print(f"Funder address (from .env):         {funder}")
    print(f"API Key:                            {api_key[:16]}...")
    print()

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )

    # Test all combinations and print RAW responses
    print("=" * 60)
    print("Testing all signature types WITH funder")
    print("=" * 60)
    for st in [0, 1, 2]:
        label = {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE"}[st]
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
            print(f"\n  Type {st} ({label}): RAW = {result}")
            balance = float(result.get("balance", 0)) / 1e6
            allowance = float(result.get("allowance", 0)) / 1e6
            print(f"    Balance: ${balance:.6f}  Allowance: ${allowance:.6f}")
            if balance > 0:
                print(f"    >>> FOUND BALANCE! Use SIGNATURE_TYPE={st}")
        except Exception as e:
            print(f"\n  Type {st} ({label}): ERROR = {e}")

    print()
    print("=" * 60)
    print("Testing all signature types WITHOUT funder")
    print("=" * 60)
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
            print(f"\n  Type {st} ({label}): RAW = {result}")
            balance = float(result.get("balance", 0)) / 1e6
            if balance > 0:
                print(f"    >>> FOUND BALANCE! Use SIGNATURE_TYPE={st}, no funder needed")
        except Exception as e:
            print(f"\n  Type {st} ({label}): ERROR = {e}")

    # Check on-chain USDC balance of funder address
    print()
    print("=" * 60)
    print("Checking on-chain USDC balance")
    print("=" * 60)
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))

        # USDC.e on Polygon
        usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # Minimal ERC20 ABI for balanceOf
        erc20_abi = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]

        usdc = w3.eth.contract(address=Web3.to_checksum_address(usdc_address), abi=erc20_abi)

        # Check both addresses
        for label, addr in [("Signing key", derived_address), ("Funder", funder)]:
            if addr:
                bal = usdc.functions.balanceOf(Web3.to_checksum_address(addr)).call()
                print(f"  {label} ({addr[:10]}...): {bal / 1e6:.6f} USDC.e")

        # Also check USDC (not bridged) - 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
        usdc2_address = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
        usdc2 = w3.eth.contract(address=Web3.to_checksum_address(usdc2_address), abi=erc20_abi)
        for label, addr in [("Signing key", derived_address), ("Funder", funder)]:
            if addr:
                bal = usdc2.functions.balanceOf(Web3.to_checksum_address(addr)).call()
                print(f"  {label} ({addr[:10]}...): {bal / 1e6:.6f} USDC (native)")

    except Exception as e:
        print(f"  On-chain check failed: {e}")

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Your Polymarket shows $18.86 but API returns $0.")
    print(f"  If on-chain balance is also $0 for both addresses,")
    print(f"  the funds might be in a conditional token position")
    print(f"  (already used for a trade), not as free USDC.")


if __name__ == "__main__":
    main()
