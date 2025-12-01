"""
Configuration loader for Windbreaker.
Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv


@dataclass
class Config:
    """Bot configuration loaded from environment variables."""
    
    # Network
    rpc_url: str
    network: str  # 'devnet' or 'mainnet-beta'
    
    # Wallet
    wallet_private_key: str
    wallet_address: Optional[str]
    
    # Trading
    min_profit_pct: float
    trade_amount_usd: float
    trade_balance_pct: float  # Percentage of balance to trade (0-100)
    fee_reserve_sol: float  # SOL to reserve for tx fees
    slippage_bps: int
    poll_interval_ms: int
    
    # Jupiter API
    jupiter_quote_api: str
    jupiter_swap_api: str
    
    # Alerts
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    
    # Copy Trading
    copy_enabled: bool
    copy_wallets: str  # Comma-separated wallet addresses
    copy_balance_pct: float  # Percentage of balance to use per copy
    copy_max_sol: float  # Maximum SOL per copy trade
    copy_min_sol: float  # Minimum SOL to trigger copy
    copy_poll_interval_ms: int  # How often to poll wallets
    copy_sells: bool  # Whether to copy sells
    copy_proportional: bool  # If true, match trader's % instead of fixed amount
    exit_fee_reserve: float  # SOL reserved per open position for exit fees
    
    # Position Management
    max_positions: int  # Maximum concurrent positions
    take_profit_pct: float  # Sell when profit reaches this % (safety limit)
    stop_loss_pct: float  # Abandon if loss reaches this % (don't sell, just free slot)
    time_limit_minutes: float  # 0 = disabled (follow trader)
    trailing_stop_pct: float  # 0 = disabled
    rug_abandon_sol: float  # If value < this, abandon position (don't sell, costs more than worth)
    
    # Ops
    log_level: str
    
    @property
    def is_devnet(self) -> bool:
        return self.network == 'devnet'
    
    @property
    def is_mainnet(self) -> bool:
        return self.network == 'mainnet-beta'
    
    @property
    def poll_interval_seconds(self) -> float:
        return self.poll_interval_ms / 1000.0
    
    @property
    def slippage_percent(self) -> float:
        return self.slippage_bps / 100.0
    
    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_config() -> Config:
    """Load configuration from environment variables."""
    load_dotenv()
    
    # Validate required fields
    rpc_url = os.getenv('RPC_URL')
    if not rpc_url:
        raise ValueError("RPC_URL environment variable is required")
    
    wallet_private_key = os.getenv('WALLET_PRIVATE_KEY_BASE58', '')
    if not wallet_private_key:
        raise ValueError("WALLET_PRIVATE_KEY_BASE58 environment variable is required")
    
    return Config(
        # Network
        rpc_url=rpc_url,
        network=os.getenv('NETWORK', 'devnet'),
        
        # Wallet
        wallet_private_key=wallet_private_key,
        wallet_address=os.getenv('WALLET_ADDRESS'),
        
        # Trading
        min_profit_pct=float(os.getenv('MIN_PROFIT_PCT', '0.5')),
        trade_amount_usd=float(os.getenv('TRADE_AMOUNT_USD', '10')),
        trade_balance_pct=float(os.getenv('TRADE_BALANCE_PCT', '80')),  # 80% of balance
        fee_reserve_sol=float(os.getenv('FEE_RESERVE_SOL', '0.05')),  # Reserve 0.05 SOL for fees
        slippage_bps=int(os.getenv('SLIPPAGE_BPS', '50')),
        poll_interval_ms=int(os.getenv('POLL_INTERVAL_MS', '500')),
        
        # Jupiter API
        jupiter_quote_api=os.getenv('JUPITER_QUOTE_API', 'https://quote-api.jup.ag/v6/quote'),
        jupiter_swap_api=os.getenv('JUPITER_SWAP_API', 'https://quote-api.jup.ag/v6/swap'),
        
        # Alerts
        telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN'),
        telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID'),
        
        # Copy Trading
        copy_enabled=os.getenv('COPY_ENABLED', 'false').lower() == 'true',
        copy_wallets=os.getenv('COPY_WALLETS', ''),
        copy_balance_pct=float(os.getenv('COPY_BALANCE_PCT', '50')),  # 50% of balance per copy
        copy_max_sol=float(os.getenv('COPY_MAX_SOL', '0.5')),  # Max 0.5 SOL per trade
        copy_min_sol=float(os.getenv('COPY_MIN_SOL', '0.05')),  # Only copy trades > 0.05 SOL
        copy_poll_interval_ms=int(os.getenv('COPY_POLL_INTERVAL_MS', '1000')),  # Poll every 1 sec (faster!)
        copy_sells=os.getenv('COPY_SELLS', 'true').lower() == 'true',
        copy_proportional=os.getenv('COPY_PROPORTIONAL', 'true').lower() == 'true',  # Match trader's %
        exit_fee_reserve=float(os.getenv('EXIT_FEE_RESERVE', '0.001')),  # 0.001 SOL per position for exit fees
        
        # Position Management
        max_positions=int(os.getenv('MAX_POSITIONS', '3')),  # Max 3 positions at once
        take_profit_pct=float(os.getenv('TAKE_PROFIT_PCT', '100')),  # Only for safety (100% = 2x)
        stop_loss_pct=float(os.getenv('STOP_LOSS_PCT', '-95')),  # Abandon if 95% loss (basically rugged)
        time_limit_minutes=float(os.getenv('TIME_LIMIT_MINUTES', '0')),  # 0 = disabled (follow trader)
        trailing_stop_pct=float(os.getenv('TRAILING_STOP_PCT', '0')),  # 0 = disabled
        rug_abandon_sol=float(os.getenv('RUG_ABANDON_SOL', '0.005')),  # If worth < 0.005 SOL, abandon (don't sell)
        
        # Ops
        log_level=os.getenv('LOG_LEVEL', 'INFO'),
    )


# Token definitions for triangular arbitrage
# Format: symbol -> (mint_address, decimals)
TOKENS = {
    'SOL': {
        'mint': 'So11111111111111111111111111111111111111112',
        'decimals': 9,
        'symbol': 'SOL'
    },
    'USDC': {
        'mint': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
        'decimals': 6,
        'symbol': 'USDC'
    },
    'USDT': {
        'mint': 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',
        'decimals': 6,
        'symbol': 'USDT'
    },
    'ETH': {
        'mint': '7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs',  # Wormhole ETH
        'decimals': 8,
        'symbol': 'ETH'
    },
    'BTC': {
        'mint': '3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh',  # Wormhole BTC
        'decimals': 8,
        'symbol': 'BTC'
    },
    'RAY': {
        'mint': '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R',
        'decimals': 6,
        'symbol': 'RAY'
    },
}

# Devnet tokens (different addresses)
TOKENS_DEVNET = {
    'SOL': {
        'mint': 'So11111111111111111111111111111111111111112',
        'decimals': 9,
        'symbol': 'SOL'
    },
    'USDC': {
        'mint': '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU',  # Devnet USDC
        'decimals': 6,
        'symbol': 'USDC'
    },
}

# Default triangular paths to scan
# Each path is a tuple of 3 token symbols: (A, B, C) meaning A -> B -> C -> A
# Single triangle for free Lite API testing
DEFAULT_TRIANGLES = [
    ('SOL', 'USDC', 'USDT'),
]

# Estimated transaction cost in SOL
ESTIMATED_TX_COST_SOL = 0.01

# Rate limiting - very conservative for free Jupiter API
MAX_REQUESTS_PER_SECOND = 0.5  # 1 request per 2 seconds
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
