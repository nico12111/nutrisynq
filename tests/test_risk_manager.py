"""Tests for the risk management module."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.config.settings import AppConfig, EnvSettings, RiskConfig, TradingConfig, WalletConfig
from src.parser.event_parser import ParsedTrade, TradeDirection
from src.risk.risk_manager import RiskManager


def _make_config(**risk_overrides) -> AppConfig:
    return AppConfig(
        env=EnvSettings(dry_run=True),
        wallets=[
            WalletConfig(address="0x" + "a" * 40, label="test-wallet", max_risk_usd=500.0)
        ],
        trading=TradingConfig(
            fixed_amount_usd=10.0,
            max_parallel_positions=3,
            min_trade_amount_usd=1.0,
            max_trade_amount_usd=100.0,
        ),
        risk=RiskConfig(
            max_exposure_per_market_usd=200.0,
            max_exposure_per_wallet_usd=500.0,
            max_total_exposure_usd=1000.0,
            max_daily_loss_usd=50.0,
            cooldown_after_loss_seconds=60,
            **risk_overrides,
        ),
    )


def _make_trade(
    token_id: str = "12345",
    condition_id: str = "cond-1",
    direction: TradeDirection = TradeDirection.BUY,
    amount_usdc: float = 10.0,
    price: float = 0.5,
    wallet_address: str | None = None,
) -> ParsedTrade:
    return ParsedTrade(
        wallet_address=wallet_address or ("0x" + "a" * 40),
        wallet_label="test-wallet",
        direction=direction,
        token_id=token_id,
        condition_id=condition_id,
        market_slug="test-market",
        market_question="Will X happen?",
        outcome="Yes",
        amount_usdc=amount_usdc,
        amount_tokens=amount_usdc / price,
        price=price,
        tx_hash="0x" + "f" * 64,
        block_number=1000,
        timestamp=int(time.time()),
    )


def test_trade_allowed_within_limits():
    config = _make_config()
    rm = RiskManager(config)
    trade = _make_trade()
    result = rm.check_trade(trade, 10.0)
    assert result.allowed is True
    assert result.adjusted_amount == 10.0


def test_max_parallel_positions():
    config = _make_config()
    rm = RiskManager(config)

    # Open 3 positions (the max)
    for i in range(3):
        trade = _make_trade(token_id=f"token-{i}", condition_id=f"cond-{i}")
        rm.record_trade_opened(trade, 10.0)

    # 4th should be blocked
    trade = _make_trade(token_id="token-4", condition_id="cond-4")
    result = rm.check_trade(trade, 10.0)
    assert result.allowed is False
    assert "Max parallel positions" in result.reason


def test_market_exposure_limit():
    config = _make_config()
    rm = RiskManager(config)

    # Open position near market limit
    trade = _make_trade(amount_usdc=190.0)
    rm.record_trade_opened(trade, 190.0)

    # Next trade on same market should be capped
    trade2 = _make_trade(token_id="token-2", amount_usdc=20.0)
    result = rm.check_trade(trade2, 20.0)
    assert result.allowed is True
    assert result.adjusted_amount <= 10.0  # 200 - 190 = 10 max


def test_daily_loss_limit():
    config = _make_config()
    rm = RiskManager(config)

    # Record a big loss
    trade = _make_trade()
    rm.record_trade_closed(trade, 50.0, pnl=-55.0)

    # Next trade should be blocked
    trade2 = _make_trade(token_id="token-2", condition_id="cond-2")
    result = rm.check_trade(trade2, 10.0)
    assert result.allowed is False


def test_total_exposure_limit():
    config = _make_config()
    rm = RiskManager(config)

    # Fill up exposure to near limit
    trade = _make_trade(condition_id="cond-1")
    rm.record_trade_opened(trade, 200.0)
    trade2 = _make_trade(token_id="t2", condition_id="cond-2")
    rm.record_trade_opened(trade2, 200.0)

    # This should still work but be capped
    trade3 = _make_trade(token_id="t3", condition_id="cond-3")
    result = rm.check_trade(trade3, 700.0)
    assert result.allowed is True
    assert result.adjusted_amount <= 600.0  # 1000 - 400 = 600
