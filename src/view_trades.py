#!/usr/bin/env python3
"""
View Trade History - Analyze copy trading performance.
Run: python -m src.view_trades
"""

import json
import sys
from pathlib import Path
from datetime import datetime

TRADE_HISTORY_FILE = "/windbreaker/trade_history.json"


def load_trades():
    """Load trade history."""
    path = Path(TRADE_HISTORY_FILE)
    if not path.exists():
        print("No trade history found yet.")
        return []
    
    with open(path, 'r') as f:
        return json.load(f)


def print_summary(trades):
    """Print trading summary."""
    if not trades:
        print("\n📊 No trades recorded yet.\n")
        return
    
    buys = [t for t in trades if t.get("trade_type") == "buy" and t.get("success")]
    sells = [t for t in trades if t.get("trade_type") == "sell" and t.get("success")]
    abandons = [t for t in trades if t.get("trade_type") == "abandon"]
    failed = [t for t in trades if not t.get("success")]
    
    total_invested = sum(t.get("our_sol_amount", 0) for t in buys)
    total_returned = sum(t.get("our_sol_amount", 0) for t in sells)
    total_lost_rugs = sum(t.get("entry_sol", 0) for t in abandons)
    
    realized_pnl = sum(t.get("pnl_sol", 0) for t in sells)
    net_pnl = realized_pnl - total_lost_rugs
    
    winning = [t for t in sells if t.get("pnl_sol", 0) > 0]
    losing = [t for t in sells if t.get("pnl_sol", 0) < 0]
    
    avg_delay = sum(t.get("delay_seconds", 0) for t in buys) / len(buys) if buys else 0
    
    print("\n" + "="*60)
    print("📊 COPY TRADING SUMMARY")
    print("="*60)
    
    print(f"\n📈 TRADES:")
    print(f"   Total Trades:     {len(trades)}")
    print(f"   Buys:             {len(buys)}")
    print(f"   Sells:            {len(sells)}")
    print(f"   Abandoned (Rugs): {len(abandons)}")
    print(f"   Failed:           {len(failed)}")
    
    print(f"\n💰 PERFORMANCE:")
    print(f"   Total Invested:   {total_invested:.4f} SOL")
    print(f"   Total Returned:   {total_returned:.4f} SOL")
    print(f"   Lost to Rugs:     {total_lost_rugs:.4f} SOL")
    print(f"   Realized P&L:     {realized_pnl:+.4f} SOL")
    print(f"   Net P&L:          {net_pnl:+.4f} SOL")
    
    if sells:
        win_rate = len(winning) / len(sells) * 100
        print(f"\n🎯 WIN RATE:")
        print(f"   Winning Trades:   {len(winning)}")
        print(f"   Losing Trades:    {len(losing)}")
        print(f"   Win Rate:         {win_rate:.1f}%")
    
    print(f"\n⏱️ TIMING:")
    print(f"   Avg Delay:        {avg_delay:.1f} seconds")
    
    print("\n" + "="*60)


def print_recent_trades(trades, limit=10):
    """Print recent trades."""
    if not trades:
        return
    
    recent = trades[-limit:][::-1]  # Last N, reversed
    
    print(f"\n📜 LAST {len(recent)} TRADES:")
    print("-"*60)
    
    for t in recent:
        ts = t.get("timestamp", "")[:19]  # Trim to seconds
        trade_type = t.get("trade_type", "?").upper()
        token = t.get("token_mint", "")[:8]
        success = "✅" if t.get("success") else "❌"
        
        if trade_type == "BUY":
            sol = t.get("our_sol_amount", 0)
            print(f"  {ts} | {success} {trade_type} | {token}... | {sol:.4f} SOL")
        elif trade_type == "SELL":
            pnl = t.get("pnl_sol", 0)
            pnl_pct = t.get("pnl_percent", 0)
            reason = t.get("exit_reason", "?")
            print(f"  {ts} | {success} {trade_type} | {token}... | {pnl:+.4f} SOL ({pnl_pct:+.1f}%) | {reason}")
        elif trade_type == "ABANDON":
            lost = t.get("entry_sol", 0)
            print(f"  {ts} | 💀 RUG  | {token}... | -{lost:.4f} SOL (abandoned)")
    
    print("-"*60)


def compare_with_wallet(trades, wallet_address):
    """Compare our trades from a specific wallet."""
    wallet_trades = [t for t in trades if t.get("copied_wallet", "").startswith(wallet_address[:8])]
    
    if not wallet_trades:
        print(f"\n❌ No trades found from wallet {wallet_address[:8]}...")
        return
    
    print(f"\n📋 TRADES FROM WALLET: {wallet_address[:8]}...")
    print("-"*60)
    
    for t in wallet_trades:
        trade_type = t.get("trade_type", "?").upper()
        token = t.get("token_mint", "")[:8]
        their_sol = t.get("their_sol_amount", 0)
        our_sol = t.get("our_sol_amount", 0)
        
        if trade_type == "BUY":
            print(f"  {token}... | They: {their_sol:.4f} SOL | We: {our_sol:.4f} SOL")
    
    print("-"*60)


def main():
    trades = load_trades()
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "summary":
            print_summary(trades)
        elif cmd == "recent":
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
            print_recent_trades(trades, limit)
        elif cmd == "wallet":
            if len(sys.argv) < 3:
                print("Usage: python -m src.view_trades wallet <wallet_address>")
                return
            compare_with_wallet(trades, sys.argv[2])
        elif cmd == "raw":
            print(json.dumps(trades, indent=2))
        else:
            print("Unknown command. Use: summary, recent, wallet <addr>, raw")
    else:
        # Default: show summary + recent
        print_summary(trades)
        print_recent_trades(trades, 10)


if __name__ == "__main__":
    main()
