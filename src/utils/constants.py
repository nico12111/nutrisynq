"""
Constants for Polymarket Copy-Trading Bot.

Polymarket uses a CTF (Conditional Token Framework) based system on Polygon.
Key contracts and their roles:
- CTF Exchange: The main exchange contract for Polymarket's CLOB
- Neg Risk CTF Exchange: Exchange for negatively-risked markets
- USDC.e: The collateral token used on Polymarket (bridged USDC on Polygon)
- Conditional Tokens Framework: ERC-1155 tokens representing market positions
"""

# --- Polymarket Core Contracts on Polygon ---

# The CTF Exchange contract - handles order matching and settlement
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Neg Risk CTF Exchange - for markets with negative risk
NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Neg Risk Adapter
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Conditional Tokens Framework (ERC-1155)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# USDC.e on Polygon (collateral token)
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# --- Event Signatures (keccak256 hashes of event signatures) ---

# OrderFilled(bytes32 orderHash, address maker, address taker, uint256 makerAssetId,
#              uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)
ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311571544571c1ba7c6b074880713e79d7965f04fdbf6"

# OrdersMatched(bytes32 takerOrderHash, address takerOrderMaker, uint256 makerAssetId,
#               uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled)
ORDERS_MATCHED_TOPIC = "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c"

# Transfer events for ERC-1155 (CTF tokens)
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

# --- Polymarket CLOB API ---
CLOB_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# --- Chain Config ---
POLYGON_CHAIN_ID = 137

# --- USDC Decimals ---
USDC_DECIMALS = 6

# --- ABI fragments for event decoding ---

ORDER_FILLED_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "orderHash", "type": "bytes32"},
        {"indexed": True, "name": "maker", "type": "address"},
        {"indexed": True, "name": "taker", "type": "address"},
        {"indexed": False, "name": "makerAssetId", "type": "uint256"},
        {"indexed": False, "name": "takerAssetId", "type": "uint256"},
        {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
        {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
        {"indexed": False, "name": "fee", "type": "uint256"},
    ],
    "name": "OrderFilled",
    "type": "event",
}

ORDERS_MATCHED_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "takerOrderHash", "type": "bytes32"},
        {"indexed": True, "name": "takerOrderMaker", "type": "address"},
        {"indexed": False, "name": "makerAssetId", "type": "uint256"},
        {"indexed": False, "name": "takerAssetId", "type": "uint256"},
        {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
        {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
    ],
    "name": "OrdersMatched",
    "type": "event",
}
