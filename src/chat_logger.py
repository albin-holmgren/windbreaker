"""
Chat History Logger - Persistent storage of Telegram messages for analysis.
Stores messages in JSONL format for future data mining and take-profit optimization.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
import structlog

logger = structlog.get_logger(__name__)


class ChatLogger:
    """Persistent logger for Telegram chat messages with rotation and cleanup."""
    
    def __init__(
        self,
        base_path: str = "/data/chat_history",
        retention_days: int = 90,
        max_file_size_mb: float = 100.0
    ):
        self.base_path = Path(base_path)
        self.retention_days = retention_days
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        
        # Ensure directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)
        
        # Stats
        self.messages_logged = 0
        self.files_created = 0
        
        logger.info("chat_logger_initialized",
                   base_path=str(self.base_path),
                   retention_days=retention_days)
    
    def _get_current_file(self) -> Path:
        """Get the file path for today's messages."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.base_path / f"chat_{today}.jsonl"
    
    def _rotate_if_needed(self, filepath: Path) -> Path:
        """Rotate file if it exceeds max size."""
        if filepath.exists() and filepath.stat().st_size > self.max_file_size_bytes:
            # Create new file with timestamp suffix
            timestamp = datetime.utcnow().strftime("%H%M%S")
            new_name = f"chat_{datetime.utcnow().strftime('%Y-%m-%d')}_{timestamp}.jsonl"
            return self.base_path / new_name
        return filepath
    
    def log_message(
        self,
        timestamp: datetime,
        chat_id: int,
        chat_name: str,
        message_id: int,
        text: str,
        has_address: bool = False,
        token_address: Optional[str] = None,
        classification: Optional[str] = None,
        confidence: Optional[str] = None,
        bot_action: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Log a single message to persistent storage.
        
        Args:
            timestamp: Message timestamp
            chat_id: Telegram chat ID
            chat_name: Chat/group name
            message_id: Telegram message ID
            text: Message text content
            has_address: Whether message contains token address
            token_address: Extracted token address if any
            classification: AI classification (fresh/old/unknown)
            confidence: AI confidence (high/medium/low)
            bot_action: What the bot did (bought/skipped/none)
            metadata: Additional flexible metadata
        """
        try:
            # Build log entry
            entry = {
                "timestamp": timestamp.isoformat(),
                "chat_id": chat_id,
                "chat_name": chat_name,
                "message_id": message_id,
                "text": text[:500],  # Truncate very long messages
                "has_address": has_address,
                "token_address": token_address,
                "classification": classification,
                "confidence": confidence,
                "bot_action": bot_action,
            }
            
            # Add any extra metadata
            if metadata:
                entry["metadata"] = metadata
            
            # Get file path (with rotation if needed)
            filepath = self._get_current_file()
            filepath = self._rotate_if_needed(filepath)
            
            # Append to file
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            
            self.messages_logged += 1
            
            # Log first few messages for debugging
            if self.messages_logged <= 5:
                logger.info("message_logged",
                           chat=chat_name,
                           has_address=has_address,
                           token=token_address[:8] if token_address else None)
            
            return True
            
        except Exception as e:
            logger.error("failed_to_log_message", error=str(e), chat=chat_name)
            return False
    
    def get_history(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        chat_id: Optional[int] = None,
        has_address: Optional[bool] = None,
        limit: int = 10000
    ) -> List[Dict[str, Any]]:
        """
        Query historical messages.
        
        Args:
            start_date: Start date (YYYY-MM-DD) inclusive
            end_date: End date (YYYY-MM-DD) inclusive
            chat_id: Filter by specific chat
            has_address: Filter for messages with addresses
            limit: Maximum results to return
            
        Returns:
            List of message dictionaries
        """
        results = []
        
        try:
            # Get all files in date range
            files = sorted(self.base_path.glob("chat_*.jsonl"))
            
            for filepath in files:
                # Check if file is in date range
                date_str = filepath.stem.replace("chat_", "").split("_")[0]
                
                if start_date and date_str < start_date:
                    continue
                if end_date and date_str > end_date:
                    continue
                
                # Read and filter
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            
                            # Apply filters
                            if chat_id and entry.get("chat_id") != chat_id:
                                continue
                            if has_address is not None and entry.get("has_address") != has_address:
                                continue
                            
                            results.append(entry)
                            
                            if len(results) >= limit:
                                return results
                                
                        except json.JSONDecodeError:
                            continue
            
            return results
            
        except Exception as e:
            logger.error("failed_to_get_history", error=str(e))
            return []
    
    def search_text(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Search message text for specific patterns (e.g., "217x", "100x").
        
        Args:
            query: Text to search for
            limit: Maximum results
            
        Returns:
            List of matching messages
        """
        results = []
        query_lower = query.lower()
        
        try:
            files = sorted(self.base_path.glob("chat_*.jsonl"))
            
            for filepath in files:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            text = entry.get("text", "").lower()
                            
                            if query_lower in text:
                                results.append(entry)
                                
                                if len(results) >= limit:
                                    return results
                        except json.JSONDecodeError:
                            continue
            
            return results
            
        except Exception as e:
            logger.error("failed_to_search", error=str(e), query=query)
            return []
    
    def cleanup_old_logs(self) -> int:
        """
        Remove log files older than retention period.
        
        Returns:
            Number of files deleted
        """
        deleted = 0
        cutoff = datetime.utcnow() - timedelta(days=self.retention_days)
        
        try:
            for filepath in self.base_path.glob("chat_*.jsonl"):
                try:
                    # Extract date from filename
                    date_str = filepath.stem.replace("chat_", "").split("_")[0]
                    file_date = datetime.strptime(date_str, "%Y-%m-%d")
                    
                    if file_date < cutoff:
                        filepath.unlink()
                        deleted += 1
                        logger.info("deleted_old_log", file=filepath.name)
                except (ValueError, OSError) as e:
                    logger.warning("failed_to_cleanup_file", file=str(filepath), error=str(e))
                    continue
            
            logger.info("cleanup_complete", files_deleted=deleted)
            return deleted
            
        except Exception as e:
            logger.error("cleanup_failed", error=str(e))
            return 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get logging statistics."""
        try:
            files = list(self.base_path.glob("chat_*.jsonl"))
            total_size = sum(f.stat().st_size for f in files)
            
            return {
                "messages_logged": self.messages_logged,
                "files_created": len(files),
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "retention_days": self.retention_days,
                "base_path": str(self.base_path)
            }
        except Exception as e:
            logger.error("failed_to_get_stats", error=str(e))
            return {}
