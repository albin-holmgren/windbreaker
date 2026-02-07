"""
Tiered Position Manager - Manages positions with tiered profit-taking.
Sells 50% at 2x, 20% at 5x, trails remaining 30% with 45% stop loss.
"""

import asyncio
import aiohttp
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import structlog

logger = structlog.get_logger(__name__)

# Constants
SOL_DECIMALS = 1_000_000_000
NATIVE_SOL = "So11111111111111111111111111111111111111112"
PUMPFUN_API = "https://pumpportal.fun/api/trade-local"


@dataclass
class TierState:
    """State of a tier in the tiered selling strategy."""
    target_multiplier: float  # e.g., 2.0 for 2x
    sell_percent: float     # e.g., 0.50 for 50%
    executed: bool = False
    executed_at: Optional[datetime] = None
    executed_price: Optional[float] = None
    signature: Optional[str] = None


@dataclass
class TieredPosition:
    """A position with tiered sell levels."""
    token_address: str
    entry_price: float  # In SOL terms (e.g., tokens per SOL)
    entry_sol: float    # Amount of SOL invested
    entry_time: datetime
    total_tokens: float
    
    # Tiers
    tier1: TierState = field(default_factory=lambda: TierState(2.0, 0.50))
    tier2: TierState = field(default_factory=lambda: TierState(5.0, 0.20))
    tier3: TierState = field(default_factory=lambda: TierState(float('inf'), 0.30))
    
    # Trailing stop for tier 3
    highest_price_seen: float = 0.0
    trailing_stop_active: bool = False
    trailing_stop_price: float = 0.0
    
    # Status
    fully_exited: bool = False
    total_sold_percent: float = 0.0
    pnl_percent: float = 0.0
    
    def __post_init__(self):
        if self.highest_price_seen == 0.0:
            self.highest_price_seen = self.entry_price


class TieredPositionManager:
    """
    Position manager with tiered selling strategy:
    - Tier 1: Sell 50% when price hits 2x
    - Tier 2: Sell 20% when price hits 5x (40% of remaining after tier 1)
    - Tier 3: Trail remaining 30% with 45% stop loss
    """
    
    def __init__(
        self,
        rpc_client,
        wallet_keypair,
        check_interval_sec: float = 5.0,
        tier3_trailing_stop_percent: float = 0.45,
        tier3_activation_multiplier: float = 2.0,
    ):
        self.rpc = rpc_client
        self.wallet = wallet_keypair
        
        self.check_interval = check_interval_sec
        self.tier3_trailing_stop = tier3_trailing_stop_percent
        self.tier3_activation = tier3_activation_multiplier
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Positions: token_address -> TieredPosition
        self.positions: Dict[str, TieredPosition] = {}
        
        # Price cache to reduce RPC calls
        self._price_cache: Dict[str, tuple[float, datetime]] = {}
        self._cache_ttl = timedelta(seconds=10)
        
        # Stats
        self.tier1_executed = 0
        self.tier2_executed = 0
        self.tier3_executed = 0
        self.checks_performed = 0
    
    async def start(self) -> None:
        """Start the position manager."""
        self.session = aiohttp.ClientSession()
        logger.info("tiered_position_manager_started",
                   check_interval=self.check_interval,
                   tier3_trailing_stop=f"{self.tier3_trailing_stop*100:.0f}%",
                   tier3_activation=f"{self.tier3_activation}x")
        
        # Start monitoring loop
        asyncio.create_task(self._monitoring_loop())
    
    async def stop(self) -> None:
        """Stop the position manager."""
        if self.session:
            await self.session.close()
    
    def add_position(self, token_address: str, entry_sol: float, total_tokens: float) -> TieredPosition:
        """Add a new position with default tiers."""
        # Default entry price: 1 (will be updated with actual price)
        position = TieredPosition(
            token_address=token_address,
            entry_price=1.0,
            entry_sol=entry_sol,
            entry_time=datetime.utcnow(),
            total_tokens=total_tokens,
        )
        
        self.positions[token_address] = position
        logger.info("position_added",
                   token=token_address[:8],
                   entry_sol=entry_sol,
                   total_tokens=total_tokens)
        
        return position
    
    async def _monitoring_loop(self) -> None:
        """Main loop to check positions and execute tiered sells."""
        while True:
            try:
                await asyncio.sleep(self.check_interval)
                await self._check_all_positions()
            except Exception as e:
                logger.error("monitoring_loop_error", error=str(e))
    
    async def _check_all_positions(self) -> None:
        """Check all positions and execute any triggered tiers."""
        if not self.positions:
            return
        
        self.checks_performed += 1
        
        for token_address, position in list(self.positions.items()):
            if position.fully_exited:
                continue
            
            try:
                # Get current price
                current_price = await self._get_token_price(token_address)
                if current_price is None:
                    continue
                
                # Set entry price on first successful price fetch
                if position.entry_price == 1.0:
                    position.entry_price = current_price
                    position.highest_price_seen = current_price
                    logger.info("entry_price_set",
                               token=token_address[:8],
                               entry_price=current_price)
                    continue  # Skip first check to avoid immediate triggers
                
                # Update highest price for trailing stop
                if current_price > position.highest_price_seen:
                    position.highest_price_seen = current_price
                
                # Calculate P&L
                price_ratio = current_price / position.entry_price if position.entry_price > 0 else 1.0
                position.pnl_percent = (price_ratio - 1.0) * 100
                
                # Check tiers
                await self._check_tier1(position, current_price)
                await self._check_tier2(position, current_price)
                await self._check_tier3(position, current_price)
                
            except Exception as e:
                logger.error("position_check_error",
                           token=token_address[:8],
                           error=str(e))
    
    async def _check_tier1(self, position: TieredPosition, current_price: float) -> None:
        """Check and execute Tier 1 (50% at 2x)."""
        if position.tier1.executed:
            return
        
        price_ratio = current_price / position.entry_price if position.entry_price > 0 else 0
        
        if price_ratio >= position.tier1.target_multiplier:
            logger.info("tier1_triggered",
                       token=position.token_address[:8],
                       price_ratio=price_ratio,
                       target=position.tier1.target_multiplier)
            
            # Sell 50% of total position
            sell_percent = 50  # 50% of total
            success = await self._execute_sell(position, sell_percent, "tier1")
            
            if success:
                position.tier1.executed = True
                position.tier1.executed_at = datetime.utcnow()
                position.tier1.executed_price = current_price
                position.total_sold_percent += 50
                self.tier1_executed += 1
                
                logger.info("tier1_executed",
                           token=position.token_address[:8],
                           sold_percent=50,
                   remaining=50)
    
    async def _check_tier2(self, position: TieredPosition, current_price: float) -> None:
        """Check and execute Tier 2 (20% at 5x)."""
        if position.tier2.executed or not position.tier1.executed:
            return
        
        price_ratio = current_price / position.entry_price if position.entry_price > 0 else 0
        
        if price_ratio >= position.tier2.target_multiplier:
            logger.info("tier2_triggered",
                       token=position.token_address[:8],
                       price_ratio=price_ratio,
                       target=position.tier2.target_multiplier)
            
            # Sell 20% of original position (40% of remaining 50%)
            sell_percent = 20
            success = await self._execute_sell(position, sell_percent, "tier2")
            
            if success:
                position.tier2.executed = True
                position.tier2.executed_at = datetime.utcnow()
                position.tier2.executed_price = current_price
                position.total_sold_percent += 20
                self.tier2_executed += 1
                
                logger.info("tier2_executed",
                           token=position.token_address[:8],
                           sold_percent=20,
                           remaining=30)
    
    async def _check_tier3(self, position: TieredPosition, current_price: float) -> None:
        """Check and execute Tier 3 (trailing stop on remaining 30%)."""
        if position.tier3.executed or not position.tier2.executed:
            return
        
        price_ratio = current_price / position.entry_price if position.entry_price > 0 else 0
        
        # Activate trailing stop after hitting tier 3 activation (2x)
        if not position.trailing_stop_active and price_ratio >= self.tier3_activation:
            position.trailing_stop_active = True
            position.trailing_stop_price = position.highest_price_seen * (1 - self.tier3_trailing_stop)
            logger.info("trailing_stop_activated",
                       token=position.token_address[:8],
                       activation_price=price_ratio,
                       stop_price=position.trailing_stop_price)
        
        if position.trailing_stop_active:
            # Update trailing stop price if we hit new highs
            new_stop = position.highest_price_seen * (1 - self.tier3_trailing_stop)
            if new_stop > position.trailing_stop_price:
                position.trailing_stop_price = new_stop
                logger.debug("trailing_stop_moved_up",
                           token=position.token_address[:8],
                           new_stop=new_stop)
            
            # Check if price hit trailing stop
            if current_price <= position.trailing_stop_price:
                logger.info("tier3_trailing_stop_triggered",
                           token=position.token_address[:8],
                           current_price=current_price,
                           stop_price=position.trailing_stop_price)
                
                # Sell remaining 30%
                sell_percent = 30
                success = await self._execute_sell(position, sell_percent, "tier3")
                
                if success:
                    position.tier3.executed = True
                    position.tier3.executed_at = datetime.utcnow()
                    position.tier3.executed_price = current_price
                    position.total_sold_percent += 30
                    position.fully_exited = True
                    self.tier3_executed += 1
                    
                    logger.info("tier3_executed_full_exit",
                               token=position.token_address[:8],
                               total_pnl=f"{position.pnl_percent:.1f}%")
    
    async def _execute_sell(self, position: TieredPosition, sell_percent: int, tier: str) -> bool:
        """Execute a sell via pump.fun with Jupiter fallback."""
        from solders.transaction import VersionedTransaction
        
        token_mint = position.token_address
        
        # Step 1: Try pump.fun sell (JSON then form-data)
        tx_bytes = await self._try_pumpfun_sell(token_mint, sell_percent, tier)
        
        # Step 2: If pump.fun failed, try Jupiter
        if tx_bytes is None:
            logger.info(f"{tier}_trying_jupiter_fallback", token=token_mint[:8])
            tx_bytes = await self._try_jupiter_sell(token_mint, sell_percent, tier)
        
        if tx_bytes is None:
            logger.error(f"{tier}_all_sell_methods_failed", token=token_mint[:8])
            return False
        
        # Sign and send
        try:
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            signature = await self.rpc.send_transaction(
                signed_tx,
                skip_preflight=True
            )
            
            # Save signature
            if tier == "tier1":
                position.tier1.signature = str(signature)
            elif tier == "tier2":
                position.tier2.signature = str(signature)
            else:
                position.tier3.signature = str(signature)
            
            logger.info(f"{tier}_sell_submitted",
                       token=token_mint[:8],
                       sell_percent=sell_percent,
                       signature=str(signature)[:16])
            
            return True
            
        except Exception as e:
            logger.error(f"{tier}_sell_send_error",
                        token=token_mint[:8],
                        error=str(e))
            return False
    
    async def _try_pumpfun_sell(self, token_mint: str, sell_percent: int, tier: str) -> Optional[bytes]:
        """Try pump.fun sell with JSON then form-data fallback."""
        try:
            payload = {
                "publicKey": str(self.wallet.pubkey()),
                "action": "sell",
                "mint": token_mint,
                "denominatedInSol": "false",
                "amount": f"{sell_percent}%",
                "slippage": 20,
                "priorityFee": 0.001,
                "pool": "auto"
            }
            
            # Try JSON first
            async with self.session.post(
                PUMPFUN_API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                error_text = await resp.text()
                logger.debug(f"{tier}_pumpfun_json_failed",
                           token=token_mint[:8],
                           status=resp.status,
                           error=error_text[:100])
            
            # Retry with form-data on 400
            async with self.session.post(
                PUMPFUN_API,
                data=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"{tier}_pumpfun_form_sell_success", token=token_mint[:8])
                    return await resp.read()
                error_text = await resp.text()
                logger.debug(f"{tier}_pumpfun_form_failed",
                           token=token_mint[:8],
                           status=resp.status,
                           error=error_text[:100])
            
            return None
        except Exception as e:
            logger.debug(f"{tier}_pumpfun_sell_exception", token=token_mint[:8], error=str(e))
            return None
    
    async def _try_jupiter_sell(self, token_mint: str, sell_percent: int, tier: str) -> Optional[bytes]:
        """Try selling via Jupiter swap (for tokens migrated off pump.fun)."""
        try:
            import base64
            
            # Get token balance
            token_balance = await self._get_token_balance(token_mint)
            if not token_balance or token_balance <= 0:
                logger.warning(f"{tier}_no_token_balance", token=token_mint[:8])
                return None
            
            # Calculate sell amount
            sell_amount = int(token_balance * sell_percent / 100)
            if sell_amount <= 0:
                return None
            
            # Get Jupiter quote (token -> SOL)
            quote_params = {
                "inputMint": token_mint,
                "outputMint": NATIVE_SOL,
                "amount": str(sell_amount),
                "slippageBps": "2000",  # 20% slippage
            }
            
            async with self.session.get(
                "https://lite-api.jup.ag/swap/v1/quote",
                params=quote_params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.warning(f"{tier}_jupiter_quote_failed",
                                 token=token_mint[:8],
                                 status=resp.status,
                                 error=error[:100])
                    return None
                quote_data = await resp.json()
            
            # Get swap transaction
            swap_payload = {
                "quoteResponse": quote_data,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": 1000000,
            }
            
            async with self.session.post(
                "https://lite-api.jup.ag/swap/v1/swap",
                json=swap_payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.warning(f"{tier}_jupiter_swap_failed",
                                 token=token_mint[:8],
                                 status=resp.status,
                                 error=error[:100])
                    return None
                swap_data = await resp.json()
            
            swap_tx = swap_data.get("swapTransaction")
            if not swap_tx:
                logger.warning(f"{tier}_jupiter_no_swap_tx", token=token_mint[:8])
                return None
            
            tx_bytes = base64.b64decode(swap_tx)
            logger.info(f"{tier}_jupiter_sell_ready",
                       token=token_mint[:8],
                       sell_amount=sell_amount,
                       out_sol=float(quote_data.get("outAmount", 0)) / SOL_DECIMALS)
            return tx_bytes
            
        except Exception as e:
            logger.error(f"{tier}_jupiter_sell_exception", token=token_mint[:8], error=str(e))
            return None
    
    async def _get_token_balance(self, token_mint: str) -> Optional[int]:
        """Get token balance in raw units."""
        try:
            from solders.pubkey import Pubkey
            result = await self.rpc._request(
                "getTokenAccountsByOwner",
                [
                    str(self.wallet.pubkey()),
                    {"mint": token_mint},
                    {"encoding": "jsonParsed"}
                ]
            )
            
            if result and "value" in result:
                for account in result["value"]:
                    info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    amount = info.get("tokenAmount", {}).get("amount", "0")
                    if int(amount) > 0:
                        return int(amount)
            return None
        except Exception as e:
            logger.error("get_token_balance_error", token=token_mint[:8], error=str(e))
            return None
    
    async def _get_token_price(self, token_address: str) -> Optional[float]:
        """Get token price from Jupiter or cache."""
        # Check cache
        if token_address in self._price_cache:
            price, timestamp = self._price_cache[token_address]
            if datetime.utcnow() - timestamp < self._cache_ttl:
                return price
        
        try:
            # Get Jupiter quote for 1 SOL worth of token
            params = {
                "inputMint": NATIVE_SOL,
                "outputMint": token_address,
                "amount": str(SOL_DECIMALS),  # 1 SOL
                "slippageBps": "100"
            }
            
            async with self.session.get(
                "https://lite-api.jup.ag/swap/v1/quote",
                params=params,
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                out_amount = float(data.get("outAmount", 0))
                
                if out_amount > 0:
                    # Price = tokens per SOL
                    price = out_amount / SOL_DECIMALS
                    self._price_cache[token_address] = (price, datetime.utcnow())
                    return price
                
                return None
                
        except Exception as e:
            logger.debug("price_fetch_error", token=token_address[:8], error=str(e))
            return None
    
    def get_position(self, token_address: str) -> Optional[TieredPosition]:
        """Get position by token address."""
        return self.positions.get(token_address)
    
    def get_all_positions(self) -> List[TieredPosition]:
        """Get all positions."""
        return list(self.positions.values())
    
    def get_stats(self) -> dict:
        """Get manager stats."""
        open_count = sum(1 for p in self.positions.values() if not p.fully_exited)
        total_pnl = sum(p.pnl_percent for p in self.positions.values())
        
        return {
            "total_positions": len(self.positions),
            "open_positions": open_count,
            "tier1_executed": self.tier1_executed,
            "tier2_executed": self.tier2_executed,
            "tier3_executed": self.tier3_executed,
            "avg_pnl": f"{total_pnl / max(len(self.positions), 1):.1f}%",
            "checks_performed": self.checks_performed
        }
