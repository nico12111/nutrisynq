"""
Event Parser Module.

Parses raw blockchain events into structured trade signals.

How Polymarket trades work on-chain:
1. Polymarket uses a CLOB (Central Limit Order Book) on Polygon
2. Trades emit OrderFilled events on the CTF Exchange contract
3. Each market has a condition_id, and each outcome has a token_id
4. The token_id (assetId in events) maps to a specific outcome (Yes/No)
5. We can resolve token_id -> market via the Polymarket Gamma API

Detection logic:
- OrderFilled: maker sells makerAssetId, receives takerAssetId
  - If makerAssetId = 0 (USDC): maker is BUYING outcome tokens (taker is selling)
  - If takerAssetId = 0 (USDC): maker is SELLING outcome tokens (taker is buying)
  - The watched wallet can be either maker or taker
- Price calculation: amount_usdc / amount_tokens (normalized by decimals)
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import aiohttp
import certifi
from eth_abi import decode
from web3 import Web3

from src.logging_mod.logger import get_logger
from src.utils.constants import (
    GAMMA_API_BASE,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
    ORDER_FILLED_TOPIC,
    ORDERS_MATCHED_TOPIC,
    USDC_DECIMALS,
)

if TYPE_CHECKING:
    from src.tracker.wallet_tracker import RawTradeEvent

logger = get_logger(__name__)


class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class ParsedTrade:
    """A fully parsed trade signal ready for the decision engine."""

    wallet_address: str
    wallet_label: str
    direction: TradeDirection
    token_id: str  # The CTF token ID (outcome token)
    condition_id: str  # The market condition ID
    market_slug: str  # Human-readable market identifier
    market_question: str  # The market question
    outcome: str  # "Yes" or "No"
    amount_usdc: float  # Amount in USDC
    amount_tokens: float  # Amount of outcome tokens
    price: float  # Approx price per token (0-1 range)
    tx_hash: str
    block_number: int
    timestamp: int
    neg_risk: bool = False  # True if from Neg Risk CTF Exchange

    @property
    def summary(self) -> str:
        dir_str = "BOUGHT" if self.direction == TradeDirection.BUY else "SOLD"
        return (
            f"[{self.wallet_label}] {dir_str} {self.outcome} on '{self.market_question}' "
            f"| ${self.amount_usdc:.2f} @ {self.price:.4f} | tx: {self.tx_hash[:16]}..."
        )


class MarketResolver:
    """
    Resolves CTF token IDs to market information via the Polymarket Gamma API.

    Caches results to avoid repeated API calls.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx)
            )
        return self._session

    async def resolve_token(self, token_id: str) -> dict[str, Any] | None:
        """
        Resolve a CTF token ID to market info.

        Tries two strategies:
        1. GET /markets?clob_token_ids={token_id} (works for CLOB token IDs)
        2. GET /markets?asset_id={token_id} (works for on-chain Neg Risk token IDs)

        Returns market question, slug, condition_id, outcome labels, and clob_token_id.
        """
        if token_id in self._cache:
            return self._cache[token_id]

        session = await self._get_session()
        url = f"{GAMMA_API_BASE}/markets"
        market = None

        try:
            # Strategy 1: try as CLOB token ID
            async with session.get(
                url, params={"clob_token_ids": token_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        market = data[0]

            # Strategy 2: try as on-chain asset ID (for Neg Risk Exchange)
            if market is None:
                async with session.get(
                    url, params={"asset_id": token_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            market = data[0]
                            logger.info(
                                "resolved_via_asset_id",
                                onchain_id=str(token_id)[:20],
                                question=market.get("question", "")[:50],
                                tokens_count=len(market.get("tokens", [])),
                                token_keys=list(market.get("tokens", [{}])[0].keys()) if market.get("tokens") else [],
                            )

            if market is None:
                logger.warning("market_not_found", token_id=str(token_id)[:30])
                return None

            outcome = self._resolve_outcome(market, token_id)
            clob_token_id = self._find_clob_token_id(market, token_id, outcome)
            result = {
                "condition_id": market.get("condition_id", ""),
                "question": market.get("question", "Unknown Market"),
                "slug": market.get("slug", ""),
                "outcome": outcome,
                "tokens": market.get("tokens", []),
                "clob_token_id": clob_token_id,
            }
            self._cache[token_id] = result
            logger.debug(
                "market_resolved",
                token_id=str(token_id)[:20],
                clob_token_id=str(clob_token_id)[:20] if clob_token_id else "none",
                question=result["question"][:50],
                outcome=outcome,
            )
            return result

        except Exception:
            logger.exception("market_resolution_failed", token_id=str(token_id)[:30])
            return None

    @staticmethod
    def _resolve_outcome(market: dict[str, Any], token_id: str) -> str:
        """Determine which outcome (Yes/No) the token_id represents."""
        tid = str(token_id)

        # Method 1: tokens array with token_id field
        tokens = market.get("tokens", [])
        for token in tokens:
            if str(token.get("token_id", "")) == tid:
                return token.get("outcome", "Unknown")

        # Method 2: clobTokenIds is a comma-separated string or JSON array,
        # outcomes is a corresponding comma-separated string or JSON array.
        # Example: clobTokenIds="123,456", outcomes="Yes,No"
        clob_ids_raw = market.get("clobTokenIds", market.get("clob_token_ids", ""))
        outcomes_raw = market.get("outcomes", "")

        if isinstance(clob_ids_raw, str) and isinstance(outcomes_raw, str):
            # Parse as JSON arrays (e.g., '["123","456"]') or comma-separated
            import json
            try:
                clob_ids = json.loads(clob_ids_raw) if clob_ids_raw.startswith("[") else clob_ids_raw.split(",")
                outcomes = json.loads(outcomes_raw) if outcomes_raw.startswith("[") else outcomes_raw.split(",")
            except (json.JSONDecodeError, ValueError):
                clob_ids = []
                outcomes = []

            for i, cid in enumerate(clob_ids):
                if str(cid).strip() == tid and i < len(outcomes):
                    return str(outcomes[i]).strip()
        elif isinstance(clob_ids_raw, list) and isinstance(outcomes_raw, list):
            for i, cid in enumerate(clob_ids_raw):
                if str(cid) == tid and i < len(outcomes_raw):
                    return str(outcomes_raw[i])

        logger.info(
            "outcome_not_matched",
            token_id=tid[:20],
            api_tokens=[str(t.get("token_id", ""))[:20] for t in tokens],
            clob_ids=str(clob_ids_raw)[:60],
            outcomes=str(outcomes_raw)[:60],
        )
        return "Unknown"

    @staticmethod
    def _find_clob_token_id(market: dict[str, Any], onchain_token_id: str, outcome: str) -> str | None:
        """
        Extract the CLOB token ID from the Gamma API market response.

        The on-chain token ID (from Neg Risk Exchange) is a 77+ digit uint256
        that differs from the CLOB API token ID. The Gamma API response contains
        a 'tokens' array where each entry has 'token_id' (= CLOB token ID) and
        'outcome' (e.g. "Up", "Down", "Yes", "No").

        We match by outcome label to find the correct CLOB token ID.
        """
        tokens = market.get("tokens", [])

        # Match by outcome label
        if outcome and outcome != "Unknown":
            for token in tokens:
                if token.get("outcome", "").lower() == outcome.lower():
                    clob_id = token.get("token_id", "")
                    if clob_id:
                        return clob_id

        # Fallback: if on-chain ID happens to match a token_id directly
        for token in tokens:
            if str(token.get("token_id", "")) == onchain_token_id:
                return onchain_token_id

        # Last resort: return first token if only one exists
        if len(tokens) == 1:
            return tokens[0].get("token_id", None)

        return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class EventParser:
    """
    Parses raw blockchain events into structured ParsedTrade objects.

    Decoding OrderFilled events:
    - Non-indexed data contains: makerAssetId, takerAssetId,
      makerAmountFilled, takerAmountFilled, fee
    - Indexed topics: orderHash, maker, taker
    """

    def __init__(self) -> None:
        self.market_resolver = MarketResolver()

    async def parse_event(self, raw: RawTradeEvent) -> ParsedTrade | None:
        """Parse a raw blockchain event into a structured trade."""
        try:
            if raw.event_topic == ORDER_FILLED_TOPIC[2:] or raw.event_topic == ORDER_FILLED_TOPIC:
                logger.info("event_type", type="OrderFilled", tx=raw.tx_hash[:16])
                return await self._parse_order_filled(raw)
            elif raw.event_topic == ORDERS_MATCHED_TOPIC[2:] or raw.event_topic == ORDERS_MATCHED_TOPIC:
                logger.info("event_type", type="OrdersMatched", tx=raw.tx_hash[:16])
                return await self._parse_orders_matched(raw)
            else:
                logger.debug("unknown_event_topic", topic=raw.event_topic)
                return None
        except Exception:
            logger.exception("event_parse_error", tx_hash=raw.tx_hash)
            return None

    async def _parse_order_filled(self, raw: RawTradeEvent) -> ParsedTrade | None:
        """
        Parse an OrderFilled event.

        Data layout (non-indexed):
        - uint256 makerAssetId
        - uint256 takerAssetId
        - uint256 makerAmountFilled
        - uint256 takerAmountFilled
        - uint256 fee

        The wallet address determines if this is a buy or sell:
        - If wallet is the maker:
          - makerAssetId = 0 means maker sends USDC -> BUY
          - takerAssetId = 0 means maker receives USDC -> SELL
        - If wallet is the taker:
          - takerAssetId = 0 means taker sends USDC -> BUY
          - makerAssetId = 0 means taker receives USDC -> SELL
        """
        log = raw.log
        topics = log.get("topics", [])
        data = log.get("data", b"")

        if isinstance(data, str):
            data = bytes.fromhex(data[2:] if data.startswith("0x") else data)

        # Decode non-indexed parameters
        decoded = decode(
            ["uint256", "uint256", "uint256", "uint256", "uint256"],
            data,
        )
        maker_asset_id = decoded[0]
        taker_asset_id = decoded[1]
        maker_amount = decoded[2]
        taker_amount = decoded[3]
        fee = decoded[4]

        # Extract maker and taker addresses from indexed topics
        maker_addr = "0x" + topics[2].hex()[-40:]
        taker_addr = "0x" + topics[3].hex()[-40:]

        wallet_addr = raw.matched_wallet.address.lower()
        is_maker = maker_addr.lower() == wallet_addr

        logger.info(
            "order_filled_raw",
            tx=raw.tx_hash[:16],
            maker=maker_addr[:10],
            taker=taker_addr[:10],
            wallet_is_maker=is_maker,
            maker_asset_id=str(maker_asset_id)[:20],
            taker_asset_id=str(taker_asset_id)[:20],
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            fee=fee,
        )

        # Determine direction and amounts.
        #
        # In OrderFilled events:
        # - makerAmountFilled = what the maker gave
        # - takerAmountFilled = what the taker gave
        # - Maker sends makerAssetId, receives takerAssetId
        # - Taker sends takerAssetId, receives makerAssetId
        #
        # Three cases:
        # 1. makerAssetId=0: maker sends USDC → maker is BUYING
        # 2. takerAssetId=0: taker sends USDC → taker is BUYING
        # 3. Both non-zero (neg-risk exchange): resolve tokens via Gamma API
        #    to determine which is the outcome token

        if maker_asset_id == 0:
            # Maker sends USDC, receives tokens -> maker is BUYING
            if is_maker:
                direction = TradeDirection.BUY
                token_id = str(taker_asset_id)
            else:
                direction = TradeDirection.SELL
                token_id = str(taker_asset_id)
            amount_usdc = maker_amount / (10**USDC_DECIMALS)
            amount_tokens = taker_amount / (10**USDC_DECIMALS)
            logger.info("branch_hit", branch="maker_asset_zero", is_maker=is_maker, direction=direction.value)
        elif taker_asset_id == 0:
            # Taker sends USDC, receives tokens -> taker is BUYING
            if is_maker:
                direction = TradeDirection.SELL
                token_id = str(maker_asset_id)
            else:
                direction = TradeDirection.BUY
                token_id = str(maker_asset_id)
            amount_usdc = taker_amount / (10**USDC_DECIMALS)
            amount_tokens = maker_amount / (10**USDC_DECIMALS)
            logger.info("branch_hit", branch="taker_asset_zero", is_maker=is_maker, direction=direction.value)
        else:
            # Both assetIds are non-zero: both are conditional tokens.
            # This is the common case on the Neg Risk CTF Exchange where
            # trades settle in conditional tokens, not USDC directly.
            #
            # To determine buy/sell direction, we resolve both tokens
            # against the Gamma API. The one that resolves to a known
            # market is the outcome token being traded. The other is
            # the complementary/payment token.
            #
            # - Maker sends makerAssetId, receives takerAssetId
            # - Taker sends takerAssetId, receives makerAssetId
            maker_market = await self.market_resolver.resolve_token(str(maker_asset_id))
            taker_market = await self.market_resolver.resolve_token(str(taker_asset_id))

            if maker_market and not taker_market:
                # makerAssetId is the outcome token
                token_id = str(maker_asset_id)
                # Maker sends outcome token → maker is SELLING
                if is_maker:
                    direction = TradeDirection.SELL
                else:
                    direction = TradeDirection.BUY
                amount_tokens = maker_amount / (10**USDC_DECIMALS)
                amount_usdc = taker_amount / (10**USDC_DECIMALS)
            elif taker_market and not maker_market:
                # takerAssetId is the outcome token
                token_id = str(taker_asset_id)
                # Maker receives outcome token → maker is BUYING
                if is_maker:
                    direction = TradeDirection.BUY
                else:
                    direction = TradeDirection.SELL
                amount_tokens = taker_amount / (10**USDC_DECIMALS)
                amount_usdc = maker_amount / (10**USDC_DECIMALS)
            elif maker_market and taker_market:
                # Both resolve (e.g. Up vs Down on same neg-risk market).
                # Both are valid outcome tokens. Direction = what wallet RECEIVES.
                # Maker sends makerAssetId, receives takerAssetId.
                # Taker sends takerAssetId, receives makerAssetId.
                if is_maker:
                    # Wallet is maker → receives takerAssetId → BUY takerAssetId
                    token_id = str(taker_asset_id)
                    direction = TradeDirection.BUY
                    amount_tokens = taker_amount / (10**USDC_DECIMALS)
                    amount_usdc = maker_amount / (10**USDC_DECIMALS)
                else:
                    # Wallet is taker → receives makerAssetId → BUY makerAssetId
                    token_id = str(maker_asset_id)
                    direction = TradeDirection.BUY
                    amount_tokens = maker_amount / (10**USDC_DECIMALS)
                    amount_usdc = taker_amount / (10**USDC_DECIMALS)
                logger.info(
                    "both_tokens_resolve_direction",
                    is_maker=is_maker,
                    received_token=token_id[:20],
                    direction=direction.value,
                )
            else:
                # Neither resolves — cannot determine trade direction
                logger.warning(
                    "neither_token_resolves",
                    tx=raw.tx_hash[:16],
                    maker_asset=str(maker_asset_id)[:20],
                    taker_asset=str(taker_asset_id)[:20],
                )
                return None

        # Calculate approximate price
        price = amount_usdc / amount_tokens if amount_tokens > 0 else 0.0

        # Resolve market info
        market_info = await self.market_resolver.resolve_token(token_id)
        condition_id = market_info["condition_id"] if market_info else ""
        market_question = market_info["question"] if market_info else f"Token: {token_id}"
        market_slug = market_info["slug"] if market_info else ""
        outcome = market_info["outcome"] if market_info else "Unknown"

        # Use CLOB token ID for order placement (on-chain ID differs on Neg Risk Exchange)
        clob_token_id = market_info.get("clob_token_id") if market_info else None
        effective_token_id = clob_token_id or token_id
        if clob_token_id and clob_token_id != token_id:
            logger.info(
                "using_clob_token_id",
                onchain_id=token_id[:20],
                clob_id=clob_token_id[:20],
            )

        is_neg_risk = raw.contract_address.lower() == NEG_RISK_CTF_EXCHANGE_ADDRESS.lower()

        trade = ParsedTrade(
            wallet_address=wallet_addr,
            wallet_label=raw.matched_wallet.label or wallet_addr[:10],
            direction=direction,
            token_id=effective_token_id,
            condition_id=condition_id,
            market_slug=market_slug,
            market_question=market_question,
            outcome=outcome,
            amount_usdc=amount_usdc,
            amount_tokens=amount_tokens,
            price=price,
            tx_hash=raw.tx_hash,
            block_number=raw.block_number,
            timestamp=raw.timestamp,
            neg_risk=is_neg_risk,
        )

        logger.info("trade_parsed", summary=trade.summary, neg_risk=is_neg_risk)
        return trade

    async def _parse_orders_matched(self, raw: RawTradeEvent) -> ParsedTrade | None:
        """
        Parse an OrdersMatched event. Similar logic to OrderFilled
        but with fewer indexed fields.

        Data layout:
        - uint256 makerAssetId
        - uint256 takerAssetId
        - uint256 makerAmountFilled
        - uint256 takerAmountFilled
        """
        log = raw.log
        topics = log.get("topics", [])
        data = log.get("data", b"")

        if isinstance(data, str):
            data = bytes.fromhex(data[2:] if data.startswith("0x") else data)

        decoded = decode(
            ["uint256", "uint256", "uint256", "uint256"],
            data,
        )
        maker_asset_id = decoded[0]
        taker_asset_id = decoded[1]
        maker_amount = decoded[2]
        taker_amount = decoded[3]

        # The takerOrderMaker is in topics[2]
        taker_maker_addr = "0x" + topics[2].hex()[-40:]
        wallet_addr = raw.matched_wallet.address.lower()

        logger.info(
            "orders_matched_raw",
            tx=raw.tx_hash[:16],
            taker_maker=taker_maker_addr[:10],
            wallet=wallet_addr[:10],
            maker_asset_id=str(maker_asset_id)[:20],
            taker_asset_id=str(taker_asset_id)[:20],
            maker_amount=maker_amount,
            taker_amount=taker_amount,
        )

        # For OrdersMatched on the Neg Risk CTF Exchange:
        # makerAssetId/takerAssetId = what each side RECEIVES (not sends).
        # makerAmountFilled/takerAmountFilled = how much each side receives.
        # The wallet is the takerOrderMaker (taker).
        if maker_asset_id == 0:
            # Maker receives USDC → maker is selling tokens.
            # Taker (wallet) receives tokens → wallet is BUYING.
            direction = TradeDirection.BUY
            token_id = str(taker_asset_id)
            amount_tokens = taker_amount / (10**USDC_DECIMALS)
            amount_usdc = maker_amount / (10**USDC_DECIMALS)
            logger.info("branch_hit_matched", branch="maker_asset_zero", direction=direction.value)
        elif taker_asset_id == 0:
            # Taker (wallet) receives USDC → wallet is SELLING tokens.
            direction = TradeDirection.SELL
            token_id = str(maker_asset_id)
            amount_usdc = taker_amount / (10**USDC_DECIMALS)
            amount_tokens = maker_amount / (10**USDC_DECIMALS)
            logger.info("branch_hit_matched", branch="taker_asset_zero", direction=direction.value)
        else:
            # Both non-zero: resolve tokens to determine direction
            maker_market = await self.market_resolver.resolve_token(str(maker_asset_id))
            taker_market = await self.market_resolver.resolve_token(str(taker_asset_id))

            if maker_market and not taker_market:
                # Maker receives makerAssetId (outcome token) → wallet sold it → SELL
                token_id = str(maker_asset_id)
                direction = TradeDirection.SELL
                amount_tokens = maker_amount / (10**USDC_DECIMALS)
                amount_usdc = taker_amount / (10**USDC_DECIMALS)
            elif taker_market and not maker_market:
                # Wallet receives takerAssetId (outcome token) → BUY
                token_id = str(taker_asset_id)
                direction = TradeDirection.BUY
                amount_tokens = taker_amount / (10**USDC_DECIMALS)
                amount_usdc = maker_amount / (10**USDC_DECIMALS)
            elif maker_market and taker_market:
                # Both resolve — wallet receives takerAssetId → BUY
                token_id = str(taker_asset_id)
                direction = TradeDirection.BUY
                amount_tokens = taker_amount / (10**USDC_DECIMALS)
                amount_usdc = maker_amount / (10**USDC_DECIMALS)
            else:
                logger.warning(
                    "neither_token_resolves",
                    tx=raw.tx_hash[:16],
                    maker_asset=str(maker_asset_id)[:20],
                    taker_asset=str(taker_asset_id)[:20],
                )
                return None

        price = amount_usdc / amount_tokens if amount_tokens > 0 else 0.0

        market_info = await self.market_resolver.resolve_token(token_id)
        condition_id = market_info["condition_id"] if market_info else ""
        market_question = market_info["question"] if market_info else f"Token: {token_id}"
        market_slug = market_info["slug"] if market_info else ""
        outcome = market_info["outcome"] if market_info else "Unknown"

        # Use CLOB token ID for order placement (on-chain ID differs on Neg Risk Exchange)
        clob_token_id = market_info.get("clob_token_id") if market_info else None
        effective_token_id = clob_token_id or token_id
        if clob_token_id and clob_token_id != token_id:
            logger.info(
                "using_clob_token_id",
                onchain_id=token_id[:20],
                clob_id=clob_token_id[:20],
            )

        is_neg_risk = raw.contract_address.lower() == NEG_RISK_CTF_EXCHANGE_ADDRESS.lower()

        trade = ParsedTrade(
            wallet_address=wallet_addr,
            wallet_label=raw.matched_wallet.label or wallet_addr[:10],
            direction=direction,
            token_id=effective_token_id,
            condition_id=condition_id,
            market_slug=market_slug,
            market_question=market_question,
            outcome=outcome,
            amount_usdc=amount_usdc,
            amount_tokens=amount_tokens,
            price=price,
            tx_hash=raw.tx_hash,
            block_number=raw.block_number,
            timestamp=raw.timestamp,
            neg_risk=is_neg_risk,
        )

        logger.info("trade_parsed", summary=trade.summary, neg_risk=is_neg_risk)
        return trade

    async def close(self) -> None:
        await self.market_resolver.close()
