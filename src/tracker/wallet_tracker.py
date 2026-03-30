"""
Wallet Tracker Module.

Monitors Polygon blockchain for transactions from watched wallets
interacting with Polymarket contracts. Uses polling with configurable intervals.

Detection approach:
1. Poll new blocks at configured interval
2. Filter logs for OrderFilled / OrdersMatched events from CTF Exchange contracts
3. Check if maker or taker address matches any watched wallet
4. Pass matching events to the event parser for trade extraction
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import certifi
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.types import FilterParams, LogReceipt

from src.logging_mod.logger import get_logger
from src.utils.constants import (
    CTF_EXCHANGE_ADDRESS,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
    ORDER_FILLED_TOPIC,
    ORDERS_MATCHED_TOPIC,
)

if TYPE_CHECKING:
    from src.config.settings import AppConfig, WalletConfig

logger = get_logger(__name__)


@dataclass
class RawTradeEvent:
    """Raw trade event from the blockchain before parsing."""

    log: LogReceipt
    block_number: int
    tx_hash: str
    contract_address: str
    event_topic: str
    matched_wallet: WalletConfig
    timestamp: int = 0


class WalletTracker:
    """
    Monitors Polygon blocks for Polymarket trade activity from watched wallets.

    Uses log filtering on CTF Exchange contracts to detect OrderFilled events
    where a tracked wallet is the maker or taker.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self.w3 = AsyncWeb3(
            AsyncHTTPProvider(
                config.env.polygon_rpc_url,
                request_kwargs={"ssl": ssl_ctx},
            )
        )
        self.watched_addresses: set[str] = set()
        self.last_processed_block: int = 0
        self._running = False
        self._timestamp_cache: dict[int, int] = {}  # block_number -> timestamp

        # Exchange contracts to monitor
        self.exchange_contracts = [
            AsyncWeb3.to_checksum_address(CTF_EXCHANGE_ADDRESS),
            AsyncWeb3.to_checksum_address(NEG_RISK_CTF_EXCHANGE_ADDRESS),
        ]

        # Event topics to filter for
        self.event_topics = [ORDER_FILLED_TOPIC, ORDERS_MATCHED_TOPIC]

        self._wallet_map: dict[str, WalletConfig] = {}

    def load_wallets(self, wallets: list[WalletConfig]) -> None:
        """Load wallets to track."""
        self.watched_addresses.clear()
        self._wallet_map.clear()
        for w in wallets:
            if w.enabled:
                addr = w.address.lower()
                self.watched_addresses.add(addr)
                self._wallet_map[addr] = w
        logger.info("wallets_loaded", count=len(self.watched_addresses))

    async def get_current_block(self) -> int:
        """Get the current block number with confirmations."""
        latest = await self.w3.eth.block_number
        return latest - self.config.monitoring.block_confirmations

    async def initialize(self) -> None:
        """Initialize tracker with current block number."""
        self.last_processed_block = await self.get_current_block()
        logger.info("tracker_initialized", start_block=self.last_processed_block)

    async def fetch_logs(self, from_block: int, to_block: int) -> list[LogReceipt]:
        """
        Fetch relevant logs from the exchange contracts.

        We filter for OrderFilled and OrdersMatched events on both
        CTF Exchange and Neg Risk CTF Exchange contracts.
        """
        if from_block > to_block:
            return []

        # Limit block range to avoid RPC timeouts
        max_range = self.config.monitoring.max_block_range
        if to_block - from_block > max_range:
            to_block = from_block + max_range

        filter_params = FilterParams(
            fromBlock=from_block,
            toBlock=to_block,
            address=self.exchange_contracts,
            topics=[self.event_topics],
        )

        try:
            logs = await self.w3.eth.get_logs(filter_params)
            logger.debug(
                "logs_fetched",
                from_block=from_block,
                to_block=to_block,
                count=len(logs),
            )
            return list(logs)
        except Exception:
            logger.exception("log_fetch_failed", from_block=from_block, to_block=to_block)
            return []

    def _match_wallet_from_log(self, log: LogReceipt) -> WalletConfig | None:
        """
        Check if any tracked wallet is involved in this log event.

        For OrderFilled events, the maker and taker are indexed topics.
        - topics[0] = event signature
        - topics[1] = orderHash (indexed)
        - topics[2] = maker (indexed)
        - topics[3] = taker (indexed)
        """
        topics = log.get("topics", [])
        if len(topics) < 4:
            # OrdersMatched has fewer indexed params
            if len(topics) >= 3:
                # topics[2] = takerOrderMaker
                addr = "0x" + topics[2].hex()[-40:]
                addr = addr.lower()
                if addr in self.watched_addresses:
                    return self._wallet_map[addr]
            return None

        # Check maker (topics[2]) and taker (topics[3])
        for idx in (2, 3):
            addr = "0x" + topics[idx].hex()[-40:]
            addr = addr.lower()
            if addr in self.watched_addresses:
                return self._wallet_map[addr]

        return None

    async def _get_block_timestamp(self, block_number: int) -> int:
        """Get timestamp for a block (cached)."""
        if block_number in self._timestamp_cache:
            return self._timestamp_cache[block_number]
        try:
            block = await self.w3.eth.get_block(block_number)
            ts = block["timestamp"]
            self._timestamp_cache[block_number] = ts
            # Keep cache small — remove old entries
            if len(self._timestamp_cache) > 100:
                oldest = min(self._timestamp_cache)
                del self._timestamp_cache[oldest]
            return ts
        except Exception:
            return 0

    async def scan_new_blocks(self) -> list[RawTradeEvent]:
        """
        Scan for new blocks and return any trade events involving tracked wallets.
        """
        current_block = await self.get_current_block()
        if current_block <= self.last_processed_block:
            return []

        from_block = self.last_processed_block + 1
        to_block = current_block

        logs = await self.fetch_logs(from_block, to_block)
        events: list[RawTradeEvent] = []

        for log in logs:
            wallet = self._match_wallet_from_log(log)
            if wallet is None:
                continue

            block_num = log["blockNumber"]
            timestamp = await self._get_block_timestamp(block_num)
            event_topic = log["topics"][0].hex() if log["topics"] else ""

            event = RawTradeEvent(
                log=log,
                block_number=block_num,
                tx_hash=log["transactionHash"].hex(),
                contract_address=log["address"].lower(),
                event_topic=event_topic,
                matched_wallet=wallet,
                timestamp=timestamp,
            )
            events.append(event)

            logger.info(
                "wallet_activity_detected",
                wallet=wallet.label or wallet.address,
                tx_hash=event.tx_hash,
                block=block_num,
            )

        self.last_processed_block = to_block
        return events

    async def run(self, event_queue: asyncio.Queue[RawTradeEvent]) -> None:
        """
        Main loop: continuously scan for new blocks and push events to queue.
        """
        self._running = True
        await self.initialize()
        logger.info("tracker_started", poll_interval=self.config.monitoring.poll_interval_seconds)

        while self._running:
            try:
                events = await self.scan_new_blocks()
                for event in events:
                    await event_queue.put(event)
            except Exception:
                logger.exception("tracker_scan_error")

            await asyncio.sleep(self.config.monitoring.poll_interval_seconds)

    def stop(self) -> None:
        """Stop the tracker loop."""
        self._running = False
        logger.info("tracker_stopped")
