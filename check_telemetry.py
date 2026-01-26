#!/usr/bin/env python3
"""Quick telemetry check - run with: python check_telemetry.py"""
import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def main():
    url = os.getenv('DATABASE_URL')
    if not url:
        print("ERROR: DATABASE_URL not set in .env")
        return
    
    print("Connecting to database...")
    pool = await asyncpg.create_pool(url, command_timeout=10)
    
    async with pool.acquire() as conn:
        print("\n=== TELEMETRY SUMMARY ===\n")
        
        # Skipped trades
        skipped_total = await conn.fetchval("SELECT COUNT(*) FROM skipped_trades")
        skipped_with_1h = await conn.fetchval("SELECT COUNT(*) FROM skipped_trades WHERE price_1h_later IS NOT NULL")
        skipped_prof = await conn.fetchval("SELECT COUNT(*) FROM skipped_trades WHERE would_have_profited IS TRUE")
        
        print(f"skipped_trades total: {skipped_total}")
        print(f"  with 1h outcome: {skipped_with_1h}")
        print(f"  would have profited: {skipped_prof}")
        if skipped_with_1h:
            print(f"  profit rate: {(skipped_prof/skipped_with_1h)*100:.1f}%")
        
        # Avg 1h change
        avg_1h = await conn.fetchval("SELECT AVG(price_change_1h_later_pct) FROM skipped_trades WHERE price_change_1h_later_pct IS NOT NULL")
        if avg_1h:
            print(f"  avg 1h change: {float(avg_1h):+.2f}%")
        
        # Followups for skipped
        followups_skipped = await conn.fetchval("SELECT COUNT(*) FROM post_trade_followups WHERE trade_id IS NULL")
        print(f"\npost_trade_followups (skipped): {followups_skipped}")
        
        by_min = await conn.fetch("""
            SELECT followup_minutes, COUNT(*) 
            FROM post_trade_followups 
            WHERE trade_id IS NULL 
            GROUP BY followup_minutes 
            ORDER BY followup_minutes
        """)
        if by_min:
            print("  by minute: " + ", ".join([f"{r[0]}m={r[1]}" for r in by_min]))
        
        # Followups for real trades
        followups_trades = await conn.fetchval("SELECT COUNT(*) FROM post_trade_followups WHERE trade_id IS NOT NULL")
        print(f"\npost_trade_followups (trades): {followups_trades}")
        
        # Cupsey trades
        cupsey_buys = await conn.fetchval("SELECT COUNT(*) FROM cupsey_trades WHERE trade_type = 'buy'")
        cupsey_sells = await conn.fetchval("SELECT COUNT(*) FROM cupsey_trades WHERE trade_type = 'sell'")
        cupsey_sells_priced = await conn.fetchval("SELECT COUNT(*) FROM cupsey_trades WHERE trade_type = 'sell' AND price_usd IS NOT NULL")
        print(f"\ncupsey_trades buys: {cupsey_buys}")
        print(f"cupsey_trades sells: {cupsey_sells} (priced: {cupsey_sells_priced})")
        
        # Top skip reasons
        top_reasons = await conn.fetch("""
            SELECT COALESCE(skip_reason, '(null)'), COUNT(*) AS n 
            FROM skipped_trades 
            GROUP BY skip_reason 
            ORDER BY n DESC 
            LIMIT 5
        """)
        print("\nTop skip reasons:")
        for r in top_reasons:
            print(f"  {r[1]}: {str(r[0])[:80]}")
        
        # Recent skipped with outcomes
        recent = await conn.fetch("""
            SELECT token_mint, price_change_1h_later_pct, would_have_profited, skip_reason
            FROM skipped_trades
            WHERE price_1h_later IS NOT NULL
            ORDER BY detected_at DESC
            LIMIT 10
        """)
        if recent:
            print("\nRecent skipped trades with 1h outcomes:")
            for r in recent:
                pct = f"{float(r[1]):+.1f}%" if r[1] else "n/a"
                prof = "✓" if r[2] else "✗"
                print(f"  {str(r[0])[:8]} | {pct} | {prof} | {str(r[3])[:50] if r[3] else ''}")
    
    await pool.close()
    print("\nDone!")

if __name__ == "__main__":
    asyncio.run(main())
