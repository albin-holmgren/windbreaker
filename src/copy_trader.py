"""
Copy Trader - Main module for copy trading functionality.
Monitors wallets, detects trades, and executes copies.
"""

import asyncio
import aiohttp
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import structlog

from .wallet_monitor import WalletMonitor, WalletTransaction
from .tx_parser import TransactionParser, ParsedSwap, SwapType
from .config import Config
from .position_manager import PositionManager

logger = structlog.get_logger(__name__)

# Jupiter API for swaps
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://lite-api.jup.ag/swap/v1/swap"

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
        
        # Position manager for auto-sell
        self.position_manager: Optional[PositionManager] = None
        
    async def start(self) -> None:
        """Start the copy trader."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        # Create position manager
        self.position_manager = PositionManager(
            config=self.config,
            wallet_keypair=self.wallet,
            rpc_client=self.rpc,
            max_positions=self.config.max_positions,
            take_profit_pct=self.config.take_profit_pct,
            stop_loss_pct=self.config.stop_loss_pct,
            time_limit_minutes=self.config.time_limit_minutes,
            trailing_stop_pct=self.config.trailing_stop_pct,
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
        
        # Start monitoring
        await self.monitor.start()
    
    async def stop(self) -> None:
        """Stop the copy trader."""
        self.running = False
        
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
        
        # Decide whether to copy
        should_copy, reason = self._should_copy(swap)
        
        if not should_copy:
            self.stats.total_skipped += 1
            logger.info("skip_copy", reason=reason)
            return
        
        # Execute the copy trade
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
        
        # For buys, check position limits
        if swap.is_buy:
            # Check if we can open more positions
            if self.position_manager and not self.position_manager.can_open_position():
                return False, f"max_positions_reached ({self.config.max_positions})"
            
            # Check if we already have this token
            if self.position_manager and self.position_manager.has_position(swap.token_mint):
                return False, "already_holding_token"
        
        # For sells, only copy if we hold the token (handled by position manager)
        if swap.is_sell and not self.copy_sells:
            return False, "sell_disabled"
        
        # Check minimum SOL value
        if swap.sol_value < self.min_sol_per_trade:
            return False, f"below_min_sol ({swap.sol_value:.4f} < {self.min_sol_per_trade})"
        
        # Don't copy the same token too frequently
        if swap.token_mint in self.recent_copies:
            return False, "recently_copied"
        
        return True, "ok"
    
    async def _execute_copy(self, swap: ParsedSwap) -> CopyTradeResult:
        """Execute a copy of the detected swap."""
        try:
            # Get our wallet balance
            balance = await self.rpc.get_balance(self.wallet.pubkey())
            balance_sol = balance / 1e9
            
            # Calculate how much to trade
            available_sol = max(0, balance_sol - self.fee_reserve)
            trade_sol = min(
                available_sol * self.copy_percentage,
                self.max_sol_per_trade,
                swap.sol_value * 2  # Don't trade more than 2x what they did
            )
            
            if trade_sol < self.min_sol_per_trade:
                return CopyTradeResult(
                    success=False,
                    error=f"insufficient_balance ({trade_sol:.4f} SOL)",
                    original_swap=swap
                )
            
            trade_lamports = int(trade_sol * 1e9)
            
            logger.info(
                "executing_copy",
                type=swap.swap_type.value,
                token=swap.token_mint[:8] + "...",
                our_sol=f"{trade_sol:.4f}",
                their_sol=f"{swap.sol_value:.4f}"
            )
            
            if swap.is_buy:
                # Buy: SOL -> Token
                result = await self._execute_swap(
                    input_mint=NATIVE_SOL,
                    output_mint=swap.token_mint,
                    amount=trade_lamports
                )
            else:
                # Sell: Token -> SOL
                # Get our token balance
                token_balance = await self._get_token_balance(swap.token_mint)
                if token_balance == 0:
                    return CopyTradeResult(
                        success=False,
                        error="no_tokens_to_sell",
                        original_swap=swap
                    )
                
                result = await self._execute_swap(
                    input_mint=swap.token_mint,
                    output_mint=NATIVE_SOL,
                    amount=token_balance
                )
            
            if result.success:
                # Track this token to avoid rapid re-copying
                self.recent_copies.add(swap.token_mint)
                # Remove from recent after 60 seconds
                asyncio.create_task(self._clear_recent_copy(swap.token_mint, 60))
                
                if swap.is_buy:
                    self.stats.total_sol_spent += trade_sol
                    
                    # Register position for auto-sell management
                    if self.position_manager:
                        # Estimate tokens received from the swap
                        # In reality, we'd parse this from the transaction result
                        estimated_tokens = int(trade_lamports * 1000)  # Placeholder
                        self.position_manager.add_position(
                            token_mint=swap.token_mint,
                            entry_sol=trade_sol,
                            token_amount=estimated_tokens,
                            entry_signature=result.signature or "",
                            copied_from=swap.wallet,
                            token_symbol=swap.token_symbol
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
    
    async def _get_token_balance(self, mint: str) -> int:
        """Get token balance for our wallet."""
        try:
            # This would need proper implementation with token accounts
            # For now, return 0 (would need to query token accounts)
            return 0
        except Exception:
            return 0
    
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
