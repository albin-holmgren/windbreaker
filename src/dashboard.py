"""
Dashboard - Simple web interface for monitoring mock trading.
"""

import os
import json
import hashlib
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

app = FastAPI(title="Windbreaker Dashboard")

# Add session middleware for auth
app.add_middleware(SessionMiddleware, secret_key=os.urandom(32).hex())

# Password hash (set via environment variable for security)
DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', '#Jagare64!')
STATE_FILE = Path(os.getenv('MOCK_STATE_FILE', '/windbreaker/mock_state.json'))

def check_auth(request: Request) -> bool:
    """Check if user is authenticated."""
    return request.session.get('authenticated', False)

REAL_STATE_FILE = Path('real_trades_state.json')

# Wallet-specific state files (same as copy_trader.py)
WALLET_STATE_FILES = [
    'mock_state_cented.json',
    'mock_state_cupsey.json', 
    'mock_state_jijo.json',
    'mock_state.json',  # Fallback
]

def get_state() -> dict:
    """Load current state from all wallet state files."""
    try:
        # Aggregate state from all wallet files
        combined_state = {
            'balance': 0,
            'starting_balance': 0,
            'pnl': 0,
            'positions': {},
            'entry_sol': {},
            'entry_times': {},
            'trades_history': [],
            'last_updated': 'Never'
        }
        
        files_found = 0
        for state_file in WALLET_STATE_FILES:
            state_path = Path(state_file)
            if state_path.exists():
                try:
                    with open(state_path, 'r') as f:
                        state = json.load(f)
                        files_found += 1
                        # Aggregate balances
                        combined_state['balance'] += state.get('balance', 0)
                        combined_state['starting_balance'] += state.get('starting_balance', 0)
                        # Merge positions
                        combined_state['positions'].update(state.get('positions', {}))
                        combined_state['entry_sol'].update(state.get('entry_sol', {}))
                        combined_state['entry_times'].update(state.get('entry_times', {}))
                        # Append trades
                        combined_state['trades_history'].extend(state.get('trades_history', []))
                        # Use latest update time
                        if state.get('last_updated', '') > combined_state['last_updated']:
                            combined_state['last_updated'] = state.get('last_updated', 'Never')
                except Exception:
                    pass
        
        # Also check the main STATE_FILE path
        if STATE_FILE.exists() and str(STATE_FILE) not in WALLET_STATE_FILES:
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    combined_state['balance'] += state.get('balance', 0)
                    combined_state['starting_balance'] += state.get('starting_balance', 0)
                    combined_state['positions'].update(state.get('positions', {}))
                    combined_state['entry_sol'].update(state.get('entry_sol', {}))
                    combined_state['entry_times'].update(state.get('entry_times', {}))
                    combined_state['trades_history'].extend(state.get('trades_history', []))
            except Exception:
                pass
        
        # Also load real trades and merge them
        if REAL_STATE_FILE.exists():
            try:
                with open(REAL_STATE_FILE, 'r') as f:
                    real_state = json.load(f)
                    real_trades = real_state.get('trades_history', [])
                    combined_state['trades_history'].extend(real_trades)
            except Exception:
                pass
        
        # Sort trades by timestamp and keep last 50
        combined_state['trades_history'].sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        combined_state['trades_history'] = combined_state['trades_history'][:50]
        
        # Calculate PnL
        combined_state['pnl'] = combined_state['balance'] - combined_state['starting_balance']
        
        return combined_state
    except Exception:
        pass
    return {
        'balance': 0,
        'starting_balance': 1,
        'pnl': 0,
        'positions': {},
        'trades_history': [],
        'last_updated': 'Never'
    }

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Windbreaker - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .login-box {{
            background: rgba(255,255,255,0.1);
            padding: 40px;
            border-radius: 16px;
            backdrop-filter: blur(10px);
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }}
        h1 {{ color: #fff; margin-bottom: 30px; text-align: center; }}
        input {{
            width: 100%;
            padding: 12px 16px;
            margin: 10px 0;
            border: none;
            border-radius: 8px;
            background: rgba(255,255,255,0.2);
            color: #fff;
            font-size: 16px;
        }}
        input::placeholder {{ color: rgba(255,255,255,0.5); }}
        button {{
            width: 100%;
            padding: 14px;
            margin-top: 20px;
            border: none;
            border-radius: 8px;
            background: #4CAF50;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background 0.3s;
        }}
        button:hover {{ background: #45a049; }}
        .error {{ color: #ff6b6b; text-align: center; margin-top: 15px; }}
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🌬️ Windbreaker</h1>
        <form method="post" action="/login">
            <input type="password" name="password" placeholder="Enter password" required>
            <button type="submit">Login</button>
        </form>
        {error}
    </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Windbreaker Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #fff;
            padding: 20px;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }}
        .logout {{ color: #ff6b6b; text-decoration: none; }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: rgba(255,255,255,0.1);
            padding: 20px;
            border-radius: 12px;
            backdrop-filter: blur(10px);
        }}
        .stat-label {{ font-size: 14px; color: rgba(255,255,255,0.7); margin-bottom: 8px; }}
        .stat-value {{ font-size: 28px; font-weight: bold; }}
        .stat-value.positive {{ color: #4CAF50; }}
        .stat-value.negative {{ color: #ff6b6b; }}
        
        .section {{
            background: rgba(255,255,255,0.1);
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 20px;
        }}
        .section h2 {{ margin-bottom: 15px; font-size: 18px; }}
        
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.1); }}
        th {{ color: rgba(255,255,255,0.7); font-weight: 500; }}
        
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
        }}
        .badge.buy {{ background: #4CAF50; }}
        .badge.sell {{ background: #2196F3; }}
        .badge.real {{ background: #FF9800; margin-left: 4px; }}
        .badge.mock {{ background: #9E9E9E; margin-left: 4px; }}
        
        .updated {{ font-size: 12px; color: rgba(255,255,255,0.5); margin-top: 20px; }}
        
        @media (max-width: 600px) {{
            .stat-value {{ font-size: 22px; }}
            table {{ font-size: 14px; }}
            th, td {{ padding: 8px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🌬️ Windbreaker Dashboard</h1>
            <a href="/logout" class="logout">Logout</a>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Current Balance</div>
                <div class="stat-value">{balance:.4f} SOL</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Starting Balance</div>
                <div class="stat-value">{starting_balance:.4f} SOL</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">P&L</div>
                <div class="stat-value {pnl_class}">{pnl:+.4f} SOL</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">P&L %</div>
                <div class="stat-value {pnl_class}">{pnl_pct:+.2f}%</div>
            </div>
        </div>
        
        <div class="section">
            <h2>📊 Open Positions ({position_count})</h2>
            <table>
                <thead>
                    <tr>
                        <th>Token</th>
                        <th>Entry SOL</th>
                        <th>Age (min)</th>
                    </tr>
                </thead>
                <tbody>
                    {positions_rows}
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2>📜 Recent Trades</h2>
            <table>
                <thead>
                    <tr>
                        <th>Type</th>
                        <th>Token</th>
                        <th>SOL</th>
                        <th>P&L</th>
                        <th>Time</th>
                    </tr>
                </thead>
                <tbody>
                    {trades_rows}
                </tbody>
            </table>
        </div>
        
        <div class="updated">Last updated: {last_updated} (auto-refreshes every 30s)</div>
    </div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=302)
    
    state = get_state()
    
    # Calculate values
    balance = state.get('balance', 0)
    starting = state.get('starting_balance', 1)
    pnl = balance - starting
    pnl_pct = (pnl / starting * 100) if starting > 0 else 0
    pnl_class = 'positive' if pnl >= 0 else 'negative'
    
    # Build positions table
    positions = state.get('positions', {})
    entry_times = state.get('entry_times', {})
    entry_sol = state.get('entry_sol', {})
    
    positions_rows = ""
    position_count = 0
    for mint, tokens in positions.items():
        if tokens and tokens > 0:
            position_count += 1
            e_sol = entry_sol.get(mint, 0)
            e_time = entry_times.get(mint, 0)
            age = (datetime.now().timestamp() - e_time) / 60 if e_time else 0
            positions_rows += f"<tr><td>{mint[:8]}...</td><td>{e_sol:.4f}</td><td>{age:.1f}</td></tr>"
    
    if not positions_rows:
        positions_rows = "<tr><td colspan='3' style='text-align:center;color:rgba(255,255,255,0.5)'>No open positions</td></tr>"
    
    # Build trades table
    trades = state.get('trades_history', [])[:20]  # Already sorted, take first 20
    trades_rows = ""
    for t in trades:
        trade_type = t.get('type', 'unknown')
        badge_class = 'buy' if trade_type == 'buy' else 'sell'
        is_real = t.get('real', False)
        mode_badge = '<span class="badge real">REAL</span>' if is_real else '<span class="badge mock">MOCK</span>'
        token = t.get('token', '?')
        sol = t.get('sol', 0)
        trade_pnl = t.get('pnl', 0) if trade_type == 'sell' else '-'
        pnl_display = f"{trade_pnl:+.4f}" if isinstance(trade_pnl, (int, float)) else trade_pnl
        timestamp = t.get('timestamp', '')[:19].replace('T', ' ')
        trades_rows += f"""<tr>
            <td><span class="badge {badge_class}">{trade_type.upper()}</span>{mode_badge}</td>
            <td>{token}</td>
            <td>{sol:.4f}</td>
            <td>{pnl_display}</td>
            <td>{timestamp}</td>
        </tr>"""
    
    if not trades_rows:
        trades_rows = "<tr><td colspan='5' style='text-align:center;color:rgba(255,255,255,0.5)'>No trades yet</td></tr>"
    
    return HTMLResponse(DASHBOARD_HTML.format(
        balance=balance,
        starting_balance=starting,
        pnl=pnl,
        pnl_pct=pnl_pct,
        pnl_class=pnl_class,
        position_count=position_count,
        positions_rows=positions_rows,
        trades_rows=trades_rows,
        last_updated=state.get('last_updated', 'Never')
    ))

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML.format(error=""))

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == DASHBOARD_PASSWORD:
        request.session['authenticated'] = True
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(LOGIN_HTML.format(error='<p class="error">Invalid password</p>'))

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

@app.get("/api/state")
async def api_state(request: Request):
    """API endpoint for getting current state (requires auth)."""
    if not check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return get_state()

@app.post("/api/reset")
async def api_reset(request: Request):
    """Reset all wallet state files to fresh 1 SOL balance."""
    if not check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    fresh_state = {
        "starting_balance": 1.0,
        "balance": 1.0,
        "positions": {},
        "entry_times": {},
        "entry_sol": {},
        "trades_history": [],
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "pnl": 0
    }
    
    reset_count = 0
    for state_file in WALLET_STATE_FILES:
        try:
            with open(state_file, 'w') as f:
                json.dump(fresh_state, f, indent=2)
            reset_count += 1
        except Exception:
            pass
    
    return {"success": True, "reset_count": reset_count, "message": f"Reset {reset_count} wallet state files to 1 SOL"}

def run_dashboard(host: str = "0.0.0.0", port: int = 8080):
    """Run the dashboard server."""
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    run_dashboard()
