import json
import os
import argparse
from collections import defaultdict

# Define market cap buckets (in USD)
MARKET_CAP_BUCKETS = [
    (0, 10000, 'Micro (<$10K)'),
    (10000, 50000, 'Small ($10K-$50K)'),
    (50000, 100000, 'Medium ($50K-$100K)'),
    (100000, 500000, 'Large ($100K-$500K)'),
    (500000, float('inf'), 'Huge (>$500K)')
]

def parse_log_file(log_file_path):
    trades = []
    with open(log_file_path, 'r') as file:
        raw = file.read().strip()

    if not raw:
        return trades

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return trades

def extract_market_cap(log_entry):
    market_cap_usd = log_entry.get('market_cap_usd')
    if market_cap_usd is not None:
        try:
            return float(market_cap_usd)
        except Exception:
            return 0.0

    if 'mcap' in log_entry:
        mcap_str = log_entry.get('mcap')
        if isinstance(mcap_str, (int, float)):
            return float(mcap_str)
        if isinstance(mcap_str, str):
            try:
                return float(mcap_str.replace('$', '').replace(',', ''))
            except Exception:
                return 0.0

    if 'market_cap' in log_entry:
        market_cap = log_entry.get('market_cap')
        if isinstance(market_cap, (int, float)):
            return float(market_cap)
        if isinstance(market_cap, str):
            try:
                return float(market_cap.replace('$', '').replace(',', ''))
            except Exception:
                return 0.0

    return 0.0

def categorize_market_cap(market_cap):
    """Categorize market cap into predefined buckets."""
    for lower, upper, label in MARKET_CAP_BUCKETS:
        if lower <= market_cap < upper:
            return label
    return 'Unknown'

def compute_profit_by_bucket(log_file_path, copied_only=True):
    trades = parse_log_file(log_file_path)
    profit_by_bucket = defaultdict(float)
    trade_count_by_bucket = defaultdict(int)
    cupsey_profit_by_bucket = defaultdict(float)
    cupsey_trade_count_by_bucket = defaultdict(int)

    for trade in trades:
        market_cap = extract_market_cap(trade)
        bucket = categorize_market_cap(market_cap)
        sol_amount = trade.get('their_sol')
        if sol_amount is None:
            sol_amount = trade.get('sol')
        if sol_amount is None:
            sol_amount = trade.get('our_sol')
        try:
            sol_amount = float(sol_amount or 0.0)
        except Exception:
            sol_amount = 0.0

        trade_type = (trade.get('trade_type') or trade.get('type') or '').lower()
        is_copied = trade.get('copied', False)

        # Track Cupsey's trades (all detected trades)
        if trade_type == 'buy':
            cupsey_profit_by_bucket[bucket] -= sol_amount
        elif trade_type == 'sell':
            cupsey_profit_by_bucket[bucket] += sol_amount
        cupsey_trade_count_by_bucket[bucket] += 1

        # Track bot's copied trades only if copied_only is True
        if copied_only and not is_copied:
            continue

        if trade_type == 'buy':
            profit_by_bucket[bucket] -= sol_amount
        elif trade_type == 'sell':
            profit_by_bucket[bucket] += sol_amount

        trade_count_by_bucket[bucket] += 1

    return profit_by_bucket, trade_count_by_bucket, cupsey_profit_by_bucket, cupsey_trade_count_by_bucket

def print_profit_analysis(log_file_path, copied_only=True):
    profit_by_bucket, trade_count_by_bucket, cupsey_profit_by_bucket, cupsey_trade_count_by_bucket = compute_profit_by_bucket(log_file_path, copied_only)
    if copied_only:
        print(f"Bot's Realized Profit Analysis (Copied Trades Only) for {log_file_path}:")
    else:
        print(f"Profit Analysis (All Trades) for {log_file_path}:")
    print("=" * 60)
    for bucket in [b[2] for b in MARKET_CAP_BUCKETS]:
        profit = profit_by_bucket[bucket]
        count = trade_count_by_bucket[bucket]
        print(f"{bucket}: {profit:.4f} SOL over {count} trades")
    print("=" * 60)

    print(f"Cupsey's Detected Trade Flow for {log_file_path}:")
    print("=" * 60)
    for bucket in [b[2] for b in MARKET_CAP_BUCKETS]:
        profit = cupsey_profit_by_bucket[bucket]
        count = cupsey_trade_count_by_bucket[bucket]
        print(f"{bucket}: {profit:.4f} SOL over {count} trades")
    print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "detected_trades.json"),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all trades, not just copied ones"
    )
    args = parser.parse_args()

    if os.path.exists(args.file):
        print_profit_analysis(args.file, copied_only=not args.all)
    else:
        print(f"Log file {args.file} does not exist.")
