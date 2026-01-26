import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import os

import asyncpg
from dotenv import load_dotenv


def _fmt_pct(x):
    if x is None:
        return "None"
    return f"{float(x):+.2f}%"


def _fmt_sol(x):
    if x is None:
        return "None"
    return f"{float(x):+.4f} SOL"


async def _with_timeout(coro, seconds: float, label: str):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(f"timeout: {label} ({seconds}s)")


async def run(hours: float, days: float, statement_timeout_ms: int, connect_timeout_s: float, query_timeout_s: float):
    load_dotenv()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set (expected in environment or .env)")

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days) - timedelta(hours=hours)

    pool = await _with_timeout(
        asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3, command_timeout=query_timeout_s),
        connect_timeout_s,
        "create_pool",
    )

    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = '{int(statement_timeout_ms)}ms'")

            trades_by_trader = await _with_timeout(
                conn.fetch(
                    """
                    SELECT
                      trader_wallet,
                      COUNT(*) FILTER (WHERE status = 'executed' AND closed_at IS NOT NULL) AS closed_trades,
                      SUM(realized_pnl_sol) FILTER (WHERE status = 'executed' AND closed_at IS NOT NULL) AS pnl_sol_sum,
                      AVG(realized_pnl_sol) FILTER (WHERE status = 'executed' AND closed_at IS NOT NULL) AS pnl_sol_avg,
                      AVG(realized_pnl_pct) FILTER (WHERE status = 'executed' AND closed_at IS NOT NULL) AS pnl_pct_avg,
                      COUNT(*) FILTER (WHERE status = 'executed' AND closed_at IS NOT NULL AND realized_pnl_sol > 0) AS wins
                    FROM trades
                    WHERE detected_at >= $1
                      AND trade_type = 'buy'
                    GROUP BY trader_wallet
                    ORDER BY pnl_sol_sum DESC NULLS LAST
                    """,
                    since,
                ),
                query_timeout_s,
                "trades_by_trader",
            )

            exit_reason_breakdown = await _with_timeout(
                conn.fetch(
                    """
                    SELECT
                      trader_wallet,
                      COALESCE(exit_reason, '(null)') AS exit_reason,
                      COUNT(*) AS n,
                      SUM(realized_pnl_sol) AS pnl_sol_sum,
                      AVG(realized_pnl_pct) AS pnl_pct_avg
                    FROM trades
                    WHERE detected_at >= $1
                      AND status = 'executed'
                      AND closed_at IS NOT NULL
                      AND trade_type = 'buy'
                    GROUP BY trader_wallet, COALESCE(exit_reason, '(null)')
                    ORDER BY trader_wallet, pnl_sol_sum DESC NULLS LAST
                    """,
                    since,
                ),
                query_timeout_s,
                "exit_reason_breakdown",
            )

            followup_minutes = [1, 3, 5, 10, 30, 60]
            followups_compare = await _with_timeout(
                conn.fetch(
                    """
                    SELECT
                      f.followup_minutes,
                      COUNT(*) AS n,
                      AVG(f.pnl_if_held_sol) AS avg_pnl_if_held_sol,
                      AVG(t.realized_pnl_sol) AS avg_realized_pnl_sol,
                      AVG(f.pnl_if_held_pct) AS avg_pnl_if_held_pct,
                      AVG(t.realized_pnl_pct) AS avg_realized_pnl_pct
                    FROM post_trade_followups f
                    JOIN trades t ON t.id = f.trade_id
                    WHERE t.detected_at >= $1
                      AND t.status = 'executed'
                      AND t.closed_at IS NOT NULL
                      AND t.trade_type = 'buy'
                      AND f.followup_minutes = ANY($2::int[])
                    GROUP BY f.followup_minutes
                    ORDER BY f.followup_minutes
                    """,
                    since,
                    followup_minutes,
                ),
                query_timeout_s,
                "followups_compare",
            )

            stop_loss_recovery = await _with_timeout(
                conn.fetchrow(
                    """
                    SELECT
                      COUNT(*) AS n,
                      AVG(t.realized_pnl_sol) AS avg_realized_pnl_sol,
                      AVG(f.pnl_if_held_sol) AS avg_pnl_if_held_sol,
                      COUNT(*) FILTER (WHERE f.pnl_if_held_sol > t.realized_pnl_sol) AS improved,
                      COUNT(*) FILTER (WHERE f.pnl_if_held_sol > 0) AS would_be_profitable
                    FROM trades t
                    JOIN post_trade_followups f ON f.trade_id = t.id AND f.followup_minutes = 60
                    WHERE t.detected_at >= $1
                      AND t.status = 'executed'
                      AND t.closed_at IS NOT NULL
                      AND t.trade_type = 'buy'
                      AND t.exit_reason = 'stop_loss'
                      AND f.pnl_if_held_sol IS NOT NULL
                      AND t.realized_pnl_sol IS NOT NULL
                    """,
                    since,
                ),
                query_timeout_s,
                "stop_loss_recovery",
            )

            skipped_summary = await _with_timeout(
                conn.fetchrow(
                    """
                    SELECT
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE price_1h_later IS NOT NULL) AS with_1h,
                      COUNT(*) FILTER (WHERE price_1h_later IS NOT NULL AND would_have_profited IS TRUE) AS would_profit,
                      AVG(price_change_1h_later_pct) FILTER (WHERE price_change_1h_later_pct IS NOT NULL) AS avg_1h_pct
                    FROM skipped_trades
                    WHERE detected_at >= $1
                    """,
                    since,
                ),
                query_timeout_s,
                "skipped_summary",
            )

            top_skip_reasons = await _with_timeout(
                conn.fetch(
                    """
                    SELECT COALESCE(skip_reason, '(null)') AS reason, COUNT(*) AS n
                    FROM skipped_trades
                    WHERE detected_at >= $1
                    GROUP BY COALESCE(skip_reason, '(null)')
                    ORDER BY n DESC
                    LIMIT 15
                    """,
                    since,
                ),
                query_timeout_s,
                "top_skip_reasons",
            )

            mcap_buckets = [0, 5_000, 10_000, 20_000, 30_000, 50_000, 100_000]
            mcap_bucket_stats = await _with_timeout(
                conn.fetch(
                    """
                    WITH base AS (
                      SELECT
                        market_cap_usd,
                        price_change_1h_later_pct
                      FROM skipped_trades
                      WHERE detected_at >= $1
                        AND price_change_1h_later_pct IS NOT NULL
                        AND market_cap_usd IS NOT NULL
                    ),
                    buckets AS (
                      SELECT
                        unnest($2::numeric[]) AS lo,
                        unnest($3::numeric[]) AS hi
                    )
                    SELECT
                      b.lo,
                      b.hi,
                      COUNT(*) AS n,
                      AVG(base.price_change_1h_later_pct) AS avg_1h
                    FROM buckets b
                    JOIN base ON base.market_cap_usd >= b.lo AND base.market_cap_usd < b.hi
                    GROUP BY b.lo, b.hi
                    ORDER BY b.lo
                    """,
                    since,
                    [float(x) for x in mcap_buckets[:-1]],
                    [float(x) for x in mcap_buckets[1:]],
                ),
                query_timeout_s,
                "mcap_bucket_stats",
            )

            thresholds = [5_000, 10_000, 20_000, 30_000]
            mcap_threshold_whatif = await _with_timeout(
                conn.fetch(
                    """
                    WITH base AS (
                      SELECT
                        market_cap_usd,
                        price_change_1h_later_pct,
                        skip_reason
                      FROM skipped_trades
                      WHERE detected_at >= $1
                        AND price_change_1h_later_pct IS NOT NULL
                        AND market_cap_usd IS NOT NULL
                        AND (skip_reason ILIKE '%market_cap%' OR skip_reason ILIKE '%mcap%')
                    ),
                    thresholds AS (
                      SELECT unnest($2::numeric[]) AS new_min_mcap
                    )
                    SELECT
                      t.new_min_mcap,
                      COUNT(*) FILTER (WHERE base.market_cap_usd >= t.new_min_mcap) AS trades_added,
                      AVG(base.price_change_1h_later_pct) FILTER (WHERE base.market_cap_usd >= t.new_min_mcap) AS avg_return_1h_pct,
                      COUNT(*) FILTER (WHERE base.market_cap_usd >= t.new_min_mcap AND base.price_change_1h_later_pct > 0) AS winners
                    FROM thresholds t
                    CROSS JOIN base
                    GROUP BY t.new_min_mcap
                    ORDER BY t.new_min_mcap
                    """,
                    since,
                    [float(x) for x in thresholds],
                ),
                query_timeout_s,
                "mcap_threshold_whatif",
            )

        print("=== TELEMETRY ANALYSIS ===")
        print(f"window_since_utc: {since.isoformat()}")
        print("")

        print("-- Baseline (closed trades) by trader_wallet --")
        if not trades_by_trader:
            print("no rows")
        for r in trades_by_trader:
            closed_trades = int(r["closed_trades"] or 0)
            wins = int(r["wins"] or 0)
            winrate = (wins / closed_trades * 100) if closed_trades else 0.0
            print(
                " ".join(
                    [
                        str(r["trader_wallet"]),
                        f"closed={closed_trades}",
                        f"winrate={winrate:.1f}%",
                        f"pnl_sum={_fmt_sol(r['pnl_sol_sum'])}",
                        f"pnl_avg={_fmt_sol(r['pnl_sol_avg'])}",
                        f"pnl_pct_avg={_fmt_pct(r['pnl_pct_avg'])}",
                    ]
                )
            )
        print("")

        print("-- Exit reason breakdown (sum pnl) --")
        if not exit_reason_breakdown:
            print("no rows")
        current_wallet = None
        for r in exit_reason_breakdown:
            tw = r["trader_wallet"]
            if tw != current_wallet:
                current_wallet = tw
                print(f" trader_wallet={tw}")
            print(
                f"  {r['exit_reason']}: n={int(r['n'])} pnl_sum={_fmt_sol(r['pnl_sol_sum'])} pnl_pct_avg={_fmt_pct(r['pnl_pct_avg'])}"
            )
        print("")

        print("-- What-if: hold after exit (compare vs realized) --")
        if not followups_compare:
            print("no followup rows")
        for r in followups_compare:
            print(
                f" {int(r['followup_minutes'])}m: n={int(r['n'])} avg_real={_fmt_sol(r['avg_realized_pnl_sol'])} avg_if_held={_fmt_sol(r['avg_pnl_if_held_sol'])} avg_real_pct={_fmt_pct(r['avg_realized_pnl_pct'])} avg_if_held_pct={_fmt_pct(r['avg_pnl_if_held_pct'])}"
            )
        print("")

        print("-- What-if: stop_loss exits, hold 60m instead --")
        if stop_loss_recovery and stop_loss_recovery["n"]:
            n = int(stop_loss_recovery["n"])
            improved = int(stop_loss_recovery["improved"] or 0)
            would_be_profitable = int(stop_loss_recovery["would_be_profitable"] or 0)
            print(
                f" n={n} improved={improved} ({(improved/n)*100:.1f}%) would_be_profitable={would_be_profitable} ({(would_be_profitable/n)*100:.1f}%) avg_real={_fmt_sol(stop_loss_recovery['avg_realized_pnl_sol'])} avg_if_held_60m={_fmt_sol(stop_loss_recovery['avg_pnl_if_held_sol'])}"
            )
        else:
            print("no stop_loss rows with 60m followup")
        print("")

        print("-- Skipped trades (regret analysis) --")
        if skipped_summary:
            total = int(skipped_summary["total"] or 0)
            with_1h = int(skipped_summary["with_1h"] or 0)
            would_profit = int(skipped_summary["would_profit"] or 0)
            rate = (would_profit / with_1h * 100) if with_1h else 0.0
            print(
                f" total={total} with_1h={with_1h} would_profit={would_profit} ({rate:.1f}%) avg_1h={_fmt_pct(skipped_summary['avg_1h_pct'])}"
            )
        print("")

        print("-- Top skip reasons --")
        for r in top_skip_reasons:
            print(f" {int(r['n'])}: {str(r['reason'])}")
        print("")

        print("-- Skipped 1h return by mcap bucket --")
        for r in mcap_bucket_stats:
            print(
                f" [{int(r['lo'])}, {int(r['hi'])}): n={int(r['n'])} avg_1h={_fmt_pct(r['avg_1h'])}"
            )
        print("")

        print("-- What-if: lower MIN_MARKET_CAP_USD (approx using skipped trades) --")
        for r in mcap_threshold_whatif:
            added = int(r["trades_added"] or 0)
            winners = int(r["winners"] or 0)
            winrate = (winners / added * 100) if added else 0.0
            print(
                f" new_min_mcap={int(r['new_min_mcap'])}: trades_added={added} winrate_1h={winrate:.1f}% avg_return_1h={_fmt_pct(r['avg_return_1h_pct'])}"
            )

    finally:
        await pool.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=0.0)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--statement-timeout-ms", type=int, default=4000)
    p.add_argument("--connect-timeout-s", type=float, default=5.0)
    p.add_argument("--query-timeout-s", type=float, default=6.0)
    args = p.parse_args()

    asyncio.run(
        run(
            hours=args.hours,
            days=args.days,
            statement_timeout_ms=args.statement_timeout_ms,
            connect_timeout_s=args.connect_timeout_s,
            query_timeout_s=args.query_timeout_s,
        )
    )


if __name__ == "__main__":
    main()
