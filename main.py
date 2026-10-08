# ============================================================
# SPX/STOCK OPTIONS PAPER BOT - DEBUG / TRANSPARENT v16
# ============================================================
# PAPER ONLY
# الهدف:
#   - مراقبة السوق باستمرار داخل ساعات التداول
#   - ML ensemble (LightGBM + Random Forest)
#   - اختيار Options بعناية
#   - إدارة مراكز Paper
#   - Logs تفصيلية جدًا لمعرفة سبب كل قرار
#
# IMPORTANT:
#   هذا النظام لا يضمن الربح. Paper trading فقط.
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

# Logs:
# heartbeat always appears
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
# ROBUST STOCK DATA LOADER
# ============================================================
def fetch_stock_bars_robust(symbol, timeframe, start, end, purpose="SCAN"):
    """
    Robust Alpaca stock-bar loader.
    Tries the configured feed first, then a default-feed request, then a
    shorter lookback. Never hides the actual API error from the logs.
    """
    attempts = []

    feeds = [STOCK_FEED, None]
    sip = getattr(DataFeed, "SIP", None)
    if sip is not None and sip not in feeds:
        feeds.append(sip)

    # Keep the original window first; then retry with a shorter window.
    windows = [(start, end), (max(start, end - timedelta(days=2)), end)]

    for window_start, window_end in windows:
        for feed in feeds:
            try:
                kwargs = dict(
                    symbol_or_symbols=symbol,
                    timeframe=timeframe,
                    start=window_start,
                    end=window_end,
                )
                if feed is not None:
                    kwargs["feed"] = feed

                feed_name = str(feed) if feed is not None else "DEFAULT"
                log_print(
                    f"      📡 DATA TRY | {purpose} | {symbol} | "
                    f"{timeframe} | feed={feed_name}"
                )

                response = stock_data_client.get_stock_bars(
                    StockBarsRequest(**kwargs)
                )

                if response and symbol in response:
                    bars = response[symbol]
                    if bars and len(bars) > 0:
                        log_print(
                            f"      ✅ DATA OK | {symbol} | bars={len(bars)} | "
                            f"feed={feed_name}"
                        )
                        return bars, f"DATA OK ({feed_name}, {len(bars)} bars)"

                attempts.append(f"{feed_name}: empty response")

            except Exception as e:
                msg = str(e).replace("\n", " ")[:180]
                attempts.append(f"{feed_name}: {msg}")
                log_print(
                    f"      ⚠️ DATA FAIL | {purpose} | {symbol} | "
                    f"feed={feed_name} | {msg}"
                )

    reason = " | ".join(attempts[-6:]) if attempts else "unknown data error"
    log_print(f"      ❌ DATA UNAVAILABLE | {symbol} | {reason}")
    return None, f"NO STOCK BAR DATA | {reason}"


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


def check_time_of_day_window():
    """
    Original code silently skipped 11:30-15:00.
    This version scans the entire regular session.

    We still identify opening/midday/power-hour,
    but do not silently stop scanning.
    """
    now = datetime.now(ET).time()
    return dt_time(9, 30) <= now <= dt_time(16, 0)


# ============================================================
# VIX
# ============================================================

def check_vix_volatility_regime():
    """
    VIX is attempted through Alpaca.
    If unavailable, we DO NOT pretend that VIX=0 is real.
    """
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

        return (
            vix > MAX_VIX_THRESHOLD,
            vix,
            "PANIC" if vix > MAX_VIX_THRESHOLD else "NORMAL"
        )

    except Exception as e:
        return False, None, f"VIX ERROR: {str(e)[:80]}"


# ============================================================
# NEWS
# ============================================================

def check_news_sentiment(symbol):
    try:
        end = datetime.now(ET)
        start = end - timedelta(hours=6)

        req = NewsRequest(
            symbol=symbol,
            start=start,
            end=end,
            limit=5
        )

        res = news_client.get_news(req)

        if not res or not hasattr(res, "news") or not res.news:
            return True, "NO RECENT NEWS"

        dangerous = [
            "fraud",
            "investigation",
            "bankruptcy",
            "halted",
            "sec investigation",
            "accounting scandal",
        ]

        for article in res.news:
            headline = str(
                getattr(article, "headline", "")
            ).lower()

            if any(k in headline for k in dangerous):
                return False, f"NEGATIVE NEWS: {headline[:90]}"

        return True, f"{len(res.news)} NEWS ITEMS CLEAN"

    except Exception as e:
        return True, f"NEWS BYPASSED: {str(e)[:60]}"


def check_social_media_sentiment(symbol):
    """
    NOTE:
    Alpaca NewsClient is NOT X/Reddit.
    This is headline sentiment only.
    """
    try:
        end = datetime.now(ET)
        start = end - timedelta(hours=4)

        res = news_client.get_news(
            NewsRequest(
                symbol=symbol,
                start=start,
                end=end,
                limit=10
            )
        )

        if not res or not hasattr(res, "news") or not res.news:
            return True, "HEADLINE RADAR NEUTRAL"

        panic_words = [
            "dump",
            "crash",
            "collapse",
            "bloodbath",
            "bankruptcy",
        ]

        score = 0

        for article in res.news:
            h = str(
                getattr(article, "headline", "")
            ).lower()

            if any(k in h for k in panic_words):
                score -= 1

        if score <= -2:
            return False, "HEADLINE PANIC"

        return True, "HEADLINE RADAR CLEAN"

    except Exception as e:
        return True, f"RADAR BYPASSED: {str(e)[:50]}"


def check_earnings_and_macro_calendar(symbol):
    try:
        end = datetime.now(ET)

        res = news_client.get_news(
            NewsRequest(
                symbol=symbol,
                start=end - timedelta(hours=24),
                end=end,
                limit=10
            )
        )

        if not res or not hasattr(res, "news") or not res.news:
            return True, "NO EVENT HEADLINES"

        keywords = [
            "earnings release",
            "quarterly results",
            "cpi report",
            "fomc meeting",
        ]

        for article in res.news:
            h = str(
                getattr(article, "headline", "")
            ).lower()

            if any(k in h for k in keywords):
                return False, f"EVENT HEADLINE: {h[:100]}"

        return True, "EVENT FILTER CLEAN"

    except Exception as e:
        return True, f"CALENDAR BYPASSED: {str(e)[:50]}"


# ============================================================
# TECHNICAL CONFLUENCE
# ============================================================

def check_multi_timeframe_confluence(symbol, direction):
    try:
        end = datetime.now(ET)

        res = stock_data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Hour,
                start=end - timedelta(days=7),
                end=end,
                feed=STOCK_FEED,
            )
        )

        if not res or symbol not in res:
            return True, "1H DATA UNAVAILABLE - BYPASSED"

        bars = res[symbol]

        if len(bars) < 20:
            return True, "INSUFFICIENT 1H DATA - BYPASSED"

        df = pd.DataFrame({
            "close": [float(b.close) for b in bars]
        })

        df["ema20"] = df["close"].ewm(
            span=20,
            adjust=False
        ).mean()

        close = float(df["close"].iloc[-1])
        ema = float(df["ema20"].iloc[-1])

        if direction == "CALL" and close < ema:
            return False, f"1H BELOW EMA20 ({close:.2f} < {ema:.2f})"

        if direction == "PUT" and close > ema:
            return False, f"1H ABOVE EMA20 ({close:.2f} > {ema:.2f})"

        return True, f"1H CONFLUENCE PASS ({close:.2f} vs EMA {ema:.2f})"

    except Exception as e:
        return True, f"MTF BYPASSED: {str(e)[:60]}"


# ============================================================
# PORTFOLIO / RISK
# ============================================================

def check_sector_correlation(symbol):
    sector = SECTOR_MAP.get(symbol, symbol)

    for pos in state["positions"].values():
        existing_sector = SECTOR_MAP.get(
            pos["underlying"],
            pos["underlying"]
        )

        if sector == existing_sector:
            return False, f"SECTOR CONFLICT ({sector})"

    return True, "SECTOR OK"


def get_option_snapshots(symbols):
    if not symbols:
        return {}

    try:
        result = option_data_client.get_option_snapshots(
            OptionSnapshotRequest(
                symbol_or_symbols=symbols,
                feed=OPTIONS_FEED
            )
        )

        return result or {}

    except Exception as e:
        log_print(f"⚠️ Option snapshot error: {str(e)[:120]}")
        return {}


def check_portfolio_delta_exposure(
    new_delta,
    new_qty,
    direction
):
    try:
        total = 0.0

        if state["positions"]:
            snapshots = get_option_snapshots(
                list(state["positions"].keys())
            )

            for symbol, pos in state["positions"].items():
                delta = 0.5

                if (
                    symbol in snapshots
                    and snapshots[symbol].greeks
                    and snapshots[symbol].greeks.delta is not None
                ):
                    delta = abs(
                        float(snapshots[symbol].greeks.delta)
                    )

                sign = 1 if pos["direction"] == "CALL" else -1

                total += delta * float(pos["qty"]) * sign

        new_sign = 1 if direction == "CALL" else -1

        projected = total + (
            float(new_delta) * float(new_qty) * new_sign
        )

        if abs(projected) > MAX_NET_PORTFOLIO_DELTA:
            return False, (
                f"DELTA CAP: current={total:.2f}, "
                f"projected={projected:.2f}"
            )

        return True, f"DELTA OK: projected={projected:.2f}"

    except Exception as e:
        return True, f"DELTA CHECK BYPASSED: {str(e)[:50]}"


def calculate_iv_rank(symbol, current_iv):
    try:
        if current_iv is None or current_iv <= 0:
            return 0.3

        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO iv_history
            (symbol, iv, recorded_at)
            VALUES (?, ?, ?)
        """, (
            symbol,
            float(current_iv),
            datetime.now(ET).isoformat()
        ))

        rows = conn.execute("""
            SELECT iv
            FROM iv_history
            WHERE symbol=?
            ORDER BY recorded_at DESC
            LIMIT 30
        """, (symbol,)).fetchall()

        conn.commit()
        conn.close()

        if len(rows) < 3:
            return 0.3

        values = [float(r[0]) for r in rows]

        lo = min(values)
        hi = max(values)

        if hi <= lo:
            return 0.3

        return (
            float(current_iv) - lo
        ) / (hi - lo)

    except Exception:
        return 0.3


def get_dynamic_confidence_threshold():
    try:
        conn = sqlite3.connect(DB_FILE)

        rows = conn.execute("""
            SELECT pnl_usd
            FROM trade_history
            ORDER BY id DESC
            LIMIT 3
        """).fetchall()

        conn.close()

        if not rows:
            return BASE_CONFIDENCE_THRESHOLD

        pnls = [float(r[0]) for r in rows]

        if sum(x < 0 for x in pnls) >= 2:
            return min(
                0.86,
                BASE_CONFIDENCE_THRESHOLD + 0.06
            )

        if all(x > 0 for x in pnls):
            return max(
                0.75,
                BASE_CONFIDENCE_THRESHOLD - 0.02
            )

        return BASE_CONFIDENCE_THRESHOLD

    except Exception:
        return BASE_CONFIDENCE_THRESHOLD


# ============================================================
# DYNAMIC UNIVERSE
# ============================================================

def get_dynamic_universe():
    pool = DEFAULT_TARGET_UNDERLYINGS.copy()

    try:
        end = datetime.now(ET)

        volumes = {}

        for symbol in pool:
            bars, _ = fetch_stock_bars_robust(
                symbol, TimeFrame.Hour,
                end - timedelta(days=3), end,
                purpose="UNIVERSE",
            )
            if not bars:
                continue
            volumes[symbol] = sum(float(b.volume) for b in bars)

        ranked = sorted(
            volumes,
            key=volumes.get,
            reverse=True
        )

        top = ranked[:7]

        return top if len(top) >= 3 else pool

    except Exception as e:
        log_print(
            f"⚠️ Universe fallback: {str(e)[:100]}"
        )
        return pool


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
        if bars is None or len(bars) < 80:
            return None

        df = pd.DataFrame({
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        })

        df["ema9"] = df["close"].ewm(
            span=9,
            adjust=False
        ).mean()

        df["ema20"] = df["close"].ewm(
            span=20,
            adjust=False
        ).mean()

        df["ema50"] = df["close"].ewm(
            span=50,
            adjust=False
        ).mean()

        delta = df["close"].diff()

        gain = delta.where(
            delta > 0,
            0
        ).rolling(14).mean()

        loss = (-delta.where(
            delta < 0,
            0
        )).rolling(14).mean()

        rs = gain / (loss + 1e-9)

        df["rsi"] = 100 - (
            100 / (1 + rs)
        )

        vol_mean = df["volume"].rolling(20).mean()
        vol_std = df["volume"].rolling(20).std()

        df["volume_zscore"] = (
            df["volume"] - vol_mean
        ) / (vol_std + 1e-9)

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
            features = [
                "ema9",
                "ema20",
                "ema50",
                "rsi",
                "volume_zscore",
                "atr",
                "return_1",
                "return_5",
            ]

            work = df.copy()

            work["future_return"] = (
                work["close"].shift(-5)
                / work["close"]
                - 1
            )

            work["target"] = 0

            work.loc[
                work["future_return"] > 0.003,
                "target"
            ] = 1

            work.loc[
                work["future_return"] < -0.003,
                "target"
            ] = -1

            work.dropna(inplace=True)

            if len(work) < 60:
                return False, "NOT ENOUGH TRAINING DATA"

            X = work[features]
            y = work["target"]

            # Need all classes for a meaningful 3-way classifier.
            if len(set(y.tolist())) < 3:
                return False, "TRAINING DATA HAS <3 CLASSES"

            split = int(len(X) * 0.80)

            if split < 40 or len(X) - split < 10:
                return False, "TRAIN/VALIDATION SPLIT TOO SMALL"

            X_train = X.iloc[:split]
            y_train = y.iloc[:split]

            self.lgb_model = lgb.LGBMClassifier(
                **self.lgb_params
            )

            self.rf_model = RandomForestClassifier(
                **self.rf_params
            )

            self.lgb_model.fit(
                X_train,
                y_train
            )

            self.rf_model.fit(
                X_train,
                y_train
            )

            self.is_trained = True
            self.last_trained_symbol = symbol
            self.last_train_time = datetime.now(ET)

            counts = y_train.value_counts().to_dict()

            return True, (
                f"TRAINED | rows={len(X_train)} | "
                f"classes={counts}"
            )

        except Exception as e:
            return False, f"TRAIN ERROR: {str(e)[:150]}"

    def analyze(self, symbol, bars):
        df = self.extract_features_from_bars(bars)

        if df is None or len(df) == 0:
            return {
                "direction": "NO TRADE",
                "confidence": 0.0,
                "reason": "INSUFFICIENT FEATURES"
            }

        # Train separately per symbol to avoid silently using
        # one stock's model for another stock.
        needs_training = (
            not self.is_trained
            or self.last_trained_symbol != symbol
            or self.last_train_time is None
            or datetime.now(ET) - self.last_train_time > timedelta(hours=1)
        )

        if needs_training:
            ok, msg = self.train_models(df, symbol)

            if not ok:
                return {
                    "direction": "NO TRADE",
                    "confidence": 0.0,
                    "reason": msg
                }

            log_print(
                f"   🧠 {symbol}: {msg}"
            )

        features = [
            "ema9",
            "ema20",
            "ema50",
            "rsi",
            "volume_zscore",
            "atr",
            "return_1",
            "return_5",
        ]

        try:
            row = df[features].iloc[[-1]]

            lgb_pred = int(
                self.lgb_model.predict(row)[0]
            )

            rf_pred = int(
                self.rf_model.predict(row)[0]
            )

            lgb_proba = self.lgb_model.predict_proba(row)[0]
            rf_proba = self.rf_model.predict_proba(row)[0]

            lgb_classes = list(
                self.lgb_model.classes_
            )

            rf_classes = list(
                self.rf_model.classes_
            )

            lgb_prob = float(
                max(lgb_proba)
            )

            rf_prob = float(
                max(rf_proba)
            )

            confidence = (
                lgb_prob + rf_prob
            ) / 2

            if lgb_pred != rf_pred:
                return {
                    "direction": "NO TRADE",
                    "confidence": confidence,
                    "reason": (
                        f"MODEL DISAGREEMENT "
                        f"(LGB={lgb_pred}, RF={rf_pred})"
                    )
                }

            direction = {
                1: "CALL",
                -1: "PUT",
                0: "NO TRADE"
            }.get(lgb_pred, "NO TRADE")

            if direction == "NO TRADE":
                return {
                    "direction": "NO TRADE",
                    "confidence": confidence,
                    "reason": "MODEL TARGET=NEUTRAL"
                }

            threshold = (
                get_dynamic_confidence_threshold()
            )

            if confidence < threshold:
                return {
                    "direction": "NO TRADE",
                    "confidence": confidence,
                    "reason": (
                        f"CONFIDENCE BELOW "
                        f"{threshold:.0%}"
                    )
                }

            return {
                "direction": direction,
                "confidence": confidence,
                "reason": (
                    f"LGB={lgb_pred}/{lgb_prob:.0%}, "
                    f"RF={rf_pred}/{rf_prob:.0%}"
                )
            }

        except Exception as e:
            return {
                "direction": "NO TRADE",
                "confidence": 0.0,
                "reason": f"ML ERROR: {str(e)[:100]}"
            }


ml_engine = MultiModelEnsembleEngine()


# ============================================================
# OPTIONS
# ============================================================

def get_option_contracts(symbol):
    try:
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            limit=500
        )

        res = trading_client.get_option_contracts(req)

        return (
            res.option_contracts
            if hasattr(res, "option_contracts")
            else res
        )

    except Exception as e:
        log_print(
            f"   ⚠️ {symbol}: option contract error: "
            f"{str(e)[:100]}"
        )
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
                exp = datetime.fromisoformat(
                    exp
                ).date()

            elif hasattr(exp, "date"):
                exp = exp.date()

            dte = (exp - today).days

            if not (
                MIN_DTE <= dte <= MAX_DTE
            ):
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

    # Limit snapshot request to reduce API load.
    symbols = [
        c.symbol for c, _ in candidates[:250]
    ]

    snapshots = get_option_snapshots(symbols)

    if not snapshots:
        return None, "NO OPTION SNAPSHOTS"

    adaptive_spread = min(
        0.15,
        BASE_MAX_SPREAD_PCT * (
            1 + (float(vix_val or 15) / 50)
        )
    )

    valid = []

    for c, dte in candidates[:250]:
        sym = c.symbol

        if sym not in snapshots:
            continue

        snap = snapshots[sym]

        quote = getattr(
            snap,
            "latest_quote",
            None
        )

        greeks = getattr(
            snap,
            "greeks",
            None
        )

        iv = getattr(
            snap,
            "implied_volatility",
            None
        )

        if not quote:
            continue

        bid = float(
            getattr(quote, "bid_price", 0) or 0
        )

        ask = float(
            getattr(quote, "ask_price", 0) or 0
        )

        if bid <= 0 or ask <= 0:
            continue

        mid = (bid + ask) / 2

        if mid <= 0:
            continue

        spread_pct = (
            (ask - bid) / mid
        )

        if mid > MAX_PREMIUM:
            continue

        if spread_pct > adaptive_spread:
            continue

        delta = (
            abs(float(greeks.delta))
            if greeks
            and greeks.delta is not None
            else 0.5
        )

        if not (
            MIN_DELTA <= delta <= MAX_DELTA
        ):
            continue

        iv_value = (
            float(iv)
            if iv is not None
            else 0.0
        )

        iv_rank = calculate_iv_rank(
            sym,
            iv_value
        )

        if iv_rank > MAX_IV_RANK:
            continue

        gamma = (
            float(greeks.gamma)
            if greeks
            and greeks.gamma is not None
            else 0.01
        )

        theta = (
            abs(float(greeks.theta))
            if greeks
            and greeks.theta is not None
            else 0.01
        )

        # Score:
        # prioritize delta near 0.50,
        # then tight spread,
        # then DTE closer to target.
        delta_distance = abs(
            delta - 0.50
        )

        score = (
            delta_distance * 3
            + spread_pct * 2
            + abs(dte - 7) * 0.01
            - gamma / (theta + 1e-6) * 0.001
        )

        valid.append({
            "contract": c,
            "symbol": sym,
            "dte": dte,
            "delta": delta,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct,
            "iv": iv_value,
            "iv_rank": iv_rank,
            "score": score,
        })

    if not valid:
        return None, (
            "ALL CONTRACTS FAILED "
            "DELTA/SPREAD/PREMIUM/IV FILTERS"
        )

    valid.sort(
        key=lambda x: x["score"]
    )

    best = valid[0]

    return best, (
        f"OPTION FOUND {best['symbol']} | "
        f"DTE={best['dte']} | "
        f"delta={best['delta']:.2f} | "
        f"mid=${best['mid']:.2f} | "
        f"spread={best['spread_pct']:.1%} | "
        f"IVrank={best['iv_rank']:.2f}"
    )


# ============================================================
# ORDER HELPERS
# ============================================================

def wait_for_fill(order_id, timeout=15):
    start = time.time()

    while time.time() - start < timeout:
        try:
            order = trading_client.get_order_by_id(
                order_id
            )

            status = str(
                order.status
            ).lower()

            if status == "filled":
                return order

            if status in [
                "canceled",
                "cancelled",
                "expired",
                "rejected"
            ]:
                return None

        except Exception:
            pass

        time.sleep(1)

    return None


def submit_smart_limit_order(
    option_symbol,
    qty,
    initial_ask
):
    current_ask = float(initial_ask)

    for attempt in range(3):
        try:
            order = trading_client.submit_order(
                LimitOrderRequest(
                    symbol=option_symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(
                        current_ask,
                        2
                    )
                )
            )

            order_id = str(order.id)

            log_print(
                f"   📨 PAPER BUY ORDER "
                f"{option_symbol} | qty={qty} | "
                f"limit=${current_ask:.2f} | "
                f"attempt={attempt+1}/3"
            )

            filled = wait_for_fill(
                order_id,
                timeout=10
            )

            if filled:
                return filled

            try:
                trading_client.cancel_order_by_id(
                    order_id
                )
            except Exception:
                pass

            snapshots = get_option_snapshots(
                [option_symbol]
            )

            if (
                option_symbol in snapshots
                and snapshots[option_symbol].latest_quote
            ):
                new_ask = float(
                    snapshots[
                        option_symbol
                    ].latest_quote.ask_price
                    or current_ask
                )

                current_ask = new_ask

            else:
                current_ask += 0.01

        except Exception as e:
            log_print(
                f"   ⚠️ order attempt failed: "
                f"{str(e)[:100]}"
            )

    return None


def calculate_kelly_qty(confidence):
    try:
        conn = sqlite3.connect(DB_FILE)

        rows = conn.execute("""
            SELECT pnl_usd
            FROM trade_history
        """).fetchall()

        conn.close()

        if len(rows) < 10:
            return BASE_CONTRACTS_PER_TRADE

        pnl = [
            float(r[0])
            for r in rows
        ]

        wins = [
            x for x in pnl if x > 0
        ]

        losses = [
            x for x in pnl if x < 0
        ]

        if not wins or not losses:
            return BASE_CONTRACTS_PER_TRADE

        win_prob = (
            len(wins) / len(pnl)
        )

        avg_win = np.mean(wins)
        avg_loss = abs(np.mean(losses))

        if avg_loss <= 0:
            return BASE_CONTRACTS_PER_TRADE

        b = avg_win / avg_loss

        raw_kelly = (
            win_prob * b
            - (1 - win_prob)
        ) / b

        # Quarter-Kelly with hard cap.
        kelly = max(
            0.05,
            min(raw_kelly * 0.25, 0.35)
        )

        qty = int(
            round(
                BASE_CONTRACTS_PER_TRADE
                + kelly * MAX_CONTRACTS_PER_TRADE
            )
        )

        qty = max(
            2,
            min(
                qty,
                MAX_CONTRACTS_PER_TRADE
            )
        )

        return qty

    except Exception:
        return BASE_CONTRACTS_PER_TRADE


# ============================================================
# ENTRY
# ============================================================

def enter_position(
    symbol,
    direction,
    confidence,
    vix_val
):
    if state["trading_halted"]:
        return False, "TRADING HALTED"

    if (
        state["trades_today"]
        >= MAX_TRADES_PER_DAY
    ):
        return False, "MAX DAILY TRADES"

    if (
        len(state["positions"])
        >= MAX_OPEN_POSITIONS
    ):
        return False, "MAX OPEN POSITIONS"

    ok, reason = check_sector_correlation(
        symbol
    )

    if not ok:
        return False, reason

    log_print(
        f"   🔎 {symbol}: sector check PASS"
    )

    ok, reason = check_news_sentiment(
        symbol
    )

    if not ok:
        return False, reason

    log_print(
        f"   📰 {symbol}: {reason}"
    )

    ok, reason = check_earnings_and_macro_calendar(
        symbol
    )

    if not ok:
        return False, reason

    log_print(
        f"   📅 {symbol}: {reason}"
    )

    ok, reason = check_social_media_sentiment(
        symbol
    )

    if not ok:
        return False, reason

    log_print(
        f"   📡 {symbol}: {reason}"
    )

    ok, reason = check_multi_timeframe_confluence(
        symbol,
        direction
    )

    if not ok:
        return False, reason

    log_print(
        f"   📊 {symbol}: {reason}"
    )

    option, reason = select_smart_option(
        symbol,
        direction,
        vix_val
    )

    if not option:
        return False, reason

    log_print(
        f"   🎯 {symbol}: {reason}"
    )

    qty = calculate_kelly_qty(
        confidence
    )

    ok, reason = check_portfolio_delta_exposure(
        option["delta"],
        qty,
        direction
    )

    if not ok:
        return False, reason

    log_print(
        f"   🛡️ {symbol}: {reason}"
    )

    filled = submit_smart_limit_order(
        option["symbol"],
        qty,
        option["ask"]
    )

    if not filled:
        return False, "ORDER NOT FILLED"

    fill_price = float(
        getattr(
            filled,
            "filled_avg_price",
            None
        ) or option["ask"]
    )

    filled_qty = float(
        getattr(
            filled,
            "filled_qty",
            qty
        ) or qty
    )

    state["trades_today"] += 1

    state["positions"][
        option["symbol"]
    ] = {
        "underlying": symbol,
        "direction": direction,
        "qty": filled_qty,
        "initial_qty": filled_qty,
        "entry_price": fill_price,
        "confidence": confidence,
        "scale_out_done": False,
        "opened_at": datetime.now(
            ET
        ).isoformat(),
        "order_id": str(filled.id),
    }

    save_state_db(state)

    log_print(
        "🚨 PAPER TRADE EXECUTED | "
        f"{symbol} | {direction} | "
        f"{option['symbol']} | "
        f"qty={filled_qty} | "
        f"entry=${fill_price:.2f}"
    )

    return True, "FILLED"


# ============================================================
# TRADE HISTORY
# ============================================================

def log_trade_history_db(rec):
    try:
        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO trade_history
            (symbol, contract, direction,
             entry_price, exit_price, qty,
             pnl_usd, reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec["symbol"],
            rec["contract"],
            rec["direction"],
            rec["entry_price"],
            rec["exit_price"],
            rec["qty"],
            rec["pnl_usd"],
            rec["reason"],
            rec["closed_at"],
        ))

        conn.commit()
        conn.close()

    except Exception as e:
        log_print(
            f"⚠️ Trade history error: {str(e)[:100]}"
        )


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def partial_exit_position(
    opt_sym,
    pos,
    current_price
):
    try:
        full_qty = float(pos["qty"])

        partial_qty = (
            int(full_qty // 2)
        )

        if partial_qty < 1:
            return False

        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=opt_sym,
                qty=partial_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )

        filled = wait_for_fill(
            str(order.id),
            20
        )

        if not filled:
            return False

        exit_price = float(
            getattr(
                filled,
                "filled_avg_price",
                None
            ) or current_price
        )

        pnl = (
            exit_price
            - float(pos["entry_price"])
        ) * partial_qty * 100

        state["estimated_daily_pnl"] += pnl

        pos["qty"] = (
            full_qty - partial_qty
        )

        pos["scale_out_done"] = True

        save_state_db(state)

        log_print(
            f"✂️ PAPER SCALE-OUT | "
            f"{opt_sym} | sold={partial_qty} | "
            f"P&L=${pnl:+.2f}"
        )

        return True

    except Exception as e:
        log_print(
            f"⚠️ Partial exit error: "
            f"{str(e)[:100]}"
        )
        return False


def exit_position(
    opt_sym,
    pos,
    current_price,
    reason
):
    try:
        qty = float(pos["qty"])

        if qty <= 0:
            state["positions"].pop(
                opt_sym,
                None
            )
            save_state_db(state)
            return True

        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=opt_sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )

        filled = wait_for_fill(
            str(order.id),
            20
        )

        if not filled:
            return False

        exit_price = float(
            getattr(
                filled,
                "filled_avg_price",
                None
            ) or current_price
        )

        pnl = (
            exit_price
            - float(pos["entry_price"])
        ) * qty * 100

        state["estimated_daily_pnl"] += pnl

        if (
            state["estimated_daily_pnl"]
            <= MAX_DAILY_LOSS_USD
        ):
            state["trading_halted"] = True

            log_print(
                "🛑 DAILY LOSS LIMIT HIT: "
                f"${state['estimated_daily_pnl']:.2f}"
            )

        log_trade_history_db({
            "symbol": pos["underlying"],
            "contract": opt_sym,
            "direction": pos["direction"],
            "entry_price": float(
                pos["entry_price"]
            ),
            "exit_price": exit_price,
            "qty": qty,
            "pnl_usd": pnl,
            "reason": reason,
            "closed_at": datetime.now(
                ET
            ).isoformat(),
        })

        state["positions"].pop(
            opt_sym,
            None
        )

        save_state_db(state)

        log_print(
            f"🛑 PAPER FULL EXIT | "
            f"{opt_sym} | "
            f"P&L=${pnl:+.2f} | "
            f"reason={reason}"
        )

        return True

    except Exception as e:
        log_print(
            f"⚠️ Full exit error: "
            f"{str(e)[:120]}"
        )
        return False


def manage_positions():
    if not state["positions"]:
        return

    symbols = list(
        state["positions"].keys()
    )

    snapshots = get_option_snapshots(
        symbols
    )

    for opt_sym, pos in list(
        state["positions"].items()
    ):
        try:
            if opt_sym not in snapshots:
                log_print(
                    f"   ⚠️ Position {opt_sym}: "
                    f"snapshot unavailable"
                )
                continue

            snap = snapshots[opt_sym]

            quote = getattr(
                snap,
                "latest_quote",
                None
            )

            greeks = getattr(
                snap,
                "greeks",
                None
            )

            if not quote:
                continue

            bid = float(
                getattr(
                    quote,
                    "bid_price",
                    0
                ) or 0
            )

            ask = float(
                getattr(
                    quote,
                    "ask_price",
                    0
                ) or 0
            )

            if bid <= 0 or ask <= 0:
                continue

            cur_price = (
                bid + ask
            ) / 2

            entry = float(
                pos["entry_price"]
            )

            initial_qty = float(
                pos["initial_qty"]
            )

            total_pnl = (
                cur_price - entry
            ) * initial_qty * 100

            pnl_pct = (
                cur_price - entry
            ) / entry

            delta = (
                abs(float(greeks.delta))
                if greeks
                and greeks.delta is not None
                else 0.5
            )

            target = TARGET_PROFIT_USD

            if delta > 0.75:
                target *= 1.25

            log_print(
                f"   📦 POSITION {opt_sym} | "
                f"mid=${cur_price:.2f} | "
                f"P&L=${total_pnl:+.2f} | "
                f"delta={delta:.2f}"
            )

            if (
                not pos.get(
                    "scale_out_done",
                    False
                )
                and total_pnl >= target / 2
                and initial_qty >= 2
            ):
                partial_exit_position(
                    opt_sym,
                    pos,
                    cur_price
                )
                continue

            if total_pnl >= target:
                exit_position(
                    opt_sym,
                    pos,
                    cur_price,
                    "TARGET PROFIT"
                )
                continue

            # Hard option premium stop.
            stop_pct = -0.30

            if pnl_pct <= stop_pct:
                exit_position(
                    opt_sym,
                    pos,
                    cur_price,
                    "OPTION STOP LOSS -30%"
                )
                continue

            # After scale-out, protect remaining position
            # at approximately breakeven.
            if (
                pos.get(
                    "scale_out_done",
                    False
                )
                and pnl_pct <= 0
            ):
                exit_position(
                    opt_sym,
                    pos,
                    cur_price,
                    "BREAKEVEN PROTECTION"
                )

        except Exception as e:
            log_print(
                f"⚠️ Position management error "
                f"{opt_sym}: {str(e)[:100]}"
            )


def force_eod_liquidation():
    now = datetime.now(ET).time()

    if not (
        dt_time(15, 55)
        <= now
        <= dt_time(16, 0)
    ):
        return

    if not state["positions"]:
        return

    log_print(
        "⏰ EOD FORCE LIQUIDATION"
    )

    snapshots = get_option_snapshots(
        list(state["positions"].keys())
    )

    for opt_sym, pos in list(
        state["positions"].items()
    ):
        try:
            if (
                opt_sym in snapshots
                and snapshots[opt_sym].latest_quote
            ):
                bid = float(
                    snapshots[
                        opt_sym
                    ].latest_quote.bid_price
                    or pos["entry_price"]
                )
            else:
                bid = float(
                    pos["entry_price"]
                )

            exit_position(
                opt_sym,
                pos,
                bid,
                "EOD FORCE LIQUIDATION"
            )

        except Exception as e:
            log_print(
                f"⚠️ EOD exit error: "
                f"{str(e)[:100]}"
            )


def sync_positions():
    try:
        real_positions = (
            trading_client.get_all_positions()
        )

        symbols = {
            p.symbol
            for p in real_positions
            if len(p.symbol) >= 10
        }

        changed = False

        for sym in list(
            state["positions"].keys()
        ):
            if sym not in symbols:
                # Only remove if it really disappeared
                # from Alpaca.
                state["positions"].pop(
                    sym,
                    None
                )
                changed = True

        if changed:
            save_state_db(state)

    except Exception as e:
        log_print(
            f"⚠️ Position sync error: "
            f"{str(e)[:100]}"
        )


# ============================================================
# EOD REPORT
# ============================================================

def print_eod_summary():
    if state["eod_summary_done"]:
        return

    now = datetime.now(ET)

    if now.time() < dt_time(16, 0):
        return

    try:
        today = get_today_date()

        conn = sqlite3.connect(DB_FILE)

        rows = conn.execute("""
            SELECT pnl_usd
            FROM trade_history
            WHERE closed_at LIKE ?
        """, (
            f"{today}%",
        )).fetchall()

        conn.close()

        pnls = [
            float(r[0])
            for r in rows
        ]

        wins = [
            x for x in pnls
            if x > 0
        ]

        win_rate = (
            len(wins)
            / len(pnls)
            * 100
            if pnls else 0
        )

        net = (
            sum(pnls)
            if pnls
            else state["estimated_daily_pnl"]
        )

        log_section("📈 END OF DAY REPORT")

        log_print(
            f"Trades: {len(pnls)}"
        )

        log_print(
            f"Win rate: {win_rate:.1f}%"
        )

        log_print(
            f"Net P&L: ${net:+.2f}"
        )

        state["eod_summary_done"] = True

        save_state_db(state)

    except Exception as e:
        log_print(
            f"⚠️ EOD report error: "
            f"{str(e)[:100]}"
        )


# ============================================================
# SCAN ONE SYMBOL
# ============================================================

def scan_symbol(symbol, vix_val):
    try:
        bars, data_reason = fetch_stock_bars_robust(
            symbol,
            TimeFrame.Minute,
            datetime.now(ET) - timedelta(days=5),
            datetime.now(ET),
            purpose="ML SCAN",
        )

        if bars is None:
            reason = data_reason

            save_scan(
                symbol,
                "NO TRADE",
                0,
                "REJECT",
                reason
            )

            if PRINT_EVERY_SYMBOL:
                log_decision(
                    symbol,
                    "NO TRADE",
                    0,
                    reason
                )

            return False

        if len(bars) < 80:
            reason = (
                f"INSUFFICIENT BARS "
                f"({len(bars)}/80)"
            )

            save_scan(
                symbol,
                "NO TRADE",
                0,
                "REJECT",
                reason
            )

            log_decision(
                symbol,
                "NO TRADE",
                0,
                reason
            )

            return False

        analysis = ml_engine.analyze(
            symbol,
            bars
        )

        direction = analysis[
            "direction"
        ]

        confidence = float(
            analysis["confidence"]
        )

        reason = analysis[
            "reason"
        ]

        if direction not in [
            "CALL",
            "PUT"
        ]:
            save_scan(
                symbol,
                direction,
                confidence,
                "REJECT",
                reason
            )

            log_decision(
                symbol,
                direction,
                confidence,
                reason
            )

            return False

        log_decision(
            symbol,
            direction,
            confidence,
            f"SIGNAL PASSED ML | {reason}"
        )

        ok, enter_reason = enter_position(
            symbol,
            direction,
            confidence,
            vix_val
        )

        if ok:
            save_scan(
                symbol,
                direction,
                confidence,
                "TRADE",
                "FILLED"
            )
            return True

        save_scan(
            symbol,
            direction,
            confidence,
            "REJECT",
            enter_reason
        )

        log_print(
            f"   ❌ {symbol}: "
            f"signal rejected AFTER ML -> "
            f"{enter_reason}"
        )

        return False

    except Exception as e:
        reason = (
            f"SCAN ERROR: {str(e)[:150]}"
        )

        save_scan(
            symbol,
            "ERROR",
            0,
            "ERROR",
            reason
        )

        log_print(
            f"   ❌ {symbol}: {reason}"
        )

        return False


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log_section(
        "🤖 TRANSPARENT PAPER QUANT BOT v16 STARTING"
    )

    log_print(
        "💰 PAPER MODE = TRUE"
    )

    log_print(
        f"⏱ Scan interval = "
        f"{SCAN_INTERVAL_SECONDS}s"
    )

    log_print(
        f"🎯 ML threshold = "
        f"{BASE_CONFIDENCE_THRESHOLD:.0%}"
    )

    log_print(
        f"🛑 Daily loss limit = "
        f"${MAX_DAILY_LOSS_USD:.2f}"
    )

    log_print(
        "📡 Logs are flushed immediately."
    )

    log_print(
        "🔍 Full-session scanning enabled: "
        "09:30-16:00 ET"
    )

    consecutive_errors = 0
    current_universe = []
    last_universe_update = None

    scan_counter = 0

    while True:
        try:
            reset_daily_state()

            sync_positions()

            manage_positions()

            force_eod_liquidation()

            print_eod_summary()

            now = datetime.now(ET)

            scan_counter += 1

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

            log_section(
                f"💓 HEARTBEAT / SCAN #{scan_counter}"
            )

            log_print(
                f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} ET"
            )

            log_print(
                f"Market: "
                f"{'OPEN' if is_market_open() else 'CLOSED'}"
            )

            log_print(
                f"Window: {market_window_name()}"
            )

            log_print(
                f"Trades today: "
                f"{state['trades_today']}/{MAX_TRADES_PER_DAY}"
            )

            log_print(
                f"Open positions: "
                f"{len(state['positions'])}/{MAX_OPEN_POSITIONS}"
            )

            log_print(
                f"Daily P&L estimate: "
                f"${state['estimated_daily_pnl']:+.2f}"
            )

            if state["trading_halted"]:
                log_print(
                    "🛑 TRADING HALTED"
                )

            # ------------------------------------------------
            # MARKET CLOSED
            # ------------------------------------------------

            if not is_market_open():
                log_print(
                    f"⏸ Market closed. "
                    f"Next scan in {SCAN_INTERVAL_SECONDS}s."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )
                continue

            if state["trading_halted"]:
                log_print(
                    "⏸ Trading halted; "
                    "position management remains active."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )
                continue

            # ------------------------------------------------
            # VIX
            # ------------------------------------------------

            panic, vix, vix_status = (
                check_vix_volatility_regime()
            )

            if vix is None:
                log_print(
                    f"🌡 VIX: {vix_status}"
                )
            else:
                log_print(
                    f"🌡 VIX: {vix:.2f} | "
                    f"{vix_status}"
                )

            if panic:
                log_print(
                    "🚨 VIX PANIC FILTER -> "
                    "NO NEW ENTRIES"
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )
                continue

            # ------------------------------------------------
            # POSITION / TRADE LIMITS
            # ------------------------------------------------

            if (
                len(state["positions"])
                >= MAX_OPEN_POSITIONS
            ):
                log_print(
                    "⏸ Max open positions reached."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )
                continue

            if (
                state["trades_today"]
                >= MAX_TRADES_PER_DAY
            ):
                log_print(
                    "⏸ Max daily trades reached."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )
                continue

            # ------------------------------------------------
            # UNIVERSE
            # ------------------------------------------------

            today = get_today_date()

            if (
                last_universe_update != today
                or not current_universe
            ):
                current_universe = (
                    get_dynamic_universe()
                )

                last_universe_update = today

                log_print(
                    "🌐 Universe: "
                    + ", ".join(
                        current_universe
                    )
                )

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            found_trade = False

            log_print(
                "🔎 STARTING SYMBOL SCAN..."
            )

            for symbol in current_universe:

                if any(
                    p["underlying"] == symbol
                    for p in state[
                        "positions"
                    ].values()
                ):
                    log_print(
                        f"   ⏭ {symbol}: "
                        f"already has position"
                    )
                    continue

                log_print(
                    f"   🔍 Checking {symbol}..."
                )

                if scan_symbol(
                    symbol,
                    vix
                ):
                    found_trade = True

                    # One entry per scan.
                    break

            if found_trade:
                log_print(
                    "✅ TRADE ACTION COMPLETED."
                )
            else:
                log_print(
                    "⏸ NO TRADE THIS SCAN."
                )

            log_print(
                f"💤 Sleeping "
                f"{SCAN_INTERVAL_SECONDS}s..."
            )

            consecutive_errors = 0

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:
            log_print(
                "🛑 Bot stopped manually."
            )
            break

        except Exception as e:
            consecutive_errors += 1

            log_print(
                f"❌ MAIN ERROR "
                f"({consecutive_errors}/"
                f"{MAX_CONSECUTIVE_ERRORS}): "
                f"{str(e)[:200]}"
            )

            if (
                consecutive_errors
                >= MAX_CONSECUTIVE_ERRORS
            ):
                state[
                    "trading_halted"
                ] = True

                save_state_db(
                    state
                )

                log_print(
                    "🚨 KILL SWITCH TRIGGERED."
                )

                break

            time.sleep(
                ERROR_SLEEP_SECONDS
            )


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":

    log_section(
        "🚀 TRADING ENGINE BOOT"
    )

    log_print(
        "🤖 TRANSPARENT PAPER OPTIONS ENGINE"
    )

    log_print(
        "📡 MARKET DATA ENGINE READY"
    )

    log_print(
        "🧠 ML ENGINE READY"
    )

    log_print(
        "💰 PAPER TRADING MODE ACTIVE"
    )

    log_print(
        "🔄 BUY / SELL ENGINE READY"
    )

    log_print(
        "📝 DETAILED CLOUD LOGGING ENABLED"
    )

    try:
        main()

    except Exception as e:
        log_print(
            f"🚨 FATAL ENGINE CRASH: "
            f"{str(e)}"
        )
        raise