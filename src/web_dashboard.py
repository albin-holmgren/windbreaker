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

# Wallet configurations for tabs
WALLET_CONFIGS = {
    'real': {
        'name': 'Real',
        'address': 'x1ULQweY5qNirmn9zUhyVM4kS7jRQYe9B2a4eHPXQA6',  # Bot's wallet
        'state_file': 'real_state.json',
        'is_real': True
    },
    'cented': {
        'name': 'Cented',
        'address': 'CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o',
        'state_file': 'mock_state_cented.json'
    },
    'cupsey': {
        'name': 'Cupsey',
        'address': '2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f',
        'state_file': 'mock_state_cupsey.json'
    },
    'jijo': {
        'name': 'Jijo',
        'address': '4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk',
        'state_file': 'mock_state_jijo.json'
    }
}

# HTML template for the dashboard
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Windbreaker</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    colors: {
                        dark: { 100: '#212121', 200: '#181818', 300: '#2a2a2a', 400: '#333333' }
                    }
                }
            }
        }
    </script>
    <style>
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .pulse { animation: pulse 2s infinite; }
        body { background: #181818; }
        .card { background: #212121; border: 1px solid #2a2a2a; }
        .tab-active { background: #212121; color: #fff; }
        .tab-inactive { background: transparent; color: #666; }
        .wallet-tab { transition: all 0.2s; border-bottom: 2px solid transparent; }
        .wallet-tab.active { border-bottom-color: #4ade80; color: #fff; }
        .wallet-tab:hover { color: #fff; }
    </style>
</head>
<body class="text-white min-h-screen font-sans">
    <div class="max-w-7xl mx-auto px-6 py-8">
        <!-- Wallet Tabs -->
        <div class="flex items-center gap-1 mb-6 border-b border-neutral-800 pb-0">
            <button onclick="switchWallet('real')" id="wallet-real" class="wallet-tab active px-4 py-3 text-sm font-medium text-green-400 border border-green-500/30 rounded-t bg-green-500/10">
                Real
            </button>
            <button onclick="switchWallet('cented')" id="wallet-cented" class="wallet-tab px-4 py-3 text-sm font-medium text-neutral-400">
                Cented
            </button>
            <button onclick="switchWallet('cupsey')" id="wallet-cupsey" class="wallet-tab px-4 py-3 text-sm font-medium text-neutral-400">
                Cupsey
            </button>
            <button onclick="switchWallet('jijo')" id="wallet-jijo" class="wallet-tab px-4 py-3 text-sm font-medium text-neutral-400">
                Jijo
            </button>
        </div>

        <!-- Header -->
        <div class="flex items-center justify-between mb-10">
            <div>
                <h1 class="text-2xl font-semibold tracking-tight">Windbreaker</h1>
                <p class="text-neutral-500 text-sm mt-1">Copy Trading</p>
            </div>
            <div class="flex items-center gap-4">
                <button onclick="refreshData()" id="refreshBtn" class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-sm transition-all">
                    <svg id="refreshIcon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
                    </svg>
                    Refresh
                </button>
                <span id="lastUpdate" class="text-neutral-600 text-xs"></span>
            </div>
        </div>

        <!-- Stats Cards -->
        <div class="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-10">
            <div class="card rounded-2xl p-5">
                <p class="text-neutral-500 text-xs uppercase tracking-wider mb-2">Balance</p>
                <p id="balance" class="text-2xl font-medium">0.00 SOL</p>
                <p id="balanceChange" class="text-neutral-600 text-xs mt-1">from 1.00 SOL</p>
            </div>
            
            <div class="card rounded-2xl p-5">
                <p class="text-neutral-500 text-xs uppercase tracking-wider mb-2">Total Portfolio</p>
                <p id="totalPortfolio" class="text-2xl font-medium">0.00 SOL</p>
                <p id="totalInvested" class="text-neutral-600 text-xs mt-1">0.00 invested</p>
            </div>
            
            <div class="card rounded-2xl p-5">
                <p class="text-neutral-500 text-xs uppercase tracking-wider mb-2">Total PnL</p>
                <p id="totalPnl" class="text-2xl font-medium">+0.00 SOL</p>
                <p id="returnPct" class="text-neutral-600 text-xs mt-1">0% return</p>
            </div>
            
            <div class="card rounded-2xl p-5">
                <p class="text-neutral-500 text-xs uppercase tracking-wider mb-2">Positions</p>
                <p id="openPositions" class="text-2xl font-medium">0</p>
                <p class="text-neutral-600 text-xs mt-1">active</p>
            </div>
            
            <div class="card rounded-2xl p-5">
                <p class="text-neutral-500 text-xs uppercase tracking-wider mb-2">Trades</p>
                <p id="totalTrades" class="text-2xl font-medium">0</p>
                <p id="tradeBreakdown" class="text-neutral-600 text-xs mt-1">0 buys, 0 sells</p>
            </div>
        </div>

        <!-- Trades Section with Tabs -->
        <div class="card rounded-2xl overflow-hidden">
            <!-- Tab Header -->
            <div class="flex border-b border-neutral-800">
                <button id="tabOpen" onclick="switchTab('open')" class="tab-active px-6 py-4 text-sm font-medium transition-all">
                    Open Positions
                </button>
                <button id="tabClosed" onclick="switchTab('closed')" class="tab-inactive px-6 py-4 text-sm font-medium transition-all">
                    Closed Trades
                </button>
            </div>
            
            <!-- Open Positions Tab -->
            <div id="openContent" class="block">
                <div class="overflow-x-auto">
                    <table class="w-full">
                        <thead class="text-left text-neutral-500 text-xs uppercase tracking-wider border-b border-neutral-800">
                            <tr>
                                <th class="px-6 py-4">Token</th>
                                <th class="px-6 py-4">Entry</th>
                                <th class="px-6 py-4">Tokens</th>
                                <th class="px-6 py-4">Opened</th>
                            </tr>
                        </thead>
                        <tbody id="openPositionsBody" class="text-sm">
                            <tr><td colspan="4" class="px-6 py-8 text-center text-neutral-600">No open positions</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <!-- Closed Trades Tab -->
            <div id="closedContent" class="hidden">
                <div class="overflow-x-auto">
                    <table class="w-full">
                        <thead class="text-left text-neutral-500 text-xs uppercase tracking-wider border-b border-neutral-800">
                            <tr>
                                <th class="px-6 py-4">Token</th>
                                <th class="px-6 py-4">Type</th>
                                <th class="px-6 py-4">Entry</th>
                                <th class="px-6 py-4">Received</th>
                                <th class="px-6 py-4">PnL</th>
                                <th class="px-6 py-4">Held</th>
                                <th class="px-6 py-4">Closed</th>
                            </tr>
                        </thead>
                        <tbody id="closedTradesBody" class="text-sm">
                            <tr><td colspan="7" class="px-6 py-8 text-center text-neutral-600">No closed trades</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tracked Wallet -->
        <div class="mt-6 flex items-center gap-2 text-neutral-600 text-xs">
            <span>Tracking:</span>
            <code id="trackedWallet" class="text-neutral-400 font-mono">Loading...</code>
        </div>
    </div>

    <script>
        let currentData = null;
        let isRefreshing = false;
        let currentWallet = 'real';
        
        const walletConfigs = {
            'real': { name: 'Real', address: 'x1ULQweY5qNirmn9zUhyVM4kS7jRQYe9B2a4eHPXQA6', isReal: true },
            'cented': { name: 'Cented', address: 'CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o' },
            'cupsey': { name: 'Cupsey', address: '2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f' },
            'jijo': { name: 'Jijo', address: '4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk' }
        };
        
        function switchWallet(wallet) {
            currentWallet = wallet;
            
            // Update tab styles
            document.querySelectorAll('.wallet-tab').forEach(tab => {
                tab.classList.remove('active');
            });
            document.getElementById('wallet-' + wallet).classList.add('active');
            
            // Fetch data for new wallet
            fetchData();
        }
        
        function switchTab(tab) {
            const openTab = document.getElementById('tabOpen');
            const closedTab = document.getElementById('tabClosed');
            const openContent = document.getElementById('openContent');
            const closedContent = document.getElementById('closedContent');
            
            if (tab === 'open') {
                openTab.className = 'tab-active px-6 py-4 text-sm font-medium transition-all';
                closedTab.className = 'tab-inactive px-6 py-4 text-sm font-medium transition-all';
                openContent.className = 'block';
                closedContent.className = 'hidden';
            } else {
                openTab.className = 'tab-inactive px-6 py-4 text-sm font-medium transition-all';
                closedTab.className = 'tab-active px-6 py-4 text-sm font-medium transition-all';
                openContent.className = 'hidden';
                closedContent.className = 'block';
            }
        }
        
        async function refreshData() {
            if (isRefreshing) return;
            isRefreshing = true;
            
            const btn = document.getElementById('refreshBtn');
            const icon = document.getElementById('refreshIcon');
            btn.disabled = true;
            icon.classList.add('animate-spin');
            
            await fetchData();
            
            setTimeout(() => {
                isRefreshing = false;
                btn.disabled = false;
                icon.classList.remove('animate-spin');
            }, 500);
        }
        
        async function fetchData() {
            try {
                const response = await fetch('/api/stats?wallet=' + currentWallet);
                currentData = await response.json();
                if (currentData.error) {
                    console.error('API error:', currentData.error);
                    // Use defaults on error
                    currentData = {
                        balance: 0, starting_balance: 0, open_positions: 0,
                        buys: 0, sells: 0, positions: {}, entry_sol: {}, entry_times: {},
                        trades: [], tracked_wallet: walletConfigs[currentWallet]?.address || ''
                    };
                }
                updateUI(currentData);
            } catch (e) {
                console.error('Failed to fetch data:', e);
            }
        }
        
        function formatTime(timestamp) {
            if (!timestamp) return '-';
            const date = new Date(timestamp);
            const now = new Date();
            const diff = now - date;
            
            if (diff < 60000) return 'Just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
            return Math.floor(diff / 86400000) + 'd ago';
        }
        
        function updateUI(data) {
            // Calculate total invested and portfolio value
            const entrySol = data.entry_sol || {};
            const totalInvestedAmount = Object.values(entrySol).reduce((sum, val) => sum + val, 0);
            const totalPortfolioValue = data.balance + totalInvestedAmount;
            const totalPnl = totalPortfolioValue - data.starting_balance;
            
            // Update stats
            document.getElementById('balance').textContent = data.balance.toFixed(4) + ' SOL';
            document.getElementById('balanceChange').textContent = 'from ' + data.starting_balance.toFixed(2) + ' SOL';
            
            document.getElementById('totalPortfolio').textContent = totalPortfolioValue.toFixed(4) + ' SOL';
            document.getElementById('totalInvested').textContent = totalInvestedAmount.toFixed(4) + ' invested';
            
            document.getElementById('totalPnl').textContent = (totalPnl >= 0 ? '+' : '') + totalPnl.toFixed(4) + ' SOL';
            
            const returnPct = (totalPnl / data.starting_balance) * 100;
            document.getElementById('returnPct').textContent = (returnPct >= 0 ? '+' : '') + returnPct.toFixed(1) + '% return';
            
            document.getElementById('openPositions').textContent = data.open_positions || 0;
            // A completed trade = buy + sell, so count sells as completed trades
            const completedTrades = data.sells || 0;
            const openTrades = data.open_positions || 0;
            document.getElementById('totalTrades').textContent = completedTrades;
            document.getElementById('tradeBreakdown').textContent = completedTrades + ' closed, ' + openTrades + ' open';
            
            if (data.tracked_wallet) {
                document.getElementById('trackedWallet').textContent = data.tracked_wallet;
            }
            
            // Update open positions with entry data
            const positions = data.positions || {};
            const entryTimes = data.entry_times || {};
            
            const openPositions = Object.entries(positions).filter(([_, amt]) => amt > 0);
            
            if (openPositions.length > 0) {
                let totalInvested = 0;
                const posHtml = openPositions.map(([mint, amt]) => {
                    const entry = entrySol[mint] || 0;
                    totalInvested += entry;
                    const entryTime = entryTimes[mint] ? new Date(entryTimes[mint] * 1000).toISOString() : null;
                    const shortMint = mint.slice(0, 8);
                    
                    return `
                        <tr class="border-b border-neutral-800/50 hover:bg-neutral-800/30 transition-colors">
                            <td class="px-6 py-4">
                                <a href="https://pump.fun/${mint}" target="_blank" class="font-mono text-neutral-300 hover:text-white transition-colors">${shortMint}</a>
                            </td>
                            <td class="px-6 py-4 text-neutral-400">${entry.toFixed(4)} SOL</td>
                            <td class="px-6 py-4 text-neutral-500">${Number(amt).toLocaleString()}</td>
                            <td class="px-6 py-4 text-neutral-500">${formatTime(entryTime)}</td>
                        </tr>
                    `;
                }).join('');
                
                // Add total row
                const totalRow = `
                    <tr class="border-t border-neutral-700 bg-neutral-800/50">
                        <td class="px-6 py-4 text-neutral-400 font-medium">Total Invested</td>
                        <td class="px-6 py-4 text-white font-medium">${totalInvested.toFixed(4)} SOL</td>
                        <td class="px-6 py-4"></td>
                        <td class="px-6 py-4"></td>
                    </tr>
                `;
                document.getElementById('openPositionsBody').innerHTML = posHtml + totalRow;
            } else {
                document.getElementById('openPositionsBody').innerHTML = '<tr><td colspan="4" class="px-6 py-8 text-center text-neutral-600">No open positions</td></tr>';
            }
            
            // Update closed trades (sells and auto_sells)
            const trades = data.trades || [];
            const closedTrades = trades.filter(t => t.type === 'sell' || t.type === 'auto_sell' || t.trade_type === 'sell');
            
            if (closedTrades.length > 0) {
                const closedHtml = closedTrades.slice(-20).reverse().map(t => {
                    const token = t.token || t.token_mint?.slice(0, 8) || 'Unknown';
                    const fullMint = t.full_mint || '';
                    const solReceived = t.sol || t.sol_amount || 0;
                    const entrySol = t.entry_sol || 0;
                    const pnl = t.pnl;
                    const holdMins = t.hold_minutes;
                    
                    // Format hold duration
                    let holdStr = '-';
                    if (holdMins !== null && holdMins !== undefined) {
                        if (holdMins < 1) holdStr = Math.floor(holdMins * 60) + 's';
                        else if (holdMins < 60) holdStr = Math.floor(holdMins) + 'm';
                        else if (holdMins < 1440) holdStr = (holdMins / 60).toFixed(1) + 'h';
                        else holdStr = (holdMins / 1440).toFixed(1) + 'd';
                    }
                    
                    // Format closed ago time
                    let closedStr = '-';
                    if (t.timestamp) {
                        const closedDate = new Date(t.timestamp);
                        const now = new Date();
                        const diffMs = now - closedDate;
                        const diffMins = diffMs / 60000;
                        if (diffMins < 1) closedStr = Math.floor(diffMs / 1000) + 's ago';
                        else if (diffMins < 60) closedStr = Math.floor(diffMins) + 'm ago';
                        else if (diffMins < 1440) closedStr = (diffMins / 60).toFixed(1) + 'h ago';
                        else closedStr = (diffMins / 1440).toFixed(1) + 'd ago';
                    }
                    
                    const pnlClass = pnl > 0 ? 'text-green-400' : pnl < 0 ? 'text-red-400' : 'text-neutral-300';
                    
                    return `
                        <tr class="border-b border-neutral-800/50 hover:bg-neutral-800/30 transition-colors">
                            <td class="px-6 py-4">
                                <a href="https://pump.fun/${fullMint}" target="_blank" class="font-mono text-neutral-300 hover:text-white transition-colors">${token}</a>
                            </td>
                            <td class="px-6 py-4">
                                <span class="text-neutral-400">SELL</span>
                            </td>
                            <td class="px-6 py-4 text-neutral-400">${Number(entrySol).toFixed(4)}</td>
                            <td class="px-6 py-4 text-neutral-400">${Number(solReceived).toFixed(4)}</td>
                            <td class="px-6 py-4 ${pnlClass}">
                                ${pnl !== null && pnl !== undefined ? (pnl >= 0 ? '+' : '') + pnl.toFixed(4) : '-'}
                            </td>
                            <td class="px-6 py-4 text-neutral-500">${holdStr}</td>
                            <td class="px-6 py-4 text-neutral-500">${closedStr}</td>
                        </tr>
                    `;
                }).join('');
                document.getElementById('closedTradesBody').innerHTML = closedHtml;
            } else {
                document.getElementById('closedTradesBody').innerHTML = '<tr><td colspan="7" class="px-6 py-8 text-center text-neutral-600">No closed trades</td></tr>';
            }
            
            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
        }
        
        // Initial fetch and auto-refresh every 30 seconds (for new trades)
        fetchData();
        setInterval(fetchData, 30000);
    </script>
</body>
</html>
'''


class WebDashboard:
    """Web dashboard server for monitoring the bot."""
    
    def __init__(self, db=None, state_file: str = 'mock_state.json', rpc_client=None, wallet_keypair=None):
        self.db = db
        self.state_file = state_file
        self.rpc_client = rpc_client
        self.wallet_keypair = wallet_keypair
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
            # Get wallet from query param, default to real
            wallet_id = request.query.get('wallet', 'real')
            wallet_config = WALLET_CONFIGS.get(wallet_id, WALLET_CONFIGS['real'])
            is_real_wallet = wallet_config.get('is_real', False)
            
            if self.db:
                stats = await self.db.get_stats()
                state = await self.db.get_state()
                stats['positions'] = state.get('positions', {})
                stats['trades'] = state.get('trades_history', [])[-20:]
                stats['tracked_wallet'] = wallet_config['address']
            else:
                stats = self._load_json_stats(wallet_config['state_file'])
                stats['tracked_wallet'] = wallet_config['address']
                
                # For real wallet, fetch actual on-chain balance
                if is_real_wallet and self.rpc_client and self.wallet_keypair:
                    try:
                        balance_resp = await self.rpc_client.get_balance(self.wallet_keypair.pubkey())
                        balance_lamports = balance_resp.value
                        balance_sol = balance_lamports / 1e9
                        stats['balance'] = balance_sol
                        stats['starting_balance'] = balance_sol  # For real wallet, current balance is the starting point
                    except Exception as e:
                        logger.warning("failed_to_fetch_real_balance", error=str(e))
            
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
    
    def _load_json_state(self, state_file: str = None) -> Dict[str, Any]:
        """Load state from JSON file."""
        file_to_load = state_file or self.state_file
        try:
            with open(file_to_load, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # Return defaults - use 0 for real wallet, 1 for mock wallets
            is_real = 'real' in (file_to_load or '')
            default_balance = 0.0 if is_real else 1.0
            return {'starting_balance': default_balance, 'balance': default_balance, 'positions': {}, 'trades_history': [], 'entry_sol': {}, 'entry_times': {}}
    
    def _load_json_stats(self, state_file: str = None) -> Dict[str, Any]:
        """Load stats from JSON file."""
        state = self._load_json_state(state_file)
        buys = [t for t in state.get('trades_history', []) if t.get('type') == 'buy']
        sells = [t for t in state.get('trades_history', []) if t.get('type') in ('sell', 'auto_sell')]
        realized_pnl = sum(t.get('pnl', 0) for t in sells if t.get('pnl') is not None)
        
        starting_bal = state.get('starting_balance', 0) or 1.0  # Avoid division by zero
        balance = state.get('balance', 0)
        total_return_pct = ((balance + realized_pnl - starting_bal) / starting_bal * 100) if starting_bal > 0 else 0
        
        return {
            'starting_balance': state.get('starting_balance', 0),
            'balance': balance,
            'open_positions': len([v for v in state.get('positions', {}).values() if v > 0]),
            'buys': len(buys),
            'sells': len(sells),
            'realized_pnl': realized_pnl,
            'total_return_pct': total_return_pct,
            'positions': state.get('positions', {}),
            'entry_sol': state.get('entry_sol', {}),
            'entry_times': state.get('entry_times', {}),
            'trades': state.get('trades_history', [])[-50:],
            'tracked_wallet': os.getenv('COPY_WALLETS', '').split(',')[0] if os.getenv('COPY_WALLETS') else 'Not configured'
        }
    
    def _save_json_state(self, state: Dict[str, Any]):
        """Save state to JSON file."""
        with open(self.state_file, 'w') as f:
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
