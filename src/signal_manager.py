"""
Fast Signal Manager - Manages trading signals with deduplication.
In-memory queue for speed, no persistence needed.
"""

import asyncio
from typing import Dict, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class TradingSignal:
    """A trading signal from Telegram."""
    token_address: str
    source_chat: str
    original_message: str
    timestamp: datetime = field(default_factory=lambda: datetime.utcnow())
    processed: bool = False
    executed: bool = False


class FastSignalManager:
    """Manage trading signals with deduplication."""
    
    def __init__(
        self,
        dedup_minutes: int = 5,
        on_signal: Optional[Callable[[TradingSignal], None]] = None,
    ):
        self.dedup_minutes = dedup_minutes
        self.on_signal = on_signal
        
        # Deduplication tracking: token -> last seen timestamp
        self._recent_signals: Dict[str, datetime] = {}
        
        # Active signals queue
        self._signals: Dict[str, TradingSignal] = {}
        
        # Stats
        self.total_signals = 0
        self.deduplicated = 0
        self.processed = 0
        
        self._lock = asyncio.Lock()
    
    async def add_signal(self, token_address: str, source_chat: str, original_message: str) -> bool:
        """
        Add a new signal. Returns True if it's a new signal, False if deduplicated.
        """
        now = datetime.utcnow()
        
        async with self._lock:
            # Check deduplication
            if token_address in self._recent_signals:
                last_seen = self._recent_signals[token_address]
                if now - last_seen < timedelta(minutes=self.dedup_minutes):
                    logger.debug("signal_deduplicated",
                               token=token_address[:8],
                               minutes_ago=(now - last_seen).total_seconds() / 60)
                    self.deduplicated += 1
                    return False
            
            # New signal
            signal = TradingSignal(
                token_address=token_address,
                source_chat=source_chat,
                original_message=original_message
            )
            
            self._recent_signals[token_address] = now
            self._signals[token_address] = signal
            self.total_signals += 1
            
            logger.info("new_signal_added",
                       token=token_address[:8] + "...",
                       chat=source_chat,
                       dedup_window=f"{self.dedup_minutes}min")
            
            # Notify handler immediately
            if self.on_signal:
                try:
                    await self.on_signal(signal)
                except Exception as e:
                    logger.error("signal_handler_error", error=str(e))
            
            return True
    
    def mark_processed(self, token_address: str, executed: bool = True) -> None:
        """Mark a signal as processed."""
        if token_address in self._signals:
            self._signals[token_address].processed = True
            self._signals[token_address].executed = executed
            self.processed += 1
    
    def is_recently_seen(self, token_address: str) -> bool:
        """Check if a token was recently signaled."""
        if token_address not in self._recent_signals:
            return False
        
        last_seen = self._recent_signals[token_address]
        return datetime.utcnow() - last_seen < timedelta(minutes=self.dedup_minutes)
    
    async def cleanup_old_signals(self) -> None:
        """Clean up old signals from memory periodically."""
        while True:
            await asyncio.sleep(60)  # Cleanup every minute
            
            now = datetime.utcnow()
            cutoff = now - timedelta(minutes=self.dedup_minutes * 2)
            
            async with self._lock:
                # Remove old dedup entries
                old_tokens = [
                    token for token, ts in self._recent_signals.items()
                    if ts < cutoff
                ]
                for token in old_tokens:
                    del self._recent_signals[token]
                    if token in self._signals:
                        del self._signals[token]
                
                if old_tokens:
                    logger.debug("cleaned_old_signals", count=len(old_tokens))
    
    def get_stats(self) -> dict:
        """Get signal manager stats."""
        return {
            "total_signals": self.total_signals,
            "deduplicated": self.deduplicated,
            "processed": self.processed,
            "active_signals": len(self._signals)
        }
