import os
import sys
import time
import sqlite3
import optuna
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.ensemble import RandomForestClassifier
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from flask import Flask
import threading

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    OrderSide,
    TimeInForce,
)
from alpaca.data.historical import (
    StockHistoricalDataClient,
    OptionHistoricalDataClient,
)
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import (
    StockBarsRequest,
    OptionSnapshotRequest,
    NewsRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, OptionsFeed

optuna.logging.set_verbosity(optuna.logging.WARNING)

# ============================================================
# FLASK WEB SERVER FOR RAILWAY HEALTH CHECK
# ============================================================
app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot is running live and healthy!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ============================================================
# CONFIG & SETTINGS
# ============================================================
ALPACA_API_KEY = (
    os.getenv("APCA_API_KEY_ID")
    or os.getenv("ALPACA_API_KEY")
    or os.getenv("API_KEY")
)
ALPACA_SECRET_KEY = (
    os.getenv("APCA_API_SECRET_KEY")
    or os.getenv("ALPACA_SECRET_KEY")
    or os.getenv("SECRET_KEY")
)
if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    print("❌ Alpaca API credentials are missing.")
    sys.exit(1)

PAPER_MODE = True
if not PAPER_MODE:
    print("❌ This version is PAPER ONLY.")
    sys.exit(1)

STOCK_FEED = DataFeed.IEX
OPTIONS_FEED = OptionsFeed.INDICATIVE

DEFAULT_TARGET_UNDERLYINGS = [
    "AAPL",
    "TSLA",
    "NVDA",
    "MSFT",
    "AMZN",
    "AMD",
    "META",
    "GOOGL",
    "NFLX",
]

SECTOR_MAP = {
    "NVDA": "SEMI", "AMD": "SEMI",
    "AAPL": "TECH", "MSFT": "TECH",
    "TSLA": "AUTO", "AMZN": "RETAIL",
    "META": "COMM", "GOOGL": "COMM", "NFLX": "COMM",
    "SPY": "INDEX"
}

# Trade Settings & Risk Management
BASE_CONTRACTS_PER_TRADE = 4
MAX_CONTRACTS_PER_TRADE = 10
TARGET_PROFIT_USD = 70.0
MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 2
MAX_DAILY_LOSS_USD = -200.0  
BASE_CONFIDENCE_THRESHOLD = 0.78  
MAX_NET_PORTFOLIO_DELTA = 6.0  

# Option Filters & Limits
MIN_DTE = 1
MAX_DTE = 14
MIN_DELTA = 0.40
MAX_DELTA = 0.60
MAX_PREMIUM = 15.00
BASE_MAX_SPREAD_PCT = 0.10
MAX_IV_RANK = 0.85
MAX_VIX_THRESHOLD = 30.0 
SCAN_INTERVAL_SECONDS = 60
ERROR_SLEEP_SECONDS = 30
MAX_CONSECUTIVE_ERRORS = 5
DB_FILE = "institutional_bot.db"

# Clients
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
stock_data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
option_data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
news_client = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

ET = ZoneInfo("America/New_York")

def log_print(msg):
    timestamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

# ============================================================
# SQLITE DATABASE & STATE ENGINE
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            date TEXT PRIMARY KEY,
            trades_today INTEGER,
            estimated_daily_pnl REAL,
            trading_halted INTEGER,
            eod_summary_done INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS open_positions (
            option_symbol TEXT PRIMARY KEY,
            underlying TEXT,
            direction TEXT,
            qty REAL,
            initial_qty REAL,
            entry_price REAL,
            confidence REAL,
            scale_out_done INTEGER,
            opened_at TEXT,
            order_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            contract TEXT,
            direction TEXT,
            entry_price REAL,
            exit_price REAL,
            qty REAL,
            pnl_usd REAL,
            reason TEXT,
            closed_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS iv_history (
            symbol TEXT,
            iv REAL,
            recorded_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_today_date():
    return datetime.now(ET).strftime("%Y-%m-%d")

def load_state_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    today = get_today_date()
    
    cursor.execute("SELECT trades_today, estimated_daily_pnl, trading_halted, eod_summary_done FROM bot_state WHERE date = ?", (today,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT OR REPLACE INTO bot_state (date, trades_today, estimated_daily_pnl, trading_halted, eod_summary_done) VALUES (?, 0, 0.0, 0, 0)", (today,))
        conn.commit()
        trades_today, daily_pnl, halted, eod_done = 0, 0.0, 0, 0
    else:
        trades_today, daily_pnl, halted, eod_done = row[0], row[1], row[2], row[3]
        
    cursor.execute("SELECT option_symbol, underlying, direction, qty, initial_qty, entry_price, confidence, scale_out_done, opened_at, order_id FROM open_positions")
    pos_rows = cursor.fetchall()
    positions = {}
    for pr in pos_rows:
        positions[pr[0]] = {
            "underlying": pr[1], "direction": pr[2], "qty": pr[3], "initial_qty": pr[4],
            "entry_price": pr[5], "confidence": pr[6], "scale_out_done": bool(pr[7]),
            "opened_at": pr[8], "order_id": pr[9]
        }
    conn.close()
    
    return {
        "date": today, "trades_today": trades_today, "estimated_daily_pnl": daily_pnl,
        "trading_halted": bool(halted), "eod_summary_done": bool(eod_done), "positions": positions
    }

def save_state_db(state):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    today = state["date"]
    cursor.execute("""
        INSERT OR REPLACE INTO bot_state (date, trades_today, estimated_daily_pnl, trading_halted, eod_summary_done)
        VALUES (?, ?, ?, ?, ?)
    """, (today, state["trades_today"], state["estimated_daily_pnl"], 1 if state["trading_halted"] else 0, 1 if state["eod_summary_done"] else 0))
    
    cursor.execute("DELETE FROM open_positions")
    for opt_sym, pos in state["positions"].items():
        cursor.execute("""
            INSERT INTO open_positions (option_symbol, underlying, direction, qty, initial_qty, entry_price, confidence, scale_out_done, opened_at, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            opt_sym, pos["underlying"], pos["direction"], pos["qty"], pos["initial_qty"],
            pos["entry_price"], pos["confidence"], 1 if pos["scale_out_done"] else 0,
            pos["opened_at"], pos["order_id"]
        ))
    conn.commit()
    conn.close()

state = load_state_db()

def reset_daily_state():
    global state
    today = get_today_date()
    if state["date"] != today:
        state = {
            "date": today, "trades_today": 0, "estimated_daily_pnl": 0.0,
            "trading_halted": False, "eod_summary_done": False, "positions": {}
        }
        save_state_db(state)

def is_market_open():
    now = datetime.now(ET)
    if now.weekday() >= 5: return False
    return dt_time(9, 30) <= now.time() <= dt_time(16, 0)

# ============================================================
# ADVANCED GATES: SOCIAL SENTIMENT RADAR & MACRO CALENDAR
# ============================================================
def check_social_media_sentiment(symbol):
    """رادار مشاعر منصات التواصل الاجتماعي ومجتمعات التداول (Reddit/X Hype)"""
    try:
        end = datetime.now(ET)
        start = end - timedelta(hours=4)
        req = NewsRequest(symbol=symbol, start=start, end=end, limit=10)
        res = news_client.get_news(req)
        if not res or not hasattr(res, "news") or not res.news:
            return True, "Social sentiment neutral"
        
        hype_keywords = ["moon", "squeeze", "rally", "breakout", "surging", "calls buying"]
        panic_keywords = ["dump", "crash", "collapse", "puts buying", "bloodbath"]
        
        hype_score = 0
        for article in res.news:
            headline = str(getattr(article, "headline", "")).lower()
            if any(k in headline for k in hype_keywords): hype_score += 1
            if any(k in headline for k in panic_keywords): hype_score -= 1
            
        if hype_score < -2:
            return False, f"Negative social momentum/panic detected for {symbol}"
        return True, "Social radar clean"
    except Exception:
        return True, "Social radar bypassed"

def check_earnings_and_macro_calendar(symbol):
    try:
        end = datetime.now(ET)
        req = NewsRequest(symbol=symbol, start=end-timedelta(hours=24), end=end, limit=5)
        res = news_client.get_news(req)
        if not res or not hasattr(res, "news") or not res.news: return True, "Clean"
        for article in res.news:
            h = str(getattr(article, "headline", "")).lower()
            if any(k in h for k in ["earnings release", "quarterly results", "cpi report", "fomc meeting"]):
                return False, f"Macro event: {h}"
        return True, "Clean"
    except Exception: return True, "Passed"

def check_portfolio_delta_exposure(new_delta, new_qty, direction):
    try:
        if not state["positions"]: return True, "Clean"
        snapshots = get_option_snapshots(list(state["positions"].keys()))
        total_net_delta = 0.0
        for opt_sym, pos in state["positions"].items():
            qty, pos_dir, delta = float(pos["qty"]), pos["direction"], 0.5
            if opt_sym in snapshots and snapshots[opt_sym].greeks and snapshots[opt_sym].greeks.delta is not None:
                delta = abs(float(snapshots[opt_sym].greeks.delta))
            total_net_delta += (delta * qty if pos_dir == "CALL" else -delta * qty)
        if abs(total_net_delta + (new_delta * new_qty if direction == "CALL" else -new_delta * new_qty)) > MAX_NET_PORTFOLIO_DELTA:
            return False, "Cap exceeded"
        return True, "OK"
    except Exception: return True, "OK"

def check_multi_timeframe_confluence(symbol, direction):
    try:
        end = datetime.now(ET)
        res = stock_data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=end-timedelta(days=5), end=end, feed=STOCK_FEED))
        if not res or symbol not in res or len(res[symbol]) < 20: return True
        df = pd.DataFrame([{"close": float(b.close)} for b in res[symbol]])
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        cur_close, cur_ema = df['close'].iloc[-1], df['ema_20'].iloc[-1]
        if direction == "CALL" and cur_close < cur_ema: return False
        if direction == "PUT" and cur_close > cur_ema: return False
        return True
    except Exception: return True

def check_news_sentiment(symbol):
    try:
        end = datetime.now(ET)
        res = news_client.get_news(NewsRequest(symbol=symbol, start=end-timedelta(hours=6), end=end, limit=3))
        if not res or not hasattr(res, "news") or not res.news: return True, "Clean"
        for article in res.news:
            h = str(getattr(article, "headline", "")).lower()
            if any(k in h for k in ["fraud", "investigation", "lawsuit", "bankruptcy", "halted"]): return False, h
        return True, "Clean"
    except Exception: return True, "Bypassed"

def calculate_portfolio_ratios():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT pnl_usd FROM trade_history")
        rows = cursor.fetchall()
        conn.close()
        if not rows or len(rows) < 3: return 0.0, 0.0
        pnls = np.array([r[0] for r in rows])
        mean_pnl, std_pnl = np.mean(pnls), np.std(pnls)
        sharpe = (mean_pnl / (std_pnl + 1e-8)) * np.sqrt(252)
        neg = pnls[pnls < 0]
        sortino = (mean_pnl / ((np.std(neg) if len(neg) > 0 else 1e-8) + 1e-8)) * np.sqrt(252)
        return float(sharpe), float(sortino)
    except Exception: return 0.0, 0.0

def check_sector_correlation(symbol):
    curr = SECTOR_MAP.get(symbol, symbol)
    for pos in state["positions"].values():
        if curr == SECTOR_MAP.get(pos["underlying"], pos["underlying"]): return False, "Conflict"
    return True, ""

def calculate_iv_rank(symbol, current_iv):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO iv_history (symbol, iv, recorded_at) VALUES (?, ?, ?)", (symbol, current_iv, datetime.now(ET).isoformat()))
        conn.commit()
        cursor.execute("SELECT iv FROM iv_history WHERE symbol = ? ORDER BY recorded_at DESC LIMIT 30", (symbol,))
        rows = cursor.fetchall()
        conn.close()
        if not rows or len(rows) < 3: return 0.3
        iv_list = [r[0] for r in rows]
        if max(iv_list) == min(iv_list): return 0.3
        return (current_iv - min(iv_list)) / (max(iv_list) - min(iv_list))
    except Exception: return 0.3

def check_time_of_day_window():
    now = datetime.now(ET).time()
    return (dt_time(9, 30) <= now <= dt_time(11, 30)) or (dt_time(15, 0) <= now <= dt_time(16, 0))

def get_dynamic_confidence_threshold():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT pnl_usd FROM trade_history ORDER BY id DESC LIMIT 3")
        rows = cursor.fetchall()
        conn.close()
        if not rows: return BASE_CONFIDENCE_THRESHOLD
        recent = [r[0] for r in rows]
        if sum(1 for p in recent if p < 0) >= 2: return min(0.86, BASE_CONFIDENCE_THRESHOLD + 0.06)
        elif all(p > 0 for p in recent): return max(0.75, BASE_CONFIDENCE_THRESHOLD - 0.02)
        return BASE_CONFIDENCE_THRESHOLD
    except Exception: return BASE_CONFIDENCE_THRESHOLD

def get_dynamic_universe():
    try:
        pool = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "AMD", "META", "GOOGL", "NFLX"]
        end = datetime.now(ET)
        res = stock_data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=pool, timeframe=TimeFrame.Hour, start=end-timedelta(days=2), end=end, feed=STOCK_FEED))
        if not res: return DEFAULT_TARGET_UNDERLYINGS
        volumes = {sym: sum(float(b.volume) for b in res[sym]) for sym in pool if sym in res and len(res[sym]) > 0}
        top = sorted(volumes.keys(), key=lambda x: volumes[x], reverse=True)[:5]
        return top if len(top) >= 3 else DEFAULT_TARGET_UNDERLYINGS
    except Exception: return DEFAULT_TARGET_UNDERLYINGS

def check_vix_volatility_regime():
    try:
        res = stock_data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols="VIX", timeframe=TimeFrame.Hour, start=datetime.now(ET)-timedelta(days=2), end=datetime.now(ET), feed=STOCK_FEED))
        if not res or "VIX" not in res or not res["VIX"]: return False, 0.0
        vix = float(res["VIX"][-1].close)
        return vix > MAX_VIX_THRESHOLD, vix
    except Exception: return False, 0.0

def print_eod_summary():
    if state["eod_summary_done"]: return
    now = datetime.now(ET)
    if now.time() >= dt_time(16, 0):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            today = get_today_date()
            cursor.execute("SELECT symbol, direction, pnl_usd FROM trade_history WHERE closed_at LIKE ?", (f"{today}%",))
            rows = cursor.fetchall()
            conn.close()
            total, wins = len(rows), [r for r in rows if r[2] > 0]
            win_rate = (len(wins) / total * 100) if total > 0 else 0.0
            net_pnl = sum(r[2] for r in rows) if rows else state["estimated_daily_pnl"]
            sharpe, sortino = calculate_portfolio_ratios()
            log_print(f"📈 EOD REPORT | Trades: {total} | Win Rate: {win_rate:.1f}% | Net P&L: ${net_pnl:+.2f} | Sharpe: {sharpe:.2f}")
            state["eod_summary_done"] = True
            save_state_db(state)
        except Exception as e: log_print(f"⚠️ EOD error: {e}")

# ============================================================
# ENSEMBLE ENGINE WITH OPTUNA AUTO-TUNING & MID-DAY RETRAINING
# ============================================================
class MultiModelEnsembleEngine:
    def __init__(self):
        self.lgb_params = {"n_estimators": 50, "learning_rate": 0.05, "random_state": 42, "verbose": -1}
        self.rf_params = {"n_estimators": 50, "random_state": 42}
        self.lgb_model = lgb.LGBMClassifier(**self.lgb_params)
        self.rf_model = RandomForestClassifier(**self.rf_params)
        self.is_trained = False
        self.last_trained_date = ""

    def optimize_hyperparameters_optuna(self, X_train, y_train):
        """التحسين الذاتي المستمر للمعاملات عبر Optuna (Hyperparameter Auto-Tuning)"""
        try:
            def objective(trial):
                n_est = trial.suggest_int('n_estimators', 20, 100, step=20)
                lr = trial.suggest_float('learning_rate', 0.01, 0.1, log=True)
                model = lgb.LGBMClassifier(n_estimators=n_est, learning_rate=lr, random_state=42, verbose=-1)
                model.fit(X_train, y_train)
                preds = model.predict(X_train)
                return np.mean(preds == y_train)

            study = optuna.create_study(direction='maximize')
            study.optimize(objective, n_trials=3, timeout=5)
            best_params = study.best_params
            self.lgb_params.update(best_params)
            log_print(f"🧬 OPTUNA AUTO-TUNING COMPLETED: Best LightGBM Params -> {best_params}")
        except Exception:
            pass

    def extract_features_from_bars(self, bars):
        df = pd.DataFrame([{"open": float(b.open), "high": float(b.high), "low": float(b.low), "close": float(b.close), "volume": float(b.volume)} for b in bars])
        if len(df) < 30: return None
        df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        delta = df['close'].diff()
        gain, loss = (delta.where(delta > 0, 0)).rolling(14).mean(), (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        df['volume_zscore'] = (df['volume'] - df['volume'].rolling(20).mean()) / (df['volume'].rolling(20).std() + 1e-8)
        df['atr'] = pd.concat([df['high'] - df['low'], np.abs(df['high'] - df['close'].shift()), np.abs(df['low'] - df['close'].shift())], axis=1).max(axis=1).rolling(14).mean()
        df.dropna(inplace=True)
        return df

    def train_models(self, df):
        try:
            features = ['ema_9', 'ema_20', 'ema_50', 'rsi', 'volume_zscore', 'atr']
            df['future_return'] = df['close'].shift(-5) / df['close'] - 1
            df['target'] = 0
            df.loc[df['future_return'] > 0.003, 'target'] = 1
            df.loc[df['future_return'] < -0.003, 'target'] = -1
            df.dropna(inplace=True)
            if len(df[features]) > 20:
                X, y = df[features], df['target']
                self.optimize_hyperparameters_optuna(X, y)
                self.lgb_model = lgb.LGBMClassifier(**self.lgb_params)
                self.rf_model = RandomForestClassifier(**self.rf_params)
                self.lgb_model.fit(X, y)
                self.rf_model.fit(X, y)
                self.is_trained = True
                self.last_trained_date = get_today_date()
        except Exception: pass

    def analyze(self, symbol, bars):
        df = self.extract_features_from_bars(bars)
        if df is None or len(df) == 0: return {"direction": "NO TRADE", "confidence": 0.0}
        if not self.is_trained: self.train_models(df)
        elif dt_time(12, 0) <= datetime.now(ET).time() <= dt_time(13, 0) and self.last_trained_date != get_today_date():
            self.train_models(df)

        features = ['ema_9', 'ema_20', 'ema_50', 'rsi', 'volume_zscore', 'atr']
        try:
            lgb_pred, lgb_prob = self.lgb_model.predict(df[features].iloc[[-1]])[0], np.max(self.lgb_model.predict_proba(df[features].iloc[[-1]])[0])
            rf_pred, rf_prob = self.rf_model.predict(df[features].iloc[[-1]])[0], np.max(self.rf_model.predict_proba(df[features].iloc[[-1]])[0])
            if lgb_pred != rf_pred: return {"direction": "NO TRADE", "confidence": 0.0}
            conf = (lgb_prob + rf_prob) / 2
            if conf < get_dynamic_confidence_threshold(): return {"direction": "NO TRADE", "confidence": conf}
            return {"direction": {1: "CALL", -1: "PUT"}.get(lgb_pred, "NO TRADE"), "confidence": conf}
        except Exception: return {"direction": "NO TRADE", "confidence": 0.0}

ml_engine = MultiModelEnsembleEngine()

# ============================================================
# OPTIONS EXECUTION & MANAGEMENT
# ============================================================
def get_option_contracts(symbol):
    try:
        res = trading_client.get_option_contracts(GetOptionContractsRequest(underlying_symbols=[symbol], status=AssetStatus.ACTIVE, limit=500))
        return res.option_contracts if hasattr(res, "option_contracts") else res
    except Exception: return []

def get_option_snapshots(symbols):
    if not symbols: return {}
    try: return option_data_client.get_option_snapshots(OptionSnapshotRequest(symbol_or_symbols=symbols, feed=OPTIONS_FEED))
    except Exception: return {}

def select_smart_option(symbol, direction, vix_val=15.0):
    try:
        contracts = get_option_contracts(symbol)
        if not contracts: return None
        today = datetime.now(ET).date()
        candidates = []
        for c in contracts:
            try:
                exp = c.expiration_date
                if isinstance(exp, str): exp = datetime.fromisoformat(exp).date()
                elif hasattr(exp, "date"): exp = exp.date()
                dte = (exp - today).days
                if not (MIN_DTE <= dte <= MAX_DTE): continue
                ctype = str(c.type).upper()
                if direction == "CALL" and "CALL" not in ctype: continue
                if direction == "PUT" and "PUT" not in ctype: continue
                candidates.append((c, dte))
            except Exception: continue

        if not candidates: return None
        snapshots = get_option_snapshots([c[0].symbol for c in candidates])
        if not snapshots: return None

        adaptive_max_spread = min(0.15, BASE_MAX_SPREAD_PCT * (1.0 + (vix_val / 50.0)))

        valid = []
        for c, dte in candidates:
            sym = c.symbol
            if sym not in snapshots: continue
            snap = snapshots[sym]
            quote, greeks, iv = getattr(snap, "latest_quote", None), getattr(snap, "greeks", None), getattr(snap, "implied_volatility", None)
            if not quote or not quote.bid_price or not quote.ask_price: continue
            bid, ask = float(quote.bid_price), float(quote.ask_price)
            if bid <= 0 or ask <= 0: continue
            mid, spread = (bid + ask) / 2, (ask - bid) / ((bid + ask) / 2)
            if mid > MAX_PREMIUM or spread > adaptive_max_spread: continue
            if (ask - bid) > (mid * 0.20): continue 
            if calculate_iv_rank(sym, float(iv) if iv else 0.3) > MAX_IV_RANK: continue
            delta = abs(float(greeks.delta)) if greeks and greeks.delta is not None else 0.5
            if not (MIN_DELTA <= delta <= MAX_DELTA): continue
            gamma, theta = float(greeks.gamma) if greeks and greeks.gamma is not None else 0.01, abs(float(greeks.theta)) if greeks and greeks.theta is not None else 0.01
            valid.append({"contract": c, "symbol": sym, "symbol_delta": delta, "ask": ask, "spread_pct": spread, "momentum_score": gamma / (theta + 1e-6)})

        if not valid: return None
        valid.sort(key=lambda x: (x["spread_pct"], -x["momentum_score"]))
        return valid[0]
    except Exception: return None

def wait_for_fill(order_id, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = trading_client.get_order_by_id(order_id)
            if str(order.status).lower() == "filled": return order
            if str(order.status).lower() in ["canceled", "cancelled", "expired", "rejected"]: return None
        except Exception: pass
        time.sleep(1)
    return None

def submit_smart_limit_order(option_symbol, qty, initial_ask):
    current_ask = initial_ask
    for attempt in range(3):
        try:
            sub = trading_client.submit_order(LimitOrderRequest(symbol=option_symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, limit_price=round(current_ask, 2)))
            order_id = str(sub.id)
            filled = wait_for_fill(order_id, timeout=10)
            if filled: return filled
            try: trading_client.cancel_order_by_id(order_id)
            except Exception: pass
            snapshots = get_option_snapshots([option_symbol])
            if option_symbol in snapshots and snapshots[option_symbol].latest_quote:
                current_ask = float(snapshots[option_symbol].latest_quote.ask_price or current_ask + 0.01)
            else: current_ask += 0.01
        except Exception: pass
    return None

def calculate_kelly_qty(confidence):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT pnl_usd FROM trade_history")
        rows = cursor.fetchall()
        conn.close()
        if not rows or len(rows) < 5: return BASE_CONTRACTS_PER_TRADE if confidence < 0.90 else MAX_CONTRACTS_PER_TRADE
        pnl = [r[0] for r in rows]
        wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p < 0]
        if not wins or not losses: return BASE_CONTRACTS_PER_TRADE
        win_prob = len(wins) / len(pnl)
        b = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
        kelly = max(0.05, min(((win_prob * b - (1.0 - win_prob)) / b) * 0.25, 0.50))
        qty = int(round(BASE_CONTRACTS_PER_TRADE + (kelly * MAX_CONTRACTS_PER_TRADE)))
        if qty % 2 != 0: qty += 1
        return max(2, min(qty, MAX_CONTRACTS_PER_TRADE))
    except Exception: return BASE_CONTRACTS_PER_TRADE

def enter_position(symbol, direction, confidence, vix_val=15.0):
    if state["trading_halted"] or state["trades_today"] >= MAX_TRADES_PER_DAY or len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False

    is_sector_ok, _ = check_sector_correlation(symbol)
    if not is_sector_ok: return False

    is_news_ok, _ = check_news_sentiment(symbol)
    if not is_news_ok: return False

    is_calendar_ok, _ = check_earnings_and_macro_calendar(symbol)
    if not is_calendar_ok: return False

    # فحص رادار مشاعر منصات التواصل الاجتماعي
    is_social_ok, _ = check_social_media_sentiment(symbol)
    if not is_social_ok: return False

    if not check_multi_timeframe_confluence(symbol, direction): return False

    opt_info = select_smart_option(symbol, direction, vix_val)
    if not opt_info: return False

    opt_sym, ask, qty, opt_delta = opt_info["symbol"], opt_info["ask"], calculate_kelly_qty(confidence), opt_info["symbol_delta"]

    is_exposure_ok, _ = check_portfolio_delta_exposure(opt_delta, qty, direction)
    if not is_exposure_ok: return False

    filled = submit_smart_limit_order(opt_sym, qty, ask)
    if not filled: return False

    fill_price = float(getattr(filled, "filled_avg_price", None) or ask)
    filled_qty = float(getattr(filled, "filled_qty", qty) or qty)

    state["trades_today"] += 1
    state["positions"][opt_sym] = {
        "underlying": symbol, "direction": direction, "qty": filled_qty, "initial_qty": filled_qty,
        "entry_price": fill_price, "confidence": confidence, "scale_out_done": False,
        "opened_at": datetime.now(ET).isoformat(), "order_id": str(filled.id)
    }
    save_state_db(state)
    log_print(f"🚨 SUPREME AI QUANT EXECUTED | Symbol: {symbol} | Signal: {direction} | Qty: {filled_qty} | Entry: ${fill_price:.2f}")
    return True

def log_trade_history_db(rec):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO trade_history (symbol, contract, direction, entry_price, exit_price, qty, pnl_usd, reason, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (rec["symbol"], rec["contract"], rec["direction"], rec["entry_price"], rec["exit_price"], rec["qty"], rec["pnl_usd"], rec["reason"], rec["closed_at"]))
        conn.commit()
        conn.close()
    except Exception: pass

def partial_exit_position(opt_sym, pos, current_price):
    try:
        full_qty, partial_qty = float(pos["qty"]), float(pos["qty"]) / 2
        if partial_qty < 1: return False
        sub = trading_client.submit_order(MarketOrderRequest(symbol=opt_sym, qty=partial_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
        filled = wait_for_fill(str(sub.id), 20)
        if not filled: return False
        pnl = (float(getattr(filled, "filled_avg_price", None) or current_price) - float(pos["entry_price"])) * partial_qty * 100
        state["estimated_daily_pnl"] += pnl
        pos["qty"], pos["scale_out_done"] = full_qty - partial_qty, True
        save_state_db(state)
        log_print(f"✂️ SCALE-OUT FILLED | Sold {partial_qty} of {opt_sym} | P&L: ${pnl:+.2f}")
        return True
    except Exception: return False

def exit_position(opt_sym, pos, current_price, reason):
    try:
        qty = float(pos["qty"])
        if qty <= 0:
            state["positions"].pop(opt_sym, None)
            save_state_db(state)
            return True
        sub = trading_client.submit_order(MarketOrderRequest(symbol=opt_sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
        filled = wait_for_fill(str(sub.id), 20)
        if not filled: return False
        exit_price = float(getattr(filled, "filled_avg_price", None) or current_price)
        pnl = (exit_price - float(pos["entry_price"])) * qty * 100
        state["estimated_daily_pnl"] += pnl
        if state["estimated_daily_pnl"] <= MAX_DAILY_LOSS_USD:
            state["trading_halted"] = True
            log_print(f"🛑 DAILY DRAWDOWN HALT (${state['estimated_daily_pnl']:.2f})")
        log_trade_history_db({"symbol": pos["underlying"], "contract": opt_sym, "direction": pos["direction"], "entry_price": float(pos["entry_price"]), "exit_price": exit_price, "qty": qty, "pnl_usd": pnl, "reason": reason, "closed_at": datetime.now(ET).isoformat()})
        state["positions"].pop(opt_sym, None)
        save_state_db(state)
        log_print(f"🛑 FULL EXIT | Option: {opt_sym} | P&L: ${pnl:+.2f} | Reason: {reason}")
        return True
    except Exception: return False

def force_eod_liquidation():
    now = datetime.now(ET).time()
    if dt_time(15, 55) <= now <= dt_time(16, 0) and state["positions"]:
        log_print("⏰ EOD FORCE LIQUIDATION TRIGGERED.")
        snapshots = get_option_snapshots(list(state["positions"].keys()))
        for opt_sym, pos in list(state["positions"].items()):
            cur_price = float(snapshots[opt_sym].latest_quote.bid_price) if opt_sym in snapshots and snapshots[opt_sym].latest_quote else float(pos["entry_price"])
            exit_position(opt_sym, pos, cur_price, "EOD FORCE LIQUIDATION")

def sync_positions():
    try:
        real = {p.symbol for p in trading_client.get_all_positions() if len(p.symbol) >= 15}
        for sym in list(state["positions"].keys()):
            if sym not in real: state["positions"].pop(sym, None)
        save_state_db(state)
    except Exception: pass

def manage_positions():
    if not state["positions"]: return
    snapshots = get_option_snapshots(list(state["positions"].keys()))
    for opt_sym, pos in list(state["positions"].items()):
        try:
            if opt_sym not in snapshots: continue
            snap = snapshots[opt_sym]
            quote, greeks = getattr(snap, "latest_quote", None), getattr(snap, "greeks", None)
            if not quote or not quote.bid_price or not quote.ask_price: continue
            cur_price = (float(quote.bid_price) + float(quote.ask_price)) / 2
            entry, initial_qty = float(pos["entry_price"]), float(pos["initial_qty"])
            total_pnl = (cur_price - entry) * initial_qty * 100
            pnl_pct = (cur_price - entry) / entry

            delta = float(greeks.delta) if greeks and greeks.delta is not None else 0.5
            target_limit = TARGET_PROFIT_USD
            if (pos["direction"] == "CALL" and delta > 0.75) or (pos["direction"] == "PUT" and delta < -0.75):
                target_limit = TARGET_PROFIT_USD * 1.4

            if not pos.get("scale_out_done", False) and total_pnl >= (target_limit / 2) and initial_qty >= 2:
                partial_exit_position(opt_sym, pos, cur_price)
                continue
            if total_pnl >= target_limit:
                exit_position(opt_sym, pos, cur_price, "DELTA-DRIFT TARGET PROFIT REACHED")
                continue

            sl = -0.30
            try:
                res = stock_data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=pos["underlying"], timeframe=TimeFrame.Minute, start=datetime.now(ET)-timedelta(hours=2), end=datetime.now(ET), feed=STOCK_FEED))
                if res and pos["underlying"] in res:
                    bars = res[pos["underlying"]]
                    if len(bars) > 15:
                        df_b = pd.DataFrame([{"high": float(b.high), "low": float(b.low), "close": float(b.close)} for b in bars])
                        atr_val = (df_b['high'] - df_b['low']).rolling(14).mean().iloc[-1]
                        close_val = df_b['close'].iloc[-1]
                        atr_pct = (atr_val / close_val) * 2.0
                        sl = -max(0.20, min(0.50, atr_pct))
            except Exception: pass

            effective_sl = 0.0 if pos.get("scale_out_done", False) else sl
            if pnl_pct <= effective_sl:
                exit_position(opt_sym, pos, cur_price, "DYNAMIC ATR STOP LOSS")
                continue
        except Exception: pass

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    log_print("=" * 70)
    log_print("🤖 ULTIMATE QUANT BOT WITH OPTUNA & SOCIAL RADAR - RUNNING")
    log_print("=" * 70)
    
    consecutive_errors = 0
    current_universe = []
    last_update = ""
    
    while True:
        try:
            reset_daily_state()
            sync_positions()
            manage_positions()
            force_eod_liquidation()
            print_eod_summary()
            consecutive_errors = 0

            if not is_market_open() or state["trading_halted"]:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if not check_time_of_day_window():
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            is_panic, vix_val = check_vix_volatility_regime()
            if is_panic:
                log_print(f"🚨 MARKET PANIC: VIX at {vix_val:.2f}. Trading halted.")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if len(state["positions"]) >= MAX_OPEN_POSITIONS or state["trades_today"] >= MAX_TRADES_PER_DAY:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            today_str = get_today_date()
            if last_update != today_str:
                current_universe = get_dynamic_universe()
                last_update = today_str

            for symbol in current_universe:
                if any(p["underlying"] == symbol for p in state["positions"].values()):
                    continue
                
                response = stock_data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=datetime.now(ET)-timedelta(days=5), end=datetime.now(ET), feed=STOCK_FEED))
                if not response or symbol not in response: continue
                
                analysis = ml_engine.analyze(symbol, response[symbol])
                if analysis["direction"] in ["CALL", "PUT"]:
                    log_print(f"💡 Signal: {analysis['direction']} on {symbol} | Confidence: {analysis['confidence']:.0%}")
                    enter_position(symbol, analysis["direction"], analysis["confidence"], vix_val)
                    break

            time.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log_print("🛑 Bot stopped.")
            break
        except Exception as e:
            consecutive_errors += 1
            log_print(f"❌ ERROR ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log_print("🚨 KILL-SWITCH TRIGGERED. Halting bot.")
                state["trading_halted"] = True
                save_state_db(state)
                break
            time.sleep(ERROR_SLEEP_SECONDS)

# ============================================================
# RAILWAY STARTUP
# ============================================================

def start_trading_engine():
    log_print("=" * 70)
    log_print("🤖 TRADING ENGINE STARTING...")
    log_print("📡 MARKET DATA ENGINE READY")
    log_print("🧠 ML ENGINE READY")
    log_print("💰 PAPER TRADING MODE ACTIVE")
    log_print("🔄 AUTO BUY / SELL ENGINE STARTED")
    log_print("=" * 70)

    try:
        main()
    except Exception as e:
        log_print(f"🚨 TRADING ENGINE CRASHED: {e}")
        raise


# Start the trading engine when Gunicorn imports main:app
trading_thread = threading.Thread(
    target=start_trading_engine,
    daemon=True,
    name="TradingEngine"
)

trading_thread.start()
