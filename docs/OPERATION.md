# Windbreaker Operations Guide

A non-technical guide for operating the Windbreaker arbitrage bot.

## Overview

Windbreaker is an automated trading bot that looks for profitable triangular arbitrage opportunities on Solana and executes them automatically.

## What It Does

1. **Scans** prices across different token pairs every 500ms
2. **Calculates** potential profit for trading A → B → C → A
3. **Executes** trades when profit exceeds your threshold (default 0.5%)
4. **Reports** via Telegram when trades succeed or fail

## Daily Operations

### Checking Status

The bot sends Telegram messages for important events:
- ✅ **Successful trade** - Shows profit and transaction link
- ❌ **Failed trade** - Shows what went wrong
- 🚀 **Bot started** - Confirms the bot is running

### Viewing Logs

If you have SSH access to the server:

```bash
# See recent logs
pm2 logs windbreaker

# See last 100 lines
pm2 logs windbreaker --lines 100
```

### Checking Trade History

Trades are logged to `metrics/trades.csv`. You can download and open in Excel.

## Common Tasks

### Restarting the Bot

If the bot seems stuck or you updated configuration:

```bash
pm2 restart windbreaker
```

### Stopping the Bot

If you need to stop trading:

```bash
pm2 stop windbreaker
```

### Starting the Bot

To start after stopping:

```bash
pm2 start windbreaker
```

## Interpreting Alerts

### Successful Trade Alert

```
✅ Trade SUCCESS

Route: SOL → USDC → ETH → SOL
Profit: 0.52%

Input: 100000000
Output: 100520000

TX: 5abc123...
```

- **Route**: The path the trade took
- **Profit**: Net profit after fees
- **Input/Output**: Amounts in base units
- **TX**: Click link to view on Solscan

### Failed Trade Alert

```
❌ Trade FAILED

Route: SOL → USDC → ETH → SOL
Profit: 0.48%

Error: Transaction not confirmed
```

- **Error**: What went wrong
- Common causes:
  - Price moved before execution
  - Network congestion
  - Insufficient balance

## Key Metrics to Monitor

### Success Rate

A healthy bot should have >50% success rate on attempted trades.

Low success rate could mean:
- Slippage too low (increase `SLIPPAGE_BPS`)
- Competition from other bots
- Network issues

### Trade Frequency

Depends on market conditions. During volatile periods, expect more opportunities.

No trades in 24h could mean:
- Market is stable (normal)
- Bot is stuck (check logs)
- Configuration issue

### Profit per Trade

Aim for consistent small profits (0.5-2%).

Very high profits could indicate:
- Unusual market conditions
- Calculation errors (verify!)

## Warning Signs

### 🚨 Critical Alerts

These require immediate attention:
- "RPC connection failed"
- "Wallet balance low"
- "Rate limited"

Actions:
1. Check bot logs
2. Verify RPC endpoint is working
3. Add funds if needed

### ⚠️ Warnings

These are informational:
- "No opportunities found" - Normal during stable markets
- "Quote unavailable" - Temporary API issue
- "Transaction timeout" - Network congestion

## Emergency Procedures

### Stop All Trading

1. Stop the bot immediately:
   ```bash
   pm2 stop windbreaker
   ```

2. Verify wallet balance hasn't been drained
3. Check for any pending transactions

### Wallet Compromised

If you suspect key compromise:
1. Stop the bot immediately
2. Transfer remaining funds to new wallet
3. Never reuse the compromised key
4. Investigate how it was exposed

## Configuration Changes

To change trading parameters:

1. Edit `.env` file:
   ```bash
   nano /root/windbreaker/.env
   ```

2. Common settings:
   - `MIN_PROFIT_PCT=0.5` - Minimum profit to trade
   - `TRADE_AMOUNT_USD=10` - Trade size in USD
   - `SLIPPAGE_BPS=50` - Slippage tolerance (50 = 0.5%)

3. Restart the bot:
   ```bash
   pm2 restart windbreaker
   ```

## Cost of Operation

### Running Costs

- **RunPod**: $5-20/day depending on instance
- **RPC**: ~$50/month for Helius Pro
- **Transaction fees**: ~$0.01 per trade

### Break-Even

You need to make enough profit to cover costs:
- $10/day in hosting = need $10+ profit per day
- With $100 trading, need 10%+ daily return (unlikely)
- With $1000 trading, need 1%+ daily return (possible)

## Best Practices

1. **Start small**: Test with $10-50 first
2. **Monitor actively**: Check Telegram several times daily initially
3. **Review weekly**: Look at trade history and performance
4. **Update regularly**: Pull latest code for bug fixes
5. **Keep backups**: Save your `.env` file securely

## Getting Help

If something isn't working:

1. Check the logs first
2. Look for error messages in Telegram
3. Verify wallet has sufficient balance
4. Check RPC endpoint status
5. Contact developer with:
   - Error message
   - Logs (last 100 lines)
   - What you were doing when it happened
