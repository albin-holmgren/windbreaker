"""
Position Manager - Tracks open positions and handles auto-sell logic.
Implements take-profit, stop-loss, and time-based exits.
"""

import asyncio
import aiohttp
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import structlog

logger = structlog.get_logger(__name__)

# Jupiter API
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://lite-api.jup.ag/swap/v1/swap"
NATIVE_SOL = "So11111111111111111111111111111111111111112"


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_LIMIT = "time_limit"
    MANUAL = "manual"
    RUG_DETECTED = "rug_detected"
    ABANDONED = "abandoned"  # Token too worthless to sell, just free the slot
    COPIED_SELL = "copied_sell"  # Trader we copied sold


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
        take_profit_pct: float = 100.0,     # Safety: sell at 2x (optional)
        stop_loss_pct: float = -95.0,       # Abandon at 95% loss
        time_limit_minutes: float = 0,      # 0 = disabled (follow trader)
        trailing_stop_pct: float = 0,       # 0 = disabled
        rug_abandon_sol: float = 0.005,     # Abandon if worth < 0.005 SOL
        check_interval_sec: float = 60.0,   # Check prices every 60s
    ):
        self.config = config
        self.wallet = wallet_keypair
        self.rpc = rpc_client
        
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
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        
        # Stats
        self.total_sells = 0
        self.total_abandoned = 0
        self.total_profit_sol = 0.0
        self.total_loss_sol = 0.0
    
    async def start(self) -> None:
        """Start the position manager."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        logger.info(
            "position_manager_started",
            max_positions=self.max_positions,
            take_profit=f"{self.take_profit_pct}%",
            stop_loss=f"{self.stop_loss_pct}%",
            time_limit=f"{self.time_limit_minutes}min"
        )
        
        # Start monitoring loop
        asyncio.create_task(self._monitor_loop())
    
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
        token_symbol: Optional[str] = None
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
    
    async def trigger_sell(self, token_mint: str, reason: ExitReason = ExitReason.COPIED_SELL) -> SellResult:
        """
        Trigger a sell for a specific token.
        Called when the copied trader sells.
        """
        if token_mint not in self.positions:
            return SellResult(success=False, error="no_position_for_token")
        
        logger.info(
            "trader_sold_copying",
            token=token_mint[:8] + "...",
            reason=reason.value
        )
        
        return await self._sell_position(token_mint, reason)
    
    def get_position(self, token_mint: str) -> Optional[Position]:
        """Get a position by token mint."""
        return self.positions.get(token_mint)
    
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
                
                # Check exit conditions
                exit_reason = self._should_exit(position)
                
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
        """Determine if we should exit a position."""
        pnl = position.pnl_percent
        
        # Check if token is worthless (abandon, don't sell)
        if position.current_value_sol < self.rug_abandon_sol:
            logger.info(
                "abandoning_rugged_token",
                token=position.token_mint[:8],
                value=f"{position.current_value_sol:.6f}",
                threshold=f"{self.rug_abandon_sol:.4f}",
                message="Not worth selling, freeing position slot"
            )
            return ExitReason.ABANDONED
        
        # Take profit (safety limit, e.g., at 2x)
        if self.take_profit_pct > 0 and pnl >= self.take_profit_pct:
            logger.info(
                "take_profit_triggered",
                token=position.token_mint[:8],
                pnl=f"{pnl:.1f}%"
            )
            return ExitReason.TAKE_PROFIT
        
        # Time limit (only if enabled, 0 = disabled)
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
    
    async def _sell_position(
        self, 
        token_mint: str, 
        reason: ExitReason
    ) -> SellResult:
        """Sell or abandon a position."""
        position = self.positions.get(token_mint)
        if not position:
            return SellResult(success=False, error="position_not_found")
        
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
            result = await self._execute_sell(position)
            
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
    
    async def _execute_sell(self, position: Position) -> SellResult:
        """Execute a sell transaction."""
        try:
            # Get quote
            quote = await self._get_quote(
                input_mint=position.token_mint,
                output_mint=NATIVE_SOL,
                amount=position.token_amount
            )
            
            if not quote:
                return SellResult(success=False, error="no_quote")
            
            # Get swap transaction
            swap_data = {
                "quoteResponse": quote,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            async with self.session.post(JUPITER_SWAP_API, json=swap_data) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    return SellResult(success=False, error=f"swap_api: {error}")
                swap_response = await resp.json()
            
            # Sign and send
            import base64
            from solders.transaction import VersionedTransaction
            
            swap_tx = swap_response.get("swapTransaction")
            if not swap_tx:
                return SellResult(success=False, error="no_swap_tx")
            
            tx_bytes = base64.b64decode(swap_tx)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            signature = await self.rpc.send_transaction(signed_tx)
            
            sol_received = int(quote.get("outAmount", 0)) / 1e9
            
            return SellResult(
                success=True,
                signature=signature,
                sol_received=sol_received
            )
            
        except Exception as e:
            return SellResult(success=False, error=str(e))
    
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
            
            async with self.session.get(JUPITER_QUOTE_API, params=params) as resp:
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
