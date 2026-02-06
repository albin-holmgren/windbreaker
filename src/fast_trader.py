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
    
    async def execute_buy(self, signal: TradingSignal) -> bool:
        """
        Execute a buy for a signal.
        Returns True if successful.
        """
        token_address = signal.token_address
        
        self.buys_attempted += 1
        
        # Check balance
        can_trade, available = await self.can_execute_trade()
        if not can_trade:
            return False
        
        logger.info("executing_buy",
                   token=token_address[:8] + "...",
                   amount=self.trade_amount_sol,
                   available=available)
        
        try:
            # Try pump.fun first (fastest for new tokens)
            result = await self._execute_pumpfun_buy(token_address)
            
            if result:
                self.buys_successful += 1
                self.total_sol_spent += self.trade_amount_sol
                self.open_positions[token_address] = {
                    "entry_time": datetime.utcnow(),
                    "entry_price": None,  # Will be filled by position manager
                    "amount_sol": self.trade_amount_sol,
                    "source_chat": signal.source_chat,
                }
                logger.info("buy_success",
                           token=token_address[:8],
                           signature=result[:16] if result else None)
                return True
            else:
                logger.warning("buy_failed", token=token_address[:8])
                return False
                
        except Exception as e:
            logger.error("buy_execution_error",
                        token=token_address[:8],
                        error=str(e))
            return False
    
    async def _execute_pumpfun_buy(self, token_mint: str) -> Optional[str]:
        """Execute buy via pump.fun API."""
        try:
            import base64
            from solders.transaction import VersionedTransaction
            
            # Build payload
            payload = {
                "publicKey": str(self.wallet.pubkey()),
                "action": "buy",
                "mint": token_mint,
                "denominatedInSol": "true",
                "amount": self.trade_amount_sol,
                "slippage": 15,  # 15% slippage for speed
                "priorityFee": 0.001,  # High priority
                "pool": "auto"
            }
            
            async with self.session.post(
                PUMPFUN_API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.warning("pumpfun_buy_failed",
                                 token=token_mint[:8],
                                 status=resp.status,
                                 error=error[:100])
                    return None
                
                tx_bytes = await resp.read()
            
            # Sign and send
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet])
            
            signature = await self.rpc.send_transaction(
                signed_tx,
                skip_preflight=True,  # Skip preflight for speed
                max_retries=3
            )
            
            # Quick confirmation check
            confirmed = await self._confirm_transaction(str(signature))
            
            if confirmed:
                return str(signature)
            else:
                logger.warning("tx_not_confirmed",
                             token=token_mint[:8],
                             signature=str(signature)[:16])
                return None
                
        except Exception as e:
            logger.error("pumpfun_buy_error", token=token_mint[:8], error=str(e))
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
            "success_rate": f"{(self.buys_successful / max(self.buys_attempted, 1) * 100):.1f}%"
        }
