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
            timeout_ms=self.config.ai_timeout_ms
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
        logger.info("telegram_monitor_initialized",
                   api_id=self.config.telegram_api_id,
                   phone=self.config.telegram_phone[:6] + "...")
    
    async def _on_telegram_message(self, text: str, address: str, chat_name: str) -> None:
        """Handle incoming Telegram message with potential signal."""
        logger.debug("processing_telegram_message",
                   chat=chat_name,
                   text_preview=text[:50],
                   extracted_address=address[:8] + "...")
        
        # Quick regex validation
        if len(address) < 32 or len(address) > 44:
            logger.debug("invalid_address_length", address=address[:10])
            return
        
        # Parse with AI for confirmation
        token_address = await self.ai_parser.extract_token_fast(text)
        
        if not token_address:
            logger.debug("ai_no_token_extracted")
            return
        
        # Add to signal manager (will trigger trade if new)
        await self.signal_manager.add_signal(
            token_address=token_address,
            source_chat=chat_name,
            original_message=text
        )
    
    async def _on_signal(self, signal) -> None:
        """Handle new trading signal."""
        logger.info("new_signal_received",
                   token=signal.token_address[:8] + "...",
                   chat=signal.source_chat)
        
        # Execute buy
        success = await self.fast_trader.execute_buy(signal)
        
        if success:
            # Register position with position manager for tiered selling
            self.position_manager.add_position(
                token_address=signal.token_address,
                entry_sol=self.config.trade_amount_sol,
                total_tokens=0  # Will be updated on first price check
            )
            self.signal_manager.mark_processed(signal.token_address, executed=True)
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
    
    async def _run_telegram_monitor_with_restart(self) -> None:
        """Run Telegram monitor with auto-restart on failure."""
        restart_delay = 5
        max_restart_delay = 60
        
        while self.running:
            try:
                logger.info("starting_telegram_monitor_task")
                await self.telegram_monitor.start()
                logger.warning("telegram_monitor_exited_gracefully")
            except Exception as e:
                logger.error("telegram_monitor_crashed", error=str(e), restart_delay=restart_delay)
            
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
