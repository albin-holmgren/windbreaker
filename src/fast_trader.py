"""
Balance-Aware Fast Trader - Executes trades immediately with balance checks.
No validation, no market data - pure speed.
"""

import asyncio
import aiohttp
from typing import Optional, Dict
from decimal import Decimal
from datetime import datetime
import structlog

from .ai_parser import AIGatewayParser
from .signal_manager import TradingSignal
from .config import Config
from .rpc import RPCClient

logger = structlog.get_logger(__name__)

# Constants
SOL_DECIMALS = 1_000_000_000
NATIVE_SOL = "So11111111111111111111111111111111111111112"
PUMPFUN_API = "https://pumpportal.fun/api/trade-local"
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://lite-api.jup.ag/swap/v1/swap"


class FastTrader:
    """Fast trader with balance-aware position management."""
    
    def __init__(
        self,
        config: Config,
        rpc_client: RPCClient,
        wallet_keypair,
        trade_amount_sol: float = 0.05,
        exit_fee_reserve_per_position: float = 0.02,
        min_balance_buffer: float = 0.01,
    ):
        self.config = config
        self.rpc = rpc_client
        self.wallet = wallet_keypair
        
        self.trade_amount_sol = trade_amount_sol
        self.exit_fee_reserve = exit_fee_reserve_per_position
        self.min_buffer = min_balance_buffer
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Track open positions for balance calculation
        self.open_positions: Dict[str, dict] = {}  # token -> position info
        
        # Track recently bought tokens to prevent re-buying
        self._recently_bought: Dict[str, datetime] = {}
        
        # Dynamic sizing config
        self.min_trade_sol = 0.01  # Minimum 0.01 SOL per trade
        self.max_trade_sol = trade_amount_sol * 2  # Max 2x the base amount
        self.max_position_percent = 0.25  # Max 25% of balance in one position
        
        # Stats
        self.buys_attempted = 0
        self.buys_successful = 0
        self.skipped_insufficient_balance = 0
        self.total_sol_spent = 0.0
    
    async def start(self) -> None:
        """Initialize HTTP session."""
        self.session = aiohttp.ClientSession()
        logger.info("fast_trader_started",
                   trade_amount=self.trade_amount_sol,
                   exit_fee_reserve=self.exit_fee_reserve)
    
    async def stop(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
    
    async def can_execute_trade(self) -> tuple[bool, float]:
        """
        Check if we have enough balance to execute a trade.
        Returns (can_trade, available_balance).
        """
        try:
            balance_lamports = await self.rpc.get_balance(self.wallet.pubkey())
            balance_sol = balance_lamports / SOL_DECIMALS
            
            open_count = len(self.open_positions)
            reserved = (self.exit_fee_reserve * open_count) + self.min_buffer
            available = balance_sol - reserved
            
            can_trade = available > self.trade_amount_sol
            
            if not can_trade:
                self.skipped_insufficient_balance += 1
                logger.warning("insufficient_balance",
                             balance=balance_sol,
                             open_positions=open_count,
                             reserved=reserved,
                             available=available,
                             needed=self.trade_amount_sol)
            
            return can_trade, available
            
        except Exception as e:
            logger.error("balance_check_error", error=str(e))
            return False, 0.0
    
    async def _get_token_market_cap(self, token_mint: str) -> Optional[float]:
        """Get token market cap from DexScreener or similar."""
        try:
            # Try DexScreener API for market cap
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        # Get highest liquidity pair
                        best_pair = max(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0)
                        market_cap = best_pair.get("marketCap")
                        if market_cap:
                            return float(market_cap)
                        
                        # Fallback: estimate from price and supply
                        price_usd = best_pair.get("priceUsd")
                        if price_usd:
                            # Assume typical pump.fun supply ~1B tokens
                            return float(price_usd) * 1_000_000_000
            
            return None
        except Exception:
            return None
    
    def _calculate_dynamic_trade_size(self, available_sol: float, market_cap: Optional[float] = None) -> float:
        """
        Calculate trade size as 5% of the token's market cap.
        
        Rules:
        - Invest 5% of market cap value
        - Cap at available balance limits
        - Minimum 0.05 SOL per trade
        """
        # SOL price approx $200 (adjust if needed)
        SOL_PRICE_USD = 200.0
        
        if market_cap and market_cap > 0:
            # Calculate 5% of market cap in USD
            target_investment_usd = market_cap * 0.05
            
            # Convert to SOL
            trade_amount = target_investment_usd / SOL_PRICE_USD
        else:
            # Default to base amount if no market cap data
            trade_amount = self.trade_amount_sol
        
        # Cap at available balance (leave some buffer)
        max_by_balance = available_sol * 0.80  # Use up to 80% of available
        trade_amount = min(trade_amount, max_by_balance)
        
        # Hard cap at max_trade_sol (0.10 SOL)
        trade_amount = min(trade_amount, self.max_trade_sol)
        
        # ENSURE MINIMUM: Never go below 0.05 SOL if we can afford it
        if available_sol >= self.trade_amount_sol:
            trade_amount = max(trade_amount, self.trade_amount_sol)
        else:
            # If balance is really low, use what we have (up to 80%)
            trade_amount = min(available_sol * 0.80, self.trade_amount_sol)
        
        return round(trade_amount, 4)
    
    async def execute_buy(self, signal: TradingSignal) -> bool:
        """
        Execute a buy for a signal.
        Returns True if successful.
        """
        token_address = signal.token_address
        
        self.buys_attempted += 1
        
        # Check if we already bought this token recently (duplicate prevention)
        now = datetime.utcnow()
        if token_address in self._recently_bought:
            last_buy = self._recently_bought[token_address]
            if (now - last_buy).total_seconds() < 86400:  # 24 hours
                logger.info("skipping_duplicate_buy",
                           token=token_address[:8],
                           hours_ago=(now - last_buy).total_seconds() / 3600)
                return False
        
        # Check balance
        can_trade, available = await self.can_execute_trade()
        if not can_trade:
            return False
        
        # Get market cap for dynamic sizing
        market_cap = await self._get_token_market_cap(token_address)
        
        # Calculate dynamic trade amount
        trade_amount = self._calculate_dynamic_trade_size(available, market_cap)
        
        logger.info("executing_buy",
                   token=token_address[:8] + "...",
                   amount=trade_amount,
                   base_amount=self.trade_amount_sol,
                   market_cap=f"${market_cap:,.0f}" if market_cap else "unknown",
                   available=available)
        
        try:
            # Try pump.fun first (fastest for new tokens)
            result = await self._execute_pumpfun_buy(token_address, trade_amount)
            
            if result:
                self.buys_successful += 1
                self.total_sol_spent += trade_amount
                self._recently_bought[token_address] = datetime.utcnow()
                self.open_positions[token_address] = {
                    "entry_time": datetime.utcnow(),
                    "entry_price": None,
                    "amount_sol": trade_amount,
                    "source_chat": signal.source_chat,
                }
                logger.info("buy_success",
                           token=token_address[:8],
                           amount=trade_amount,
                           signature=result[:16] if result else None)
                return True
            else:
                # Fallback: Try Jupiter if pump.fun fails
                logger.info("pumpfun_failed_trying_jupiter", token=token_address[:8])
                jupiter_result = await self._execute_jupiter_buy(token_address, trade_amount)
                
                if jupiter_result:
                    self.buys_successful += 1
                    self.total_sol_spent += trade_amount
                    self._recently_bought[token_address] = datetime.utcnow()
                    self.open_positions[token_address] = {
                        "entry_time": datetime.utcnow(),
                        "entry_price": None,
                        "amount_sol": trade_amount,
                        "source_chat": signal.source_chat,
                    }
                    logger.info("buy_success_jupiter",
                               token=token_address[:8],
                               amount=trade_amount,
                               signature=jupiter_result[:16] if jupiter_result else None)
                    return True
                else:
                    logger.warning("buy_failed", token=token_address[:8])
                    return False
                
        except Exception as e:
            logger.error("buy_execution_error",
                        token=token_address[:8],
                        error=str(e))
            return False
    
    async def _execute_pumpfun_buy(self, token_mint: str, trade_amount: float) -> Optional[str]:
        """Execute buy via pump.fun API - with pool fallbacks like copy trader."""
        try:
            import base64
            from solders.transaction import VersionedTransaction
            
            # Try multiple pools like copy_trader does
            pools_to_try = ["auto", "pump", "pump-amm", "raydium", "raydium-cpmm"]
            last_error = None
            
            for pool in pools_to_try:
                # Build payload with dynamic amount
                payload = {
                    "publicKey": str(self.wallet.pubkey()),
                    "action": "buy",
                    "mint": token_mint,
                    "denominatedInSol": "true",
                    "amount": trade_amount,
                    "slippage": 15,  # 15% slippage for speed
                    "priorityFee": 0.001,  # High priority
                    "pool": pool
                }
                
                logger.debug("pumpfun_trying_pool", token=token_mint[:8], pool=pool)
                
                tx_bytes = None
                status_code = None
                error_text = None
                
                # Try JSON first
                try:
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
                except Exception as e:
                    error_text = str(e)
                
                # If 400 error, retry with form data
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
                                logger.info("pumpfun_form_retry_success", token=token_mint[:8], pool=pool)
                            else:
                                error_text2 = await resp2.text()
                                error_text = f"{error_text} | form: {error_text2}"
                    except Exception as e:
                        error_text = f"{error_text} | form_exception: {e}"
                
                if tx_bytes is not None:
                    # Success! Sign and send
                    try:
                        tx = VersionedTransaction.from_bytes(tx_bytes)
                        signed_tx = VersionedTransaction(tx.message, [self.wallet])
                        
                        signature = await self.rpc.send_transaction(
                            signed_tx,
                            skip_preflight=True  # Skip preflight for speed
                        )
                        
                        # Quick confirmation check
                        confirmed = await self._confirm_transaction(str(signature))
                        
                        if confirmed:
                            logger.info("pumpfun_buy_success", 
                                       token=token_mint[:8], 
                                       pool=pool,
                                       signature=str(signature)[:16])
                            return str(signature)
                        else:
                            logger.warning("tx_not_confirmed",
                                         token=token_mint[:8],
                                         pool=pool,
                                         signature=str(signature)[:16])
                            last_error = f"{pool}: tx_not_confirmed"
                            continue  # Try next pool
                            
                    except Exception as e:
                        logger.error("sign_send_error", token=token_mint[:8], pool=pool, error=str(e))
                        last_error = f"{pool}: sign_error: {e}"
                        continue
                else:
                    last_error = f"{pool}: HTTP {status_code} - {error_text[:50] if error_text else 'unknown'}"
                    logger.debug("pumpfun_pool_failed", 
                                token=token_mint[:8], 
                                pool=pool, 
                                status=status_code,
                                error=error_text[:100] if error_text else None)
                    continue  # Try next pool
            
            # All pools failed
            logger.warning("pumpfun_buy_failed_all_pools",
                         token=token_mint[:8],
                         last_error=last_error)
            return None
                
        except Exception as e:
            logger.error("pumpfun_buy_error", token=token_mint[:8], error=str(e))
            return None
    
    async def _execute_jupiter_buy(self, token_mint: str, trade_amount: float) -> Optional[str]:
        """Execute buy via Jupiter API as fallback."""
        try:
            import base64
            from solders.transaction import VersionedTransaction
            
            trade_lamports = int(trade_amount * SOL_DECIMALS)
            
            # Get quote from Jupiter
            quote_url = (
                f"{JUPITER_QUOTE_API}?inputMint={NATIVE_SOL}"
                f"&outputMint={token_mint}"
                f"&amount={trade_lamports}"
                f"&slippageBps=1500"  # 15% slippage
            )
            
            async with self.session.get(
                quote_url,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.debug("jupiter_quote_failed", 
                                token=token_mint[:8], 
                                status=resp.status)
                    return None
                
                quote_data = await resp.json()
            
            if not quote_data or "route" not in quote_data:
                logger.debug("jupiter_no_route", token=token_mint[:8])
                return None
            
            # Build swap transaction
            swap_payload = {
                "userPublicKey": str(self.wallet.pubkey()),
                "route": quote_data["route"],
                "wrapUnwrapSOL": True,
                "feeAccount": None,
                "priorityFeeLamports": 500000  # 0.0005 SOL priority fee
            }
            
            async with self.session.post(
                JUPITER_SWAP_API,
                json=swap_payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.debug("jupiter_swap_failed",
                                token=token_mint[:8],
                                status=resp.status)
                    return None
                
                swap_data = await resp.json()
            
            if not swap_data or "swapTransaction" not in swap_data:
                logger.debug("jupiter_no_swap_tx", token=token_mint[:8])
                return None
            
            # Deserialize and sign
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            # Send transaction
            signature = await self.rpc.send_transaction(
                signed_tx,
                skip_preflight=True
            )
            
            # Confirm
            confirmed = await self._confirm_transaction(str(signature))
            
            if confirmed:
                logger.info("jupiter_buy_success",
                           token=token_mint[:8],
                           signature=str(signature)[:16])
                return str(signature)
            else:
                logger.warning("jupiter_tx_not_confirmed",
                             token=token_mint[:8],
                             signature=str(signature)[:16])
                return None
                
        except Exception as e:
            logger.error("jupiter_buy_error", token=token_mint[:8], error=str(e))
            return None
    
    async def _confirm_transaction(self, signature: str, max_wait: int = 30) -> bool:
        """Quick confirmation check."""
        for i in range(max_wait):
            try:
                result = await self.rpc._request(
                    "getSignatureStatuses",
                    [[signature], {"searchTransactionHistory": True}]
                )
                
                if result and "value" in result and result["value"]:
                    status = result["value"][0]
                    if status:
                        if status.get("err"):
                            return False
                        confirmation_status = status.get("confirmationStatus")
                        if confirmation_status in ["confirmed", "finalized"]:
                            return True
            except Exception:
                pass
            
            await asyncio.sleep(1)
        
        return False
    
    def register_position(self, token_address: str, entry_price: float) -> None:
        """Register position with entry price for tiered selling."""
        if token_address in self.open_positions:
            self.open_positions[token_address]["entry_price"] = entry_price
    
    def close_position(self, token_address: str) -> None:
        """Remove position from tracking."""
        if token_address in self.open_positions:
            del self.open_positions[token_address]
    
    def get_open_position_count(self) -> int:
        """Get number of open positions."""
        return len(self.open_positions)
    
    def get_position(self, token_address: str) -> Optional[dict]:
        """Get position info."""
        return self.open_positions.get(token_address)
    
    def get_stats(self) -> dict:
        """Get trader stats."""
        return {
            "buys_attempted": self.buys_attempted,
            "buys_successful": self.buys_successful,
            "skipped_balance": self.skipped_insufficient_balance,
            "open_positions": len(self.open_positions),
            "total_sol_spent": f"{self.total_sol_spent:.4f}",
            "success_rate": f"{(self.buys_successful / max(self.buys_attempted, 1) * 100):.1f}%",
            "dynamic_sizing": True,
            "min_trade": self.min_trade_sol,
            "max_trade": self.max_trade_sol,
            "max_position_pct": f"{self.max_position_percent*100:.0f}%"
        }
