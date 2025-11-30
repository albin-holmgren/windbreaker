"""
Transaction executor for Windbreaker.
Builds and sends swap transactions using Jupiter.
"""

import asyncio
import base64
from dataclasses import dataclass
from typing import Optional, Dict, Any
import aiohttp
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
import structlog

from .config import Config
from .wallet import Wallet
from .rpc import RPCClient
from .arb_engine import TriangleOpportunity, Quote

logger = structlog.get_logger()


@dataclass
class ExecutionResult:
    """Result of a trade execution."""
    success: bool
    signature: Optional[str]
    opportunity: TriangleOpportunity
    error: Optional[str] = None
    
    @property
    def explorer_url(self) -> Optional[str]:
        """Get Solana explorer URL for the transaction."""
        if not self.signature:
            return None
        return f"https://solscan.io/tx/{self.signature}"


class Executor:
    """Executes arbitrage trades on Solana via Jupiter."""
    
    def __init__(
        self,
        config: Config,
        wallet: Wallet,
        rpc_client: RPCClient
    ):
        self.config = config
        self.wallet = wallet
        self.rpc = rpc_client
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def get_swap_transaction(
        self,
        quote: Quote,
        user_pubkey: str
    ) -> Optional[VersionedTransaction]:
        """
        Get a swap transaction from Jupiter.
        
        Args:
            quote: The quote to execute
            user_pubkey: User's public key
        
        Returns:
            Unsigned versioned transaction
        """
        session = await self._get_session()
        
        # Build swap request
        payload = {
            "quoteResponse": {
                "inputMint": quote.input_mint,
                "outputMint": quote.output_mint,
                "inAmount": str(quote.input_amount),
                "outAmount": str(quote.output_amount),
                "priceImpactPct": str(quote.price_impact_pct),
                "routePlan": quote.route_plan
            },
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto"
        }
        
        try:
            async with session.post(
                self.config.jupiter_swap_api,
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        "swap_api_error",
                        status=response.status,
                        error=error_text[:200]
                    )
                    return None
                
                data = await response.json()
                
                # Decode the transaction
                swap_tx_b64 = data.get("swapTransaction")
                if not swap_tx_b64:
                    logger.error("no_swap_transaction_in_response")
                    return None
                
                tx_bytes = base64.b64decode(swap_tx_b64)
                transaction = VersionedTransaction.from_bytes(tx_bytes)
                
                return transaction
                
        except Exception as e:
            logger.error("get_swap_transaction_failed", error=str(e))
            return None
    
    async def execute_single_swap(
        self,
        quote: Quote
    ) -> Optional[str]:
        """
        Execute a single swap and return the transaction signature.
        
        Args:
            quote: The quote to execute
        
        Returns:
            Transaction signature if successful
        """
        # Get the swap transaction
        transaction = await self.get_swap_transaction(
            quote,
            self.wallet.address
        )
        
        if not transaction:
            return None
        
        try:
            # Sign the transaction
            signed_tx = self.wallet.sign_versioned_transaction(transaction)
            
            # Send the transaction
            signature = await self.rpc.send_transaction(signed_tx)
            
            logger.info(
                "swap_sent",
                signature=signature,
                input_mint=quote.input_mint[:8],
                output_mint=quote.output_mint[:8]
            )
            
            return signature
            
        except Exception as e:
            logger.error("swap_execution_failed", error=str(e))
            return None
    
    async def execute_triangle(
        self,
        opportunity: TriangleOpportunity,
        simulate_first: bool = True
    ) -> ExecutionResult:
        """
        Execute a triangular arbitrage trade.
        
        This executes the three swaps atomically through Jupiter's route.
        
        Args:
            opportunity: The arbitrage opportunity to execute
            simulate_first: Whether to simulate before sending
        
        Returns:
            ExecutionResult with success status and details
        """
        quote_ab, quote_bc, quote_ca = opportunity.quotes
        
        logger.info(
            "executing_triangle",
            path=f"{opportunity.path[0]}->{opportunity.path[1]}->{opportunity.path[2]}",
            expected_profit=f"{opportunity.net_profit_pct:.4f}%"
        )
        
        try:
            # For atomic execution, we need to build a combined route
            # Jupiter can handle multi-hop swaps as a single transaction
            # We'll execute the full A -> B -> C -> A route
            
            # Get quote for full route (A -> A via B and C)
            session = await self._get_session()
            
            # Request a full route quote
            params = {
                "inputMint": quote_ab.input_mint,
                "outputMint": quote_ab.input_mint,  # Same as input (back to A)
                "amount": str(opportunity.input_amount),
                "slippageBps": str(self.config.slippage_bps),
            }
            
            # Get the combined quote
            async with session.get(
                self.config.jupiter_quote_api,
                params=params
            ) as response:
                if response.status != 200:
                    # Fallback: execute swaps sequentially
                    return await self._execute_sequential(opportunity)
                
                quote_data = await response.json()
            
            # Build swap transaction for the full route
            payload = {
                "quoteResponse": quote_data,
                "userPublicKey": self.wallet.address,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            async with session.post(
                self.config.jupiter_swap_api,
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status != 200:
                    return await self._execute_sequential(opportunity)
                
                swap_data = await response.json()
            
            # Decode and sign transaction
            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                return await self._execute_sequential(opportunity)
            
            tx_bytes = base64.b64decode(swap_tx_b64)
            transaction = VersionedTransaction.from_bytes(tx_bytes)
            
            # Optionally simulate first
            if simulate_first:
                sim_result = await self.rpc.simulate_transaction(transaction)
                if sim_result.get("err"):
                    return ExecutionResult(
                        success=False,
                        signature=None,
                        opportunity=opportunity,
                        error=f"Simulation failed: {sim_result['err']}"
                    )
            
            # Sign and send
            signed_tx = self.wallet.sign_versioned_transaction(transaction)
            signature = await self.rpc.send_transaction(signed_tx)
            
            # Wait for confirmation
            confirmed = await self.rpc.confirm_transaction(signature)
            
            if confirmed:
                logger.info(
                    "triangle_executed",
                    signature=signature,
                    path=f"{opportunity.path[0]}->{opportunity.path[1]}->{opportunity.path[2]}",
                    profit_pct=f"{opportunity.net_profit_pct:.4f}%"
                )
                
                return ExecutionResult(
                    success=True,
                    signature=signature,
                    opportunity=opportunity
                )
            else:
                return ExecutionResult(
                    success=False,
                    signature=signature,
                    opportunity=opportunity,
                    error="Transaction not confirmed"
                )
                
        except Exception as e:
            logger.error("triangle_execution_failed", error=str(e))
            return ExecutionResult(
                success=False,
                signature=None,
                opportunity=opportunity,
                error=str(e)
            )
    
    async def _execute_sequential(
        self,
        opportunity: TriangleOpportunity
    ) -> ExecutionResult:
        """
        Execute triangle as sequential swaps (fallback).
        
        WARNING: This is not atomic and may result in partial execution.
        Only use for testing or when combined route is not available.
        """
        logger.warning("falling_back_to_sequential_execution")
        
        quote_ab, quote_bc, quote_ca = opportunity.quotes
        signatures = []
        
        try:
            # Swap A -> B
            sig1 = await self.execute_single_swap(quote_ab)
            if not sig1:
                return ExecutionResult(
                    success=False,
                    signature=None,
                    opportunity=opportunity,
                    error="First swap failed"
                )
            signatures.append(sig1)
            
            # Wait for confirmation before next swap
            if not await self.rpc.confirm_transaction(sig1):
                return ExecutionResult(
                    success=False,
                    signature=sig1,
                    opportunity=opportunity,
                    error="First swap not confirmed"
                )
            
            # Swap B -> C
            sig2 = await self.execute_single_swap(quote_bc)
            if not sig2:
                return ExecutionResult(
                    success=False,
                    signature=sig1,
                    opportunity=opportunity,
                    error="Second swap failed (PARTIAL EXECUTION)"
                )
            signatures.append(sig2)
            
            if not await self.rpc.confirm_transaction(sig2):
                return ExecutionResult(
                    success=False,
                    signature=sig2,
                    opportunity=opportunity,
                    error="Second swap not confirmed (PARTIAL EXECUTION)"
                )
            
            # Swap C -> A
            sig3 = await self.execute_single_swap(quote_ca)
            if not sig3:
                return ExecutionResult(
                    success=False,
                    signature=sig2,
                    opportunity=opportunity,
                    error="Third swap failed (PARTIAL EXECUTION)"
                )
            signatures.append(sig3)
            
            if not await self.rpc.confirm_transaction(sig3):
                return ExecutionResult(
                    success=False,
                    signature=sig3,
                    opportunity=opportunity,
                    error="Third swap not confirmed (PARTIAL EXECUTION)"
                )
            
            # All swaps successful
            return ExecutionResult(
                success=True,
                signature=sig3,  # Last signature
                opportunity=opportunity
            )
            
        except Exception as e:
            return ExecutionResult(
                success=False,
                signature=signatures[-1] if signatures else None,
                opportunity=opportunity,
                error=f"Sequential execution failed: {e}"
            )


def create_executor(
    config: Config,
    wallet: Wallet,
    rpc_client: RPCClient
) -> Executor:
    """Factory function to create an executor."""
    return Executor(config, wallet, rpc_client)
