"""
Configuration management using Pydantic Settings.
Loads from .env + config.json with validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class WalletConfig(BaseModel):
    """Configuration for a single tracked wallet."""

    address: str
    label: str = ""
    enabled: bool = True
    copy_buys: bool = True
    copy_sells: bool = True
    max_risk_usd: float = 500.0

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError(f"Invalid Ethereum address: {v}")
        return v.lower()


class TradingConfig(BaseModel):
    """Trading parameters."""

    mode: Literal["fixed", "percent"] = "fixed"
    fixed_amount_usd: float = 10.0
    percent_of_balance: float = 2.0
    max_slippage_percent: float = 2.0
    max_parallel_positions: int = 10
    min_trade_amount_usd: float = 1.0
    max_trade_amount_usd: float = 100.0


class RiskConfig(BaseModel):
    """Risk management parameters."""

    max_exposure_per_market_usd: float = 200.0
    max_exposure_per_wallet_usd: float = 500.0
    max_total_exposure_usd: float = 2000.0
    max_daily_loss_usd: float = 100.0
    cooldown_after_loss_seconds: int = 300


class MonitoringConfig(BaseModel):
    """Blockchain monitoring parameters."""

    poll_interval_seconds: int = 5
    block_confirmations: int = 3
    max_block_range: int = 100


class EnvSettings(BaseSettings):
    """Settings loaded from environment / .env file."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # Polygon RPC
    polygon_rpc_url: str = "https://polygon-mainnet.g.alchemy.com/v2/demo"
    polygon_rpc_wss: str = ""

    # Polymarket CLOB API
    polymarket_api_url: str = "https://clob.polymarket.com"
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""

    # Wallet
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1  # 1 = POLY_PROXY (Magic Link), 2 = GNOSIS_SAFE

    # Bot
    dry_run: bool = True
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"


class AppConfig(BaseModel):
    """Complete application configuration."""

    env: EnvSettings
    wallets: list[WalletConfig] = Field(default_factory=list)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @property
    def active_wallets(self) -> list[WalletConfig]:
        return [w for w in self.wallets if w.enabled]

    @property
    def is_dry_run(self) -> bool:
        return self.env.dry_run


def load_config(config_path: str = "config.json") -> AppConfig:
    """Load configuration from .env and config.json."""
    env = EnvSettings()

    config_file = Path(config_path)
    file_config: dict = {}
    if config_file.exists():
        with open(config_file) as f:
            file_config = json.load(f)

    wallets = [WalletConfig(**w) for w in file_config.get("wallets", [])]
    trading = TradingConfig(**file_config.get("trading", {}))
    risk = RiskConfig(**file_config.get("risk", {}))
    monitoring = MonitoringConfig(**file_config.get("monitoring", {}))

    return AppConfig(
        env=env,
        wallets=wallets,
        trading=trading,
        risk=risk,
        monitoring=monitoring,
    )
