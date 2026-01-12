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

# Wallet-to-state-file mapping for multi-wallet tracking
WALLET_STATE_FILES = {
    '6mWEJG9LoRdto8TwTdZxmnJpkXpTsEerizcGiCNZvzXd': 'mock_state.json',  # Slingor
    'CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o': 'mock_state_cented.json',  # Cented
    '2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f': 'mock_state_cupsey.json',  # Cupsey
    '4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk': 'mock_state_jijo.json',  # Jijo
}

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
        state_file: str = 'mock_state.json',
    ):
        self.config = config
        self.target_wallets = target_wallets
        self.wallet = wallet_keypair
        self.rpc = rpc_client
        self.state_file = state_file
        
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
        self.skip_creator_tokens = config.skip_creator_tokens
        
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
        
        # Mock trading support - now per-wallet
        self.mock_trading = self.config.mock_trading
        self.trader_sold_cooldown: Dict[str, float] = {}  # mint -> timestamp when trader sold
        self.sell_cooldown_seconds = 60
        # Max age before abandoning
        self.mock_position_max_age_minutes = int(os.getenv('MOCK_MAX_POSITION_AGE_MINUTES', '10'))
        
        # Per-wallet state tracking
        self.wallet_states: Dict[str, dict] = {}  # wallet_address -> state dict
        
        # Load persisted state for all tracked wallets
        if self.mock_trading:
            self._load_all_wallet_states()
            total_positions = sum(
                len([p for p in ws.get('positions', {}).values() if p > 0])
                for ws in self.wallet_states.values()
            )
            logger.info(
                "mock_trading_enabled",
                tracked_wallets=len(self.target_wallets),
                total_open_positions=total_positions,
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
        """Check mock positions for stop-loss and rug detection across all wallets."""
        MIN_LIQUIDITY_USD = float(os.getenv('MOCK_MIN_LIQUIDITY_USD', '500'))
        MIN_MARKET_CAP_USD = float(os.getenv('MOCK_MIN_MARKET_CAP_USD', '2000'))
        STOP_LOSS_PCT = self.config.stop_loss_pct  # e.g. -35 means exit at -35%
        
        # Iterate over all tracked wallets
        for wallet, state in self.wallet_states.items():
            positions = state.get('positions', {})
            entry_sol_map = state.get('entry_sol', {})
            
            active_mints = [mint for mint, tokens in positions.items() if tokens > 0]
            if not active_mints:
                continue
            
            logger.debug(
                "mock_health_check",
                wallet=wallet[:8],
                active_positions=len(active_mints),
                min_liquidity=f"${MIN_LIQUIDITY_USD:,.0f}",
                min_mcap=f"${MIN_MARKET_CAP_USD:,.0f}",
                tokens=",".join([m[:8] for m in active_mints])
            )
            
            for mint in active_mints:
                try:
                    market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h = await self._get_token_info(mint)
                    
                    entry_sol = entry_sol_map.get(mint, 0)
                    tokens_held = positions.get(mint, 0)
                    reason = None
                    should_sell = False
                    current_value = 0
                    
                    # Get current position value for stop-loss check
                    if entry_sol > 0:
                        current_value = await self._get_mock_position_value(mint, tokens_held)
                        if current_value > 0:
                            pnl_pct = ((current_value - entry_sol) / entry_sol) * 100
                            
                            # Check stop loss (-35% by default)
                            if pnl_pct <= STOP_LOSS_PCT:
                                reason = f"stop_loss_triggered ({pnl_pct:.1f}% < {STOP_LOSS_PCT}%)"
                                should_sell = True
                    
                    # Check how long we've held this position
                    entry_times = state.get('entry_times', {})
                    entry_time_str = entry_times.get(mint)
                    hold_minutes = 0
                    if entry_time_str:
                        try:
                            entry_dt = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                            hold_minutes = (datetime.now(entry_dt.tzinfo) - entry_dt).total_seconds() / 60
                        except:
                            pass
                    
                    # Rug detection checks
                    if not should_sell:
                        if liquidity < MIN_LIQUIDITY_USD and liquidity > 0 and age_minutes > 10:
                            reason = f"rug_detected_low_liquidity (${liquidity:,.0f})"
                            should_sell = True
                            current_value = entry_sol * 0.1  # Assume 90% loss
                        elif market_cap < MIN_MARKET_CAP_USD and market_cap > 0 and age_minutes > 10:
                            reason = f"rug_detected_low_mcap (${market_cap:,.0f})"
                            should_sell = True
                            current_value = entry_sol * 0.1
                        elif market_cap == 0 and liquidity == 0 and age_minutes > 10:
                            reason = "rug_detected_not_on_dex"
                            should_sell = True
                            current_value = entry_sol * 0.05  # Assume 95% loss
                    
                    # Stale position fallback: if held >4 hours and no price, force sell as dead
                    if not should_sell and hold_minutes > 240 and current_value == 0:
                        reason = f"stale_position_no_price (held {hold_minutes/60:.1f}h)"
                        should_sell = True
                        current_value = entry_sol * 0.02  # Assume 98% loss for dead tokens
                    
                    if should_sell and reason:
                        await self._auto_mock_sell(wallet, mint, tokens_held, entry_sol, current_value, reason)
                        
                except Exception as e:
                    logger.debug("health_check_error", wallet=wallet[:8], token=mint[:8], error=str(e))
    
    async def _get_mock_position_value(self, mint: str, tokens_held: int) -> float:
        """Get current value of a mock position in SOL using DexScreener price."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        # Get price in native token (SOL) from highest liquidity pair
                        best_pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
                        price_native = float(best_pair.get("priceNative", 0) or 0)
                        
                        # Calculate current value in SOL
                        # price_native is price per token in SOL
                        # tokens_held is in smallest units (like lamports for SOL)
                        # Need to adjust for token decimals
                        decimals = 6  # Most SPL tokens use 6 decimals
                        token_amount = tokens_held / (10 ** decimals)
                        current_value = token_amount * price_native
                        return current_value
            return 0
        except Exception as e:
            logger.debug("get_position_value_error", mint=mint[:8], error=str(e))
            return 0
    
    async def _auto_mock_sell(self, wallet: str, mint: str, tokens_held: int, entry_sol: float, current_value: float, reason: str) -> None:
        """Perform automatic mock sell for stop-loss/take-profit/rug detection."""
        state = self._get_wallet_state(wallet)
        
        sol_received = current_value if current_value > 0 else 0
        pnl = sol_received - entry_sol
        
        # Calculate hold duration before removing entry_times
        entry_times = state.get('entry_times', {})
        entry_time = entry_times.get(mint, time.time())
        hold_seconds = time.time() - entry_time
        hold_minutes = hold_seconds / 60
        
        # Add SOL to wallet's mock balance
        state['balance'] = state.get('balance', 1.0) + sol_received
        
        # Remove tokens from wallet's mock positions and tracking
        positions = state.setdefault('positions', {})
        positions[mint] = 0
        state.get('entry_times', {}).pop(mint, None)
        state.get('entry_sol', {}).pop(mint, None)
        
        logger.warning(
            "mock_auto_sell",
            wallet=wallet[:8],
            token=mint[:8],
            tokens_sold=tokens_held,
            sol_received=f"{sol_received:.4f}",
            entry_sol=f"{entry_sol:.4f}",
            pnl=f"{pnl:+.4f}",
            reason=reason,
            new_balance=f"{state['balance']:.4f}"
        )
        
        # Track trade in history
        trades = state.setdefault('trades_history', [])
        trades.append({
            'type': 'auto_sell',
            'token': mint[:8],
            'full_mint': mint,
            'sol': sol_received,
            'tokens': tokens_held,
            'entry_sol': entry_sol,
            'pnl': pnl,
            'reason': reason,
            'balance_after': state['balance'],
            'timestamp': datetime.now().isoformat(),
            'hold_minutes': hold_minutes
        })
        
        # Save state for this wallet
        self._save_wallet_state(wallet)
    
    async def stop(self) -> None:
        """Stop the copy trader."""
        self.running = False
        
        # Save state for all wallets before stopping
        if self.mock_trading:
            self._save_all_wallet_states()
        
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
    
    def _load_all_wallet_states(self) -> None:
        """Load persisted mock trading state for all tracked wallets."""
        for wallet in self.target_wallets:
            state_file = WALLET_STATE_FILES.get(wallet, f'mock_state_{wallet[:8]}.json')
            try:
                if Path(state_file).exists():
                    with open(state_file, 'r') as f:
                        state = json.load(f)
                    
                    # Recalculate balance from trade history to fix any corruption
                    state = self._recalculate_balance_from_history(state)
                    
                    self.wallet_states[wallet] = state
                    logger.info("wallet_state_loaded", 
                        wallet=wallet[:8],
                        balance=f"{state.get('balance', 1.0):.4f}",
                        positions=len([p for p in state.get('positions', {}).values() if p > 0])
                    )
                else:
                    # Initialize fresh state for this wallet
                    self.wallet_states[wallet] = self._get_default_wallet_state()
                    logger.info("wallet_state_initialized", wallet=wallet[:8])
            except Exception as e:
                logger.warning("wallet_state_load_error", wallet=wallet[:8], error=str(e))
                self.wallet_states[wallet] = self._get_default_wallet_state()
    
    def _get_default_wallet_state(self) -> dict:
        """Get default state for a new wallet."""
        return {
            'balance': self.config.mock_balance_sol,
            'starting_balance': self.config.mock_balance_sol,
            'positions': {},
            'entry_times': {},
            'entry_sol': {},
            'trades_history': [],
            'last_updated': datetime.now().isoformat(),
            'pnl': 0
        }
    
    def _recalculate_balance_from_history(self, state: dict) -> dict:
        """Recalculate balance from trade history to fix any corruption."""
        starting_balance = state.get('starting_balance', 1.0)
        trades = state.get('trades_history', [])
        entry_sol = state.get('entry_sol', {})
        total_invested = sum(entry_sol.values())
        old_balance = state.get('balance', 1.0)
        
        # If no trades and no open positions, balance should equal starting balance
        if not trades and total_invested == 0:
            if abs(old_balance - starting_balance) > 0.01:
                logger.warning(
                    "balance_reset_no_trades",
                    old_balance=f"{old_balance:.4f}",
                    new_balance=f"{starting_balance:.4f}",
                    reason="no_trades_no_positions"
                )
                state['balance'] = starting_balance
                state['pnl'] = 0
            return state
        
        # Calculate balance by replaying all trades
        calculated_balance = starting_balance
        
        for trade in trades:
            trade_type = trade.get('type', '')
            sol_amount = trade.get('sol', 0)
            
            if trade_type == 'buy':
                calculated_balance -= sol_amount
            elif trade_type in ('sell', 'auto_sell'):
                calculated_balance += sol_amount
        
        # Only fix if there's a significant discrepancy (>0.01 SOL)
        if abs(calculated_balance - old_balance) > 0.01:
            logger.warning(
                "balance_recalculated",
                old_balance=f"{old_balance:.4f}",
                new_balance=f"{calculated_balance:.4f}",
                trades_count=len(trades),
                starting_balance=f"{starting_balance:.4f}"
            )
            state['balance'] = calculated_balance
            state['pnl'] = calculated_balance + total_invested - starting_balance
        
        return state
    
    def _get_wallet_state(self, wallet: str) -> dict:
        """Get state for a specific wallet, creating if needed."""
        if wallet not in self.wallet_states:
            self.wallet_states[wallet] = self._get_default_wallet_state()
        return self.wallet_states[wallet]
    
    def _save_wallet_state(self, wallet: str) -> None:
        """Save mock trading state to file for a specific wallet."""
        try:
            state = self.wallet_states.get(wallet, self._get_default_wallet_state())
            state['last_updated'] = datetime.now().isoformat()
            state['pnl'] = state.get('balance', 1.0) - state.get('starting_balance', 1.0)
            
            # Keep last 100 trades
            if 'trades_history' in state:
                state['trades_history'] = state['trades_history'][-100:]
            
            state_file = WALLET_STATE_FILES.get(wallet, f'mock_state_{wallet[:8]}.json')
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2)
                
        except Exception as e:
            logger.warning("wallet_state_save_error", wallet=wallet[:8], error=str(e))
    
    def _save_all_wallet_states(self) -> None:
        """Save state for all tracked wallets."""
        for wallet in self.wallet_states:
            self._save_wallet_state(wallet)
    
    def get_dashboard_state(self, wallet: str = None) -> Dict:
        """Get current state for dashboard display. If wallet specified, returns that wallet's state."""
        if wallet:
            state = self._get_wallet_state(wallet)
            positions = state.get('positions', {})
            entry_sol = state.get('entry_sol', {})
            entry_times = state.get('entry_times', {})
            balance = state.get('balance', 1.0)
            starting = state.get('starting_balance', 1.0)
            trades = state.get('trades_history', [])
        else:
            # Aggregate across all wallets (for backward compatibility)
            positions = {}
            entry_sol = {}
            entry_times = {}
            balance = 0
            starting = 0
            trades = []
            for ws in self.wallet_states.values():
                positions.update(ws.get('positions', {}))
                entry_sol.update(ws.get('entry_sol', {}))
                entry_times.update(ws.get('entry_times', {}))
                balance += ws.get('balance', 1.0)
                starting += ws.get('starting_balance', 1.0)
                trades.extend(ws.get('trades_history', []))
        
        active_positions = []
        for mint, tokens in positions.items():
            if tokens > 0:
                e_sol = entry_sol.get(mint, 0)
                e_time = entry_times.get(mint, 0)
                age_minutes = (time.time() - e_time) / 60 if e_time else 0
                active_positions.append({
                    'token': mint[:8] + '...',
                    'full_mint': mint,
                    'tokens': tokens,
                    'entry_sol': e_sol,
                    'age_minutes': round(age_minutes, 1)
                })
        
        return {
            'balance': round(balance, 4),
            'starting_balance': round(starting, 4),
            'pnl': round(balance - starting, 4),
            'pnl_percent': round((balance / starting - 1) * 100, 2) if starting > 0 else 0,
            'active_positions': active_positions,
            'position_count': len(active_positions),
            'max_positions': self.config.max_positions,
            'recent_trades': sorted(trades, key=lambda x: x.get('timestamp', ''), reverse=True)[:20],
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
            # NOTE: Removed sell cooldown check - it was blocking profitable re-entries
            # when we missed the initial buy but the trader re-bought after selling
            
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
                token_balance = await self._get_token_balance(swap.token_mint, swap.wallet)
                if token_balance == 0:
                    logger.debug("no_tokens_to_sell", token=swap.token_mint[:8])
                    # DON'T trigger cooldown when we never had a position!
                    # Old logic was blocking profitable re-entries when:
                    # 1. Trader buys (we miss due to filters/timing)
                    # 2. Trader sells (we have nothing to sell)
                    # 3. Trader re-buys (we were blocked by cooldown!)
                    # Now we only log this as informational and allow future buys
                    logger.info(
                        "missed_sell_opportunity",
                        token=swap.token_mint[:8],
                        reason="never_had_position"
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
            
            # BUYS: Check if wallet is token creator (skip pump and dumps)
            if self.skip_creator_tokens:
                is_creator = await self._is_wallet_token_creator(swap.token_mint, swap.wallet)
                if is_creator:
                    return CopyTradeResult(
                        success=False,
                        error="wallet_is_creator (skipping pump and dump)",
                        original_swap=swap
                    )
            
            # BUYS: Check all token filters (market cap, age, liquidity, volume, price change, txns)
            # For pump.fun tokens, use Pump.fun API instead of DexScreener
            is_pumpfun = swap.dex == "pump.fun"
            
            # TRUST TRADER MODE: Always skip filters for pump.fun tokens from tracked wallets
            # These traders are profitable - trust their judgment completely
            if is_pumpfun:
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
            else:
                # Use DexScreener for other DEXes
                market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h = await self._get_token_info(swap.token_mint)
                
                # If not on DexScreener, ALWAYS trust trader - they're buying for a reason!
                # These are often the best early entries that APIs don't know about yet
                if market_cap == 0 and age_minutes == 0:
                    logger.info(
                        "trust_trader_unknown_token",
                        token=swap.token_mint[:8],
                        message="Token not on DexScreener - trusting trader's early entry!"
                    )
                    # Set defaults for unknown token - assume brand new
                    market_cap = 50000
                    age_minutes = 0.5
                    liquidity = 5000
                    volume_24h = 1000
                    price_change_1h = 0
                    txns_1h = 100
            
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
                wallet_state = self._get_wallet_state(swap.wallet)
                balance_sol = wallet_state.get('balance', 1.0)
            else:
                balance = await self.rpc.get_balance(self.wallet.pubkey())
                balance_sol = balance / 1e9
            
            # Calculate fee reserve needed for existing + new positions
            if self.mock_trading:
                wallet_state = self._get_wallet_state(swap.wallet)
                current_positions = len([p for p in wallet_state.get('positions', {}).values() if p > 0])
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
                    
                    # Estimate tokens received from the swap
                    # In reality, we'd parse this from the transaction result
                    estimated_tokens = int(trade_lamports * 1000)  # Placeholder
                    
                    # Register position for auto-sell management
                    if self.position_manager:
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
        # Get wallet-specific state
        wallet = swap.wallet
        state = self._get_wallet_state(wallet)
        
        # Estimate token amount received (use swap data as reference)
        if swap.sol_value > 0 and swap.token_amount > 0:
            estimated_tokens = int((trade_sol / swap.sol_value) * swap.token_amount)
        else:
            estimated_tokens = int(trade_sol * 1_000_000)
        
        # Deduct SOL from wallet's mock balance
        state['balance'] = state.get('balance', 1.0) - trade_sol
        
        # Add tokens to wallet's mock positions
        positions = state.setdefault('positions', {})
        current_tokens = positions.get(swap.token_mint, 0)
        positions[swap.token_mint] = current_tokens + estimated_tokens
        
        # Track entry time and SOL for new positions
        entry_times = state.setdefault('entry_times', {})
        entry_sol = state.setdefault('entry_sol', {})
        if swap.token_mint not in entry_times:
            entry_times[swap.token_mint] = time.time()
            entry_sol[swap.token_mint] = trade_sol
        else:
            entry_sol[swap.token_mint] = entry_sol.get(swap.token_mint, 0) + trade_sol
        
        logger.info(
            "mock_buy",
            wallet=wallet[:8],
            token=swap.token_mint[:8],
            sol_spent=f"{trade_sol:.4f}",
            tokens_received=estimated_tokens,
            new_balance=f"{state['balance']:.4f}",
            total_tokens=positions[swap.token_mint]
        )
        
        # Track trade in history
        trades = state.setdefault('trades_history', [])
        trades.append({
            'type': 'buy',
            'token': swap.token_mint[:8],
            'full_mint': swap.token_mint,
            'sol': trade_sol,
            'tokens': estimated_tokens,
            'balance_after': state['balance'],
            'timestamp': datetime.now().isoformat()
        })
        
        # Save state for this wallet
        self._save_wallet_state(wallet)
        
        return CopyTradeResult(
            success=True,
            signature=f"MOCK_BUY_{swap.signature[:8]}",
            mock=True
        )
    
    def _simulate_mock_sell(self, swap: 'ParsedSwap', token_balance: int) -> 'CopyTradeResult':
        """Simulate a sell trade without executing on-chain."""
        # Get wallet-specific state
        wallet = swap.wallet
        state = self._get_wallet_state(wallet)
        
        # Estimate SOL received based on trader's price per token
        # IMPORTANT: Cap at trader's ratio to avoid unrealistic gains from having more tokens
        # In reality, selling more tokens causes more slippage
        if swap.token_amount > 0:
            price_per_token = swap.sol_value / swap.token_amount
            # Calculate our theoretical value at trader's price
            raw_sol = token_balance * price_per_token
            # Cap at trader's received amount - we can't do better than them
            # If we have fewer tokens, we get proportionally less
            # If we have more tokens, we'd face slippage - cap at their price
            sol_received = min(raw_sol, swap.sol_value)
        else:
            sol_received = token_balance / 1_000_000
        
        # Calculate P&L for this trade
        entry_sol_map = state.get('entry_sol', {})
        entry_sol = entry_sol_map.get(swap.token_mint, 0)
        pnl = sol_received - entry_sol
        
        # Calculate hold duration
        entry_times = state.get('entry_times', {})
        entry_time = entry_times.get(swap.token_mint, time.time())
        hold_seconds = time.time() - entry_time
        hold_minutes = hold_seconds / 60
        
        # Add SOL to wallet's mock balance
        state['balance'] = state.get('balance', 1.0) + sol_received
        
        # Remove tokens from wallet's mock positions and tracking
        positions = state.setdefault('positions', {})
        positions[swap.token_mint] = 0
        state.get('entry_times', {}).pop(swap.token_mint, None)
        state.get('entry_sol', {}).pop(swap.token_mint, None)
        
        logger.info(
            "mock_sell",
            wallet=wallet[:8],
            token=swap.token_mint[:8],
            tokens_sold=token_balance,
            sol_received=f"{sol_received:.4f}",
            entry_sol=f"{entry_sol:.4f}",
            pnl=f"{pnl:+.4f}",
            new_balance=f"{state['balance']:.4f}"
        )
        
        # Track trade in history
        trades = state.setdefault('trades_history', [])
        trades.append({
            'type': 'sell',
            'token': swap.token_mint[:8],
            'full_mint': swap.token_mint,
            'sol': sol_received,
            'tokens': token_balance,
            'entry_sol': entry_sol,
            'pnl': pnl,
            'balance_after': state['balance'],
            'timestamp': datetime.now().isoformat(),
            'hold_minutes': hold_minutes
        })
        
        # Save state for this wallet
        self._save_wallet_state(wallet)
        
        return CopyTradeResult(
            success=True,
            signature=f"MOCK_SELL_{swap.signature[:8]}",
            mock=True
        )
    
    async def _get_token_balance(self, mint: str, wallet: str = None) -> int:
        """Get token balance for our wallet by finding the associated token account."""
        if self.mock_trading:
            if wallet:
                state = self._get_wallet_state(wallet)
                return state.get('positions', {}).get(mint, 0)
            # Fallback: check all wallets for this token
            for ws in self.wallet_states.values():
                tokens = ws.get('positions', {}).get(mint, 0)
                if tokens > 0:
                    return tokens
            return 0
        
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
    
    async def _is_wallet_token_creator(self, mint: str, wallet: str) -> bool:
        """Check if the wallet is the creator of the token using Pump.fun API.
        
        Returns:
            bool: True if wallet is the token creator, False otherwise
        """
        try:
            # Try Pump.fun API first (most pump.fun tokens)
            url = f"https://frontend-api.pump.fun/coins/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    creator = data.get("creator")
                    if creator:
                        # Compare with tracked wallet (case-insensitive, first 8 chars for logging)
                        is_creator = creator.lower() == wallet.lower()
                        if is_creator:
                            logger.info(
                                "wallet_is_token_creator",
                                token=mint[:8],
                                wallet=wallet[:8],
                                message="Skipping - tracked wallet created this token"
                            )
                        return is_creator
            
            # Try RugCheck API as fallback
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    creator_info = data.get("creator")
                    creator_addr = ""
                    
                    # Handle both string and dict formats from RugCheck
                    if isinstance(creator_info, str):
                        creator_addr = creator_info
                    elif isinstance(creator_info, dict):
                        creator_addr = creator_info.get("address", "")
                    
                    if creator_addr:
                        is_creator = creator_addr.lower() == wallet.lower()
                        if is_creator:
                            logger.info(
                                "wallet_is_token_creator",
                                token=mint[:8],
                                wallet=wallet[:8],
                                message="Skipping - tracked wallet created this token (RugCheck)"
                            )
                        return is_creator
            
            return False
        except Exception as e:
            logger.debug("creator_check_error", mint=mint[:8], error=str(e))
            return False
    
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
