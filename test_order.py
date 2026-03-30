"""Standalone test to debug 'invalid signature' error."""
import os
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions

key = os.getenv("PRIVATE_KEY", "")
funder = os.getenv("FUNDER_ADDRESS", "")
sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
creds = ApiCreds(
    api_key=os.getenv("POLYMARKET_API_KEY", ""),
    api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
    api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
)

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=key,
    creds=creds,
    signature_type=sig_type,
    funder=funder or None,
)

print(f"EOA: {client.get_address()}")
print(f"Funder: {funder}")
print(f"Sig type: {sig_type}")
print(f"OK: {client.get_ok()}")
print()

# Get ALL markets and find one with real liquidity
print("Searching for active market...")
result = client.get_markets(next_cursor="")
test_token = None
neg_risk = False

if isinstance(result, dict):
    items = result.get("data", [])
elif isinstance(result, list):
    items = result
else:
    items = []
    print(f"Unexpected type: {type(result)}")

for m in items:
    if not isinstance(m, dict):
        continue
    tokens = m.get("tokens", [])
    for t in tokens:
        tid = t.get("token_id", "")
        if not tid:
            continue
        try:
            book = client.get_order_book(tid)
            if book.bids and book.asks:
                bid = float(book.bids[0].price)
                ask = float(book.asks[0].price)
                if bid > 0.05 and ask < 0.95:
                    test_token = tid
                    neg_risk = m.get("neg_risk", False)
                    print(f"Found: {m.get('question', '?')[:70]}")
                    print(f"  Token: {tid[:40]}")
                    print(f"  Bid: {bid}, Ask: {ask}")
                    print(f"  neg_risk: {neg_risk}")
                    break
        except:
            continue
    if test_token:
        break

if not test_token:
    print("No active market found!")
    exit(1)

# Try placing order with different configs
book = client.get_order_book(test_token)
low_price = round(float(book.bids[0].price) * 0.5, 2)
if low_price < 0.01:
    low_price = 0.01

for try_neg in [neg_risk, not neg_risk]:
    for try_sig in [sig_type]:
        print(f"\n--- neg_risk={try_neg}, sig_type={try_sig} ---")
        try:
            # Recreate client if sig type changes
            if try_sig != sig_type:
                c = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                               key=key, creds=creds, signature_type=try_sig,
                               funder=funder or None)
            else:
                c = client

            args = OrderArgs(token_id=test_token, price=low_price, size=1.0, side="BUY")
            opts = PartialCreateOrderOptions(neg_risk=try_neg)
            signed = c.create_order(args, opts)
            print(f"  Signed OK")
            resp = c.post_order(signed, OrderType.GTC)
            print(f"  SUCCESS: {resp}")
            # Cancel immediately
            oid = resp.get("orderID", "")
            if oid:
                try:
                    c.cancel(oid)
                    print(f"  Cancelled: {oid}")
                except:
                    pass
            break
        except Exception as e:
            print(f"  FAILED: {e}")

# Also try with sig_type=0 (EOA) and no funder
print(f"\n--- sig_type=0 (EOA, no funder) ---")
try:
    c2 = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                     key=key, creds=creds, signature_type=0, funder=None)
    args = OrderArgs(token_id=test_token, price=low_price, size=1.0, side="BUY")
    opts = PartialCreateOrderOptions(neg_risk=neg_risk)
    signed = c2.create_order(args, opts)
    print(f"  Signed OK")
    resp = c2.post_order(signed, OrderType.GTC)
    print(f"  SUCCESS: {resp}")
    oid = resp.get("orderID", "")
    if oid:
        try:
            c2.cancel(oid)
            print(f"  Cancelled: {oid}")
        except:
            pass
except Exception as e:
    print(f"  FAILED: {e}")

print("\nDone.")
