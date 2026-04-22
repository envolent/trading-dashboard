import os
import sqlite3
import threading
import time
import random
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DB_PATH = os.environ.get('DB_PATH', 'trading.db')
STARTING_BALANCE = 10000.0

PENNY_STOCK_MIN = 5.0  # SEC definition: under $5 = penny stock

# S&P 500 fallback used until dynamic fetch succeeds
_SP500_FALLBACK = [
    'AAPL','MSFT','GOOGL','AMZN','META','NVDA','TSLA','BRK-B','JPM','V',
    'UNH','XOM','MA','JNJ','PG','HD','CVX','MRK','ABBV','PEP','KO','LLY',
    'AVGO','COST','MCD','WMT','BAC','TMO','CSCO','ACN','ABT','CRM','DHR',
    'ADBE','NKE','TXN','WFC','PM','NEE','RTX','BMY','AMGN','QCOM','HON',
    'SBUX','T','LOW','GE','ELV','INTC','AMD','INTU','MDT','GILD','CAT',
    'SPGI','GS','BLK','AXP','ISRG','C','VRTX','REGN','CVS','SYK','ZTS',
    'ADI','BKNG','MO','MDLZ','CI','PYPL','TGT','CB','SO','DUK','MMM',
    'ETN','PLD','NOC','LMT','F','GM','USB','TFC','MS','SCHW','ITW',
    'AON','MCO','CME','ICE','NSC','UNP','DE','EMR','APD','ECL','FDX',
    'SPY','QQQ','IWM','DIA','GLD',
]

WATCHLIST = list(_SP500_FALLBACK)  # replaced at runtime by _load_sp500()


def _load_sp500():
    """Fetch current S&P 500 tickers from Wikipedia. Falls back to hardcoded list."""
    global WATCHLIST
    try:
        import requests
        r = requests.get(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        # Parse the first table's Symbol column
        symbols = []
        in_table = False
        for line in r.text.splitlines():
            if '<table class="wikitable' in line:
                in_table = True
            if in_table and '<td>' in line and '</td>' in line:
                cell = line.strip().replace('<td>', '').replace('</td>', '').replace('\n', '')
                # Symbol cells contain a link: <td><a ...>AAPL</a></td>
                if '<a' in cell:
                    import re
                    m = re.search(r'>([A-Z]{1,5}(?:\.[A-Z])?)<', cell)
                    if m:
                        sym = m.group(1).replace('.', '-')
                        symbols.append(sym)
                        if len(symbols) >= 505:
                            break
        if len(symbols) > 100:
            WATCHLIST = symbols
            print(f'Loaded {len(WATCHLIST)} S&P 500 symbols')
        else:
            print(f'S&P 500 parse got only {len(symbols)} symbols, keeping fallback')
    except Exception as e:
        print(f'S&P 500 load error: {e} — using fallback list')

MAX_TRADE_VALUE  = 500.0   # max $ per buy order (buy 1 share if price > this)
MAX_DAILY_TRADES = 25      # bot + manual combined

STRATEGIES = {
    'ultra_safe':       {'interval': 120, 'position_pct': 0.02, 'max_pos': 3,  'threshold': 0.025, 'label': 'Ultra Safe'},
    'safe':             {'interval':  60, 'position_pct': 0.05, 'max_pos': 5,  'threshold': 0.015, 'label': 'Safe'},
    'risky':            {'interval':  30, 'position_pct': 0.10, 'max_pos': 7,  'threshold': 0.008, 'label': 'Risky'},
    'aggressive':       {'interval':  15, 'position_pct': 0.20, 'max_pos': 8,  'threshold': 0.004, 'label': 'Aggressive'},
    'ultra_aggressive': {'interval':   8, 'position_pct': 0.35, 'max_pos': 10, 'threshold': 0.001, 'label': 'Ultra Aggressive'},
}

_price_cache = {}       # symbol -> float  (updated by background thread)
_prev_price_cache = {}  # symbol -> float  (snapshot from previous refresh cycle)
_change_cache = {}      # symbol -> float  (intraday % change from prev close, decimal)
_ma_cache = {}          # symbol -> float  (10-day moving average)
_db_lock = threading.Lock()
_prices_ready = False  # True once first fetch completes


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')   # concurrent reads while bot writes
    conn.execute('PRAGMA synchronous=NORMAL')
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
# Price fetching — direct Yahoo Finance JSON, no pandas/numpy
# ---------------------------------------------------------------------------

_YF_HEADERS = {'User-Agent': 'Mozilla/5.0'}

def _fetch_quotes_batch(symbols):
    """Fetch current prices + intraday change % for up to 100 symbols in one call."""
    import requests
    try:
        csv = ','.join(symbols)
        url = (f'https://query1.finance.yahoo.com/v7/finance/quote'
               f'?symbols={csv}&fields=regularMarketPrice,regularMarketChangePercent')
        r = requests.get(url, headers=_YF_HEADERS, timeout=15)
        for q in r.json().get('quoteResponse', {}).get('result', []):
            sym = q.get('symbol')
            p   = q.get('regularMarketPrice')
            chg = q.get('regularMarketChangePercent')
            if sym and p and float(p) >= PENNY_STOCK_MIN:
                _price_cache[sym] = float(p)
                if chg is not None:
                    _change_cache[sym] = float(chg) / 100  # store as decimal, e.g. 0.015
    except Exception as e:
        print(f'Quote batch error: {e}')


def _refresh_prices():
    """Refresh all prices in 100-symbol batches."""
    global _prices_ready, _prev_price_cache
    symbols = list(WATCHLIST)

    # Snapshot previous prices before overwriting
    _prev_price_cache = dict(_price_cache)

    # Batch into groups of 100 (Yahoo Finance limit per request)
    for i in range(0, len(symbols), 100):
        _fetch_quotes_batch(symbols[i:i+100])
    if _price_cache:
        _prices_ready = True
        print(f'Prices refreshed for {len(_price_cache)} symbols')

    # -- 10-day MA: fetch 50 per cycle so cache fills in ~10 minutes --
    import requests
    missing_ma = [s for s in symbols if s not in _ma_cache]
    for sym in missing_ma[:50]:
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=20d'
            r = requests.get(url, headers=_YF_HEADERS, timeout=5)
            closes = r.json()['chart']['result'][0]['indicators']['quote'][0]['close']
            closes = [c for c in closes if c is not None]
            if len(closes) >= 10:
                _ma_cache[sym] = sum(closes[-10:]) / 10
        except Exception as e:
            print(f'MA fetch error {sym}: {e}')


def get_price(symbol):
    return _price_cache.get(symbol)


def fetch_price_now(symbol):
    """Fetch a single symbol price immediately (used for manual trades)."""
    import requests
    try:
        url = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}&fields=regularMarketPrice'
        r = requests.get(url, headers=_YF_HEADERS, timeout=10)
        result = r.json().get('quoteResponse', {}).get('result', [])
        if result:
            p = result[0].get('regularMarketPrice')
            if p:
                _price_cache[symbol] = float(p)
                return float(p)
    except Exception as e:
        print(f"fetch_price_now error {symbol}: {e}")
    return _price_cache.get(symbol)


def _price_refresh_loop():
    """Refresh current prices every 60 s; MAs are fetched once on startup."""
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

_last_trade = {}        # symbol -> unix timestamp
_last_equity_rec = 0    # unix timestamp
_last_claude_call = 0   # unix timestamp — Claude decides every 5 min

_PT = ZoneInfo('America/Los_Angeles')

def is_market_open():
    now_pt = datetime.now(_PT)
    if now_pt.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return 6 <= now_pt.hour < 13       # 6:00 AM – 1:00 PM PT


def _claude_decide(bal, positions_db, n_pos, pval, remaining, cfg):
    """Call Claude to decide what to trade. Returns list of action tuples."""
    global _last_claude_call
    import anthropic

    # Build stock universe for Claude — prefer day-change movers, fall back to price cache
    if _change_cache:
        movers = sorted(
            [(s, c, _price_cache[s]) for s, c in _change_cache.items() if s in _price_cache],
            key=lambda x: abs(x[1]), reverse=True
        )[:30]
        mover_lines = [f"  {s}: {c*100:+.2f}% today @ ${pr:.2f}" for s, c, pr in movers]
    elif _price_cache:
        sample = list(_price_cache.items())[:30]
        mover_lines = [f"  {s}: ${pr:.2f}" for s, pr in sample]
    else:
        # No price data yet — skip this cycle
        _last_claude_call = time.time()
        return []

    pos_lines = []
    for sym, p in positions_db.items():
        cur = get_price(sym) or p['avg_price']
        pnl = (cur - p['avg_price']) * p['shares']
        pos_lines.append(f"  {sym}: {p['shares']:.4f} sh @ ${p['avg_price']:.2f}, now ${cur:.2f}, P&L ${pnl:+.2f}")

    max_pos = cfg['max_pos']
    pos_pct = cfg['position_pct']

    prompt = (
        f"You are an autonomous paper trading bot managing a paper portfolio. Make trades NOW.\n\n"
        f"PORTFOLIO:\n"
        f"  Cash: ${bal:.2f}\n"
        f"  Total value: ${pval:.2f} (started at $10,000)\n"
        f"  Open positions: {n_pos}/{max_pos}\n"
        f"  Trades left today: {remaining}\n\n"
        f"CURRENT POSITIONS:\n" + ("\n".join(pos_lines) if pos_lines else "  None") + "\n\n"
        f"AVAILABLE STOCKS:\n" + "\n".join(mover_lines) + "\n\n"
        f"RULES:\n"
        f"  - You MUST buy something if cash > $50 and positions < {max_pos}\n"
        f"  - Max ${MAX_TRADE_VALUE:.0f} per buy order\n"
        f"  - If a stock price > ${MAX_TRADE_VALUE:.0f}, buy exactly 1 share\n"
        f"  - Otherwise buy shares = floor({MAX_TRADE_VALUE:.0f} / price)\n"
        f"  - No penny stocks (price must be >= $5)\n"
        f"  - Don't buy a symbol already in positions\n"
        f"  - Sell positions that are losing >2% or have gained >3%\n\n"
        f"Respond with a JSON array ONLY — no markdown, no explanation:\n"
        f'[{{"action":"BUY","symbol":"NVDA","shares":1}},{{"action":"SELL","symbol":"AAPL","shares":2.5}}]\n'
        f"You MUST include at least 1 trade if cash is available and positions < max."
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
        msg = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _last_claude_call = time.time()
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:].strip()
        decisions = json.loads(text)
        print(f'Claude decisions: {decisions}')
    except Exception as e:
        print(f'Claude decision error: {e}')
        _last_claude_call = time.time()  # back off even on error
        return []

    # Convert Claude's decisions into validated action tuples
    actions = []
    cur_bal = bal
    cur_n_pos = n_pos

    for d in decisions:
        action = d.get('action', '').upper()
        symbol = d.get('symbol', '').upper().strip()
        if not action or not symbol:
            continue

        price = get_price(symbol) or fetch_price_now(symbol)
        if not price or price < PENNY_STOCK_MIN:
            continue

        if action == 'BUY':
            if cur_n_pos >= max_pos:
                continue
            if symbol in positions_db:
                continue
            raw_shares = d.get('shares', 0)
            try:
                shares = round(float(raw_shares), 4)
            except Exception:
                continue
            if shares <= 0:
                continue
            # Enforce $500 cap
            if price <= MAX_TRADE_VALUE:
                max_shares = round(MAX_TRADE_VALUE / price, 4)
                shares = min(shares, max_shares)
            else:
                shares = 1.0
            cost = shares * price
            if cost > cur_bal or shares <= 0:
                continue
            actions.append(('BUY', symbol, shares, price, cost, None))
            cur_bal -= cost
            cur_n_pos += 1

        elif action == 'SELL':
            pos = positions_db.get(symbol)
            if not pos:
                continue
            shares = pos['shares']
            proceeds = shares * price
            pnl = proceeds - shares * pos['avg_price']
            actions.append(('SELL', symbol, shares, price, proceeds, pnl))
            cur_bal += proceeds
            cur_n_pos -= 1

    return actions


def _rule_decide(bal, positions_db, n_pos, pval, remaining, cfg, now):
    """Fallback rule-based decisions when no API key is set."""
    actions = []
    for symbol in WATCHLIST:
        if len(actions) >= remaining:
            break
        if now - _last_trade.get(symbol, 0) < cfg['interval']:
            continue
        price = get_price(symbol)
        if not price:
            continue
        ma   = _ma_cache.get(symbol)
        chg  = _change_cache.get(symbol)
        prev = _prev_price_cache.get(symbol)
        if ma and ma > 0:
            signal = (price - ma) / ma
        elif chg is not None:
            signal = chg
        elif prev and prev > 0:
            signal = (price - prev) / prev
        else:
            continue
        pos = positions_db.get(symbol)
        if signal > cfg['threshold'] and pos is None and n_pos < cfg['max_pos']:
            shares = 1.0 if price > MAX_TRADE_VALUE else round(min(pval * cfg['position_pct'], MAX_TRADE_VALUE) / price, 4)
            cost = shares * price
            if cost <= bal and shares > 0:
                actions.append(('BUY', symbol, shares, price, cost, None))
                bal -= cost
                n_pos += 1
        elif signal < -cfg['threshold'] and pos is not None:
            shares = pos['shares']
            proceeds = shares * price
            pnl = proceeds - shares * pos['avg_price']
            actions.append(('SELL', symbol, shares, price, proceeds, pnl))
            bal += proceeds
            n_pos -= 1
    return actions


def trading_bot():
    global _last_equity_rec
    print('Trading bot started')
    while True:
        try:
            if not is_market_open():
                time.sleep(30)
                continue

            now = time.time()

            today_str = datetime.utcnow().strftime('%Y-%m-%d')

            # --- Read phase (short lock) ---
            with _db_lock:
                conn = get_db()
                _row = conn.execute('SELECT strategy FROM settings WHERE id = 1').fetchone()
                strategy = _row['strategy'] if _row else 'safe'
                bal = conn.execute('SELECT balance FROM portfolio WHERE id = 1').fetchone()['balance']
                positions_db = {r['symbol']: dict(r) for r in conn.execute('SELECT * FROM positions').fetchall()}
                n_pos = len(positions_db)
                pval = portfolio_value(conn)
                daily_trades = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE timestamp LIKE ?", (today_str + '%',)
                ).fetchone()[0]
                conn.close()

            if daily_trades >= MAX_DAILY_TRADES:
                time.sleep(60)
                continue

            cfg = STRATEGIES.get(strategy, STRATEGIES['safe'])
            remaining = MAX_DAILY_TRADES - daily_trades

            # --- Decision phase: Claude AI (every 5 min) or rule-based fallback ---
            actions = []
            api_key = os.environ.get('ANTHROPIC_API_KEY', '')

            if api_key and now - _last_claude_call >= 120:
                actions = _claude_decide(
                    bal, positions_db, n_pos, pval, remaining, cfg)
            elif not api_key:
                actions = _rule_decide(
                    bal, positions_db, n_pos, pval, remaining, cfg, now)

            # --- Write phase ---
            if actions:
                with _db_lock:
                    conn = get_db()
                    ts = datetime.utcnow().isoformat()
                    for action, symbol, shares, price, total, pnl in actions:
                        if action == 'BUY':
                            conn.execute('UPDATE portfolio SET balance = balance - ?, updated_at = ? WHERE id = 1', (total, ts))
                            conn.execute(
                                'INSERT INTO positions (symbol, shares, avg_price, created_at) VALUES (?, ?, ?, ?)'
                                ' ON CONFLICT(symbol) DO UPDATE SET'
                                ' avg_price = (avg_price * shares + excluded.avg_price * excluded.shares) / (shares + excluded.shares),'
                                ' shares = shares + excluded.shares',
                                (symbol, shares, price, ts))
                            conn.execute('INSERT INTO trades (symbol, action, shares, price, total, pnl, timestamp) VALUES (?, "BUY", ?, ?, ?, NULL, ?)',
                                         (symbol, shares, price, total, ts))
                            print(f'BUY  {symbol}: {shares:.4f} @ ${price:.2f}')
                        else:
                            conn.execute('UPDATE portfolio SET balance = balance + ?, updated_at = ? WHERE id = 1', (total, ts))
                            conn.execute('DELETE FROM positions WHERE symbol = ?', (symbol,))
                            conn.execute('INSERT INTO trades (symbol, action, shares, price, total, pnl, timestamp) VALUES (?, "SELL", ?, ?, ?, ?, ?)',
                                         (symbol, shares, price, total, pnl, ts))
                            print(f'SELL {symbol}: {shares:.4f} @ ${price:.2f}  pnl=${pnl:.2f}')
                        _last_trade[symbol] = now
                    conn.commit()
                    conn.close()

            # --- Equity snapshot every 30 s (short lock) ---
            if now - _last_equity_rec >= 30:
                with _db_lock:
                    conn = get_db()
                    pval = portfolio_value(conn)
                    conn.execute('INSERT INTO equity_history (value, timestamp) VALUES (?, ?)',
                                 (pval, datetime.utcnow().isoformat()))
                    conn.commit()
                    conn.close()
                _last_equity_rec = now

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
        first_rec = conn.execute('SELECT timestamp FROM equity_history ORDER BY timestamp ASC LIMIT 1').fetchone()
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        today_start_rec = conn.execute(
            "SELECT value FROM equity_history WHERE timestamp LIKE ? ORDER BY timestamp ASC LIMIT 1",
            (today_str + '%',)
        ).fetchone()
        daily_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp LIKE ?", (today_str + '%',)
        ).fetchone()[0]
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

    # Day counter (capped at 30)
    if first_rec:
        start_dt = datetime.fromisoformat(first_rec['timestamp'])
        day_number = min(30, max(1, (datetime.utcnow() - start_dt).days + 1))
    else:
        day_number = 1

    # Today's P&L %
    today_start_val = today_start_rec['value'] if today_start_rec else STARTING_BALANCE
    today_pnl_pct = (total - today_start_val) / today_start_val * 100 if today_start_val > 0 else 0

    return jsonify({
        'balance': round(bal, 2),
        'total_value': round(total, 2),
        'position_value': round(pos_val, 2),
        'total_pnl': round(total_pnl, 2),
        'total_return': round(ret, 2),
        'positions': positions,
        'day_number': day_number,
        'today_pnl_pct': round(today_pnl_pct, 2),
        'market_open': is_market_open(),
        'daily_trades': daily_trades,
        'max_daily_trades': MAX_DAILY_TRADES,
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


@app.route('/api/watchlist')
def api_watchlist():
    return jsonify(WATCHLIST)


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

    price = get_price(symbol) or fetch_price_now(symbol)
    if not price:
        return jsonify({'error': f'Could not fetch price for {symbol}'}), 400

    if price < PENNY_STOCK_MIN:
        return jsonify({'error': f'{symbol} is a penny stock (${price:.2f} < $5.00) — not allowed'}), 400

    cost = shares * price
    if price <= MAX_TRADE_VALUE and cost > MAX_TRADE_VALUE:
        return jsonify({'error': f'Max ${MAX_TRADE_VALUE:.0f} per order (would cost ${cost:.2f})'}), 400

    with _db_lock:
        conn = get_db()
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        daily_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp LIKE ?", (today_str + '%',)
        ).fetchone()[0]
        if daily_trades >= MAX_DAILY_TRADES:
            conn.close()
            return jsonify({'error': f'Daily trade limit reached ({MAX_DAILY_TRADES}/day)'}), 400
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
    _reasoning_cache.clear()
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Trade Analysis / AI Reasoning
# ---------------------------------------------------------------------------

_reasoning_cache = {}
_reasoning_lock = threading.Lock()
REASONING_TTL = 600  # 10 minutes


def fetch_news_headlines(symbol, max_items=3):
    """Fetch Yahoo Finance RSS headlines for a symbol."""
    import requests
    try:
        url = f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US'
        r = requests.get(url, headers=_YF_HEADERS, timeout=4)
        root = ET.fromstring(r.content)
        headlines = []
        for item in root.iter('item'):
            title = item.findtext('title', '').strip()
            if title:
                headlines.append(title)
            if len(headlines) >= max_items:
                break
        return headlines
    except Exception as e:
        print(f'News fetch error {symbol}: {e}')
        return []


def generate_reasoning():
    """Fetch news + call Claude to produce per-symbol trade reasoning."""
    import anthropic

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY not set')

    with _db_lock:
        conn = get_db()
        positions = [dict(r) for r in conn.execute('SELECT * FROM positions').fetchall()]
        recent_trades = [dict(r) for r in conn.execute(
            'SELECT * FROM trades ORDER BY timestamp DESC LIMIT 20').fetchall()]
        conn.close()

    if not positions and not recent_trades:
        return []

    symbols = list({p['symbol'] for p in positions} | {t['symbol'] for t in recent_trades[:10]})

    news_map = {}
    for sym in symbols[:5]:          # cap to 5 symbols max to keep response fast
        news_map[sym] = fetch_news_headlines(sym)

    pos_lines = []
    for p in positions:
        price = get_price(p['symbol']) or p['avg_price']
        pnl = (price - p['avg_price']) * p['shares']
        pos_lines.append(
            f"  {p['symbol']}: {p['shares']:.4f} sh @ ${p['avg_price']:.2f}, "
            f"now ${price:.2f}, P&L ${pnl:+.2f}")

    trade_lines = []
    for t in recent_trades[:10]:
        line = f"  {t['action']} {t['symbol']}: {t['shares']:.4f} sh @ ${t['price']:.2f}"
        if t['pnl'] is not None:
            line += f" (P&L ${t['pnl']:+.2f})"
        trade_lines.append(line)

    news_lines = []
    for sym, headlines in news_map.items():
        if headlines:
            news_lines.append(f"  {sym}: " + " | ".join(headlines))

    prompt = (
        "You are a financial analyst for a paper trading bot. Analyze the positions and trades below, "
        "then provide brief reasoning for each symbol.\n\n"
        "CURRENT POSITIONS:\n" + ("\n".join(pos_lines) if pos_lines else "  None") + "\n\n"
        "RECENT TRADES (last 10):\n" + ("\n".join(trade_lines) if trade_lines else "  None") + "\n\n"
        "LATEST NEWS HEADLINES:\n" + ("\n".join(news_lines) if news_lines else "  No news available") + "\n\n"
        "Consider: technical signals, news sentiment, current macroeconomic conditions, "
        "Fed policy, political environment, sector trends.\n\n"
        "Respond with a JSON array ONLY (no markdown, no extra text):\n"
        '[{"symbol":"AAPL","sentiment":"BULLISH","reasoning":"2-3 sentences.","headlines":["headline1","headline2"]}]'
    )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=1500,
        messages=[{'role': 'user', 'content': prompt}]
    )

    text = message.content[0].text.strip()
    if text.startswith('```'):
        parts = text.split('```')
        text = parts[1] if len(parts) > 1 else text
        if text.startswith('json'):
            text = text[4:].strip()

    result = json.loads(text)
    for item in result:
        if not item.get('headlines'):
            item['headlines'] = news_map.get(item['symbol'], [])
    return result


@app.route('/api/reasoning')
def api_reasoning():
    force = request.args.get('force', '0') == '1'
    with _reasoning_lock:
        cached = _reasoning_cache.get('data')
        ts = _reasoning_cache.get('ts', 0)
        if cached is not None and not force and time.time() - ts < REASONING_TTL:
            return jsonify({'data': cached, 'cached': True, 'age': int(time.time() - ts)})

    try:
        data = generate_reasoning()
        with _reasoning_lock:
            _reasoning_cache['data'] = data
            _reasoning_cache['ts'] = time.time()
        return jsonify({'data': data, 'cached': False, 'age': 0})
    except Exception as e:
        print(f'Reasoning error: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

init_db()
_load_sp500()
_pricer = threading.Thread(target=_price_refresh_loop, daemon=True)
_pricer.start()
_bot = threading.Thread(target=trading_bot, daemon=True)
_bot.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
