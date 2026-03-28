"""
Risk Management Module.

Enforces trading limits and risk controls before any trade is executed.
Tracks exposure per market, per wallet, and total portfolio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.logging_mod.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import AppConfig
    from src.parser.event_parser import ParsedTrade

logger = get_logger(__name__)


@dataclass
class Position:
    """Tracks an open position."""

    token_id: str
    condition_id: str
    market_question: str
    outcome: str
    amount_usdc: float
    entry_price: float
    wallet_source: str  # Which wallet triggered this
    opened_at: float = field(default_factory=time.time)


@dataclass
class RiskState:
    """Current risk state of the portfolio."""

    positions: dict[str, Position] = field(default_factory=dict)  # token_id -> Position
    exposure_per_market: dict[str, float] = field(default_factory=dict)  # condition_id -> USD
    exposure_per_wallet: dict[str, float] = field(default_factory=dict)  # wallet_addr -> USD
    total_exposure: float = 0.0
    daily_pnl: float = 0.0
    daily_reset_timestamp: float = 0.0
    last_loss_timestamp: float = 0.0


@dataclass
class RiskCheckResult:
    """Result of a risk check."""

    allowed: bool
    reason: str = ""
    adjusted_amount: float = 0.0  # May be reduced from requested amount


class RiskManager:
    """
    Enforces risk limits and tracks portfolio exposure.

    Checks performed before each trade:
    1. Max exposure per market
    2. Max exposure per wallet source
    3. Max total exposure
    4. Max daily loss (cooldown period)
    5. Max parallel positions
    6. Trade amount within min/max bounds
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = RiskState()

    def check_trade(self, trade: ParsedTrade, trade_amount_usd: float) -> RiskCheckResult:
        """
        Run all risk checks for a proposed trade.

        Returns RiskCheckResult with allowed=True if all checks pass,
        or allowed=False with a reason if any check fails.
        The adjusted_amount may be less than requested if limits require it.
        """
        self._maybe_reset_daily()

        # Check daily loss cooldown
        if self.state.last_loss_timestamp > 0:
            cooldown = self.config.risk.cooldown_after_loss_seconds
            elapsed = time.time() - self.state.last_loss_timestamp
            if elapsed < cooldown:
                remaining = int(cooldown - elapsed)
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Loss cooldown active, {remaining}s remaining",
                )

        # Check daily loss limit
        if self.state.daily_pnl <= -self.config.risk.max_daily_loss_usd:
            return RiskCheckResult(
                allowed=False,
                reason=f"Daily loss limit reached: ${abs(self.state.daily_pnl):.2f}",
            )

        # Check max parallel positions
        if len(self.state.positions) >= self.config.trading.max_parallel_positions:
            return RiskCheckResult(
                allowed=False,
                reason=f"Max parallel positions reached: {len(self.state.positions)}",
            )

        # Enforce min/max trade amount
        amount = trade_amount_usd
        amount = max(amount, self.config.trading.min_trade_amount_usd)
        amount = min(amount, self.config.trading.max_trade_amount_usd)

        # Check per-market exposure
        market_id = trade.condition_id
        current_market_exposure = self.state.exposure_per_market.get(market_id, 0.0)
        if current_market_exposure + amount > self.config.risk.max_exposure_per_market_usd:
            max_allowed = self.config.risk.max_exposure_per_market_usd - current_market_exposure
            if max_allowed < self.config.trading.min_trade_amount_usd:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Market exposure limit: ${current_market_exposure:.2f}/{self.config.risk.max_exposure_per_market_usd:.2f}",
                )
            amount = min(amount, max_allowed)

        # Check per-wallet exposure
        wallet = trade.wallet_address
        current_wallet_exposure = self.state.exposure_per_wallet.get(wallet, 0.0)
        wallet_limit = self._get_wallet_risk_limit(wallet)
        if current_wallet_exposure + amount > wallet_limit:
            max_allowed = wallet_limit - current_wallet_exposure
            if max_allowed < self.config.trading.min_trade_amount_usd:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Wallet exposure limit: ${current_wallet_exposure:.2f}/{wallet_limit:.2f}",
                )
            amount = min(amount, max_allowed)

        # Check total portfolio exposure
        if self.state.total_exposure + amount > self.config.risk.max_total_exposure_usd:
            max_allowed = self.config.risk.max_total_exposure_usd - self.state.total_exposure
            if max_allowed < self.config.trading.min_trade_amount_usd:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Total exposure limit: ${self.state.total_exposure:.2f}/{self.config.risk.max_total_exposure_usd:.2f}",
                )
            amount = min(amount, max_allowed)

        return RiskCheckResult(allowed=True, adjusted_amount=amount)

    def record_trade_opened(self, trade: ParsedTrade, amount_usd: float) -> None:
        """Record a new position being opened."""
        position = Position(
            token_id=trade.token_id,
            condition_id=trade.condition_id,
            market_question=trade.market_question,
            outcome=trade.outcome,
            amount_usdc=amount_usd,
            entry_price=trade.price,
            wallet_source=trade.wallet_address,
        )
        self.state.positions[trade.token_id] = position
        self.state.exposure_per_market[trade.condition_id] = (
            self.state.exposure_per_market.get(trade.condition_id, 0.0) + amount_usd
        )
        self.state.exposure_per_wallet[trade.wallet_address] = (
            self.state.exposure_per_wallet.get(trade.wallet_address, 0.0) + amount_usd
        )
        self.state.total_exposure += amount_usd

        logger.info(
            "position_opened",
            token_id=trade.token_id,
            amount_usd=amount_usd,
            total_exposure=self.state.total_exposure,
        )

    def record_trade_closed(self, trade: ParsedTrade, amount_usd: float, pnl: float = 0.0) -> None:
        """Record a position being closed."""
        position = self.state.positions.pop(trade.token_id, None)
        if position:
            self.state.exposure_per_market[trade.condition_id] = max(
                0, self.state.exposure_per_market.get(trade.condition_id, 0.0) - position.amount_usdc
            )
            self.state.exposure_per_wallet[trade.wallet_address] = max(
                0, self.state.exposure_per_wallet.get(trade.wallet_address, 0.0) - position.amount_usdc
            )
            self.state.total_exposure = max(0, self.state.total_exposure - position.amount_usdc)

        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.last_loss_timestamp = time.time()

        logger.info(
            "position_closed",
            token_id=trade.token_id,
            pnl=pnl,
            daily_pnl=self.state.daily_pnl,
            total_exposure=self.state.total_exposure,
        )

    def get_status(self) -> dict:
        """Get current risk status summary."""
        return {
            "open_positions": len(self.state.positions),
            "total_exposure_usd": round(self.state.total_exposure, 2),
            "daily_pnl_usd": round(self.state.daily_pnl, 2),
            "markets_with_exposure": len([v for v in self.state.exposure_per_market.values() if v > 0]),
        }

    def _get_wallet_risk_limit(self, wallet_address: str) -> float:
        """Get the risk limit for a specific wallet."""
        for w in self.config.wallets:
            if w.address.lower() == wallet_address.lower():
                return w.max_risk_usd
        return self.config.risk.max_exposure_per_wallet_usd

    def _maybe_reset_daily(self) -> None:
        """Reset daily PnL at midnight."""
        now = time.time()
        day_start = now - (now % 86400)
        if self.state.daily_reset_timestamp < day_start:
            self.state.daily_pnl = 0.0
            self.state.daily_reset_timestamp = now
            logger.info("daily_pnl_reset")
