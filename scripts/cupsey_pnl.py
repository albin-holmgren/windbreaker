import asyncio
import json
import logging
import structlog
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import aiohttp


# Silence structlog/debug spam from tx_parser when running analytics
logging.basicConfig(level=logging.WARNING)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

CUPSEY_WALLET = "2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f"

MAX_SIGNATURES = 1200
CONCURRENCY = 15


@dataclass
class Filters:
    min_market_cap_usd: float = 30000.0
    min_liquidity_usd: float = 5000.0
    min_volume_24h_usd: float = 10000.0
    min_txns_1h: int = 50
    min_token_age_minutes: float = 0.0


FILTERS = Filters()


def simulate_with_filters(
    *,
    by_buy: dict[str, dict[str, Any]],
    detected_by_sig: dict[str, dict[str, Any]],
    detected_by_mint: dict[str, dict[str, Any]],
    filters: Filters,
    our_entry_sol: float = 0.03,
) -> dict[str, Any]:
    def _passes(det: Optional[dict[str, Any]]) -> tuple[bool, str]:
        if not det:
            return False, "no_detection_market_data"

        mcap = float(det.get("market_cap_usd") or 0)
        liq = float(det.get("liquidity_usd") or 0)
        vol = float(det.get("volume_24h_usd") or 0)
        txns = int(det.get("txns_1h") or 0)
        age = float(det.get("age_minutes") or 0)

        if mcap < filters.min_market_cap_usd:
            return False, "low_mcap"
        if liq < filters.min_liquidity_usd:
            return False, "low_liquidity"
        if vol < filters.min_volume_24h_usd:
            return False, "low_volume"
        if txns < filters.min_txns_1h:
            return False, "low_txns_1h"
        if age < filters.min_token_age_minutes:
            return False, "too_new"
        return True, "pass"

    passed_rows: list[dict[str, Any]] = []
    failed_reasons = defaultdict(int)
    missing_market_data = 0
    our_pnl_est = 0.0

    for buy_sig, agg in by_buy.items():
        # Try to find market data by signature first, then by token_mint
        det = detected_by_sig.get(buy_sig)
        if det is None and agg["mint"]:
            det = detected_by_mint.get(agg["mint"])
        
        ok, reason = _passes(det)
        if det is None:
            missing_market_data += 1
        if not ok:
            failed_reasons[reason] += 1
            continue

        cost = float(agg["cost"])
        pnl = float(agg["pnl"])
        passed_rows.append({"buy_sig": buy_sig, "mint": agg["mint"], "buy_time": agg["buy_time"], "cost": cost, "pnl": pnl, "mcap": det.get("market_cap_usd", 0)})
        if cost > 0:
            our_pnl_est += our_entry_sol * (pnl / cost)

    passed_cost = sum(x["cost"] for x in passed_rows)
    passed_pnl = sum(x["pnl"] for x in passed_rows)
    passed_pct = (passed_pnl / passed_cost * 100.0) if passed_cost > 0 else 0.0

    return {
        "passed": passed_rows,
        "failed_reasons": dict(failed_reasons),
        "missing_market_data": missing_market_data,
        "pass_trades": len(passed_rows),
        "pass_cost": passed_cost,
        "pass_pnl": passed_pnl,
        "pass_pct": passed_pct,
        "our_entry_sol": our_entry_sol,
        "our_pnl_est": our_pnl_est,
    }


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def ts(block_time: Optional[int]) -> str:
    if not block_time:
        return "n/a"
    return datetime.utcfromtimestamp(int(block_time)).strftime("%Y-%m-%d %H:%M:%S")


async def rpc_call(session: aiohttp.ClientSession, rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(
        rpc_url,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
        headers={"Content-Type": "application/json"},
    ) as resp:
        data = await resp.json()
        if "error" in data:
            return None
        return data.get("result")


async def fetch_signatures(session: aiohttp.ClientSession, rpc_url: str, address: str, limit_total: int) -> list[str]:
    signatures: list[str] = []
    before: Optional[str] = None

    while len(signatures) < limit_total:
        batch = min(200, limit_total - len(signatures))
        opts: dict[str, Any] = {"limit": batch}
        if before:
            opts["before"] = before

        res = await rpc_call(session, rpc_url, "getSignaturesForAddress", [address, opts])
        if not res:
            break

        for r in res:
            if r.get("err") is None:
                signatures.append(r["signature"])

        before = res[-1].get("signature")
        if len(res) < batch:
            break

    return signatures


async def fetch_transaction(session: aiohttp.ClientSession, rpc_url: str, signature: str) -> Optional[dict[str, Any]]:
    return await rpc_call(
        session,
        rpc_url,
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )


def load_detected_trades(path: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load detected trades, return (by_signature, by_token_mint) dicts."""
    try:
        with open(path, "r") as f:
            rows = json.load(f)
    except Exception:
        return {}, {}

    by_sig: dict[str, dict[str, Any]] = {}
    by_mint: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("wallet_name") != "cupsey":
            continue
        if r.get("trade_type") != "buy":
            continue
        sig = r.get("their_signature")
        mint = r.get("token_mint")
        if sig:
            by_sig[sig] = r
        # Keep first detection per mint (earliest market data)
        if mint and mint not in by_mint:
            by_mint[mint] = r
    return by_sig, by_mint


def passes_filters(det: Optional[dict[str, Any]]) -> tuple[bool, str]:
    if not det:
        return False, "no_detection_market_data"

    mcap = float(det.get("market_cap_usd") or 0)
    liq = float(det.get("liquidity_usd") or 0)
    vol = float(det.get("volume_24h_usd") or 0)
    txns = int(det.get("txns_1h") or 0)
    age = float(det.get("age_minutes") or 0)

    if mcap < FILTERS.min_market_cap_usd:
        return False, f"low_mcap ({mcap:.0f} < {FILTERS.min_market_cap_usd:.0f})"
    if liq < FILTERS.min_liquidity_usd:
        return False, f"low_liquidity ({liq:.0f} < {FILTERS.min_liquidity_usd:.0f})"
    if vol < FILTERS.min_volume_24h_usd:
        return False, f"low_volume ({vol:.0f} < {FILTERS.min_volume_24h_usd:.0f})"
    if txns < FILTERS.min_txns_1h:
        return False, f"low_txns_1h ({txns} < {FILTERS.min_txns_1h})"
    if age < FILTERS.min_token_age_minutes:
        return False, f"too_new ({age:.1f}m < {FILTERS.min_token_age_minutes:.1f}m)"

    return True, "pass"


async def main() -> None:
    from src.tx_parser import TransactionParser

    env = load_env("/root/windbreaker/.env")
    rpc_url = env.get("RPC_URL")
    if not rpc_url:
        raise SystemExit("Missing RPC_URL in /root/windbreaker/.env")

    detected_by_sig, detected_by_mint = load_detected_trades("/root/windbreaker/detected_trades.json")
    parser = TransactionParser(min_sol_value=0.00001)

    async with aiohttp.ClientSession() as session:
        sigs = await fetch_signatures(session, rpc_url, CUPSEY_WALLET, MAX_SIGNATURES)

        sem = asyncio.Semaphore(CONCURRENCY)
        txs: dict[str, dict[str, Any]] = {}

        async def worker(sig: str) -> None:
            async with sem:
                tx = await fetch_transaction(session, rpc_url, sig)
                if tx:
                    txs[sig] = tx

        await asyncio.gather(*[worker(s) for s in sigs])

    swaps: list[dict[str, Any]] = []
    for sig, tx in txs.items():
        swap = parser.parse_transaction(tx, CUPSEY_WALLET)
        if not swap:
            continue
        swaps.append(
            {
                "sig": sig,
                "time": tx.get("blockTime"),
                "type": swap.swap_type.value,
                "mint": swap.token_mint,
                "token_amount": int(swap.token_amount or 0),
                "sol": float(swap.sol_value),
                "dex": swap.dex,
            }
        )

    swaps.sort(key=lambda x: ((x["time"] or 0), x["sig"]))

    lots: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    realized: list[dict[str, Any]] = []

    for s in swaps:
        if s["type"] == "buy":
            if s["token_amount"] <= 0 or s["sol"] <= 0:
                continue
            lots[s["mint"]].append(
                {
                    "tokens": s["token_amount"],
                    "cost_sol": s["sol"],
                    "buy_sig": s["sig"],
                    "buy_time": s["time"],
                }
            )
            continue

        if s["type"] != "sell":
            continue
        if s["token_amount"] <= 0 or s["sol"] <= 0:
            continue
        if not lots[s["mint"]]:
            continue

        remaining = s["token_amount"]
        sell_tokens_total = remaining
        sell_sol_total = s["sol"]

        while remaining > 0 and lots[s["mint"]]:
            lot = lots[s["mint"]][0]
            take = min(remaining, lot["tokens"])

            cost = lot["cost_sol"] * (take / lot["tokens"])
            proceeds = sell_sol_total * (take / sell_tokens_total)
            pnl = proceeds - cost

            realized.append(
                {
                    "mint": s["mint"],
                    "buy_sig": lot["buy_sig"],
                    "buy_time": lot["buy_time"],
                    "sell_sig": s["sig"],
                    "sell_time": s["time"],
                    "cost_sol": cost,
                    "proceeds_sol": proceeds,
                    "pnl_sol": pnl,
                }
            )

            lot["tokens"] -= take
            lot["cost_sol"] -= cost
            remaining -= take

            if lot["tokens"] <= 0:
                lots[s["mint"]].popleft()

    by_buy: dict[str, dict[str, Any]] = defaultdict(lambda: {"mint": None, "buy_time": None, "cost": 0.0, "pnl": 0.0})
    for r in realized:
        b = by_buy[r["buy_sig"]]
        b["mint"] = r["mint"]
        b["buy_time"] = r["buy_time"]
        b["cost"] += r["cost_sol"]
        b["pnl"] += r["pnl_sol"]

    total_cost = sum(v["cost"] for v in by_buy.values())
    total_pnl = sum(v["pnl"] for v in by_buy.values())
    total_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0

    baseline = simulate_with_filters(by_buy=by_buy, detected_by_sig=detected_by_sig, detected_by_mint=detected_by_mint, filters=FILTERS, our_entry_sol=0.03)

    print("=== CUPSEY REALIZED PnL (on-chain swaps parsed) ===")
    print(f"signatures_limit={MAX_SIGNATURES} swaps_parsed={len(swaps)} realized_trades={len(by_buy)}")
    print(f"total_realized_pnl_sol={total_pnl:+.4f} on_cost_sol={total_cost:.4f} pnl_pct={total_pct:+.2f}%")
    print("")

    print("=== FILTER SIMULATION (requires detected_trades.json market data) ===")
    print(
        "filters="
        + f"mcap>={FILTERS.min_market_cap_usd:.0f} liq>={FILTERS.min_liquidity_usd:.0f} vol24h>={FILTERS.min_volume_24h_usd:.0f} "
        + f"txns1h>={FILTERS.min_txns_1h} age>={FILTERS.min_token_age_minutes:.1f}m"
    )
    print(
        f"pass_trades={baseline['pass_trades']} fail_trades={len(by_buy)-baseline['pass_trades']} "
        f"missing_market_data={baseline['missing_market_data']}"
    )
    print(
        f"pass_realized_pnl_sol={baseline['pass_pnl']:+.4f} on_cost_sol={baseline['pass_cost']:.4f} pnl_pct={baseline['pass_pct']:+.2f}%"
    )
    if baseline["failed_reasons"]:
        top_reasons = sorted(baseline["failed_reasons"].items(), key=lambda kv: kv[1], reverse=True)[:8]
        print("fail_reasons_top=" + ",".join([f"{k}:{v}" for k, v in top_reasons]))
    print("")

    print("=== OUR PnL ESTIMATE (simple) ===")
    print(
        f"assume_entry_per_trade_sol={baseline['our_entry_sol']:.2f} "
        f"estimated_realized_pnl_sol={baseline['our_pnl_est']:+.4f}"
    )
    print("")

    # What-if table to calibrate volume/txns/liquidity thresholds
    scenarios = [
        ("mcap_only", Filters(min_market_cap_usd=30000, min_liquidity_usd=0, min_volume_24h_usd=0, min_txns_1h=0, min_token_age_minutes=0)),
        ("no_mcap_vol10k_txns50", Filters(min_market_cap_usd=0, min_liquidity_usd=0, min_volume_24h_usd=10000, min_txns_1h=50, min_token_age_minutes=0)),
        ("no_mcap_liq5k", Filters(min_market_cap_usd=0, min_liquidity_usd=5000, min_volume_24h_usd=0, min_txns_1h=0, min_token_age_minutes=0)),
        ("no_mcap_liq5k_vol10k", Filters(min_market_cap_usd=0, min_liquidity_usd=5000, min_volume_24h_usd=10000, min_txns_1h=0, min_token_age_minutes=0)),
        ("no_mcap_liq5k_vol10k_txns50", Filters(min_market_cap_usd=0, min_liquidity_usd=5000, min_volume_24h_usd=10000, min_txns_1h=50, min_token_age_minutes=0)),
        ("mcap+txns20", Filters(min_market_cap_usd=30000, min_liquidity_usd=0, min_volume_24h_usd=0, min_txns_1h=20, min_token_age_minutes=0)),
        ("mcap+liq5k", Filters(min_market_cap_usd=30000, min_liquidity_usd=5000, min_volume_24h_usd=0, min_txns_1h=0, min_token_age_minutes=0)),
        ("mcap+vol2k", Filters(min_market_cap_usd=30000, min_liquidity_usd=0, min_volume_24h_usd=2000, min_txns_1h=0, min_token_age_minutes=0)),
        ("mcap+vol10k", Filters(min_market_cap_usd=30000, min_liquidity_usd=0, min_volume_24h_usd=10000, min_txns_1h=0, min_token_age_minutes=0)),
        ("mcap+vol10k+txns50", Filters(min_market_cap_usd=30000, min_liquidity_usd=0, min_volume_24h_usd=10000, min_txns_1h=50, min_token_age_minutes=0)),
        ("mcap+liq5k+vol10k+txns50", FILTERS),
    ]

    print("=== WHAT-IF (PnL if we only take trades passing filters) ===")
    for name, fset in scenarios:
        sim = simulate_with_filters(by_buy=by_buy, detected_by_sig=detected_by_sig, detected_by_mint=detected_by_mint, filters=fset, our_entry_sol=0.03)
        print(
            f"{name}: pass={sim['pass_trades']} pnl={sim['pass_pnl']:+.4f}SOL on_cost={sim['pass_cost']:.4f} ({sim['pass_pct']:+.2f}%) "
            f"our_est={sim['our_pnl_est']:+.4f}SOL missing={sim['missing_market_data']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
