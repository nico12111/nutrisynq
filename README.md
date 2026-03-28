# Polymarket Copy-Trading Bot

A modular Python bot that monitors Polygon wallet addresses for Polymarket trading activity and optionally copies their trades on your own account.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     CopyTradingBot (Orchestrator)           │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │ WalletTracker │───>│ EventParser  │───>│DecisionEngine │  │
│  │ (Polygon RPC) │    │ (ABI decode) │    │ (copy logic)  │  │
│  └──────────────┘    └──────────────┘    └──────┬────────┘  │
│                                                  │          │
│                          ┌───────────────┐       │          │
│                          │ RiskManager   │<──────┘          │
│                          │ (limits/expo) │                  │
│                          └──────┬────────┘                  │
│                                 │                           │
│                     ┌───────────▼──────────┐                │
│                     │PolymarketExecutor    │                │
│                     │(CLOB API / dry-run)  │                │
│                     └──────────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

### Modules

| Module | File | Purpose |
|---|---|---|
| **Config** | `src/config/settings.py` | Loads `.env` + `config.json`, validates with Pydantic |
| **WalletTracker** | `src/tracker/wallet_tracker.py` | Polls Polygon blocks for CTF Exchange events from watched wallets |
| **EventParser** | `src/parser/event_parser.py` | Decodes `OrderFilled`/`OrdersMatched` events, resolves market info via Gamma API |
| **DecisionEngine** | `src/engine/decision_engine.py` | Applies wallet filters, calculates trade size, checks risk |
| **RiskManager** | `src/risk/risk_manager.py` | Enforces per-market, per-wallet, and portfolio exposure limits |
| **PolymarketExecutor** | `src/execution/polymarket_executor.py` | Places orders via CLOB API or simulates in dry-run mode |
| **Bot** | `src/bot.py` | Orchestrates all modules with async event pipeline |
| **CLI** | `src/main.py` | Click-based CLI with `run`, `status`, `check-wallets` commands |

## How Trade Detection Works

### On-Chain Detection

Polymarket trades happen through the **CTF Exchange** contracts on Polygon:
- `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` (CTF Exchange)
- `0xC5d563A36AE78145C45a50134d48A1215220f80a` (Neg Risk CTF Exchange)

The bot filters for `OrderFilled` and `OrdersMatched` events where a tracked wallet is the maker or taker.

### Buy vs. Sell Detection

From `OrderFilled` events:
- **BUY**: Wallet sends USDC (assetId=0) and receives outcome tokens
- **SELL**: Wallet sends outcome tokens and receives USDC

### Market Resolution

The `token_id` from the event maps to a specific market outcome. The bot queries the **Polymarket Gamma API** (`gamma-api.polymarket.com/markets?clob_token_ids=...`) to resolve:
- Market question (e.g., "Will X happen by Y?")
- Outcome label (Yes/No)
- Condition ID

### Price Calculation

Approximate price: `amount_usdc / amount_tokens` (normalized by decimals).

> Note: The actual execution price on your copy trade may differ due to slippage and order book depth.

## Setup

### 1. Prerequisites

- Python 3.11+
- A Polygon RPC endpoint (Alchemy, Infura, QuickNode recommended)
- A Polymarket account with API credentials
- USDC.e on Polygon for live trading

### 2. Install

```bash
# Clone and install
git clone <repo-url> && cd polymarket-copy-trader
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
# Copy and edit environment variables
cp .env.example .env
# Edit .env with your RPC URL, API keys, and private key

# Edit config.json with wallet addresses and trading parameters
```

### 4. Run

```bash
# Dry-run mode (default, no real trades)
python -m src.main run --dry-run

# Check wallet configuration
python -m src.main check-wallets

# Show current config
python -m src.main status

# Live mode (CAUTION: real trades!)
# First set DRY_RUN=false in .env, then:
python -m src.main run
```

### 5. Docker

```bash
docker compose up -d
# Logs: docker compose logs -f
```

## Configuration

### `.env` - Secrets & Environment

| Variable | Description |
|---|---|
| `POLYGON_RPC_URL` | Polygon JSON-RPC endpoint |
| `POLYMARKET_API_KEY` | Your Polymarket CLOB API key |
| `POLYMARKET_API_SECRET` | Your Polymarket CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | Your Polymarket CLOB API passphrase |
| `PRIVATE_KEY` | Your wallet private key for signing orders |
| `DRY_RUN` | `true` for paper trading, `false` for live |

### `config.json` - Trading Parameters

**Wallets**: List of addresses to track with per-wallet settings (copy_buys, copy_sells, max_risk).

**Trading**: `mode` (fixed/percent), `fixed_amount_usd`, `percent_of_balance`, `max_slippage_percent`, `max_parallel_positions`.

**Risk**: `max_exposure_per_market_usd`, `max_exposure_per_wallet_usd`, `max_total_exposure_usd`, `max_daily_loss_usd`, `cooldown_after_loss_seconds`.

**Monitoring**: `poll_interval_seconds`, `block_confirmations`, `max_block_range`.

## Risks & Limitations

### Technical Risks
- **Execution delay**: Block confirmation + parsing + API call = seconds of delay. The copied price will differ from the original.
- **Slippage**: Thin order books mean your market order may execute at a worse price.
- **RPC reliability**: Public RPCs have rate limits and can miss blocks. Use a paid provider.
- **API rate limits**: Both Polygon RPCs and Polymarket APIs have rate limits.
- **Event detection gaps**: If the bot is offline, it will miss trades. It does NOT backfill.

### Financial Risks
- **Front-running awareness**: Your copy trades are visible on-chain. Others could front-run you.
- **Adverse selection**: By the time you copy, the price has already moved.
- **Correlation risk**: Copying multiple wallets that trade the same markets amplifies exposure.
- **Smart money isn't always right**: Even profitable wallets have losing trades.

### Legal Risks
- **Regulatory uncertainty**: Prediction market regulations vary by jurisdiction.
- **No financial advice**: This tool is for educational and research purposes.
- **Terms of service**: Verify that automated trading is permitted under Polymarket's ToS.

### Operational Risks
- **Private key security**: Your private key is stored in `.env`. Use a dedicated wallet with limited funds.
- **No guaranteed fills**: Market orders can fail if there's no liquidity.
- **Bot failure**: Crashes, network issues, or bugs can cause missed trades or stuck positions.

## Backtesting Approach

To evaluate whether copying specific wallets is profitable:

1. **Collect historical data**: Use the Polygon archive node to replay `OrderFilled` events for target wallets
2. **Record trades**: Build a dataset of all trades (market, direction, price, timestamp)
3. **Simulate execution**: For each trade, check what price you would have gotten N seconds later
4. **Account for costs**: Subtract estimated slippage (based on order book depth), fees (~0.2%), and execution delay
5. **Calculate metrics**: Win rate, average PnL per trade, Sharpe ratio, max drawdown

This can be implemented by extending the `EventParser` to process historical logs instead of live ones.

## Project Structure

```
polymarket-copy-trader/
├── src/
│   ├── config/
│   │   └── settings.py          # Pydantic config management
│   ├── tracker/
│   │   └── wallet_tracker.py    # Polygon blockchain monitoring
│   ├── parser/
│   │   └── event_parser.py      # Event decoding & market resolution
│   ├── engine/
│   │   └── decision_engine.py   # Trade decision logic
│   ├── risk/
│   │   └── risk_manager.py      # Risk limits & exposure tracking
│   ├── execution/
│   │   └── polymarket_executor.py  # CLOB API interaction
│   ├── logging_mod/
│   │   └── logger.py            # Structured logging (JSON + console)
│   ├── utils/
│   │   └── constants.py         # Contract addresses, ABIs, chain config
│   ├── bot.py                   # Main orchestrator
│   └── main.py                  # CLI entry point
├── tests/
├── config.json                  # Trading configuration
├── .env.example                 # Environment template
├── pyproject.toml               # Dependencies & project metadata
├── Dockerfile
├── docker-compose.yml
└── README.md
```
