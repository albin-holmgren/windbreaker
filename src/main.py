#!/usr/bin/env python3
"""
Windbreaker - Solana Triangular Arbitrage Bot
Main entry point.
"""

import asyncio
import signal
import sys
from datetime import datetime
import structlog

from .config import load_config, Config
from .wallet import create_wallet, Wallet
from .rpc import create_rpc_client, RPCClient
from .arb_engine import create_arb_engine, ArbitrageEngine, TriangleOpportunity
from .executor import create_executor, Executor
from .monitor import create_monitor, Monitor


# Configure structured logging
def configure_logging(log_level: str) -> None:
    """Configure structured JSON logging."""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    import logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper())
    )


logger = structlog.get_logger()


class WindbreakerBot:
    """Main bot class that orchestrates all components."""
    
    def __init__(self):
        self.config: Config = None
        self.wallet: Wallet = None
        self.rpc: RPCClient = None
        self.engine: ArbitrageEngine = None
        self.executor: Executor = None
        self.monitor: Monitor = None
        self._running = False
        self._shutdown_event = asyncio.Event()
    
    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("initializing_windbreaker")
        
        # Load configuration
        self.config = load_config()
        configure_logging(self.config.log_level)
        
        # Initialize components
        self.wallet = create_wallet(self.config)
        self.rpc = create_rpc_client(self.config)
        self.engine = create_arb_engine(self.config)
        self.executor = create_executor(self.config, self.wallet, self.rpc)
        self.monitor = create_monitor(self.config)
        
        # Check wallet balance
        try:
            balance = await self.rpc.get_balance(self.wallet.pubkey)
            balance_sol = balance / 1e9
            logger.info(
                "wallet_balance",
                address=self.wallet.address,
                balance_sol=f"{balance_sol:.4f}"
            )
            
            if balance_sol < 0.01:
                logger.warning(
                    "low_balance",
                    message="Wallet balance is very low. May not be able to execute trades."
                )
        except Exception as e:
            logger.warning("balance_check_failed", error=str(e))
        
        logger.info(
            "windbreaker_initialized",
            network=self.config.network,
            wallet=self.wallet.address[:12] + "...",
            min_profit_pct=self.config.min_profit_pct,
            trade_amount_usd=self.config.trade_amount_usd
        )
    
    async def cleanup(self) -> None:
        """Clean up resources."""
        logger.info("cleaning_up")
        
        if self.engine:
            await self.engine.close()
        if self.executor:
            await self.executor.close()
        if self.rpc:
            await self.rpc.close()
        if self.monitor:
            await self.monitor.close()
    
    async def run_scan_cycle(self) -> None:
        """Run a single scan and execution cycle."""
        try:
            # Get current SOL price (simplified)
            sol_price = await self.rpc.get_sol_price_usd()
            
            # Get wallet balance and calculate trade amount
            balance_lamports = await self.rpc.get_balance(self.wallet.pubkey)
            balance_sol = balance_lamports / 1e9
            
            # Reserve SOL for fees, trade percentage of the rest
            available_sol = max(0, balance_sol - self.config.fee_reserve_sol)
            trade_sol = available_sol * (self.config.trade_balance_pct / 100.0)
            trade_amount_usd = trade_sol * sol_price
            
            logger.info(
                "balance_check",
                balance_sol=f"{balance_sol:.4f}",
                available_sol=f"{available_sol:.4f}",
                trade_sol=f"{trade_sol:.4f}",
                trade_usd=f"{trade_amount_usd:.2f}"
            )
            
            # Skip if trade amount too small (< $0.50)
            if trade_amount_usd < 0.50:
                logger.warning("trade_amount_too_small", trade_usd=f"{trade_amount_usd:.2f}")
                return
            
            # Find best opportunity with dynamic trade amount
            opportunity = await self.engine.find_best_opportunity(
                sol_price_usd=sol_price,
                input_amount_usd=trade_amount_usd
            )
            
            # Update monitor
            await self.monitor.on_scan_complete(1 if opportunity else 0)
            
            if opportunity and opportunity.net_profit_pct >= self.config.min_profit_pct:
                logger.info(
                    "executing_opportunity",
                    path=f"{opportunity.path[0]}->{opportunity.path[1]}->{opportunity.path[2]}",
                    net_profit=f"{opportunity.net_profit_pct:.4f}%"
                )
                
                # Execute the trade
                result = await self.executor.execute_triangle(opportunity)
                
                # Report result
                await self.monitor.on_trade_executed(result)
                
                if result.success:
                    logger.info(
                        "trade_success",
                        signature=result.signature,
                        profit_pct=f"{opportunity.net_profit_pct:.4f}%"
                    )
                else:
                    logger.warning(
                        "trade_failed",
                        error=result.error
                    )
            
        except Exception as e:
            logger.error("scan_cycle_error", error=str(e))
            await self.monitor.on_error("scan_cycle_error", str(e))
    
    async def run(self) -> None:
        """Main bot loop."""
        await self.initialize()
        await self.monitor.on_startup()
        
        self._running = True
        poll_interval = self.config.poll_interval_seconds
        
        logger.info(
            "starting_main_loop",
            poll_interval_ms=self.config.poll_interval_ms
        )
        
        try:
            while self._running:
                cycle_start = datetime.utcnow()
                
                await self.run_scan_cycle()
                
                # Calculate time to sleep
                elapsed = (datetime.utcnow() - cycle_start).total_seconds()
                sleep_time = max(0, poll_interval - elapsed)
                
                if sleep_time > 0:
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=sleep_time
                        )
                        # If we get here, shutdown was requested
                        break
                    except asyncio.TimeoutError:
                        # Normal timeout, continue loop
                        pass
                        
        except asyncio.CancelledError:
            logger.info("bot_cancelled")
        finally:
            await self.cleanup()
    
    def stop(self) -> None:
        """Signal the bot to stop."""
        logger.info("stop_requested")
        self._running = False
        self._shutdown_event.set()


async def main() -> None:
    """Main entry point."""
    bot = WindbreakerBot()
    
    # Set up signal handlers
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        bot.stop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
        bot.stop()
    except Exception as e:
        logger.error("fatal_error", error=str(e))
        raise
    finally:
        logger.info("windbreaker_shutdown_complete")


def run() -> None:
    """Entry point for the bot."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
