"""
Chat History Analysis Tool - Query and analyze logged Telegram messages.

Usage:
    python -m src.analyze_chats --help
    python -m src.analyze_chats --start 2026-02-01 --end 2026-02-08 --search "217x"
    python -m src.analyze_chats --days 7 --stats
    python -m src.analyze_chats --today --chat "SOL SPACE100X"
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any
import re


def load_messages(
    base_path: str = "/data/chat_history",
    start_date: str = None,
    end_date: str = None,
    chat_name: str = None,
    has_address: bool = None,
    bot_action: str = None,
    limit: int = 10000
) -> List[Dict[str, Any]]:
    """Load messages from chat history files."""
    base = Path(base_path)
    results = []
    
    if not base.exists():
        print(f"No chat history found at {base_path}")
        return results
    
    files = sorted(base.glob("chat_*.jsonl"))
    
    for filepath in files:
        # Extract date from filename
        date_str = filepath.stem.replace("chat_", "").split("_")[0]
        
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    
                    # Apply filters
                    if chat_name and chat_name.lower() not in entry.get("chat_name", "").lower():
                        continue
                    if has_address is not None and entry.get("has_address") != has_address:
                        continue
                    if bot_action and entry.get("bot_action") != bot_action:
                        continue
                    
                    results.append(entry)
                    
                    if len(results) >= limit:
                        return results
                except (json.JSONDecodeError, KeyError):
                    continue
    
    return results


def search_performance_claims(messages: List[Dict], pattern: str = r"(\d+)x") -> List[Dict]:
    """Search for performance claims like "217x", "100x", etc."""
    results = []
    regex = re.compile(pattern, re.IGNORECASE)
    
    for msg in messages:
        text = msg.get("text", "")
        matches = regex.findall(text)
        if matches:
            # Extract the multiplier numbers
            multipliers = [int(m) for m in matches if m.isdigit()]
            if multipliers:
                msg["claimed_multiplier"] = max(multipliers)
                results.append(msg)
    
    return sorted(results, key=lambda x: x.get("claimed_multiplier", 0), reverse=True)


def print_stats(messages: List[Dict]) -> None:
    """Print statistics about the messages."""
    if not messages:
        print("No messages found.")
        return
    
    total = len(messages)
    with_address = sum(1 for m in messages if m.get("has_address"))
    fresh = sum(1 for m in messages if m.get("classification") == "fresh")
    old = sum(1 for m in messages if m.get("classification") == "old")
    high_confidence = sum(1 for m in messages if m.get("confidence") == "high")
    
    # Bot actions
    bought = sum(1 for m in messages if m.get("bot_action") == "bought")
    skipped = sum(1 for m in messages if "skipped" in str(m.get("bot_action", "")))
    
    # Chat breakdown
    chats = {}
    for m in messages:
        name = m.get("chat_name", "unknown")
        chats[name] = chats.get(name, 0) + 1
    
    print(f"\n{'='*60}")
    print(f"CHAT HISTORY STATISTICS")
    print(f"{'='*60}")
    print(f"Total messages: {total}")
    print(f"With token addresses: {with_address} ({100*with_address/total:.1f}%)")
    print(f"Classified as 'fresh': {fresh}")
    print(f"Classified as 'old': {old}")
    print(f"High confidence signals: {high_confidence}")
    print(f"Bot bought: {bought}")
    print(f"Bot skipped: {skipped}")
    print(f"\nTop chats by message count:")
    for name, count in sorted(chats.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {name}: {count}")


def print_messages(messages: List[Dict], limit: int = 20) -> None:
    """Print messages in readable format."""
    if not messages:
        print("No messages found.")
        return
    
    print(f"\n{'='*60}")
    print(f"MESSAGES (showing {min(limit, len(messages))} of {len(messages)})")
    print(f"{'='*60}")
    
    for i, msg in enumerate(messages[:limit], 1):
        ts = msg.get("timestamp", "unknown")[:16]  # Truncate to YYYY-MM-DD HH:MM
        chat = msg.get("chat_name", "unknown")
        classification = msg.get("classification", "unknown")
        confidence = msg.get("confidence", "unknown")
        action = msg.get("bot_action", "none")
        token = msg.get("token_address", "")
        
        print(f"\n{i}. [{ts}] {chat}")
        print(f"   Classification: {classification} | Confidence: {confidence} | Action: {action}")
        if token:
            print(f"   Token: {token[:20]}...")
        text = msg.get("text", "")[:100]
        if len(msg.get("text", "")) > 100:
            text += "..."
        print(f"   Text: {text}")


def main():
    parser = argparse.ArgumentParser(description="Analyze Telegram chat history")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--today", action="store_true", help="Show only today")
    parser.add_argument("--days", type=int, help="Show last N days")
    parser.add_argument("--chat", help="Filter by chat name (partial match)")
    parser.add_argument("--search", help="Search text for keyword/regex")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--with-address", action="store_true", help="Only messages with addresses")
    parser.add_argument("--limit", type=int, default=50, help="Limit results")
    parser.add_argument("--path", default="/data/chat_history", help="Path to chat history")
    
    args = parser.parse_args()
    
    # Determine date range
    if args.today:
        start = end = datetime.utcnow().strftime("%Y-%m-%d")
    elif args.days:
        end = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    else:
        start = args.start
        end = args.end
    
    # Load messages
    print(f"Loading messages from {args.path}...")
    messages = load_messages(
        base_path=args.path,
        start_date=start,
        end_date=end,
        chat_name=args.chat,
        has_address=args.with_address,
        limit=args.limit * 10 if args.search else args.limit  # Load more if searching
    )
    
    if not messages:
        print("No messages found matching criteria.")
        return
    
    print(f"Found {len(messages)} messages")
    
    # Apply text search if specified
    if args.search:
        pattern = args.search.lower()
        messages = [m for m in messages if pattern in m.get("text", "").lower()]
        print(f"After search: {len(messages)} messages")
    
    # Show stats
    if args.stats:
        print_stats(messages)
    
    # Show messages
    print_messages(messages, args.limit)
    
    # Search for performance claims if --search not specified
    if not args.search and not args.stats:
        print(f"\n{'='*60}")
        print("PERFORMANCE CLAIMS (100x, 200x, etc.)")
        print(f"{'='*60}")
        claims = search_performance_claims(messages)
        if claims:
            for msg in claims[:10]:
                mult = msg.get("claimed_multiplier", 0)
                chat = msg.get("chat_name", "unknown")
                text = msg.get("text", "")[:80]
                print(f"  {mult}x claim in {chat}: {text}...")
        else:
            print("  No performance claims found.")


if __name__ == "__main__":
    main()
