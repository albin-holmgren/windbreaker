#!/usr/bin/env python3
"""
Analyze Traders - Compare tracked wallet trades vs our copies.
Identifies gaps and opportunities for improvement.
Run: python -m src.analyze_traders
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict

from .config import load_config
from .rpc import RPCClient
from .tx_parser import TransactionParser

TRADE_HISTORY_FILE = "/windbreaker/trade_history.json"


async def fetch_wallet_trades(rpc: RPCClient, wallet: str, limit: int = 50) -> List[Dict]:
    """Fetch recent trades from a wallet."""
    from solders.pubkey import Pubkey
    
    try:
        pubkey = Pubkey.from_string(wallet)
        signatures = await rpc.get_signatures_for_address(pubkey, limit=limit)
        
        parser = TransactionParser(min_sol_value=0.001)  # Catch all trades
        trades = []
        
        for sig_info in signatures[:30]:  # Analyze last 30 txs
            # Handle both dict and object formats
            if isinstance(sig_info, dict):
                sig = sig_info.get("signature", "")
            else:
                sig = sig_info.signature if hasattr(sig_info, 'signature') else str(sig_info)
            
            if not sig:
                continue
                
            try:
                tx = await rpc.get_transaction(sig)
                if tx:
                    swap = parser.parse_swap(tx, wallet)
                    if swap:
                        trades.append({
                            "timestamp": datetime.utcnow().isoformat(),
                            "signature": str(sig)[:16],
                            "type": "buy" if swap.is_buy else "sell",
                            "token": swap.token_mint[:8],
                            "token_full": swap.token_mint,
                            "sol": swap.sol_value,
                            "dex": swap.dex
                        })
            except Exception as e:
                continue
                
        return trades
    except Exception as e:
        print(f"Error fetching {wallet[:8]}...: {e}")
        return []


def load_our_trades() -> List[Dict]:
    """Load our trade history."""
    path = Path(TRADE_HISTORY_FILE)
    if not path.exists():
        return []
    
    with open(path, 'r') as f:
        return json.load(f)


def analyze_trading_patterns(wallet_trades: Dict[str, List], our_trades: List) -> Dict:
    """Analyze trading patterns and gaps."""
    
    analysis = {
        "summary": {},
        "patterns": {},
        "missed_trades": [],
        "our_performance": {},
        "recommendations": []
    }
    
    # Count their trades by type
    all_their_trades = []
    for wallet, trades in wallet_trades.items():
        for t in trades:
            t["wallet"] = wallet[:8]
            all_their_trades.append(t)
    
    their_buys = [t for t in all_their_trades if t["type"] == "buy"]
    their_sells = [t for t in all_their_trades if t["type"] == "sell"]
    
    our_buys = [t for t in our_trades if t.get("trade_type") == "buy"]
    our_sells = [t for t in our_trades if t.get("trade_type") == "sell"]
    
    analysis["summary"] = {
        "their_total_trades": len(all_their_trades),
        "their_buys": len(their_buys),
        "their_sells": len(their_sells),
        "our_buys": len(our_buys),
        "our_sells": len(our_sells),
        "copy_rate": f"{len(our_buys) / len(their_buys) * 100:.1f}%" if their_buys else "0%"
    }
    
    # Analyze token frequency (same coin multiple times)
    token_counts = defaultdict(int)
    for t in their_buys:
        token_counts[t["token"]] += 1
    
    repeated_tokens = {k: v for k, v in token_counts.items() if v > 1}
    analysis["patterns"]["repeated_buys"] = repeated_tokens
    analysis["patterns"]["unique_tokens"] = len(token_counts)
    analysis["patterns"]["avg_buys_per_token"] = len(their_buys) / len(token_counts) if token_counts else 0
    
    # Analyze trade sizes
    their_sol_amounts = [t["sol"] for t in their_buys]
    our_sol_amounts = [t.get("our_sol_amount", 0) for t in our_buys]
    
    analysis["patterns"]["their_avg_trade_sol"] = sum(their_sol_amounts) / len(their_sol_amounts) if their_sol_amounts else 0
    analysis["patterns"]["their_min_trade_sol"] = min(their_sol_amounts) if their_sol_amounts else 0
    analysis["patterns"]["their_max_trade_sol"] = max(their_sol_amounts) if their_sol_amounts else 0
    analysis["patterns"]["our_avg_trade_sol"] = sum(our_sol_amounts) / len(our_sol_amounts) if our_sol_amounts else 0
    
    # Find tokens they traded that we didn't
    their_tokens = set(t["token_full"] for t in their_buys)
    our_tokens = set(t.get("token_mint", "")[:44] for t in our_buys)
    
    missed_tokens = their_tokens - our_tokens
    analysis["missed_trades"] = list(missed_tokens)[:10]  # First 10
    
    # Buy/Sell ratio analysis
    if their_buys and their_sells:
        their_buy_sell_ratio = len(their_sells) / len(their_buys)
        analysis["patterns"]["their_buy_sell_ratio"] = f"{their_buy_sell_ratio:.2f}"
        analysis["patterns"]["their_style"] = "quick_flipper" if their_buy_sell_ratio > 0.8 else "holder"
    
    # Generate recommendations
    if len(repeated_tokens) > 0:
        avg_repeats = sum(repeated_tokens.values()) / len(repeated_tokens)
        if avg_repeats > 2:
            analysis["recommendations"].append(
                f"They buy same tokens {avg_repeats:.1f}x on average. Consider allowing multiple buys of same token."
            )
    
    if analysis["patterns"].get("their_min_trade_sol", 0) < 0.02:
        analysis["recommendations"].append(
            f"Their smallest trade is {analysis['patterns']['their_min_trade_sol']:.4f} SOL. Lower COPY_MIN_SOL to catch more."
        )
    
    if analysis["patterns"].get("their_style") == "quick_flipper":
        analysis["recommendations"].append(
            "They're quick flippers - selling fast after buying. Ensure COPY_SELLS=true or auto-sell is working."
        )
    
    if len(missed_tokens) > 5:
        analysis["recommendations"].append(
            f"Missed {len(missed_tokens)} tokens they traded. Check if position limit or balance is blocking copies."
        )
    
    return analysis


def print_analysis(analysis: Dict):
    """Print analysis results."""
    print("\n" + "="*70)
    print("📊 TRADER ANALYSIS - Their Trades vs Our Copies")
    print("="*70)
    
    s = analysis["summary"]
    print(f"\n📈 TRADE COUNTS:")
    print(f"   Their Total Trades:  {s['their_total_trades']}")
    print(f"   Their Buys:          {s['their_buys']}")
    print(f"   Their Sells:         {s['their_sells']}")
    print(f"   Our Buys:            {s['our_buys']}")
    print(f"   Our Sells:           {s['our_sells']}")
    print(f"   Copy Rate:           {s['copy_rate']}")
    
    p = analysis["patterns"]
    print(f"\n🔄 TRADING PATTERNS:")
    print(f"   Unique Tokens:       {p.get('unique_tokens', 0)}")
    print(f"   Avg Buys/Token:      {p.get('avg_buys_per_token', 0):.1f}")
    print(f"   Their Avg Trade:     {p.get('their_avg_trade_sol', 0):.4f} SOL")
    print(f"   Their Min Trade:     {p.get('their_min_trade_sol', 0):.4f} SOL")
    print(f"   Their Max Trade:     {p.get('their_max_trade_sol', 0):.4f} SOL")
    print(f"   Our Avg Trade:       {p.get('our_avg_trade_sol', 0):.4f} SOL")
    print(f"   Trading Style:       {p.get('their_style', 'unknown')}")
    print(f"   Buy/Sell Ratio:      {p.get('their_buy_sell_ratio', 'N/A')}")
    
    if p.get("repeated_buys"):
        print(f"\n🔁 REPEATED BUYS (same token multiple times):")
        for token, count in list(p["repeated_buys"].items())[:5]:
            print(f"   {token}...: {count}x")
    
    if analysis["missed_trades"]:
        print(f"\n❌ MISSED TOKENS (they traded, we didn't):")
        for token in analysis["missed_trades"][:5]:
            print(f"   {token[:16]}...")
    
    if analysis["recommendations"]:
        print(f"\n💡 RECOMMENDATIONS:")
        for i, rec in enumerate(analysis["recommendations"], 1):
            print(f"   {i}. {rec}")
    
    print("\n" + "="*70)


async def main():
    print("🔍 Analyzing trader patterns...")
    
    config = load_config()
    rpc = RPCClient(config)
    
    # Get tracked wallets
    wallets = [w.strip() for w in config.copy_wallets.split(",") if w.strip()]
    
    if not wallets:
        print("❌ No wallets configured in COPY_WALLETS")
        return
    
    print(f"📡 Fetching trades from {len(wallets)} wallets...")
    
    # Fetch their trades
    wallet_trades = {}
    for wallet in wallets:
        print(f"   Fetching {wallet[:8]}...")
        trades = await fetch_wallet_trades(rpc, wallet)
        wallet_trades[wallet] = trades
        print(f"   Found {len(trades)} swaps")
    
    # Load our trades
    our_trades = load_our_trades()
    print(f"\n📋 Our trade history: {len(our_trades)} trades")
    
    # Analyze
    analysis = analyze_trading_patterns(wallet_trades, our_trades)
    
    # Print results
    print_analysis(analysis)
    
    # Save raw analysis
    with open("/windbreaker/trader_analysis.json", "w") as f:
        json.dump(analysis, f, indent=2, default=str)
    print("\n💾 Full analysis saved to /windbreaker/trader_analysis.json")


if __name__ == "__main__":
    asyncio.run(main())
