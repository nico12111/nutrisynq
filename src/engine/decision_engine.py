"""
Trade Decision Engine.

Decides whether to copy a detected trade based on:
1. Wallet configuration (copy_buys, copy_sells)
2. Risk management checks
3. Trade amount calculation (fixed or percentage)
4. Current portfolio state
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from src.logging_mod.logger import get_logger
from src.parser.event_parser import TradeDirection

if TYPE_CHECKING:
    from src.config.settings import AppConfig, WalletConfig
    from src.parser.event_parser import ParsedTrade
    from src.risk.risk_manager import RiskManager

logger = get_logger(__name__)


class DecisionAction(str, Enum):
    EXECUTE_BUY = "EXECUTE_BUY"
    EXECUTE_SELL = "EXECUTE_SELL"
    SKIP = "SKIP"


@dataclass
class TradeDecision:
    """The decision output for a detected trade."""

    action: DecisionAction
    trade: ParsedTrade
    amount_usd: float
    reason: str
    token_id: str
    outcome: str
    max_slippage: float


class DecisionEngine:
    """
    Evaluates detected trades and decides whether to copy them.

    Flow:
    1. Check if wallet config allows this trade direction
    2. Calculate trade amount based on mode (fixed/percent)
    3. Run risk checks
    4. Produce a TradeDecision
    """

    def __init__(self, config: AppConfig, risk_manager: RiskManager) -> None:
        self.config = config
        self.risk_manager = risk_manager
        self._balance_cache: float = 0.0

    def set_balance(self, balance_usd: float) -> None:
        """Update cached account balance for percent-based sizing."""
        self._balance_cache = balance_usd

    def evaluate(self, trade: ParsedTrade) -> TradeDecision:
        """
        Evaluate a parsed trade and produce a decision.

        Returns TradeDecision with action=SKIP if the trade should not be copied,
        or action=EXECUTE_BUY/EXECUTE_SELL with the calculated amount.
        """
        wallet_config = self._get_wallet_config(trade.wallet_address)
        if wallet_config is None:
            return self._skip(trade, "Wallet not in config")

        if not wallet_config.enabled:
            return self._skip(trade, "Wallet disabled")

        # Check direction filter
        if trade.direction == TradeDirection.BUY and not wallet_config.copy_buys:
            return self._skip(trade, "Buy copying disabled for this wallet")
        if trade.direction == TradeDirection.SELL and not wallet_config.copy_sells:
            return self._skip(trade, "Sell copying disabled for this wallet")

        # For sells, check if we have a position to sell
        if trade.direction == TradeDirection.SELL:
            if trade.token_id not in self.risk_manager.state.positions:
                return self._skip(trade, "No open position to sell")

        # Calculate trade amount
        amount_usd = self._calculate_amount(trade)

        # Run risk checks
        risk_result = self.risk_manager.check_trade(trade, amount_usd)
        if not risk_result.allowed:
            return self._skip(trade, f"Risk check failed: {risk_result.reason}")

        # Use potentially adjusted amount from risk manager
        final_amount = risk_result.adjusted_amount

        action = (
            DecisionAction.EXECUTE_BUY
            if trade.direction == TradeDirection.BUY
            else DecisionAction.EXECUTE_SELL
        )

        decision = TradeDecision(
            action=action,
            trade=trade,
            amount_usd=final_amount,
            reason=f"Copying {trade.wallet_label} - {trade.direction.value}",
            token_id=trade.token_id,
            outcome=trade.outcome,
            max_slippage=self.config.trading.max_slippage_percent / 100.0,
        )

        logger.info(
            "trade_decision",
            action=action.value,
            amount_usd=final_amount,
            market=trade.market_question[:60],
            wallet=trade.wallet_label,
        )

        return decision

    def _calculate_amount(self, trade: ParsedTrade) -> float:
        """Calculate trade amount based on configured mode."""
        if self.config.trading.mode == "fixed":
            return self.config.trading.fixed_amount_usd
        else:
            # Percent of balance
            return self._balance_cache * (self.config.trading.percent_of_balance / 100.0)

    def _get_wallet_config(self, address: str) -> WalletConfig | None:
        """Look up wallet configuration by address."""
        addr = address.lower()
        for w in self.config.wallets:
            if w.address.lower() == addr:
                return w
        return None

    @staticmethod
    def _skip(trade: ParsedTrade, reason: str) -> TradeDecision:
        """Create a SKIP decision."""
        logger.info("trade_skipped", reason=reason, summary=trade.summary)
        return TradeDecision(
            action=DecisionAction.SKIP,
            trade=trade,
            amount_usd=0.0,
            reason=reason,
            token_id=trade.token_id,
            outcome=trade.outcome,
            max_slippage=0.0,
        )
