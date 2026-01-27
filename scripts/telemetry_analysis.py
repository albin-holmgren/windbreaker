import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import os

import ssl
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

try:
    import asyncpg
except ModuleNotFoundError:
    asyncpg = None

try:
    import pg8000.dbapi as pg8000
except ModuleNotFoundError:
    pg8000 = None
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        env_path = ".env"
        try:
            if not os.path.exists(env_path):
                return False
            loaded = False
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if not k:
                        continue
                    if k in os.environ:
                        continue
                    os.environ[k] = v
                    loaded = True
            return loaded
        except Exception:
            return False


def _fmt_pct(x):
    if x is None:
        return "None"
    return f"{float(x):+.2f}%"


def _fmt_sol(x):
    if x is None:
        return "None"
    return f"{float(x):+.4f} SOL"


def _as_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _pct_change(new_v: Any, old_v: Any) -> Optional[float]:
    n = _as_float(new_v)
    o = _as_float(old_v)
    if n is None or o is None or o == 0:
        return None
    return (n / o - 1.0) * 100.0


def _parse_csv_floats(s: Optional[str]) -> List[float]:
    if not s:
        return []
    out: List[float] = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _parse_csv_ints(s: Optional[str]) -> List[int]:
    if not s:
        return []
    out: List[int] = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(float(part)))
    return out


def _summarize_returns(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "avg": None,
            "winrate": None,
            "rug60": None,
            "rug90": None,
            "p50": None,
            "p10": None,
            "p90": None,
        }
    xs = sorted(values)
    n = len(xs)

    def q(p: float) -> float:
        if n == 1:
            return xs[0]
        i = int(round((n - 1) * p))
        i = max(0, min(n - 1, i))
        return xs[i]

    wins = sum(1 for v in xs if v > 0)
    rug60 = sum(1 for v in xs if v <= -60)
    rug90 = sum(1 for v in xs if v <= -90)
    return {
        "n": n,
        "avg": sum(xs) / n,
        "winrate": wins / n * 100.0,
        "rug60": rug60 / n * 100.0,
        "rug90": rug90 / n * 100.0,
        "p50": q(0.50),
        "p10": q(0.10),
        "p90": q(0.90),
    }


def _simulate_skipped(
    *,
    skipped_rows: List[Dict[str, Any]],
    followup_rows: List[Dict[str, Any]],
    exit_minutes: List[int],
    mcap_thresholds: List[float],
    liq_thresholds: List[float],
    max_price_change_1h: Optional[float],
    max_price_change_5m: Optional[float],
    min_volume_5m: Optional[float],
    min_txns_5m: Optional[int],
    min_buy_ratio_5m: Optional[float],
    stop_losses: List[float],
    top_n: int,
) -> None:
    skipped: Dict[str, Dict[str, Any]] = {}
    for r in skipped_rows:
        cid = str(r.get("correlation_id"))
        skipped[cid] = r

    returns_by_cid: Dict[str, Dict[int, float]] = {}
    for f in followup_rows:
        cid = str(f.get("correlation_id"))
        if cid not in skipped:
            continue
        mins = f.get("followup_minutes")
        if mins is None:
            continue
        mins_i = int(mins)
        if mins_i not in exit_minutes:
            continue
        ret = _pct_change(f.get("price_usd"), f.get("exit_price_usd"))
        if ret is None:
            continue
        returns_by_cid.setdefault(cid, {})[mins_i] = float(ret)

    def _risk_score(summary: Dict[str, Any]) -> Optional[float]:
        p10 = summary.get("p10")
        avg = summary.get("avg")
        rug60 = summary.get("rug60")
        n = int(summary.get("n") or 0)
        if p10 is None or rug60 is None or avg is None:
            return None
        score = 0.25 * float(avg) + 0.75 * float(p10) - 0.75 * float(rug60)
        if n < 50:
            score -= float(50 - n) * 0.2
        return score

    def _score_key(v: Optional[float]) -> float:
        return float(v) if v is not None else -1e18

    def passes_filters(cid: str, min_mcap: float, min_liq: float) -> bool:
        s = skipped[cid]
        mcap = _as_float(s.get("market_cap_usd"))
        liq = _as_float(s.get("liquidity_usd"))
        if mcap is None or liq is None:
            return False
        if mcap < min_mcap:
            return False
        if liq < min_liq:
            return False
        if max_price_change_1h is not None:
            pc1h = _as_float(s.get("price_change_1h_pct"))
            if pc1h is not None and pc1h > max_price_change_1h:
                return False

        if max_price_change_5m is not None:
            pcm5 = _as_float(s.get("price_change_m5_pct"))
            if pcm5 is None:
                return False
            if pcm5 > max_price_change_5m:
                return False

        if min_volume_5m is not None:
            v5 = _as_float(s.get("volume_5m_usd"))
            if v5 is None:
                return False
            if v5 < min_volume_5m:
                return False

        if min_txns_5m is not None:
            b = s.get("txns_5m_buys")
            se = s.get("txns_5m_sells")
            if b is None or se is None:
                return False
            tx5 = int(b or 0) + int(se or 0)
            if tx5 < int(min_txns_5m):
                return False

        if min_buy_ratio_5m is not None:
            b = s.get("txns_5m_buys")
            se = s.get("txns_5m_sells")
            if b is None or se is None:
                return False
            buys = float(b or 0)
            sells = float(se or 0)
            denom = buys + sells
            if denom <= 0:
                return False
            ratio = buys / denom
            if ratio < float(min_buy_ratio_5m):
                return False
        return True

    print("")
    print("-- Simulation (skipped trades): policy sweeps --")
    print(
        " ".join(
            [
                f"exit_minutes={exit_minutes}",
                f"mcap_thresholds={mcap_thresholds}",
                f"liq_thresholds={liq_thresholds}",
                f"max_price_change_1h={max_price_change_1h}",
                f"max_price_change_5m={max_price_change_5m}",
                f"min_volume_5m={min_volume_5m}",
                f"min_txns_5m={min_txns_5m}",
                f"min_buy_ratio_5m={min_buy_ratio_5m}",
                f"stop_losses={stop_losses}",
            ]
        )
    )

    results: List[Dict[str, Any]] = []
    for mins in exit_minutes:
        for min_mcap in mcap_thresholds:
            for min_liq in liq_thresholds:
                vals: List[float] = []
                for cid, by_min in returns_by_cid.items():
                    if mins not in by_min:
                        continue
                    if not passes_filters(cid, min_mcap=min_mcap, min_liq=min_liq):
                        continue
                    vals.append(by_min[mins])
                summary = _summarize_returns(vals)
                if summary["n"] <= 0:
                    continue
                score = _risk_score(summary)
                results.append(
                    {
                        "exit_minutes": mins,
                        "min_mcap": min_mcap,
                        "min_liq": min_liq,
                        "score": score,
                        **summary,
                    }
                )

    results.sort(
        key=lambda r: (
            r["exit_minutes"],
            -_score_key(r.get("score")),
            -(r["avg"] or -1e18),
            -int(r.get("n") or 0),
        )
    )

    by_exit: Dict[int, List[Dict[str, Any]]] = {}
    for r in results:
        by_exit.setdefault(int(r["exit_minutes"]), []).append(r)

    for mins in exit_minutes:
        rs = by_exit.get(int(mins), [])
        if not rs:
            continue
        print(f" exit={int(mins)}m (top {top_n} by risk score)")
        for r in rs[:top_n]:
            print(
                " ".join(
                    [
                        f"  min_mcap={int(r['min_mcap'])}",
                        f"min_liq={int(r['min_liq'])}",
                        f"n={int(r['n'])}",
                        f"score={_score_key(r.get('score')):.2f}",
                        f"avg={_fmt_pct(r['avg'])}",
                        f"p50={_fmt_pct(r['p50'])}",
                        f"p10={_fmt_pct(r['p10'])}",
                        f"win={float(r['winrate']):.1f}%",
                        f"rug60={float(r['rug60']):.1f}%",
                        f"rug90={float(r['rug90']):.1f}%",
                    ]
                )
            )

    if not stop_losses:
        return

    print("")
    print("-- Simulation (skipped trades): stoploss at checkpoints --")
    stop_results: List[Dict[str, Any]] = []
    for mins in exit_minutes:
        for stop in stop_losses:
            for min_mcap in mcap_thresholds:
                for min_liq in liq_thresholds:
                    vals: List[float] = []
                    for cid, by_min in returns_by_cid.items():
                        if mins not in by_min:
                            continue
                        if not passes_filters(cid, min_mcap=min_mcap, min_liq=min_liq):
                            continue
                        path = [(m, by_min[m]) for m in sorted(by_min.keys()) if m <= mins]
                        if not path:
                            continue
                        exit_ret = None
                        for m, v in path:
                            if v <= stop:
                                exit_ret = v
                                break
                        if exit_ret is None:
                            exit_ret = by_min[mins]
                        vals.append(float(exit_ret))
                    summary = _summarize_returns(vals)
                    if summary["n"] <= 0:
                        continue
                    score = _risk_score(summary)
                    stop_results.append(
                        {
                            "exit_minutes": mins,
                            "stop": stop,
                            "min_mcap": min_mcap,
                            "min_liq": min_liq,
                            "score": score,
                            **summary,
                        }
                    )

    stop_results.sort(
        key=lambda r: (
            r["exit_minutes"],
            r["stop"],
            -_score_key(r.get("score")),
            -(r["avg"] or -1e18),
            -int(r.get("n") or 0),
        )
    )

    by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in stop_results:
        by_key.setdefault((int(r["exit_minutes"]), float(r["stop"])), []).append(r)

    for mins in exit_minutes:
        for stop in stop_losses:
            rs = by_key.get((int(mins), float(stop)), [])
            if not rs:
                continue
            print(f" exit={int(mins)}m stoploss={float(stop):.1f}% (top {top_n} by risk score)")
            for r in rs[:top_n]:
                print(
                    " ".join(
                        [
                            f"  min_mcap={int(r['min_mcap'])}",
                            f"min_liq={int(r['min_liq'])}",
                            f"n={int(r['n'])}",
                            f"score={_score_key(r.get('score')):.2f}",
                            f"avg={_fmt_pct(r['avg'])}",
                            f"p50={_fmt_pct(r['p50'])}",
                            f"p10={_fmt_pct(r['p10'])}",
                            f"win={float(r['winrate']):.1f}%",
                            f"rug60={float(r['rug60']):.1f}%",
                            f"rug90={float(r['rug90']):.1f}%",
                        ]
                    )
                )


async def _with_timeout(coro, seconds: float, label: str):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(f"timeout: {label} ({seconds}s)")


def _pg8000_connect(dsn: str):
    if pg8000 is None:
        raise RuntimeError("pg8000 not installed")

    u = urlparse(dsn)
    if u.scheme not in ("postgres", "postgresql"):
        raise RuntimeError(f"unsupported DATABASE_URL scheme: {u.scheme}")

    host = u.hostname
    if not host:
        raise RuntimeError("DATABASE_URL missing hostname")

    database = (u.path or "").lstrip("/")
    if not database:
        raise RuntimeError("DATABASE_URL missing database name")

    user = unquote(u.username or "")
    password = unquote(u.password or "")
    port = u.port or 5432

    qs = parse_qs(u.query or "")
    sslmode = (qs.get("sslmode", [""])[0] or "").lower()
    ssl_context = None
    if sslmode and sslmode != "disable":
        ssl_context = ssl.create_default_context()

    return pg8000.connect(
        user=user or None,
        password=password or None,
        host=host,
        port=port,
        database=database,
        ssl_context=ssl_context,
    )


def _pg8000_fetch(conn, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(sql, params)
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _pg8000_fetchrow(conn, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    rows = _pg8000_fetch(conn, sql, params)
    return rows[0] if rows else None


async def run(
    hours: float,
    days: float,
    statement_timeout_ms: int,
    connect_timeout_s: float,
    query_timeout_s: float,
    simulate_skipped: bool,
    sim_wallet: Optional[str],
    sim_exit_minutes_csv: str,
    sim_mcap_thresholds_csv: str,
    sim_liq_thresholds_csv: str,
    sim_max_price_change_1h: Optional[float],
    sim_max_price_change_5m: Optional[float],
    sim_min_volume_5m: Optional[float],
    sim_min_txns_5m: Optional[int],
    sim_min_buy_ratio_5m: Optional[float],
    sim_stop_losses_csv: str,
    sim_top: int,
):
    load_dotenv()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set (expected in environment or .env)")

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days) - timedelta(hours=hours)

    if asyncpg is None:
        if pg8000 is None:
            raise RuntimeError("asyncpg is not installed; install pg8000 to run this script on Python 3.13")

        conn = _pg8000_connect(dsn)
        try:
            conn.autocommit = True
            _pg8000_fetch(conn, f"SET statement_timeout = '{int(statement_timeout_ms)}ms'")

            latest_closed = _pg8000_fetch(
                conn,
                """
                SELECT
                  detected_at,
                  trader_wallet,
                  token_symbol,
                  token_mint,
                  exit_reason,
                  realized_pnl_sol,
                  realized_pnl_pct
                FROM trades
                WHERE detected_at >= %s
                  AND status IN ('executed','closed')
                  AND closed_at IS NOT NULL
                  AND trade_type = 'buy'
                ORDER BY detected_at DESC
                LIMIT 25
                """,
                (since,),
            )

            trades_by_trader = _pg8000_fetch(
                conn,
                """
                SELECT
                  trader_wallet,
                  COUNT(*) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS closed_trades,
                  SUM(realized_pnl_sol) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS pnl_sol_sum,
                  AVG(realized_pnl_sol) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS pnl_sol_avg,
                  AVG(realized_pnl_pct) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS pnl_pct_avg,
                  COUNT(*) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL AND realized_pnl_sol > 0) AS wins
                FROM trades
                WHERE detected_at >= %s
                  AND trade_type = 'buy'
                GROUP BY trader_wallet
                ORDER BY pnl_sol_sum DESC NULLS LAST
                """,
                (since,),
            )

            exit_reason_breakdown = _pg8000_fetch(
                conn,
                """
                SELECT
                  trader_wallet,
                  COALESCE(exit_reason, '(null)') AS exit_reason,
                  COUNT(*) AS n,
                  SUM(realized_pnl_sol) AS pnl_sol_sum,
                  AVG(realized_pnl_pct) AS pnl_pct_avg
                FROM trades
                WHERE detected_at >= %s
                  AND status IN ('executed','closed')
                  AND closed_at IS NOT NULL
                  AND trade_type = 'buy'
                GROUP BY trader_wallet, COALESCE(exit_reason, '(null)')
                ORDER BY trader_wallet, pnl_sol_sum DESC NULLS LAST
                """,
                (since,),
            )

            print("=== TELEMETRY ANALYSIS ===")
            print(f"window_since_utc: {since.isoformat()}")
            print("")

            print("-- Latest closed trades (most recent first) --")
            if not latest_closed:
                print("no rows")
            for r in latest_closed:
                sym = r.get("token_symbol") or "?"
                mint = r.get("token_mint") or ""
                print(
                    " ".join(
                        [
                            str(r.get("detected_at")),
                            str(r.get("trader_wallet")),
                            f"{sym}",
                            f"{mint[:8]}",
                            f"exit={r.get('exit_reason')}",
                            f"pnl={_fmt_sol(r.get('realized_pnl_sol'))}",
                            f"pnl_pct={_fmt_pct(r.get('realized_pnl_pct'))}",
                        ]
                    )
                )
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

            if simulate_skipped:
                exit_minutes = _parse_csv_ints(sim_exit_minutes_csv) or [1, 3, 5, 10, 30, 60]
                mcap_thresholds = _parse_csv_floats(sim_mcap_thresholds_csv) or [50_000, 30_000, 20_000, 10_000, 0]
                liq_thresholds = _parse_csv_floats(sim_liq_thresholds_csv) or [20_000, 10_000, 5_000, 1_000, 0]
                stop_losses = _parse_csv_floats(sim_stop_losses_csv)

                skipped_where = "WHERE s.detected_at >= %s"
                skipped_params: List[Any] = [since]
                if sim_wallet:
                    skipped_where += " AND s.trader_wallet = %s"
                    skipped_params.append(sim_wallet)

                skipped_rows = _pg8000_fetch(
                    conn,
                    f"""
                    SELECT
                      s.correlation_id,
                      s.trader_wallet,
                      s.skip_reason,
                      s.skip_category,
                      COALESCE(ms.market_cap_usd, s.market_cap_usd) AS market_cap_usd,
                      COALESCE(ms.liquidity_usd, s.liquidity_usd) AS liquidity_usd,
                      s.volume_24h_usd,
                      s.price_change_1h_pct,
                      s.token_age_minutes,
                      ms.volume_5m_usd,
                      ms.txns_5m_buys,
                      ms.txns_5m_sells,
                      ms.price_change_m5_pct
                    FROM skipped_trades s
                    LEFT JOIN market_snapshots ms
                      ON ms.trade_id IS NULL
                      AND ms.correlation_id = s.correlation_id
                      AND ms.snapshot_type = 'skipped_detection'
                    {skipped_where}
                    """,
                    tuple(skipped_params),
                )

                in_placeholders = ", ".join(["%s"] * len(exit_minutes))
                followup_sql = f"""
                    SELECT
                      correlation_id,
                      followup_minutes,
                      exit_price_usd,
                      price_usd
                    FROM post_trade_followups
                    WHERE trade_id IS NULL
                      AND exit_at >= %s
                      AND followup_minutes IN ({in_placeholders})
                """
                followup_params: List[Any] = [since, *exit_minutes]
                followup_rows = _pg8000_fetch(conn, followup_sql, tuple(followup_params))

                _simulate_skipped(
                    skipped_rows=skipped_rows,
                    followup_rows=followup_rows,
                    exit_minutes=exit_minutes,
                    mcap_thresholds=mcap_thresholds,
                    liq_thresholds=liq_thresholds,
                    max_price_change_1h=sim_max_price_change_1h,
                    max_price_change_5m=sim_max_price_change_5m,
                    min_volume_5m=sim_min_volume_5m,
                    min_txns_5m=sim_min_txns_5m,
                    min_buy_ratio_5m=sim_min_buy_ratio_5m,
                    stop_losses=stop_losses,
                    top_n=sim_top,
                )

        finally:
            try:
                conn.close()
            except Exception:
                pass
        return

    pool = await _with_timeout(
        asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3, command_timeout=query_timeout_s),
        connect_timeout_s,
        "create_pool",
    )

    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = '{int(statement_timeout_ms)}ms'")

            latest_closed = await _with_timeout(
                conn.fetch(
                    """
                    SELECT
                      detected_at,
                      trader_wallet,
                      token_symbol,
                      token_mint,
                      exit_reason,
                      realized_pnl_sol,
                      realized_pnl_pct
                    FROM trades
                    WHERE detected_at >= $1
                      AND status IN ('executed', 'closed')
                      AND closed_at IS NOT NULL
                      AND trade_type = 'buy'
                    ORDER BY detected_at DESC
                    LIMIT 25
                    """,
                    since,
                ),
                query_timeout_s,
                "latest_closed",
            )

            trades_by_trader = await _with_timeout(
                conn.fetch(
                    """
                    SELECT
                      trader_wallet,
                      COUNT(*) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS closed_trades,
                      SUM(realized_pnl_sol) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS pnl_sol_sum,
                      AVG(realized_pnl_sol) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS pnl_sol_avg,
                      AVG(realized_pnl_pct) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL) AS pnl_pct_avg,
                      COUNT(*) FILTER (WHERE status IN ('executed','closed') AND closed_at IS NOT NULL AND realized_pnl_sol > 0) AS wins
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
                      AND status IN ('executed', 'closed')
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
                      AND t.status IN ('executed', 'closed')
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
                      AND t.status IN ('executed', 'closed')
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
                      COUNT(*) FILTER (WHERE price_change_1h_later_pct IS NOT NULL AND price_change_1h_later_pct <= -60) AS rug_60,
                      COUNT(*) FILTER (WHERE price_change_1h_later_pct IS NOT NULL AND price_change_1h_later_pct <= -90) AS rug_90,
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
                      AVG(base.price_change_1h_later_pct) AS avg_1h,
                      COUNT(*) FILTER (WHERE base.price_change_1h_later_pct > 0) AS winners,
                      COUNT(*) FILTER (WHERE base.price_change_1h_later_pct <= -60) AS rug_60,
                      COUNT(*) FILTER (WHERE base.price_change_1h_later_pct <= -90) AS rug_90
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
                      COUNT(*) FILTER (WHERE base.market_cap_usd >= t.new_min_mcap AND base.price_change_1h_later_pct > 0) AS winners,
                      COUNT(*) FILTER (WHERE base.market_cap_usd >= t.new_min_mcap AND base.price_change_1h_later_pct <= -60) AS rug_60,
                      COUNT(*) FILTER (WHERE base.market_cap_usd >= t.new_min_mcap AND base.price_change_1h_later_pct <= -90) AS rug_90
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

        print("-- Latest closed trades (most recent first) --")
        if not latest_closed:
            print("no rows")
        for r in latest_closed:
            sym = r.get("token_symbol") or "?"
            mint = r.get("token_mint") or ""
            print(
                " ".join(
                    [
                        str(r.get("detected_at")),
                        str(r.get("trader_wallet")),
                        f"{sym}",
                        f"{mint[:8]}",
                        f"exit={r.get('exit_reason')}",
                        f"pnl={_fmt_sol(r.get('realized_pnl_sol'))}",
                        f"pnl_pct={_fmt_pct(r.get('realized_pnl_pct'))}",
                    ]
                )
            )
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
            rug_60 = int(skipped_summary["rug_60"] or 0)
            rug_90 = int(skipped_summary["rug_90"] or 0)
            rate = (would_profit / with_1h * 100) if with_1h else 0.0
            print(
                f" total={total} with_1h={with_1h} would_profit={would_profit} ({rate:.1f}%) rug_60={rug_60} rug_90={rug_90} avg_1h={_fmt_pct(skipped_summary['avg_1h_pct'])}"
            )
        print("")

        print("-- Top skip reasons --")
        for r in top_skip_reasons:
            print(f" {int(r['n'])}: {str(r['reason'])}")
        print("")

        print("-- Skipped 1h return by mcap bucket --")
        for r in mcap_bucket_stats:
            n = int(r["n"] or 0)
            winners = int(r["winners"] or 0)
            rug_60 = int(r["rug_60"] or 0)
            rug_90 = int(r["rug_90"] or 0)
            winrate = (winners / n * 100) if n else 0.0
            rug60_rate = (rug_60 / n * 100) if n else 0.0
            rug90_rate = (rug_90 / n * 100) if n else 0.0
            print(
                f" [{int(r['lo'])}, {int(r['hi'])}): n={n} winrate={winrate:.1f}% rug60={rug60_rate:.1f}% rug90={rug90_rate:.1f}% avg_1h={_fmt_pct(r['avg_1h'])}"
            )
        print("")

        print("-- What-if: lower MIN_MARKET_CAP_USD (approx using skipped trades) --")
        for r in mcap_threshold_whatif:
            added = int(r["trades_added"] or 0)
            winners = int(r["winners"] or 0)
            rug_60 = int(r["rug_60"] or 0)
            rug_90 = int(r["rug_90"] or 0)
            winrate = (winners / added * 100) if added else 0.0
            rug60_rate = (rug_60 / added * 100) if added else 0.0
            rug90_rate = (rug_90 / added * 100) if added else 0.0
            print(
                f" new_min_mcap={int(r['new_min_mcap'])}: trades_added={added} winrate_1h={winrate:.1f}% rug60={rug60_rate:.1f}% rug90={rug90_rate:.1f}% avg_return_1h={_fmt_pct(r['avg_return_1h_pct'])}"
            )

        if simulate_skipped:
            exit_minutes = _parse_csv_ints(sim_exit_minutes_csv) or [1, 3, 5, 10, 30, 60]
            mcap_thresholds = _parse_csv_floats(sim_mcap_thresholds_csv) or [50_000, 30_000, 20_000, 10_000, 0]
            liq_thresholds = _parse_csv_floats(sim_liq_thresholds_csv) or [20_000, 10_000, 5_000, 1_000, 0]
            stop_losses = _parse_csv_floats(sim_stop_losses_csv)

            async with pool.acquire() as sim_conn:
                await sim_conn.execute(f"SET statement_timeout = '{int(statement_timeout_ms)}ms'")

                skipped_where = "WHERE s.detected_at >= $1"
                params: List[Any] = [since]
                if sim_wallet:
                    skipped_where += " AND s.trader_wallet = $2"
                    params.append(sim_wallet)

                skipped_rows = await _with_timeout(
                    sim_conn.fetch(
                        f"""
                        SELECT
                          s.correlation_id,
                          s.trader_wallet,
                          s.skip_reason,
                          s.skip_category,
                          COALESCE(ms.market_cap_usd, s.market_cap_usd) AS market_cap_usd,
                          COALESCE(ms.liquidity_usd, s.liquidity_usd) AS liquidity_usd,
                          s.volume_24h_usd,
                          s.price_change_1h_pct,
                          s.token_age_minutes,
                          ms.volume_5m_usd,
                          ms.txns_5m_buys,
                          ms.txns_5m_sells,
                          ms.price_change_m5_pct
                        FROM skipped_trades s
                        LEFT JOIN market_snapshots ms
                          ON ms.trade_id IS NULL
                          AND ms.correlation_id = s.correlation_id
                          AND ms.snapshot_type = 'skipped_detection'
                        {skipped_where}
                        """,
                        *params,
                    ),
                    query_timeout_s,
                    "sim_skipped_rows",
                )

                followup_rows = await _with_timeout(
                    sim_conn.fetch(
                        """
                        SELECT
                          correlation_id,
                          followup_minutes,
                          exit_price_usd,
                          price_usd
                        FROM post_trade_followups
                        WHERE trade_id IS NULL
                          AND exit_at >= $1
                          AND followup_minutes = ANY($2::int[])
                        """,
                        since,
                        exit_minutes,
                    ),
                    query_timeout_s,
                    "sim_followup_rows",
                )

            _simulate_skipped(
                skipped_rows=[dict(r) for r in skipped_rows],
                followup_rows=[dict(r) for r in followup_rows],
                exit_minutes=exit_minutes,
                mcap_thresholds=mcap_thresholds,
                liq_thresholds=liq_thresholds,
                max_price_change_1h=sim_max_price_change_1h,
                max_price_change_5m=sim_max_price_change_5m,
                min_volume_5m=sim_min_volume_5m,
                min_txns_5m=sim_min_txns_5m,
                min_buy_ratio_5m=sim_min_buy_ratio_5m,
                stop_losses=stop_losses,
                top_n=sim_top,
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
    p.add_argument("--simulate-skipped", action="store_true")
    p.add_argument("--sim-wallet", type=str, default=None)
    p.add_argument("--sim-exit-minutes", type=str, default="1,3,5,10,30,60")
    p.add_argument("--sim-mcap-thresholds", type=str, default="50000,30000,20000,10000,0")
    p.add_argument("--sim-liq-thresholds", type=str, default="20000,10000,5000,1000,0")
    p.add_argument("--sim-max-price-change-1h", type=float, default=None)
    p.add_argument("--sim-max-price-change-5m", type=float, default=None)
    p.add_argument("--sim-min-volume-5m", type=float, default=None)
    p.add_argument("--sim-min-txns-5m", type=int, default=None)
    p.add_argument("--sim-min-buy-ratio-5m", type=float, default=None)
    p.add_argument("--sim-stop-losses", type=str, default="")
    p.add_argument("--sim-top", type=int, default=10)
    args = p.parse_args()

    asyncio.run(
        run(
            hours=args.hours,
            days=args.days,
            statement_timeout_ms=args.statement_timeout_ms,
            connect_timeout_s=args.connect_timeout_s,
            query_timeout_s=args.query_timeout_s,
            simulate_skipped=args.simulate_skipped,
            sim_wallet=args.sim_wallet,
            sim_exit_minutes_csv=args.sim_exit_minutes,
            sim_mcap_thresholds_csv=args.sim_mcap_thresholds,
            sim_liq_thresholds_csv=args.sim_liq_thresholds,
            sim_max_price_change_1h=args.sim_max_price_change_1h,
            sim_max_price_change_5m=args.sim_max_price_change_5m,
            sim_min_volume_5m=args.sim_min_volume_5m,
            sim_min_txns_5m=args.sim_min_txns_5m,
            sim_min_buy_ratio_5m=args.sim_min_buy_ratio_5m,
            sim_stop_losses_csv=args.sim_stop_losses,
            sim_top=args.sim_top,
        )
    )


if __name__ == "__main__":
    main()
