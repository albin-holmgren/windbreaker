# Windbreaker 🌊

**Solana Triangular Arbitrage Bot**

A Python-based bot that scans Solana DEX routes via Jupiter, detects triangular price mismatches (A → B → C → A), and executes atomic multi-swap transactions when profitable.

## Features

- 🔍 **Triangular Arbitrage Detection** - Scans multiple token paths for price inefficiencies
- ⚡ **Jupiter Integration** - Uses Jupiter aggregator for optimal routing and execution
- 🔐 **Wallet-Funded Trades** - No flash loans, uses only available wallet balance
- 📊 **Real-time Monitoring** - Telegram alerts and CSV metrics tracking
- 🛡️ **Safety First** - Simulation before execution, configurable thresholds
- 🚀 **RunPod Ready** - Deployment scripts for low-latency cloud hosting

## Quick Start

### Prerequisites

- Python 3.10+
- Solana wallet with SOL balance
- RPC endpoint (Helius/QuickNode recommended)

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/windbreaker.git
cd windbreaker

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```bash
# Copy example environment file
cp .env.example .env

# Edit with your settings
nano .env
```

Required settings:
- `RPC_URL` - Your Solana RPC endpoint
- `WALLET_PRIVATE_KEY_BASE58` - Your wallet's private key (base58 encoded)
- `NETWORK` - `devnet` for testing, `mainnet-beta` for production

### Running

```bash
# Development/testing (devnet)
python -m src.main

# Production with PM2
./scripts/start-prod.sh
```

## Architecture

```
windbreaker/
├── src/
│   ├── main.py          # Entry point and main loop
│   ├── config.py        # Configuration and constants
│   ├── wallet.py        # Wallet abstraction
│   ├── rpc.py           # RPC client with rate limiting
│   ├── arb_engine.py    # Arbitrage detection engine
│   ├── executor.py      # Transaction execution
│   └── monitor.py       # Alerts and metrics
├── tests/
│   ├── test_simulation.py
│   └── test_devnet_integration.sh
├── scripts/
│   ├── runpod-deploy.sh
│   └── start-prod.sh
└── docs/
    ├── ARCHITECTURE.md
    └── RUNPOD.md
```

## How It Works

1. **Poll**: Fetch price quotes from Jupiter for configured token paths
2. **Simulate**: Calculate expected profit after fees and slippage
3. **Decide**: Execute only if net profit exceeds threshold (default 0.5%)
4. **Execute**: Send atomic multi-swap transaction
5. **Report**: Log result and send Telegram alert

## Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PROFIT_PCT` | 0.5 | Minimum net profit to execute (%) |
| `TRADE_AMOUNT_USD` | 10 | Trade size in USD equivalent |
| `SLIPPAGE_BPS` | 50 | Slippage tolerance (0.5%) |
| `POLL_INTERVAL_MS` | 500 | Time between scans (ms) |

## Testing

```bash
# Run unit tests
pytest tests/test_simulation.py -v

# Run devnet integration test
chmod +x tests/test_devnet_integration.sh
./tests/test_devnet_integration.sh
```

## Deployment

See [docs/RUNPOD.md](docs/RUNPOD.md) for detailed deployment instructions.

Quick deploy to RunPod:
```bash
./scripts/runpod-deploy.sh user@pod-xyz.runpod.net ~/.env.windbreaker
```

## Security

⚠️ **Important Security Notes:**

- **Never commit private keys** to version control
- Use environment variables or secure secret managers
- Start with small amounts ($10-100) for testing
- Keep hot wallet funds minimal
- Consider hardware wallet integration for production

## Monitoring

The bot sends Telegram alerts for:
- ✅ Successful trades (with tx signature and profit)
- ❌ Failed trades (with error details)
- 🚨 Critical errors (RPC issues, repeated failures)

Metrics are saved to `metrics/trades.csv`:
- Timestamp, path, amounts, profit, signature

## License

MIT License - see [LICENSE](LICENSE)

## Disclaimer

This software is for educational purposes. Cryptocurrency trading carries significant risk. Use at your own risk and never trade with funds you cannot afford to lose.
# windbreaker
