"""
Main entry point for Copy Trading mode.
Run with: python -m src.main_copy
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
from .copy_trader import CopyTrader
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


class CopyTradingBot:
    """Main copy trading bot class."""
    
    def __init__(self, env_file: str = '.env', state_file: str = 'mock_state.json'):
        self.config = None
        self.wallet = None
        self.rpc = None
        self.copy_trader = None
        self.dashboard = None
        self.running = False
        self.env_file = env_file
        self.state_file = state_file
    
    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("initializing_copy_trader", env_file=self.env_file, state_file=self.state_file)
        
        # Load config
        self.config = load_config(self.env_file)
        
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
        self.rpc = RPCClient(self.config)
        
        # Check balance (non-fatal if it fails - don't crash on RPC issues at startup)
        try:
            balance = await self.rpc.get_balance(self.wallet.pubkey)
            balance_sol = balance / 1e9
            logger.info("wallet_balance", balance_sol=f"{balance_sol:.4f}")
            
            if balance_sol < 0.05:
                logger.warning("low_balance", 
                              message="Balance is very low, may not be able to execute trades")
        except Exception as e:
            logger.warning("balance_check_failed", error=str(e), message="Continuing without balance check")
        
        # Create copy trader
        self.copy_trader = CopyTrader(
            config=self.config,
            target_wallets=target_wallets,
            wallet_keypair=self.wallet.keypair,
            rpc_client=self.rpc,
            state_file=self.state_file
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
        if self.dashboard:
            await self.dashboard.stop()
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
            # Start web dashboard with RPC client and wallet for real-time balance
            self.dashboard = WebDashboard(
                state_file=self.state_file,
                rpc_client=self.rpc,
                wallet_keypair=self.wallet
            )
            await self.dashboard.start()
            
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


async def run_dashboard_only(state_file: str):
    """Run only the dashboard without trading (for Railway)."""
    logger.info("starting_dashboard_only_mode", state_file=state_file)
    
    dashboard = WebDashboard(state_file=state_file)
    await dashboard.start()
    
    # Keep running
    while True:
        await asyncio.sleep(3600)


async def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description='Windbreaker Copy Trading Bot')
    parser.add_argument('--env', default='.env', help='Path to .env file (default: .env)')
    parser.add_argument('--state', default='mock_state.json', help='Path to state file (default: mock_state.json)')
    parser.add_argument('--dashboard-only', action='store_true', help='Run dashboard only without trading')
    args = parser.parse_args()
    
    # Check for DASHBOARD_ONLY environment variable
    dashboard_only = args.dashboard_only or os.getenv('DASHBOARD_ONLY', 'false').lower() == 'true'
    
    if dashboard_only:
        await run_dashboard_only(args.state)
        return
    
    bot = CopyTradingBot(env_file=args.env, state_file=args.state)
    
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
