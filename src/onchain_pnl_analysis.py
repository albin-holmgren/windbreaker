import asyncio
import os
import json
from typing import List, Dict, Any
from datetime import datetime, timedelta

from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts

# Configuration
RPC_ENDPOINT = os.getenv("RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")  # Replace with your bot's wallet address

# Market cap buckets (in USD)
MARKET_CAP_BUCKETS = [
    (0, 10000, 'Micro (<$10K)'),
    (10000, 50000, 'Small ($10K-$50K)'),
    (50000, 100000, 'Medium ($50K-$100K)'),
    (100000, 500000, 'Large ($100K-$500K)'),
    (500000, float('inf'), 'Huge (>$500K)')
]

def categorize_market_cap(market_cap: float) -> str:
    """Categorize market cap into predefined buckets."""
    for lower, upper, label in MARKET_CAP_BUCKETS:
        if lower <= market_cap < upper:
            return label
    return 'Unknown'

async def fetch_transactions(client: AsyncClient, wallet: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Fetch recent transactions for a given wallet."""
    wallet_pubkey = Pubkey.from_string(wallet)
    try:
        response = await client.get_signatures_for_address(wallet_pubkey, limit=limit)
        signatures = response.value
        transactions = []
        for sig in signatures:
            tx = await client.get_transaction(sig.signature, opts=TxOpts(skip_preflight=True))
            if tx.value and tx.value.transaction:
                transactions.append({
                    'signature': str(sig.signature),
                    'slot': tx.value.slot,
                    'timestamp': tx.value.block_time,
                    'meta': tx.value.transaction.meta.to_json() if tx.value.transaction.meta else None
                })
        return transactions
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []

async def analyze_pnl(transactions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze transactions to compute realized PnL."""
    # This is a placeholder for actual PnL calculation logic
    # In a real implementation, you'd parse transaction metadata to identify buys/sells and SOL deltas
    profit_by_bucket = {}
    trade_count_by_bucket = {}
    for bucket in [b[2] for b in MARKET_CAP_BUCKETS]:
        profit_by_bucket[bucket] = 0.0
        trade_count_by_bucket[bucket] = 0
    return {
        'profit_by_bucket': profit_by_bucket,
        'trade_count_by_bucket': trade_count_by_bucket,
        'total_transactions': len(transactions)
    }

async def main():
    if not WALLET_ADDRESS:
        print("Error: WALLET_ADDRESS environment variable not set. Please set your bot's wallet address.")
        return

    async with AsyncClient(RPC_ENDPOINT) as client:
        print(f"Fetching transactions for wallet: {WALLET_ADDRESS[:8]}...")
        transactions = await fetch_transactions(client, WALLET_ADDRESS)
        print(f"Retrieved {len(transactions)} transactions.")

        if transactions:
            analysis = await analyze_pnl(transactions)
            print("\nBot's Realized Profit Analysis (On-Chain Transactions):")
            print("=" * 60)
            for bucket in [b[2] for b in MARKET_CAP_BUCKETS]:
                profit = analysis['profit_by_bucket'][bucket]
                count = analysis['trade_count_by_bucket'][bucket]
                print(f"{bucket}: {profit:.4f} SOL over {count} trades")
            print("=" * 60)
            print(f"Total Transactions Analyzed: {analysis['total_transactions']}")
        else:
            print("No transactions found for analysis.")

if __name__ == "__main__":
    asyncio.run(main())
