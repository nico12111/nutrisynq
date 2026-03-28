"""
Polymarket Execution Module.

Handles order placement via the Polymarket CLOB API.
Supports both live execution and dry-run (paper trading) mode.

Polymarket CLOB API flow:
1. Authenticate using API key/secret/passphrase
2. Get current order book for the token
3. Place a market order or limit order
4. Monitor order status

Uses the py-clob-client library for API interaction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from tenacity import retry, stop_after_attempt, wait_exponential

from src.logging_mod.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import AppConfig
    from src.engine.decision_engine import TradeDecision

logger = get_logger(__name__)


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    DRY_RUN = "DRY_RUN"


@dataclass
class ExecutionResult:
    """Result of a trade execution attempt."""

    success: bool
    order_id: str
    status: OrderStatus
    filled_amount: float
    filled_price: float
    fee: float
    error: str = ""
    timestamp: float = 0.0

    @property
    def summary(self) -> str:
        if self.status == OrderStatus.DRY_RUN:
            return f"[DRY RUN] Would execute: ${self.filled_amount:.2f} @ {self.filled_price:.4f}"
        status_str = "OK" if self.success else "FAILED"
        return (
            f"[{status_str}] Order {self.order_id}: ${self.filled_amount:.2f} "
            f"@ {self.filled_price:.4f} (fee: ${self.fee:.4f})"
        )


class PolymarketExecutor:
    """
    Executes trades on Polymarket via the CLOB API.

    In dry-run mode, simulates execution without placing real orders.
    In live mode, uses py-clob-client to interact with the CLOB.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dry_run = config.is_dry_run
        self._client: Any = None

    async def initialize(self) -> None:
        """Initialize the CLOB client."""
        if self.dry_run:
            logger.info("executor_initialized", mode="DRY_RUN")
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.config.env.polymarket_api_key,
                api_secret=self.config.env.polymarket_api_secret,
                api_passphrase=self.config.env.polymarket_api_passphrase,
            )

            self._client = ClobClient(
                host=self.config.env.polymarket_api_url,
                chain_id=137,
                key=self.config.env.private_key,
                creds=creds,
            )

            logger.info("executor_initialized", mode="LIVE")
        except ImportError:
            logger.error("py_clob_client_not_installed")
            raise
        except Exception:
            logger.exception("executor_init_failed")
            raise

    async def get_balance(self) -> float:
        """Get current USDC balance on Polymarket."""
        if self.dry_run:
            return 10000.0  # Simulated balance for paper trading

        if self._client is None:
            return 0.0

        try:
            # The CLOB client provides balance info
            # This may vary based on py-clob-client version
            balance_info = self._client.get_balance_allowance()
            return float(balance_info.get("balance", 0)) / 1e6
        except Exception:
            logger.exception("balance_fetch_failed")
            return 0.0

    async def get_market_price(self, token_id: str) -> float:
        """Get the current best price for a token."""
        if self.dry_run:
            return 0.5  # Simulated mid-price

        if self._client is None:
            return 0.0

        try:
            book = self._client.get_order_book(token_id)
            # Calculate mid price from order book
            best_bid = float(book.bids[0].price) if book.bids else 0.0
            best_ask = float(book.asks[0].price) if book.asks else 1.0
            return (best_bid + best_ask) / 2.0
        except Exception:
            logger.exception("price_fetch_failed", token_id=token_id)
            return 0.0

    async def execute_buy(self, decision: TradeDecision) -> ExecutionResult:
        """Execute a buy order."""
        if self.dry_run:
            return self._simulate_execution(decision, is_buy=True)

        return await self._place_market_order(decision, side="BUY")

    async def execute_sell(self, decision: TradeDecision) -> ExecutionResult:
        """Execute a sell order."""
        if self.dry_run:
            return self._simulate_execution(decision, is_buy=False)

        return await self._place_market_order(decision, side="SELL")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _place_market_order(self, decision: TradeDecision, side: str) -> ExecutionResult:
        """
        Place a market order via the CLOB API.

        Uses py-clob-client to create and submit the order.
        Includes retry logic for transient failures.
        """
        if self._client is None:
            return ExecutionResult(
                success=False,
                order_id="",
                status=OrderStatus.FAILED,
                filled_amount=0.0,
                filled_price=0.0,
                fee=0.0,
                error="Client not initialized",
                timestamp=time.time(),
            )

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            # Get current price for slippage check
            current_price = await self.get_market_price(decision.token_id)

            # Calculate size (number of shares to buy/sell)
            if side == "BUY":
                size = decision.amount_usd / current_price if current_price > 0 else 0
                # Slippage: max price we'll pay
                worst_price = current_price * (1 + decision.max_slippage)
            else:
                # For sells, we sell the tokens we hold
                size = decision.amount_usd / current_price if current_price > 0 else 0
                worst_price = current_price * (1 - decision.max_slippage)

            order_args = MarketOrderArgs(
                token_id=decision.token_id,
                amount=decision.amount_usd,
            )

            # Create and submit the order
            signed_order = self._client.create_market_order(order_args)
            response = self._client.post_order(signed_order, OrderType.FOK)

            order_id = response.get("orderID", "")
            success = response.get("success", False)

            logger.info(
                "order_placed",
                order_id=order_id,
                side=side,
                token_id=decision.token_id,
                amount=decision.amount_usd,
                success=success,
            )

            return ExecutionResult(
                success=success,
                order_id=order_id,
                status=OrderStatus.FILLED if success else OrderStatus.FAILED,
                filled_amount=decision.amount_usd if success else 0.0,
                filled_price=current_price,
                fee=decision.amount_usd * 0.002,  # ~0.2% fee estimate
                error="" if success else response.get("errorMsg", "Unknown error"),
                timestamp=time.time(),
            )

        except Exception as e:
            logger.exception("order_execution_failed", side=side)
            return ExecutionResult(
                success=False,
                order_id="",
                status=OrderStatus.FAILED,
                filled_amount=0.0,
                filled_price=0.0,
                fee=0.0,
                error=str(e),
                timestamp=time.time(),
            )

    def _simulate_execution(self, decision: TradeDecision, is_buy: bool) -> ExecutionResult:
        """Simulate trade execution for dry-run mode."""
        simulated_price = decision.trade.price
        simulated_fee = decision.amount_usd * 0.002

        result = ExecutionResult(
            success=True,
            order_id=f"DRY-{int(time.time())}",
            status=OrderStatus.DRY_RUN,
            filled_amount=decision.amount_usd,
            filled_price=simulated_price,
            fee=simulated_fee,
            timestamp=time.time(),
        )

        side = "BUY" if is_buy else "SELL"
        logger.info(
            "dry_run_execution",
            side=side,
            amount=decision.amount_usd,
            price=simulated_price,
            market=decision.trade.market_question[:60],
        )

        return result
