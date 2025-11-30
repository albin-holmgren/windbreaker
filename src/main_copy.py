"""
Main entry point for Copy Trading mode.
Run with: python -m src.main_copy
"""

import asyncio
import signal
import sys
from datetime import datetime
import structlog

from .config import load_config
from .wallet import create_wallet
from .rpc import RPCClient
from .copy_trader import CopyTrader

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


class CopyTradingBot:
    """Main copy trading bot class."""
    
    def __init__(self):
        self.config = None
        self.wallet = None
        self.rpc = None
        self.copy_trader = None
        self.running = False
    
    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("initializing_copy_trader")
        
        # Load config
        self.config = load_config()
        
        if not self.config.copy_enabled:
            logger.error("copy_trading_disabled", 
                        message="Set COPY_ENABLED=true in .env")
            sys.exit(1)
        
        if not self.config.copy_wallets:
            logger.error("no_wallets_configured",
                        message="Set COPY_WALLETS in .env (comma-separated)")
            sys.exit(1)
        
        # Parse target wallets
        target_wallets = [
            w.strip() for w in self.config.copy_wallets.split(',')
            if w.strip()
        ]
        
        if not target_wallets:
            logger.error("no_valid_wallets")
            sys.exit(1)
        
        # Create wallet
        self.wallet = create_wallet(self.config)
        logger.info(
            "wallet_loaded",
            address=self.wallet.address,
            network=self.config.network
        )
        
        # Create RPC client
        self.rpc = RPCClient(self.config.rpc_url)
        
        # Check balance
        balance = await self.rpc.get_balance(self.wallet.pubkey)
        balance_sol = balance / 1e9
        logger.info("wallet_balance", balance_sol=f"{balance_sol:.4f}")
        
        if balance_sol < 0.05:
            logger.warning("low_balance", 
                          message="Balance is very low, may not be able to execute trades")
        
        # Create copy trader
        self.copy_trader = CopyTrader(
            config=self.config,
            target_wallets=target_wallets,
            wallet_keypair=self.wallet.keypair,
            rpc_client=self.rpc
        )
        
        logger.info(
            "copy_trader_initialized",
            target_wallets=len(target_wallets),
            wallets=[w[:8] + "..." for w in target_wallets],
            copy_pct=f"{self.config.copy_balance_pct}%",
            max_sol=self.config.copy_max_sol,
            min_sol=self.config.copy_min_sol,
            copy_sells=self.config.copy_sells
        )
    
    async def cleanup(self) -> None:
        """Clean up resources."""
        logger.info("cleaning_up")
        
        if self.copy_trader:
            await self.copy_trader.stop()
        if self.rpc:
            await self.rpc.close()
    
    async def run(self) -> None:
        """Main run loop."""
        self.running = True
        
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)
        
        logger.info(
            "copy_trader_starting",
            message="Monitoring wallets for trades to copy..."
        )
        
        try:
            # Start copy trader (this blocks)
            await self.copy_trader.start()
        except asyncio.CancelledError:
            logger.info("copy_trader_cancelled")
        except Exception as e:
            logger.error("copy_trader_error", error=str(e))
        finally:
            await self.cleanup()
            logger.info("copy_trader_shutdown_complete")
    
    def _handle_shutdown(self) -> None:
        """Handle shutdown signal."""
        logger.info("shutdown_requested")
        self.running = False
        
        if self.copy_trader:
            self.copy_trader.running = False


async def main():
    """Entry point."""
    bot = CopyTradingBot()
    
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
