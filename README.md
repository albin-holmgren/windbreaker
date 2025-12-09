# Windbreaker 🌊

**Solana Copy Trading Bot**

Automatically copy trades from successful meme coin traders. Follow top traders from Fomo app or any Solana wallet and execute their trades in real-time.

## Features

- 👥 **Copy Trading** - Follow any Solana wallet and auto-copy their trades
- ⚡ **Fast Execution** - Detects and copies trades within seconds
- 🎯 **Pump.fun Support** - Native support for pump.fun token trades
- 📊 **Position Management** - Automatic stop loss, take profit, trailing stop
- 🛡️ **Safety Filters** - Market cap, liquidity, holder distribution filters
- 🚀 **RunPod Ready** - Deploy for 24/7 low-latency trading

## Quick Start

### Prerequisites

- Python 3.10+
- Solana wallet with SOL balance
- RPC endpoint (Helius recommended)
- Wallet addresses to copy (from Fomo app or other sources)

### Installation

```bash
git clone https://github.com/albin-holmgren/windbreaker.git
cd windbreaker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
nano .env
```

Key settings:
```bash
# Your wallet
WALLET_PRIVATE_KEY_BASE58=your_private_key
RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

# Wallets to copy (comma-separated)
COPY_WALLETS=wallet1,wallet2,wallet3

# Trade settings
COPY_MAX_SOL=0.1          # Max SOL per trade
COPY_MIN_SOL=0.05         # Min SOL per trade
SLIPPAGE_BPS=1500         # 15% slippage for meme coins

# Position management
TAKE_PROFIT_PCT=50        # Sell at 50% profit
STOP_LOSS_PCT=-35         # Sell at 35% loss
TRAILING_STOP_PCT=25      # 25% trailing stop
```

### Running

```bash
python -m src.main
```

## How It Works

1. **Monitor** - Watches configured wallet addresses for new transactions
2. **Detect** - Identifies buy/sell swaps on Jupiter, Raydium, Pump.fun
3. **Filter** - Applies safety filters (market cap, liquidity, etc.)
4. **Execute** - Copies the trade with configured size
5. **Manage** - Tracks position with stop loss / take profit

## Finding Wallets to Copy

### From Fomo App
1. Download [Fomo app](https://apps.apple.com/us/app/fomo-never-miss-out/id6741115427)
2. Find top traders on leaderboard
3. Get their Solana wallet address
4. Add to `COPY_WALLETS` in .env

### From Other Sources
- Cielo Finance - wallet PnL tracker
- Birdeye - top traders leaderboard
- Solscan - analyze successful wallets

## Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `COPY_WALLETS` | - | Comma-separated wallet addresses to copy |
| `COPY_MAX_SOL` | 0.1 | Maximum SOL per copy trade |
| `COPY_MIN_SOL` | 0.05 | Minimum SOL per copy trade |
| `SLIPPAGE_BPS` | 1500 | Slippage tolerance (15%) |
| `TAKE_PROFIT_PCT` | 50 | Take profit percentage |
| `STOP_LOSS_PCT` | -35 | Stop loss percentage |
| `TRAILING_STOP_PCT` | 25 | Trailing stop percentage |
| `COPY_SELLS` | true | Copy sell transactions too |
| `MAX_POSITIONS` | 3 | Maximum concurrent positions |

## Deployment (RunPod)

```bash
# On RunPod
git clone https://github.com/albin-holmgren/windbreaker.git
cd windbreaker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
nano .env

# Run in screen
screen -S copybot
python -m src.main
# Ctrl+A, D to detach
```

## Security

⚠️ **Important:**
- Never commit private keys
- Start with small amounts ($10-50)
- Test with one wallet first
- Monitor initial trades closely

## License

MIT License - see [LICENSE](LICENSE)

## Disclaimer

This software is for educational purposes. Cryptocurrency trading carries significant risk. Use at your own risk and never trade with funds you cannot afford to lose.
