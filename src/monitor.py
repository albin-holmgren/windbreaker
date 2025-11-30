"""
Monitoring and alerting for Windbreaker.
Handles Telegram notifications and metrics tracking.
"""

import asyncio
import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
import aiohttp
import structlog

from .config import Config
from .executor import ExecutionResult

logger = structlog.get_logger()


class TelegramAlert:
    """Telegram bot for sending alerts."""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False
    ) -> bool:
        """Send a message to the configured chat."""
        session = await self._get_session()
        
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification
        }
        
        try:
            async with session.post(
                f"{self.api_url}/sendMessage",
                json=payload
            ) as response:
                if response.status == 200:
                    return True
                else:
                    error = await response.text()
                    logger.warning("telegram_send_failed", error=error[:100])
                    return False
                    
        except Exception as e:
            logger.error("telegram_error", error=str(e))
            return False
    
    async def send_trade_alert(self, result: ExecutionResult) -> bool:
        """Send a trade execution alert."""
        opp = result.opportunity
        path_str = f"{opp.path[0]} → {opp.path[1]} → {opp.path[2]} → {opp.path[0]}"
        
        if result.success:
            emoji = "✅"
            status = "SUCCESS"
        else:
            emoji = "❌"
            status = "FAILED"
        
        message = f"""
{emoji} <b>Trade {status}</b>

<b>Route:</b> {path_str}
<b>Profit:</b> {opp.net_profit_pct:.4f}%

<b>Input:</b> {opp.input_amount}
<b>Output:</b> {opp.final_amount}

"""
        
        if result.signature:
            message += f'<b>TX:</b> <a href="https://solscan.io/tx/{result.signature}">{result.signature[:16]}...</a>\n'
        
        if result.error:
            message += f"\n<b>Error:</b> {result.error}"
        
        message += f"\n<i>{datetime.utcnow().isoformat()}</i>"
        
        return await self.send_message(message)
    
    async def send_error_alert(
        self,
        error_type: str,
        error_message: str,
        critical: bool = False
    ) -> bool:
        """Send an error alert."""
        emoji = "🚨" if critical else "⚠️"
        priority = "CRITICAL" if critical else "WARNING"
        
        message = f"""
{emoji} <b>{priority}: {error_type}</b>

{error_message}

<i>{datetime.utcnow().isoformat()}</i>
"""
        
        return await self.send_message(message, disable_notification=not critical)
    
    async def send_startup_message(self, config: Config) -> bool:
        """Send bot startup notification."""
        message = f"""
🚀 <b>Windbreaker Started</b>

<b>Network:</b> {config.network}
<b>Min Profit:</b> {config.min_profit_pct}%
<b>Trade Size:</b> ${config.trade_amount_usd}

<i>{datetime.utcnow().isoformat()}</i>
"""
        return await self.send_message(message)


class MetricsTracker:
    """Track and persist bot metrics."""
    
    def __init__(self, metrics_dir: str = "metrics"):
        self.metrics_dir = Path(metrics_dir)
        self.metrics_dir.mkdir(exist_ok=True)
        
        # Counters
        self.successful_trades = 0
        self.failed_trades = 0
        self.total_profit_usd = 0.0
        self.scans_performed = 0
        self.opportunities_found = 0
        
        # CSV file for trade history
        self.trades_file = self.metrics_dir / "trades.csv"
        self._ensure_csv_header()
    
    def _ensure_csv_header(self) -> None:
        """Ensure trades CSV has header."""
        if not self.trades_file.exists():
            with open(self.trades_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp',
                    'path',
                    'input_amount',
                    'output_amount',
                    'profit_pct',
                    'net_profit_pct',
                    'success',
                    'signature',
                    'error'
                ])
    
    def record_trade(self, result: ExecutionResult) -> None:
        """Record a trade execution."""
        opp = result.opportunity
        
        if result.success:
            self.successful_trades += 1
        else:
            self.failed_trades += 1
        
        # Write to CSV
        with open(self.trades_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.utcnow().isoformat(),
                f"{opp.path[0]}->{opp.path[1]}->{opp.path[2]}",
                opp.input_amount,
                opp.final_amount,
                f"{opp.profit_pct:.6f}",
                f"{opp.net_profit_pct:.6f}",
                result.success,
                result.signature or '',
                result.error or ''
            ])
        
        logger.info(
            "trade_recorded",
            success=result.success,
            total_success=self.successful_trades,
            total_failed=self.failed_trades
        )
    
    def record_scan(self, opportunities_count: int) -> None:
        """Record a scan cycle."""
        self.scans_performed += 1
        self.opportunities_found += opportunities_count
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return {
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "total_trades": self.successful_trades + self.failed_trades,
            "success_rate": (
                self.successful_trades / (self.successful_trades + self.failed_trades)
                if (self.successful_trades + self.failed_trades) > 0
                else 0
            ),
            "scans_performed": self.scans_performed,
            "opportunities_found": self.opportunities_found,
            "total_profit_usd": self.total_profit_usd
        }


class Monitor:
    """Combined monitoring system."""
    
    def __init__(self, config: Config):
        self.config = config
        self.metrics = MetricsTracker()
        self.telegram: Optional[TelegramAlert] = None
        
        if config.telegram_enabled:
            self.telegram = TelegramAlert(
                config.telegram_bot_token,
                config.telegram_chat_id
            )
    
    async def close(self) -> None:
        """Close resources."""
        if self.telegram:
            await self.telegram.close()
    
    async def on_startup(self) -> None:
        """Handle bot startup."""
        logger.info(
            "bot_started",
            network=self.config.network,
            min_profit=self.config.min_profit_pct
        )
        
        if self.telegram:
            await self.telegram.send_startup_message(self.config)
    
    async def on_trade_executed(self, result: ExecutionResult) -> None:
        """Handle trade execution."""
        self.metrics.record_trade(result)
        
        if self.telegram:
            await self.telegram.send_trade_alert(result)
    
    async def on_scan_complete(self, opportunities_count: int) -> None:
        """Handle scan completion."""
        self.metrics.record_scan(opportunities_count)
    
    async def on_error(
        self,
        error_type: str,
        error_message: str,
        critical: bool = False
    ) -> None:
        """Handle error."""
        if critical:
            logger.error(error_type, message=error_message)
        else:
            logger.warning(error_type, message=error_message)
        
        if self.telegram and (critical or self.config.log_level == "DEBUG"):
            await self.telegram.send_error_alert(error_type, error_message, critical)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return self.metrics.get_stats()


def create_monitor(config: Config) -> Monitor:
    """Factory function to create a monitor."""
    return Monitor(config)
