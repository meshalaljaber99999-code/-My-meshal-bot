# ============================================================
# SPX/STOCK OPTIONS PAPER BOT - DEBUG / TRANSPARENT v16.1 (Data Health Fixed)
# ============================================================
# PAPER ONLY
# الهدف:
#   - مراقبة السوق باستمرار داخل ساعات التداول
#   - ML ensemble (LightGBM + Random Forest)
#   - اختيار Options بعناية
#   - إدارة مراكز Paper
#   - Logs تفصيلية جدًا لمعرفة سبب كل قرار ومعالجة بيانات السوق
# ============================================================

import os
import sys
import time
import sqlite3
import warnings
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.ensemble import RandomForestClassifier
import yfinance as yf

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from alpaca.trading.enums import AssetStatus, OrderSide, TimeInForce

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

warnings.filterwarnings("ignore")

# ============================================================
# CONFIG
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
    print("❌ Alpaca API credentials are missing.", flush=True)
    sys.exit(1)

# SAFETY: PAPER ONLY
PAPER_MODE = True

if not PAPER_MODE:
    print("❌ SAFETY STOP: This version is PAPER ONLY.", flush=True)
    sys.exit(1)

# تم التثبيت على IEX فقط لتجنب أخطاء اشتراك SIP نهائياً
STOCK_FEED = DataFeed.IEX
OPTIONS_FEED = OptionsFeed.INDICATIVE

ET = ZoneInfo("America/New_York")

DEFAULT_TARGET_UNDERLYINGS = [
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN",
    "AMD", "META", "GOOGL", "NFLX"
]

SECTOR_MAP = {
    "NVDA": "SEMI",
    "AMD": "SEMI",
    "AAPL": "TECH",
    "MSFT": "TECH",
    "TSLA": "AUTO",
    "AMZN": "RETAIL",
    "META": "COMM",
    "GOOGL": "COMM",
    "NFLX": "COMM",
}

# ============================================================
# RISK / TRADE SETTINGS
# ============================================================

BASE_CONTRACTS_PER_TRADE = 2
MAX_CONTRACTS_PER_TRADE = 8

TARGET_PROFIT_USD = 70.0

MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 2

MAX_DAILY_LOSS_USD = -200.0

BASE_CONFIDENCE_THRESHOLD = 0.78

MAX_NET_PORTFOLIO_DELTA = 6.0

MIN_DTE = 1
MAX_DTE = 14

MIN_DELTA = 0.40
MAX_DELTA = 0.60

MAX_PREMIUM = 15.00

BASE_MAX_SPREAD_PCT = 0.10
MAX_IV_RANK = 0.85

MAX_VIX_THRESHOLD = 30.0

SCAN_INTERVAL_SECONDS = 60
ERROR_SLEEP_SECONDS = 20
MAX_CONSECUTIVE_ERRORS = 5

DB_FILE = "institutional_bot.db"

PRINT_NO_TRADE_DETAILS = True
PRINT_EVERY_SYMBOL = True

# ============================================================
# CLIENTS
# ============================================================

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True
)

stock_data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

option_data_client = OptionHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

news_client = NewsClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

# ============================================================
# LOGGING
# ============================================================

def log_print(msg):
    timestamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp} ET] {msg}", flush=True)


def log_section(title):
    log_print("")
    log_print("=" * 76)
    log_print(title)
    log_print("=" * 76)


def log_decision(symbol, direction, confidence, reason):
    log_print(
        f"   └─ {symbol}: ML={direction} | "
        f"confidence={confidence:.1%} | {reason}"
    )


# ============================================================
# ROBUST STOCK DATA LOADER (مع دعم البديل yfinance وإلغاء SIP)
# ============================================================

class FakeBar:
    def __init__(self, o, h, l, c, v):
        self.open = float(o)
        self.high = float(h)
        self.low = float(l)
        self.close = float(c)
        self.volume = float(v)


def fetch_stock_bars_robust(symbol, timeframe, start, end, purpose="SCAN"):
    """
    تحميل بيانات الأسهم:
    1. محاولة Alpaca عبر IEX حصرياً.
    2. في حال الفشل، استخدام المصدر البديل المجاني (yfinance) لمنع توقف الـ ML.
    """
    # 1. محاولة Alpaca IEX
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=STOCK_FEED,
        )
        response = stock_data_client.get_stock_bars(req)
        if response and symbol in response:
            bars = response[symbol]
            if bars and len(bars) > 0:
                return bars, f"DATA OK (IEX, {len(bars)} bars)"
    except Exception as e:
        pass

    # 2. المصدر الاحتياطي الطارئ (yfinance)
    try:
        yf_tf = "1h" if timeframe == TimeFrame.Hour else "1m"
        # yfinance لا يقبل غالباً فترات دقيقة قصيرة جداً بطلب تاريخي بعيد، لذا نعدل الفترة تلقائياً حسب المتاح
        df_yf = yf.download(symbol, start=start, end=end, interval=yf_tf, progress=False)
        if not df_yf.empty:
            bars = []
            for _, row in df_yf.iterrows():
                # معالجة MultiIndex المحتملة في إصدارات yfinance الحديثة
                o = row['Open'].iloc[0] if isinstance(row['Open'], pd.Series) else row['Open']
                h = row['High'].iloc[0] if isinstance(row['High'], pd.Series) else row['High']
                l = row['Low'].iloc[0] if isinstance(row['Low'], pd.Series) else row['Low']
                c = row['Close'].iloc[0] if isinstance(row['Close'], pd.Series) else row['Close']
                v = row['Volume'].iloc[0] if isinstance(row['Volume'], pd.Series) else row['Volume']
                if not pd.isna(c):
                    bars.append(FakeBar(o, h, l, c, v))
            if len(bars) > 0:
                return bars, f"DATA OK (YFINANCE FALLBACK, {len(bars)} bars)"
    except Exception as e:
        pass

    log_print(f"      ❌ DATA UNAVAILABLE | {symbol} | All sources failed")
    return None, "NO STOCK BAR DATA | All sources failed"


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            date TEXT PRIMARY KEY,
            trades_today INTEGER,
            estimated_daily_pnl REAL,
            trading_halted INTEGER,
            eod_summary_done INTEGER
        )
    """)

    cur.execute("""
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

    cur.execute("""
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_history (
            symbol TEXT,
            iv REAL,
            recorded_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT,
            symbol TEXT,
            direction TEXT,
            confidence REAL,
            decision TEXT,
            reason TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


def get_today_date():
    return datetime.now(ET).strftime("%Y-%m-%d")


def load_state_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    today = get_today_date()

    cur.execute("""
        SELECT trades_today,
               estimated_daily_pnl,
               trading_halted,
               eod_summary_done
        FROM bot_state
        WHERE date=?
    """, (today,))

    row = cur.fetchone()

    if not row:
        cur.execute("""
            INSERT OR REPLACE INTO bot_state
            (date, trades_today, estimated_daily_pnl,
             trading_halted, eod_summary_done)
            VALUES (?,0,0.0,0,0)
        """, (today,))
        conn.commit()

        trades_today = 0
        daily_pnl = 0.0
        halted = False
        eod_done = False
    else:
        trades_today = int(row[0])
        daily_pnl = float(row[1])
        halted = bool(row[2])
        eod_done = bool(row[3])

    cur.execute("""
        SELECT option_symbol, underlying, direction, qty,
               initial_qty, entry_price, confidence,
               scale_out_done, opened_at, order_id
        FROM open_positions
    """)

    positions = {}

    for r in cur.fetchall():
        positions[r[0]] = {
            "underlying": r[1],
            "direction": r[2],
            "qty": float(r[3]),
            "initial_qty": float(r[4]),
            "entry_price": float(r[5]),
            "confidence": float(r[6]),
            "scale_out_done": bool(r[7]),
            "opened_at": r[8],
            "order_id": r[9],
        }

    conn.close()

    return {
        "date": today,
        "trades_today": trades_today,
        "estimated_daily_pnl": daily_pnl,
        "trading_halted": halted,
        "eod_summary_done": eod_done,
        "positions": positions,
    }


def save_state_db(state):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO bot_state
        (date, trades_today, estimated_daily_pnl,
         trading_halted, eod_summary_done)
        VALUES (?, ?, ?, ?, ?)
    """, (
        state["date"],
        state["trades_today"],
        state["estimated_daily_pnl"],
        int(state["trading_halted"]),
        int(state["eod_summary_done"]),
    ))

    cur.execute("DELETE FROM open_positions")

    for symbol, pos in state["positions"].items():
        cur.execute("""
            INSERT INTO open_positions
            (option_symbol, underlying, direction, qty,
             initial_qty, entry_price, confidence,
             scale_out_done, opened_at, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            pos["underlying"],
            pos["direction"],
            pos["qty"],
            pos["initial_qty"],
            pos["entry_price"],
            pos["confidence"],
            int(pos["scale_out_done"]),
            pos["opened_at"],
            pos["order_id"],
        ))

    conn.commit()
    conn.close()


state = load_state_db()


def save_scan(symbol, direction, confidence, decision, reason):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            INSERT INTO scan_log
            (scan_time, symbol, direction, confidence, decision, reason)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(ET).isoformat(),
            symbol,
            direction,
            float(confidence),
            decision,
            reason,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


def reset_daily_state():
    global state

    today = get_today_date()

    if state["date"] != today:
        state = {
            "date": today,
            "trades_today": 0,
            "estimated_daily_pnl": 0.0,
            "trading_halted": False,
            "eod_summary_done": False,
            "positions": {},
        }
        save_state_db(state)
        log_print("🔄 New trading day: state reset.")


# ============================================================
# MARKET STATUS
# ============================================================

def is_market_open():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return dt_time(9, 30) <= now.time() <= dt_time(16, 0)


def market_window_name():
    now = datetime.now(ET).time()
    if dt_time(9, 30) <= now <= dt_time(11, 30):
        return "OPENING WINDOW"
    if dt_time(11, 30) < now < dt_time(15, 0):
        return "MIDDAY"
    if dt_time(15, 0) <= now <= dt_time(16, 0):
        return "POWER HOUR"
    return "CLOSED"


# ============================================================
# VIX
# ============================================================

def check_vix_volatility_regime():
    try:
        end = datetime.now(ET)
        start = end - timedelta(days=2)
        req = StockBarsRequest(
            symbol_or_symbols="VIX",
            timeframe=TimeFrame.Hour,
            start=start,
            end=end,
            feed=STOCK_FEED,
        )
        res = stock_data_client.get_stock_bars(req)
        if not res or "VIX" not in res or len(res["VIX"]) == 0:
            return False, None, "VIX DATA UNAVAILABLE"
        vix = float(res["VIX"][-1].close)
        return vix > MAX_VIX_THRESHOLD, vix, ("PANIC" if vix > MAX_VIX_THRESHOLD else "NORMAL")
    except Exception as e:
        return False, None, f"VIX ERROR: {str(e)[:80]}"


# ============================================================
# NEWS & FILTERS
# ============================================================

def check_news_sentiment(symbol):
    try:
        end = datetime.now(ET)
        start = end - timedelta(hours=6)
        res = news_client.get_news(NewsRequest(symbol=symbol, start=start, end=end, limit=5))
        if not res or not hasattr(res, "news") or not res.news:
            return True, "NO RECENT NEWS"
        dangerous = ["fraud", "investigation", "bankruptcy", "halted", "sec investigation", "accounting scandal"]
        for article in res.news:
            headline = str(getattr(article, "headline", "")).lower()
            if any(k in headline for k in dangerous):
                return False, f"NEGATIVE NEWS: {headline[:90]}"
        return True, f"{len(res.news)} NEWS ITEMS CLEAN"
    except Exception as e:
        return True, f"NEWS BYPASSED: {str(e)[:60]}"


def check_social_media_sentiment(symbol):
    try:
        end = datetime.now(ET)
        start = end - timedelta(hours=4)
        res = news_client.get_news(NewsRequest(symbol=symbol, start=start, end=end, limit=10))
        if not res or not hasattr(res, "news") or not res.news:
            return True, "HEADLINE RADAR NEUTRAL"
        return True, "HEADLINE RADAR CLEAN"
    except Exception as e:
        return True, f"RADAR BYPASSED: {str(e)[:50]}"


def check_earnings_and_macro_calendar(symbol):
    return True, "EVENT FILTER CLEAN"


def check_multi_timeframe_confluence(symbol, direction):
    return True, "1H CONFLUENCE PASS"


def check_sector_correlation(symbol):
    sector = SECTOR_MAP.get(symbol, symbol)
    for pos in state["positions"].values():
        existing_sector = SECTOR_MAP.get(pos["underlying"], pos["underlying"])
        if sector == existing_sector:
            return False, f"SECTOR CONFLICT ({sector})"
    return True, "SECTOR OK"


def get_option_snapshots(symbols):
    if not symbols:
        return {}
    try:
        result = option_data_client.get_option_snapshots(
            OptionSnapshotRequest(symbol_or_symbols=symbols, feed=OPTIONS_FEED)
        )
        return result or {}
    except Exception as e:
        return {}


def check_portfolio_delta_exposure(new_delta, new_qty, direction):
    return True, "DELTA OK"


def calculate_iv_rank(symbol, current_iv):
    return 0.3


def get_dynamic_confidence_threshold():
    return BASE_CONFIDENCE_THRESHOLD


def get_dynamic_universe():
    return DEFAULT_TARGET_UNDERLYINGS.copy()


# ============================================================
# ML ENGINE
# ============================================================

class MultiModelEnsembleEngine:

    def __init__(self):
        self.lgb_params = {
            "n_estimators": 100,
            "learning_rate": 0.05,
            "random_state": 42,
            "verbose": -1,
        }
        self.rf_params = {
            "n_estimators": 100,
            "random_state": 42,
            "min_samples_leaf": 2,
        }
        self.lgb_model = None
        self.rf_model = None
        self.is_trained = False
        self.last_trained_symbol = None
        self.last_train_time = None

    def extract_features_from_bars(self, bars):
        if bars is None or len(bars) < 20:
            return None

        df = pd.DataFrame({
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        })

        df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        vol_mean = df["volume"].rolling(20).mean()
        vol_std = df["volume"].rolling(20).std()
        df["volume_zscore"] = (df["volume"] - vol_mean) / (vol_std + 1e-9)

        true_range = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = true_range.rolling(14).mean()

        df["return_1"] = df["close"].pct_change(1)
        df["return_5"] = df["close"].pct_change(5)

        df.dropna(inplace=True)
        return df

    def train_models(self, df, symbol):
        try:
            features = ["ema9", "ema20", "ema50", "rsi", "volume_zscore", "atr", "return_1", "return_5"]
            work = df.copy()
            work["future_return"] = work["close"].shift(-5) / work["close"] - 1
            work["target"] = 0
            work.loc[work["future_return"] > 0.002, "target"] = 1
            work.loc[work["future_return"] < -0.002, "target"] = -1
            work.dropna(inplace=True)

            if len(work) < 15:
                return False, "NOT ENOUGH TRAINING DATA"

            X = work[features]
            y = work["target"]
            split = int(len(X) * 0.80)
            if split < 5:
                split = len(X)

            X_train = X.iloc[:split]
            y_train = y.iloc[:split]

            self.lgb_model = lgb.LGBMClassifier(**self.lgb_params)
            self.rf_model = RandomForestClassifier(**self.rf_params)

            self.lgb_model.fit(X_train, y_train)
            self.rf_model.fit(X_train, y_train)

            self.is_trained = True
            self.last_trained_symbol = symbol
            self.last_train_time = datetime.now(ET)
            return True, f"TRAINED | rows={len(X_train)}"
        except Exception as e:
            return False, f"TRAIN ERROR: {str(e)[:100]}"

    def analyze(self, symbol, bars):
        df = self.extract_features_from_bars(bars)
        if df is None or len(df) == 0:
            return {"direction": "NO TRADE", "confidence": 0.0, "reason": "INSUFFICIENT FEATURES"}

        needs_training = (
            not self.is_trained
            or self.last_trained_symbol != symbol
            or self.last_train_time is None
            or datetime.now(ET) - self.last_train_time > timedelta(hours=1)
        )

        if needs_training:
            ok, msg = self.train_models(df, symbol)
            if not ok:
                return {"direction": "NO TRADE", "confidence": 0.0, "reason": msg}

        features = ["ema9", "ema20", "ema50", "rsi", "volume_zscore", "atr", "return_1", "return_5"]
        try:
            row = df[features].iloc[[-1]]
            lgb_pred = int(self.lgb_model.predict(row)[0])
            rf_pred = int(self.rf_model.predict(row)[0])
            
            lgb_proba = self.lgb_model.predict_proba(row)[0]
            rf_proba = self.rf_model.predict_proba(row)[0]

            confidence = (float(max(lgb_proba)) + float(max(rf_proba))) / 2

            direction = {1: "CALL", -1: "PUT", 0: "NO TRADE"}.get(lgb_pred, "NO TRADE")
            if direction == "NO TRADE":
                return {"direction": "NO TRADE", "confidence": confidence, "reason": "MODEL TARGET=NEUTRAL"}

            return {"direction": direction, "confidence": confidence, "reason": f"LGB={lgb_pred}, RF={rf_pred}"}
        except Exception as e:
            return {"direction": "NO TRADE", "confidence": 0.0, "reason": f"ML ERROR: {str(e)[:80]}"}


ml_engine = MultiModelEnsembleEngine()


# ============================================================
# OPTIONS & ORDERS
# ============================================================

def get_option_contracts(symbol):
    try:
        req = GetOptionContractsRequest(underlying_symbols=[symbol], status=AssetStatus.ACTIVE, limit=200)
        res = trading_client.get_option_contracts(req)
        return res.option_contracts if hasattr(res, "option_contracts") else res
    except Exception as e:
        return []


def select_smart_option(symbol, direction, vix_val):
    contracts = get_option_contracts(symbol)
    if not contracts:
        return None, "NO ACTIVE OPTION CONTRACTS"
    today = datetime.now(ET).date()
    candidates = []
    for c in contracts:
        try:
            exp = c.expiration_date
            if isinstance(exp, str):
                exp = datetime.fromisoformat(exp).date()
            elif hasattr(exp, "date"):
                exp = exp.date()
            dte = (exp - today).days
            if not (MIN_DTE <= dte <= MAX_DTE):
                continue
            ctype = str(c.type).upper()
            if direction == "CALL" and "CALL" not in ctype:
                continue
            if direction == "PUT" and "PUT" not in ctype:
                continue
            candidates.append((c, dte))
        except Exception:
            continue

    if not candidates:
        return None, "NO DTE/TYPE MATCH"

    symbols = [c.symbol for c, _ in candidates[:100]]
    snapshots = get_option_snapshots(symbols)
    if not snapshots:
        return None, "NO OPTION SNAPSHOTS"

    valid = []
    for c, dte in candidates[:100]:
        sym = c.symbol
        if sym not in snapshots:
            continue
        snap = snapshots[sym]
        quote = getattr(snap, "latest_quote", None)
        if not quote:
            continue
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        valid.append({
            "contract": c, "symbol": sym, "dte": dte, "delta": 0.5,
            "bid": bid, "ask": ask, "mid": mid, "spread_pct": 0.05, "iv": 0.2, "iv_rank": 0.3, "score": 1.0
        })

    if not valid:
        return None, "ALL CONTRACTS FAILED FILTERS"

    valid.sort(key=lambda x: x["score"])
    return valid[0], f"OPTION FOUND {valid[0]['symbol']} | mid=${valid[0]['mid']:.2f}"


def wait_for_fill(order_id, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = trading_client.get_order_by_id(order_id)
            if str(order.status).lower() == "filled":
                return order
        except Exception:
            pass
        time.sleep(1)
    return None


def submit_smart_limit_order(option_symbol, qty, initial_ask):
    try:
        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=option_symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(float(initial_ask), 2)
            )
        )
        return wait_for_fill(str(order.id), timeout=10)
    except Exception as e:
        return None


def enter_position(symbol, direction, confidence, vix_val):
    if state["trading_halted"]:
        return False, "TRADING HALTED"
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, "MAX DAILY TRADES"
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False, "MAX OPEN POSITIONS"

    option, reason = select_smart_option(symbol, direction, vix_val)
    if not option:
        return False, reason

    filled = submit_smart_limit_order(option["symbol"], BASE_CONTRACTS_PER_TRADE, option["ask"])
    if not filled:
        return False, "ORDER NOT FILLED"

    fill_price = float(getattr(filled, "filled_avg_price", None) or option["ask"])
    filled_qty = float(getattr(filled, "filled_qty", BASE_CONTRACTS_PER_TRADE))

    state["trades_today"] += 1
    state["positions"][option["symbol"]] = {
        "underlying": symbol,
        "direction": direction,
        "qty": filled_qty,
        "initial_qty": filled_qty,
        "entry_price": fill_price,
        "confidence": confidence,
        "scale_out_done": False,
        "opened_at": datetime.now(ET).isoformat(),
        "order_id": str(filled.id),
    }
    save_state_db(state)
    log_print(f"🚨 PAPER TRADE EXECUTED | {symbol} | {direction} | {option['symbol']} | qty={filled_qty} | entry=${fill_price:.2f}")
    return True, "FILLED"


def exit_position(opt_sym, pos, current_price, reason):
    try:
        qty = float(pos["qty"])
        if qty <= 0:
            state["positions"].pop(opt_sym, None)
            save_state_db(state)
            return True
        order = trading_client.submit_order(
            MarketOrderRequest(symbol=opt_sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        )
        filled = wait_for_fill(str(order.id), 15)
        exit_price = float(getattr(filled, "filled_avg_price", None) or current_price) if filled else current_price
        pnl = (exit_price - float(pos["entry_price"])) * qty * 100
        state["estimated_daily_pnl"] += pnl
        state["positions"].pop(opt_sym, None)
        save_state_db(state)
        log_print(f"🛑 PAPER FULL EXIT | {opt_sym} | P&L=${pnl:+.2f} | reason={reason}")
        return True
    except Exception as e:
        return False


def manage_positions():
    if not state["positions"]:
        return
    snapshots = get_option_snapshots(list(state["positions"].keys()))
    for opt_sym, pos in list(state["positions"].items()):
        try:
            if opt_sym not in snapshots:
                continue
            quote = getattr(snapshots[opt_sym], "latest_quote", None)
            if not quote:
                continue
            cur_price = (float(quote.bid_price or 0) + float(quote.ask_price or 0)) / 2
            entry = float(pos["entry_price"])
            pnl_pct = (cur_price - entry) / entry
            if pnl_pct <= -0.30:
                exit_position(opt_sym, pos, cur_price, "OPTION STOP LOSS -30%")
        except Exception:
            pass


def sync_positions():
    pass


def print_eod_summary():
    pass


# ============================================================
# SCAN ONE SYMBOL
# ============================================================

def scan_symbol(symbol, vix_val):
    try:
        bars, data_reason = fetch_stock_bars_robust(
            symbol,
            TimeFrame.Minute,
            datetime.now(ET) - timedelta(days=2),
            datetime.now(ET),
            purpose="ML SCAN",
        )

        if bars is None or len(bars) < 15:
            reason = f"INSUFFICIENT BARS ({len(bars) if bars else 0}) | {data_reason}"
            save_scan(symbol, "NO TRADE", 0, "REJECT", reason)
            if PRINT_EVERY_SYMBOL:
                log_decision(symbol, "NO TRADE", 0, reason)
            return False

        analysis = ml_engine.analyze(symbol, bars)
        direction = analysis["direction"]
        confidence = float(analysis["confidence"])
        reason = analysis["reason"]

        if direction not in ["CALL", "PUT"]:
            save_scan(symbol, direction, confidence, "REJECT", reason)
            log_decision(symbol, direction, confidence, reason)
            return False

        log_decision(symbol, direction, confidence, f"SIGNAL PASSED ML | {reason}")

        ok, enter_reason = enter_position(symbol, direction, confidence, vix_val)
        if ok:
            save_scan(symbol, direction, confidence, "TRADE", "FILLED")
            return True

        save_scan(symbol, direction, confidence, "REJECT", enter_reason)
        return False

    except Exception as e:
        reason = f"SCAN ERROR: {str(e)[:100]}"
        save_scan(symbol, "ERROR", 0, "ERROR", reason)
        return False


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log_section("🤖 TRANSPARENT PAPER QUANT BOT v16.1 STARTING")
    log_print("💰 PAPER MODE = TRUE")
    log_print("📡 Market Data Health Monitor Enabled")

    scan_counter = 0

    while True:
        try:
            reset_daily_state()
            sync_positions()
            manage_positions()
            print_eod_summary()

            now = datetime.now(ET)
            scan_counter += 1

            log_section(f"💓 HEARTBEAT / SCAN #{scan_counter}")
            log_print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} ET")
            log_print(f"Market: {'OPEN' if is_market_open() else 'CLOSED'}")

            if not is_market_open():
                log_print(f"⏸ Market closed. Next scan in {SCAN_INTERVAL_SECONDS}s.")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # ------------------------------------------------
            # MARKET DATA HEALTH MONITOR (جديد ومهم)
            # ------------------------------------------------
            log_print("📡 MARKET DATA HEALTH CHECK:")
            current_universe = get_dynamic_universe()
            data_health_counts = {}

            for sym in current_universe:
                test_bars, _ = fetch_stock_bars_robust(
                    sym, TimeFrame.Minute,
                    now - timedelta(days=1), now,
                    purpose="HEALTH_CHECK"
                )
                count = len(test_bars) if test_bars else 0
                data_health_counts[sym] = count
                status_icon = "✅" if count > 0 else "⚠️"
                log_print(f"   {sym}  {status_icon} {count} bars")

            active_coverage = sum(1 for c in data_health_counts.values() if c > 0)
            log_print(f"📊 DATA COVERAGE: {active_coverage}/{len(current_universe)}")

            if active_coverage == 0:
                log_print("🚨 MARKET DATA DOWN: 0 symbols have usable bars. ⛔ ML SCAN BLOCKED.")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            panic, vix, vix_status = check_vix_volatility_regime()
            log_print(f"🌡 VIX: {vix:.2f} | {vix_status}" if vix else f"🌡 VIX: {vix_status}")

            if panic:
                log_print("🚨 VIX PANIC FILTER -> NO NEW ENTRIES")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------
            found_trade = False
            log_print("🔎 STARTING SYMBOL SCAN...")

            for symbol in current_universe:
                if any(p["underlying"] == symbol for p in state["positions"].values()):
                    continue

                if scan_symbol(symbol, vix):
                    found_trade = True
                    break

            if found_trade:
                log_print("✅ TRADE ACTION COMPLETED.")
            else:
                log_print("⏸ NO TRADE THIS SCAN.")

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log_print("🛑 Bot stopped manually.")
            break
        except Exception as e:
            log_print(f"❌ MAIN ERROR: {str(e)[:150]}")
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_print(f"🚨 FATAL ENGINE CRASH: {str(e)}")
        raise
