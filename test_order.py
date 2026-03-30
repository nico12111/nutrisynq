"""
Standalone test: place a tiny order directly via py-clob-client.
This bypasses the bot completely to isolate the 'invalid signature' issue.
"""
import json
import os
from dotenv import load_dotenv

load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions

# Load credentials
host = os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com")
key = os.getenv("PRIVATE_KEY", "")
funder = os.getenv("FUNDER_ADDRESS", "")
sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))

api_key = os.getenv("POLYMARKET_API_KEY", "")
api_secret = os.getenv("POLYMARKET_API_SECRET", "")
api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")

print(f"Host: {host}")
print(f"Key: {key[:12]}...")
print(f"Funder: {funder}")
print(f"Signature type: {sig_type}")
print(f"API Key: {api_key[:16]}...")
print()

creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)

# Create client
client = ClobClient(
    host=host,
    chain_id=137,
    key=key,
    creds=creds,
    signature_type=sig_type,
    funder=funder if funder else None,
)

print(f"EOA address: {client.get_address()}")
print(f"get_ok(): {client.get_ok()}")
print()

# Find an active market with a real order book
print("Fetching sampling markets...")
try:
    markets = client.get_sampling_markets()
    print(f"Got {len(markets)} sampling markets")
except Exception as e:
    print(f"get_sampling_markets failed: {e}")
    markets = []

# Try to find any market with a working order book
test_token = None
for m in markets:
    tokens = m.get("tokens", [])
    for t in tokens:
        tid = t.get("token_id", "")
        if not tid:
            continue
        try:
            book = client.get_order_book(tid)
            if book.bids and book.asks:
                best_bid = float(book.bids[0].price)
                best_ask = float(book.asks[0].price)
                if best_bid > 0.02 and best_ask < 0.98:  # Real market with liquidity
                    test_token = tid
                    neg_risk = m.get("neg_risk", False)
                    print(f"\nFound active market:")
                    print(f"  Question: {m.get('question', m.get('condition_id', ''))[:80]}")
                    print(f"  Token: {tid[:40]}...")
                    print(f"  Bid: {best_bid}, Ask: {best_ask}")
                    print(f"  Neg risk: {neg_risk}")
                    break
        except:
            continue
    if test_token:
        break

if not test_token:
    # Fallback: try to get markets from CLOB
    print("\nNo sampling market found, trying get_markets...")
    try:
        result = client.get_markets(next_cursor="")
        for m in result.get("data", []):
            tokens = m.get("tokens", [])
            for t in tokens:
                tid = t.get("token_id", "")
                if not tid:
                    continue
                try:
                    book = client.get_order_book(tid)
                    if book.bids and book.asks:
                        best_bid = float(book.bids[0].price)
                        best_ask = float(book.asks[0].price)
                        if best_bid > 0.02 and best_ask < 0.98:
                            test_token = tid
                            neg_risk = m.get("neg_risk", False)
                            print(f"\nFound active market:")
                            print(f"  Question: {m.get('question', m.get('condition_id', ''))[:80]}")
                            print(f"  Token: {tid[:40]}...")
                            print(f"  Bid: {best_bid}, Ask: {best_ask}")
                            print(f"  Neg risk: {neg_risk}")
                            break
                except:
                    continue
            if test_token:
                break
    except Exception as e:
        print(f"get_markets failed: {e}")

if not test_token:
    print("\nERROR: Could not find any active market with liquidity!")
    print("Please check VPN connection.")
    exit(1)

# Now try to place a tiny order
print(f"\n--- Attempting to place order ---")
for try_neg_risk in [False, True]:
    print(f"\nTrying with neg_risk={try_neg_risk}:")
    try:
        book = client.get_order_book(test_token)
        best_bid = float(book.bids[0].price)

        # Use a very low bid price so the order won't fill
        order_price = round(best_bid * 0.5, 2)
        if order_price < 0.01:
            order_price = 0.01

        order_args = OrderArgs(
            token_id=test_token,
            price=order_price,
            size=1.0,
            side="BUY",
        )
        options = PartialCreateOrderOptions(neg_risk=try_neg_risk)

        print(f"  Price: {order_price}, Size: 1.0, Side: BUY")
        signed_order = client.create_order(order_args, options)
        print(f"  Signed order created OK")

        # Try posting with GTC (not FOK) so it just places a limit order
        response = client.post_order(signed_order, OrderType.GTC)
        print(f"  SUCCESS! Response: {response}")

        # Cancel it immediately if it was placed
        order_id = response.get("orderID", "")
        if order_id:
            try:
                client.cancel(order_id)
                print(f"  Order cancelled: {order_id}")
            except:
                pass
        break

    except Exception as e:
        print(f"  FAILED: {e}")

print("\nDone.")
