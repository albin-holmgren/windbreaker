"""
Web Dashboard for monitoring the copy trading bot.
Provides a real-time view of trades, positions, and performance.
"""

import os
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, List
from aiohttp import web
import structlog

logger = structlog.get_logger()

# HTML template for the dashboard
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Windbreaker - Copy Trading Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .animate-pulse-slow { animation: pulse 2s infinite; }
        .gradient-bg { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <div class="container mx-auto px-4 py-8">
        <!-- Header -->
        <div class="flex items-center justify-between mb-8">
            <div>
                <h1 class="text-3xl font-bold bg-gradient-to-r from-purple-400 to-pink-500 bg-clip-text text-transparent">
                    🌊 Windbreaker
                </h1>
                <p class="text-gray-400 mt-1">Copy Trading Dashboard</p>
            </div>
            <div class="flex items-center gap-4">
                <span id="status" class="flex items-center gap-2 px-3 py-1 rounded-full bg-green-500/20 text-green-400">
                    <span class="w-2 h-2 bg-green-400 rounded-full animate-pulse-slow"></span>
                    Live
                </span>
                <span id="lastUpdate" class="text-gray-500 text-sm"></span>
            </div>
        </div>

        <!-- Stats Cards -->
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400">Balance</span>
                    <i data-lucide="wallet" class="w-5 h-5 text-purple-400"></i>
                </div>
                <p id="balance" class="text-2xl font-bold mt-2">0.00 SOL</p>
                <p id="balanceChange" class="text-sm text-gray-500 mt-1">from 1.00 SOL start</p>
            </div>
            
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400">Realized PnL</span>
                    <i data-lucide="trending-up" class="w-5 h-5 text-green-400"></i>
                </div>
                <p id="realizedPnl" class="text-2xl font-bold mt-2 text-green-400">+0.00 SOL</p>
                <p id="returnPct" class="text-sm text-gray-500 mt-1">0% return</p>
            </div>
            
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400">Open Positions</span>
                    <i data-lucide="layers" class="w-5 h-5 text-blue-400"></i>
                </div>
                <p id="openPositions" class="text-2xl font-bold mt-2">0</p>
                <p class="text-sm text-gray-500 mt-1">active tokens</p>
            </div>
            
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400">Total Trades</span>
                    <i data-lucide="activity" class="w-5 h-5 text-yellow-400"></i>
                </div>
                <p id="totalTrades" class="text-2xl font-bold mt-2">0</p>
                <p id="tradeBreakdown" class="text-sm text-gray-500 mt-1">0 buys, 0 sells</p>
            </div>
        </div>

        <!-- Main Content Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <!-- Open Positions -->
            <div class="lg:col-span-1 bg-gray-800 rounded-xl border border-gray-700">
                <div class="p-4 border-b border-gray-700">
                    <h2 class="text-lg font-semibold flex items-center gap-2">
                        <i data-lucide="briefcase" class="w-5 h-5 text-blue-400"></i>
                        Open Positions
                    </h2>
                </div>
                <div id="positions" class="p-4 space-y-3 max-h-96 overflow-y-auto">
                    <p class="text-gray-500 text-center py-4">No open positions</p>
                </div>
            </div>

            <!-- Recent Trades -->
            <div class="lg:col-span-2 bg-gray-800 rounded-xl border border-gray-700">
                <div class="p-4 border-b border-gray-700">
                    <h2 class="text-lg font-semibold flex items-center gap-2">
                        <i data-lucide="list" class="w-5 h-5 text-purple-400"></i>
                        Recent Trades
                    </h2>
                </div>
                <div id="trades" class="overflow-x-auto">
                    <table class="w-full">
                        <thead class="text-left text-gray-400 text-sm border-b border-gray-700">
                            <tr>
                                <th class="p-4">Type</th>
                                <th class="p-4">Token</th>
                                <th class="p-4">SOL</th>
                                <th class="p-4">PnL</th>
                                <th class="p-4">Time</th>
                            </tr>
                        </thead>
                        <tbody id="tradesBody" class="text-sm">
                            <tr><td colspan="5" class="p-4 text-center text-gray-500">No trades yet</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tracked Wallet -->
        <div class="mt-6 bg-gray-800 rounded-xl p-4 border border-gray-700">
            <div class="flex items-center gap-2 text-gray-400">
                <i data-lucide="eye" class="w-4 h-4"></i>
                <span>Tracking wallet:</span>
                <code id="trackedWallet" class="text-purple-400 font-mono text-sm">Loading...</code>
            </div>
        </div>
    </div>

    <script>
        lucide.createIcons();
        
        async function fetchData() {
            try {
                const response = await fetch('/api/stats');
                const data = await response.json();
                updateUI(data);
            } catch (e) {
                console.error('Failed to fetch data:', e);
            }
        }
        
        function updateUI(data) {
            // Update stats
            document.getElementById('balance').textContent = data.balance.toFixed(4) + ' SOL';
            document.getElementById('balanceChange').textContent = 'from ' + data.starting_balance.toFixed(2) + ' SOL start';
            
            const pnl = data.realized_pnl || 0;
            const pnlEl = document.getElementById('realizedPnl');
            pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4) + ' SOL';
            pnlEl.className = 'text-2xl font-bold mt-2 ' + (pnl >= 0 ? 'text-green-400' : 'text-red-400');
            
            const returnPct = data.total_return_pct || 0;
            document.getElementById('returnPct').textContent = returnPct.toFixed(1) + '% return';
            
            document.getElementById('openPositions').textContent = data.open_positions || 0;
            document.getElementById('totalTrades').textContent = (data.buys || 0) + (data.sells || 0);
            document.getElementById('tradeBreakdown').textContent = (data.buys || 0) + ' buys, ' + (data.sells || 0) + ' sells';
            
            if (data.tracked_wallet) {
                document.getElementById('trackedWallet').textContent = data.tracked_wallet;
            }
            
            // Update positions
            if (data.positions && Object.keys(data.positions).length > 0) {
                const posHtml = Object.entries(data.positions)
                    .filter(([_, amt]) => amt > 0)
                    .map(([mint, amt]) => `
                        <div class="bg-gray-700/50 rounded-lg p-3">
                            <div class="flex justify-between items-center">
                                <span class="font-mono text-sm text-purple-300">${mint.slice(0, 8)}...</span>
                            </div>
                            <p class="text-xs text-gray-400 mt-1">${Number(amt).toLocaleString()} tokens</p>
                        </div>
                    `).join('');
                document.getElementById('positions').innerHTML = posHtml || '<p class="text-gray-500 text-center py-4">No open positions</p>';
            }
            
            // Update trades
            if (data.trades && data.trades.length > 0) {
                const tradesHtml = data.trades.slice(0, 20).map(t => {
                    const isBuy = t.trade_type === 'buy' || t.type === 'buy';
                    const pnl = t.pnl;
                    const token = t.token_symbol || t.token || t.token_mint?.slice(0, 8) || 'Unknown';
                    const sol = t.sol_amount || t.sol || 0;
                    const time = t.created_at || t.timestamp || '';
                    const timeStr = time ? new Date(time).toLocaleString() : '';
                    
                    return `
                        <tr class="border-b border-gray-700/50 hover:bg-gray-700/30">
                            <td class="p-4">
                                <span class="px-2 py-1 rounded text-xs font-medium ${isBuy ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}">
                                    ${isBuy ? 'BUY' : 'SELL'}
                                </span>
                            </td>
                            <td class="p-4 font-mono text-sm">${token}</td>
                            <td class="p-4">${Number(sol).toFixed(4)}</td>
                            <td class="p-4 ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}">
                                ${pnl !== null && pnl !== undefined ? (pnl >= 0 ? '+' : '') + pnl.toFixed(4) : '-'}
                            </td>
                            <td class="p-4 text-gray-400 text-xs">${timeStr}</td>
                        </tr>
                    `;
                }).join('');
                document.getElementById('tradesBody').innerHTML = tradesHtml;
            }
            
            document.getElementById('lastUpdate').textContent = 'Updated: ' + new Date().toLocaleTimeString();
        }
        
        // Initial fetch and refresh every 5 seconds
        fetchData();
        setInterval(fetchData, 5000);
    </script>
</body>
</html>
'''


class WebDashboard:
    """Web dashboard server for monitoring the bot."""
    
    def __init__(self, db=None):
        self.db = db
        self.app = web.Application()
        self.runner = None
        self._setup_routes()
    
    def _setup_routes(self):
        """Set up web routes."""
        self.app.router.add_get('/', self.handle_dashboard)
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/trades', self.handle_trades)
        self.app.router.add_get('/api/positions', self.handle_positions)
        self.app.router.add_post('/api/reset', self.handle_reset)
        self.app.router.add_post('/api/import', self.handle_import)
        self.app.router.add_get('/health', self.handle_health)
    
    async def handle_dashboard(self, request):
        """Serve the dashboard HTML."""
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')
    
    async def handle_stats(self, request):
        """Get trading statistics."""
        try:
            if self.db:
                stats = await self.db.get_stats()
                state = await self.db.get_state()
                stats['positions'] = state.get('positions', {})
                stats['trades'] = state.get('trades_history', [])[-20:]  # Last 20 trades
                stats['tracked_wallet'] = os.getenv('COPY_WALLETS', '').split(',')[0] if os.getenv('COPY_WALLETS') else 'Not configured'
            else:
                stats = self._load_json_stats()
            
            return web.json_response(stats)
        except Exception as e:
            logger.error("stats_error", error=str(e))
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_trades(self, request):
        """Get trade history."""
        try:
            if self.db:
                state = await self.db.get_state()
                trades = state.get('trades_history', [])
            else:
                state = self._load_json_state()
                trades = state.get('trades_history', [])
            
            return web.json_response({'trades': trades[-50:]})  # Last 50 trades
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_positions(self, request):
        """Get open positions."""
        try:
            if self.db:
                state = await self.db.get_state()
                positions = state.get('positions', {})
            else:
                state = self._load_json_state()
                positions = state.get('positions', {})
            
            # Filter to only open positions
            open_positions = {k: v for k, v in positions.items() if v > 0}
            return web.json_response({'positions': open_positions})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_reset(self, request):
        """Reset trading state."""
        try:
            data = await request.json()
            starting_balance = data.get('starting_balance', 1.0)
            
            if self.db:
                await self.db.reset_state(starting_balance)
            else:
                self._save_json_state({
                    'starting_balance': starting_balance,
                    'balance': starting_balance,
                    'positions': {},
                    'trades_history': [],
                    'last_updated': datetime.utcnow().isoformat()
                })
            
            return web.json_response({'success': True, 'starting_balance': starting_balance})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_import(self, request):
        """Import full state from another instance."""
        try:
            state = await request.json()
            
            # Validate required fields
            if 'balance' not in state or 'positions' not in state:
                return web.json_response({'error': 'Invalid state format'}, status=400)
            
            # Ensure required fields exist
            state.setdefault('starting_balance', 1.0)
            state.setdefault('trades_history', [])
            state['last_updated'] = datetime.utcnow().isoformat()
            
            # Save the imported state
            self._save_json_state(state)
            
            logger.info("state_imported", 
                       balance=state['balance'],
                       positions=len([v for v in state.get('positions', {}).values() if v > 0]),
                       trades=len(state.get('trades_history', [])))
            
            return web.json_response({
                'success': True, 
                'balance': state['balance'],
                'positions': len([v for v in state.get('positions', {}).values() if v > 0]),
                'trades': len(state.get('trades_history', []))
            })
        except Exception as e:
            logger.error("import_error", error=str(e))
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_health(self, request):
        """Health check endpoint."""
        return web.json_response({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})
    
    def _load_json_state(self) -> Dict[str, Any]:
        """Load state from JSON file."""
        try:
            with open('mock_state.json', 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {'starting_balance': 1.0, 'balance': 1.0, 'positions': {}, 'trades_history': []}
    
    def _load_json_stats(self) -> Dict[str, Any]:
        """Load stats from JSON file."""
        state = self._load_json_state()
        buys = [t for t in state.get('trades_history', []) if t.get('type') == 'buy']
        sells = [t for t in state.get('trades_history', []) if t.get('type') == 'sell']
        realized_pnl = sum(t.get('pnl', 0) for t in sells if t.get('pnl'))
        
        return {
            'starting_balance': state.get('starting_balance', 1.0),
            'balance': state.get('balance', 1.0),
            'open_positions': len([v for v in state.get('positions', {}).values() if v > 0]),
            'buys': len(buys),
            'sells': len(sells),
            'realized_pnl': realized_pnl,
            'total_return_pct': ((state.get('balance', 1.0) + realized_pnl - state.get('starting_balance', 1.0)) / state.get('starting_balance', 1.0) * 100),
            'positions': state.get('positions', {}),
            'trades': state.get('trades_history', [])[-20:],
            'tracked_wallet': os.getenv('COPY_WALLETS', '').split(',')[0] if os.getenv('COPY_WALLETS') else 'Not configured'
        }
    
    def _save_json_state(self, state: Dict[str, Any]):
        """Save state to JSON file."""
        with open('mock_state.json', 'w') as f:
            json.dump(state, f, indent=2)
    
    async def start(self, host='0.0.0.0', port=None):
        """Start the web server."""
        if port is None:
            port = int(os.getenv('PORT', 8080))
        
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        logger.info("dashboard_started", host=host, port=port, url=f"http://{host}:{port}")
        return self
    
    async def stop(self):
        """Stop the web server."""
        if self.runner:
            await self.runner.cleanup()


async def run_dashboard(db=None):
    """Run the dashboard as a standalone server."""
    dashboard = WebDashboard(db)
    await dashboard.start()
    
    # Keep running
    while True:
        await asyncio.sleep(3600)
