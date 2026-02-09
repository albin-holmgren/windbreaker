"""
Main entry point for Telegram AI Trading mode.
Monitors Telegram groups, parses signals with AI, executes trades with tiered selling.
Run with: python -m src.main_telegram
"""

import asyncio
import signal
import sys
import os
import argparse
from datetime import datetime
import structlog

from .config import load_config
from .wallet import create_wallet
from .rpc import RPCClient
from .telegram_monitor import TelegramUserMonitor
from .ai_parser import AIGatewayParser
from .signal_manager import FastSignalManager
from .fast_trader import FastTrader
from .tiered_position_manager import TieredPositionManager
from .web_dashboard import WebDashboard
from .chat_logger import ChatLogger

# Configure logging
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class TelegramAITrader:
    """Telegram AI Trading Bot - Monitors groups, trades on signals."""
    
    # Singleton instance for dashboard access
    _instance = None
    
    def __init__(self, env_file: str = '.env'):
        TelegramAITrader._instance = self
        self.config = None
        self.wallet = None
        self.rpc = None
        self.telegram_monitor = None
        self.ai_parser = None
        self.signal_manager = None
        self.fast_trader = None
        self.position_manager = None
        self.dashboard = None
        self.running = False
        self.env_file = env_file
        self.chat_logger: Optional[ChatLogger] = None
    
    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("initializing_telegram_ai_trader", env_file=self.env_file)
        
        # Load config
        self.config = load_config(self.env_file)
        
        # Validate Telegram credentials
        if not self.config.telegram_api_id or not self.config.telegram_api_hash:
            logger.error("telegram_credentials_missing",
                       message="Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
            sys.exit(1)
        
        if not self.config.telegram_phone:
            logger.error("telegram_phone_missing",
                       message="Set TELEGRAM_PHONE in .env")
            sys.exit(1)
        
        # Validate AI credentials
        if not self.config.ai_gateway_key:
            logger.error("ai_gateway_key_missing",
                       message="Set AI_GATEWAY_KEY in .env")
            sys.exit(1)
        
        # Create wallet
        self.wallet = create_wallet(self.config)
        logger.info("wallet_loaded",
                   address=self.wallet.address,
                   network=self.config.network)
        
        # Create RPC client
        self.rpc = RPCClient(self.config)
        
        # Check balance
        try:
            balance = await self.rpc.get_balance(self.wallet.pubkey)
            balance_sol = balance / 1e9
            logger.info("wallet_balance", balance_sol=f"{balance_sol:.4f}")
            
            if balance_sol < 0.1:
                logger.warning("low_balance",
                             message="Balance is low, may not be able to execute many trades")
        except Exception as e:
            logger.warning("balance_check_failed", error=str(e), message="Continuing without balance check")
        
        # Initialize AI parser
        self.ai_parser = AIGatewayParser(
            api_key=self.config.ai_gateway_key,
            model=self.config.ai_model,
            timeout_ms=self.config.ai_timeout_ms,
            gateway_url=self.config.ai_gateway_url
        )
        await self.ai_parser.start()
        logger.info("ai_parser_initialized", model=self.config.ai_model)
        
        # Initialize signal manager
        self.signal_manager = FastSignalManager(
            dedup_minutes=5,
            on_signal=self._on_signal
        )
        # Start cleanup task
        asyncio.create_task(self.signal_manager.cleanup_old_signals())
        logger.info("signal_manager_initialized")
        
        # Initialize fast trader
        self.fast_trader = FastTrader(
            config=self.config,
            rpc_client=self.rpc,
            wallet_keypair=self.wallet.keypair,
            trade_amount_sol=self.config.trade_amount_sol,
            exit_fee_reserve_per_position=self.config.exit_fee_reserve_per_position,
            min_balance_buffer=self.config.min_balance_buffer
        )
        await self.fast_trader.start()
        logger.info("fast_trader_initialized",
                   trade_amount=self.config.trade_amount_sol)
        
        # Initialize position manager (tiered selling)
        self.position_manager = TieredPositionManager(
            rpc_client=self.rpc,
            wallet_keypair=self.wallet.keypair,
            check_interval_sec=self.config.position_check_interval_sec,
            tier3_trailing_stop_percent=self.config.tier3_trailing_stop,
            tier3_activation_multiplier=self.config.tier1_multiplier  # Activate trailing after 2x
        )
        await self.position_manager.start()
        logger.info("tiered_position_manager_initialized",
                   tier1=f"{self.config.tier1_sell_percent*100:.0f}% at {self.config.tier1_multiplier}x",
                   tier2=f"{self.config.tier2_sell_percent*100:.0f}% at {self.config.tier2_multiplier}x",
                   tier3=f"{self.config.tier3_trailing_stop*100:.0f}% trailing stop")
        
        # Initialize Telegram monitor (last, because it blocks)
        self.telegram_monitor = TelegramUserMonitor(
            api_id=self.config.telegram_api_id,
            api_hash=self.config.telegram_api_hash,
            phone=self.config.telegram_phone,
            session_name=self.config.telegram_session_name,
            on_message=self._on_telegram_message
        )
        # Initialize chat logger for history
        self.chat_logger = ChatLogger(
            base_path="/data/chat_history",
            retention_days=90,
            max_file_size_mb=100.0
        )
        logger.info("chat_logger_initialized", retention_days=90)
    
    async def _on_telegram_message(self, text: str, address: str, chat_name: str, chat_id: int = 0, message_id: int = 0) -> None:
        """Handle incoming Telegram message with potential signal."""
        now = datetime.utcnow()
        
        logger.info("processing_telegram_message",  # Changed to INFO for visibility
                   chat=chat_name,
                   text_preview=text[:50],
                   extracted_address=address[:8] + "...",
                   chat_id=chat_id)
        
        # Quick regex validation
        if len(address) < 32 or len(address) > 44:
            logger.warning("invalid_address_length", address=address[:10], length=len(address))  # Changed to WARNING
            # Log even invalid messages for completeness
            if self.chat_logger:
                self.chat_logger.log_message(
                    timestamp=now,
                    chat_id=chat_id or hash(chat_name) % 10000000000,  # Use hash if no ID
                    chat_name=chat_name,
                    message_id=message_id,
                    text=text,
                    has_address=False,
                    bot_action="invalid_address"
                )
            return
        
        # Parse with AI for confirmation and fresh launch detection
        # Add timeout to prevent hanging
        try:
            token_address, classification, confidence = await asyncio.wait_for(
                self.ai_parser.extract_token_fast(text, chat_id or chat_name),
                timeout=5.0  # 5 second max for AI + fallback
            )
            logger.info("ai_parse_result", 
                       has_token=bool(token_address),
                       classification=classification,
                       confidence=confidence,
                       token_preview=token_address[:8] if token_address else None)
        except asyncio.TimeoutError:
            logger.warning("ai_parser_timeout_using_fallback", extracted_address=address[:8])
            # Use the regex-extracted address directly
            token_address = address
            classification = "unknown"
            confidence = "low"
        except Exception as e:
            logger.error("ai_parser_error_using_fallback", error=str(e), extracted_address=address[:8])
            # Use the regex-extracted address directly
            token_address = address
            classification = "unknown"
            confidence = "low"
        
        # Determine bot action
        bot_action = "none"
        if not token_address:
            bot_action = "no_token_extracted"
            logger.warning("ai_no_token_extracted")  # Changed to WARNING
        elif classification == "old":
            bot_action = "skipped_old"
            logger.info("skipping_old_call_message",
                       token=token_address[:8],
                       chat=chat_name,
                       reason="ai_classified_as_old")
        elif classification == "fresh":
            bot_action = "fresh_detected"
            logger.info("fresh_launch_detected",
                       token=token_address[:8],
                       chat=chat_name,
                       confidence=confidence,
                       message_preview=text[:60])
        else:
            bot_action = f"classification_{classification}"
            logger.info("token_with_classification",
                       token=token_address[:8],
                       classification=classification,
                       confidence=confidence)
        
        # Log confidence level for debugging
        if confidence == "high":
            logger.info("high_confidence_signal",
                       token=token_address[:8] if token_address else "none",
                       chat=chat_name,
                       classification=classification)
        
        # Log message to persistent storage
        if self.chat_logger:
            self.chat_logger.log_message(
                timestamp=now,
                chat_id=chat_id or hash(chat_name) % 10000000000,
                chat_name=chat_name,
                message_id=message_id,
                text=text,
                has_address=token_address is not None,
                token_address=token_address,
                classification=classification,
                confidence=confidence,
                bot_action=bot_action
            )
        
        # Skip if AI classified this as an OLD call (not a fresh launch)
        if classification == "old" or not token_address:
            logger.info("skipping_signal",
                       reason="old_or_no_token",
                       classification=classification,
                       has_token=bool(token_address))
            return
        
        logger.info("adding_signal_to_manager",
                   token=token_address[:8],
                   chat=chat_name,
                   classification=classification)
        
        # Add to signal manager (will trigger trade if new)
        try:
            result = await self.signal_manager.add_signal(
                token_address=token_address,
                source_chat=chat_name,
                original_message=text
            )
            logger.info("signal_add_result",
                       token=token_address[:8],
                       is_new_signal=result)
        except Exception as e:
            logger.error("signal_add_failed",
                      token=token_address[:8],
                      error=str(e))
    
    async def _on_signal(self, signal) -> None:
        """Handle new trading signal."""
        logger.info("new_signal_received",
                   token=signal.token_address[:8] + "...",
                   chat=signal.source_chat)
        
        # Execute buy
        success = await self.fast_trader.execute_buy(signal)
        
        if success:
            # Register position with position manager for tiered selling
            position = self.position_manager.add_position(
                token_address=signal.token_address,
                entry_sol=self.config.trade_amount_sol,
                total_tokens=0  # Will be updated on first price check
            )
            # Immediately update balance so it shows correctly in dashboard
            await self.position_manager._update_position_balance(position)
            
            self.signal_manager.mark_processed(signal.token_address, executed=True)
            self.signal_manager.mark_bought(signal.token_address)
            logger.info("trade_executed",
                       token=signal.token_address[:8],
                       amount=self.config.trade_amount_sol)
        else:
            self.signal_manager.mark_processed(signal.token_address, executed=False)
            logger.warning("trade_failed",
                          token=signal.token_address[:8])
    
    async def cleanup(self) -> None:
        """Clean up resources."""
        logger.info("cleaning_up")
        
        if self.telegram_monitor:
            await self.telegram_monitor.stop()
        if self.ai_parser:
            await self.ai_parser.stop()
        if self.fast_trader:
            await self.fast_trader.stop()
        if self.position_manager:
            await self.position_manager.stop()
        if self.dashboard:
            await self.dashboard.stop()
        if self.rpc:
            await self.rpc.close()
    
    async def run(self) -> None:
        """Main run loop with auto-restart for Telegram monitor."""
        self.running = True
        
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)
        
        logger.info("telegram_ai_trader_starting",
                   message="Monitoring Telegram groups for signals...")
        
        # Start dashboard
        self.dashboard = WebDashboard(
            state_file='telegram_trader_state.json',
            rpc_client=self.rpc,
            wallet_keypair=self.wallet
        )
        await self.dashboard.start()
        
        # Start position manager monitoring
        position_task = asyncio.create_task(self._run_position_manager())
        
        # Start Telegram monitor with auto-restart
        telegram_task = asyncio.create_task(self._run_telegram_monitor_with_restart())
        
        try:
            # Wait for both tasks
            await asyncio.gather(position_task, telegram_task)
        except asyncio.CancelledError:
            logger.info("trader_cancelled")
        except Exception as e:
            logger.error("trader_error", error=str(e))
        finally:
            await self.cleanup()
            logger.info("trader_shutdown_complete")
    
    async def _run_position_manager(self) -> None:
        """Run position manager monitoring loop."""
        logger.info("position_manager_task_started")
        while self.running:
            try:
                await asyncio.sleep(5)  # Check positions every 5 seconds
                # Position manager runs its own checks
            except Exception as e:
                logger.error("position_manager_error", error=str(e))
                await asyncio.sleep(10)  # Backoff on error
    
    async def _send_alert(self, message: str) -> None:
        """Send alert via Telegram bot token (Layer 2: session death alerts)."""
        bot_token = self.config.telegram_bot_token
        chat_id = self.config.telegram_chat_id
        
        if not bot_token or not chat_id:
            logger.warning("alert_skipped_no_bot_token", 
                          has_token=bool(bot_token), has_chat_id=bool(chat_id))
            return
        
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        logger.info("alert_sent", chat_id=chat_id)
                    else:
                        logger.warning("alert_send_failed", status=resp.status)
        except Exception as e:
            logger.warning("alert_send_error", error=str(e))
    
    async def _run_telegram_monitor_with_restart(self) -> None:
        """Run Telegram monitor with auto-restart on failure."""
        restart_delay = 5
        max_restart_delay = 60
        session_death_alerted = False
        
        while self.running:
            try:
                logger.info("starting_telegram_monitor_task")
                session_death_alerted = False  # Reset on successful start attempt
                await self.telegram_monitor.start()
                logger.warning("telegram_monitor_exited_gracefully")
            except Exception as e:
                error_str = str(e)
                logger.error("telegram_monitor_crashed", error=error_str, restart_delay=restart_delay)
                
                # --- Layer 2: Alert when session dies ---
                if "invalid or expired" in error_str or "not authorized" in error_str.lower():
                    if not session_death_alerted:
                        session_death_alerted = True
                        await self._send_alert(
                            "\u26a0\ufe0f <b>Windbreaker Bot Session Died!</b>\n\n"
                            "The Telegram session has been invalidated.\n\n"
                            "<b>To fix:</b>\n"
                            "1. Run generate_session.py locally\n"
                            "2. Update TELEGRAM_SESSION_STRING in Railway\n"
                            "3. Redeploy\n\n"
                            "<b>To prevent:</b> Don't press 'Terminate All Other Sessions' "
                            "in Telegram Settings > Active Sessions. "
                            "Look for <i>Windbreaker Bot</i> and leave it alone."
                        )
            
            if not self.running:
                break
            
            # Wait before restart with exponential backoff
            logger.info("restarting_telegram_monitor", delay=restart_delay)
            await asyncio.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, max_restart_delay)
    
    def _handle_shutdown(self) -> None:
        """Handle shutdown signal."""
        logger.info("shutdown_requested")
        self.running = False
        
        if self.telegram_monitor:
            self.telegram_monitor.running = False
            # Disconnect to break run_until_disconnected()
            if self.telegram_monitor.client:
                self.telegram_monitor.client.disconnect()


async def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description='Windbreaker Telegram AI Trading Bot')
    parser.add_argument('--env', default='.env', help='Path to .env file (default: .env)')
    args = parser.parse_args()
    
    bot = TelegramAITrader(env_file=args.env)
    
    try:
        await bot.initialize()
        await bot.run()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    except Exception as e:
        logger.error("fatal_error", error=str(e))
        sys.exit(1)


def run():
    """Sync entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
