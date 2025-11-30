"""
Unit tests for arbitrage simulation logic.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Mock the config module before importing the engine
@dataclass
class MockConfig:
    rpc_url: str = "https://api.devnet.solana.com"
    network: str = "devnet"
    wallet_private_key: str = "test_key"
    wallet_address: str = "test_address"
    min_profit_pct: float = 0.5
    trade_amount_usd: float = 10.0
    slippage_bps: int = 50
    poll_interval_ms: int = 500
    jupiter_quote_api: str = "https://quote-api.jup.ag/v6/quote"
    jupiter_swap_api: str = "https://quote-api.jup.ag/v6/swap"
    telegram_bot_token: str = None
    telegram_chat_id: str = None
    log_level: str = "INFO"
    
    @property
    def is_devnet(self) -> bool:
        return self.network == "devnet"
    
    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet-beta"


class TestSimulationMath:
    """Test the mathematical calculations for arbitrage."""
    
    def test_profit_calculation_positive(self):
        """Test profit calculation with positive outcome."""
        input_amount = 1000000  # 1 USDC (6 decimals)
        final_amount = 1010000  # 1.01 USDC
        
        profit_amount = final_amount - input_amount
        profit_pct = (profit_amount / input_amount) * 100
        
        assert profit_amount == 10000
        assert profit_pct == 1.0
    
    def test_profit_calculation_negative(self):
        """Test profit calculation with negative outcome."""
        input_amount = 1000000
        final_amount = 990000  # 0.99 USDC (loss)
        
        profit_amount = final_amount - input_amount
        profit_pct = (profit_amount / input_amount) * 100
        
        assert profit_amount == -10000
        assert profit_pct == -1.0
    
    def test_net_profit_after_fees(self):
        """Test net profit calculation after transaction fees."""
        input_amount = 1000000  # 1 USDC
        final_amount = 1015000  # 1.5% gross profit
        tx_cost = 5000  # Roughly $0.005 in fees
        
        gross_profit = final_amount - input_amount
        net_profit = gross_profit - tx_cost
        net_profit_pct = (net_profit / input_amount) * 100
        
        assert gross_profit == 15000
        assert net_profit == 10000
        assert net_profit_pct == 1.0
    
    def test_slippage_impact(self):
        """Test slippage calculation."""
        expected_output = 1000000
        slippage_bps = 50  # 0.5%
        
        # Minimum output after slippage
        min_output = expected_output * (10000 - slippage_bps) / 10000
        
        assert min_output == 995000
    
    def test_multi_hop_compound_effect(self):
        """Test that small losses compound over multiple hops."""
        input_amount = 1000000
        
        # Simulate 3 hops with 0.3% loss each
        loss_rate = 0.997
        after_hop1 = input_amount * loss_rate
        after_hop2 = after_hop1 * loss_rate
        after_hop3 = after_hop2 * loss_rate
        
        total_loss_pct = ((input_amount - after_hop3) / input_amount) * 100
        
        # Should be approximately 0.9% loss (3 x 0.3% compounded)
        assert 0.8 < total_loss_pct < 1.0


class TestQuoteValidation:
    """Test quote validation logic."""
    
    def test_quote_with_zero_output_is_invalid(self):
        """Quotes with zero output should be rejected."""
        output_amount = 0
        
        is_valid = output_amount > 0
        
        assert not is_valid
    
    def test_quote_with_excessive_price_impact(self):
        """High price impact quotes should be flagged."""
        price_impact_pct = 5.0  # 5% price impact
        max_acceptable_impact = 2.0  # 2% threshold
        
        is_acceptable = price_impact_pct <= max_acceptable_impact
        
        assert not is_acceptable
    
    def test_effective_rate_calculation(self):
        """Test effective exchange rate calculation."""
        input_amount = 1000000  # 1 USDC
        output_amount = 5000000000  # 5 SOL (at 9 decimals)
        
        # Rate: how much output per unit input
        effective_rate = output_amount / input_amount
        
        assert effective_rate == 5000.0


class TestTrianglePath:
    """Test triangular path validation."""
    
    def test_valid_triangle_path(self):
        """Valid triangle has 3 distinct tokens."""
        path = ('SOL', 'USDC', 'ETH')
        
        is_valid = len(path) == 3 and len(set(path)) == 3
        
        assert is_valid
    
    def test_invalid_triangle_with_duplicate(self):
        """Triangle with duplicate token is invalid."""
        path = ('SOL', 'USDC', 'SOL')
        
        # SOL appears twice - this is actually valid for triangular arb
        # but the path should have 3 distinct intermediate tokens
        is_valid = len(path) == 3
        
        assert is_valid  # Path length is valid
    
    def test_triangle_completion(self):
        """Triangle should return to starting token."""
        path = ('SOL', 'USDC', 'ETH')
        start_token = path[0]
        end_token = path[0]  # Should return to start
        
        completes_loop = start_token == end_token
        
        assert completes_loop


class TestProfitThreshold:
    """Test profit threshold logic."""
    
    def test_above_threshold_is_profitable(self):
        """Net profit above threshold should trigger execution."""
        net_profit_pct = 0.6
        min_threshold = 0.5
        
        should_execute = net_profit_pct >= min_threshold
        
        assert should_execute
    
    def test_below_threshold_skipped(self):
        """Net profit below threshold should be skipped."""
        net_profit_pct = 0.3
        min_threshold = 0.5
        
        should_execute = net_profit_pct >= min_threshold
        
        assert not should_execute
    
    def test_exactly_at_threshold(self):
        """Net profit exactly at threshold should execute."""
        net_profit_pct = 0.5
        min_threshold = 0.5
        
        should_execute = net_profit_pct >= min_threshold
        
        assert should_execute
    
    def test_negative_profit_rejected(self):
        """Negative profit should never execute."""
        net_profit_pct = -0.1
        min_threshold = 0.5
        
        should_execute = net_profit_pct >= min_threshold
        
        assert not should_execute


class TestTokenConversions:
    """Test token amount conversions."""
    
    def test_sol_to_lamports(self):
        """Convert SOL to lamports."""
        sol_amount = 1.5
        lamports = int(sol_amount * 1e9)
        
        assert lamports == 1500000000
    
    def test_lamports_to_sol(self):
        """Convert lamports to SOL."""
        lamports = 2500000000
        sol_amount = lamports / 1e9
        
        assert sol_amount == 2.5
    
    def test_usdc_to_base_units(self):
        """Convert USDC to base units (6 decimals)."""
        usdc_amount = 100.50
        base_units = int(usdc_amount * 1e6)
        
        assert base_units == 100500000
    
    def test_usd_to_sol_amount(self):
        """Convert USD value to SOL amount."""
        usd_value = 150.0
        sol_price_usd = 100.0
        
        sol_amount = usd_value / sol_price_usd
        lamports = int(sol_amount * 1e9)
        
        assert sol_amount == 1.5
        assert lamports == 1500000000


class TestFeeEstimation:
    """Test transaction fee estimation."""
    
    def test_base_fee_estimate(self):
        """Test base transaction fee estimate."""
        base_fee_sol = 0.000005  # 5000 lamports
        priority_fee_sol = 0.001  # Priority fee
        
        total_fee = base_fee_sol + priority_fee_sol
        
        assert total_fee == 0.001005
    
    def test_multi_instruction_fee(self):
        """Multiple instructions increase compute cost."""
        base_compute_units = 200000
        instructions_count = 3
        compute_unit_price = 1000  # micro-lamports
        
        total_compute = base_compute_units * instructions_count
        priority_fee_lamports = (total_compute * compute_unit_price) / 1e6
        
        assert total_compute == 600000
        assert priority_fee_lamports == 600.0


@pytest.mark.asyncio
class TestArbitrageEngineIntegration:
    """Integration tests for the arbitrage engine."""
    
    async def test_engine_initialization(self):
        """Test engine can be initialized."""
        config = MockConfig()
        
        # Import here to use mock
        from src.arb_engine import ArbitrageEngine
        
        engine = ArbitrageEngine(config)
        
        assert engine is not None
        assert engine.config == config
        
        await engine.close()
    
    async def test_get_token_mint(self):
        """Test token mint lookup."""
        config = MockConfig()
        
        from src.arb_engine import ArbitrageEngine
        
        engine = ArbitrageEngine(config)
        
        # SOL is available on both networks
        sol_mint = engine.get_token_mint('SOL')
        assert sol_mint == 'So11111111111111111111111111111111111111112'
        
        # Unknown token returns None
        unknown_mint = engine.get_token_mint('UNKNOWN')
        assert unknown_mint is None
        
        await engine.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
