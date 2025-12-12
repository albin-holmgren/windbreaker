"""
Copy Trader - Main module for copy trading functionality.
Monitors wallets, detects trades, and executes copies.
"""

import asyncio
import aiohttp
import json
import os
import time
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import structlog

from .wallet_monitor import WalletMonitor, WalletTransaction
from .tx_parser import TransactionParser, ParsedSwap, SwapType
from .config import Config
from .position_manager import PositionManager
from .trade_logger import trade_logger

logger = structlog.get_logger(__name__)

# Jupiter API for swaps - using lite-api (public, no auth required)
JUPITER_QUOTE_API = "https://lite-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://lite-api.jup.ag/v6/swap"

# Pump.fun API for bonding curve trades
PUMPFUN_API = "https://pumpportal.fun/api/trade-local"

# Native SOL
NATIVE_SOL = "So11111111111111111111111111111111111111112"


@dataclass
class CopyTradeResult:
    """Result of a copy trade execution."""
    success: bool
    signature: Optional[str] = None
    error: Optional[str] = None
    original_swap: Optional[ParsedSwap] = None
    our_sol_amount: int = 0
    mock: bool = False
    

@dataclass
class TradeStats:
    """Statistics for copy trading."""
    total_detected: int = 0
    total_copied: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    total_sol_spent: float = 0.0
    total_sol_received: float = 0.0
    tokens_held: Dict[str, int] = field(default_factory=dict)


class CopyTrader:
    """
    Copy Trading Bot - Monitors wallets and copies their trades.
    """
    
    def __init__(
        self,
        config: Config,
        target_wallets: List[str],
        wallet_keypair,  # solders.Keypair
        rpc_client,      # RPCClient from rpc.py
    ):
        self.config = config
        self.target_wallets = target_wallets
        self.wallet = wallet_keypair
        self.rpc = rpc_client
        
        # Components
        self.monitor: Optional[WalletMonitor] = None
        self.parser = TransactionParser(min_sol_value=config.copy_min_sol)
        self.session: Optional[aiohttp.ClientSession] = None
        
        # State
        self.stats = TradeStats()
        self.recent_copies: Set[str] = set()  # Track recently copied tokens
        self.running = False
        
        # Settings from config
        self.copy_percentage = config.copy_balance_pct / 100.0
        self.max_sol_per_trade = config.copy_max_sol
        self.min_sol_per_trade = config.copy_min_sol
        self.copy_sells = config.copy_sells
        self.fee_reserve = config.fee_reserve_sol
        self.copy_proportional = config.copy_proportional
        self.exit_fee_reserve = config.exit_fee_reserve
        self.max_positions = config.max_positions
        self.min_market_cap_usd = config.min_market_cap_usd
        self.min_token_age_minutes = config.min_token_age_minutes
        self.min_liquidity_usd = config.min_liquidity_usd
        self.min_volume_24h_usd = config.min_volume_24h_usd
        self.max_price_change_1h_pct = config.max_price_change_1h_pct
        self.min_txns_1h = config.min_txns_1h
        self.max_top10_holders_pct = config.max_top10_holders_pct
        self.max_dev_holdings_pct = config.max_dev_holdings_pct
        self.min_holders_count = config.min_holders_count
        self.trust_trader_pumpfun = config.trust_trader_pumpfun
        
        # Cache for token info (to avoid repeated API calls)
        # mint -> (market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h, cache_time)
        self.token_info_cache: Dict[str, tuple[float, float, float, float, float, int, float]] = {}
        
        # Cache for holder info from RugCheck (to avoid repeated API calls)
        # mint -> (top10_pct, dev_pct, holders_count, cache_time)
        self.holder_info_cache: Dict[str, tuple[float, float, int, float]] = {}
        
        # Track trader wallet balances for proportional sizing
        self.trader_balances: Dict[str, float] = {}
        
        # Position manager for auto-sell
        self.position_manager: Optional[PositionManager] = None
        
        # Mock trading support
        self.mock_trading = self.config.mock_trading
        self.mock_balance = self.config.mock_balance_sol
        self.mock_token_positions: Dict[str, int] = {}  # mint -> token amount (base units)
        self.mock_position_entry_time: Dict[str, float] = {}  # mint -> entry timestamp
        self.mock_position_entry_sol: Dict[str, float] = {}  # mint -> SOL spent
        self.trader_sold_cooldown: Dict[str, float] = {}  # mint -> timestamp when trader sold (but we had no position)
        self.sell_cooldown_seconds = 60  # Don't buy tokens the trader just sold (prevents out-of-sync positions)
        self.mock_state_file = Path(os.getenv('MOCK_STATE_FILE', '/windbreaker/mock_state.json'))
        self.mock_trades_history: List[Dict] = []  # Track all trades for dashboard
        self.mock_starting_balance = self.config.mock_balance_sol  # Remember starting balance
        # Max age before abandoning - pump.fun tokens rug fast, use short timeout
        self.mock_position_max_age_minutes = int(os.getenv('MOCK_MAX_POSITION_AGE_MINUTES', '10'))
        
        # Load persisted state if exists
        if self.mock_trading:
            self._load_mock_state()
            logger.info(
                "mock_trading_enabled",
                starting_balance=f"{self.mock_starting_balance:.4f} SOL",
                current_balance=f"{self.mock_balance:.4f} SOL",
                open_positions=len([p for p in self.mock_token_positions.values() if p > 0]),
                rug_detection="liquidity/mcap based"
            )
        
    async def start(self) -> None:
        """Start the copy trader."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        # Create position manager unless we're in mock mode
        if not self.mock_trading:
            self.position_manager = PositionManager(
                config=self.config,
                wallet_keypair=self.wallet,
                rpc_client=self.rpc,
                max_positions=self.config.max_positions,
                take_profit_pct=self.config.take_profit_pct,
                stop_loss_pct=self.config.stop_loss_pct,
                time_limit_minutes=self.config.time_limit_minutes,
                trailing_stop_pct=self.config.trailing_stop_pct,
                rug_abandon_sol=self.config.rug_abandon_sol,
                mcap_stop_loss_usd=self.config.mcap_stop_loss_usd,
            )
            await self.position_manager.start()
        
        # Create wallet monitor
        self.monitor = WalletMonitor(
            rpc_url=self.config.rpc_url,
            target_wallets=self.target_wallets,
            poll_interval_ms=self.config.copy_poll_interval_ms,
            on_transaction=self._on_transaction
        )
        
        logger.info(
            "copy_trader_started",
            wallets=len(self.target_wallets),
            copy_pct=f"{self.copy_percentage*100:.0f}%",
            max_sol=self.max_sol_per_trade,
            max_positions=self.config.max_positions,
            take_profit=f"{self.config.take_profit_pct}%",
            stop_loss=f"{self.config.stop_loss_pct}%"
        )
        
        # Start mock position cleanup task if in mock mode (BEFORE monitor blocks)
        if self.mock_trading:
            asyncio.create_task(self._mock_position_cleanup_loop())
        
        # Start monitoring (this blocks forever)
        await self.monitor.start()
    
    async def _mock_position_cleanup_loop(self) -> None:
        """Periodically clean up stale mock positions to free slots for new trades."""
        logger.info("mock_cleanup_loop_started")
        while self.running:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._cleanup_stale_mock_positions()
            except Exception as e:
                logger.error("mock_cleanup_error", error=str(e))
    
    async def _cleanup_stale_mock_positions(self) -> None:
        """Abandon mock positions based on token health (liquidity, market cap)."""
        # Thresholds for considering a token rugged
        MIN_LIQUIDITY_USD = float(os.getenv('MOCK_MIN_LIQUIDITY_USD', '1000'))  # Abandon if liquidity < $1000
        MIN_MARKET_CAP_USD = float(os.getenv('MOCK_MIN_MARKET_CAP_USD', '5000'))  # Abandon if mcap < $5000
        
        # Get active positions
        active_mints = [mint for mint, tokens in self.mock_token_positions.items() if tokens > 0]
        
        if not active_mints:
            return
        
        logger.debug(
            "mock_health_check",
            active_positions=len(active_mints),
            min_liquidity=f"${MIN_LIQUIDITY_USD:,.0f}",
            min_mcap=f"${MIN_MARKET_CAP_USD:,.0f}"
        )
        
        for mint in active_mints:
            try:
                # Get current token health from DexScreener
                market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h = await self._get_token_info(mint)
                
                entry_sol = self.mock_position_entry_sol.get(mint, 0)
                reason = None
                
                # Check if rugged (liquidity pulled or market cap crashed)
                if liquidity < MIN_LIQUIDITY_USD and liquidity > 0:
                    reason = f"liquidity_too_low (${liquidity:,.0f})"
                elif market_cap < MIN_MARKET_CAP_USD and market_cap > 0:
                    reason = f"mcap_too_low (${market_cap:,.0f})"
                elif market_cap == 0 and liquidity == 0 and age_minutes > 5:
                    # Token not found on DexScreener after 5 min = likely rugged
                    reason = "not_on_dexscreener_anymore"
                
                if reason:
                    logger.warning(
                        "mock_position_abandoned",
                        token=mint[:8],
                        entry_sol=f"{entry_sol:.4f}",
                        market_cap=f"${market_cap:,.0f}",
                        liquidity=f"${liquidity:,.0f}",
                        reason=reason
                    )
                    
                    # Clear the position (assume 100% loss)
                    self.mock_token_positions[mint] = 0
                    self.mock_position_entry_time.pop(mint, None)
                    self.mock_position_entry_sol.pop(mint, None)
                    
            except Exception as e:
                logger.debug("health_check_error", token=mint[:8], error=str(e))
    
    async def stop(self) -> None:
        """Stop the copy trader."""
        self.running = False
        
        # Save state before stopping
        if self.mock_trading:
            self._save_mock_state()
        
        if self.position_manager:
            await self.position_manager.stop()
        if self.monitor:
            await self.monitor.stop()
        if self.session:
            await self.session.close()
        
        logger.info(
            "copy_trader_stopped",
            stats=self._format_stats()
        )
    
    def _load_mock_state(self) -> None:
        """Load persisted mock trading state from file."""
        try:
            if self.mock_state_file.exists():
                with open(self.mock_state_file, 'r') as f:
                    state = json.load(f)
                
                self.mock_balance = state.get('balance', self.mock_balance)
                self.mock_starting_balance = state.get('starting_balance', self.mock_starting_balance)
                self.mock_token_positions = state.get('positions', {})
                self.mock_position_entry_time = {k: float(v) for k, v in state.get('entry_times', {}).items()}
                self.mock_position_entry_sol = {k: float(v) for k, v in state.get('entry_sol', {}).items()}
                self.mock_trades_history = state.get('trades_history', [])
                
                logger.info("mock_state_loaded", 
                    balance=f"{self.mock_balance:.4f}",
                    positions=len([p for p in self.mock_token_positions.values() if p > 0])
                )
        except Exception as e:
            logger.warning("mock_state_load_error", error=str(e))
    
    def _save_mock_state(self) -> None:
        """Save mock trading state to file for persistence."""
        try:
            state = {
                'balance': self.mock_balance,
                'starting_balance': self.mock_starting_balance,
                'positions': self.mock_token_positions,
                'entry_times': self.mock_position_entry_time,
                'entry_sol': self.mock_position_entry_sol,
                'trades_history': self.mock_trades_history[-100:],  # Keep last 100 trades
                'last_updated': datetime.now().isoformat(),
                'pnl': self.mock_balance - self.mock_starting_balance
            }
            
            with open(self.mock_state_file, 'w') as f:
                json.dump(state, f, indent=2)
                
        except Exception as e:
            logger.warning("mock_state_save_error", error=str(e))
    
    def get_dashboard_state(self) -> Dict:
        """Get current state for dashboard display."""
        active_positions = []
        for mint, tokens in self.mock_token_positions.items():
            if tokens > 0:
                entry_sol = self.mock_position_entry_sol.get(mint, 0)
                entry_time = self.mock_position_entry_time.get(mint, 0)
                age_minutes = (time.time() - entry_time) / 60 if entry_time else 0
                active_positions.append({
                    'token': mint[:8] + '...',
                    'full_mint': mint,
                    'tokens': tokens,
                    'entry_sol': entry_sol,
                    'age_minutes': round(age_minutes, 1)
                })
        
        return {
            'balance': round(self.mock_balance, 4),
            'starting_balance': round(self.mock_starting_balance, 4),
            'pnl': round(self.mock_balance - self.mock_starting_balance, 4),
            'pnl_percent': round((self.mock_balance / self.mock_starting_balance - 1) * 100, 2) if self.mock_starting_balance > 0 else 0,
            'active_positions': active_positions,
            'position_count': len(active_positions),
            'max_positions': self.config.max_positions,
            'recent_trades': self.mock_trades_history[-20:],
            'last_updated': datetime.now().isoformat()
        }
    
    async def _on_transaction(self, tx: WalletTransaction) -> None:
        """Called when a new transaction is detected from a target wallet."""
        self.stats.total_detected += 1
        
        # Parse the transaction
        swap = self.parser.parse_transaction(tx.raw_tx, tx.wallet)
        
        if not swap:
            logger.debug("no_swap_detected", signature=tx.signature[:16])
            return
        
        logger.info(
            "swap_detected",
            wallet=tx.wallet[:8] + "...",
            type=swap.swap_type.value,
            token=swap.token_mint[:8] + "...",
            sol=f"{swap.sol_value:.4f}",
            dex=swap.dex
        )
        
        # If trader sells a token we hold, copy the sell!
        if swap.is_sell and self.position_manager and self.position_manager.has_position(swap.token_mint):
            logger.info(
                "copying_trader_sell",
                token=swap.token_mint[:8] + "...",
                message="Trader sold, we're selling too!"
            )
            from .position_manager import ExitReason
            result = await self.position_manager.trigger_sell(swap.token_mint, ExitReason.COPIED_SELL)
            if result.success:
                self.stats.total_sol_received += result.sol_received
                logger.info("copied_sell_success", sol_received=f"{result.sol_received:.4f}")
            else:
                logger.warning("copied_sell_failed", error=result.error)
            return
        
        # Decide whether to copy buy
        should_copy, reason = self._should_copy(swap)
        
        if not should_copy:
            self.stats.total_skipped += 1
            logger.info("skip_copy", reason=reason)
            return
        
        # Execute the copy trade (buy)
        result = await self._execute_copy(swap)
        
        if result.success:
            self.stats.total_copied += 1
            logger.info(
                "copy_success",
                signature=result.signature[:16] if result.signature else "none",
                sol_amount=f"{result.our_sol_amount / 1e9:.4f}"
            )
        else:
            self.stats.total_failed += 1
            logger.warning("copy_failed", error=result.error)
    
    def _should_copy(self, swap: ParsedSwap) -> tuple[bool, str]:
        """Determine if we should copy this swap."""
        
        # For buys, check position limits (but allow stacking same token)
        if swap.is_buy:
            # Check sell cooldown - don't buy tokens the trader just sold
            # This prevents us from getting out of sync (buying after they exit)
            if swap.token_mint in self.trader_sold_cooldown:
                cooldown_elapsed = time.time() - self.trader_sold_cooldown[swap.token_mint]
                if cooldown_elapsed < self.sell_cooldown_seconds:
                    remaining = self.sell_cooldown_seconds - cooldown_elapsed
                    return False, f"sell_cooldown_active ({remaining:.0f}s remaining)"
                else:
                    # Cooldown expired, remove from tracking
                    del self.trader_sold_cooldown[swap.token_mint]
            
            # Check if we can open more positions (only for NEW tokens)
            if self.position_manager:
                has_token = self.position_manager.has_position(swap.token_mint)
                if not has_token and not self.position_manager.can_open_position():
                    return False, f"max_positions_reached ({self.config.max_positions})"
            # Allow stacking - can buy more of same token (removed already_holding_token check)
        
        # For sells, only copy if we hold the token (handled by position manager)
        if swap.is_sell and not self.copy_sells:
            return False, "sell_disabled"
        
        # Check minimum SOL value (only for BUYS - always allow sells)
        if swap.is_buy and swap.sol_value < self.min_sol_per_trade:
            return False, f"below_min_sol ({swap.sol_value:.4f} < {self.min_sol_per_trade})"
        
        # Don't RE-BUY the same token too frequently (but always allow sells)
        if swap.is_buy and swap.token_mint in self.recent_copies:
            return False, "recently_copied"
        
        return True, "ok"
    
    async def _execute_copy(self, swap: ParsedSwap) -> CopyTradeResult:
        """Execute a copy of the detected swap."""
        try:
            # FAST PATH for sells - skip balance calculations, AGGRESSIVE RETRIES
            if not swap.is_buy:
                token_balance = await self._get_token_balance(swap.token_mint)
                if token_balance == 0:
                    logger.debug("no_tokens_to_sell", token=swap.token_mint[:8])
                    # Track this sell - don't buy this token for a cooldown period
                    # This prevents us from buying right after trader exits
                    self.trader_sold_cooldown[swap.token_mint] = time.time()
                    logger.info(
                        "sell_cooldown_started",
                        token=swap.token_mint[:8],
                        cooldown_seconds=self.sell_cooldown_seconds,
                        reason="trader_sold_but_we_had_no_position"
                    )
                    return CopyTradeResult(success=False, error="no_tokens_to_sell", original_swap=swap)
                
                logger.info(
                    "fast_sell",
                    token=swap.token_mint[:8],
                    our_balance=token_balance,
                    their_sol=f"{swap.sol_value:.4f}"
                )
                
                # Detect if this is a pump.fun token
                is_pumpfun_sell = swap.dex == "pump.fun"
                
                if self.mock_trading:
                    return self._simulate_mock_sell(swap, token_balance)

                # AGGRESSIVE RETRY LOOP with exponential backoff
                max_retries = 5
                result = None
                for attempt in range(max_retries):
                    if is_pumpfun_sell:
                        # Use Pump.fun API for bonding curve sells
                        # Estimate SOL value from token balance (rough estimate)
                        estimated_sol = token_balance / 1e9 * 0.00001  # Very rough, will be adjusted by API
                        result = await self._execute_pumpfun_swap(
                            token_mint=swap.token_mint,
                            sol_amount=estimated_sol,
                            is_buy=False
                        )
                    else:
                        # Use Jupiter for Raydium/other DEXes
                        result = await self._execute_swap(
                            input_mint=swap.token_mint,
                            output_mint=NATIVE_SOL,
                            amount=token_balance
                        )
                    
                    if result.success:
                        self.stats.total_sol_received += swap.sol_value * 0.01  # Estimate
                        trade_logger.log_sell(
                            token_mint=swap.token_mint,
                            token_symbol=swap.token_symbol,
                            sol_received=0,
                            tokens_sold=token_balance,
                            our_signature=result.signature or "",
                            trigger="copied_sell",
                            success=True
                        )
                        logger.info("sell_success", token=swap.token_mint[:8], attempt=attempt+1)
                        return result
                    
                    # Exponential backoff: 0.5s, 1s, 2s, 4s, 8s
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        "sell_retry",
                        token=swap.token_mint[:8],
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        next_retry_sec=delay,
                        error=result.error if result else "unknown"
                    )
                    await asyncio.sleep(delay)
                
                # All retries failed - add to retry queue for background retries
                logger.error("sell_failed_queuing_retry", token=swap.token_mint[:8])
                if self.position_manager:
                    self.position_manager.queue_failed_sell(swap.token_mint, token_balance)
                
                return result or CopyTradeResult(success=False, error="sell_failed_all_retries", original_swap=swap)
            
            # BUYS: Check all token filters (market cap, age, liquidity, volume, price change, txns)
            # For pump.fun tokens, use Pump.fun API instead of DexScreener
            is_pumpfun = swap.dex == "pump.fun"
            
            # TRUST TRADER MODE: Skip all filters for pump.fun tokens
            if is_pumpfun and self.trust_trader_pumpfun:
                logger.info(
                    "trust_trader_pumpfun",
                    token=swap.token_mint[:8],
                    sol=f"{swap.sol_value:.4f}",
                    message="Skipping filters - trusting trader for pump.fun token"
                )
                # Skip directly to trade execution (no filters)
                market_cap = 0
                age_minutes = 0
                liquidity = 0
                volume_24h = 0
                price_change_1h = 0
                txns_1h = 0
            elif is_pumpfun:
                # Try Pump.fun API first, then DexScreener as fallback
                market_cap, age_minutes = await self._get_pumpfun_token_info(swap.token_mint)
                liquidity = market_cap * 0.1  # Pump.fun uses bonding curve, estimate ~10% as liquidity
                volume_24h = 0
                price_change_1h = 0
                txns_1h = 100  # Assume active if trader is buying
                
                # If Pump.fun API failed, try DexScreener as fallback
                if market_cap == 0:
                    logger.debug("pumpfun_api_failed_trying_dexscreener", token=swap.token_mint[:8])
                    market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h = await self._get_token_info(swap.token_mint)
                
                # If still no data, skip
                if market_cap == 0 and age_minutes == 0:
                    logger.info(
                        "skipping_unknown_token",
                        token=swap.token_mint[:8],
                        reason="no_data_available"
                    )
                    return CopyTradeResult(
                        success=False,
                        error="token_unknown (not found on pump.fun or DexScreener)",
                        original_swap=swap
                    )
                
                logger.info(
                    "pumpfun_token_info",
                    token=swap.token_mint[:8],
                    market_cap=f"${market_cap:,.0f}",
                    liquidity=f"${liquidity:,.0f}",
                    age=f"{age_minutes:.1f}m"
                )
            else:
                # Use DexScreener for other DEXes
                market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h = await self._get_token_info(swap.token_mint)
                
                # If not on DexScreener, skip (unless trusting trader)
                if market_cap == 0 and age_minutes == 0:
                    if self.trust_trader_pumpfun:  # Trust trader mode applies to all
                        logger.info(
                            "trust_trader_unknown_token",
                            token=swap.token_mint[:8],
                            message="Token not on DexScreener but trusting trader"
                        )
                        # Set defaults for unknown token
                        market_cap = 100000
                        age_minutes = 1
                        liquidity = 10000
                        volume_24h = 1000
                        price_change_1h = 0
                        txns_1h = 100
                    else:
                        logger.info(
                            "skipping_unknown_token",
                            token=swap.token_mint[:8],
                            reason="not_on_dexscreener"
                        )
                        return CopyTradeResult(
                            success=False,
                            error="token_unknown (not on DexScreener yet)",
                            original_swap=swap
                        )
            
            # Skip all filters in trust trader mode
            skip_filters = self.trust_trader_pumpfun
            
            # Check token age
            if not skip_filters and self.min_token_age_minutes > 0 and age_minutes < self.min_token_age_minutes:
                logger.info(
                    "skipping_new_token",
                    token=swap.token_mint[:8],
                    age=f"{age_minutes:.1f}m",
                    min_age=f"{self.min_token_age_minutes}m"
                )
                return CopyTradeResult(
                    success=False,
                    error=f"token_too_new ({age_minutes:.1f}m < {self.min_token_age_minutes}m)",
                    original_swap=swap
                )
            
            # Check market cap
            if not skip_filters and self.min_market_cap_usd > 0 and market_cap < self.min_market_cap_usd:
                logger.info(
                    "skipping_low_mcap",
                    token=swap.token_mint[:8],
                    market_cap=f"${market_cap:,.0f}",
                    min_required=f"${self.min_market_cap_usd:,.0f}"
                )
                return CopyTradeResult(
                    success=False,
                    error=f"market_cap_too_low (${market_cap:,.0f} < ${self.min_market_cap_usd:,.0f})",
                    original_swap=swap
                )
            
            # Check liquidity - CRITICAL for being able to sell!
            if not skip_filters and self.min_liquidity_usd > 0 and liquidity < self.min_liquidity_usd:
                logger.info(
                    "skipping_low_liquidity",
                    token=swap.token_mint[:8],
                    liquidity=f"${liquidity:,.0f}",
                    min_required=f"${self.min_liquidity_usd:,.0f}"
                )
                return CopyTradeResult(
                    success=False,
                    error=f"liquidity_too_low (${liquidity:,.0f} < ${self.min_liquidity_usd:,.0f})",
                    original_swap=swap
                )
            
            # Check 24h volume - indicates trading activity
            if not skip_filters and self.min_volume_24h_usd > 0 and volume_24h < self.min_volume_24h_usd:
                logger.info(
                    "skipping_low_volume",
                    token=swap.token_mint[:8],
                    volume_24h=f"${volume_24h:,.0f}",
                    min_required=f"${self.min_volume_24h_usd:,.0f}"
                )
                return CopyTradeResult(
                    success=False,
                    error=f"volume_too_low (${volume_24h:,.0f} < ${self.min_volume_24h_usd:,.0f})",
                    original_swap=swap
                )
            
            # Check if token already pumped too much - avoid buying tops!
            if not skip_filters and self.max_price_change_1h_pct > 0 and price_change_1h > self.max_price_change_1h_pct:
                logger.info(
                    "skipping_already_pumped",
                    token=swap.token_mint[:8],
                    price_change_1h=f"+{price_change_1h:.0f}%",
                    max_allowed=f"+{self.max_price_change_1h_pct:.0f}%"
                )
                return CopyTradeResult(
                    success=False,
                    error=f"already_pumped (+{price_change_1h:.0f}% > +{self.max_price_change_1h_pct:.0f}%)",
                    original_swap=swap
                )
            
            # Check minimum transactions - ensure active trading
            if not skip_filters and self.min_txns_1h > 0 and txns_1h < self.min_txns_1h:
                logger.info(
                    "skipping_low_activity",
                    token=swap.token_mint[:8],
                    txns_1h=txns_1h,
                    min_required=self.min_txns_1h
                )
                return CopyTradeResult(
                    success=False,
                    error=f"low_activity ({txns_1h} txns < {self.min_txns_1h} min)",
                    original_swap=swap
                )
            
            logger.info(
                "token_filters_passed",
                token=swap.token_mint[:8],
                market_cap=f"${market_cap:,.0f}",
                liquidity=f"${liquidity:,.0f}",
                volume_24h=f"${volume_24h:,.0f}",
                price_change_1h=f"{price_change_1h:+.0f}%",
                txns_1h=txns_1h,
                age=f"{age_minutes:.1f}m"
            )
            
            # BUYS: Check holder distribution filters (using RugCheck API)
            # Skip for pump.fun tokens - they're too new for RugCheck data
            if not is_pumpfun and (self.max_top10_holders_pct > 0 or self.max_dev_holdings_pct > 0 or self.min_holders_count > 0):
                top10_pct, dev_pct, holders_count = await self._get_holder_info(swap.token_mint)
                
                # Only apply filters if we got data (0 means API failed/no data)
                if top10_pct > 0 or holders_count > 0:
                    # Check top 10 holders concentration
                    if self.max_top10_holders_pct > 0 and top10_pct > self.max_top10_holders_pct:
                        logger.info(
                            "skipping_concentrated_holdings",
                            token=swap.token_mint[:8],
                            top10_pct=f"{top10_pct:.1f}%",
                            max_allowed=f"{self.max_top10_holders_pct:.0f}%"
                        )
                        return CopyTradeResult(
                            success=False,
                            error=f"top10_holders_too_high ({top10_pct:.1f}% > {self.max_top10_holders_pct:.0f}%)",
                            original_swap=swap
                        )
                    
                    # Check dev holdings
                    if self.max_dev_holdings_pct > 0 and dev_pct > self.max_dev_holdings_pct:
                        logger.info(
                            "skipping_high_dev_holdings",
                            token=swap.token_mint[:8],
                            dev_pct=f"{dev_pct:.1f}%",
                            max_allowed=f"{self.max_dev_holdings_pct:.0f}%"
                        )
                        return CopyTradeResult(
                            success=False,
                            error=f"dev_holdings_too_high ({dev_pct:.1f}% > {self.max_dev_holdings_pct:.0f}%)",
                            original_swap=swap
                        )
                    
                    # Check minimum holders count
                    if self.min_holders_count > 0 and holders_count < self.min_holders_count:
                        logger.info(
                            "skipping_low_holders",
                            token=swap.token_mint[:8],
                            holders=holders_count,
                            min_required=self.min_holders_count
                        )
                        return CopyTradeResult(
                            success=False,
                            error=f"too_few_holders ({holders_count} < {self.min_holders_count})",
                            original_swap=swap
                        )
                    
                    logger.info(
                        "holder_filters_passed",
                        token=swap.token_mint[:8],
                        top10_pct=f"{top10_pct:.1f}%",
                        dev_pct=f"{dev_pct:.1f}%",
                        holders=holders_count
                    )
            
            # BUYS: Full calculation path
            if self.mock_trading:
                balance_sol = self.mock_balance
            else:
                balance = await self.rpc.get_balance(self.wallet.pubkey())
                balance_sol = balance / 1e9
            
            # Calculate fee reserve needed for existing + new positions
            if self.mock_trading:
                current_positions = len([p for p in self.mock_token_positions.values() if p > 0])
            else:
                current_positions = len(self.position_manager.positions) if self.position_manager else 0
            
            # Check max positions limit
            if current_positions >= self.max_positions:
                return CopyTradeResult(
                    success=False,
                    error=f"max_positions_reached ({current_positions}/{self.max_positions})",
                    original_swap=swap
                )
            
            total_fee_reserve = self.fee_reserve + (self.exit_fee_reserve * (current_positions + 1))
            
            # Available balance after fee reserve
            available_sol = max(0, balance_sol - total_fee_reserve)
            
            logger.debug(
                "balance_calculation",
                balance=f"{balance_sol:.4f}",
                positions=current_positions,
                fee_reserve=f"{total_fee_reserve:.4f}",
                available=f"{available_sol:.4f}"
            )
            
            # Calculate trade size
            if self.copy_proportional:
                # Proportional: match their percentage
                # Get trader's balance (cache it to avoid too many RPC calls)
                if swap.wallet not in self.trader_balances:
                    try:
                        from solders.pubkey import Pubkey
                        trader_balance = await self.rpc.get_balance(Pubkey.from_string(swap.wallet))
                        self.trader_balances[swap.wallet] = trader_balance / 1e9
                    except:
                        self.trader_balances[swap.wallet] = 10.0  # Default assumption
                
                trader_total = self.trader_balances[swap.wallet]
                their_percentage = swap.sol_value / trader_total if trader_total > 0 else 0.1
                
                # Apply their percentage to our available balance
                # BUT ensure minimum floor (at least enough to meet min_sol or 15% of available)
                min_percentage = max(self.min_sol_per_trade / available_sol, 0.15) if available_sol > 0 else 0.15
                effective_percentage = max(their_percentage, min_percentage)
                
                trade_sol = min(
                    available_sol * effective_percentage,  # Match their % (with floor)
                    available_sol * 0.5,                   # Never more than 50% on one trade
                    self.max_sol_per_trade                 # Hard cap
                )
                
                logger.info(
                    "proportional_sizing",
                    their_pct=f"{their_percentage*100:.1f}%",
                    effective_pct=f"{effective_percentage*100:.1f}%",
                    their_sol=f"{swap.sol_value:.4f}",
                    our_sol=f"{trade_sol:.4f}",
                    our_available=f"{available_sol:.4f}"
                )
            else:
                # Fixed: use configured percentage
                trade_sol = min(
                    available_sol * self.copy_percentage,
                    self.max_sol_per_trade,
                    swap.sol_value * 2
                )
            
            # Round to avoid floating point precision issues (0.04999 -> 0.05)
            trade_sol = round(trade_sol, 4)
            
            # Ensure minimum trade size if we have enough balance
            if trade_sol < self.min_sol_per_trade:
                if available_sol >= self.min_sol_per_trade:
                    trade_sol = self.min_sol_per_trade  # Bump up to minimum
                else:
                    return CopyTradeResult(
                        success=False,
                        error=f"insufficient_balance ({available_sol:.4f} SOL < {self.min_sol_per_trade} min)",
                        original_swap=swap
                    )
            
            trade_lamports = int(trade_sol * 1e9)
            
            logger.info(
                "executing_copy",
                type=swap.swap_type.value,
                token=swap.token_mint[:8] + "...",
                our_sol=f"{trade_sol:.4f}",
                their_sol=f"{swap.sol_value:.4f}",
                dex=swap.dex
            )
            
            # Buy: Use appropriate API based on DEX
            if is_pumpfun:
                # Use Pump.fun API for bonding curve tokens
                if self.mock_trading:
                    result = self._simulate_mock_buy(swap, trade_sol)
                else:
                    result = await self._execute_pumpfun_swap(
                        token_mint=swap.token_mint,
                        sol_amount=trade_sol,
                        is_buy=True
                    )
            else:
                if self.mock_trading:
                    result = self._simulate_mock_buy(swap, trade_sol)
                else:
                    # Use Jupiter for Raydium/other DEXes
                    result = await self._execute_swap(
                        input_mint=NATIVE_SOL,
                        output_mint=swap.token_mint,
                        amount=trade_lamports
                    )
            
            if result.success:
                # For BUYS: Track to avoid rapid re-buying (30 sec cooldown)
                # For SELLS: Don't track - allow multiple sell attempts
                if swap.is_buy:
                    self.recent_copies.add(swap.token_mint)
                    asyncio.create_task(self._clear_recent_copy(swap.token_mint, 30))
                
                if swap.is_buy:
                    self.stats.total_sol_spent += trade_sol
                    
                    # Register position for auto-sell management
                    if self.position_manager:
                        # Estimate tokens received from the swap
                        # In reality, we'd parse this from the transaction result
                        estimated_tokens = int(trade_lamports * 1000)  # Placeholder
                        self.position_manager.add_position(
                            token_mint=swap.token_mint,
                            token_symbol=swap.token_symbol,
                            our_sol=trade_sol,
                            our_tokens=estimated_tokens,
                            our_signature=result.signature,
                            copied_from=swap.wallet,
                            dex="pump.fun" if is_pumpfun else swap.dex
                        )
                    
                    # Log the trade for analysis
                    trade_logger.log_buy(
                        token_mint=swap.token_mint,
                        token_symbol=swap.token_symbol,
                        our_sol=trade_sol,
                        our_tokens=estimated_tokens,
                        our_signature=result.signature,
                        copied_wallet=swap.wallet,
                        their_sol=swap.sol_value,
                        their_signature=swap.signature,
                        their_timestamp=None,
                        delay_seconds=(datetime.utcnow() - datetime.utcnow()).total_seconds(),  # TODO: track actual delay
                        success=True
                    )
                else:
                    self.stats.total_sol_received += trade_sol
            
            result.original_swap = swap
            result.our_sol_amount = trade_lamports
            return result
            
        except Exception as e:
            return CopyTradeResult(
                success=False,
                error=str(e),
                original_swap=swap
            )
    
    async def _execute_swap(
        self, 
        input_mint: str, 
        output_mint: str, 
        amount: int
    ) -> CopyTradeResult:
        """Execute a swap via Jupiter."""
        try:
            # Get quote
            quote_params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(self.config.slippage_bps)
            }
            
            async with self.session.get(JUPITER_QUOTE_API, params=quote_params) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return CopyTradeResult(success=False, error=f"quote_failed: {error_text}")
                quote = await resp.json()
            
            # Get swap transaction with HIGH priority fees for fast execution
            swap_data = {
                "quoteResponse": quote,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": 500000  # Very high priority ~0.0005 SOL for fastest execution
            }
            
            async with self.session.post(JUPITER_SWAP_API, json=swap_data) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return CopyTradeResult(success=False, error=f"swap_failed: {error_text}")
                swap_response = await resp.json()
            
            # Sign and send transaction
            swap_tx_base64 = swap_response.get("swapTransaction")
            if not swap_tx_base64:
                return CopyTradeResult(success=False, error="no_swap_transaction")
            
            # Decode, sign, and send
            import base64
            from solders.transaction import VersionedTransaction
            
            tx_bytes = base64.b64decode(swap_tx_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Sign the transaction
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            # Send
            signature = await self.rpc.send_transaction(signed_tx)
            
            return CopyTradeResult(success=True, signature=signature)
            
        except Exception as e:
            return CopyTradeResult(success=False, error=str(e))
    
    async def _execute_pumpfun_swap(
        self,
        token_mint: str,
        sol_amount: float,
        is_buy: bool,
        sell_percentage: int = 100  # For sells: percentage of holdings to sell (100 = all)
    ) -> CopyTradeResult:
        """Execute a swap on Pump.fun's bonding curve."""
        try:
            import base64
            from solders.transaction import VersionedTransaction
            
            action = "buy" if is_buy else "sell"
            
            # Request transaction from PumpPortal
            # Use high slippage for pump.fun (tokens move fast) - minimum 15%
            pumpfun_slippage = max(self.config.slippage_bps / 100, 15)
            
            if is_buy:
                payload = {
                    "publicKey": str(self.wallet.pubkey()),
                    "action": action,
                    "mint": token_mint,
                    "denominatedInSol": "true",
                    "amount": sol_amount,
                    "slippage": pumpfun_slippage,
                    "priorityFee": 0.001,  # Higher priority for faster execution
                    "pool": "pump"
                }
            else:
                # For sells, use percentage of holdings
                payload = {
                    "publicKey": str(self.wallet.pubkey()),
                    "action": action,
                    "mint": token_mint,
                    "denominatedInSol": "false",
                    "amount": f"{sell_percentage}%",
                    "slippage": pumpfun_slippage,
                    "priorityFee": 0.001,
                    "pool": "pump"
                }
            
            logger.info(
                "pumpfun_swap_request",
                action=action,
                token=token_mint[:8],
                sol=f"{sol_amount:.4f}"
            )
            
            async with self.session.post(PUMPFUN_API, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return CopyTradeResult(success=False, error=f"pumpfun_api_failed: {error_text}")
                
                # Response is the raw transaction bytes
                tx_bytes = await resp.read()
            
            # Deserialize and sign the transaction
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            # Send the transaction
            signature = await self.rpc.send_transaction(signed_tx)
            
            logger.info(
                "pumpfun_swap_success",
                action=action,
                token=token_mint[:8],
                signature=str(signature)[:16] if signature else None
            )
            
            return CopyTradeResult(success=True, signature=signature)
            
        except Exception as e:
            logger.error("pumpfun_swap_error", error=str(e))
            return CopyTradeResult(success=False, error=f"pumpfun_error: {str(e)}")
    
    def _simulate_mock_buy(self, swap: 'ParsedSwap', trade_sol: float) -> 'CopyTradeResult':
        """Simulate a buy trade without executing on-chain."""
        # Estimate token amount received (use swap data as reference)
        if swap.sol_value > 0 and swap.token_amount > 0:
            # Extrapolate based on trader's swap ratio
            estimated_tokens = int((trade_sol / swap.sol_value) * swap.token_amount)
        else:
            # Fallback: assume 1M tokens per SOL (rough estimate)
            estimated_tokens = int(trade_sol * 1_000_000)
        
        # Deduct SOL from mock balance
        self.mock_balance -= trade_sol
        
        # Add tokens to mock positions
        current_tokens = self.mock_token_positions.get(swap.token_mint, 0)
        self.mock_token_positions[swap.token_mint] = current_tokens + estimated_tokens
        
        # Track entry time and SOL for new positions
        if swap.token_mint not in self.mock_position_entry_time:
            self.mock_position_entry_time[swap.token_mint] = time.time()
            self.mock_position_entry_sol[swap.token_mint] = trade_sol
        else:
            # Averaging in - add to entry SOL
            self.mock_position_entry_sol[swap.token_mint] = self.mock_position_entry_sol.get(swap.token_mint, 0) + trade_sol
        
        logger.info(
            "mock_buy",
            token=swap.token_mint[:8],
            sol_spent=f"{trade_sol:.4f}",
            tokens_received=estimated_tokens,
            new_balance=f"{self.mock_balance:.4f}",
            total_tokens=self.mock_token_positions[swap.token_mint]
        )
        
        # Track trade in history
        self.mock_trades_history.append({
            'type': 'buy',
            'token': swap.token_mint[:8],
            'full_mint': swap.token_mint,
            'sol': trade_sol,
            'tokens': estimated_tokens,
            'balance_after': self.mock_balance,
            'timestamp': datetime.now().isoformat()
        })
        
        # Save state after each trade
        self._save_mock_state()
        
        return CopyTradeResult(
            success=True,
            signature=f"MOCK_BUY_{swap.signature[:8]}",
            mock=True
        )
    
    def _simulate_mock_sell(self, swap: 'ParsedSwap', token_balance: int) -> 'CopyTradeResult':
        """Simulate a sell trade without executing on-chain."""
        # Estimate SOL received based on trader's swap ratio
        if swap.token_amount > 0:
            sol_received = (token_balance / swap.token_amount) * swap.sol_value
        else:
            # Fallback: assume same ratio as buy
            sol_received = token_balance / 1_000_000
        
        # Calculate P&L for this trade (get entry_sol BEFORE removing)
        entry_sol = self.mock_position_entry_sol.get(swap.token_mint, 0)
        pnl = sol_received - entry_sol
        
        # Add SOL to mock balance
        self.mock_balance += sol_received
        
        # Remove tokens from mock positions and tracking
        self.mock_token_positions[swap.token_mint] = 0
        self.mock_position_entry_time.pop(swap.token_mint, None)
        self.mock_position_entry_sol.pop(swap.token_mint, None)
        
        logger.info(
            "mock_sell",
            token=swap.token_mint[:8],
            tokens_sold=token_balance,
            sol_received=f"{sol_received:.4f}",
            entry_sol=f"{entry_sol:.4f}",
            pnl=f"{pnl:+.4f}",
            new_balance=f"{self.mock_balance:.4f}"
        )
        
        # Track trade in history
        self.mock_trades_history.append({
            'type': 'sell',
            'token': swap.token_mint[:8],
            'full_mint': swap.token_mint,
            'sol': sol_received,
            'tokens': token_balance,
            'entry_sol': entry_sol,
            'pnl': pnl,
            'balance_after': self.mock_balance,
            'timestamp': datetime.now().isoformat()
        })
        
        # Save state after each trade
        self._save_mock_state()
        
        return CopyTradeResult(
            success=True,
            signature=f"MOCK_SELL_{swap.signature[:8]}",
            mock=True
        )
    
    async def _get_token_balance(self, mint: str) -> int:
        """Get token balance for our wallet by finding the associated token account."""
        if self.mock_trading:
            return self.mock_token_positions.get(mint, 0)
        
        try:
            from solders.pubkey import Pubkey
            
            # SPL Token Program ID
            TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            
            # Get all token accounts for our wallet
            wallet_pubkey = self.wallet.pubkey()
            
            # Use getTokenAccountsByOwner RPC call
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    str(wallet_pubkey),
                    {"mint": mint},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if result and "value" in result:
                accounts = result["value"]
                if not accounts:
                    return 0
                # Get the token amount from the first account
                account_data = accounts[0].get("account", {}).get("data", {})
                parsed = account_data.get("parsed", {}).get("info", {})
                token_amount = parsed.get("tokenAmount", {})
                amount = int(token_amount.get("amount", 0))
                
                if amount > 0:
                    logger.info(
                        "token_balance_found",
                        token=mint[:8],
                        amount=amount
                    )
                return amount
            
            return 0
        except Exception as e:
            logger.debug("get_token_balance_error", mint=mint[:8], error=str(e))
            return 0
    
    async def _get_token_info(self, mint: str) -> tuple[float, float, float, float, float, int]:
        """Get market cap, token age, liquidity, volume, price change and txn count using DexScreener API.
        
        Returns:
            tuple: (market_cap_usd, age_minutes, liquidity_usd, volume_24h_usd, price_change_1h_pct, txns_1h)
        """
        import time
        
        # Check cache (valid for 60 seconds)
        if mint in self.token_info_cache:
            cached_cap, cached_age, cached_liq, cached_vol, cached_price_chg, cached_txns, cached_time = self.token_info_cache[mint]
            if time.time() - cached_time < 60:
                # Adjust age for time passed since cache
                adjusted_age = cached_age + (time.time() - cached_time) / 60
                return cached_cap, adjusted_age, cached_liq, cached_vol, cached_price_chg, cached_txns
        
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        # Get the best pair's stats (highest liquidity)
                        market_cap = 0
                        oldest_age = 0
                        total_liquidity = 0
                        total_volume_24h = 0
                        price_change_1h = 0
                        total_txns_1h = 0
                        best_pair = None
                        
                        for pair in pairs:
                            mc = pair.get("marketCap") or pair.get("fdv") or 0
                            if mc > market_cap:
                                market_cap = mc
                                best_pair = pair  # Track the main pair
                            
                            # Sum up liquidity across all pairs
                            liq = pair.get("liquidity", {}).get("usd", 0) or 0
                            total_liquidity += liq
                            
                            # Sum up 24h volume across all pairs
                            vol = pair.get("volume", {}).get("h24", 0) or 0
                            total_volume_24h += vol
                            
                            # Sum up 1h transactions (buys + sells)
                            txns = pair.get("txns", {}).get("h1", {})
                            buys_1h = txns.get("buys", 0) or 0
                            sells_1h = txns.get("sells", 0) or 0
                            total_txns_1h += buys_1h + sells_1h
                            
                            # Get pair creation time
                            created_at = pair.get("pairCreatedAt")
                            if created_at:
                                age_ms = time.time() * 1000 - created_at
                                age_minutes = age_ms / 60000
                                if age_minutes > oldest_age:
                                    oldest_age = age_minutes
                        
                        # Get 1h price change from best pair
                        if best_pair:
                            price_change_1h = best_pair.get("priceChange", {}).get("h1", 0) or 0
                        
                        self.token_info_cache[mint] = (market_cap, oldest_age, total_liquidity, total_volume_24h, price_change_1h, total_txns_1h, time.time())
                        return market_cap, oldest_age, total_liquidity, total_volume_24h, price_change_1h, total_txns_1h
            
            return 0, 0, 0, 0, 0, 0
        except Exception as e:
            logger.debug("token_info_fetch_error", mint=mint[:8], error=str(e))
            return 0, 0, 0, 0, 0, 0
    
    async def _get_pumpfun_token_info(self, mint: str) -> tuple[float, float]:
        """Get token info from Pump.fun API.
        
        Returns:
            tuple: (market_cap_usd, age_minutes)
        """
        import time
        
        # Check cache (valid for 30 seconds for pump.fun - things move fast)
        cache_key = f"pumpfun_{mint}"
        if cache_key in self.token_info_cache:
            cached = self.token_info_cache[cache_key]
            if len(cached) >= 3 and time.time() - cached[2] < 30:
                return cached[0], cached[1]
        
        try:
            url = f"https://frontend-api.pump.fun/coins/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Get market cap in USD
                    market_cap = data.get("usd_market_cap", 0) or 0
                    
                    # Get token age from created_timestamp (in milliseconds)
                    created_ts = data.get("created_timestamp")
                    age_minutes = 0
                    if created_ts:
                        age_ms = time.time() * 1000 - created_ts
                        age_minutes = age_ms / 60000
                    
                    # Cache it
                    self.token_info_cache[cache_key] = (market_cap, age_minutes, time.time())
                    logger.debug("pumpfun_api_success", mint=mint[:8], market_cap=market_cap, age=age_minutes)
                    return market_cap, age_minutes
                else:
                    logger.debug("pumpfun_api_error", mint=mint[:8], status=resp.status)
            
            return 0, 0
        except Exception as e:
            logger.debug("pumpfun_info_fetch_error", mint=mint[:8], error=str(e))
            return 0, 0
    
    async def _get_holder_info(self, mint: str) -> tuple[float, float, int]:
        """Get holder distribution info using RugCheck API.
        
        Returns:
            tuple: (top10_holders_pct, dev_holdings_pct, holders_count)
        """
        import time
        
        # Check cache (valid for 5 minutes - holder data doesn't change fast)
        if mint in self.holder_info_cache:
            cached_top10, cached_dev, cached_holders, cached_time = self.holder_info_cache[mint]
            if time.time() - cached_time < 300:  # 5 minutes
                return cached_top10, cached_dev, cached_holders
        
        try:
            # RugCheck API
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Get top holders percentage
                    top_holders = data.get("topHolders", [])
                    top10_pct = 0
                    for i, holder in enumerate(top_holders[:10]):
                        top10_pct += holder.get("pct", 0)
                    
                    # Get creator/dev holdings
                    creator_pct = 0
                    creator = data.get("creator")
                    if creator:
                        creator_pct = creator.get("pct", 0) or 0
                    
                    # Also check for "insider" or high-risk holders
                    risks = data.get("risks", [])
                    for risk in risks:
                        if "creator" in risk.get("name", "").lower():
                            # Try to extract percentage from risk description
                            pass
                    
                    # Get total holders count
                    holders_count = data.get("holderCount", 0) or len(top_holders)
                    
                    self.holder_info_cache[mint] = (top10_pct, creator_pct, holders_count, time.time())
                    return top10_pct, creator_pct, holders_count
            
            return 0, 0, 0
        except Exception as e:
            logger.debug("holder_info_fetch_error", mint=mint[:8], error=str(e))
            return 0, 0, 0
    
    async def _clear_recent_copy(self, token_mint: str, delay: int) -> None:
        """Remove token from recent copies after delay."""
        await asyncio.sleep(delay)
        self.recent_copies.discard(token_mint)
    
    def _format_stats(self) -> Dict:
        """Format stats for logging."""
        return {
            "detected": self.stats.total_detected,
            "copied": self.stats.total_copied,
            "skipped": self.stats.total_skipped,
            "failed": self.stats.total_failed,
            "sol_spent": f"{self.stats.total_sol_spent:.4f}",
            "sol_received": f"{self.stats.total_sol_received:.4f}"
        }
    
    def get_stats(self) -> TradeStats:
        """Get current statistics."""
        return self.stats
