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
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import aiohttp
from eth_abi import decode
from web3 import Web3

from src.logging_mod.logger import get_logger
from src.utils.constants import (
    GAMMA_API_BASE,
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
            self._session = aiohttp.ClientSession()
        return self._session

    async def resolve_token(self, token_id: str) -> dict[str, Any] | None:
        """
        Resolve a CTF token ID to market info.

        Uses the Gamma API: GET /markets?clob_token_ids={token_id}
        Returns market question, slug, condition_id, outcome labels.
        """
        if token_id in self._cache:
            return self._cache[token_id]

        session = await self._get_session()
        url = f"{GAMMA_API_BASE}/markets"
        params = {"clob_token_ids": token_id}

        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("gamma_api_error", status=resp.status, token_id=token_id)
                    return None

                data = await resp.json()
                if not data:
                    logger.warning("market_not_found", token_id=token_id)
                    return None

                market = data[0]
                result = {
                    "condition_id": market.get("condition_id", ""),
                    "question": market.get("question", "Unknown Market"),
                    "slug": market.get("slug", ""),
                    "outcome": self._resolve_outcome(market, token_id),
                    "tokens": market.get("tokens", []),
                }
                self._cache[token_id] = result
                return result

        except Exception:
            logger.exception("market_resolution_failed", token_id=token_id)
            return None

    @staticmethod
    def _resolve_outcome(market: dict[str, Any], token_id: str) -> str:
        """Determine which outcome (Yes/No) the token_id represents."""
        tokens = market.get("tokens", [])
        for token in tokens:
            if str(token.get("token_id", "")) == str(token_id):
                return token.get("outcome", "Unknown")
        return "Unknown"

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
                return await self._parse_order_filled(raw)
            elif raw.event_topic == ORDERS_MATCHED_TOPIC[2:] or raw.event_topic == ORDERS_MATCHED_TOPIC:
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

        # Determine direction and amounts
        if is_maker:
            if maker_asset_id == 0:
                # Maker sends USDC (asset 0), receives tokens -> BUY
                direction = TradeDirection.BUY
                token_id = str(taker_asset_id)
                amount_usdc = taker_amount / (10**USDC_DECIMALS)
                amount_tokens = maker_amount / (10**USDC_DECIMALS)
            else:
                # Maker sends tokens, receives USDC -> SELL
                direction = TradeDirection.SELL
                token_id = str(maker_asset_id)
                amount_tokens = maker_amount / (10**USDC_DECIMALS)
                amount_usdc = taker_amount / (10**USDC_DECIMALS)
        else:
            # Wallet is the taker
            if taker_asset_id == 0:
                # Taker sends USDC, receives tokens -> BUY
                direction = TradeDirection.BUY
                token_id = str(maker_asset_id)
                amount_usdc = maker_amount / (10**USDC_DECIMALS)
                amount_tokens = taker_amount / (10**USDC_DECIMALS)
            else:
                # Taker sends tokens, receives USDC -> SELL
                direction = TradeDirection.SELL
                token_id = str(taker_asset_id)
                amount_tokens = taker_amount / (10**USDC_DECIMALS)
                amount_usdc = maker_amount / (10**USDC_DECIMALS)

        # Calculate approximate price
        price = amount_usdc / amount_tokens if amount_tokens > 0 else 0.0

        # Resolve market info
        market_info = await self.market_resolver.resolve_token(token_id)
        condition_id = market_info["condition_id"] if market_info else ""
        market_question = market_info["question"] if market_info else f"Token: {token_id}"
        market_slug = market_info["slug"] if market_info else ""
        outcome = market_info["outcome"] if market_info else "Unknown"

        trade = ParsedTrade(
            wallet_address=wallet_addr,
            wallet_label=raw.matched_wallet.label or wallet_addr[:10],
            direction=direction,
            token_id=token_id,
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
        )

        logger.info("trade_parsed", summary=trade.summary)
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

        # For OrdersMatched, the tracked wallet is the takerOrderMaker
        if maker_asset_id == 0:
            direction = TradeDirection.SELL
            token_id = str(taker_asset_id)
            amount_tokens = taker_amount / (10**USDC_DECIMALS)
            amount_usdc = maker_amount / (10**USDC_DECIMALS)
        else:
            direction = TradeDirection.BUY
            token_id = str(maker_asset_id)
            amount_usdc = taker_amount / (10**USDC_DECIMALS)
            amount_tokens = maker_amount / (10**USDC_DECIMALS)

        price = amount_usdc / amount_tokens if amount_tokens > 0 else 0.0

        market_info = await self.market_resolver.resolve_token(token_id)
        condition_id = market_info["condition_id"] if market_info else ""
        market_question = market_info["question"] if market_info else f"Token: {token_id}"
        market_slug = market_info["slug"] if market_info else ""
        outcome = market_info["outcome"] if market_info else "Unknown"

        trade = ParsedTrade(
            wallet_address=wallet_addr,
            wallet_label=raw.matched_wallet.label or wallet_addr[:10],
            direction=direction,
            token_id=token_id,
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
        )

        logger.info("trade_parsed", summary=trade.summary)
        return trade

    async def close(self) -> None:
        await self.market_resolver.close()
