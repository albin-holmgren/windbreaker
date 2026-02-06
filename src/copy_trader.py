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
from datetime import datetime, timedelta, timezone
from pathlib import Path
import structlog

from .wallet_monitor import WalletMonitor, WalletTransaction
from .tx_parser import TransactionParser, ParsedSwap, SwapType
from .config import Config
from .position_manager import PositionManager
from .trade_logger import trade_logger
from .detection_logger import detection_logger
from .trade_telemetry import (
    TradeTelemetry, TradeRecord, MarketSnapshot, ExecutionDetails, 
    TokenRiskData, init_telemetry, get_telemetry
)
from decimal import Decimal
import uuid

logger = structlog.get_logger(__name__)

# Wallet-to-state-file mapping for multi-wallet tracking
WALLET_STATE_FILES = {
    'CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o': 'mock_state_cented.json',  # Cented
    '2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f': 'mock_state_cupsey.json',  # Cupsey
}

# Jupiter API for swaps (lite-api is more reliable)
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://lite-api.jup.ag/swap/v1/swap"

# Pump.fun API for bonding curve trades
PUMPFUN_API = "https://pumpportal.fun/api/trade-local"

# Native SOL
NATIVE_SOL = "So11111111111111111111111111111111111111112"

HARD_MIN_SOL_FLOOR_SOL = 0.05

@dataclass
class CopyTradeResult:
    """Result of a copy trade execution."""
    success: bool
    signature: Optional[str] = None
    error: Optional[str] = None
    original_swap: Optional[ParsedSwap] = None
    our_sol_amount: int = 0
    mock: bool = False
    execution_details: Optional[ExecutionDetails] = None
    

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
        self.recent_copies_by_wallet: Dict[str, Set[str]] = {}  # Track recently copied tokens (per followed wallet)
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

        try:
            hard_min_sol = float(os.getenv('HARD_MIN_SOL_PER_TRADE', str(HARD_MIN_SOL_FLOOR_SOL)))
        except ValueError:
            hard_min_sol = HARD_MIN_SOL_FLOOR_SOL
        hard_min_sol = max(hard_min_sol, HARD_MIN_SOL_FLOOR_SOL)
        if hard_min_sol > 0:
            self.min_sol_per_trade = max(self.min_sol_per_trade, hard_min_sol)

        try:
            hard_max_pump = float(os.getenv('HARD_MAX_PRICE_CHANGE_1H_PCT', '120'))
        except ValueError:
            hard_max_pump = 120.0
        if hard_max_pump > 0:
            if self.max_price_change_1h_pct <= 0:
                self.max_price_change_1h_pct = hard_max_pump
            else:
                self.max_price_change_1h_pct = min(self.max_price_change_1h_pct, hard_max_pump)

        self.parser.min_sol_value = self.min_sol_per_trade
        self.max_top10_holders_pct = config.max_top10_holders_pct
        self.max_dev_holdings_pct = config.max_dev_holdings_pct
        self.min_holders_count = config.min_holders_count
        self.trust_trader_pumpfun = config.trust_trader_pumpfun
        self.skip_creator_tokens = self.config.skip_creator_tokens
        raw_slippage_steps = [str(self.config.slippage_bps)]
        if self.config.slippage_steps_bps:
            raw_slippage_steps.extend(self.config.slippage_steps_bps.split(','))
        self.slippage_steps_bps: List[int] = []
        for step in raw_slippage_steps:
            try:
                value = int(step.strip())
                if value <= 0:
                    continue
                if value not in self.slippage_steps_bps:
                    self.slippage_steps_bps.append(value)
            except ValueError:
                continue
        if not self.slippage_steps_bps:
            self.slippage_steps_bps = [50]
        self.sync_mock_with_real = self.config.sync_mock_with_real
        self.jupiter_priority_fee_lamports = max(0, self.config.jupiter_priority_fee_lamports)
        self.pumpfun_priority_fee_sol = max(0.0, self.config.pumpfun_priority_fee_sol)
        
        # Cache for token info (to avoid repeated API calls)
        # mint -> (market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h, cache_time)
        self.token_info_cache: Dict[str, tuple[float, float, float, float, float, int, float]] = {}

        self.pumpfun_token_info_cache: Dict[str, tuple[float, float, float]] = {}
        
        # Cache for holder info from RugCheck (to avoid repeated API calls)
        # mint -> (top10_pct, dev_pct, holders_count, cache_time)
        self.holder_info_cache: Dict[str, tuple[float, float, int, float]] = {}
        
        # Track trader wallet balances for proportional sizing
        self.trader_balances: Dict[str, float] = {}
        
        # Position manager for auto-sell
        self.position_manager: Optional[PositionManager] = None
        
        # Trade telemetry for comprehensive tracking
        self.telemetry: Optional[TradeTelemetry] = None
        
        # Mock trading support - now per-wallet
        self.mock_trading = self.config.mock_trading
        raw_shadow_wallets = os.getenv('SHADOW_WALLETS', '').strip()
        self.shadow_wallets: Optional[Set[str]] = None
        if raw_shadow_wallets:
            self.shadow_wallets = {w.strip() for w in raw_shadow_wallets.split(',') if w.strip()}
            if not self.shadow_wallets:
                self.shadow_wallets = None
        # Real trading can run alongside mock (for actual execution while still tracking mock stats)
        self.real_trading_enabled = os.getenv('REAL_TRADING_ENABLED', 'false').lower() == 'true'
        self.real_trading_wallet = os.getenv('REAL_TRADING_FOLLOW', '')  # Which wallet to copy for real trades
        self.trader_sold_cooldown: Dict[str, float] = {}  # mint -> timestamp when trader sold
        self.sell_cooldown_seconds = 60
        # Max age before abandoning
        self.mock_position_max_age_minutes = int(os.getenv('MOCK_MAX_POSITION_AGE_MINUTES', '10'))
        
        # Per-wallet state tracking
        self.wallet_states: Dict[str, dict] = {}  # wallet_address -> state dict
        
        # CLASS-LEVEL unsellable token tracking (persists across all loop iterations)
        # Tokens that have failed to sell after multiple cycles - stop trying to sell them
        self._unsellable_tokens: Set[str] = set()
        self._unsellable_attempt_count: Dict[str, int] = {}

        self._sell_locks: Dict[str, asyncio.Lock] = {}
        
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

    def _is_shadow_wallet(self, wallet: str) -> bool:
        if not self.mock_trading:
            return False
        if self.shadow_wallets is None:
            return True
        return wallet in self.shadow_wallets

    def _get_recent_copies(self, wallet: str) -> Set[str]:
        if wallet not in self.recent_copies_by_wallet:
            self.recent_copies_by_wallet[wallet] = set()
        return self.recent_copies_by_wallet[wallet]

    def _get_sell_lock(self, token_mint: str) -> asyncio.Lock:
        sell_lock = self._sell_locks.get(token_mint)
        if sell_lock is None:
            sell_lock = asyncio.Lock()
            self._sell_locks[token_mint] = sell_lock
        return sell_lock

    async def _get_token_info(self, mint: str) -> tuple[float, float, float, float, float, int]:
        cache_ttl_sec = 20.0
        now = time.time()
        cached = self.token_info_cache.get(mint)
        if cached is not None:
            try:
                market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h, cache_time = cached
                if now - float(cache_time) <= cache_ttl_sec:
                    return market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h
            except Exception:
                pass

        market_cap = 0.0
        age_minutes = 0.0
        liquidity = 0.0
        volume_24h = 0.0
        price_change_1h = 0.0
        txns_1h = 0

        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    self.token_info_cache[mint] = (market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h, now)
                    return market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h

                data = await resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    self.token_info_cache[mint] = (market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h, now)
                    return market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h

                best_pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)

                try:
                    market_cap = float(best_pair.get("marketCap") or 0)
                except Exception:
                    market_cap = 0.0

                try:
                    liquidity = float((best_pair.get("liquidity") or {}).get("usd") or 0)
                except Exception:
                    liquidity = 0.0

                try:
                    volume_24h = float((best_pair.get("volume") or {}).get("h24") or 0)
                except Exception:
                    volume_24h = 0.0

                try:
                    price_change_1h = float((best_pair.get("priceChange") or {}).get("h1") or 0)
                except Exception:
                    price_change_1h = 0.0

                try:
                    txns = (best_pair.get("txns") or {}).get("h1") or {}
                    txns_1h = int((txns.get("buys") or 0) + (txns.get("sells") or 0))
                except Exception:
                    txns_1h = 0

                try:
                    created_ms = best_pair.get("pairCreatedAt")
                    if created_ms:
                        age_minutes = max(0.0, (now * 1000.0 - float(created_ms)) / 60000.0)
                except Exception:
                    age_minutes = 0.0

        except Exception:
            pass

        self.token_info_cache[mint] = (market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h, now)
        return market_cap, age_minutes, liquidity, volume_24h, price_change_1h, txns_1h

    async def _get_pumpfun_token_info(self, mint: str) -> tuple[float, float]:
        cache_ttl_sec = 20.0
        now = time.time()
        cached = self.pumpfun_token_info_cache.get(mint)
        if cached is not None:
            try:
                market_cap, age_minutes, cache_time = cached
                if now - float(cache_time) <= cache_ttl_sec:
                    return float(market_cap), float(age_minutes)
            except Exception:
                pass

        market_cap = 0.0
        age_minutes = 0.0

        if not self.session:
            return market_cap, age_minutes

        try:
            url = f"https://frontend-api.pump.fun/coins/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    self.pumpfun_token_info_cache[mint] = (market_cap, age_minutes, now)
                    return market_cap, age_minutes

                data = await resp.json()
                if not isinstance(data, dict):
                    self.pumpfun_token_info_cache[mint] = (market_cap, age_minutes, now)
                    return market_cap, age_minutes

                raw_mcap = (
                    data.get("usd_market_cap")
                    or data.get("market_cap_usd")
                    or data.get("marketCapUsd")
                    or data.get("marketCap")
                    or data.get("market_cap")
                    or 0
                )
                try:
                    market_cap = float(raw_mcap or 0)
                except Exception:
                    market_cap = 0.0

                raw_created = (
                    data.get("created_timestamp")
                    or data.get("createdTs")
                    or data.get("created_at")
                    or data.get("createdAt")
                    or data.get("timestamp")
                )
                if raw_created is not None:
                    try:
                        created = float(raw_created)
                        if created > 1e12:
                            created = created / 1000.0
                        age_minutes = max(0.0, (now - created) / 60.0)
                    except Exception:
                        age_minutes = 0.0
        except Exception:
            pass

        self.pumpfun_token_info_cache[mint] = (market_cap, age_minutes, now)
        return market_cap, age_minutes

    async def _is_wallet_token_creator(self, token_mint: str, wallet: str) -> bool:
        try:
            result = await self.rpc._request(
                "getAccountInfo",
                [
                    token_mint,
                    {"encoding": "jsonParsed"}
                ]
            )

            value = result.get("value") if isinstance(result, dict) else None
            data = value.get("data") if isinstance(value, dict) else None
            parsed = data.get("parsed") if isinstance(data, dict) else None
            info = parsed.get("info") if isinstance(parsed, dict) else None
            if not isinstance(info, dict):
                return False

            mint_authority = info.get("mintAuthority")
            freeze_authority = info.get("freezeAuthority")

            return wallet in (mint_authority, freeze_authority)
        except Exception as e:
            logger.debug(
                "token_creator_check_error",
                wallet=wallet[:8],
                token=token_mint[:8],
                error=str(e),
            )
            return False
        
    async def start(self) -> None:
        """Start the copy trader."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        # Initialize trade telemetry
        self.telemetry = await init_telemetry()
        logger.info("telemetry_initialized")
        
        # Create position manager for real trading (either standalone or alongside mock)
        if not self.mock_trading or self.real_trading_enabled:
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
                check_interval_sec=self.config.position_check_interval_sec,
            )
            await self.position_manager.start()
            if self.real_trading_enabled:
                logger.info(
                    "real_trading_enabled",
                    follow_wallet=self.real_trading_wallet[:8] if self.real_trading_wallet else "all",
                    alongside_mock=self.mock_trading
                )
        
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
            min_sol=self.min_sol_per_trade,
            pumpfun_priority_fee_sol=self.pumpfun_priority_fee_sol,
            max_positions=self.config.max_positions,
            take_profit=f"{self.config.take_profit_pct}%",
            stop_loss=f"{self.config.stop_loss_pct}%"
        )
        
        # Start mock position cleanup task if in mock mode (BEFORE monitor blocks)
        if self.mock_trading:
            asyncio.create_task(self._mock_position_cleanup_loop())
        
        # Start trader position monitor - checks if trader exited positions we hold
        asyncio.create_task(self._trader_position_monitor_loop())
        
        # Start failed sells retry loop - keeps trying until all sells succeed
        asyncio.create_task(self._failed_sells_retry_loop())
        
        # Start monitoring (this blocks forever)
        await self.monitor.start()
    
    async def _mock_position_cleanup_loop(self) -> None:
        """Periodically clean up stale mock positions to free slots for new trades."""
        logger.info("mock_cleanup_loop_started")
        while self.running:
            try:
                await asyncio.sleep(1)  # Check every 1 second - sell as close to Cupsey as possible
                await self._cleanup_stale_mock_positions()
            except Exception as e:
                logger.error("mock_cleanup_error", error=str(e))
    
    async def _trader_position_monitor_loop(self) -> None:
        """Monitor tracked wallets' actual holdings and sell when they exit positions.
        
        This is a BACKUP mechanism - if we miss a sell transaction, this will catch
        when the trader no longer holds a token we have a position in.
        """
        logger.info("trader_position_monitor_started", wallets=len(self.target_wallets))
        
        # Collect ALL holdings from ALL tracked wallets
        check_count = 0
        
        # NOTE: Unsellable token tracking is now CLASS-LEVEL (self._unsellable_tokens)
        # This ensures persistence across all iterations and on-chain syncs
        
        while self.running:
            try:
                await asyncio.sleep(1)  # Check every 1 second for fast sell detection
                check_count += 1
                
                # Collect holdings from ALL tracked wallets
                all_trader_holdings: Set[str] = set()
                
                for wallet in self.target_wallets:
                    try:
                        holdings = await self._fetch_wallet_token_holdings(wallet)
                        if holdings:
                            all_trader_holdings.update(holdings)
                    except Exception as e:
                        logger.debug("fetch_holdings_error", wallet=wallet[:8], error=str(e))
                
                # Get ALL our real positions (regardless of which wallet they came from)
                # CRITICAL: Also fetch actual on-chain holdings to catch positions not in memory
                our_real_positions: Set[str] = set()
                if self.position_manager:
                    our_real_positions = set(self.position_manager.positions.keys())
                
                # Sync with actual on-chain holdings (backup for missed tracking)
                if self.real_trading_enabled:
                    try:
                        our_wallet = str(self.wallet.pubkey())
                        actual_holdings = await self._fetch_wallet_token_holdings(our_wallet)
                        if actual_holdings:
                            new_found = 0
                            # Add any on-chain holdings not in position_manager
                            for mint in actual_holdings:
                                if mint not in our_real_positions:
                                    our_real_positions.add(mint)
                                    new_found += 1
                            if new_found > 0:
                                logger.info(
                                    "synced_onchain_holdings",
                                    wallet=our_wallet[:8],
                                    found=len(actual_holdings),
                                    new_added=new_found,
                                    total_real=len(our_real_positions)
                                )
                    except Exception as e:
                        logger.warning("fetch_our_holdings_error", error=str(e))
                
                # CRITICAL: Filter out unsellable tokens AFTER sync to prevent endless retries
                if self._unsellable_tokens:
                    before_count = len(our_real_positions)
                    our_real_positions -= self._unsellable_tokens
                    if before_count != len(our_real_positions):
                        logger.debug(
                            "filtered_unsellable_from_positions",
                            removed=before_count - len(our_real_positions),
                            unsellable_count=len(self._unsellable_tokens)
                        )
                
                # Get ALL our mock positions across all wallets
                our_mock_positions: Dict[str, Set[str]] = {}  # wallet -> set of mints
                if self.mock_trading:
                    for wallet, state in self.wallet_states.items():
                        if not self._is_shadow_wallet(wallet):
                            continue
                        mints = {mint for mint, amount in state.get('positions', {}).items() if amount > 0}
                        if mints:
                            our_mock_positions[wallet] = mints
                
                # Log status every 20 checks (~60 seconds)
                if check_count % 20 == 0:
                    logger.info(
                        "position_monitor_status",
                        our_real_positions=len(our_real_positions),
                        our_mock_wallets=len(our_mock_positions),
                        trader_holdings=len(all_trader_holdings),
                        real_tokens=[m[:8] for m in list(our_real_positions)[:5]]
                    )
                
                # Check REAL positions - if trader doesn't hold OR position value < $20 USD (rugged)
                TRADER_RUG_THRESHOLD_USD = 20.0  # Sell if trader's position value drops below this
                
                for mint in list(our_real_positions):
                    # Skip tokens we've already given up on (redundant check - already filtered above)
                    if mint in self._unsellable_tokens:
                        continue
                    
                    should_sell = False
                    sell_reason = None
                    trader_value_total = 0.0
                    
                    if mint not in all_trader_holdings:
                        # Trader completely exited - URGENT SELL
                        should_sell = True
                        sell_reason = "trader_exited"
                        logger.warning(
                            "trader_exited_position_detected",
                            token=mint[:8],
                            our_real=True,
                            trader_holdings_count=len(all_trader_holdings),
                            action="URGENT_SELL"
                        )
                    else:
                        # Trader still holds - check position value on EVERY iteration (critical!)
                        for wallet in self.target_wallets:
                            try:
                                trader_value = await self._get_trader_position_value_usd(wallet, mint)
                                if trader_value is not None:
                                    trader_value_total += trader_value
                            except Exception as e:
                                logger.debug("trader_value_check_error", wallet=wallet[:8], token=mint[:8], error=str(e))
                        
                        # Log every 5 checks for monitoring
                        if check_count % 5 == 0:
                            logger.info(
                                "trader_position_value_check",
                                token=mint[:8],
                                trader_value_usd=f"${trader_value_total:.2f}",
                                threshold=f"${TRADER_RUG_THRESHOLD_USD}"
                            )
                        
                        # If trader's total position value < $20 USD, they've essentially rugged
                        if trader_value_total < TRADER_RUG_THRESHOLD_USD:
                            should_sell = True
                            sell_reason = "trader_position_below_threshold"
                            logger.warning(
                                "trader_position_below_threshold",
                                token=mint[:8],
                                trader_value_usd=f"${trader_value_total:.2f}",
                                threshold=f"${TRADER_RUG_THRESHOLD_USD}",
                                action="URGENT_SELL"
                            )
                    
                    if not should_sell:
                        continue
                        
                    # Trigger real sell with retries until success
                    from .position_manager import ExitReason
                    exit_reason = ExitReason.COPIED_SELL if sell_reason == "trader_exited" else ExitReason.ABANDONED
                    try:
                        max_retries = int(os.getenv("URGENT_SELL_MAX_RETRIES", "3"))
                    except Exception:
                        max_retries = 3
                    retry_delay = 2
                    
                    # Check if position is tracked by position_manager
                    in_position_manager = self.position_manager and mint in self.position_manager.positions

                    if not in_position_manager and exit_reason == ExitReason.ABANDONED:
                        logger.info(
                            "abandoning_untracked_token",
                            token=mint[:8],
                            reason=sell_reason
                        )
                        our_real_positions.discard(mint)
                        self._unsellable_tokens.add(mint)
                        continue
                    
                    for attempt in range(max_retries):
                        if in_position_manager:
                            result = await self.position_manager.trigger_sell(mint, exit_reason)
                        else:
                            # Token is on-chain but not tracked - sell directly via pump.fun/jupiter
                            logger.info("selling_untracked_token", token=mint[:8], attempt=attempt+1, reason=sell_reason)
                            sell_lock = self._get_sell_lock(mint)
                            logger.info("untracked_sell_lock_attempt", token=mint[:8], lock_id=id(sell_lock))
                            async with sell_lock:
                                logger.info("untracked_sell_lock_acquired", token=mint[:8], lock_id=id(sell_lock))
                                try:
                                    result = await self._execute_pumpfun_swap(
                                        token_mint=mint,
                                        sol_amount=0,
                                        is_buy=False,
                                        sell_percentage=100,
                                        attempt_number=attempt + 1
                                    )
                                    if not result.success:
                                        logger.info("pumpfun_sell_failed_trying_jupiter", token=mint[:8])
                                        result = await self._sell_via_jupiter(mint)
                                finally:
                                    logger.info("untracked_sell_lock_released", token=mint[:8], lock_id=id(sell_lock))
                        
                        if result.success:
                            logger.info(
                                "real_sell_triggered",
                                token=mint[:8],
                                reason=sell_reason,
                                sol=f"{result.sol_received:.4f}" if hasattr(result, 'sol_received') and result.sol_received else "unknown",
                                attempt=attempt + 1,
                                was_tracked=in_position_manager
                            )
                            # Remove from our tracking
                            our_real_positions.discard(mint)
                            break
                        else:
                            logger.warning(
                                "real_sell_retry",
                                token=mint[:8],
                                attempt=attempt + 1,
                                max_retries=max_retries,
                                error=result.error
                            )
                            err = (result.error or "").lower() if result else ""
                            if any(
                                s in err
                                for s in [
                                    "could not find any route",
                                    "no_route",
                                    "no_quote",
                                    "quote_failed",
                                    "all_pools_failed",
                                    "http_400",
                                    "bad request",
                                ]
                            ):
                                # Unrecoverable: there is no viable way to exit this token.
                                # IMPORTANT: we must mark it unsellable HERE because this code path
                                # breaks out of the retry loop early (and the for-else would never run).
                                self._unsellable_attempt_count[mint] = self._unsellable_attempt_count.get(mint, 0) + 1
                                logger.error(
                                    "token_marked_unsellable",
                                    token=mint[:8],
                                    reason=sell_reason,
                                    cycles=self._unsellable_attempt_count[mint]
                                )
                                self._unsellable_tokens.add(mint)

                                # Remove from position manager AND its retry queues to stop fee drain.
                                if self.position_manager:
                                    try:
                                        if hasattr(self.position_manager, "failed_sells") and mint in self.position_manager.failed_sells:
                                            del self.position_manager.failed_sells[mint]
                                        if hasattr(self.position_manager, "failed_sell_attempts"):
                                            self.position_manager.failed_sell_attempts.pop(mint, None)
                                        if mint in getattr(self.position_manager, "positions", {}):
                                            del self.position_manager.positions[mint]
                                    except Exception:
                                        pass

                                break
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 1.5, 10)
                    else:
                        # Track how many full sell cycles we've tried for this token (CLASS-LEVEL)
                        self._unsellable_attempt_count[mint] = self._unsellable_attempt_count.get(mint, 0) + 1
                        
                        if self._unsellable_attempt_count[mint] >= 3:
                            # Give up after 3 full sell cycles - token is unsellable (no liquidity/no route)
                            logger.error(
                                "token_marked_unsellable",
                                token=mint[:8],
                                reason=sell_reason,
                                cycles=self._unsellable_attempt_count[mint]
                            )
                            self._unsellable_tokens.add(mint)
                            # Remove from position manager to stop all retry attempts
                            if self.position_manager and mint in self.position_manager.positions:
                                del self.position_manager.positions[mint]
                        else:
                            logger.error(
                                "real_sell_all_retries_failed",
                                token=mint[:8],
                                reason=sell_reason,
                                attempts=max_retries,
                                cycle=self._unsellable_attempt_count[mint]
                            )
                        
                        if not hasattr(self, '_failed_sells'):
                            self._failed_sells = set()
                        self._failed_sells.add(mint)
                
                # Check MOCK positions for each wallet
                for wallet, mints in our_mock_positions.items():
                    for mint in list(mints):
                        if mint not in all_trader_holdings:
                            logger.warning(
                                "trader_exited_mock_position",
                                wallet=wallet[:8],
                                token=mint[:8]
                            )
                            await self._trigger_exit_sell(wallet, mint, "trader_exited")
                        
            except Exception as e:
                logger.error("trader_position_monitor_error", error=str(e))
    
    async def _failed_sells_retry_loop(self) -> None:
        """Keep retrying failed sells until they succeed."""
        self._failed_sells: Set[str] = set()
        self._failed_sell_attempts: Dict[str, int] = {}
        try:
            max_attempts = int(os.getenv("FAILED_SELL_RETRY_MAX_ATTEMPTS", "3"))
        except Exception:
            max_attempts = 3
        
        while self.running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                if not self._failed_sells or not self.position_manager:
                    continue
                
                # Copy set to avoid modification during iteration
                mints_to_retry = list(self._failed_sells)
                
                for mint in mints_to_retry:
                    # Check if we still have this position
                    if not self.position_manager.has_position(mint):
                        self._failed_sells.discard(mint)
                        self._failed_sell_attempts.pop(mint, None)
                        continue

                    attempts = self._failed_sell_attempts.get(mint, 0)
                    if attempts >= max_attempts:
                        self._failed_sells.discard(mint)
                        self._failed_sell_attempts.pop(mint, None)
                        continue
                    
                    logger.info("retrying_failed_sell", token=mint[:8])
                    
                    from .position_manager import ExitReason
                    result = await self.position_manager.trigger_sell(mint, ExitReason.COPIED_SELL)
                    self._failed_sell_attempts[mint] = attempts + 1
                    
                    if result.success:
                        logger.info(
                            "failed_sell_finally_succeeded",
                            token=mint[:8],
                            sol=f"{result.sol_received:.4f}"
                        )
                        self._failed_sells.discard(mint)
                    else:
                        logger.warning(
                            "failed_sell_still_failing",
                            token=mint[:8],
                            error=result.error,
                            next_retry="30s"
                        )
                    
                    # Small delay between retries
                    await asyncio.sleep(2)
                    
            except Exception as e:
                logger.error("failed_sells_retry_error", error=str(e))
    
    async def _fetch_wallet_token_holdings(self, wallet: str) -> Optional[Set[str]]:
        """Fetch all token mints that a wallet currently holds (both SPL and Token-2022)."""
        try:
            from solders.pubkey import Pubkey
            
            # Query BOTH token programs (SPL Token and Token-2022)
            token_programs = [
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
            ]
            wallet_pubkey = Pubkey.from_string(wallet)
            
            holdings = set()
            
            for program_id in token_programs:
                try:
                    result = await self.rpc._request(
                        "getTokenAccountsByOwner",
                        [
                            str(wallet_pubkey),
                            {"programId": program_id},
                            {"encoding": "jsonParsed"}
                        ]
                    )
                    
                    if not result or "value" not in result:
                        continue
                    
                    for account in result["value"]:
                        try:
                            parsed = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                            mint = parsed.get("mint", "")
                            amount = int(parsed.get("tokenAmount", {}).get("amount", 0))
                            
                            # Only include tokens with actual balance
                            if amount > 0 and mint:
                                holdings.add(mint)
                        except Exception:
                            continue
                except Exception:
                    continue

            return holdings if holdings else None
            
        except Exception as e:
            logger.debug("fetch_wallet_holdings_error", wallet=wallet[:8], error=str(e))
            return None
    
    async def _get_trader_position_value_usd(self, wallet: str, mint: str) -> Optional[float]:
        """Get the USD value of a trader's position in a specific token."""
        try:
            from solders.pubkey import Pubkey
            
            # Get trader's token balance
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    wallet,
                    {"mint": mint},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if not result or "value" not in result or not result["value"]:
                return 0.0
            
            token_amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            decimals = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["decimals"])
            
            if token_amount == 0:
                return 0.0
            
            # Get token price from DexScreener
            market_cap, _, liquidity, _, _, _ = await self._get_token_info(mint)
            
            if market_cap <= 0:
                return 0.0
            
            # Estimate position value: (token_amount / total_supply) * market_cap
            # For simplicity, use liquidity as a proxy for sellable value
            # A more accurate method would be to get a quote, but this is faster
            token_amount_normalized = token_amount / (10 ** decimals)
            
            # Get total supply estimate from market cap and price
            # Use DexScreener price data if available
            try:
                async with self.session.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=5
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs", [])
                        if pairs:
                            price_usd = float(pairs[0].get("priceUsd", 0) or 0)
                            if price_usd > 0:
                                position_value_usd = token_amount_normalized * price_usd
                                return position_value_usd
            except Exception:
                pass
            
            # Fallback: estimate based on market cap ratio (rough estimate)
            return 0.0
            
        except Exception as e:
            logger.debug("get_trader_position_value_error", wallet=wallet[:8], mint=mint[:8], error=str(e))
            return None
    
    async def _trigger_exit_sell(self, wallet: str, mint: str, reason: str) -> None:
        """Trigger a mock sell when trader exits a position."""
        if wallet not in self.wallet_states:
            return
        
        state = self._get_wallet_state(wallet)
        positions = state.get('positions', {})
        token_balance = positions.get(mint, 0)
        
        if token_balance <= 0:
            return
        
        # Get current price to calculate SOL received
        try:
            market_cap, _, liquidity, _, _, _ = await self._get_token_info(mint)
            
            # Estimate token value (rough estimate based on position size)
            entry_sol = state.get('entry_sol', {}).get(mint, 0.05)
            
            # If liquidity is very low, we likely can't sell - abandon position
            if liquidity < 1000:
                logger.warning(
                    "mock_position_abandoned_no_liquidity",
                    wallet=wallet[:8],
                    token=mint[:8],
                    liquidity=f"${liquidity:.0f}",
                    entry_sol=f"{entry_sol:.4f}"
                )
                # Remove position without adding SOL back (total loss)
                positions[mint] = 0
                if mint in state.get('entry_sol', {}):
                    del state['entry_sol'][mint]
                if mint in state.get('entry_times', {}):
                    del state['entry_times'][mint]
                correlation_id = state.get('correlation_ids', {}).pop(mint, None)
                
                # Log as abandoned trade
                state.setdefault('trades_history', []).append({
                    'type': 'abandoned',
                    'token': mint[:8],
                    'full_mint': mint,
                    'sol': 0,
                    'entry_sol': entry_sol,
                    'pnl': -entry_sol,
                    'reason': f'{reason}_no_liquidity',
                    'timestamp': datetime.now().isoformat()
                })
                self._save_wallet_state(wallet)

                if self.telemetry and correlation_id and entry_sol > 0:
                    try:
                        pnl_sol = Decimal(str(-entry_sol))
                        asyncio.create_task(self.telemetry.update_trade_exit(
                            correlation_id=correlation_id,
                            exit_reason=f"{reason}_no_liquidity",
                            exit_signature=f"MOCK_ABANDON_{mint[:8]}",
                            sol_received=Decimal('0'),
                            pnl_sol=pnl_sol,
                            pnl_pct=Decimal('-100'),
                            exit_mcap=Decimal(str(market_cap)) if market_cap else None,
                            time_in_trade_sec=None,
                            cupsey_still_holding=None
                        ))
                    except Exception as e:
                        logger.error("telemetry_mock_exit_error", error=str(e), token=mint[:8])
                return
            
            # Estimate current value - assume same ratio as entry for simplicity
            # In reality this could be higher or lower
            estimated_sol = entry_sol * 0.8  # Assume 20% loss on average when forced to sell
            
            # Update mock state
            positions[mint] = 0
            state['balance'] = state.get('balance', 1.0) + estimated_sol
            pnl = estimated_sol - entry_sol
            
            if mint in state.get('entry_sol', {}):
                del state['entry_sol'][mint]
            if mint in state.get('entry_times', {}):
                del state['entry_times'][mint]
            correlation_id = state.get('correlation_ids', {}).pop(mint, None)
            
            # Log the trade
            state.setdefault('trades_history', []).append({
                'type': 'auto_sell',
                'token': mint[:8],
                'full_mint': mint,
                'sol': estimated_sol,
                'entry_sol': entry_sol,
                'pnl': pnl,
                'reason': reason,
                'timestamp': datetime.now().isoformat()
            })
            
            logger.info(
                "mock_auto_sell_trader_exit",
                wallet=wallet[:8],
                token=mint[:8],
                sol_received=f"{estimated_sol:.4f}",
                pnl=f"{pnl:.4f}",
                new_balance=f"{state['balance']:.4f}"
            )
            
            self._save_wallet_state(wallet)

            if self.telemetry and correlation_id and entry_sol > 0:
                try:
                    pnl_sol = Decimal(str(pnl))
                    entry_sol_dec = Decimal(str(entry_sol))
                    pnl_pct = (pnl_sol / entry_sol_dec * Decimal('100')) if entry_sol_dec > 0 else Decimal('0')
                    asyncio.create_task(self.telemetry.update_trade_exit(
                        correlation_id=correlation_id,
                        exit_reason=reason,
                        exit_signature=f"MOCK_AUTO_SELL_{mint[:8]}",
                        sol_received=Decimal(str(estimated_sol)),
                        pnl_sol=pnl_sol,
                        pnl_pct=pnl_pct,
                        exit_mcap=Decimal(str(market_cap)) if market_cap else None,
                        time_in_trade_sec=None,
                        cupsey_still_holding=None
                    ))
                except Exception as e:
                    logger.error("telemetry_mock_exit_error", error=str(e), token=mint[:8])
            
        except Exception as e:
            logger.error("trigger_exit_sell_error", wallet=wallet[:8], token=mint[:8], error=str(e))
    
    async def _cleanup_stale_mock_positions(self) -> None:
        """Check mock positions for rug protection and stop-loss.
        
        IMPORTANT: Even when trust_trader_pumpfun is enabled for BUYS,
        we STILL protect ourselves on EXITS. We exit when liquidity drops
        to avoid holding unsellable rugged tokens.
        
        Trust trader = follow their buys without filters
        Rug protection = always exit before token becomes unsellable
        """
        STOP_LOSS_PCT = float(os.getenv('STOP_LOSS_PCT', '-60'))
        TIME_LIMIT_MINUTES = float(os.getenv('TIME_LIMIT_MINUTES', '0'))
        RUG_DROP_PCT = float(os.getenv('RUG_DROP_PCT', '-80'))
        
        # ALWAYS use rug detection for EXITS - protect from holding unsellable tokens
        # Exit BEFORE token becomes unsellable (rugs typically die at $5k mcap)
        MIN_LIQUIDITY_USD = float(os.getenv('MOCK_MIN_LIQUIDITY_USD', '5000'))  # Exit if liquidity < $5k
        MIN_MARKET_CAP_USD = float(os.getenv('MOCK_MIN_MARKET_CAP_USD', '10000'))  # Exit if mcap < $10k
        
        # Iterate over all tracked wallets
        for wallet, state in self.wallet_states.items():
            if not self._is_shadow_wallet(wallet):
                continue
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
                        
                        logger.debug(
                            "position_value_check",
                            wallet=wallet[:8],
                            token=mint[:8],
                            entry_sol=f"{entry_sol:.4f}",
                            current_value=f"{current_value:.4f}",
                            market_cap=f"${market_cap:,.0f}",
                            liquidity=f"${liquidity:,.0f}"
                        )
                        
                        if current_value > 0:
                            pnl_pct = ((current_value - entry_sol) / entry_sol) * 100
                            
                            logger.info(
                                "position_pnl_check",
                                wallet=wallet[:8],
                                token=mint[:8],
                                pnl_pct=f"{pnl_pct:.1f}%",
                                stop_loss=f"{STOP_LOSS_PCT}%"
                            )
                            
                            # Check stop loss
                            if pnl_pct <= STOP_LOSS_PCT:
                                reason = f"stop_loss_triggered ({pnl_pct:.1f}% < {STOP_LOSS_PCT}%)"
                                should_sell = True
                    
                    # Rug detection: sudden price drop in last hour (e.g. -50%)
                    if not should_sell and price_change_1h <= RUG_DROP_PCT:
                        reason = f"rug_sudden_drop ({price_change_1h:.1f}% in 1h < {RUG_DROP_PCT}%)"
                        should_sell = True
                        if current_value == 0:
                            current_value = entry_sol * 0.3  # Assume 70% loss if no price
                    
                    # Check how long we've held this position
                    entry_times = state.get('entry_times', {})
                    entry_time_str = entry_times.get(mint, time.time())
                    hold_minutes = (time.time() - entry_time_str) / 60 if entry_time_str else 0
                    if not should_sell and TIME_LIMIT_MINUTES > 0 and hold_minutes >= TIME_LIMIT_MINUTES:
                        reason = f"time_limit_triggered (held {hold_minutes:.1f}m >= {TIME_LIMIT_MINUTES:.1f}m)"
                        should_sell = True
                        if current_value == 0:
                            current_value = entry_sol * 0.5
                    
                    # Rug detection checks - ALWAYS run to protect from unsellable tokens
                    # Even when trust_trader is on for buys, we still protect exits
                    if not should_sell and age_minutes > 3:
                        # Check liquidity - $0 liquidity means can't sell!
                        if liquidity == 0:
                            reason = f"rug_detected_zero_liquidity (can't sell!)"
                            should_sell = True
                            current_value = entry_sol * 0.05  # Assume 95% loss - can't actually sell
                            logger.info("rug_zero_liquidity", wallet=wallet[:8], token=mint[:8])
                        elif liquidity < MIN_LIQUIDITY_USD:
                            reason = f"rug_detected_low_liquidity (${liquidity:,.0f} < ${MIN_LIQUIDITY_USD:,.0f})"
                            should_sell = True
                            current_value = entry_sol * 0.1  # Assume 90% loss
                            logger.info("rug_low_liquidity", wallet=wallet[:8], token=mint[:8], liquidity=f"${liquidity:,.0f}")
                        # Check market cap
                        elif market_cap == 0:
                            reason = "rug_detected_not_on_dex"
                            should_sell = True
                            current_value = entry_sol * 0.05  # Assume 95% loss
                            logger.info("rug_not_on_dex", wallet=wallet[:8], token=mint[:8])
                        elif market_cap < MIN_MARKET_CAP_USD:
                            reason = f"rug_detected_low_mcap (${market_cap:,.0f} < ${MIN_MARKET_CAP_USD:,.0f})"
                            should_sell = True
                            current_value = entry_sol * 0.1
                            logger.info("rug_low_mcap", wallet=wallet[:8], token=mint[:8], market_cap=f"${market_cap:,.0f}")
                    
                    # Stale position fallback: if held >4 hours and no price, force sell as dead
                    if not should_sell and hold_minutes > 240 and current_value == 0:
                        reason = f"stale_position_no_price (held {hold_minutes/60:.1f}h)"
                        should_sell = True
                        current_value = entry_sol * 0.02  # Assume 98% loss for dead tokens
                    
                    # CRITICAL: Check if trader has exited this position (missed sell detection)
                    # Check immediately (after 5 seconds) - Cupsey might hold for only 10 seconds!
                    if not should_sell and hold_minutes > 0.08:
                        trader_still_holds = await self._check_trader_holds_token(wallet, mint)
                        if not trader_still_holds:
                            reason = f"trader_exited_position (missed sell - syncing with trader)"
                            should_sell = True
                            # Use current value if available, otherwise estimate based on current mcap
                            if current_value == 0:
                                current_value = entry_sol * 0.5
                    
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
        correlation_id = state.get('correlation_ids', {}).pop(mint, None)
        
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

        if self.telemetry and correlation_id and entry_sol > 0:
            try:
                pnl_sol = Decimal(str(pnl))
                entry_sol_dec = Decimal(str(entry_sol))
                pnl_pct = (pnl_sol / entry_sol_dec * Decimal('100')) if entry_sol_dec > 0 else Decimal('0')
                asyncio.create_task(self.telemetry.update_trade_exit(
                    correlation_id=correlation_id,
                    exit_reason=reason,
                    exit_signature=f"MOCK_AUTO_SELL_{mint[:8]}",
                    sol_received=Decimal(str(sol_received)),
                    pnl_sol=pnl_sol,
                    pnl_pct=pnl_pct,
                    exit_mcap=Decimal(str(market_cap)) if 'market_cap' in locals() and market_cap else None,
                    time_in_trade_sec=int(hold_seconds),
                    cupsey_still_holding=None
                ))
            except Exception as e:
                logger.error("telemetry_mock_exit_error", error=str(e), token=mint[:8])
        
        # CRITICAL: Also trigger real sell if we have real tokens!
        if self.real_trading_enabled and self.position_manager and "trader_exited" in reason:
            try:
                # Check if we have real tokens for this mint
                real_balance = await self._get_token_balance(mint)
                if real_balance > 0:
                    logger.warning(
                        "triggering_real_sell_sync",
                        token=mint[:8],
                        real_balance=real_balance,
                        reason="syncing with trader exit"
                    )
                    # Queue for immediate sell via position manager
                    self.position_manager.queue_failed_sell(mint, real_balance)
            except Exception as e:
                logger.error("real_sell_sync_error", token=mint[:8], error=str(e))
    
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
            if self.shadow_wallets is not None and wallet not in self.shadow_wallets:
                continue
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
            'correlation_ids': {},
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
        self.wallet_states[wallet].setdefault('correlation_ids', {})
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
        
        # Record ALL detected trades (buys AND sells) to cupsey_trades for profitability analysis
        if self.telemetry:
            try:
                # Fetch market snapshot for sell recording (to get price)
                snapshot = None
                if swap.is_sell:
                    snapshot = await self.telemetry.fetch_market_snapshot(swap.token_mint, "sell_detection")
                
                await self.telemetry.record_cupsey_trade(
                    signature=swap.signature,
                    wallet=swap.wallet,
                    trade_type=swap.swap_type.value,
                    token_mint=swap.token_mint,
                    sol_amount=Decimal(str(swap.sol_value)),
                    token_amount=Decimal(str(swap.token_amount)) if swap.token_amount is not None else None,
                    dex=swap.dex,
                    block_time=getattr(swap, 'block_time', None),
                    slot=getattr(swap, 'slot', None),
                    market_snapshot=snapshot,
                    copied=False,  # Will be updated to True later for executed buys
                    skip_reason=None
                )
            except Exception as e:
                logger.debug("cupsey_trade_record_error", error=str(e))
        
        # If trader sells a token we hold, copy the sell!
        if swap.is_sell:
            # Check if we have MOCK position to sell
            mock_sold = False
            if self._is_shadow_wallet(swap.wallet):
                state = self._get_wallet_state(swap.wallet)
                mock_balance = state.get('positions', {}).get(swap.token_mint, 0)
                if mock_balance > 0:
                    logger.info(
                        "copying_trader_sell_mock",
                        token=swap.token_mint[:8] + "...",
                        mock_balance=mock_balance,
                        message="Trader sold, mock selling!"
                    )
                    self._simulate_mock_sell(swap, mock_balance)
                    mock_sold = True
            
            # Check if we have REAL position to sell (only if real trading enabled)
            real_sold = False
            if self.position_manager:
                has_real_position = self.position_manager.has_position(swap.token_mint)
                
                # FALLBACK: Check on-chain balance even if not in position_manager
                # This catches tokens we bought but failed to register
                onchain_balance = 0
                if not has_real_position and not self.mock_trading:
                    onchain_balance = await self._get_token_balance(swap.token_mint)
                    if onchain_balance > 0:
                        logger.info(
                            "found_untracked_position",
                            token=swap.token_mint[:8],
                            onchain_balance=onchain_balance,
                            message="Position not tracked but tokens found on-chain!"
                        )
                        has_real_position = True  # Force sell
                
                logger.info(
                    "checking_real_position_for_sell",
                    token=swap.token_mint[:8],
                    has_position=has_real_position,
                    onchain_balance=onchain_balance,
                    tracked_positions=list(self.position_manager.positions.keys())[:5] if self.position_manager.positions else []
                )
                if has_real_position:
                    logger.info(
                        "copying_trader_sell_real",
                        token=swap.token_mint[:8] + "...",
                        message="Trader sold, real selling!"
                    )
                    from .position_manager import ExitReason
                    
                    # If untracked, sell directly via copy_trader instead of position_manager
                    if onchain_balance > 0 and not self.position_manager.has_position(swap.token_mint):
                        logger.info("selling_untracked_position", token=swap.token_mint[:8], balance=onchain_balance)
                        if self.position_manager:
                            correlation_id = str(uuid.uuid4())
                            sell_lock = self._get_sell_lock(swap.token_mint)
                            logger.info("untracked_sell_lock_attempt", token=swap.token_mint[:8], lock_id=id(sell_lock))
                            async with sell_lock:
                                logger.info("untracked_sell_lock_acquired", token=swap.token_mint[:8], lock_id=id(sell_lock))
                                try:
                                    sell_result = await self.position_manager._execute_direct_sell(
                                        swap.token_mint,
                                        onchain_balance,
                                        correlation_id=correlation_id
                                    )
                                finally:
                                    logger.info("untracked_sell_lock_released", token=swap.token_mint[:8], lock_id=id(sell_lock))
                            if sell_result and sell_result.success:
                                real_sold = True
                                logger.info("untracked_sell_success", token=swap.token_mint[:8])
                            else:
                                logger.warning("untracked_sell_failed", error=sell_result.error if sell_result else "no_result")

                    else:
                        result = await self.position_manager.trigger_sell(swap.token_mint, ExitReason.COPIED_SELL)
                        if result.success:
                            self.stats.total_sol_received += result.sol_received
                            real_sold = True
                            logger.info("copied_sell_success", sol_received=f"{result.sol_received:.4f}")
                        else:
                            logger.warning("copied_sell_failed", error=result.error)
            
            # If we sold anything (mock or real), we're done - don't try to buy
            if mock_sold or real_sold:
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
        if swap.is_buy and swap.token_mint in self._get_recent_copies(swap.wallet):
            return False, "recently_copied"
        
        return True, "ok"
    
    async def _execute_copy(self, swap: ParsedSwap) -> CopyTradeResult:
        """Execute a copy of the detected swap."""
        try:
            # RACE CONDITION FIX: Block concurrent processing of same token for buys
            # This prevents websocket + polling from both executing the same trade
            if swap.is_buy and swap.token_mint in self._get_recent_copies(swap.wallet):
                logger.debug("blocking_concurrent_buy", token=swap.token_mint[:8])
                return CopyTradeResult(success=False, error="already_processing", original_swap=swap)
            
            # FAST PATH for sells - skip balance calculations, AGGRESSIVE RETRIES
            if not swap.is_buy:
                # Correlation ID for sell-side telemetry (prefer the existing position correlation_id)
                correlation_id = None
                if self.position_manager:
                    position = self.position_manager.get_position(swap.token_mint)
                    if position and getattr(position, "correlation_id", None):
                        correlation_id = position.correlation_id
                if not correlation_id:
                    correlation_id = str(uuid.uuid4())

                # Check if we should execute real sell
                should_execute_real = (
                    not self.mock_trading or 
                    (self.real_trading_enabled and 
                     (not self.real_trading_wallet or swap.wallet == self.real_trading_wallet))
                )
                
                # Get mock balance for mock trading
                mock_balance = 0
                if self._is_shadow_wallet(swap.wallet):
                    state = self._get_wallet_state(swap.wallet)
                    mock_balance = state.get('positions', {}).get(swap.token_mint, 0)
                
                # Get real balance for real trading (separate check)
                real_balance = 0
                if should_execute_real:
                    # Temporarily disable mock to get real balance
                    original_mock = self.mock_trading
                    self.mock_trading = False
                    real_balance = await self._get_token_balance(swap.token_mint)
                    self.mock_trading = original_mock
                    logger.debug("real_sell_balance_check", token=swap.token_mint[:8], real_balance=real_balance)
                
                # Check if we have anything to sell (mock or real)
                if mock_balance == 0 and real_balance == 0:
                    logger.debug("no_tokens_to_sell", token=swap.token_mint[:8])
                    logger.info(
                        "missed_sell_opportunity",
                        token=swap.token_mint[:8],
                        reason="never_had_position"
                    )
                    return CopyTradeResult(success=False, error="no_tokens_to_sell", original_swap=swap)
                
                logger.info(
                    "fast_sell",
                    token=swap.token_mint[:8],
                    mock_balance=mock_balance,
                    real_balance=real_balance,
                    their_sol=f"{swap.sol_value:.4f}"
                )
                
                # Detect if this is a pump.fun token
                is_pumpfun_sell = swap.dex == "pump.fun"
                
                # Execute mock sell if we have mock balance
                if self._is_shadow_wallet(swap.wallet) and mock_balance > 0:
                    mock_result = self._simulate_mock_sell(swap, mock_balance)
                    if not should_execute_real or real_balance == 0:
                        return mock_result

                # Only execute real sell if we have real balance
                if real_balance == 0:
                    logger.info("skip_real_sell", token=swap.token_mint[:8], reason="no_real_balance")
                    return CopyTradeResult(success=True, mock=True, original_swap=swap)

                sell_lock = self._get_sell_lock(swap.token_mint)
                logger.info("fast_sell_lock_attempt", token=swap.token_mint[:8], lock_id=id(sell_lock))
                async with sell_lock:
                    logger.info("fast_sell_lock_acquired", token=swap.token_mint[:8], lock_id=id(sell_lock))
                    try:
                        try:
                            max_retries = int(os.getenv("URGENT_SELL_MAX_RETRIES", "3"))
                        except Exception:
                            max_retries = 3
                        result = None
                        unrecoverable = False
                        for attempt in range(max_retries):
                            if is_pumpfun_sell:
                                estimated_sol = real_balance / 1e9 * 0.00001
                                result = await self._execute_pumpfun_swap(
                                    token_mint=swap.token_mint,
                                    sol_amount=estimated_sol,
                                    is_buy=False,
                                    correlation_id=correlation_id,
                                    attempt_number=attempt + 1
                                )
                                logger.info("pumpfun_swap_attempt", token=swap.token_mint[:8], attempt=attempt+1)
                                if result.success:
                                    logger.info("pumpfun_swap_success", token=swap.token_mint[:8], signature=str(result.signature)[:16] if result.signature else None)
                                else:
                                    logger.error("pumpfun_swap_failed", token=swap.token_mint[:8], error=result.error if result.error else 'unknown', status_code='unknown')
                            else:
                                result = await self._execute_swap(
                                    input_mint=swap.token_mint,
                                    output_mint=NATIVE_SOL,
                                    amount=real_balance,
                                    correlation_id=correlation_id,
                                    attempt_number=attempt + 1
                                )

                            if result.success:
                                if self.telemetry and result.execution_details:
                                    asyncio.create_task(self.telemetry.record_execution_details(
                                        correlation_id=correlation_id,
                                        exec_detail=result.execution_details
                                    ))
                                self.stats.total_sol_received += swap.sol_value * 0.01
                                trade_logger.log_sell(
                                    token_mint=swap.token_mint,
                                    token_symbol=swap.token_symbol,
                                    our_sol_received=swap.sol_value * 0.01,
                                    our_tokens_sold=real_balance,
                                    our_signature=result.signature or "",
                                    copied_wallet=swap.wallet,
                                    their_sol=swap.sol_value,
                                    their_signature=swap.signature,
                                    delay_seconds=0,
                                    entry_sol=swap.sol_value * 0.01,
                                    exit_reason="copied_sell",
                                    success=True
                                )
                                logger.info("sell_success", token=swap.token_mint[:8], attempt=attempt+1)
                                self._log_real_trade_to_state(
                                    swap=swap,
                                    trade_sol=swap.sol_value * 0.01,
                                    trade_type="sell",
                                    signature=result.signature
                                )
                                return result

                            delay = 0.5 * (2 ** attempt)
                            logger.warning(
                                "sell_retry",
                                token=swap.token_mint[:8],
                                attempt=attempt+1,
                                error=result.error,
                                delay=f"{delay:.1f}s"
                            )
                            err = (result.error or "").lower() if result else ""
                            if any(
                                s in err
                                for s in [
                                    "could not find any route",
                                    "no_route",
                                    "no_quote",
                                    "quote_failed",
                                    "all_pools_failed",
                                    "http_400",
                                    "bad request",
                                ]
                            ):
                                unrecoverable = True
                                break
                            await asyncio.sleep(delay)
                            delay = min(delay * 1.2, 10)
                    finally:
                        logger.info("fast_sell_lock_released", token=swap.token_mint[:8], lock_id=id(sell_lock))
                
                # All retries failed - add to retry queue for background retries
                if unrecoverable:
                    try:
                        self._unsellable_attempt_count[swap.token_mint] = self._unsellable_attempt_count.get(swap.token_mint, 0) + 1
                    except Exception:
                        pass
                    self._unsellable_tokens.add(swap.token_mint)
                    if self.position_manager:
                        try:
                            if hasattr(self.position_manager, "failed_sells") and swap.token_mint in self.position_manager.failed_sells:
                                del self.position_manager.failed_sells[swap.token_mint]
                            if hasattr(self.position_manager, "failed_sell_attempts"):
                                self.position_manager.failed_sell_attempts.pop(swap.token_mint, None)
                            if swap.token_mint in getattr(self.position_manager, "positions", {}):
                                del self.position_manager.positions[swap.token_mint]
                        except Exception:
                            pass
                else:
                    logger.error("sell_failed_queuing_retry", token=swap.token_mint[:8])
                    if self.position_manager:
                        self.position_manager.queue_failed_sell(swap.token_mint, real_balance)
                
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
            
            # BUYS: Pre-check sellability - skip tokens we can't sell later
            # SKIP for pump.fun tokens - they sell via PumpPortal, not Jupiter
            if not is_pumpfun:
                has_sell_route = await self._check_token_sellable(swap.token_mint)
                if not has_sell_route:
                    logger.warning(
                        "skipping_unsellable_token",
                        token=swap.token_mint[:8],
                        reason="no_sell_route"
                    )
                    return CopyTradeResult(
                        success=False,
                        error="token_not_sellable (no route on Jupiter)",
                        original_swap=swap
                    )

            # Get token info from DexScreener for ALL tokens (including pump.fun)
            (
                market_cap,
                age_minutes,
                liquidity,
                volume_24h,
                price_change_1h,
                txns_1h,
            ) = await self._get_token_info(swap.token_mint)

            # FALLBACK: If DexScreener returns $0 mcap for pump.fun tokens, try pump.fun API
            if is_pumpfun and market_cap == 0:
                pf_mcap, pf_age = await self._get_pumpfun_token_info(swap.token_mint)
                if pf_mcap > 0:
                    market_cap = pf_mcap
                    logger.info("pumpfun_fallback_mcap", token=swap.token_mint[:8], mcap=f"${pf_mcap:,.0f}")
                if pf_age > 0 and age_minutes == 0:
                    age_minutes = pf_age

            # Log the token info
            logger.info(
                "token_info_fetched",
                token=swap.token_mint[:8],
                dex=swap.dex,
                market_cap=f"${market_cap:,.0f}",
                age=f"{age_minutes:.1f}m",
                liquidity=f"${liquidity:,.0f}",
            )
            
            # trust_trader_pumpfun: skip TIMING filters (age, volume, txns, price change)
            # but ALWAYS apply SAFETY filters (market cap, liquidity) to avoid buying into rugs
            skip_timing_filters = self.trust_trader_pumpfun and is_pumpfun
            
            # Helper to log detection with all market data and record telemetry
            def log_skip(skip_reason: str):
                detection_logger.log_detection(
                    wallet=swap.wallet,
                    trade_type="buy",
                    token_mint=swap.token_mint,
                    token_symbol=swap.token_symbol,
                    dex=swap.dex,
                    their_sol=swap.sol_value,
                    their_signature=swap.signature,
                    market_cap_usd=market_cap,
                    liquidity_usd=liquidity,
                    volume_24h_usd=volume_24h,
                    age_minutes=age_minutes,
                    price_change_1h=price_change_1h,
                    txns_1h=txns_1h,
                    copied=False,
                    skip_reason=skip_reason
                )
                # Record comprehensive telemetry for skipped trade
                if self.telemetry:
                    correlation_id = str(uuid.uuid4())
                    asyncio.create_task(self.telemetry.record_skipped_trade(
                        correlation_id=correlation_id,
                        token_mint=swap.token_mint,
                        trader_wallet=swap.wallet,
                        their_signature=swap.signature,
                        their_sol_amount=Decimal(str(swap.sol_value)),
                        their_dex=swap.dex,
                        skip_reason=skip_reason,
                        skip_category=self._categorize_skip_reason(skip_reason),
                        market_snapshot=None,
                        filter_thresholds={
                            "min_mcap": self.min_market_cap_usd,
                            "min_liquidity": self.min_liquidity_usd,
                            "min_volume": self.min_volume_24h_usd,
                            "min_age": self.min_token_age_minutes,
                            "max_pump": self.max_price_change_1h_pct if hasattr(self, 'max_price_change_1h_pct') else 0
                        },
                        error_code="skipped",
                        error_message=skip_reason
                    ))
            
            # Check token age (timing filter - can be skipped)
            if not skip_timing_filters and self.min_token_age_minutes > 0 and age_minutes < self.min_token_age_minutes:
                logger.info(
                    "skipping_new_token",
                    token=swap.token_mint[:8],
                    age=f"{age_minutes:.1f}m",
                    min_age=f"{self.min_token_age_minutes}m"
                )
                log_skip(f"token_too_new ({age_minutes:.1f}m < {self.min_token_age_minutes}m)")
                return CopyTradeResult(
                    success=False,
                    error=f"token_too_new ({age_minutes:.1f}m < {self.min_token_age_minutes}m)",
                    original_swap=swap
                )
            
            # SAFETY filters
            # DexScreener liquidity is often 0 for early pump.fun bonding curve tokens.
            # If TRUST_TRADER_PUMPFUN is enabled, allow liquidity==0 for pump.fun tokens (we sell via PumpPortal).
            allow_pumpfun_liquidity_zero = bool(skip_timing_filters and liquidity == 0)

            # Check market cap (SAFETY filter)
            if self.min_market_cap_usd > 0 and market_cap < self.min_market_cap_usd:
                logger.info(
                    "skipping_low_mcap",
                    token=swap.token_mint[:8],
                    market_cap=f"${market_cap:,.0f}",
                    min_required=f"${self.min_market_cap_usd:,.0f}"
                )
                log_skip(f"market_cap_too_low (${market_cap:,.0f} < ${self.min_market_cap_usd:,.0f})")
                return CopyTradeResult(
                    success=False,
                    error=f"market_cap_too_low (${market_cap:,.0f} < ${self.min_market_cap_usd:,.0f})",
                    original_swap=swap
                )
            
            # Check liquidity - CRITICAL for being able to sell! (SAFETY filter)
            if not allow_pumpfun_liquidity_zero and self.min_liquidity_usd > 0 and liquidity < self.min_liquidity_usd:
                logger.info(
                    "skipping_low_liquidity",
                    token=swap.token_mint[:8],
                    liquidity=f"${liquidity:,.0f}",
                    min_required=f"${self.min_liquidity_usd:,.0f}"
                )
                log_skip(f"liquidity_too_low (${liquidity:,.0f} < ${self.min_liquidity_usd:,.0f})")
                return CopyTradeResult(
                    success=False,
                    error=f"liquidity_too_low (${liquidity:,.0f} < ${self.min_liquidity_usd:,.0f})",
                    original_swap=swap
                )
            
            # Check 24h volume - indicates trading activity (timing filter - can be skipped)
            if not skip_timing_filters and self.min_volume_24h_usd > 0 and volume_24h < self.min_volume_24h_usd:
                logger.info(
                    "skipping_low_volume",
                    token=swap.token_mint[:8],
                    volume_24h=f"${volume_24h:,.0f}",
                    min_required=f"${self.min_volume_24h_usd:,.0f}"
                )
                log_skip(f"volume_too_low (${volume_24h:,.0f} < ${self.min_volume_24h_usd:,.0f})")
                return CopyTradeResult(
                    success=False,
                    error=f"volume_too_low (${volume_24h:,.0f} < ${self.min_volume_24h_usd:,.0f})",
                    original_swap=swap
                )
            
            # Check if token already pumped too much - avoid buying tops! (timing filter - can be skipped)
            if not skip_timing_filters and self.max_price_change_1h_pct > 0 and price_change_1h > self.max_price_change_1h_pct:
                logger.info(
                    "skipping_already_pumped",
                    token=swap.token_mint[:8],
                    price_change_1h=f"+{price_change_1h:.0f}%",
                    max_allowed=f"+{self.max_price_change_1h_pct:.0f}%"
                )
                log_skip(f"already_pumped (+{price_change_1h:.0f}% > +{self.max_price_change_1h_pct:.0f}%)")
                return CopyTradeResult(
                    success=False,
                    error=f"already_pumped (+{price_change_1h:.0f}% > +{self.max_price_change_1h_pct:.0f}%)",
                    original_swap=swap
                )
            
            # Check minimum transactions - ensure active trading (timing filter - can be skipped)
            if not skip_timing_filters and self.min_txns_1h > 0 and txns_1h < self.min_txns_1h:
                logger.info(
                    "skipping_low_activity",
                    token=swap.token_mint[:8],
                    txns_1h=txns_1h,
                    min_required=self.min_txns_1h
                )
                log_skip(f"low_activity ({txns_1h} txns < {self.min_txns_1h} min)")
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
            # Check if we should execute real trades
            should_execute_real = (
                not self.mock_trading or 
                (self.real_trading_enabled and 
                 (not self.real_trading_wallet or swap.wallet == self.real_trading_wallet))
            )
            
            # Get mock balance for mock trading
            mock_balance_sol = 0
            if self._is_shadow_wallet(swap.wallet):
                wallet_state = self._get_wallet_state(swap.wallet)
                mock_balance_sol = wallet_state.get('balance', 1.0)
            
            # Get real balance for real trading
            real_balance_sol = 0
            if should_execute_real:
                real_balance = await self.rpc.get_balance(self.wallet.pubkey())
                real_balance_sol = real_balance / 1e9
            
            # Use mock balance for sizing when mock trading is enabled
            # Real trade execution will be skipped if real balance is insufficient
            # This ensures mock trades proceed even when real balance is low
            if should_execute_real:
                balance_sol = real_balance_sol
            elif self.mock_trading:
                balance_sol = mock_balance_sol
            else:
                balance_sol = real_balance_sol
            
            # Track if real balance is too low (will skip real execution but allow mock)
            real_balance_insufficient = should_execute_real and real_balance_sol < 0.01
            
            # Calculate fee reserve needed for existing + new positions
            if self._is_shadow_wallet(swap.wallet):
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
            
            logger.info(
                "balance_calculation",
                balance=f"{balance_sol:.4f}",
                mock_bal=f"{mock_balance_sol:.4f}" if self.mock_trading else "N/A",
                real_bal=f"{real_balance_sol:.4f}" if should_execute_real else "N/A",
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

            # Correlation ID links detection -> our execution -> position exit telemetry
            correlation_id = str(uuid.uuid4())
            
            # RACE CONDITION FIX: Add to recent_copies BEFORE execution to prevent
            # concurrent processing of the same token (websocket + polling can both trigger)
            if swap.is_buy and swap.token_mint not in self._get_recent_copies(swap.wallet):
                self._get_recent_copies(swap.wallet).add(swap.token_mint)
                asyncio.create_task(self._clear_recent_copy(swap.wallet, swap.token_mint, 60))  # Block for 60s
            
            # Buy: Use appropriate API based on DEX
            # Check if we should execute real sell
            should_execute_real = (
                not self.mock_trading or 
                (self.real_trading_enabled and 
                 (not self.real_trading_wallet or swap.wallet == self.real_trading_wallet))
            )
            
            real_result = None
            mock_result = None
            real_trade_sol = 0  # Track actual real trade amount for position manager
            require_real_success = self.sync_mock_with_real and should_execute_real and self.real_trading_enabled

            # Execute real trade first (only if real balance is sufficient)
            if should_execute_real and not real_balance_insufficient:
                # IMPORTANT: Recalculate trade size based on REAL balance
                # Keep enough SOL reserved for selling ALL open positions (including this new one)
                real_open_positions = len(self.position_manager.positions) if self.position_manager else 0
                # Reserve 0.02 SOL per position for sell fees (priority fees are typically <0.01)
                sell_reserve = 0.02 * (real_open_positions + 1)
                base_reserve = 0.01  # Minimal base reserve for rent
                real_available = max(0, real_balance_sol - sell_reserve - base_reserve)
                
                logger.debug(
                    "fee_reserve_calculation",
                    real_balance=f"{real_balance_sol:.4f}",
                    open_positions=real_open_positions,
                    sell_reserve=f"{sell_reserve:.4f}",
                    base_reserve=f"{base_reserve:.4f}",
                    real_available=f"{real_available:.4f}"
                )
                
                # Size real trade based on real available balance
                real_trade_sol = min(trade_sol, real_available, self.max_sol_per_trade)
                real_trade_sol = round(real_trade_sol, 4)
                
                if real_trade_sol < self.min_sol_per_trade:
                    logger.info(
                        "real_trade_skipped_insufficient_after_reserve",
                        token=swap.token_mint[:8],
                        real_balance=f"{real_balance_sol:.4f}",
                        real_available=f"{real_available:.4f}",
                        sell_reserve=f"{sell_reserve:.4f}",
                        min_required=f"{self.min_sol_per_trade}"
                    )
                else:
                    real_trade_lamports = int(real_trade_sol * 1e9)
                    logger.info(
                        "real_trade_sizing",
                        real_balance=f"{real_balance_sol:.4f}",
                        real_available=f"{real_available:.4f}",
                        real_trade_sol=f"{real_trade_sol:.4f}",
                        open_positions=real_open_positions
                    )
                    
                    real_result = await self._execute_real_trade_with_fallbacks(
                        swap=swap,
                        trade_lamports=real_trade_lamports,
                        trade_sol=real_trade_sol,
                        is_pumpfun=is_pumpfun,
                        correlation_id=correlation_id
                    )
                    
                    if real_result and not real_result.success:
                        # Record failed trade telemetry (so we can analyze missed executions)
                        if self.telemetry:
                            asyncio.create_task(self.telemetry.record_failed_execution(
                                trade_id=None,
                                correlation_id=correlation_id,
                                token_mint=swap.token_mint,
                                execution_type="buy",
                                method="jupiter",
                                error_code="real_trade_failed",
                                error_message=real_result.error,
                                error_category="real_trade_error",
                                attempt_number=1,
                                requested_amount=Decimal(str(real_trade_sol)),
                                slippage_bps=self.config.slippage_bps,
                                priority_fee=self.jupiter_priority_fee_lamports
                            ))
                        if not self.mock_trading or require_real_success:
                            return real_result
                    elif real_result and real_result.success:
                        logger.info("real_trade_executed", type="buy", token=swap.token_mint[:8], sol=real_trade_sol)
                        # Log real trade to state file for dashboard display
                        self._log_real_trade_to_state(
                            swap=swap,
                            trade_sol=real_trade_sol,
                            trade_type="buy",
                            signature=real_result.signature
                        )
            elif should_execute_real and real_balance_insufficient:
                logger.info(
                    "real_trade_skipped_low_balance",
                    token=swap.token_mint[:8],
                    real_balance=f"{real_balance_sol:.4f}",
                    min_required=f"{self.min_sol_per_trade}"
                )
            
            # Simulate/mock trade - allow if:
            # 1. Mock trading is enabled AND
            # 2. Either real success is not required, OR real succeeded, OR real was skipped due to low balance
            allow_mock_execution = self.mock_trading and (
                not require_real_success or 
                (real_result and real_result.success) or
                real_balance_insufficient  # Allow mock even when real skipped due to low balance
            )
            if allow_mock_execution and self._is_shadow_wallet(swap.wallet):
                mock_result = self._simulate_mock_buy(swap, trade_sol, correlation_id=correlation_id)
            elif self.mock_trading and require_real_success:
                logger.warning(
                    "mock_trade_skipped_due_to_real_failure",
                    token=swap.token_mint[:8]
                )
                return real_result or CopyTradeResult(
                    success=False,
                    error="real_trade_required_but_failed",
                    original_swap=swap
                )
            
            # Determine which result to return for downstream logic
            if self._is_shadow_wallet(swap.wallet):
                result = mock_result
            else:
                result = real_result
            
            if result.success:
                # For BUYS: Track to avoid rapid re-buying (30 sec cooldown)
                # For SELLS: Don't track - allow multiple sell attempts
                if swap.is_buy:
                    self._get_recent_copies(swap.wallet).add(swap.token_mint)
                    asyncio.create_task(self._clear_recent_copy(swap.wallet, swap.token_mint, 30))
                
                if swap.is_buy:
                    self.stats.total_sol_spent += trade_sol
                    
                    # Estimate tokens received from the swap
                    # In reality, we'd parse this from the transaction result
                    estimated_tokens = int(trade_lamports * 1000)  # Placeholder
                    
                    # Register position for auto-sell management (REAL positions only)
                    # Add to position_manager when: real-only mode OR real+mock mode with successful real trade
                    should_register_real_position = (
                        self.position_manager and 
                        (not self.mock_trading or (self.real_trading_enabled and real_result and real_result.success))
                    )
                    if should_register_real_position:
                        # Use actual real trade amount if available, otherwise fall back to trade_sol
                        actual_entry_sol = real_trade_sol if real_trade_sol > 0 else trade_sol
                        self.position_manager.add_position(
                            token_mint=swap.token_mint,
                            token_symbol=swap.token_symbol,
                            entry_sol=actual_entry_sol,
                            token_amount=estimated_tokens,
                            entry_signature=real_result.signature if real_result else (result.signature or ""),
                            copied_from=swap.wallet,
                            dex="pump.fun" if is_pumpfun else swap.dex,
                            correlation_id=correlation_id
                        )
                        logger.info(
                            "real_position_registered",
                            token=swap.token_mint[:8],
                            entry_sol=f"{actual_entry_sol:.4f}",
                            copied_from=swap.wallet[:8],
                            total_positions=len(self.position_manager.positions)
                        )
                    
                    # Log the trade for analysis
                    trade_logger.log_buy(
                        token_mint=swap.token_mint,
                        token_symbol=swap.token_symbol,
                        our_sol=trade_sol,
                        our_tokens=estimated_tokens,
                        our_signature=(real_result.signature if real_result else result.signature),
                        copied_wallet=swap.wallet,
                        their_sol=swap.sol_value,
                        their_signature=swap.signature,
                        their_timestamp=None,
                        delay_seconds=(datetime.utcnow() - datetime.utcnow()).total_seconds(),  # TODO: track actual delay
                        entry_sol=swap.sol_value * 0.01,
                        exit_reason="copied_buy",
                        success=True
                    )
                    
                    # Log to detection logger for filter analysis
                    detection_logger.log_detection(
                        wallet=swap.wallet,
                        trade_type="buy",
                        token_mint=swap.token_mint,
                        token_symbol=swap.token_symbol,
                        dex=swap.dex,
                        their_sol=swap.sol_value,
                        their_signature=swap.signature,
                        market_cap_usd=market_cap,
                        liquidity_usd=liquidity,
                        volume_24h_usd=volume_24h,
                        age_minutes=age_minutes,
                        price_change_1h=price_change_1h,
                        txns_1h=txns_1h,
                        copied=True,
                        our_sol=trade_sol,
                        our_signature=(real_result.signature if real_result else result.signature)
                    )
                    
                    # Record comprehensive telemetry for executed trade
                    asyncio.create_task(self._record_trade_telemetry(
                        swap=swap,
                        correlation_id=correlation_id,
                        status="executed",
                        market_cap=market_cap,
                        liquidity=liquidity,
                        volume_24h=volume_24h,
                        price_change_1h=price_change_1h,
                        txns_1h=txns_1h,
                        age_minutes=age_minutes,
                        our_sol_amount=trade_sol,
                        our_signature=(real_result.signature if real_result else result.signature),
                        entry_reason="copied_buy",
                        execution_details=(real_result.execution_details if real_result else result.execution_details),
                        filters_passed={
                            "market_cap": market_cap,
                            "liquidity": liquidity,
                            "volume_24h": volume_24h,
                            "price_change_1h": price_change_1h,
                            "txns_1h": txns_1h,
                            "age_minutes": age_minutes
                        }
                    ))
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
    
    async def _record_trade_telemetry(
        self,
        swap: ParsedSwap,
        correlation_id: str,
        status: str,
        market_cap: float = 0,
        liquidity: float = 0,
        volume_24h: float = 0,
        price_change_1h: float = 0,
        txns_1h: int = 0,
        age_minutes: float = 0,
        our_sol_amount: float = 0,
        our_signature: Optional[str] = None,
        entry_reason: Optional[str] = None,
        skip_reason: Optional[str] = None,
        error_message: Optional[str] = None,
        filters_passed: Optional[Dict] = None,
        execution_details: Optional[ExecutionDetails] = None
    ):
        """Record comprehensive trade telemetry to database."""
        if not self.telemetry:
            return
        
        try:
            # Fetch comprehensive market snapshot
            market_snapshot = await self.telemetry.fetch_market_snapshot(
                swap.token_mint, 
                "entry" if status == "executed" else "detection"
            )
            
            # Create trade record
            trade = TradeRecord(
                correlation_id=correlation_id,
                token_mint=swap.token_mint,
                token_symbol=swap.token_symbol,
                trader_wallet=swap.wallet,
                bot_wallet=str(self.wallet.pubkey()),
                trade_type=swap.swap_type.value,
                status=status,
                detected_at=datetime.now(timezone.utc),
                their_signature=swap.signature,
                their_sol_amount=Decimal(str(swap.sol_value)),
                their_dex=swap.dex,
                our_sol_amount=Decimal(str(our_sol_amount)) if our_sol_amount else None,
                our_signature=our_signature,
                entry_reason=entry_reason,
                filters_passed=filters_passed,
                skip_reason=skip_reason,
                error_message=error_message
            )
            
            # Add market snapshot
            trade.market_snapshots.append(market_snapshot)
            
            # Add execution details if provided
            if execution_details:
                trade.execution_details.append(execution_details)
            
            # Fetch token risk data for executed trades
            if status == "executed":
                trade.token_risk = await self.telemetry.fetch_token_risk_data(swap.token_mint)
            
            # Record to database
            await self.telemetry.record_trade(trade)
            
            # Also record Cupsey trade detection
            await self.telemetry.record_cupsey_trade(
                signature=swap.signature,
                wallet=swap.wallet,
                trade_type=swap.swap_type.value,
                token_mint=swap.token_mint,
                sol_amount=Decimal(str(swap.sol_value)),
                token_amount=Decimal(str(swap.token_amount)) if swap.token_amount is not None else None,
                dex=swap.dex,
                block_time=None,
                slot=None,
                market_snapshot=market_snapshot,
                copied=(status == "executed"),
                skip_reason=skip_reason
            )
            
            # Record skipped trade if applicable
            if status == "skipped" and skip_reason:
                await self.telemetry.record_skipped_trade(
                    correlation_id=correlation_id,
                    token_mint=swap.token_mint,
                    trader_wallet=swap.wallet,
                    their_signature=swap.signature,
                    their_sol_amount=Decimal(str(swap.sol_value)),
                    their_dex=swap.dex,
                    skip_reason=skip_reason,
                    skip_category=self._categorize_skip_reason(skip_reason),
                    market_snapshot=market_snapshot,
                    filter_thresholds={
                        "min_mcap": self.min_market_cap_usd,
                        "min_liquidity": self.min_liquidity_usd,
                        "min_volume": self.min_volume_24h_usd,
                        "min_age": self.min_token_age_minutes,
                        "max_pump": self.max_price_change_1h_pct if hasattr(self, 'max_price_change_1h_pct') else 0
                    },
                    error_code=error_message[:64] if error_message else None,
                    error_message=error_message
                )
                
        except Exception as e:
            logger.error("telemetry_record_error", error=str(e), token=swap.token_mint[:8])
    
    def _categorize_skip_reason(self, reason: str) -> str:
        """Categorize skip reason for analytics."""
        reason_lower = reason.lower()
        if "market_cap" in reason_lower or "mcap" in reason_lower:
            return "filter_mcap"
        elif "liquidity" in reason_lower:
            return "filter_liquidity"
        elif "volume" in reason_lower:
            return "filter_volume"
        elif "age" in reason_lower or "new" in reason_lower:
            return "filter_age"
        elif "pump" in reason_lower:
            return "filter_pump"
        elif "holder" in reason_lower or "top10" in reason_lower:
            return "filter_holders"
        elif "creator" in reason_lower:
            return "filter_creator"
        elif "recent" in reason_lower or "cooldown" in reason_lower:
            return "concurrency"
        elif "balance" in reason_lower or "insufficient" in reason_lower:
            return "insufficient_balance"
        elif "position" in reason_lower:
            return "max_positions"
        else:
            return "other"
    
    async def _confirm_transaction(self, signature: str, max_retries: int = 10, delay: float = 0.5) -> bool:
        """Confirm a transaction was finalized on-chain. Returns True if confirmed, False if failed/expired."""
        for attempt in range(max_retries):
            try:
                result = await self.rpc._request(
                    "getSignatureStatuses",
                    [[str(signature)], {"searchTransactionHistory": True}]
                )
                
                if result and "value" in result and result["value"]:
                    status = result["value"][0]
                    if status:
                        # Check for error
                        if status.get("err"):
                            logger.warning("tx_confirmed_with_error", signature=str(signature)[:16], error=status.get("err"))
                            return False
                        
                        # Check confirmation status
                        conf_status = status.get("confirmationStatus", "")
                        if conf_status in ["confirmed", "finalized"]:
                            return True
                
                # Not confirmed yet, wait and retry
                await asyncio.sleep(delay)
                delay = min(delay * 1.2, 2.0)  # Increase delay up to 2 seconds
                
            except Exception as e:
                logger.debug("tx_confirm_check_error", signature=str(signature)[:16], error=str(e))
                await asyncio.sleep(delay)
        
        logger.warning("tx_confirmation_timeout", signature=str(signature)[:16], attempts=max_retries)
        return False
    
    async def _execute_swap(
        self, 
        input_mint: str, 
        output_mint: str, 
        amount: int,
        *,
        slippage_bps: Optional[int] = None,
        priority_fee_lamports: Optional[int] = None,
        correlation_id: Optional[str] = None,
        attempt_number: int = 1
    ) -> CopyTradeResult:
        """Execute a swap via Jupiter."""
        submit_at = datetime.now(timezone.utc)
        exec_type = "buy" if input_mint == NATIVE_SOL else "sell"
        token_mint = output_mint if input_mint == NATIVE_SOL else input_mint
        
        # CRITICAL: Use high slippage for sells (memecoins move fast)
        # Sells need at least 50% slippage to avoid failed txs that burn fees
        if exec_type == "sell":
            effective_slippage_bps = max(slippage_bps or self.config.slippage_bps, 5000)  # Min 50% for sells
        else:
            effective_slippage_bps = slippage_bps if slippage_bps is not None else self.config.slippage_bps
        
        # Dynamic priority fee escalation: start low, increase on retries
        # attempt 1: base fee, attempt 2: 2x, attempt 3: 4x, etc.
        base_priority_fee = int(priority_fee_lamports if priority_fee_lamports is not None else self.jupiter_priority_fee_lamports)
        fee_multiplier = min(2 ** (attempt_number - 1), 8)  # Cap at 8x base fee
        effective_priority_fee = int(base_priority_fee * fee_multiplier)

        if exec_type == "sell":
            effective_priority_fee = min(effective_priority_fee, 1_000_000)
        else:
            effective_priority_fee = min(effective_priority_fee, 2_000_000)

        try:
            # Get quote with expanded routing options
            quote_params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(effective_slippage_bps),
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "false"
            }
            
            async with self.session.get(
                JUPITER_QUOTE_API,
                params=quote_params,
                timeout=aiohttp.ClientTimeout(total=4)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    if self.telemetry and correlation_id:
                        asyncio.create_task(self.telemetry.record_failed_execution(
                            trade_id=None,
                            correlation_id=correlation_id,
                            token_mint=token_mint,
                            execution_type=exec_type,
                            method="jupiter_quote",
                            error_code=f"http_{resp.status}",
                            error_message=error_text,
                            error_category="api_error",
                            attempt_number=attempt_number,
                            requested_amount=Decimal(str(amount)),
                            slippage_bps=effective_slippage_bps,
                            priority_fee=effective_priority_fee
                        ))
                    return CopyTradeResult(success=False, error=f"quote_failed: {error_text}")
                quote = await resp.json()

            if not quote or (isinstance(quote, dict) and (quote.get("error") or quote.get("errorCode"))):
                error_text = str(quote.get("error") or quote.get("errorCode") or "no_quote") if isinstance(quote, dict) else "no_quote"
                if self.telemetry and correlation_id:
                    asyncio.create_task(self.telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=token_mint,
                        execution_type=exec_type,
                        method="jupiter_quote",
                        error_code="quote_error",
                        error_message=error_text,
                        error_category="no_route",
                        attempt_number=attempt_number,
                        requested_amount=Decimal(str(amount)),
                        slippage_bps=effective_slippage_bps,
                        priority_fee=effective_priority_fee
                    ))
                return CopyTradeResult(success=False, error=f"quote_failed: {error_text}")

            # Get swap transaction with HIGH priority fees for fast execution
            swap_data = {
                "quoteResponse": quote,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": effective_priority_fee
            }
            
            async with self.session.post(
                JUPITER_SWAP_API,
                json=swap_data,
                timeout=aiohttp.ClientTimeout(total=6)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    if self.telemetry and correlation_id:
                        asyncio.create_task(self.telemetry.record_failed_execution(
                            trade_id=None,
                            correlation_id=correlation_id,
                            token_mint=token_mint,
                            execution_type=exec_type,
                            method="jupiter_swap",
                            error_code=f"http_{resp.status}",
                            error_message=error_text,
                            error_category="api_error",
                            attempt_number=attempt_number,
                            requested_amount=Decimal(str(amount)),
                            slippage_bps=effective_slippage_bps,
                            priority_fee=effective_priority_fee
                        ))
                    return CopyTradeResult(success=False, error=f"swap_failed: {error_text}")
                swap_response = await resp.json()
            
            # Sign and send transaction
            swap_tx_base64 = swap_response.get("swapTransaction")
            if not swap_tx_base64:
                if self.telemetry and correlation_id:
                    asyncio.create_task(self.telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=token_mint,
                        execution_type=exec_type,
                        method="jupiter_swap",
                        error_code="no_swap_transaction",
                        error_message=str(swap_response)[:5000],
                        error_category="api_error",
                        attempt_number=attempt_number,
                        requested_amount=Decimal(str(amount)),
                        slippage_bps=effective_slippage_bps,
                        priority_fee=effective_priority_fee
                    ))
                return CopyTradeResult(success=False, error="no_swap_transaction")
            
            # Decode, sign, and send
            import base64
            from solders.transaction import VersionedTransaction
            
            tx_bytes = base64.b64decode(swap_tx_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Sign the transaction
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            # Send
            signature = await self.rpc.send_transaction(signed_tx, skip_preflight=False)
            
            # CRITICAL: Confirm transaction actually succeeded on-chain
            confirmed = await self._confirm_transaction(signature)
            if not confirmed:
                if self.telemetry and correlation_id:
                    asyncio.create_task(self.telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=token_mint,
                        execution_type=exec_type,
                        method="jupiter_confirm",
                        error_code="tx_not_confirmed",
                        error_message=str(signature)[:64],
                        error_category="tx_error",
                        attempt_number=attempt_number,
                        requested_amount=Decimal(str(amount)),
                        slippage_bps=effective_slippage_bps,
                        priority_fee=effective_priority_fee
                    ))

                exec_detail = ExecutionDetails(
                    executor="bot",
                    execution_type=exec_type,
                    signature=str(signature),
                    slot=None,
                    block_time=None,
                    program_ids=None,
                    dex_used="jupiter",
                    jupiter_route=quote,
                    jupiter_route_hops=len(quote.get("routePlan", [])) if isinstance(quote.get("routePlan"), list) else None,
                    jupiter_dexes_used=[
                        (hop.get("swapInfo", {}) or {}).get("label")
                        for hop in (quote.get("routePlan") or [])
                        if isinstance(hop, dict) and (hop.get("swapInfo", {}) or {}).get("label")
                    ] if isinstance(quote.get("routePlan"), list) else None,
                    jupiter_quote_in=Decimal(str(quote.get("inAmount"))) if quote.get("inAmount") is not None else None,
                    jupiter_quote_out=Decimal(str(quote.get("outAmount"))) if quote.get("outAmount") is not None else None,
                    jupiter_price_impact_pct=Decimal(str(quote.get("priceImpactPct"))) if quote.get("priceImpactPct") is not None else None,
                    requested_in_amount=Decimal(str(amount)),
                    requested_out_min=Decimal(str(quote.get("otherAmountThreshold"))) if quote.get("otherAmountThreshold") is not None else None,
                    slippage_bps_configured=effective_slippage_bps,
                    priority_fee_lamports=effective_priority_fee,
                    submit_at=submit_at,
                    confirm_at=datetime.now(timezone.utc),
                    send_to_confirm_ms=int((datetime.now(timezone.utc) - submit_at).total_seconds() * 1000),
                    attempt_number=attempt_number,
                    total_retries=max(0, attempt_number - 1),
                    final_status="failed"
                )

                return CopyTradeResult(success=False, error=f"tx_not_confirmed: {signature[:16]}...", signature=signature, execution_details=exec_detail)

            confirm_at = datetime.now(timezone.utc)

            tx_fee_lamports = None
            compute_units_used = None
            slot = None
            block_time = None
            program_ids = None
            try:
                tx_info = await self.rpc.get_transaction(str(signature))
                if tx_info:
                    slot = tx_info.get("slot")
                    block_time_unix = tx_info.get("blockTime")
                    if block_time_unix:
                        block_time = datetime.fromtimestamp(block_time_unix, tz=timezone.utc)
                    meta = tx_info.get("meta") or {}
                    tx_fee_lamports = meta.get("fee")
                    compute_units_used = meta.get("computeUnitsConsumed")

                    instructions = ((tx_info.get("transaction") or {}).get("message") or {}).get("instructions")
                    if isinstance(instructions, list):
                        program_ids = [
                            ix.get("programId")
                            for ix in instructions
                            if isinstance(ix, dict) and ix.get("programId")
                        ] or None
            except Exception:
                pass

            route_plan = quote.get("routePlan") if isinstance(quote, dict) else None
            dexes_used = None
            if isinstance(route_plan, list):
                dexes_used = []
                for hop in route_plan:
                    if not isinstance(hop, dict):
                        continue
                    swap_info = hop.get("swapInfo") or {}
                    label = swap_info.get("label")
                    if label:
                        dexes_used.append(label)
                dexes_used = dexes_used or None

            exec_detail = ExecutionDetails(
                executor="bot",
                execution_type=exec_type,
                signature=str(signature),
                slot=slot,
                block_time=block_time,
                program_ids=program_ids,
                dex_used="jupiter",
                jupiter_route=quote,
                jupiter_route_hops=len(route_plan) if isinstance(route_plan, list) else None,
                jupiter_dexes_used=dexes_used,
                jupiter_quote_in=Decimal(str(quote.get("inAmount"))) if isinstance(quote, dict) and quote.get("inAmount") is not None else None,
                jupiter_quote_out=Decimal(str(quote.get("outAmount"))) if isinstance(quote, dict) and quote.get("outAmount") is not None else None,
                jupiter_price_impact_pct=Decimal(str(quote.get("priceImpactPct"))) if isinstance(quote, dict) and quote.get("priceImpactPct") is not None else None,
                requested_in_amount=Decimal(str(amount)),
                requested_out_min=Decimal(str(quote.get("otherAmountThreshold"))) if isinstance(quote, dict) and quote.get("otherAmountThreshold") is not None else None,
                slippage_bps_configured=effective_slippage_bps,
                priority_fee_lamports=effective_priority_fee,
                compute_units_used=compute_units_used,
                tx_fee_lamports=tx_fee_lamports,
                total_cost_sol=Decimal(str((tx_fee_lamports or 0) / 1e9)) if tx_fee_lamports is not None else None,
                submit_at=submit_at,
                confirm_at=confirm_at,
                send_to_confirm_ms=int((confirm_at - submit_at).total_seconds() * 1000),
                attempt_number=attempt_number,
                total_retries=max(0, attempt_number - 1),
                final_status="success"
            )

            return CopyTradeResult(success=True, signature=signature, execution_details=exec_detail)
            
        except Exception as e:
            if self.telemetry and correlation_id:
                asyncio.create_task(self.telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=token_mint,
                    execution_type=exec_type,
                    method="jupiter_exception",
                    error_code="exception",
                    error_message=str(e),
                    error_category="exception",
                    attempt_number=attempt_number,
                    requested_amount=Decimal(str(amount)),
                    slippage_bps=effective_slippage_bps,
                    priority_fee=effective_priority_fee
                ))
            return CopyTradeResult(success=False, error=str(e))
    
    async def _execute_pumpfun_swap(
        self,
        token_mint: str,
        sol_amount: float,
        is_buy: bool,
        sell_percentage: int = 100,  # For sells: percentage of holdings to sell (100 = all)
        *,
        correlation_id: Optional[str] = None,
        attempt_number: int = 1
    ) -> CopyTradeResult:
        """Execute a swap via PumpPortal API - tries multiple pools."""
        submit_at = datetime.now(timezone.utc)
        try:
            import base64
            from solders.transaction import VersionedTransaction
            
            action = "buy" if is_buy else "sell"
            
            # Request transaction from PumpPortal
            # Use VERY high slippage for pump.fun (tokens move extremely fast) - minimum 50% for sells
            pumpfun_slippage = max(int(self.config.slippage_bps / 100), 50 if not is_buy else 30)
            
            # Dynamic priority fee escalation: start low, increase on retries
            # attempt 1: base fee, attempt 2: 2x, attempt 3: 4x, etc.
            base_fee_buy = min(max(self.pumpfun_priority_fee_sol, 0.0005), 0.0015)
            base_fee_sell = min(max(self.pumpfun_priority_fee_sol, 0.0002), 0.001)
            fee_multiplier = min(2 ** (attempt_number - 1), 8)  # Cap at 8x
            priority_fee = (base_fee_buy if is_buy else base_fee_sell) * fee_multiplier

            priority_fee = min(priority_fee, 0.0015 if is_buy else 0.001)
            
            pools_to_try = ["auto", "pump", "pump-amm", "raydium", "raydium-cpmm", "launchlab", "bonk"]
            last_error = None
            
            for pool in pools_to_try:
                if is_buy:
                    payload = {
                        "publicKey": str(self.wallet.pubkey()),
                        "action": action,
                        "mint": token_mint,
                        "denominatedInSol": "true",
                        "amount": sol_amount,
                        "slippage": pumpfun_slippage,
                        "priorityFee": priority_fee,
                        "pool": pool
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
                        "priorityFee": priority_fee,
                        "pool": pool
                    }
                
                logger.info(
                    "pumpfun_swap_request",
                    action=action,
                    token=token_mint[:8] + "...",
                    full_mint=token_mint,
                    sol=sol_amount,
                    pool=pool
                )
                logger.info(
                    "pumpfun_swap_request_details",
                    action=action,
                    token=token_mint[:8] + "...",
                    full_mint=token_mint,
                    sol=sol_amount,
                    pool=pool,
                    payload=payload
                )
                
                tx_bytes = None
                status_code = None
                error_text = None
                async with self.session.post(
                    PUMPFUN_API,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    status_code = resp.status
                    if resp.status == 200:
                        tx_bytes = await resp.read()
                    else:
                        error_text = await resp.text()

                if tx_bytes is None and status_code == 400:
                    try:
                        async with self.session.post(
                            PUMPFUN_API,
                            data=payload,
                            timeout=aiohttp.ClientTimeout(total=6)
                        ) as resp2:
                            status_code = resp2.status
                            if resp2.status == 200:
                                tx_bytes = await resp2.read()
                            else:
                                error_text2 = await resp2.text()
                                error_text = f"{error_text} | form: {error_text2}" if error_text else error_text2
                    except Exception as e:
                        error_text = f"{error_text} | form_exception: {e}" if error_text else f"form_exception: {e}"

                if tx_bytes is None:
                    last_error = f"{pool}: {error_text or 'unknown_error'}"
                    logger.debug(
                        "pumpfun_pool_failed",
                        pool=pool,
                        status=status_code,
                        response=str(error_text or "")[:100],
                        mint=token_mint[:8]
                    )
                    if self.telemetry and correlation_id:
                        asyncio.create_task(self.telemetry.record_failed_execution(
                            trade_id=None,
                            correlation_id=correlation_id,
                            token_mint=token_mint,
                            execution_type="buy" if is_buy else "sell",
                            method=f"pumpfun_{action}",
                            error_code=f"http_{status_code or 'unknown'}",
                            error_message=last_error,
                            error_category="api_error",
                            attempt_number=attempt_number,
                            requested_amount=Decimal(str(sol_amount)) if is_buy else None,
                            slippage_bps=int(pumpfun_slippage * 100),
                            priority_fee=int(priority_fee * 1e9)
                        ))
                    continue  # Try next pool
                
                # Deserialize and sign the transaction
                tx = VersionedTransaction.from_bytes(tx_bytes)
                signed_tx = VersionedTransaction(tx.message, [self.wallet])
                
                # Send the transaction
                signature = await self.rpc.send_transaction(signed_tx, skip_preflight=False)
                
                # CRITICAL: Confirm transaction actually succeeded on-chain
                confirmed = await self._confirm_transaction(signature)
                if not confirmed:
                    logger.warning(
                        "pumpfun_tx_not_confirmed",
                        action=action,
                        token=token_mint[:8],
                        pool=pool,
                        signature=str(signature)[:16] if signature else None
                    )
                    last_error = f"{pool}: tx_not_confirmed"
                    if self.telemetry and correlation_id:
                        asyncio.create_task(self.telemetry.record_failed_execution(
                            trade_id=None,
                            correlation_id=correlation_id,
                            token_mint=token_mint,
                            execution_type="buy" if is_buy else "sell",
                            method=f"pumpfun_{action}",
                            error_code="tx_not_confirmed",
                            error_message=last_error,
                            error_category="tx_error",
                            attempt_number=attempt_number,
                            requested_amount=Decimal(str(sol_amount)) if is_buy else None,
                            slippage_bps=int(pumpfun_slippage * 100),
                            priority_fee=int(priority_fee * 1e9)
                        ))
                    continue  # Try next pool
                
                logger.info(
                    "pumpfun_swap_success",
                    action=action,
                    token=token_mint[:8],
                    pool=pool,
                    signature=str(signature)[:16] if signature else None
                )

                confirm_at = datetime.now(timezone.utc)

                tx_fee_lamports = None
                compute_units_used = None
                slot = None
                block_time = None
                program_ids = None
                try:
                    tx_info = await self.rpc.get_transaction(str(signature))
                    if tx_info:
                        slot = tx_info.get("slot")
                        block_time_unix = tx_info.get("blockTime")
                        if block_time_unix:
                            block_time = datetime.fromtimestamp(block_time_unix, tz=timezone.utc)
                        meta = tx_info.get("meta") or {}
                        tx_fee_lamports = meta.get("fee")
                        compute_units_used = meta.get("computeUnitsConsumed")

                        instructions = ((tx_info.get("transaction") or {}).get("message") or {}).get("instructions")
                        if isinstance(instructions, list):
                            program_ids = [
                                ix.get("programId")
                                for ix in instructions
                                if isinstance(ix, dict) and ix.get("programId")
                            ] or None
                except Exception:
                    pass

                exec_detail = ExecutionDetails(
                    executor="bot",
                    execution_type="buy" if is_buy else "sell",
                    signature=str(signature),
                    slot=slot,
                    block_time=block_time,
                    program_ids=program_ids,
                    dex_used="pump.fun",
                    pumpfun_pool_type=pool,
                    requested_in_amount=Decimal(str(sol_amount)) if is_buy else None,
                    slippage_bps_configured=int(pumpfun_slippage * 100),
                    priority_fee_lamports=int(priority_fee * 1e9),
                    compute_units_used=compute_units_used,
                    tx_fee_lamports=tx_fee_lamports,
                    total_cost_sol=Decimal(str((tx_fee_lamports or 0) / 1e9)) if tx_fee_lamports is not None else None,
                    submit_at=submit_at,
                    confirm_at=confirm_at,
                    send_to_confirm_ms=int((confirm_at - submit_at).total_seconds() * 1000),
                    attempt_number=attempt_number,
                    total_retries=max(0, attempt_number - 1),
                    final_status="success"
                )

                # Close empty token account after successful sell
                if not is_buy:
                    asyncio.create_task(self._close_empty_token_accounts(token_mint))

                return CopyTradeResult(success=True, signature=signature, execution_details=exec_detail)
            
            # All pools failed
            logger.warning("pumpfun_all_pools_failed", token=token_mint[:8], last_error=last_error)
            if self.telemetry and correlation_id:
                asyncio.create_task(self.telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=token_mint,
                    execution_type="buy" if is_buy else "sell",
                    method=f"pumpfun_{action}",
                    error_code="all_pools_failed",
                    error_message=str(last_error)[:5000] if last_error else "all_pools_failed",
                    error_category="no_route",
                    attempt_number=attempt_number,
                    requested_amount=Decimal(str(sol_amount)) if is_buy else None,
                    slippage_bps=int(pumpfun_slippage * 100),
                    priority_fee=int(priority_fee * 1e9)
                ))
            return CopyTradeResult(success=False, error=f"pumpfun_api_failed: {last_error}")
            
        except Exception as e:
            logger.error("pumpfun_swap_error", error=str(e))
            if self.telemetry and correlation_id:
                asyncio.create_task(self.telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=token_mint,
                    execution_type="buy" if is_buy else "sell",
                    method=f"pumpfun_{'buy' if is_buy else 'sell'}",
                    error_code="exception",
                    error_message=str(e),
                    error_category="exception",
                    attempt_number=attempt_number,
                    requested_amount=Decimal(str(sol_amount)) if is_buy else None,
                    slippage_bps=int(pumpfun_slippage * 100) if 'pumpfun_slippage' in locals() else None,
                    priority_fee=int(priority_fee * 1e9) if 'priority_fee' in locals() else None
                ))
            return CopyTradeResult(success=False, error=f"pumpfun_error: {str(e)}")
    
    async def _sell_via_jupiter(self, token_mint: str) -> CopyTradeResult:
        """Sell all tokens via Jupiter as fallback when pump.fun fails."""
        try:
            import base64
            from solders.transaction import VersionedTransaction
            from solders.pubkey import Pubkey
            
            # First get actual token balance
            token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            wallet_pubkey = self.wallet.pubkey()
            
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    wallet_pubkey,
                    {"mint": token_mint},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if not result or "value" not in result or not result["value"]:
                return CopyTradeResult(success=False, error="no_token_account")
            
            amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            if amount == 0:
                return CopyTradeResult(success=False, error="zero_balance")
            
            logger.info("jupiter_sell_attempt", token=token_mint[:8], amount=amount)
            
            # Use the same Jupiter swap path as _execute_swap (uses lite-api.jup.ag)
            # This avoids DNS issues with quote-api.jup.ag on some servers.
            result = await self._execute_swap(
                input_mint=token_mint,
                output_mint=NATIVE_SOL,
                amount=amount,
                slippage_bps=5000
            )

            if result.success:
                logger.info("jupiter_sell_success", token=token_mint[:8], signature=str(result.signature)[:16] if result.signature else None)
                # Close empty token account to reclaim rent
                asyncio.create_task(self._close_empty_token_accounts(token_mint))
            return result
            
        except Exception as e:
            logger.error("jupiter_sell_error", token=token_mint[:8], error=str(e))
            return CopyTradeResult(success=False, error=f"jupiter_error: {str(e)}")

    async def _close_empty_token_accounts(self, token_mint: str) -> None:
        """Close empty token accounts for a given mint to reclaim rent SOL."""
        try:
            from solders.pubkey import Pubkey
            from solders.instruction import Instruction, AccountMeta
            from solders.transaction import Transaction
            from solders.message import Message
            from solders.hash import Hash

            wallet_pubkey = self.wallet.pubkey()

            token_programs = [
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            ]

            for program_id in token_programs:
                result = await self.rpc._request(
                    "getTokenAccountsByOwner",
                    [
                        str(wallet_pubkey),
                        {"mint": token_mint},
                        {"encoding": "jsonParsed", "commitment": "confirmed"}
                    ]
                )

                if not result or "value" not in result:
                    continue

                for account in result["value"] or []:
                    try:
                        pubkey_str = account.get("pubkey", "")
                        info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        amount = int((info.get("tokenAmount") or {}).get("amount", 0))
                        owner = info.get("owner", "")

                        if amount != 0 or owner != str(wallet_pubkey):
                            continue

                        account_pubkey = Pubkey.from_string(pubkey_str)
                        token_program_pubkey = Pubkey.from_string(program_id)

                        close_ix = Instruction(
                            program_id=token_program_pubkey,
                            accounts=[
                                AccountMeta(pubkey=account_pubkey, is_signer=False, is_writable=True),
                                AccountMeta(pubkey=wallet_pubkey, is_signer=False, is_writable=True),
                                AccountMeta(pubkey=wallet_pubkey, is_signer=True, is_writable=False),
                            ],
                            data=bytes([9]),  # CloseAccount instruction index
                        )

                        blockhash_str = await self.rpc.get_latest_blockhash()
                        blockhash = Hash.from_string(blockhash_str)
                        msg = Message.new_with_blockhash([close_ix], wallet_pubkey, blockhash)
                        tx = Transaction.new_unsigned(msg)
                        tx.sign([self.wallet], blockhash)

                        sig = await self.rpc.send_transaction(tx, skip_preflight=True)
                        logger.info(
                            "token_account_closed",
                            token=token_mint[:8],
                            account=pubkey_str[:12],
                            signature=sig[:16] if sig else "none",
                        )
                    except Exception as e:
                        logger.debug("close_account_error", token=token_mint[:8], error=str(e))

        except Exception as e:
            logger.debug("close_empty_accounts_error", token=token_mint[:8], error=str(e))

    def _simulate_mock_buy(self, swap: 'ParsedSwap', trade_sol: float, *, correlation_id: Optional[str] = None) -> 'CopyTradeResult':
        """Simulate a buy trade without executing on-chain."""
        # Get wallet-specific state
        wallet = swap.wallet
        state = self._get_wallet_state(wallet)
        state.setdefault('correlation_ids', {})
        
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
            if correlation_id:
                state['correlation_ids'][swap.token_mint] = correlation_id
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
        state.setdefault('correlation_ids', {})
        
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
            sol_received = token_balance / 1e9
        
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
        correlation_id = state.get('correlation_ids', {}).pop(swap.token_mint, None)
        
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
        """Get token balance for our wallet by finding the associated token account.
        
        Checks BOTH SPL Token and Token-2022 programs to handle all pump.fun tokens.
        """
        if self.mock_trading and wallet and self._is_shadow_wallet(wallet):
            state = self._get_wallet_state(wallet)
            return state.get('positions', {}).get(mint, 0)
        
        try:
            wallet_pubkey = self.wallet.pubkey()

            # Fast path: mint filter (works on most RPCs for both SPL + Token-2022)
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
                    # No token account = trader doesn't hold this token
                    logger.info("trader_exited_no_account", trader=wallet[:8], token=mint[:8])
                    return 0
                
                # Check if balance is > 0
                account_data = accounts[0].get("account", {}).get("data", {})
                parsed = account_data.get("parsed", {}).get("info", {})
                token_amount = parsed.get("tokenAmount", {})
                amount = int(token_amount.get("amount", 0))
                
                if amount > 0:
                    logger.info("token_balance_found", token=mint[:8], amount=amount)
                    return amount

            # Fallback: query BOTH token programs and filter by mint client-side
            token_programs = [
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
            ]

            for program_id in token_programs:
                try:
                    result = await self.rpc._request(
                        "getTokenAccountsByOwner",
                        [
                            str(wallet_pubkey),
                            {"programId": program_id},
                            {"encoding": "jsonParsed"}
                        ]
                    )
                    if not result or "value" not in result:
                        continue

                    for acct in result["value"] or []:
                        account_data = acct.get("account", {}).get("data", {})
                        info = account_data.get("parsed", {}).get("info", {})
                        if info.get("mint") != mint:
                            continue
                        token_amount = info.get("tokenAmount", {})
                        amount = int(token_amount.get("amount", 0))
                        if amount > 0:
                            logger.info(
                                "token_balance_found",
                                token=mint[:8],
                                amount=amount,
                                program="token-2022" if "Tokenz" in program_id else "spl-token"
                            )
                            return amount

                except Exception:
                    continue

            return 0
        except Exception as e:
            logger.debug("get_token_balance_error", mint=mint[:8], error=str(e))
            return 0
    
    async def _check_trader_holds_token(self, trader_wallet: str, mint: str) -> bool:
        """Check if the tracked trader still holds a specific token on-chain.
        
        This is CRITICAL for missed sell detection - if trader sold and we missed it,
        we need to know so we can sync our position.
        
        Returns:
            True if trader still holds the token, False if they've exited
        """
        try:
            # Use getTokenAccountsByOwner RPC call to check trader's balance
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    trader_wallet,
                    {"mint": mint},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if result and "value" in result:
                accounts = result["value"]
                if not accounts:
                    # No token account = trader doesn't hold this token
                    logger.info("trader_exited_no_account", trader=trader_wallet[:8], token=mint[:8])
                    return False
                
                # Check if balance is > 0
                account_data = accounts[0].get("account", {}).get("data", {})
                parsed = account_data.get("parsed", {}).get("info", {})
                token_amount = parsed.get("tokenAmount", {})
                amount = int(token_amount.get("amount", 0))
                
                if amount > 0:
                    logger.debug("trader_still_holds", trader=trader_wallet[:8], token=mint[:8], amount=amount)
                    return True
                else:
                    logger.info("trader_exited_zero_balance", trader=trader_wallet[:8], token=mint[:8])
                    return False
            else:
                # RPC returned unexpected format - log it
                logger.warning("trader_check_unexpected_result", trader=trader_wallet[:8], token=mint[:8], result=str(result)[:100])
                return True
            
        except Exception as e:
            logger.warning("check_trader_holds_error", trader=trader_wallet[:8], token=mint[:8], error=str(e))
            # On error, assume trader still holds (safer)
            return True
    
    async def _clear_recent_copy(self, wallet: str, token_mint: str, delay: int) -> None:
        """Remove token from a wallet's recent copies after delay."""
        await asyncio.sleep(delay)
        self._get_recent_copies(wallet).discard(token_mint)
    
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

    async def _execute_real_trade_with_fallbacks(
        self,
        swap: ParsedSwap,
        trade_lamports: int,
        trade_sol: float,
        is_pumpfun: bool,
        correlation_id: str
    ) -> CopyTradeResult:
        """Execute a real trade with fallbacks for failed executions."""
        max_retries = 3
        for attempt in range(max_retries):
            if is_pumpfun:
                result = await self._execute_pumpfun_swap(
                    token_mint=swap.token_mint,
                    sol_amount=trade_sol,
                    is_buy=True,
                    correlation_id=correlation_id,
                    attempt_number=attempt + 1
                )
            else:
                result = await self._execute_swap(
                    input_mint=NATIVE_SOL,
                    output_mint=swap.token_mint,
                    amount=trade_lamports,
                    correlation_id=correlation_id,
                    attempt_number=attempt + 1
                )
            
            if result.success:
                return result
            
            # Exponential backoff: wait 2^attempt seconds before retrying
            wait_time = 2 ** attempt
            logger.info("real_trade_retry_wait", attempt=attempt+1, wait_time=wait_time, token=swap.token_mint[:8])
            await asyncio.sleep(wait_time)
        
        return CopyTradeResult(success=False, error="all_retries_failed")

    async def _log_real_trade_to_state(
        self,
        swap: ParsedSwap,
        trade_sol: float,
        trade_type: str,
        signature: str
    ) -> None:
        """Log real trade to state file for dashboard display."""
        if self.mock_trading:
            state = self._get_wallet_state(swap.wallet)
            trades = state.setdefault('trades_history', [])
            trades.append({
                'type': trade_type,
                'token': swap.token_mint[:8],
                'full_mint': swap.token_mint,
                'sol': trade_sol,
                'signature': signature,
                'timestamp': datetime.now().isoformat()
            })
            self._save_wallet_state(swap.wallet)
