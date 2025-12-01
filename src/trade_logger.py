"""
Trade Logger - Records all trades for analysis and debugging.
Saves trade history to JSON for later comparison with tracked wallets.
"""

import json
import os
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from pathlib import Path
import structlog

logger = structlog.get_logger(__name__)

# Trade history file
TRADE_HISTORY_FILE = "/windbreaker/trade_history.json"


@dataclass
class TradeRecord:
    """Single trade record for history."""
    timestamp: str
    trade_type: str  # "buy" or "sell"
    token_mint: str
    token_symbol: Optional[str]
    
    # Our trade details
    our_sol_amount: float
    our_token_amount: float
    our_signature: Optional[str]
    
    # Copied trader details
    copied_wallet: str
    their_sol_amount: float
    their_signature: str
    their_timestamp: Optional[str]
    
    # Timing
    delay_seconds: float  # How long after their trade we executed
    
    # Result
    success: bool
    error: Optional[str]
    
    # For sells - P&L
    entry_sol: Optional[float] = None
    exit_sol: Optional[float] = None
    pnl_sol: Optional[float] = None
    pnl_percent: Optional[float] = None
    exit_reason: Optional[str] = None  # "copied_sell", "abandoned", etc.


class TradeLogger:
    """Logs all trades for analysis."""
    
    def __init__(self, history_file: str = TRADE_HISTORY_FILE):
        self.history_file = Path(history_file)
        self.trades: List[TradeRecord] = []
        self._load_history()
    
    def _load_history(self) -> None:
        """Load existing trade history."""
        if self.history_file.exists():
            try:
                with open(self.history_file, 'r') as f:
                    data = json.load(f)
                    # Don't load into memory - just append new trades
                    logger.info("trade_history_loaded", count=len(data))
            except Exception as e:
                logger.warning("history_load_failed", error=str(e))
    
    def _save_trade(self, trade: TradeRecord) -> None:
        """Append trade to history file."""
        try:
            # Load existing
            existing = []
            if self.history_file.exists():
                with open(self.history_file, 'r') as f:
                    existing = json.load(f)
            
            # Append new
            existing.append(asdict(trade))
            
            # Save
            with open(self.history_file, 'w') as f:
                json.dump(existing, f, indent=2)
            
            logger.debug("trade_saved", token=trade.token_mint[:8])
        except Exception as e:
            logger.error("trade_save_failed", error=str(e))
    
    def log_buy(
        self,
        token_mint: str,
        token_symbol: Optional[str],
        our_sol: float,
        our_tokens: float,
        our_signature: Optional[str],
        copied_wallet: str,
        their_sol: float,
        their_signature: str,
        their_timestamp: Optional[str],
        delay_seconds: float,
        success: bool,
        error: Optional[str] = None
    ) -> None:
        """Log a buy trade."""
        trade = TradeRecord(
            timestamp=datetime.utcnow().isoformat(),
            trade_type="buy",
            token_mint=token_mint,
            token_symbol=token_symbol,
            our_sol_amount=our_sol,
            our_token_amount=our_tokens,
            our_signature=our_signature,
            copied_wallet=copied_wallet,
            their_sol_amount=their_sol,
            their_signature=their_signature,
            their_timestamp=their_timestamp,
            delay_seconds=delay_seconds,
            success=success,
            error=error
        )
        
        self._save_trade(trade)
        
        logger.info(
            "trade_logged",
            type="buy",
            token=token_mint[:8],
            our_sol=f"{our_sol:.4f}",
            delay=f"{delay_seconds:.1f}s",
            success=success
        )
    
    def log_sell(
        self,
        token_mint: str,
        token_symbol: Optional[str],
        our_sol_received: float,
        our_tokens_sold: float,
        our_signature: Optional[str],
        copied_wallet: str,
        their_sol: float,
        their_signature: str,
        delay_seconds: float,
        entry_sol: float,
        exit_reason: str,
        success: bool,
        error: Optional[str] = None
    ) -> None:
        """Log a sell trade with P&L."""
        pnl_sol = our_sol_received - entry_sol if success else 0
        pnl_percent = ((our_sol_received / entry_sol) - 1) * 100 if success and entry_sol > 0 else 0
        
        trade = TradeRecord(
            timestamp=datetime.utcnow().isoformat(),
            trade_type="sell",
            token_mint=token_mint,
            token_symbol=token_symbol,
            our_sol_amount=our_sol_received,
            our_token_amount=our_tokens_sold,
            our_signature=our_signature,
            copied_wallet=copied_wallet,
            their_sol_amount=their_sol,
            their_signature=their_signature,
            their_timestamp=None,
            delay_seconds=delay_seconds,
            success=success,
            error=error,
            entry_sol=entry_sol,
            exit_sol=our_sol_received,
            pnl_sol=pnl_sol,
            pnl_percent=pnl_percent,
            exit_reason=exit_reason
        )
        
        self._save_trade(trade)
        
        logger.info(
            "trade_logged",
            type="sell",
            token=token_mint[:8],
            entry=f"{entry_sol:.4f}",
            exit=f"{our_sol_received:.4f}",
            pnl=f"{pnl_sol:+.4f} SOL ({pnl_percent:+.1f}%)",
            reason=exit_reason,
            success=success
        )
    
    def log_abandon(
        self,
        token_mint: str,
        token_symbol: Optional[str],
        entry_sol: float,
        final_value: float,
        copied_wallet: str
    ) -> None:
        """Log an abandoned (rugged) position."""
        trade = TradeRecord(
            timestamp=datetime.utcnow().isoformat(),
            trade_type="abandon",
            token_mint=token_mint,
            token_symbol=token_symbol,
            our_sol_amount=0,
            our_token_amount=0,
            our_signature=None,
            copied_wallet=copied_wallet,
            their_sol_amount=0,
            their_signature="",
            their_timestamp=None,
            delay_seconds=0,
            success=True,
            error=None,
            entry_sol=entry_sol,
            exit_sol=final_value,
            pnl_sol=-entry_sol,  # Total loss
            pnl_percent=-100,
            exit_reason="abandoned_rug"
        )
        
        self._save_trade(trade)
        
        logger.info(
            "trade_logged",
            type="abandon",
            token=token_mint[:8],
            lost=f"{entry_sol:.4f} SOL",
            final_value=f"{final_value:.6f} SOL"
        )
    
    def get_summary(self) -> Dict[str, Any]:
        """Get trading summary statistics."""
        try:
            if not self.history_file.exists():
                return {"total_trades": 0}
            
            with open(self.history_file, 'r') as f:
                trades = json.load(f)
            
            buys = [t for t in trades if t.get("trade_type") == "buy" and t.get("success")]
            sells = [t for t in trades if t.get("trade_type") == "sell" and t.get("success")]
            abandons = [t for t in trades if t.get("trade_type") == "abandon"]
            
            total_invested = sum(t.get("our_sol_amount", 0) for t in buys)
            total_returned = sum(t.get("our_sol_amount", 0) for t in sells)
            total_lost_to_rugs = sum(t.get("entry_sol", 0) for t in abandons)
            
            total_pnl = sum(t.get("pnl_sol", 0) for t in sells)
            
            avg_delay = sum(t.get("delay_seconds", 0) for t in buys) / len(buys) if buys else 0
            
            winning_sells = [t for t in sells if t.get("pnl_sol", 0) > 0]
            losing_sells = [t for t in sells if t.get("pnl_sol", 0) < 0]
            
            return {
                "total_trades": len(trades),
                "buys": len(buys),
                "sells": len(sells),
                "abandons": len(abandons),
                "total_invested_sol": total_invested,
                "total_returned_sol": total_returned,
                "total_lost_to_rugs_sol": total_lost_to_rugs,
                "realized_pnl_sol": total_pnl,
                "net_pnl_sol": total_pnl - total_lost_to_rugs,
                "win_rate": len(winning_sells) / len(sells) * 100 if sells else 0,
                "avg_delay_seconds": avg_delay,
                "winning_trades": len(winning_sells),
                "losing_trades": len(losing_sells)
            }
        except Exception as e:
            logger.error("summary_failed", error=str(e))
            return {"error": str(e)}


# Global instance
trade_logger = TradeLogger()
