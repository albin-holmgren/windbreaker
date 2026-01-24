"""
Detection Logger - Logs ALL detected trades for filter analysis.
Captures both copied and skipped trades with full market data.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
import structlog

logger = structlog.get_logger(__name__)

DETECTION_LOG_FILE = Path("detected_trades.json")


@dataclass
class DetectedTrade:
    """A detected trade from a tracked wallet."""
    timestamp: str
    wallet: str
    wallet_name: str  # e.g., "cupsey", "cented"
    trade_type: str  # "buy" or "sell"
    token_mint: str
    token_symbol: Optional[str]
    dex: str
    their_sol: float
    their_signature: str
    
    # Market data at detection time
    market_cap_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    age_minutes: float
    price_change_1h: float
    txns_1h: int
    
    # Copy decision
    copied: bool
    skip_reason: Optional[str]  # None if copied, reason if skipped
    our_sol: Optional[float]  # How much we used (if copied)
    our_signature: Optional[str]  # Our tx signature (if copied)
    
    # For later analysis - track outcome
    price_at_detection: Optional[float] = None
    price_1min_later: Optional[float] = None
    price_5min_later: Optional[float] = None
    trader_sold_at: Optional[str] = None
    trader_sold_sol: Optional[float] = None


class DetectionLogger:
    """Logs all detected trades for filter analysis."""
    
    def __init__(self, log_file: Path = DETECTION_LOG_FILE):
        self.log_file = log_file
        self._ensure_file_exists()
    
    def _ensure_file_exists(self):
        """Create log file if it doesn't exist."""
        if not self.log_file.exists():
            with open(self.log_file, 'w') as f:
                json.dump([], f)
    
    def _load_trades(self) -> list:
        """Load existing trades from file."""
        try:
            with open(self.log_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []
    
    def _save_trades(self, trades: list):
        """Save trades to file."""
        with open(self.log_file, 'w') as f:
            json.dump(trades, f, indent=2)
    
    def log_detection(
        self,
        wallet: str,
        trade_type: str,
        token_mint: str,
        token_symbol: Optional[str],
        dex: str,
        their_sol: float,
        their_signature: str,
        market_cap_usd: float,
        liquidity_usd: float,
        volume_24h_usd: float,
        age_minutes: float,
        price_change_1h: float,
        txns_1h: int,
        copied: bool,
        skip_reason: Optional[str] = None,
        our_sol: Optional[float] = None,
        our_signature: Optional[str] = None
    ):
        """Log a detected trade."""
        # Map wallet to name
        wallet_names = {
            '2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f': 'cupsey',
            'CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o': 'cented',
        }
        wallet_name = wallet_names.get(wallet, wallet[:8])
        
        trade = DetectedTrade(
            timestamp=datetime.utcnow().isoformat(),
            wallet=wallet,
            wallet_name=wallet_name,
            trade_type=trade_type,
            token_mint=token_mint,
            token_symbol=token_symbol,
            dex=dex,
            their_sol=their_sol,
            their_signature=their_signature,
            market_cap_usd=market_cap_usd,
            liquidity_usd=liquidity_usd,
            volume_24h_usd=volume_24h_usd,
            age_minutes=age_minutes,
            price_change_1h=price_change_1h,
            txns_1h=txns_1h,
            copied=copied,
            skip_reason=skip_reason,
            our_sol=our_sol,
            our_signature=our_signature
        )
        
        try:
            trades = self._load_trades()
            trades.append(asdict(trade))
            self._save_trades(trades)
            
            logger.info(
                "trade_detected_logged",
                wallet=wallet_name,
                type=trade_type,
                token=token_mint[:8],
                mcap=f"${market_cap_usd:,.0f}",
                copied=copied,
                skip_reason=skip_reason
            )
        except Exception as e:
            logger.warning("detection_log_error", error=str(e))
    
    def log_trader_sell(self, token_mint: str, sold_at: str, sold_sol: float):
        """Update a trade record when trader sells."""
        try:
            trades = self._load_trades()
            # Find the most recent buy for this token
            for trade in reversed(trades):
                if trade['token_mint'] == token_mint and trade['trade_type'] == 'buy':
                    trade['trader_sold_at'] = sold_at
                    trade['trader_sold_sol'] = sold_sol
                    break
            self._save_trades(trades)
        except Exception as e:
            logger.warning("detection_log_update_error", error=str(e))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get summary statistics for analysis."""
        trades = self._load_trades()
        
        if not trades:
            return {"total": 0}
        
        buys = [t for t in trades if t['trade_type'] == 'buy']
        copied = [t for t in buys if t['copied']]
        skipped = [t for t in buys if not t['copied']]
        
        # Group skips by reason
        skip_reasons = {}
        for t in skipped:
            reason = t.get('skip_reason', 'unknown')
            # Extract main reason type
            if 'market_cap' in reason.lower():
                key = 'low_mcap'
            elif 'liquidity' in reason.lower():
                key = 'low_liquidity'
            elif 'age' in reason.lower() or 'new' in reason.lower():
                key = 'too_new'
            elif 'volume' in reason.lower():
                key = 'low_volume'
            elif 'recently_copied' in reason.lower():
                key = 'cooldown'
            else:
                key = reason[:30] if reason else 'unknown'
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
        
        # Market cap distribution of skipped trades
        skipped_mcaps = [t['market_cap_usd'] for t in skipped]
        
        return {
            "total_detections": len(trades),
            "total_buys": len(buys),
            "copied": len(copied),
            "skipped": len(skipped),
            "copy_rate": f"{len(copied)/len(buys)*100:.1f}%" if buys else "0%",
            "skip_reasons": skip_reasons,
            "skipped_mcap_range": {
                "min": min(skipped_mcaps) if skipped_mcaps else 0,
                "max": max(skipped_mcaps) if skipped_mcaps else 0,
                "avg": sum(skipped_mcaps)/len(skipped_mcaps) if skipped_mcaps else 0
            },
            "by_wallet": {
                wallet: len([t for t in buys if t.get('wallet_name') == wallet])
                for wallet in set(t.get('wallet_name', 'unknown') for t in buys)
            }
        }


# Global instance
detection_logger = DetectionLogger()
