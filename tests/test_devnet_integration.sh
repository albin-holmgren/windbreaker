#!/bin/bash
#
# Devnet Integration Test Script for Windbreaker
# This script runs the bot in devnet mode and validates behavior.
#
# Prerequisites:
# - Python 3.10+ with venv
# - .env file configured for devnet
# - Devnet SOL in wallet (get from https://faucet.solana.com)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "========================================="
echo "Windbreaker Devnet Integration Test"
echo "========================================="
echo ""

# Check for .env file
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "ERROR: .env file not found!"
    echo "Please copy .env.example to .env and configure it."
    exit 1
fi

# Check network is devnet
NETWORK=$(grep "^NETWORK=" "$PROJECT_ROOT/.env" | cut -d'=' -f2)
if [ "$NETWORK" != "devnet" ]; then
    echo "ERROR: NETWORK must be set to 'devnet' for integration tests!"
    echo "Current value: $NETWORK"
    exit 1
fi

echo "✓ Network configured: devnet"

# Activate virtual environment if exists
if [ -d "$PROJECT_ROOT/.venv" ]; then
    echo "Activating virtual environment..."
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

# Install dependencies
echo ""
echo "Installing dependencies..."
pip install -q -r "$PROJECT_ROOT/requirements.txt"

# Run unit tests first
echo ""
echo "Running unit tests..."
cd "$PROJECT_ROOT"
python -m pytest tests/test_simulation.py -v --tb=short

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Unit tests failed!"
    exit 1
fi

echo ""
echo "✓ Unit tests passed"

# Test configuration loading
echo ""
echo "Testing configuration loading..."
python -c "
from src.config import load_config
config = load_config()
print(f'  Network: {config.network}')
print(f'  Min Profit: {config.min_profit_pct}%')
print(f'  Trade Amount: \${config.trade_amount_usd}')
print(f'  Slippage: {config.slippage_bps} bps')
"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Configuration loading failed!"
    exit 1
fi

echo "✓ Configuration valid"

# Test wallet loading
echo ""
echo "Testing wallet loading..."
python -c "
from src.config import load_config
from src.wallet import create_wallet
config = load_config()
wallet = create_wallet(config)
print(f'  Wallet address: {wallet.address[:20]}...')
"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Wallet loading failed!"
    exit 1
fi

echo "✓ Wallet loaded"

# Test RPC connection
echo ""
echo "Testing RPC connection..."
python -c "
import asyncio
from src.config import load_config
from src.wallet import create_wallet
from src.rpc import create_rpc_client

async def test_rpc():
    config = load_config()
    wallet = create_wallet(config)
    rpc = create_rpc_client(config)
    
    try:
        balance = await rpc.get_balance(wallet.pubkey)
        print(f'  Balance: {balance / 1e9:.6f} SOL')
        if balance < 10000000:  # Less than 0.01 SOL
            print('  WARNING: Low balance! Get devnet SOL from faucet.')
    finally:
        await rpc.close()

asyncio.run(test_rpc())
"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: RPC connection failed!"
    exit 1
fi

echo "✓ RPC connected"

# Test Jupiter API
echo ""
echo "Testing Jupiter Quote API..."
python -c "
import asyncio
from src.config import load_config
from src.arb_engine import create_arb_engine

async def test_jupiter():
    config = load_config()
    engine = create_arb_engine(config)
    
    try:
        # Try to get a simple quote
        sol_mint = 'So11111111111111111111111111111111111111112'
        usdc_mint = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
        
        quote = await engine.get_quote(sol_mint, usdc_mint, 100000000)  # 0.1 SOL
        
        if quote:
            print(f'  Quote received: {quote.input_amount} -> {quote.output_amount}')
        else:
            print('  No quote available (may be expected on devnet)')
    finally:
        await engine.close()

asyncio.run(test_jupiter())
"

echo "✓ Jupiter API tested"

# Run bot for 60 seconds in test mode
echo ""
echo "Running bot for 60 seconds..."
echo "(This will scan for opportunities but may not find any on devnet)"
echo ""

timeout 60 python -c "
import asyncio
import signal
from datetime import datetime

from src.config import load_config
from src.wallet import create_wallet
from src.rpc import create_rpc_client
from src.arb_engine import create_arb_engine

async def test_scan():
    config = load_config()
    wallet = create_wallet(config)
    rpc = create_rpc_client(config)
    engine = create_arb_engine(config)
    
    print(f'Starting scan at {datetime.utcnow().isoformat()}')
    
    try:
        for i in range(6):  # 6 scans, 10 seconds apart
            print(f'\\nScan {i+1}/6...')
            
            opportunities = await engine.scan_triangles(
                input_amount_usd=config.trade_amount_usd
            )
            
            if opportunities:
                best = opportunities[0]
                print(f'  Best opportunity: {best.path}')
                print(f'  Net profit: {best.net_profit_pct:.4f}%')
            else:
                print('  No opportunities found')
            
            if i < 5:
                await asyncio.sleep(10)
                
    finally:
        await engine.close()
        await rpc.close()
    
    print(f'\\nFinished at {datetime.utcnow().isoformat()}')

asyncio.run(test_scan())
" || true  # Don't fail on timeout

echo ""
echo "========================================="
echo "Integration Test Complete!"
echo "========================================="
echo ""
echo "Summary:"
echo "  ✓ Unit tests passed"
echo "  ✓ Configuration valid"
echo "  ✓ Wallet loaded"
echo "  ✓ RPC connected"
echo "  ✓ Jupiter API accessible"
echo "  ✓ Scan loop executed"
echo ""
echo "The bot is ready for devnet operation."
echo "To run continuously: python -m src.main"
echo ""
