"""
Database module for persistent trade storage.
Uses PostgreSQL when available, falls back to SQLite/JSON for local development.
"""

import os
import json
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
import structlog

logger = structlog.get_logger()


@dataclass
class TradeRecord:
    """A single trade record."""
    id: Optional[int]
    trade_type: str  # 'buy' or 'sell'
    token_mint: str
    token_symbol: str
    sol_amount: float
    token_amount: float
    balance_after: float
    pnl: Optional[float]  # Only for sells
    entry_sol: Optional[float]  # Only for sells
    timestamp: str
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PositionRecord:
    """An open position record."""
    token_mint: str
    token_symbol: str
    token_amount: float
    entry_sol: float
    entry_timestamp: str
    current_value_sol: Optional[float] = None
    pnl_pct: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Database:
    """Database abstraction for trade storage."""
    
    def __init__(self):
        self.database_url = os.getenv('DATABASE_URL')
        self.pool = None
        self._initialized = False
        
    async def initialize(self):
        """Initialize database connection."""
        if self._initialized:
            return
            
        if self.database_url and self.database_url.startswith('postgres'):
            try:
                import asyncpg
                # Railway uses postgres:// but asyncpg needs postgresql://
                db_url = self.database_url.replace('postgres://', 'postgresql://')
                self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
                await self._create_tables()
                logger.info("database_connected", type="postgresql")
                self._initialized = True
            except Exception as e:
                logger.warning("database_connection_failed", error=str(e), fallback="json")
                self.pool = None
        else:
            logger.info("database_mode", type="json_file")
            
        self._initialized = True
    
    async def _create_tables(self):
        """Create database tables if they don't exist."""
        if not self.pool:
            return
            
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS mock_state (
                    id SERIAL PRIMARY KEY,
                    starting_balance REAL NOT NULL,
                    balance REAL NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    trade_type VARCHAR(10) NOT NULL,
                    token_mint VARCHAR(64) NOT NULL,
                    token_symbol VARCHAR(20) NOT NULL,
                    sol_amount REAL NOT NULL,
                    token_amount REAL NOT NULL,
                    balance_after REAL NOT NULL,
                    pnl REAL,
                    entry_sol REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    id SERIAL PRIMARY KEY,
                    token_mint VARCHAR(64) UNIQUE NOT NULL,
                    token_symbol VARCHAR(20) NOT NULL,
                    token_amount REAL NOT NULL,
                    entry_sol REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Check if we have initial state
            row = await conn.fetchrow('SELECT * FROM mock_state ORDER BY id DESC LIMIT 1')
            if not row:
                await conn.execute(
                    'INSERT INTO mock_state (starting_balance, balance) VALUES ($1, $2)',
                    1.0, 1.0
                )
                logger.info("database_initialized", starting_balance=1.0)
    
    async def get_state(self) -> Dict[str, Any]:
        """Get current mock trading state."""
        if self.pool:
            async with self.pool.acquire() as conn:
                state = await conn.fetchrow('SELECT * FROM mock_state ORDER BY id DESC LIMIT 1')
                trades = await conn.fetch('SELECT * FROM trades ORDER BY created_at DESC')
                positions = await conn.fetch('SELECT * FROM positions WHERE token_amount > 0')
                
                return {
                    'starting_balance': state['starting_balance'] if state else 1.0,
                    'balance': state['balance'] if state else 1.0,
                    'positions': {p['token_mint']: p['token_amount'] for p in positions},
                    'trades_history': [dict(t) for t in trades],
                    'last_updated': state['updated_at'].isoformat() if state else datetime.utcnow().isoformat()
                }
        else:
            # Fall back to JSON file
            return self._load_json_state()
    
    async def update_balance(self, balance: float):
        """Update the current balance."""
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'UPDATE mock_state SET balance = $1, updated_at = CURRENT_TIMESTAMP WHERE id = (SELECT MAX(id) FROM mock_state)',
                    balance
                )
        else:
            state = self._load_json_state()
            state['balance'] = balance
            state['last_updated'] = datetime.utcnow().isoformat()
            self._save_json_state(state)
    
    async def record_trade(self, trade: TradeRecord):
        """Record a trade."""
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO trades (trade_type, token_mint, token_symbol, sol_amount, token_amount, balance_after, pnl, entry_sol)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ''', trade.trade_type, trade.token_mint, trade.token_symbol, 
                    trade.sol_amount, trade.token_amount, trade.balance_after, 
                    trade.pnl, trade.entry_sol)
        else:
            state = self._load_json_state()
            state['trades_history'].append({
                'type': trade.trade_type,
                'token': trade.token_symbol,
                'full_mint': trade.token_mint,
                'sol': trade.sol_amount,
                'tokens': trade.token_amount,
                'balance_after': trade.balance_after,
                'pnl': trade.pnl,
                'entry_sol': trade.entry_sol,
                'timestamp': trade.timestamp
            })
            self._save_json_state(state)
    
    async def update_position(self, token_mint: str, token_symbol: str, amount: float, entry_sol: float = 0):
        """Update or create a position."""
        if self.pool:
            async with self.pool.acquire() as conn:
                if amount > 0:
                    await conn.execute('''
                        INSERT INTO positions (token_mint, token_symbol, token_amount, entry_sol)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (token_mint) DO UPDATE SET 
                            token_amount = positions.token_amount + $3,
                            entry_sol = positions.entry_sol + $4,
                            updated_at = CURRENT_TIMESTAMP
                    ''', token_mint, token_symbol, amount, entry_sol)
                else:
                    await conn.execute(
                        'DELETE FROM positions WHERE token_mint = $1',
                        token_mint
                    )
        else:
            state = self._load_json_state()
            if amount > 0:
                current = state['positions'].get(token_mint, 0)
                state['positions'][token_mint] = current + amount
            else:
                state['positions'].pop(token_mint, None)
            self._save_json_state(state)
    
    async def get_position(self, token_mint: str) -> Optional[float]:
        """Get position amount for a token."""
        if self.pool:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT token_amount FROM positions WHERE token_mint = $1',
                    token_mint
                )
                return row['token_amount'] if row else None
        else:
            state = self._load_json_state()
            return state['positions'].get(token_mint)
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get trading statistics."""
        if self.pool:
            async with self.pool.acquire() as conn:
                state = await conn.fetchrow('SELECT * FROM mock_state ORDER BY id DESC LIMIT 1')
                
                buy_count = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE trade_type = 'buy'")
                sell_count = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE trade_type = 'sell'")
                realized_pnl = await conn.fetchval("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE trade_type = 'sell'")
                position_count = await conn.fetchval("SELECT COUNT(*) FROM positions WHERE token_amount > 0")
                
                return {
                    'starting_balance': state['starting_balance'] if state else 1.0,
                    'balance': state['balance'] if state else 1.0,
                    'open_positions': position_count,
                    'buys': buy_count,
                    'sells': sell_count,
                    'realized_pnl': realized_pnl,
                    'total_return_pct': ((state['balance'] + realized_pnl - state['starting_balance']) / state['starting_balance'] * 100) if state else 0
                }
        else:
            state = self._load_json_state()
            buys = [t for t in state.get('trades_history', []) if t.get('type') == 'buy']
            sells = [t for t in state.get('trades_history', []) if t.get('type') == 'sell']
            realized_pnl = sum(t.get('pnl', 0) for t in sells)
            
            return {
                'starting_balance': state.get('starting_balance', 1.0),
                'balance': state.get('balance', 1.0),
                'open_positions': len([v for v in state.get('positions', {}).values() if v > 0]),
                'buys': len(buys),
                'sells': len(sells),
                'realized_pnl': realized_pnl,
                'total_return_pct': ((state.get('balance', 1.0) + realized_pnl - state.get('starting_balance', 1.0)) / state.get('starting_balance', 1.0) * 100)
            }
    
    async def reset_state(self, starting_balance: float = 1.0):
        """Reset to fresh state."""
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute('DELETE FROM trades')
                await conn.execute('DELETE FROM positions')
                await conn.execute('DELETE FROM mock_state')
                await conn.execute(
                    'INSERT INTO mock_state (starting_balance, balance) VALUES ($1, $2)',
                    starting_balance, starting_balance
                )
        else:
            self._save_json_state({
                'starting_balance': starting_balance,
                'balance': starting_balance,
                'positions': {},
                'trades_history': [],
                'last_updated': datetime.utcnow().isoformat()
            })
        logger.info("state_reset", starting_balance=starting_balance)
    
    def _load_json_state(self) -> Dict[str, Any]:
        """Load state from JSON file."""
        try:
            with open('mock_state.json', 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                'starting_balance': 1.0,
                'balance': 1.0,
                'positions': {},
                'trades_history': [],
                'last_updated': datetime.utcnow().isoformat()
            }
    
    def _save_json_state(self, state: Dict[str, Any]):
        """Save state to JSON file."""
        with open('mock_state.json', 'w') as f:
            json.dump(state, f, indent=2)
    
    async def close(self):
        """Close database connection."""
        if self.pool:
            await self.pool.close()


# Global database instance
db = Database()
