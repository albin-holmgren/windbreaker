"""
Position Manager - Tracks open positions and handles auto-sell logic.
Implements take-profit, stop-loss, and time-based exits.
"""

import asyncio
import aiohttp
import json
import os
import uuid
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import structlog

from .trade_telemetry import get_telemetry, ExecutionDetails

logger = structlog.get_logger(__name__)

# Jupiter API - lite-api is more reliable
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://lite-api.jup.ag/swap/v1/swap"

# Pump.fun API for bonding curve trades
PUMPFUN_API = "https://pumpportal.fun/api/trade-local"

NATIVE_SOL = "So11111111111111111111111111111111111111112"


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_LIMIT = "time_limit"
    MANUAL = "manual"
    RUG_DETECTED = "rug_detected"
    ABANDONED = "abandoned"  # Token too worthless to sell, just free the slot
    COPIED_SELL = "copied_sell"  # Trader we copied sold
    MCAP_STOP_LOSS = "mcap_stop_loss"  # Market cap dropped below threshold


@dataclass
class Position:
    """Represents an open position."""
    token_mint: str
    token_symbol: Optional[str]
    entry_sol: float          # SOL spent to buy
    token_amount: int         # Tokens received
    entry_time: datetime
    entry_signature: str
    copied_from: str          # Wallet we copied
    dex: str = "jupiter"      # DEX used for buy (pump.fun, raydium, jupiter)
    correlation_id: str = ""  # Links entry to exit in telemetry
    
    # Tracking
    current_value_sol: float = 0.0
    last_price_check: Optional[datetime] = None
    highest_value_sol: float = 0.0  # For trailing stop
    
    @property
    def age_minutes(self) -> float:
        return (datetime.utcnow() - self.entry_time).total_seconds() / 60
    
    @property
    def pnl_percent(self) -> float:
        if self.entry_sol == 0:
            return 0
        return ((self.current_value_sol - self.entry_sol) / self.entry_sol) * 100
    
    @property
    def is_profitable(self) -> bool:
        return self.current_value_sol > self.entry_sol


@dataclass
class SellResult:
    success: bool
    signature: Optional[str] = None
    sol_received: float = 0.0
    reason: ExitReason = ExitReason.MANUAL
    error: Optional[str] = None


class PositionManager:
    """
    Manages open positions - follows trader strategy.
    
    Features:
    - Track positions from copied trades
    - Abandon rugged tokens (don't sell, just free slot)
    - Optional take profit (safety limit)
    - Copy sells from trader
    - Max concurrent positions
    """
    
    def __init__(
        self,
        config,
        wallet_keypair,
        rpc_client,
        max_positions: int = 3,
        take_profit_pct: float = 0,         # DISABLED - follow the trader
        stop_loss_pct: float = -60.0,       # Stop loss at -60%
        time_limit_minutes: float = 0,      # 0 = disabled (follow trader)
        trailing_stop_pct: float = 0,       # 0 = disabled
        rug_abandon_sol: float = 0.005,     # Abandon if worth < 0.005 SOL
        check_interval_sec: float = 60.0,   # Check prices every 60s
        mcap_stop_loss_usd: float = 0,      # 0 = disabled, sell if mcap drops below
    ):
        self.config = config
        self.wallet = wallet_keypair
        self.rpc = rpc_client
        self.mcap_stop_loss_usd = mcap_stop_loss_usd
        
        # Cache for market caps
        self.mcap_cache: Dict[str, tuple[float, float]] = {}  # mint -> (mcap, timestamp)
        
        # Settings
        self.max_positions = max_positions
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.time_limit_minutes = time_limit_minutes
        self.trailing_stop_pct = trailing_stop_pct
        self.rug_abandon_sol = rug_abandon_sol  # Threshold to abandon (not sell)
        self.check_interval = check_interval_sec
        
        # State
        self.positions: Dict[str, Position] = {}  # token_mint -> Position
        self.abandoned_tokens: Dict[str, float] = {}  # token_mint -> entry_sol (for stats)
        self.failed_sells: Dict[str, int] = {}  # token_mint -> token_amount (queued for retry)
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        
        # Stats
        self.total_sells = 0
        self.total_abandoned = 0
        self.total_profit_sol = 0.0
        self.total_loss_sol = 0.0
    
    async def start(self, state_file: str = "real_state.json") -> None:
        """Start the position manager."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        # CRITICAL: Load positions from state file to survive restarts
        loaded_count = self._load_positions_from_state(state_file)
        
        logger.info(
            "position_manager_started",
            max_positions=self.max_positions,
            take_profit=f"{self.take_profit_pct}%",
            stop_loss=f"{self.stop_loss_pct}%",
            time_limit=f"{self.time_limit_minutes}min",
            loaded_positions=loaded_count
        )
        
        # Start monitoring loop
        asyncio.create_task(self._monitor_loop())
        
        # Start failed sells retry loop
        asyncio.create_task(self._retry_failed_sells_loop())
    
    def _load_positions_from_state(self, state_file: str) -> int:
        """Load positions from real_state.json to survive restarts.
        
        CRITICAL: Without this, bot restarts clear all positions and
        mcap stop loss / other protections never run.
        """
        try:
            if not os.path.exists(state_file):
                return 0
            
            with open(state_file, 'r') as f:
                state = json.load(f)
            
            positions_data = state.get("positions", {})
            entry_times = state.get("entry_times", {})
            entry_sol = state.get("entry_sol", {})
            
            loaded = 0
            for token_mint, balance in positions_data.items():
                # Skip tokens with 0 balance (already sold)
                if balance == 0:
                    continue
                
                # Skip if already tracked
                if token_mint in self.positions:
                    continue
                
                # Get entry time, default to now if missing
                entry_time_str = entry_times.get(token_mint)
                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str)
                    except:
                        entry_time = datetime.utcnow()
                else:
                    entry_time = datetime.utcnow()
                
                # Get entry SOL amount
                entry_amount = entry_sol.get(token_mint, 0.03)  # Default to 0.03
                
                position = Position(
                    token_mint=token_mint,
                    token_symbol=token_mint[:8],
                    entry_sol=entry_amount,
                    token_amount=0,  # Will be updated on first check
                    entry_time=entry_time,
                    entry_signature="loaded_from_state",
                    copied_from="unknown",  # Not stored in state
                    dex="unknown",
                    current_value_sol=entry_amount,
                    highest_value_sol=entry_amount
                )
                
                self.positions[token_mint] = position
                loaded += 1
                
                logger.info(
                    "position_loaded_from_state",
                    token=token_mint[:8],
                    entry_sol=f"{entry_amount:.4f}",
                    age_minutes=f"{position.age_minutes:.1f}"
                )
            
            return loaded
            
        except Exception as e:
            logger.warning("failed_to_load_positions", error=str(e))
            return 0
    
    async def stop(self) -> None:
        """Stop the position manager."""
        self.running = False
        if self.session:
            await self.session.close()
        
        logger.info(
            "position_manager_stopped",
            open_positions=len(self.positions),
            total_sells=self.total_sells,
            total_profit=f"{self.total_profit_sol:.4f}",
            total_loss=f"{self.total_loss_sol:.4f}"
        )
    
    def can_open_position(self) -> bool:
        """Check if we can open a new position."""
        return len(self.positions) < self.max_positions
    
    def has_position(self, token_mint: str) -> bool:
        """Check if we already have a position in this token."""
        return token_mint in self.positions
    
    def add_position(
        self,
        token_mint: str,
        entry_sol: float,
        token_amount: int,
        entry_signature: str,
        copied_from: str,
        token_symbol: Optional[str] = None,
        dex: str = "jupiter",
        correlation_id: Optional[str] = None
    ) -> Position:
        """Add a new position."""
        position = Position(
            token_mint=token_mint,
            token_symbol=token_symbol,
            entry_sol=entry_sol,
            token_amount=token_amount,
            entry_time=datetime.utcnow(),
            entry_signature=entry_signature,
            copied_from=copied_from,
            dex=dex,
            correlation_id=correlation_id or str(uuid.uuid4()),
            current_value_sol=entry_sol,
            highest_value_sol=entry_sol
        )
        
        self.positions[token_mint] = position
        
        logger.info(
            "position_opened",
            token=token_mint[:8] + "...",
            entry_sol=f"{entry_sol:.4f}",
            tokens=token_amount,
            open_positions=len(self.positions)
        )
        
        return position
    
    async def trigger_sell(self, token_mint: str, reason: ExitReason = ExitReason.COPIED_SELL, max_retries: int = 5) -> SellResult:
        """
        Trigger a sell for a specific token with aggressive retries.
        Called when the copied trader sells - MUST succeed!
        """
        if token_mint not in self.positions:
            return SellResult(success=False, error="no_position_for_token")
        
        logger.info(
            "trader_sold_copying",
            token=token_mint[:8] + "...",
            reason=reason.value,
            message="URGENT: Copying trader's sell!"
        )
        
        # Aggressive retry loop with exponential backoff
        for attempt in range(max_retries):
            result = await self._sell_position(token_mint, reason, attempt_number=attempt + 1)
            
            if result.success:
                logger.info(
                    "sell_success",
                    token=token_mint[:8],
                    attempt=attempt + 1,
                    sol_received=f"{result.sol_received:.4f}"
                )
                return result
            
            # Exponential backoff: 0.5s, 1s, 2s, 4s, 8s
            delay = 0.5 * (2 ** attempt)
            logger.warning(
                "sell_retry",
                token=token_mint[:8],
                attempt=attempt + 1,
                max_retries=max_retries,
                next_retry_sec=delay,
                error=result.error
            )
            await asyncio.sleep(delay)
        
        logger.error(
            "sell_failed_all_retries",
            token=token_mint[:8],
            attempts=max_retries
        )
        return SellResult(success=False, error=f"failed_after_{max_retries}_retries", reason=reason)
    
    def get_position(self, token_mint: str) -> Optional[Position]:
        """Get a position by token mint."""
        return self.positions.get(token_mint)
    
    def queue_failed_sell(self, token_mint: str, token_amount: int) -> None:
        """Queue a failed sell for background retry."""
        self.failed_sells[token_mint] = token_amount
        logger.info(
            "sell_queued_for_retry",
            token=token_mint[:8],
            amount=token_amount,
            queue_size=len(self.failed_sells)
        )
    
    async def _retry_failed_sells_loop(self) -> None:
        """Background loop to retry failed sells every 10 seconds."""
        while self.running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                if not self.failed_sells:
                    continue
                
                # Process failed sells
                for token_mint, token_amount in list(self.failed_sells.items()):
                    logger.info(
                        "retrying_failed_sell",
                        token=token_mint[:8],
                        amount=token_amount
                    )
                    
                    # Try to sell
                    try:
                        position = self.positions.get(token_mint)
                        correlation_id = position.correlation_id if position and position.correlation_id else str(uuid.uuid4())
                        result = await self._execute_direct_sell(token_mint, token_amount, correlation_id=correlation_id)
                        
                        if result.success:
                            logger.info(
                                "retry_sell_success",
                                token=token_mint[:8],
                                sol_received=f"{result.sol_received:.4f}"
                            )
                            # Remove from queue
                            del self.failed_sells[token_mint]
                            # Also remove from positions if tracked
                            if token_mint in self.positions:
                                del self.positions[token_mint]
                        else:
                            logger.warning(
                                "retry_sell_failed",
                                token=token_mint[:8],
                                error=result.error
                            )
                    except Exception as e:
                        logger.warning("retry_sell_error", token=token_mint[:8], error=str(e))
                    
                    # Small delay between retries
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                logger.error("retry_loop_error", error=str(e))
    
    async def _execute_direct_sell(self, token_mint: str, token_amount: int, *, correlation_id: Optional[str] = None) -> SellResult:
        """Execute a direct sell without position tracking - tries Jupiter then PumpPortal."""
        import base64
        from solders.transaction import VersionedTransaction
        
        telemetry = get_telemetry()

        # First try Jupiter
        try:
            quote = await self._get_quote(
                input_mint=token_mint,
                output_mint=NATIVE_SOL,
                amount=token_amount
            )
            
            if quote:
                swap_data = {
                    "quoteResponse": quote,
                    "userPublicKey": str(self.wallet.pubkey()),
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": 500000  # Very high priority for retries
                }
                
                async with self.session.post(
                    JUPITER_SWAP_API,
                    json=swap_data,
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        swap_response = await resp.json()
                        swap_tx = swap_response.get("swapTransaction")
                        if swap_tx:
                            tx_bytes = base64.b64decode(swap_tx)
                            tx = VersionedTransaction.from_bytes(tx_bytes)
                            signed_tx = VersionedTransaction(tx.message, [self.wallet])
                            signature = await self.rpc.send_transaction(signed_tx, skip_preflight=True)
                            sol_received = int(quote.get("outAmount", 0)) / 1e9

                            if telemetry and correlation_id:
                                exec_detail = ExecutionDetails(
                                    executor="bot",
                                    execution_type="sell",
                                    signature=str(signature),
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
                                    requested_in_amount=Decimal(str(token_amount)),
                                    slippage_bps_configured=self.config.slippage_bps,
                                    priority_fee_lamports=500000,
                                    final_status="submitted"
                                )
                                asyncio.create_task(telemetry.record_execution_details(
                                    correlation_id=correlation_id,
                                    exec_detail=exec_detail
                                ))

                            return SellResult(success=True, signature=signature, sol_received=sol_received, reason=ExitReason.COPIED_SELL)
                    else:
                        if telemetry and correlation_id:
                            error_text = await resp.text()
                            asyncio.create_task(telemetry.record_failed_execution(
                                trade_id=None,
                                correlation_id=correlation_id,
                                token_mint=token_mint,
                                execution_type="sell",
                                method="jupiter_swap",
                                error_code=f"http_{resp.status}",
                                error_message=error_text,
                                error_category="api_error",
                                attempt_number=1,
                                requested_amount=Decimal(str(token_amount)),
                                slippage_bps=self.config.slippage_bps,
                                priority_fee=500000
                            ))
            else:
                if telemetry and correlation_id:
                    asyncio.create_task(telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=token_mint,
                        execution_type="sell",
                        method="jupiter_quote",
                        error_code="no_quote",
                        error_message="no_quote",
                        error_category="no_route",
                        attempt_number=1,
                        requested_amount=Decimal(str(token_amount)),
                        slippage_bps=self.config.slippage_bps,
                        priority_fee=500000
                    ))
        except Exception as e:
            logger.debug("direct_sell_jupiter_failed", token=token_mint[:8], error=str(e))
            if telemetry and correlation_id:
                asyncio.create_task(telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=token_mint,
                    execution_type="sell",
                    method="jupiter_exception",
                    error_code="exception",
                    error_message=str(e),
                    error_category="exception",
                    attempt_number=1,
                    requested_amount=Decimal(str(token_amount)),
                    slippage_bps=self.config.slippage_bps,
                    priority_fee=500000
                ))
        
        # Fallback to PumpPortal - try multiple pools
        pumpfun_slippage = max(self.config.slippage_bps / 100, 30)
        pools_to_try = ["auto", "pump", "pump-amm", "raydium", "raydium-cpmm", "launchlab"]
        last_error = None
        
        for pool in pools_to_try:
            try:
                payload = {
                    "publicKey": str(self.wallet.pubkey()),
                    "action": "sell",
                    "mint": token_mint,
                    "denominatedInSol": "false",
                    "amount": "100%",
                    "slippage": pumpfun_slippage,
                    "priorityFee": 0.005,
                    "pool": pool
                }
                
                async with self.session.post(
                    PUMPFUN_API,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    if resp.status != 200:
                        last_error = await resp.text()
                        if telemetry and correlation_id:
                            asyncio.create_task(telemetry.record_failed_execution(
                                trade_id=None,
                                correlation_id=correlation_id,
                                token_mint=token_mint,
                                execution_type="sell",
                                method="pumpfun_sell",
                                error_code=f"{pool}_http_{resp.status}",
                                error_message=last_error,
                                error_category="api_error",
                                attempt_number=1,
                                slippage_bps=int(pumpfun_slippage * 100),
                                priority_fee=int(0.005 * 1e9)
                            ))
                        continue
                    tx_bytes = await resp.read()
                
                tx = VersionedTransaction.from_bytes(tx_bytes)
                signed_tx = VersionedTransaction(tx.message, [self.wallet])
                signature = await self.rpc.send_transaction(signed_tx, skip_preflight=True)
                
                logger.info("direct_sell_pumpfun_success", token=token_mint[:8], pool=pool)
                if telemetry and correlation_id:
                    exec_detail = ExecutionDetails(
                        executor="bot",
                        execution_type="sell",
                        signature=str(signature),
                        dex_used="pump.fun",
                        pumpfun_pool_type=pool,
                        slippage_bps_configured=int(pumpfun_slippage * 100),
                        priority_fee_lamports=int(0.005 * 1e9),
                        final_status="submitted"
                    )
                    asyncio.create_task(telemetry.record_execution_details(
                        correlation_id=correlation_id,
                        exec_detail=exec_detail
                    ))
                return SellResult(success=True, signature=signature, sol_received=0, reason=ExitReason.COPIED_SELL)
            except Exception as e:
                last_error = str(e)
                if telemetry and correlation_id:
                    asyncio.create_task(telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=token_mint,
                        execution_type="sell",
                        method="pumpfun_sell",
                        error_code=f"{pool}_exception",
                        error_message=last_error,
                        error_category="exception",
                        attempt_number=1,
                        slippage_bps=int(pumpfun_slippage * 100),
                        priority_fee=int(0.005 * 1e9)
                    ))
                continue
        
        return SellResult(success=False, error=f"all_methods_failed: {last_error}")
    
    async def _monitor_loop(self) -> None:
        """Main loop to monitor positions and trigger sells."""
        while self.running:
            try:
                await self._check_all_positions()
            except Exception as e:
                logger.error("monitor_loop_error", error=str(e))
            
            await asyncio.sleep(self.check_interval)
    
    async def _check_all_positions(self) -> None:
        """Check all positions and sell if needed."""
        if not self.positions:
            return
        
        positions_to_sell = []
        
        for token_mint, position in list(self.positions.items()):
            try:
                # Update price
                await self._update_position_value(position)
                
                # Check exit conditions (price-based)
                exit_reason = self._should_exit(position)
                
                # Check market cap stop loss (if enabled)
                if not exit_reason:
                    exit_reason = await self._check_mcap_stop_loss(position)
                
                # CRITICAL: Check if trader has exited (missed sell detection)
                # Only check after holding for at least 2 minutes to avoid race conditions
                if not exit_reason and position.age_minutes > 2:
                    trader_exited = await self._check_trader_exited(position)
                    if trader_exited:
                        exit_reason = ExitReason.COPIED_SELL
                        logger.warning(
                            "missed_sell_detected_real",
                            token=token_mint[:8],
                            trader=position.copied_from[:8],
                            age_minutes=f"{position.age_minutes:.1f}",
                            message="Trader exited but we missed the sell - syncing now!"
                        )
                
                if exit_reason:
                    positions_to_sell.append((token_mint, exit_reason))
                    
            except Exception as e:
                logger.warning(
                    "position_check_error",
                    token=token_mint[:8],
                    error=str(e)
                )
        
        # Execute sells
        for token_mint, reason in positions_to_sell:
            await self._sell_position(token_mint, reason)
    
    async def _update_position_value(self, position: Position) -> None:
        """Update the current value of a position."""
        try:
            # CRITICAL: Fetch actual on-chain balance instead of using placeholder
            actual_balance = await self._get_actual_token_balance(position.token_mint)
            if actual_balance and actual_balance > 0:
                if actual_balance != position.token_amount:
                    logger.debug(
                        "updating_token_amount",
                        token=position.token_mint[:8],
                        old=position.token_amount,
                        new=actual_balance
                    )
                position.token_amount = actual_balance
            elif actual_balance == 0:
                # Token balance is 0 - position was already sold or rugged
                logger.warning(
                    "zero_balance_detected",
                    token=position.token_mint[:8],
                    message="Token balance is 0 on-chain"
                )
                position.current_value_sol = 0
                return
            
            # Get quote for selling our tokens
            quote = await self._get_quote(
                input_mint=position.token_mint,
                output_mint=NATIVE_SOL,
                amount=position.token_amount
            )
            
            if quote:
                position.current_value_sol = int(quote.get("outAmount", 0)) / 1e9
                position.last_price_check = datetime.utcnow()
                
                # Update highest value for trailing stop
                if position.current_value_sol > position.highest_value_sol:
                    position.highest_value_sol = position.current_value_sol
                
                logger.debug(
                    "position_updated",
                    token=position.token_mint[:8],
                    value=f"{position.current_value_sol:.4f}",
                    pnl=f"{position.pnl_percent:.1f}%"
                )
                
        except Exception as e:
            logger.warning("price_update_failed", error=str(e))
    
    def _should_exit(self, position: Position) -> Optional[ExitReason]:
        """
        Determine if we should exit a position.
        Matches mock trading logic for full parity.
        """
        # Check if token is worthless (abandon, don't sell)
        if position.current_value_sol > 0 and position.current_value_sol < self.rug_abandon_sol:
            logger.info(
                "abandoning_rugged_token",
                token=position.token_mint[:8],
                value=f"{position.current_value_sol:.6f}",
                threshold=f"{self.rug_abandon_sol:.4f}",
                message="Not worth selling, freeing position slot"
            )
            return ExitReason.ABANDONED
        
        # STOP LOSS - same as mock trading (default -35%)
        if position.entry_sol > 0 and position.current_value_sol > 0:
            pnl_pct = position.pnl_percent
            if pnl_pct <= self.stop_loss_pct:
                logger.info(
                    "stop_loss_triggered",
                    token=position.token_mint[:8],
                    pnl=f"{pnl_pct:.1f}%",
                    threshold=f"{self.stop_loss_pct}%"
                )
                return ExitReason.STOP_LOSS
        
        # TAKE PROFIT - optional safety limit (default 100% = 2x)
        if self.take_profit_pct > 0 and position.pnl_percent >= self.take_profit_pct:
            logger.info(
                "take_profit_triggered",
                token=position.token_mint[:8],
                pnl=f"{position.pnl_percent:.1f}%",
                threshold=f"{self.take_profit_pct}%"
            )
            return ExitReason.TAKE_PROFIT
        
        # Time limit (only if enabled, 0 = disabled) - DISABLED BY DEFAULT
        if self.time_limit_minutes > 0 and position.age_minutes >= self.time_limit_minutes:
            logger.info(
                "time_limit_triggered",
                token=position.token_mint[:8],
                age=f"{position.age_minutes:.0f}min"
            )
            return ExitReason.TIME_LIMIT
        
        # Trailing stop (only if enabled and we've been profitable)
        if self.trailing_stop_pct > 0 and position.highest_value_sol > position.entry_sol:
            drop_from_high = ((position.current_value_sol - position.highest_value_sol) 
                             / position.highest_value_sol) * 100
            if drop_from_high <= -self.trailing_stop_pct:
                logger.info(
                    "trailing_stop_triggered",
                    token=position.token_mint[:8],
                    drop=f"{drop_from_high:.1f}%"
                )
                return ExitReason.STOP_LOSS
        
        # No exit needed - follow the trader
        return None
    
    async def _get_market_cap(self, mint: str) -> float:
        """Get market cap in USD using DexScreener API."""
        import time
        
        # Check cache (valid for 30 seconds)
        if mint in self.mcap_cache:
            cached_cap, cached_time = self.mcap_cache[mint]
            if time.time() - cached_time < 30:
                return cached_cap
        
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        market_cap = 0
                        for pair in pairs:
                            mc = pair.get("marketCap") or pair.get("fdv") or 0
                            if mc > market_cap:
                                market_cap = mc
                        
                        self.mcap_cache[mint] = (market_cap, time.time())
                        return market_cap
            
            return 0
        except Exception as e:
            logger.debug("mcap_fetch_error", mint=mint[:8], error=str(e))
            return 0
    
    async def _check_mcap_stop_loss(self, position: Position) -> Optional[ExitReason]:
        """Check if market cap dropped below stop loss threshold."""
        if self.mcap_stop_loss_usd <= 0:
            return None
        
        mcap = await self._get_market_cap(position.token_mint)
        if mcap > 0 and mcap < self.mcap_stop_loss_usd:
            logger.warning(
                "mcap_stop_loss_triggered",
                token=position.token_mint[:8],
                market_cap=f"${mcap:,.0f}",
                threshold=f"${self.mcap_stop_loss_usd:,.0f}"
            )
            return ExitReason.MCAP_STOP_LOSS
        
        return None
    
    async def _check_trader_exited(self, position: Position) -> bool:
        """Check if the trader we copied has exited this position.
        
        CRITICAL for missed sell detection - if trader sold and we missed it,
        we need to know so we can sync our position.
        
        Returns:
            True if trader has exited (we should sell), False if they still hold
        """
        try:
            trader_wallet = position.copied_from
            token_mint = position.token_mint
            
            # Use getTokenAccountsByOwner RPC call to check trader's balance
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    trader_wallet,
                    {"mint": token_mint},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if result and "value" in result:
                accounts = result["value"]
                if not accounts:
                    # No token account = trader doesn't hold this token
                    logger.debug("trader_no_token_account_real", trader=trader_wallet[:8], token=token_mint[:8])
                    return True  # Trader exited
                
                # Check if balance is > 0
                account_data = accounts[0].get("account", {}).get("data", {})
                parsed = account_data.get("parsed", {}).get("info", {})
                token_amount = parsed.get("tokenAmount", {})
                amount = int(token_amount.get("amount", 0))
                
                if amount > 0:
                    logger.debug("trader_still_holds_real", trader=trader_wallet[:8], token=token_mint[:8], amount=amount)
                    return False  # Trader still holds
                else:
                    logger.debug("trader_exited_real", trader=trader_wallet[:8], token=token_mint[:8])
                    return True  # Trader exited
            
            # If RPC call fails, assume trader still holds (safer - don't sell on error)
            return False
            
        except Exception as e:
            logger.debug("check_trader_exited_error", token=position.token_mint[:8], error=str(e))
            # On error, assume trader still holds (safer)
            return False
    
    async def _sell_position(
        self, 
        token_mint: str, 
        reason: ExitReason,
        attempt_number: int = 1
    ) -> SellResult:
        """Sell or abandon a position."""
        position = self.positions.get(token_mint)
        if not position:
            return SellResult(success=False, error="position_not_found")
        
        # CRITICAL: Check actual on-chain balance before trying to sell
        # This prevents infinite sell loops when we don't actually hold the token
        actual_balance = await self._get_actual_token_balance(token_mint)
        if actual_balance == 0:
            logger.warning(
                "skip_sell_zero_balance",
                token=token_mint[:8],
                reason=reason.value,
                message="Removing position - we don't hold any tokens"
            )
            # Remove from tracking since we don't have any tokens
            del self.positions[token_mint]
            return SellResult(success=False, error="zero_balance_on_chain")
        
        # If abandoned, just remove from tracking (don't try to sell)
        if reason == ExitReason.ABANDONED:
            logger.info(
                "position_abandoned",
                token=token_mint[:8] + "...",
                entry_sol=f"{position.entry_sol:.4f}",
                current_value=f"{position.current_value_sol:.6f}",
                message="Rugged token abandoned, slot freed for new trade"
            )
            # Track for stats
            self.abandoned_tokens[token_mint] = position.entry_sol
            self.total_abandoned += 1
            self.total_loss_sol += position.entry_sol
            # Remove from active positions
            del self.positions[token_mint]
            return SellResult(success=True, reason=ExitReason.ABANDONED, sol_received=0)
        
        logger.info(
            "selling_position",
            token=token_mint[:8] + "...",
            reason=reason.value,
            entry=f"{position.entry_sol:.4f}",
            current=f"{position.current_value_sol:.4f}"
        )
        
        try:
            # Execute sell via Jupiter
            result = await self._execute_sell(position, attempt_number=attempt_number)
            
            if result.success:
                # Update stats
                pnl_sol = result.sol_received - position.entry_sol
                if pnl_sol > 0:
                    self.total_profit_sol += pnl_sol
                else:
                    self.total_loss_sol += abs(pnl_sol)
                
                self.total_sells += 1
                
                # Remove position
                del self.positions[token_mint]
                
                logger.info(
                    "position_sold",
                    token=token_mint[:8] + "...",
                    reason=reason.value,
                    sol_received=f"{result.sol_received:.4f}",
                    pnl=f"{pnl_sol:.4f}",
                    signature=result.signature[:16] if result.signature else "none"
                )
                
                # Record exit telemetry
                try:
                    telemetry = get_telemetry()
                    if telemetry:
                        time_in_trade = int((datetime.now(timezone.utc) - position.entry_time.replace(tzinfo=timezone.utc)).total_seconds()) if position.entry_time else 0
                        pnl_pct = (pnl_sol / position.entry_sol * 100) if position.entry_sol > 0 else 0
                        
                        # Use correlation_id if available, otherwise generate new one
                        correlation_id = getattr(position, 'correlation_id', None) or str(uuid.uuid4())
                        
                        asyncio.create_task(telemetry.update_trade_exit(
                            correlation_id=correlation_id,
                            exit_reason=reason.value,
                            exit_signature=result.signature or "",
                            sol_received=Decimal(str(result.sol_received)),
                            pnl_sol=Decimal(str(pnl_sol)),
                            pnl_pct=Decimal(str(pnl_pct)),
                            exit_mcap=Decimal(str(position.current_value_sol * 1000000)) if position.current_value_sol else None,
                            time_in_trade_sec=time_in_trade,
                            cupsey_still_holding=None  # Would need to check trader holdings
                        ))
                except Exception as e:
                    logger.debug("exit_telemetry_error", error=str(e))
            else:
                logger.warning(
                    "sell_failed",
                    token=token_mint[:8],
                    error=result.error
                )
            
            result.reason = reason
            return result
            
        except Exception as e:
            return SellResult(success=False, error=str(e), reason=reason)
    
    async def _execute_sell(self, position: Position, *, attempt_number: int = 1) -> SellResult:
        """Execute a sell transaction."""
        telemetry = get_telemetry()
        correlation_id = getattr(position, "correlation_id", None) or str(uuid.uuid4())
        try:
            import base64
            from solders.transaction import VersionedTransaction
            from solders.pubkey import Pubkey
            
            # ALWAYS fetch actual on-chain token balance before selling
            actual_balance = await self._get_actual_token_balance(position.token_mint)
            if actual_balance and actual_balance > 0:
                logger.info(
                    "using_actual_balance_for_sell",
                    token=position.token_mint[:8],
                    tracked=position.token_amount,
                    actual=actual_balance
                )
                position.token_amount = actual_balance
            
            # Use Pump.fun API for pump.fun tokens (uses 100% so doesn't need exact amount)
            if position.dex == "pump.fun":
                result = await self._execute_pumpfun_sell(position, correlation_id=correlation_id, attempt_number=attempt_number)
                if result.success:
                    return result
                # Pump.fun failed - fall back to Jupiter
                logger.info(
                    "pumpfun_sell_failed_trying_jupiter",
                    token=position.token_mint[:8],
                    error=result.error
                )
            
            # Use Jupiter for other DEXes or as fallback
            # Get quote with ACTUAL balance
            quote = await self._get_quote(
                input_mint=position.token_mint,
                output_mint=NATIVE_SOL,
                amount=position.token_amount
            )
            
            if not quote:
                if telemetry:
                    asyncio.create_task(telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=position.token_mint,
                        execution_type="sell",
                        method="jupiter_quote",
                        error_code="no_quote",
                        error_message="no_quote",
                        error_category="no_route",
                        attempt_number=attempt_number,
                        requested_amount=Decimal(str(position.token_amount)),
                        slippage_bps=self.config.slippage_bps,
                        priority_fee=100000
                    ))
                return SellResult(success=False, error="no_quote")
            
            # Get swap transaction with HIGH priority fees for fast execution
            swap_data = {
                "quoteResponse": quote,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": 100000  # High priority ~0.0001 SOL for fast confirmation
            }
            
            async with self.session.post(JUPITER_SWAP_API, json=swap_data) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    if telemetry:
                        asyncio.create_task(telemetry.record_failed_execution(
                            trade_id=None,
                            correlation_id=correlation_id,
                            token_mint=position.token_mint,
                            execution_type="sell",
                            method="jupiter_swap",
                            error_code=f"http_{resp.status}",
                            error_message=error,
                            error_category="api_error",
                            attempt_number=attempt_number,
                            requested_amount=Decimal(str(position.token_amount)),
                            slippage_bps=self.config.slippage_bps,
                            priority_fee=100000
                        ))
                    return SellResult(success=False, error=f"swap_api: {error}")
                swap_response = await resp.json()
            
            swap_tx = swap_response.get("swapTransaction")
            if not swap_tx:
                if telemetry:
                    asyncio.create_task(telemetry.record_failed_execution(
                        trade_id=None,
                        correlation_id=correlation_id,
                        token_mint=position.token_mint,
                        execution_type="sell",
                        method="jupiter_swap",
                        error_code="no_swap_tx",
                        error_message=str(swap_response)[:5000],
                        error_category="api_error",
                        attempt_number=attempt_number,
                        requested_amount=Decimal(str(position.token_amount)),
                        slippage_bps=self.config.slippage_bps,
                        priority_fee=100000
                    ))
                return SellResult(success=False, error="no_swap_tx")
            
            tx_bytes = base64.b64decode(swap_tx)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            submit_at = datetime.now(timezone.utc)
            signature = await self.rpc.send_transaction(signed_tx, skip_preflight=True)
            
            sol_received = int(quote.get("outAmount", 0)) / 1e9

            if telemetry:
                asyncio.create_task(self._finalize_jupiter_sell_telemetry(
                    correlation_id=correlation_id,
                    signature=str(signature),
                    token_mint=position.token_mint,
                    quote=quote,
                    requested_in_amount=Decimal(str(position.token_amount)),
                    slippage_bps_configured=self.config.slippage_bps,
                    priority_fee_lamports=100000,
                    submit_at=submit_at,
                    attempt_number=attempt_number
                ))
            
            return SellResult(
                success=True,
                signature=signature,
                sol_received=sol_received
            )
            
        except Exception as e:
            if telemetry:
                asyncio.create_task(telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=position.token_mint,
                    execution_type="sell",
                    method="sell_exception",
                    error_code="exception",
                    error_message=str(e),
                    error_category="exception",
                    attempt_number=attempt_number,
                    requested_amount=Decimal(str(position.token_amount)) if position.token_amount else None,
                    slippage_bps=self.config.slippage_bps,
                    priority_fee=100000
                ))
            return SellResult(success=False, error=str(e))

    async def _finalize_jupiter_sell_telemetry(
        self,
        *,
        correlation_id: str,
        signature: str,
        token_mint: str,
        quote: Dict,
        requested_in_amount: Decimal,
        slippage_bps_configured: int,
        priority_fee_lamports: int,
        submit_at: datetime,
        attempt_number: int
    ) -> None:
        telemetry = get_telemetry()
        if not telemetry:
            return

        confirmed = False
        try:
            confirmed = await self.rpc.confirm_transaction(signature)
        except Exception:
            confirmed = False

        confirm_at = datetime.now(timezone.utc)

        tx_fee_lamports = None
        compute_units_used = None
        slot = None
        block_time = None
        program_ids = None
        try:
            tx_info = await self.rpc.get_transaction(signature)
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

        if not confirmed:
            asyncio.create_task(telemetry.record_failed_execution(
                trade_id=None,
                correlation_id=correlation_id,
                token_mint=token_mint,
                execution_type="sell",
                method="jupiter_confirm",
                error_code="tx_not_confirmed",
                error_message=signature[:64],
                error_category="tx_error",
                attempt_number=attempt_number,
                requested_amount=requested_in_amount,
                slippage_bps=slippage_bps_configured,
                priority_fee=priority_fee_lamports
            ))

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
            execution_type="sell",
            signature=signature,
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
            requested_in_amount=requested_in_amount,
            requested_out_min=Decimal(str(quote.get("otherAmountThreshold"))) if isinstance(quote, dict) and quote.get("otherAmountThreshold") is not None else None,
            slippage_bps_configured=slippage_bps_configured,
            priority_fee_lamports=priority_fee_lamports,
            compute_units_used=compute_units_used,
            tx_fee_lamports=tx_fee_lamports,
            total_cost_sol=Decimal(str((tx_fee_lamports or 0) / 1e9)) if tx_fee_lamports is not None else None,
            submit_at=submit_at,
            confirm_at=confirm_at,
            send_to_confirm_ms=int((confirm_at - submit_at).total_seconds() * 1000),
            attempt_number=attempt_number,
            total_retries=max(0, attempt_number - 1),
            final_status="success" if confirmed else "failed"
        )

        await telemetry.record_execution_details(
            correlation_id=correlation_id,
            exec_detail=exec_detail
        )
    
    async def _execute_pumpfun_sell(self, position: Position, *, correlation_id: Optional[str] = None, attempt_number: int = 1) -> SellResult:
        """Execute a sell via PumpPortal API - tries multiple pools."""
        telemetry = get_telemetry()
        try:
            import base64
            from solders.transaction import VersionedTransaction
            
            # Request transaction from PumpPortal - sell 100% of holdings
            # Use high slippage for pump.fun (tokens move fast) - minimum 30%
            pumpfun_slippage = max(self.config.slippage_bps / 100, 30)
            
            # Try pools in order: pump (bonding curve), pump-amm (graduated), raydium
            pools_to_try = ["auto", "pump", "pump-amm", "raydium", "raydium-cpmm", "launchlab"]
            last_error = None
            
            for pool in pools_to_try:
                payload = {
                    "publicKey": str(self.wallet.pubkey()),
                    "action": "sell",
                    "mint": position.token_mint,
                    "denominatedInSol": "false",
                    "amount": "100%",  # Sell all tokens
                    "slippage": pumpfun_slippage,
                    "priorityFee": 0.005,  # Higher priority for faster execution
                    "pool": pool
                }
                
                logger.info(
                    "pumpfun_sell_request",
                    token=position.token_mint[:8],
                    pool=pool
                )
                
                async with self.session.post(
                    PUMPFUN_API,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        last_error = f"{pool}: {error_text}"
                        logger.debug("pumpfun_sell_pool_failed", pool=pool, status=resp.status, response=error_text[:100], token=position.token_mint[:8])
                        if telemetry and correlation_id:
                            asyncio.create_task(telemetry.record_failed_execution(
                                trade_id=None,
                                correlation_id=correlation_id,
                                token_mint=position.token_mint,
                                execution_type="sell",
                                method="pumpfun_sell",
                                error_code=f"{pool}_http_{resp.status}",
                                error_message=error_text,
                                error_category="api_error",
                                attempt_number=attempt_number,
                                slippage_bps=int(pumpfun_slippage * 100),
                                priority_fee=int(0.005 * 1e9)
                            ))
                        continue  # Try next pool
                    
                    tx_bytes = await resp.read()
                
                tx = VersionedTransaction.from_bytes(tx_bytes)
                signed_tx = VersionedTransaction(tx.message, [self.wallet])
                
                submit_at = datetime.now(timezone.utc)
                signature = await self.rpc.send_transaction(signed_tx, skip_preflight=True)
                
                logger.info(
                    "pumpfun_sell_success",
                    token=position.token_mint[:8],
                    pool=pool,
                    signature=str(signature)[:16] if signature else None
                )

                if telemetry and correlation_id:
                    asyncio.create_task(self._finalize_pumpfun_sell_telemetry(
                        correlation_id=correlation_id,
                        signature=str(signature),
                        token_mint=position.token_mint,
                        pool=pool,
                        slippage_bps_configured=int(pumpfun_slippage * 100),
                        priority_fee_lamports=int(0.005 * 1e9),
                        submit_at=submit_at,
                        attempt_number=attempt_number
                    ))
                
                return SellResult(
                    success=True,
                    signature=signature,
                    sol_received=position.current_value_sol  # Estimate
                )
            
            # All pools failed
            logger.warning("pumpfun_sell_all_pools_failed", token=position.token_mint[:8], last_error=last_error)
            if telemetry and correlation_id:
                asyncio.create_task(telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=position.token_mint,
                    execution_type="sell",
                    method="pumpfun_sell",
                    error_code="all_pools_failed",
                    error_message=str(last_error)[:5000] if last_error else "all_pools_failed",
                    error_category="no_route",
                    attempt_number=attempt_number,
                    slippage_bps=int(pumpfun_slippage * 100),
                    priority_fee=int(0.005 * 1e9)
                ))
            return SellResult(success=False, error=f"pumpfun_api: {last_error}")
            
        except Exception as e:
            logger.error("pumpfun_sell_error", error=str(e))
            if telemetry and correlation_id:
                asyncio.create_task(telemetry.record_failed_execution(
                    trade_id=None,
                    correlation_id=correlation_id,
                    token_mint=position.token_mint,
                    execution_type="sell",
                    method="pumpfun_sell",
                    error_code="exception",
                    error_message=str(e),
                    error_category="exception",
                    attempt_number=attempt_number,
                    slippage_bps=int(pumpfun_slippage * 100) if 'pumpfun_slippage' in locals() else None,
                    priority_fee=int(0.005 * 1e9)
                ))
            return SellResult(success=False, error=f"pumpfun_error: {str(e)}")

    async def _finalize_pumpfun_sell_telemetry(
        self,
        *,
        correlation_id: str,
        signature: str,
        token_mint: str,
        pool: str,
        slippage_bps_configured: int,
        priority_fee_lamports: int,
        submit_at: datetime,
        attempt_number: int
    ) -> None:
        telemetry = get_telemetry()
        if not telemetry:
            return

        confirmed = False
        try:
            confirmed = await self.rpc.confirm_transaction(signature)
        except Exception:
            confirmed = False

        confirm_at = datetime.now(timezone.utc)

        tx_fee_lamports = None
        compute_units_used = None
        slot = None
        block_time = None
        program_ids = None
        try:
            tx_info = await self.rpc.get_transaction(signature)
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

        if not confirmed:
            asyncio.create_task(telemetry.record_failed_execution(
                trade_id=None,
                correlation_id=correlation_id,
                token_mint=token_mint,
                execution_type="sell",
                method="pumpfun_confirm",
                error_code="tx_not_confirmed",
                error_message=signature[:64],
                error_category="tx_error",
                attempt_number=attempt_number,
                slippage_bps=slippage_bps_configured,
                priority_fee=priority_fee_lamports
            ))

        exec_detail = ExecutionDetails(
            executor="bot",
            execution_type="sell",
            signature=signature,
            slot=slot,
            block_time=block_time,
            program_ids=program_ids,
            dex_used="pump.fun",
            pumpfun_pool_type=pool,
            slippage_bps_configured=slippage_bps_configured,
            priority_fee_lamports=priority_fee_lamports,
            compute_units_used=compute_units_used,
            tx_fee_lamports=tx_fee_lamports,
            total_cost_sol=Decimal(str((tx_fee_lamports or 0) / 1e9)) if tx_fee_lamports is not None else None,
            submit_at=submit_at,
            confirm_at=confirm_at,
            send_to_confirm_ms=int((confirm_at - submit_at).total_seconds() * 1000),
            attempt_number=attempt_number,
            total_retries=max(0, attempt_number - 1),
            final_status="success" if confirmed else "failed"
        )

        await telemetry.record_execution_details(
            correlation_id=correlation_id,
            exec_detail=exec_detail
        )
    
    async def _get_actual_token_balance(self, token_mint: str) -> Optional[int]:
        """Fetch actual on-chain token balance for our wallet."""
        try:
            from solders.pubkey import Pubkey
            
            wallet_pubkey = self.wallet.pubkey()
            token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            mint_pubkey = Pubkey.from_string(token_mint)
            
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    str(wallet_pubkey),
                    {"mint": str(mint_pubkey)},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if not result or "value" not in result or not result["value"]:
                return None
            
            # Get balance from first token account
            for account in result["value"]:
                try:
                    parsed = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    amount = int(parsed.get("tokenAmount", {}).get("amount", 0))
                    if amount > 0:
                        return amount
                except Exception:
                    continue
            
            return None
            
        except Exception as e:
            logger.debug("get_actual_balance_error", token=token_mint[:8], error=str(e))
            return None
    
    async def _get_quote(
        self, 
        input_mint: str, 
        output_mint: str, 
        amount: int
    ) -> Optional[Dict]:
        """Get a Jupiter quote."""
        try:
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(self.config.slippage_bps)
            }
            
            async with self.session.get(JUPITER_QUOTE_API, params=params, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
                
        except Exception:
            return None
    
    def get_positions_summary(self) -> Dict:
        """Get a summary of all positions."""
        return {
            "open": len(self.positions),
            "max": self.max_positions,
            "total_invested": sum(p.entry_sol for p in self.positions.values()),
            "total_current": sum(p.current_value_sol for p in self.positions.values()),
            "positions": [
                {
                    "token": p.token_mint[:8],
                    "entry": f"{p.entry_sol:.4f}",
                    "current": f"{p.current_value_sol:.4f}",
                    "pnl": f"{p.pnl_percent:.1f}%",
                    "age": f"{p.age_minutes:.0f}min"
                }
                for p in self.positions.values()
            ]
        }
