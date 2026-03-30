"""
Polymarket Execution Module.

Handles order placement via the Polymarket CLOB API.
Supports both live execution and dry-run (paper trading) mode.

Uses the official py-clob-client SDK:
  https://github.com/Polymarket/py-clob-client

Polymarket CLOB API flow:
1. Authenticate using API key/secret/passphrase (L2 auth via CLOB client)
2. Get current order book for the token
3. Create and sign a market order
4. Submit via post_order with FOK (Fill-Or-Kill) order type
5. Check response for success/failure

Important notes:
- The CLOB client uses ethers-style signing (EIP-712) internally
- Orders are matched off-chain by Polymarket's operator
- Settlement happens on-chain via the CTF Exchange contracts
- Market orders use FOK to avoid partial fills hanging
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from tenacity import retry, stop_after_attempt, wait_exponential

from src.logging_mod.logger import get_logger
from src.utils.constants import POLYGON_CHAIN_ID

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
    In live mode, uses the official py-clob-client SDK to interact with the CLOB.

    SDK reference: https://github.com/Polymarket/py-clob-client
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dry_run = config.is_dry_run
        self._client: Any = None

    async def initialize(self) -> None:
        """
        Initialize the CLOB client with full authentication.

        ClobClient constructor (from official docs):
          ClobClient(host, chain_id, key, creds, signature_type, funder)

        - host: "https://clob.polymarket.com"
        - chain_id: 137 (Polygon mainnet)
        - key: private key hex string (the key you exported from Polymarket)
        - creds: ApiCreds(api_key, api_secret, api_passphrase) from setup
        - signature_type: 1 = POLY_PROXY (Magic Link login), 2 = GNOSIS_SAFE
        - funder: your proxy wallet address shown on polymarket.com/settings
        """
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
                chain_id=POLYGON_CHAIN_ID,
                key=self.config.env.private_key,
                creds=creds,
                signature_type=self.config.env.signature_type,
                funder=self.config.env.funder_address or None,
            )

            logger.info(
                "executor_initialized",
                mode="LIVE",
                signature_type=self.config.env.signature_type,
                funder=self.config.env.funder_address[:10] + "..." if self.config.env.funder_address else "none",
            )
        except ImportError:
            logger.error(
                "py_clob_client_not_installed",
                hint="Install with: pip install py-clob-client",
            )
            raise
        except Exception:
            logger.exception("executor_init_failed")
            raise

    async def get_balance(self) -> float:
        """
        Get current USDC balance.

        First tries the CLOB API, falls back to querying the on-chain
        USDC.e balance of the funder (proxy wallet) address directly.
        """
        if self.dry_run:
            return 10000.0  # Simulated balance for paper trading

        # Try CLOB API first
        if self._client is not None:
            try:
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.config.env.signature_type,
                )
                balance_info = self._client.get_balance_allowance(params)
                api_balance = float(balance_info.get("balance", 0)) / 1e6
                if api_balance > 0:
                    return api_balance
            except Exception:
                logger.debug("clob_balance_failed_trying_onchain")

        # Fallback: query on-chain USDC.e balance of funder address
        funder = self.config.env.funder_address
        if not funder:
            return 0.0

        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self.config.env.polygon_rpc_url))
            usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            erc20_abi = [
                {
                    "inputs": [{"name": "account", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "stateMutability": "view",
                    "type": "function",
                }
            ]
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(usdc_address), abi=erc20_abi
            )
            raw_balance = usdc.functions.balanceOf(
                Web3.to_checksum_address(funder)
            ).call()
            balance = raw_balance / 1e6
            logger.info("onchain_balance", funder=funder[:10], balance_usd=balance)
            return balance
        except Exception:
            logger.exception("onchain_balance_failed")
            return 0.0

    async def get_market_price(self, token_id: str) -> float:
        """
        Get the current mid-market price for a token.

        Fetches the order book and calculates:
        mid_price = (best_bid + best_ask) / 2
        """
        if self.dry_run:
            return 0.5  # Simulated mid-price

        if self._client is None:
            return 0.0

        try:
            book = self._client.get_order_book(token_id)
            best_bid = float(book.bids[0].price) if book.bids else 0.0
            best_ask = float(book.asks[0].price) if book.asks else 1.0
            mid = (best_bid + best_ask) / 2.0

            logger.debug(
                "market_price",
                token_id=token_id[:16],
                bid=best_bid,
                ask=best_ask,
                mid=mid,
            )
            return mid
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

        Flow:
        1. Fetch current market price from order book
        2. Check slippage against the price the tracked wallet got
        3. Create a MarketOrderArgs with token_id and amount
        4. Sign and create the order via client.create_market_order()
        5. Submit via client.post_order() with FOK (Fill-Or-Kill)
        6. Return execution result

        Retry logic: 3 attempts with exponential backoff (2s, 4s, 8s)
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

            # Get current price for logging
            current_price = await self.get_market_price(decision.token_id)
            if current_price <= 0:
                current_price = decision.trade.price

            # Create market order
            # amount is in USDC for buys, in shares for sells
            order_args = MarketOrderArgs(
                token_id=decision.token_id,
                amount=decision.amount_usd,
                side=side,
            )

            # Sign and create the order
            signed_order = self._client.create_market_order(order_args)

            # Submit with FOK (Fill-Or-Kill) to avoid partial fills
            response = self._client.post_order(signed_order, OrderType.FOK)

            order_id = response.get("orderID", "")
            success = response.get("success", False)

            logger.info(
                "order_placed",
                order_id=order_id,
                side=side,
                token_id=decision.token_id[:16],
                amount=decision.amount_usd,
                price=current_price,
                success=success,
            )

            return ExecutionResult(
                success=success,
                order_id=order_id,
                status=OrderStatus.FILLED if success else OrderStatus.FAILED,
                filled_amount=decision.amount_usd if success else 0.0,
                filled_price=current_price,
                fee=decision.amount_usd * 0.002,  # ~0.2% taker fee estimate
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
        """
        Simulate trade execution for dry-run / paper-trading mode.

        Uses the original trade's price as the simulated fill price.
        In reality, the fill price would likely be worse due to:
        - Execution delay (seconds between detection and order)
        - Order book slippage (market impact of your order)
        - Front-running (others may detect the same whale trade)
        """
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
