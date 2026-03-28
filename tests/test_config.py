"""Tests for configuration loading."""

from __future__ import annotations

import json
import os
import tempfile

from src.config.settings import WalletConfig, load_config


def test_wallet_address_validation():
    w = WalletConfig(address="0x" + "A" * 40, label="test")
    assert w.address == "0x" + "a" * 40  # should be lowercased


def test_wallet_address_invalid():
    import pytest

    with pytest.raises(ValueError, match="Invalid Ethereum address"):
        WalletConfig(address="not-an-address", label="test")


def test_load_config_with_file():
    config_data = {
        "wallets": [
            {
                "address": "0x" + "b" * 40,
                "label": "test-w",
                "enabled": True,
                "copy_buys": True,
                "copy_sells": False,
                "max_risk_usd": 100.0,
            }
        ],
        "trading": {"mode": "fixed", "fixed_amount_usd": 5.0},
        "risk": {"max_total_exposure_usd": 500.0},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config_data, f)
        f.flush()
        config = load_config(f.name)

    os.unlink(f.name)

    assert len(config.wallets) == 1
    assert config.wallets[0].label == "test-w"
    assert config.trading.fixed_amount_usd == 5.0
    assert config.risk.max_total_exposure_usd == 500.0
    assert config.active_wallets == config.wallets
