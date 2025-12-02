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
from .trade_logger import trade_logger

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
        self.copy_proportional = config.copy_proportional
        self.exit_fee_reserve = config.exit_fee_reserve
        self.max_positions = config.max_positions
        self.min_market_cap_usd = config.min_market_cap_usd
        self.min_token_age_minutes = config.min_token_age_minutes
        
        # Cache for token info (to avoid repeated API calls)
        self.token_info_cache: Dict[str, tuple[float, float, float]] = {}  # mint -> (market_cap, age_minutes, cache_time)
        
        # Track trader wallet balances for proportional sizing
        self.trader_balances: Dict[str, float] = {}
        
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
                    return CopyTradeResult(success=False, error="no_tokens_to_sell", original_swap=swap)
                
                logger.info(
                    "fast_sell",
                    token=swap.token_mint[:8],
                    our_balance=token_balance,
                    their_sol=f"{swap.sol_value:.4f}"
                )
                
                # AGGRESSIVE RETRY LOOP - sells MUST succeed
                max_retries = 5
                result = None
                for attempt in range(max_retries):
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
                    
                    # Failed - log and retry immediately
                    logger.warning(
                        "sell_retry",
                        token=swap.token_mint[:8],
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        error=result.error if result else "unknown"
                    )
                    await asyncio.sleep(0.15)  # 150ms between retries
                
                # All retries failed - add to retry queue for background retries
                logger.error("sell_failed_queuing_retry", token=swap.token_mint[:8])
                if self.position_manager:
                    self.position_manager.queue_failed_sell(swap.token_mint, token_balance)
                
                return result or CopyTradeResult(success=False, error="sell_failed_all_retries", original_swap=swap)
            
            # BUYS: Check market cap and token age filters
            if self.min_market_cap_usd > 0 or self.min_token_age_minutes > 0:
                market_cap, age_minutes = await self._get_token_info(swap.token_mint)
                
                # If not on DexScreener, skip (likely very new/risky token)
                if market_cap == 0 and age_minutes == 0:
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
                
                # Check token age
                if self.min_token_age_minutes > 0 and age_minutes < self.min_token_age_minutes:
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
                if self.min_market_cap_usd > 0 and market_cap < self.min_market_cap_usd:
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
                
                logger.info(
                    "token_filters_passed",
                    token=swap.token_mint[:8],
                    market_cap=f"${market_cap:,.0f}",
                    age=f"{age_minutes:.1f}m"
                )
            
            # BUYS: Full calculation path
            balance = await self.rpc.get_balance(self.wallet.pubkey())
            balance_sol = balance / 1e9
            
            # Calculate fee reserve needed for existing + new positions
            current_positions = len(self.position_manager.positions) if self.position_manager else 0
            total_fee_reserve = self.fee_reserve + (self.exit_fee_reserve * (current_positions + 1))
            
            # Available balance after fee reserve
            available_sol = max(0, balance_sol - total_fee_reserve)
            
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
            
            # Buy: SOL -> Token (sells are handled by fast path above)
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
                            entry_sol=trade_sol,
                            token_amount=estimated_tokens,
                            entry_signature=result.signature or "",
                            copied_from=swap.wallet,
                            token_symbol=swap.token_symbol
                        )
                    
                    # Log the trade for analysis
                    trade_logger.log_buy(
                        token_mint=swap.token_mint,
                        token_symbol=swap.token_symbol,
                        our_sol=trade_sol,
                        our_tokens=estimated_tokens if self.position_manager else 0,
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
    
    async def _get_token_balance(self, mint: str) -> int:
        """Get token balance for our wallet by finding the associated token account."""
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
                if accounts and len(accounts) > 0:
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
    
    async def _get_token_info(self, mint: str) -> tuple[float, float]:
        """Get market cap and token age using DexScreener API.
        
        Returns:
            tuple: (market_cap_usd, age_minutes)
        """
        import time
        
        # Check cache (valid for 60 seconds)
        if mint in self.token_info_cache:
            cached_cap, cached_age, cached_time = self.token_info_cache[mint]
            if time.time() - cached_time < 60:
                # Adjust age for time passed since cache
                adjusted_age = cached_age + (time.time() - cached_time) / 60
                return cached_cap, adjusted_age
        
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        # Get the highest liquidity pair's market cap and age
                        market_cap = 0
                        oldest_age = 0
                        
                        for pair in pairs:
                            mc = pair.get("marketCap") or pair.get("fdv") or 0
                            if mc > market_cap:
                                market_cap = mc
                            
                            # Get pair creation time
                            created_at = pair.get("pairCreatedAt")
                            if created_at:
                                age_ms = time.time() * 1000 - created_at
                                age_minutes = age_ms / 60000
                                if age_minutes > oldest_age:
                                    oldest_age = age_minutes
                        
                        self.token_info_cache[mint] = (market_cap, oldest_age, time.time())
                        return market_cap, oldest_age
            
            return 0, 0
        except Exception as e:
            logger.debug("token_info_fetch_error", mint=mint[:8], error=str(e))
            return 0, 0
    
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
