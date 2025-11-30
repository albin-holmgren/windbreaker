"""
Core arbitrage engine for Windbreaker.
Finds and simulates triangular arbitrage opportunities using Jupiter.
"""

import asyncio
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
import aiohttp
import structlog

from .config import (
    Config, 
    TOKENS, 
    TOKENS_DEVNET, 
    DEFAULT_TRIANGLES,
    ESTIMATED_TX_COST_SOL
)

logger = structlog.get_logger()


@dataclass
class Quote:
    """Represents a single swap quote."""
    input_mint: str
    output_mint: str
    input_amount: int
    output_amount: int
    price_impact_pct: float
    route_plan: List[Dict[str, Any]]
    
    @property
    def effective_rate(self) -> float:
        """Calculate effective exchange rate."""
        if self.input_amount == 0:
            return 0
        return self.output_amount / self.input_amount


@dataclass
class TriangleOpportunity:
    """Represents a triangular arbitrage opportunity."""
    path: Tuple[str, str, str]  # Token symbols: (A, B, C)
    quotes: Tuple[Quote, Quote, Quote]  # A->B, B->C, C->A
    input_amount: int  # Initial amount in token A
    final_amount: int  # Final amount back in token A
    profit_amount: int  # Profit in token A
    profit_pct: float  # Profit percentage
    net_profit_pct: float  # Profit after fees
    estimated_tx_cost: int  # Estimated transaction cost in lamports
    
    @property
    def is_profitable(self) -> bool:
        return self.net_profit_pct > 0
    
    def __str__(self) -> str:
        return (
            f"Triangle {self.path[0]}->{self.path[1]}->{self.path[2]}->{self.path[0]}: "
            f"Input={self.input_amount}, Final={self.final_amount}, "
            f"Profit={self.profit_pct:.4f}%, Net={self.net_profit_pct:.4f}%"
        )


class ArbitrageEngine:
    """Engine for finding and simulating triangular arbitrage opportunities."""
    
    def __init__(self, config: Config):
        self.config = config
        self.tokens = TOKENS_DEVNET if config.is_devnet else TOKENS
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
    
    def get_token_mint(self, symbol: str) -> Optional[str]:
        """Get token mint address from symbol."""
        token = self.tokens.get(symbol)
        return token['mint'] if token else None
    
    def get_token_decimals(self, symbol: str) -> int:
        """Get token decimals from symbol."""
        token = self.tokens.get(symbol)
        return token['decimals'] if token else 9
    
    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
    ) -> Optional[Quote]:
        """Get a swap quote from Jupiter API."""
        session = await self._get_session()
        
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(self.config.slippage_bps),
        }
        
        try:
            async with session.get(
                self.config.jupiter_quote_api,
                params=params
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.warning(
                        "quote_api_error",
                        status=response.status,
                        error=error_text[:200]
                    )
                    return None
                
                data = await response.json()
                
                return Quote(
                    input_mint=input_mint,
                    output_mint=output_mint,
                    input_amount=int(data.get('inAmount', amount)),
                    output_amount=int(data.get('outAmount', 0)),
                    price_impact_pct=float(data.get('priceImpactPct', 0)),
                    route_plan=data.get('routePlan', [])
                )
                
        except Exception as e:
            logger.error(
                "quote_request_failed",
                input_mint=input_mint[:8],
                output_mint=output_mint[:8],
                error=str(e)
            )
            return None
    
    async def simulate_triangle(
        self,
        path: Tuple[str, str, str],
        input_amount_usd: float,
        sol_price_usd: float = 100.0
    ) -> Optional[TriangleOpportunity]:
        """
        Simulate a triangular arbitrage: A -> B -> C -> A.
        
        Args:
            path: Tuple of token symbols (A, B, C)
            input_amount_usd: Input amount in USD
            sol_price_usd: Current SOL price in USD
        
        Returns:
            TriangleOpportunity if valid, None if any quote fails
        """
        token_a, token_b, token_c = path
        
        # Get mints
        mint_a = self.get_token_mint(token_a)
        mint_b = self.get_token_mint(token_b)
        mint_c = self.get_token_mint(token_c)
        
        if not all([mint_a, mint_b, mint_c]):
            logger.warning("unknown_token_in_path", path=path)
            return None
        
        # Calculate input amount in token A's base units
        decimals_a = self.get_token_decimals(token_a)
        
        # Convert USD to token A amount
        # For simplicity, assume 1 SOL = sol_price_usd, 1 USDC/USDT = 1 USD
        if token_a == 'SOL':
            input_amount = int((input_amount_usd / sol_price_usd) * (10 ** decimals_a))
        elif token_a in ('USDC', 'USDT'):
            input_amount = int(input_amount_usd * (10 ** decimals_a))
        else:
            # For other tokens, use a rough estimate
            input_amount = int(input_amount_usd * (10 ** decimals_a) / 10)
        
        # Get quotes for each leg
        # A -> B
        quote_ab = await self.get_quote(mint_a, mint_b, input_amount)
        if not quote_ab or quote_ab.output_amount == 0:
            return None
        
        # B -> C
        quote_bc = await self.get_quote(mint_b, mint_c, quote_ab.output_amount)
        if not quote_bc or quote_bc.output_amount == 0:
            return None
        
        # C -> A
        quote_ca = await self.get_quote(mint_c, mint_a, quote_bc.output_amount)
        if not quote_ca or quote_ca.output_amount == 0:
            return None
        
        # Calculate profit
        final_amount = quote_ca.output_amount
        profit_amount = final_amount - input_amount
        profit_pct = (profit_amount / input_amount) * 100 if input_amount > 0 else 0
        
        # Estimate transaction cost
        # 3 swaps typically cost around 0.01-0.03 SOL
        tx_cost_lamports = int(ESTIMATED_TX_COST_SOL * 1e9)
        
        # Convert tx cost to token A for net profit calculation
        if token_a == 'SOL':
            tx_cost_in_token_a = tx_cost_lamports
        else:
            # Approximate conversion
            tx_cost_usd = ESTIMATED_TX_COST_SOL * sol_price_usd
            if token_a in ('USDC', 'USDT'):
                tx_cost_in_token_a = int(tx_cost_usd * (10 ** decimals_a))
            else:
                tx_cost_in_token_a = int(tx_cost_usd * (10 ** decimals_a) / 10)
        
        net_profit_amount = profit_amount - tx_cost_in_token_a
        net_profit_pct = (net_profit_amount / input_amount) * 100 if input_amount > 0 else 0
        
        opportunity = TriangleOpportunity(
            path=path,
            quotes=(quote_ab, quote_bc, quote_ca),
            input_amount=input_amount,
            final_amount=final_amount,
            profit_amount=profit_amount,
            profit_pct=profit_pct,
            net_profit_pct=net_profit_pct,
            estimated_tx_cost=tx_cost_lamports
        )
        
        logger.debug(
            "triangle_simulated",
            path=f"{token_a}->{token_b}->{token_c}",
            input=input_amount,
            output=final_amount,
            profit_pct=f"{profit_pct:.4f}%",
            net_profit_pct=f"{net_profit_pct:.4f}%"
        )
        
        return opportunity
    
    async def scan_triangles(
        self,
        triangles: Optional[List[Tuple[str, str, str]]] = None,
        input_amount_usd: float = 10.0,
        sol_price_usd: float = 100.0
    ) -> List[TriangleOpportunity]:
        """
        Scan multiple triangular paths for arbitrage opportunities.
        
        Args:
            triangles: List of token symbol tuples to scan, defaults to DEFAULT_TRIANGLES
            input_amount_usd: Input amount in USD
            sol_price_usd: Current SOL price
        
        Returns:
            List of opportunities sorted by net profit (descending)
        """
        if triangles is None:
            triangles = DEFAULT_TRIANGLES
        
        # Filter triangles to only include tokens we have
        valid_triangles = [
            t for t in triangles
            if all(self.get_token_mint(s) for s in t)
        ]
        
        if not valid_triangles:
            logger.warning("no_valid_triangles", network=self.config.network)
            return []
        
        logger.info(
            "scanning_triangles",
            count=len(valid_triangles),
            input_usd=input_amount_usd
        )
        
        # Scan all triangles concurrently
        tasks = [
            self.simulate_triangle(path, input_amount_usd, sol_price_usd)
            for path in valid_triangles
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter successful opportunities
        opportunities = []
        for result in results:
            if isinstance(result, TriangleOpportunity):
                opportunities.append(result)
            elif isinstance(result, Exception):
                logger.warning("triangle_scan_error", error=str(result))
        
        # Sort by net profit descending
        opportunities.sort(key=lambda x: x.net_profit_pct, reverse=True)
        
        logger.info(
            "scan_complete",
            total=len(valid_triangles),
            valid=len(opportunities),
            profitable=sum(1 for o in opportunities if o.is_profitable)
        )
        
        return opportunities
    
    async def find_best_opportunity(
        self,
        min_profit_pct: Optional[float] = None,
        input_amount_usd: Optional[float] = None,
        sol_price_usd: float = 100.0
    ) -> Optional[TriangleOpportunity]:
        """
        Find the best profitable arbitrage opportunity.
        
        Args:
            min_profit_pct: Minimum net profit threshold, defaults to config value
            input_amount_usd: Input amount in USD, defaults to config value
            sol_price_usd: Current SOL price
        
        Returns:
            Best opportunity if profitable, None otherwise
        """
        if min_profit_pct is None:
            min_profit_pct = self.config.min_profit_pct
        
        if input_amount_usd is None:
            input_amount_usd = self.config.trade_amount_usd
        
        opportunities = await self.scan_triangles(
            input_amount_usd=input_amount_usd,
            sol_price_usd=sol_price_usd
        )
        
        # Find first opportunity meeting threshold
        for opp in opportunities:
            if opp.net_profit_pct >= min_profit_pct:
                logger.info(
                    "profitable_opportunity_found",
                    path=f"{opp.path[0]}->{opp.path[1]}->{opp.path[2]}",
                    net_profit_pct=f"{opp.net_profit_pct:.4f}%"
                )
                return opp
        
        if opportunities:
            best = opportunities[0]
            logger.debug(
                "no_profitable_opportunity",
                best_path=f"{best.path[0]}->{best.path[1]}->{best.path[2]}",
                best_profit_pct=f"{best.net_profit_pct:.4f}%",
                threshold=f"{min_profit_pct}%"
            )
        
        return None


def create_arb_engine(config: Config) -> ArbitrageEngine:
    """Factory function to create an arbitrage engine."""
    return ArbitrageEngine(config)
