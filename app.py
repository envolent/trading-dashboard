import os
import sqlite3
import threading
import time
import random
from datetime import datetime
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DB_PATH = os.environ.get('DB_PATH', 'trading.db')
STARTING_BALANCE = 10000.0

WATCHLIST = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 'AMD', 'NFLX', 'SPY']

STRATEGIES = {
    'ultra_safe':       {'interval': 120, 'position_pct': 0.02, 'max_pos': 3,  'threshold': 0.025, 'label': 'Ultra Safe'},
    'safe':             {'interval':  60, 'position_pct': 0.05, 'max_pos': 5,  'threshold': 0.015, 'label': 'Safe'},
    'risky':            {'interval':  30, 'position_pct': 0.10, 'max_pos': 7,  'threshold': 0.008, 'label': 'Risky'},
    'aggressive':       {'interval':  15, 'position_pct': 0.20, 'max_pos': 8,  'threshold': 0.004, 'label': 'Aggressive'},
    'ultra_aggressive': {'interval':   8, 'position_pct': 0.35, 'max_pos': 10, 'threshold': 0.001, 'label': 'Ultra Aggressive'},
}

_price_cache = {}   # symbol -> float  (updated by background thread)
_ma_cache = {}      # symbol -> float
_db_lock = threading.Lock()
_prices_ready = False  # True once first fetch completes


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY,
            balance REAL NOT NULL DEFAULT 10000.0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            shares REAL NOT NULL,
            avg_price REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            shares REAL NOT NULL,
            price REAL NOT NULL,
            total REAL NOT NULL,
            pnl REAL,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            strategy TEXT NOT NULL DEFAULT 'safe',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value REAL NOT NULL,
            timestamp TEXT NOT NULL
        );
    ''')
    now = datetime.utcnow().isoformat()
    conn.execute('INSERT OR IGNORE INTO portfolio (id, balance, updated_at) VALUES (1, ?, ?)', (STARTING_BALANCE, now))
    conn.execute('INSERT OR IGNORE INTO settings (id, strategy, updated_at) VALUES (1, "safe", ?)', (now,))
    row = conn.execute('SELECT COUNT(*) FROM equity_history').fetchone()
    if row[0] == 0:
        conn.execute('INSERT INTO equity_history (value, timestamp) VALUES (?, ?)', (STARTING_BALANCE, now))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Price fetching — bulk refresh in background, zero blocking in request path
# ---------------------------------------------------------------------------

def _refresh_prices():
    """Fetch all watchlist prices + MAs in one bulk yf.download call."""
    global _prices_ready
    try:
        import yfinance as yf
        # Single bulk download for current prices (1-min bars, today)
        raw = yf.download(
            WATCHLIST, period='1d', interval='1m',
            group_by='ticker', progress=False, auto_adjust=True, threads=True
        )
        for sym in WATCHLIST:
            try:
                col = raw[sym]['Close'] if len(WATCHLIST) > 1 else raw['Close']
                col = col.dropna()
                if not col.empty:
                    _price_cache[sym] = float(col.iloc[-1])
            except Exception:
                pass

        # Daily data for 10-day MA (one bulk call)
        daily = yf.download(
            WATCHLIST, period='20d', interval='1d',
            group_by='ticker', progress=False, auto_adjust=True, threads=True
        )
        for sym in WATCHLIST:
            try:
                col = daily[sym]['Close'] if len(WATCHLIST) > 1 else daily['Close']
                col = col.dropna()
                if len(col) >= 10:
                    _ma_cache[sym] = float(col.tail(10).mean())
            except Exception:
                pass

        _prices_ready = True
        print(f"Prices refreshed: {list(_price_cache.keys())}")
    except Exception as e:
        print(f"Price refresh error: {e}")
        # Apply tiny random walk to keep cached prices alive
        for sym in list(_price_cache.keys()):
            _price_cache[sym] *= (1 + random.gauss(0, 0.0003))


def get_price(symbol):
    """Return cached price instantly — never blocks on network."""
    return _price_cache.get(symbol)


def get_ma(symbol):
    return _ma_cache.get(symbol)


def _price_refresh_loop():
    """Background thread: refresh every 60 s."""
    while True:
        _refresh_prices()
        time.sleep(60)


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

def portfolio_value(conn):
    bal = conn.execute('SELECT balance FROM portfolio WHERE id = 1').fetchone()['balance']
    rows = conn.execute('SELECT symbol, shares FROM positions').fetchall()
    pos_val = sum((get_price(r['symbol']) or 0) * r['shares'] for r in rows)
    return bal + pos_val


# ---------------------------------------------------------------------------
# Trading bot (background thread)
# ---------------------------------------------------------------------------

_last_trade = {}       # symbol -> unix timestamp
_last_equity_rec = 0   # unix timestamp


def trading_bot():
    global _last_equity_rec
    print('Trading bot started')
    while True:
        try:
            with _db_lock:
                conn = get_db()
                row = conn.execute('SELECT strategy FROM settings WHERE id = 1').fetchone()
                strategy = row['strategy'] if row else 'safe'
                cfg = STRATEGIES.get(strategy, STRATEGIES['safe'])
                now = time.time()

                for symbol in WATCHLIST:
                    if now - _last_trade.get(symbol, 0) < cfg['interval']:
                        continue

                    price = get_price(symbol)
                    if not price:
                        continue

                    ma = _ma_cache.get(symbol)
                    signal = ((price - ma) / ma) if ma else random.uniform(-0.01, 0.01)

                    bal = conn.execute('SELECT balance FROM portfolio WHERE id = 1').fetchone()['balance']
                    pos = conn.execute('SELECT * FROM positions WHERE symbol = ?', (symbol,)).fetchone()
                    n_pos = conn.execute('SELECT COUNT(DISTINCT symbol) FROM positions').fetchone()[0]

                    ts = datetime.utcnow().isoformat()
                    pval = portfolio_value(conn)
                    trade_val = pval * cfg['position_pct']

                    # BUY
                    if signal > cfg['threshold'] and pos is None and n_pos < cfg['max_pos']:
                        shares = round(trade_val / price, 4)
                        cost = shares * price
                        if cost <= bal and shares > 0:
                            conn.execute('UPDATE portfolio SET balance = balance - ?, updated_at = ? WHERE id = 1', (cost, ts))
                            conn.execute(
                                'INSERT INTO positions (symbol, shares, avg_price, created_at) VALUES (?, ?, ?, ?)'
                                ' ON CONFLICT(symbol) DO UPDATE SET'
                                ' avg_price = (avg_price * shares + excluded.avg_price * excluded.shares) / (shares + excluded.shares),'
                                ' shares = shares + excluded.shares',
                                (symbol, shares, price, ts))
                            conn.execute(
                                'INSERT INTO trades (symbol, action, shares, price, total, pnl, timestamp)'
                                ' VALUES (?, "BUY", ?, ?, ?, NULL, ?)',
                                (symbol, shares, price, cost, ts))
                            conn.commit()
                            _last_trade[symbol] = now
                            print(f'BUY  {symbol}: {shares:.4f} @ ${price:.2f}  signal={signal:.4f}')

                    # SELL
                    elif signal < -cfg['threshold'] and pos is not None:
                        shares = pos['shares']
                        proceeds = shares * price
                        pnl = proceeds - shares * pos['avg_price']
                        conn.execute('UPDATE portfolio SET balance = balance + ?, updated_at = ? WHERE id = 1', (proceeds, ts))
                        conn.execute('DELETE FROM positions WHERE symbol = ?', (symbol,))
                        conn.execute(
                            'INSERT INTO trades (symbol, action, shares, price, total, pnl, timestamp)'
                            ' VALUES (?, "SELL", ?, ?, ?, ?, ?)',
                            (symbol, shares, price, proceeds, pnl, ts))
                        conn.commit()
                        _last_trade[symbol] = now
                        print(f'SELL {symbol}: {shares:.4f} @ ${price:.2f}  pnl=${pnl:.2f}')

                # Equity snapshot every 30 s
                if now - _last_equity_rec >= 30:
                    pval = portfolio_value(conn)
                    conn.execute('INSERT INTO equity_history (value, timestamp) VALUES (?, ?)',
                                 (pval, datetime.utcnow().isoformat()))
                    conn.commit()
                    _last_equity_rec = now

                conn.close()

        except Exception as e:
            print(f'Bot error: {e}')
            import traceback; traceback.print_exc()

        time.sleep(5)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/portfolio')
def api_portfolio():
    with _db_lock:
        conn = get_db()
        bal = conn.execute('SELECT balance FROM portfolio WHERE id = 1').fetchone()['balance']
        rows = conn.execute('SELECT * FROM positions').fetchall()
        conn.close()

    positions, pos_val, total_pnl = [], 0, 0
    for r in rows:
        p = get_price(r['symbol']) or r['avg_price']
        val = r['shares'] * p
        pnl = val - r['shares'] * r['avg_price']
        pnl_pct = pnl / (r['shares'] * r['avg_price']) * 100 if r['avg_price'] > 0 else 0
        pos_val += val
        total_pnl += pnl
        positions.append({
            'symbol': r['symbol'],
            'shares': round(r['shares'], 4),
            'avg_price': round(r['avg_price'], 2),
            'current_price': round(p, 2),
            'value': round(val, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
        })

    total = bal + pos_val
    ret = (total - STARTING_BALANCE) / STARTING_BALANCE * 100

    return jsonify({
        'balance': round(bal, 2),
        'total_value': round(total, 2),
        'position_value': round(pos_val, 2),
        'total_pnl': round(total_pnl, 2),
        'total_return': round(ret, 2),
        'positions': positions,
    })


@app.route('/api/trades')
def api_trades():
    limit = request.args.get('limit', 50, type=int)
    with _db_lock:
        conn = get_db()
        rows = conn.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?', (limit,)).fetchall()
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/equity')
def api_equity():
    limit = request.args.get('limit', 200, type=int)
    with _db_lock:
        conn = get_db()
        rows = conn.execute('SELECT value, timestamp FROM equity_history ORDER BY timestamp ASC').fetchall()
        conn.close()
    data = [{'value': r['value'], 'timestamp': r['timestamp']} for r in rows]
    return jsonify(data[-limit:] if len(data) > limit else data)


@app.route('/api/prices')
def api_prices():
    return jsonify({s: round(get_price(s), 2) for s in WATCHLIST if get_price(s)})


@app.route('/api/buy', methods=['POST'])
def api_buy():
    data = request.json or {}
    symbol = data.get('symbol', '').upper().strip()
    try:
        shares = float(data.get('shares', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid shares'}), 400

    if not symbol or shares <= 0:
        return jsonify({'error': 'Symbol and positive share count required'}), 400

    price = get_price(symbol)
    if not price:
        return jsonify({'error': f'Could not fetch price for {symbol}'}), 400

    cost = shares * price
    with _db_lock:
        conn = get_db()
        bal = conn.execute('SELECT balance FROM portfolio WHERE id = 1').fetchone()['balance']
        if cost > bal:
            conn.close()
            return jsonify({'error': f'Insufficient funds — need ${cost:.2f}, have ${bal:.2f}'}), 400
        ts = datetime.utcnow().isoformat()
        conn.execute('UPDATE portfolio SET balance = balance - ?, updated_at = ? WHERE id = 1', (cost, ts))
        conn.execute(
            'INSERT INTO positions (symbol, shares, avg_price, created_at) VALUES (?, ?, ?, ?)'
            ' ON CONFLICT(symbol) DO UPDATE SET'
            ' avg_price = (avg_price * shares + excluded.avg_price * excluded.shares) / (shares + excluded.shares),'
            ' shares = shares + excluded.shares',
            (symbol, shares, price, ts))
        conn.execute('INSERT INTO trades (symbol, action, shares, price, total, pnl, timestamp) VALUES (?, "BUY", ?, ?, ?, NULL, ?)',
                     (symbol, shares, price, cost, ts))
        conn.commit()
        conn.close()

    return jsonify({'success': True, 'symbol': symbol, 'shares': shares, 'price': price, 'total': round(cost, 2)})


@app.route('/api/sell', methods=['POST'])
def api_sell():
    data = request.json or {}
    symbol = data.get('symbol', '').upper().strip()
    try:
        shares = float(data.get('shares', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid shares'}), 400

    if not symbol or shares <= 0:
        return jsonify({'error': 'Symbol and positive share count required'}), 400

    with _db_lock:
        conn = get_db()
        pos = conn.execute('SELECT * FROM positions WHERE symbol = ?', (symbol,)).fetchone()
        if not pos:
            conn.close()
            return jsonify({'error': f'No position in {symbol}'}), 400
        if pos['shares'] < shares - 0.0001:
            conn.close()
            return jsonify({'error': f'Only {pos["shares"]:.4f} shares available'}), 400

        price = get_price(symbol)
        if not price:
            conn.close()
            return jsonify({'error': f'Could not fetch price for {symbol}'}), 400

        proceeds = shares * price
        pnl = proceeds - shares * pos['avg_price']
        ts = datetime.utcnow().isoformat()
        conn.execute('UPDATE portfolio SET balance = balance + ?, updated_at = ? WHERE id = 1', (proceeds, ts))
        remaining = pos['shares'] - shares
        if remaining < 0.0001:
            conn.execute('DELETE FROM positions WHERE symbol = ?', (symbol,))
        else:
            conn.execute('UPDATE positions SET shares = ? WHERE symbol = ?', (remaining, symbol))
        conn.execute('INSERT INTO trades (symbol, action, shares, price, total, pnl, timestamp) VALUES (?, "SELL", ?, ?, ?, ?, ?)',
                     (symbol, shares, price, proceeds, pnl, ts))
        conn.commit()
        conn.close()

    return jsonify({'success': True, 'symbol': symbol, 'shares': shares, 'price': price,
                    'total': round(proceeds, 2), 'pnl': round(pnl, 2)})


@app.route('/api/strategy', methods=['GET', 'POST'])
def api_strategy():
    if request.method == 'POST':
        s = (request.json or {}).get('strategy', '')
        if s not in STRATEGIES:
            return jsonify({'error': 'Invalid strategy'}), 400
        with _db_lock:
            conn = get_db()
            conn.execute('UPDATE settings SET strategy = ?, updated_at = ? WHERE id = 1',
                         (s, datetime.utcnow().isoformat()))
            conn.commit()
            conn.close()
        return jsonify({'success': True, 'strategy': s})

    with _db_lock:
        conn = get_db()
        row = conn.execute('SELECT strategy FROM settings WHERE id = 1').fetchone()
        conn.close()
    s = row['strategy'] if row else 'safe'
    return jsonify({'strategy': s, 'config': STRATEGIES[s]})


@app.route('/api/reset', methods=['POST'])
def api_reset():
    with _db_lock:
        conn = get_db()
        ts = datetime.utcnow().isoformat()
        conn.execute('UPDATE portfolio SET balance = ?, updated_at = ? WHERE id = 1', (STARTING_BALANCE, ts))
        conn.execute('DELETE FROM positions')
        conn.execute('DELETE FROM trades')
        conn.execute('DELETE FROM equity_history')
        conn.execute('INSERT INTO equity_history (value, timestamp) VALUES (?, ?)', (STARTING_BALANCE, ts))
        conn.commit()
        conn.close()
    _last_trade.clear()
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

init_db()
# Price refresher starts first so cache is warm before bot needs it
_pricer = threading.Thread(target=_price_refresh_loop, daemon=True)
_pricer.start()
_bot = threading.Thread(target=trading_bot, daemon=True)
_bot.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
