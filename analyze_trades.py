#!/usr/bin/env python3
"""
Trade Analysis Script - Pull real trades and compare against Cupsey
"""

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

import asyncpg


DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://")


@dataclass
class TokenState:
    token_balance: int = 0
    cost_basis_sol: float = 0.0
    realized_pnl_sol: float = 0.0
    realized_wins: int = 0
    realized_losses: int = 0

async def analyze():
    print("=" * 80)
    print("WINDBREAKER TRADE ANALYSIS")
    print("=" * 80)
    
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL env var is required (Railway provides it automatically)")

    conn = await asyncpg.connect(DATABASE_URL)
    
    # 1. Get overall stats from trades table
    print("\n## OUR BOT TRADES (from 'trades' table)")
    print("-" * 60)
    
    trades = await conn.fetch("""
        SELECT * FROM trades 
        ORDER BY detected_at DESC 
        LIMIT 100
    """)
    
    if trades:
        print(f"Total trades in DB: {len(trades)}")
        
        executed = [t for t in trades if t['status'] == 'executed']
        skipped = [t for t in trades if t['status'] == 'skipped']
        failed = [t for t in trades if t['status'] == 'failed']
        
        print(f"  - Executed: {len(executed)}")
        print(f"  - Skipped: {len(skipped)}")
        print(f"  - Failed: {len(failed)}")
        
        # PnL analysis for executed sells
        sells = [t for t in executed if t['trade_type'] == 'sell']
        buys = [t for t in executed if t['trade_type'] == 'buy']
        
        print(f"\nExecuted trades breakdown:")
        print(f"  - Buys: {len(buys)}")
        print(f"  - Sells: {len(sells)}")
        
        if sells:
            total_pnl_sol = sum(float(t['realized_pnl_sol'] or 0) for t in sells)
            total_pnl_pct = [float(t['realized_pnl_pct'] or 0) for t in sells if t['realized_pnl_pct']]
            avg_pnl_pct = sum(total_pnl_pct) / len(total_pnl_pct) if total_pnl_pct else 0
            
            wins = [t for t in sells if (t['realized_pnl_sol'] or 0) > 0]
            losses = [t for t in sells if (t['realized_pnl_sol'] or 0) < 0]
            
            print(f"\n## SELL PERFORMANCE")
            print(f"  Total Realized PnL: {total_pnl_sol:.6f} SOL")
            print(f"  Avg PnL %: {avg_pnl_pct:.2f}%")
            print(f"  Win Rate: {len(wins)}/{len(sells)} ({100*len(wins)/len(sells):.1f}%)")
            
            print(f"\n  Recent sells:")
            for t in sells[:10]:
                pnl = float(t['realized_pnl_sol'] or 0)
                pnl_pct = float(t['realized_pnl_pct'] or 0)
                exit_reason = t['exit_reason'] or 'unknown'
                token = t['token_symbol'] or t['token_mint'][:8]
                print(f"    {token}: {pnl:+.6f} SOL ({pnl_pct:+.2f}%) - {exit_reason}")
    else:
        print("No trades found in 'trades' table")
    
    # 2. Check cupsey_trades table
    print("\n" + "=" * 80)
    print("## CUPSEY'S TRADES (from 'cupsey_trades' table)")
    print("-" * 60)
    
    cupsey = await conn.fetch(
        """
        SELECT * FROM cupsey_trades
        ORDER BY detected_at DESC
        LIMIT 100
        """
    )
    
    if cupsey:
        print(f"Total Cupsey trades detected: {len(cupsey)}")
        
        copied = [c for c in cupsey if c['copied']]
        not_copied = [c for c in cupsey if not c['copied']]
        
        print(f"  - Copied: {len(copied)}")
        print(f"  - Not copied: {len(not_copied)}")
        
        # Analyze skip reasons
        skip_reasons = defaultdict(int)
        for c in not_copied:
            reason = c['skip_reason'] or 'unknown'
            # Simplify reason
            if 'market_cap' in reason.lower():
                skip_reasons['market_cap_filter'] += 1
            elif 'pump' in reason.lower():
                skip_reasons['already_pumped'] += 1
            elif 'liquidity' in reason.lower():
                skip_reasons['low_liquidity'] += 1
            elif 'sellable' in reason.lower() or 'route' in reason.lower():
                skip_reasons['not_sellable'] += 1
            elif 'position' in reason.lower():
                skip_reasons['position_limit'] += 1
            else:
                skip_reasons[reason[:40]] += 1
        
        print(f"\n  Skip reasons breakdown:")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"    - {reason}: {count}")
        
        # Show recent Cupsey trades
        print(f"\n  Recent Cupsey trades:")
        for c in cupsey[:15]:
            trade_type = c['trade_type']
            token = c['token_symbol'] or c['token_mint'][:8]
            sol = float(c['sol_amount'] or 0)
            copied_str = "✓ COPIED" if c['copied'] else f"✗ {(c['skip_reason'] or 'unknown')[:30]}"
            mcap = c['market_cap_usd']
            mcap_str = f"${mcap:,.0f}" if mcap else "$?"
            print(f"    {trade_type.upper():4} {token:12} {sol:.4f} SOL | mcap={mcap_str:>12} | {copied_str}")
    else:
        print("No cupsey_trades found")
    
    # 3. Check skipped_trades table
    print("\n" + "=" * 80)
    print("## SKIPPED TRADES ANALYSIS (from 'skipped_trades' table)")
    print("-" * 60)
    
    skipped = await conn.fetch("""
        SELECT * FROM skipped_trades 
        ORDER BY detected_at DESC 
        LIMIT 50
    """)
    
    if skipped:
        print(f"Total skipped trades: {len(skipped)}")
        
        skip_categories = defaultdict(int)
        for s in skipped:
            cat = s['skip_category'] or s['skip_reason'] or 'unknown'
            skip_categories[cat[:30]] += 1
        
        print(f"\n  Skip categories:")
        for cat, count in sorted(skip_categories.items(), key=lambda x: -x[1]):
            print(f"    - {cat}: {count}")
        
        # Check regret analysis
        regrets = [s for s in skipped if s.get('would_have_profited')]
        print(f"\n  Would have profited if copied: {len(regrets)}")
    else:
        print("No skipped_trades found")
    
    # 4. Check failed_executions
    print("\n" + "=" * 80)
    print("## FAILED EXECUTIONS (from 'failed_executions' table)")
    print("-" * 60)
    
    failed = await conn.fetch("""
        SELECT * FROM failed_executions 
        ORDER BY attempt_at DESC 
        LIMIT 30
    """)
    
    if failed:
        print(f"Total failed executions: {len(failed)}")
        
        error_cats = defaultdict(int)
        for f in failed:
            cat = f['error_category'] or f['error_code'] or 'unknown'
            error_cats[cat] += 1
        
        print(f"\n  Error categories:")
        for cat, count in sorted(error_cats.items(), key=lambda x: -x[1]):
            print(f"    - {cat}: {count}")
        
        # Show recent failures
        print(f"\n  Recent failures:")
        for f in failed[:10]:
            token = f['token_mint'][:8]
            exec_type = f['execution_type']
            error = (f['error_message'] or f['error_code'] or 'unknown')[:50]
            print(f"    {exec_type.upper():4} {token}: {error}")
    else:
        print("No failed_executions found")
    
    # 5. Analyze Cupsey's actual P&L by tracking inventory per wallet
    print("\n" + "=" * 80)
    print("## CUPSEY'S ACTUAL PERFORMANCE (Buy/Sell Pairs)")
    print("-" * 60)
    
    since = timedelta(days=7)
    since_days = int(since.total_seconds() // 86400)
    wallets = await conn.fetch(
        """
        SELECT wallet, COUNT(*) AS n
        FROM cupsey_trades
        WHERE detected_at > (NOW() - make_interval(days => $1))
        GROUP BY wallet
        ORDER BY n DESC
        """,
        since_days,
    )

    if wallets:
        print(f"Wallets seen in last {since.days}d: {len(wallets)}")
        for w in wallets[:10]:
            print(f"  {w['wallet'][:12]}... trades={w['n']}")

    for wallet_row in wallets[:5]:
        wallet = wallet_row["wallet"]
        print("\n" + "-" * 60)
        print(f"Wallet {wallet[:12]}... (last {since_days}d)")

        rows = await conn.fetch(
            """
            SELECT detected_at, trade_type, token_mint, token_symbol, sol_amount, token_amount
            FROM cupsey_trades
            WHERE wallet = $1
              AND detected_at > (NOW() - make_interval(days => $2))
            ORDER BY detected_at ASC
            """,
            wallet,
            since_days,
        )

        per_token: dict[str, TokenState] = defaultdict(TokenState)
        token_symbols: dict[str, str] = {}

        for r in rows:
            mint = r["token_mint"]
            token_symbols.setdefault(mint, r["token_symbol"] or mint[:8])
            state = per_token[mint]

            sol = float(r["sol_amount"] or 0)
            tok = int(r["token_amount"] or 0)
            if tok <= 0 or sol <= 0:
                continue

            if r["trade_type"] == "buy":
                state.token_balance += tok
                state.cost_basis_sol += sol
            elif r["trade_type"] == "sell":
                if state.token_balance <= 0 or state.cost_basis_sol <= 0:
                    continue

                sell_tok = min(tok, state.token_balance)
                frac = sell_tok / state.token_balance if state.token_balance else 1.0
                realized_cost = state.cost_basis_sol * frac
                pnl = sol - realized_cost
                state.realized_pnl_sol += pnl
                if pnl >= 0:
                    state.realized_wins += 1
                else:
                    state.realized_losses += 1
                state.token_balance -= sell_tok
                state.cost_basis_sol -= realized_cost

        realized = [(mint, st) for mint, st in per_token.items() if (st.realized_wins + st.realized_losses) > 0]
        total_pnl = sum(st.realized_pnl_sol for _, st in realized)
        total_wins = sum(st.realized_wins for _, st in realized)
        total_losses = sum(st.realized_losses for _, st in realized)
        total_trades = total_wins + total_losses

        print(f"Realized PnL (approx): {total_pnl:+.4f} SOL")
        if total_trades:
            print(f"Win rate (sell events with inventory): {total_wins}/{total_trades} ({100*total_wins/total_trades:.1f}%)")

        top = sorted(realized, key=lambda x: x[1].realized_pnl_sol, reverse=True)[:10]
        print("Top PnL tokens:")
        for mint, st in top:
            sym = token_symbols.get(mint, mint[:8])
            n = st.realized_wins + st.realized_losses
            print(f"  {sym:12} pnl={st.realized_pnl_sol:+.4f} SOL sells={n}")
    
    # 6. Summary and recommendations
    print("\n" + "=" * 80)
    print("## KEY FINDINGS & RECOMMENDATIONS")
    print("=" * 80)
    
    print("""
PROBLEM IDENTIFIED:
==================
1. Previous Cupsey PnL calc was not scoped by wallet and did not use inventory/cost basis.
   This script now computes an approximate realized PnL using buy/sell SOL flows per wallet.

2. If you see almost all trades skipped, it usually means entry filters (e.g. MIN_MARKET_CAP) are excluding most Cupsey entries.

3. The time-limit exit only affects real trading if TIME_LIMIT_MINUTES > 0. Default is 0 (disabled).

RECOMMENDATIONS:
================
1. LOWER MIN_MARKET_CAP to $5,000-$10,000 
   Set in Railway: MIN_MARKET_CAP=5000
   
   This will let you enter when Cupsey enters (early)
   
2. KEEP the pre-buy sellability check
   This protects against unsellable tokens
   
3. Consider DISABLING already_pumped filter for first entry
   Or set MAX_PRICE_CHANGE_1H higher (e.g., 500%)
   
4. The WIN is in EARLY entry + QUICK exit
   Cupsey's strategy = buy early, sell on first pump
   Missing the entry = missing the entire trade
""")
    
    await conn.close()

if __name__ == "__main__":
    asyncio.run(analyze())
