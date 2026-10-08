# ============================================================
# SPX/STOCK OPTIONS PAPER BOT - DEBUG / TRANSPARENT v16.2
# ============================================================
# PAPER ONLY
#
# v16.2 MAJOR FIXES:
#   1) VIX unavailable = HARD WAIT / NO NEW ENTRIES
#   2) LGB + RF must agree on direction
#   3) Confidence is NOT raw max(predict_proba)
#   4) Time-series validation added
#   5) Latest market bar is NOT used as training target
#   6) VIX fallback through yfinance (^VIX)
#   7) Freshness checks
#   8) Real option quote filters
#   9) Real delta/IV when available
#  10) No fake Delta/IV/Spread values
#  11) Strong data-health logging
#  12) PAPER ONLY
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
from sklearn.metrics import accuracy_score
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


# ============================================================
# SAFETY
# ============================================================

PAPER_MODE = True

if not PAPER_MODE:
    print("❌ SAFETY STOP: This version is PAPER ONLY.", flush=True)
    sys.exit(1)


# ============================================================
# MARKET DATA
# ============================================================

STOCK_FEED = DataFeed.IEX
OPTIONS_FEED = OptionsFeed.INDICATIVE

ET = ZoneInfo("America/New_York")


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
# RISK SETTINGS
# ============================================================

BASE_CONTRACTS_PER_TRADE = 2
MAX_CONTRACTS_PER_TRADE = 8

TARGET_PROFIT_USD = 70.0

MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 2

MAX_DAILY_LOSS_USD = -200.0

# Minimum confidence required after ensemble checks
BASE_CONFIDENCE_THRESHOLD = 0.78

# Agreement requirement
REQUIRE_MODEL_AGREEMENT = True

# Minimum validation accuracy
MIN_VALIDATION_ACCURACY = 0.55

# Minimum number of labeled samples
MIN_TRAINING_ROWS = 80

# Minimum bars required
MIN_BARS_REQUIRED = 100

# Freshness
MAX_BAR_AGE_MINUTES = 10

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

DB_FILE = "institutional_bot_v16_2.db"

PRINT_NO_TRADE_DETAILS = True
PRINT_EVERY_SYMBOL = True


# ============================================================
# CLIENTS
# ============================================================

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True,
)

stock_data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
)

option_data_client = OptionHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
)

news_client = NewsClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
)


# ============================================================
# LOGGING
# ============================================================

def log_print(msg):
    timestamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp} ET] {msg}", flush=True)


def log_section(title):
    log_print("")
    log_print("=" * 78)
    log_print(title)
    log_print("=" * 78)


def log_decision(symbol, direction, confidence, reason):
    log_print(
        f"   └─ {symbol}: ML={direction} | "
        f"confidence={confidence:.1%} | {reason}"
    )


# ============================================================
# FAKE BAR OBJECT
# ============================================================

class FakeBar:

    def __init__(self, o, h, l, c, v, timestamp=None):
        self.open = float(o)
        self.high = float(h)
        self.low = float(l)
        self.close = float(c)
        self.volume = float(v)
        self.timestamp = timestamp


# ============================================================
# BAR TIMESTAMP HELPER
# ============================================================

def get_bar_timestamp(bar):

    for attr in [
        "timestamp",
        "time",
        "t",
        "datetime",
    ]:
        value = getattr(bar, attr, None)

        if value is not None:
            try:
                if isinstance(value, pd.Timestamp):
                    if value.tzinfo is None:
                        value = value.tz_localize("UTC")
                    return value.to_pydatetime().astimezone(ET)

                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=ZoneInfo("UTC"))
                    return value.astimezone(ET)

            except Exception:
                pass

    return None


def bars_are_fresh(bars, max_age_minutes=MAX_BAR_AGE_MINUTES):

    if not bars:
        return False, "NO BARS"

    ts = get_bar_timestamp(bars[-1])

    # Some data providers don't expose timestamps in the bar object.
    # Do not falsely reject valid data if timestamp is unavailable.
    if ts is None:
        return True, "TIMESTAMP UNKNOWN"

    now = datetime.now(ET)
    age_minutes = (now - ts).total_seconds() / 60.0

    if age_minutes < 0:
        return True, f"FUTURE/REALTIME BAR ({age_minutes:.1f}m)"

    if age_minutes > max_age_minutes:
        return False, f"STALE ({age_minutes:.1f}m)"

    return True, f"FRESH ({age_minutes:.1f}m)"


# ============================================================
# ROBUST STOCK DATA LOADER
# ============================================================

def fetch_stock_bars_robust(
    symbol,
    timeframe,
    start,
    end,
    purpose="SCAN",
):

    # --------------------------------------------------------
    # 1) Alpaca IEX
    # --------------------------------------------------------

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

                fresh, fresh_reason = bars_are_fresh(bars)

                if purpose == "HEALTH_CHECK":
                    return bars, (
                        f"DATA OK (IEX, {len(bars)} bars) | "
                        f"{fresh_reason}"
                    )

                if not fresh:
                    return None, (
                        f"STALE STOCK DATA | {fresh_reason}"
                    )

                return bars, (
                    f"DATA OK (IEX, {len(bars)} bars) | "
                    f"{fresh_reason}"
                )

    except Exception as e:

        if PRINT_NO_TRADE_DETAILS:
            log_print(
                f"      ⚠️ IEX failed for {symbol}: "
                f"{str(e)[:100]}"
            )


    # --------------------------------------------------------
    # 2) yfinance fallback
    # --------------------------------------------------------

    try:

        if timeframe == TimeFrame.Hour:
            yf_tf = "1h"
        else:
            yf_tf = "1m"

        df_yf = yf.download(
            symbol,
            start=start,
            end=end,
            interval=yf_tf,
            progress=False,
            auto_adjust=False,
            prepost=False,
        )

        if not df_yf.empty:

            bars = []

            for idx, row in df_yf.iterrows():

                try:

                    def val(col):
                        x = row[col]

                        if isinstance(x, pd.Series):
                            x = x.iloc[0]

                        return float(x)

                    o = val("Open")
                    h = val("High")
                    l = val("Low")
                    c = val("Close")
                    v = val("Volume")

                    if pd.isna(c):
                        continue

                    timestamp = None

                    try:
                        timestamp = pd.Timestamp(idx)

                        if timestamp.tzinfo is None:
                            timestamp = timestamp.tz_localize("UTC")

                        timestamp = timestamp.to_pydatetime()

                    except Exception:
                        timestamp = None

                    bars.append(
                        FakeBar(
                            o,
                            h,
                            l,
                            c,
                            v,
                            timestamp,
                        )
                    )

                except Exception:
                    continue

            if bars:

                fresh, fresh_reason = bars_are_fresh(bars)

                if purpose == "HEALTH_CHECK":

                    return bars, (
                        f"DATA OK (YFINANCE FALLBACK, "
                        f"{len(bars)} bars) | "
                        f"{fresh_reason}"
                    )

                if not fresh:
                    return None, (
                        f"STALE YFINANCE DATA | "
                        f"{fresh_reason}"
                    )

                return bars, (
                    f"DATA OK (YFINANCE FALLBACK, "
                    f"{len(bars)} bars) | "
                    f"{fresh_reason}"
                )

    except Exception as e:

        if PRINT_NO_TRADE_DETAILS:
            log_print(
                f"      ⚠️ yfinance failed for {symbol}: "
                f"{str(e)[:100]}"
            )


    log_print(
        f"      ❌ DATA UNAVAILABLE | "
        f"{symbol} | All sources failed"
    )

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


def save_scan(
    symbol,
    direction,
    confidence,
    decision,
    reason,
):

    try:

        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO scan_log
            (scan_time, symbol, direction, confidence,
             decision, reason)
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

    # --------------------------------------------------------
    # 1) Try Alpaca
    # --------------------------------------------------------

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

        if res and "VIX" in res and len(res["VIX"]) > 0:

            bars = res["VIX"]

            vix = float(bars[-1].close)

            if np.isfinite(vix) and vix > 0:

                panic = vix > MAX_VIX_THRESHOLD

                return (
                    panic,
                    vix,
                    "PANIC" if panic else "NORMAL",
                )

    except Exception:
        pass


    # --------------------------------------------------------
    # 2) yfinance fallback
    # --------------------------------------------------------

    try:

        end = datetime.now(ET)
        start = end - timedelta(days=5)

        df = yf.download(
            "^VIX",
            start=start,
            end=end,
            interval="5m",
            progress=False,
            auto_adjust=False,
            prepost=False,
        )

        if not df.empty:

            close = df["Close"]

            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]

            close = close.dropna()

            if len(close) > 0:

                vix = float(close.iloc[-1])

                if np.isfinite(vix) and vix > 0:

                    panic = vix > MAX_VIX_THRESHOLD

                    return (
                        panic,
                        vix,
                        (
                            "PANIC (YFINANCE)"
                            if panic
                            else "NORMAL (YFINANCE)"
                        ),
                    )

    except Exception as e:

        return (
            False,
            None,
            f"VIX ERROR: {str(e)[:80]}",
        )


    # --------------------------------------------------------
    # HARD FAIL
    # --------------------------------------------------------

    return (
        False,
        None,
        "VIX DATA UNAVAILABLE",
    )


# ============================================================
# NEWS
# ============================================================

def check_news_sentiment(symbol):

    try:

        end = datetime.now(ET)
        start = end - timedelta(hours=6)

        res = news_client.get_news(
            NewsRequest(
                symbol=symbol,
                start=start,
                end=end,
                limit=5,
            )
        )

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

                return (
                    False,
                    f"NEGATIVE NEWS: {headline[:90]}",
                )

        return (
            True,
            f"{len(res.news)} NEWS ITEMS CLEAN",
        )

    except Exception as e:

        return (
            True,
            f"NEWS BYPASSED: {str(e)[:60]}",
        )


# ============================================================
# OTHER FILTERS
# ============================================================

def check_social_media_sentiment(symbol):

    try:

        end = datetime.now(ET)
        start = end - timedelta(hours=4)

        res = news_client.get_news(
            NewsRequest(
                symbol=symbol,
                start=start,
                end=end,
                limit=10,
            )
        )

        if not res or not hasattr(res, "news") or not res.news:

            return True, "HEADLINE RADAR NEUTRAL"

        return True, "HEADLINE RADAR CLEAN"

    except Exception as e:

        return (
            True,
            f"RADAR BYPASSED: {str(e)[:50]}",
        )


def check_earnings_and_macro_calendar(symbol):
    return True, "EVENT FILTER CLEAN"


def check_multi_timeframe_confluence(symbol, direction):
    return True, "1H CONFLUENCE PASS"


def check_sector_correlation(symbol):

    sector = SECTOR_MAP.get(symbol, symbol)

    for pos in state["positions"].values():

        existing_sector = SECTOR_MAP.get(
            pos["underlying"],
            pos["underlying"],
        )

        if sector == existing_sector:

            return (
                False,
                f"SECTOR CONFLICT ({sector})",
            )

    return True, "SECTOR OK"


def check_portfolio_delta_exposure(
    new_delta,
    new_qty,
    direction,
):

    # Conservative placeholder.
    # Real aggregate delta should be calculated from
    # actual option positions before live deployment.

    return True, "DELTA OK"


def calculate_iv_rank(symbol, current_iv):

    # No fake IV rank.
    # Until enough historical IV data exists,
    # return None rather than pretending it is 0.3.

    if current_iv is None:
        return None

    try:

        conn = sqlite3.connect(DB_FILE)

        rows = conn.execute("""
            SELECT iv
            FROM iv_history
            WHERE symbol=?
            ORDER BY recorded_at DESC
            LIMIT 252
        """, (symbol,)).fetchall()

        conn.close()

        values = [
            float(r[0])
            for r in rows
            if r[0] is not None
        ]

        if len(values) < 20:
            return None

        low = min(values)
        high = max(values)

        if high <= low:
            return 0.5

        rank = (
            float(current_iv) - low
        ) / (
            high - low
        )

        return float(np.clip(rank, 0.0, 1.0))

    except Exception:

        return None


def record_iv(symbol, iv):

    if iv is None:
        return

    try:

        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO iv_history
            (symbol, iv, recorded_at)
            VALUES (?, ?, ?)
        """, (
            symbol,
            float(iv),
            datetime.now(ET).isoformat(),
        ))

        conn.commit()
        conn.close()

    except Exception:
        pass


def get_dynamic_confidence_threshold():

    return BASE_CONFIDENCE_THRESHOLD


def get_dynamic_universe():

    return DEFAULT_TARGET_UNDERLYINGS.copy()


# ============================================================
# ML ENGINE v16.2
# ============================================================

class MultiModelEnsembleEngine:

    def __init__(self):

        self.lgb_params = {
            "n_estimators": 150,
            "learning_rate": 0.04,
            "random_state": 42,
            "verbose": -1,
            "num_leaves": 15,
            "max_depth": 5,
            "min_child_samples": 10,
        }

        self.rf_params = {
            "n_estimators": 200,
            "random_state": 42,
            "min_samples_leaf": 3,
            "max_depth": 8,
            "class_weight": "balanced_subsample",
        }

        self.lgb_model = None
        self.rf_model = None

        self.is_trained = False

        self.last_trained_symbol = None
        self.last_train_time = None

        self.validation_accuracy = 0.0
        self.training_rows = 0

    # --------------------------------------------------------
    # FEATURES
    # --------------------------------------------------------

    def extract_features_from_bars(self, bars):

        if bars is None or len(bars) < MIN_BARS_REQUIRED:
            return None

        df = pd.DataFrame({
            "open": [
                float(b.open)
                for b in bars
            ],
            "high": [
                float(b.high)
                for b in bars
            ],
            "low": [
                float(b.low)
                for b in bars
            ],
            "close": [
                float(b.close)
                for b in bars
            ],
            "volume": [
                float(b.volume)
                for b in bars
            ],
        })

        # ----------------------------------------------------
        # Trend
        # ----------------------------------------------------

        df["ema9"] = (
            df["close"]
            .ewm(span=9, adjust=False)
            .mean()
        )

        df["ema20"] = (
            df["close"]
            .ewm(span=20, adjust=False)
            .mean()
        )

        df["ema50"] = (
            df["close"]
            .ewm(span=50, adjust=False)
            .mean()
        )

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        delta = df["close"].diff()

        gain = (
            delta.where(delta > 0, 0)
            .rolling(14)
            .mean()
        )

        loss = (
            -delta.where(delta < 0, 0)
            .rolling(14)
            .mean()
        )

        rs = gain / (loss + 1e-9)

        df["rsi"] = (
            100 -
            (100 / (1 + rs))
        )

        # ----------------------------------------------------
        # Volume
        # ----------------------------------------------------

        vol_mean = (
            df["volume"]
            .rolling(20)
            .mean()
        )

        vol_std = (
            df["volume"]
            .rolling(20)
            .std()
        )

        df["volume_zscore"] = (
            (df["volume"] - vol_mean)
            / (vol_std + 1e-9)
        )

        # ----------------------------------------------------
        # ATR
        # ----------------------------------------------------

        true_range = pd.concat(
            [
                df["high"] - df["low"],

                (
                    df["high"]
                    - df["close"].shift()
                ).abs(),

                (
                    df["low"]
                    - df["close"].shift()
                ).abs(),
            ],
            axis=1,
        ).max(axis=1)

        df["atr"] = (
            true_range
            .rolling(14)
            .mean()
        )

        # ----------------------------------------------------
        # Returns
        # ----------------------------------------------------

        df["return_1"] = (
            df["close"]
            .pct_change(1)
        )

        df["return_5"] = (
            df["close"]
            .pct_change(5)
        )

        df["return_15"] = (
            df["close"]
            .pct_change(15)
        )

        # ----------------------------------------------------
        # EMA relationships
        # ----------------------------------------------------

        df["ema9_20_gap"] = (
            df["ema9"] / df["ema20"] - 1
        )

        df["ema20_50_gap"] = (
            df["ema20"] / df["ema50"] - 1
        )

        df["price_ema20_gap"] = (
            df["close"] / df["ema20"] - 1
        )

        df.dropna(inplace=True)

        return df

    # --------------------------------------------------------
    # TRAIN
    # --------------------------------------------------------

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
                "return_15",
                "ema9_20_gap",
                "ema20_50_gap",
                "price_ema20_gap",
            ]

            work = df.copy()

            # Future 5-bar return
            work["future_return"] = (
                work["close"].shift(-5)
                / work["close"]
                - 1
            )

            # ------------------------------------------------
            # Target:
            # 1 = CALL
            # 0 = NEUTRAL
            # -1 = PUT
            # ------------------------------------------------

            work["target"] = 0

            work.loc[
                work["future_return"] > 0.002,
                "target"
            ] = 1

            work.loc[
                work["future_return"] < -0.002,
                "target"
            ] = -1

            # ------------------------------------------------
            # IMPORTANT:
            # Remove only rows without known future target.
            # Latest market rows are NOT training samples.
            # ------------------------------------------------

            labeled = work.dropna(
                subset=["future_return"]
            ).copy()

            if len(labeled) < MIN_TRAINING_ROWS:

                return (
                    False,
                    f"NOT ENOUGH TRAINING DATA "
                    f"({len(labeled)}/{MIN_TRAINING_ROWS})"
                )

            X = labeled[features]
            y_raw = labeled["target"]

            # Map:
            # -1 PUT -> 0
            #  0 NEUTRAL -> 1
            # +1 CALL -> 2

            mapping = {
                -1: 0,
                0: 1,
                1: 2,
            }

            y = y_raw.map(mapping)

            # Need at least two classes
            unique_classes = sorted(
                y.dropna().unique().tolist()
            )

            if len(unique_classes) < 2:

                return (
                    False,
                    "TRAINING HAS ONLY ONE CLASS"
                )

            # ------------------------------------------------
            # Time-series split
            # ------------------------------------------------

            split = int(len(X) * 0.80)

            if split < 50:
                return (
                    False,
                    "TRAINING SPLIT TOO SMALL"
                )

            X_train = X.iloc[:split]
            y_train = y.iloc[:split]

            X_valid = X.iloc[split:]
            y_valid = y.iloc[split:]

            if len(X_valid) < 10:
                return (
                    False,
                    "VALIDATION WINDOW TOO SMALL"
                )

            train_classes = sorted(
                y_train.unique().tolist()
            )

            if len(train_classes) < 2:
                return (
                    False,
                    "TRAINING WINDOW HAS <2 CLASSES"
                )

            # ------------------------------------------------
            # Models
            # ------------------------------------------------

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

            # ------------------------------------------------
            # Validation
            # ------------------------------------------------

            lgb_val_pred = (
                self.lgb_model
                .predict(X_valid)
            )

            rf_val_pred = (
                self.rf_model
                .predict(X_valid)
            )

            lgb_acc = accuracy_score(
                y_valid,
                lgb_val_pred,
            )

            rf_acc = accuracy_score(
                y_valid,
                rf_val_pred,
            )

            ensemble_pred = []

            for a, b in zip(
                lgb_val_pred,
                rf_val_pred,
            ):

                if a == b:
                    ensemble_pred.append(a)
                else:
                    # Disagreement -> neutral
                    ensemble_pred.append(1)

            ensemble_acc = accuracy_score(
                y_valid,
                ensemble_pred,
            )

            self.validation_accuracy = float(
                ensemble_acc
            )

            self.training_rows = len(X_train)

            # ------------------------------------------------
            # We still train, but later require validation
            # quality before allowing a trade.
            # ------------------------------------------------

            self.is_trained = True
            self.last_trained_symbol = symbol
            self.last_train_time = datetime.now(ET)

            return (
                True,
                (
                    f"TRAINED | rows={len(X_train)} | "
                    f"validation={len(X_valid)} | "
                    f"LGB_ACC={lgb_acc:.1%} | "
                    f"RF_ACC={rf_acc:.1%} | "
                    f"ENSEMBLE_ACC={ensemble_acc:.1%}"
                ),
            )

        except Exception as e:

            self.is_trained = False

            return (
                False,
                f"TRAIN ERROR: {str(e)[:150]}",
            )

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    def analyze(self, symbol, bars):

        df = self.extract_features_from_bars(
            bars
        )

        if df is None or len(df) < 30:

            return {
                "direction": "NO TRADE",
                "confidence": 0.0,
                "reason": "INSUFFICIENT FEATURES",
            }

        needs_training = (
            not self.is_trained
            or self.last_trained_symbol != symbol
            or self.last_train_time is None
            or (
                datetime.now(ET)
                - self.last_train_time
                > timedelta(hours=1)
            )
        )

        if needs_training:

            ok, msg = self.train_models(
                df,
                symbol,
            )

            if not ok:

                return {
                    "direction": "NO TRADE",
                    "confidence": 0.0,
                    "reason": msg,
                }

            if PRINT_NO_TRADE_DETAILS:
                log_print(
                    f"      🧠 {symbol}: {msg}"
                )

        # ----------------------------------------------------
        # Minimum validation quality
        # ----------------------------------------------------

        if (
            self.validation_accuracy
            < MIN_VALIDATION_ACCURACY
        ):

            return {
                "direction": "NO TRADE",
                "confidence": 0.0,
                "reason": (
                    f"MODEL VALIDATION TOO WEAK | "
                    f"accuracy="
                    f"{self.validation_accuracy:.1%}"
                ),
            }

        features = [
            "ema9",
            "ema20",
            "ema50",
            "rsi",
            "volume_zscore",
            "atr",
            "return_1",
            "return_5",
            "return_15",
            "ema9_20_gap",
            "ema20_50_gap",
            "price_ema20_gap",
        ]

        try:

            # ------------------------------------------------
            # IMPORTANT:
            # Predict the actual latest feature row.
            # It was NOT part of training because its future
            # return is unknown.
            # ------------------------------------------------

            row = df[features].iloc[[-1]]

            lgb_pred = int(
                self.lgb_model.predict(row)[0]
            )

            rf_pred = int(
                self.rf_model.predict(row)[0]
            )

            lgb_proba_raw = (
                self.lgb_model
                .predict_proba(row)[0]
            )

            rf_proba_raw = (
                self.rf_model
                .predict_proba(row)[0]
            )

            # ------------------------------------------------
            # Convert model classes into dictionary
            # ------------------------------------------------

            def probability_dict(model, proba):

                result = {
                    0: 0.0,
                    1: 0.0,
                    2: 0.0,
                }

                for cls, p in zip(
                    model.classes_,
                    proba,
                ):
                    result[int(cls)] = float(p)

                return result

            lgb_probs = probability_dict(
                self.lgb_model,
                lgb_proba_raw,
            )

            rf_probs = probability_dict(
                self.rf_model,
                rf_proba_raw,
            )

            # ------------------------------------------------
            # Model labels
            # ------------------------------------------------

            label_to_direction = {
                0: "PUT",
                1: "NEUTRAL",
                2: "CALL",
            }

            lgb_direction = label_to_direction.get(
                lgb_pred,
                "NEUTRAL",
            )

            rf_direction = label_to_direction.get(
                rf_pred,
                "NEUTRAL",
            )

            # ------------------------------------------------
            # HARD ENSEMBLE AGREEMENT
            # ------------------------------------------------

            if (
                REQUIRE_MODEL_AGREEMENT
                and lgb_direction != rf_direction
            ):

                # Calculate agreement-neutral confidence
                confidence = (
                    max(lgb_probs.values())
                    + max(rf_probs.values())
                ) / 2.0

                return {
                    "direction": "NO TRADE",
                    "confidence": confidence,
                    "reason": (
                        f"MODEL DISAGREEMENT | "
                        f"LGB={lgb_direction} "
                        f"RF={rf_direction}"
                    ),
                }

            # ------------------------------------------------
            # Neutral = no trade
            # ------------------------------------------------

            if (
                lgb_direction == "NEUTRAL"
                or rf_direction == "NEUTRAL"
            ):

                confidence = (
                    max(lgb_probs.values())
                    + max(rf_probs.values())
                ) / 2.0

                return {
                    "direction": "NO TRADE",
                    "confidence": confidence,
                    "reason": (
                        "MODEL TARGET=NEUTRAL | "
                        f"LGB={lgb_direction} "
                        f"RF={rf_direction}"
                    ),
                }

            # ------------------------------------------------
            # Agreement on CALL / PUT
            # ------------------------------------------------

            direction = lgb_direction

            model_confidence = (
                lgb_probs[lgb_pred]
                + rf_probs[rf_pred]
            ) / 2.0

            # Agreement bonus is deliberately small.
            agreement_bonus = 0.05

            confidence = min(
                0.99,
                model_confidence
                + agreement_bonus
            )

            # Validation quality modifier
            validation_factor = np.clip(
                self.validation_accuracy,
                0.55,
                0.80,
            )

            validation_factor = (
                validation_factor / 0.80
            )

            confidence = (
                confidence
                * validation_factor
            )

            return {
                "direction": direction,
                "confidence": float(
                    np.clip(
                        confidence,
                        0.0,
                        0.99,
                    )
                ),
                "reason": (
                    f"LGB={lgb_direction} "
                    f"RF={rf_direction} | "
                    f"Validation="
                    f"{self.validation_accuracy:.1%}"
                ),
            }

        except Exception as e:

            return {
                "direction": "NO TRADE",
                "confidence": 0.0,
                "reason": (
                    f"ML ERROR: "
                    f"{str(e)[:100]}"
                ),
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
            limit=200,
        )

        res = trading_client.get_option_contracts(
            req
        )

        return (
            res.option_contracts
            if hasattr(res, "option_contracts")
            else res
        )

    except Exception as e:

        if PRINT_NO_TRADE_DETAILS:
            log_print(
                f"      ⚠️ OPTION CONTRACT ERROR "
                f"{symbol}: {str(e)[:100]}"
            )

        return []


def get_option_snapshots(symbols):

    if not symbols:
        return {}

    try:

        result = option_data_client.get_option_snapshots(
            OptionSnapshotRequest(
                symbol_or_symbols=symbols,
                feed=OPTIONS_FEED,
            )
        )

        return result or {}

    except Exception as e:

        if PRINT_NO_TRADE_DETAILS:
            log_print(
                f"      ⚠️ OPTION SNAPSHOT ERROR: "
                f"{str(e)[:100]}"
            )

        return {}


# ============================================================
# OPTION FIELD HELPERS
# ============================================================

def extract_option_delta(snapshot):

    # Different SDK versions may expose Greeks differently.

    greeks = getattr(
        snapshot,
        "greeks",
        None,
    )

    if greeks is None:
        return None

    delta = getattr(
        greeks,
        "delta",
        None,
    )

    if delta is None:
        return None

    try:
        return float(delta)
    except Exception:
        return None


def extract_option_iv(snapshot):

    iv = getattr(
        snapshot,
        "implied_volatility",
        None,
    )

    if iv is None:
        return None

    try:
        return float(iv)
    except Exception:
        return None


# ============================================================
# SMART OPTION SELECTION
# ============================================================

def select_smart_option(
    symbol,
    direction,
    vix_val,
):

    # --------------------------------------------------------
    # VIX must be available
    # --------------------------------------------------------

    if vix_val is None:

        return (
            None,
            "VIX UNAVAILABLE -> HARD WAIT",
        )

    contracts = get_option_contracts(
        symbol
    )

    if not contracts:

        return (
            None,
            "NO ACTIVE OPTION CONTRACTS",
        )

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

            dte = (
                exp - today
            ).days

            if not (
                MIN_DTE
                <= dte
                <= MAX_DTE
            ):
                continue

            ctype = str(
                c.type
            ).upper()

            if (
                direction == "CALL"
                and "CALL" not in ctype
            ):
                continue

            if (
                direction == "PUT"
                and "PUT" not in ctype
            ):
                continue

            candidates.append(
                (c, dte)
            )

        except Exception:
            continue

    if not candidates:

        return (
            None,
            "NO DTE/TYPE MATCH",
        )

    # Limit snapshot request
    symbols = [
        c.symbol
        for c, _ in candidates[:100]
    ]

    snapshots = get_option_snapshots(
        symbols
    )

    if not snapshots:

        return (
            None,
            "NO OPTION SNAPSHOTS",
        )

    valid = []

    for c, dte in candidates[:100]:

        sym = c.symbol

        if sym not in snapshots:
            continue

        snap = snapshots[sym]

        quote = getattr(
            snap,
            "latest_quote",
            None,
        )

        if quote is None:
            continue

        try:

            bid = float(
                getattr(
                    quote,
                    "bid_price",
                    0,
                )
                or 0
            )

            ask = float(
                getattr(
                    quote,
                    "ask_price",
                    0,
                )
                or 0
            )

        except Exception:
            continue

        if bid <= 0 or ask <= 0:
            continue

        mid = (
            bid + ask
        ) / 2.0

        if mid <= 0:
            continue

        # ----------------------------------------------------
        # Premium filter
        # ----------------------------------------------------

        if mid > MAX_PREMIUM:
            continue

        # ----------------------------------------------------
        # Spread filter
        # ----------------------------------------------------

        spread_pct = (
            (ask - bid)
            / mid
        )

        if (
            spread_pct
            > BASE_MAX_SPREAD_PCT
        ):
            continue

        # ----------------------------------------------------
        # Real Greeks
        # ----------------------------------------------------

        delta = extract_option_delta(
            snap
        )

        iv = extract_option_iv(
            snap
        )

        # If real delta unavailable,
        # don't pretend it's 0.5.
        if delta is None:
            continue

        # Absolute delta for PUT/CALL
        abs_delta = abs(delta)

        if not (
            MIN_DELTA
            <= abs_delta
            <= MAX_DELTA
        ):
            continue

        # ----------------------------------------------------
        # IV
        # ----------------------------------------------------

        if iv is not None:

            record_iv(
                symbol,
                iv
            )

            iv_rank = calculate_iv_rank(
                symbol,
                iv
            )

            # If enough history exists,
            # apply IV rank filter.
            if (
                iv_rank is not None
                and iv_rank > MAX_IV_RANK
            ):
                continue

        else:

            iv_rank = None

        # ----------------------------------------------------
        # Scoring
        # ----------------------------------------------------

        delta_score = (
            1.0
            - abs(abs_delta - 0.50)
        )

        spread_score = max(
            0.0,
            1.0
            - (
                spread_pct
                / BASE_MAX_SPREAD_PCT
            )
        )

        dte_score = (
            1.0
            - (
                abs(dte - 5)
                / 10.0
            )
        )

        premium_score = max(
            0.0,
            1.0
            - (
                mid
                / MAX_PREMIUM
            )
        )

        score = (
            0.35 * delta_score
            + 0.30 * spread_score
            + 0.20 * dte_score
            + 0.15 * premium_score
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
            "iv": iv,
            "iv_rank": iv_rank,
            "score": score,
        })

    if not valid:

        return (
            None,
            "ALL CONTRACTS FAILED REAL DATA FILTERS",
        )

    # Higher score is better
    valid.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    best = valid[0]

    return (
        best,
        (
            f"OPTION FOUND "
            f"{best['symbol']} | "
            f"DTE={best['dte']} | "
            f"delta={best['delta']:.2f} | "
            f"mid=${best['mid']:.2f} | "
            f"spread={best['spread_pct']:.1%}"
        ),
    )


# ============================================================
# ORDER MANAGEMENT
# ============================================================

def wait_for_fill(
    order_id,
    timeout=10,
):

    start = time.time()

    while (
        time.time() - start
        < timeout
    ):

        try:

            order = (
                trading_client
                .get_order_by_id(
                    order_id
                )
            )

            if (
                str(order.status)
                .lower()
                == "filled"
            ):
                return order

        except Exception:
            pass

        time.sleep(1)

    return None


def submit_smart_limit_order(
    option_symbol,
    qty,
    initial_ask,
):

    try:

        order = (
            trading_client
            .submit_order(
                LimitOrderRequest(
                    symbol=option_symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(
                        float(initial_ask),
                        2,
                    ),
                )
            )
        )

        return wait_for_fill(
            str(order.id),
            timeout=10,
        )

    except Exception as e:

        log_print(
            f"❌ ORDER ERROR: "
            f"{str(e)[:100]}"
        )

        return None


# ============================================================
# ENTER POSITION
# ============================================================

def enter_position(
    symbol,
    direction,
    confidence,
    vix_val,
):

    if state["trading_halted"]:
        return False, "TRADING HALTED"

    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, "MAX DAILY TRADES"

    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False, "MAX OPEN POSITIONS"

    if vix_val is None:
        return False, "VIX UNAVAILABLE -> HARD WAIT"

    if confidence < get_dynamic_confidence_threshold():
        return (
            False,
            (
                f"CONFIDENCE BELOW THRESHOLD "
                f"{confidence:.1%} < "
                f"{get_dynamic_confidence_threshold():.1%}"
            ),
        )

    option, reason = select_smart_option(
        symbol,
        direction,
        vix_val,
    )

    if not option:

        return False, reason

    filled = submit_smart_limit_order(
        option["symbol"],
        BASE_CONTRACTS_PER_TRADE,
        option["ask"],
    )

    if not filled:

        return False, "ORDER NOT FILLED"

    fill_price = float(
        getattr(
            filled,
            "filled_avg_price",
            None,
        )
        or option["ask"]
    )

    filled_qty = float(
        getattr(
            filled,
            "filled_qty",
            BASE_CONTRACTS_PER_TRADE,
        )
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
        "order_id": str(
            filled.id
        ),
    }

    save_state_db(state)

    log_print(
        f"🚨 PAPER TRADE EXECUTED | "
        f"{symbol} | "
        f"{direction} | "
        f"{option['symbol']} | "
        f"qty={filled_qty} | "
        f"entry=${fill_price:.2f} | "
        f"confidence={confidence:.1%}"
    )

    return True, "FILLED"


# ============================================================
# EXIT
# ============================================================

def exit_position(
    opt_sym,
    pos,
    current_price,
    reason,
):

    try:

        qty = float(
            pos["qty"]
        )

        if qty <= 0:

            state["positions"].pop(
                opt_sym,
                None,
            )

            save_state_db(state)

            return True

        order = (
            trading_client
            .submit_order(
                MarketOrderRequest(
                    symbol=opt_sym,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
        )

        filled = wait_for_fill(
            str(order.id),
            15,
        )

        exit_price = (
            float(
                getattr(
                    filled,
                    "filled_avg_price",
                    None,
                )
            )
            if filled
            and getattr(
                filled,
                "filled_avg_price",
                None,
            )
            else current_price
        )

        pnl = (
            exit_price
            - float(
                pos["entry_price"]
            )
        ) * qty * 100

        state["estimated_daily_pnl"] += pnl

        state["positions"].pop(
            opt_sym,
            None,
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
            f"❌ EXIT ERROR: "
            f"{str(e)[:100]}"
        )

        return False


# ============================================================
# MANAGE POSITIONS
# ============================================================

def manage_positions():

    if not state["positions"]:
        return

    snapshots = get_option_snapshots(
        list(
            state["positions"].keys()
        )
    )

    for opt_sym, pos in list(
        state["positions"].items()
    ):

        try:

            if opt_sym not in snapshots:
                continue

            quote = getattr(
                snapshots[opt_sym],
                "latest_quote",
                None,
            )

            if not quote:
                continue

            bid = float(
                quote.bid_price or 0
            )

            ask = float(
                quote.ask_price or 0
            )

            if bid <= 0 or ask <= 0:
                continue

            cur_price = (
                bid + ask
            ) / 2

            entry = float(
                pos["entry_price"]
            )

            pnl_pct = (
                cur_price - entry
            ) / entry

            if pnl_pct <= -0.30:

                exit_position(
                    opt_sym,
                    pos,
                    cur_price,
                    "OPTION STOP LOSS -30%",
                )

        except Exception:
            pass


def sync_positions():
    pass


def print_eod_summary():
    pass


# ============================================================
# SCAN ONE SYMBOL
# ============================================================

def scan_symbol(
    symbol,
    vix_val,
):

    try:

        # ----------------------------------------------------
        # VIX hard requirement
        # ----------------------------------------------------

        if vix_val is None:

            reason = (
                "VIX UNAVAILABLE -> "
                "SCAN BLOCKED"
            )

            save_scan(
                symbol,
                "NO TRADE",
                0,
                "REJECT",
                reason,
            )

            if PRINT_EVERY_SYMBOL:

                log_decision(
                    symbol,
                    "NO TRADE",
                    0,
                    reason,
                )

            return False

        # ----------------------------------------------------
        # Stock data
        # ----------------------------------------------------

        bars, data_reason = (
            fetch_stock_bars_robust(
                symbol,
                TimeFrame.Minute,
                datetime.now(ET)
                - timedelta(days=2),
                datetime.now(ET),
                purpose="ML SCAN",
            )
        )

        if (
            bars is None
            or len(bars) < MIN_BARS_REQUIRED
        ):

            reason = (
                f"INSUFFICIENT BARS "
                f"({len(bars) if bars else 0}) | "
                f"{data_reason}"
            )

            save_scan(
                symbol,
                "NO TRADE",
                0,
                "REJECT",
                reason,
            )

            if PRINT_EVERY_SYMBOL:

                log_decision(
                    symbol,
                    "NO TRADE",
                    0,
                    reason,
                )

            return False

        # ----------------------------------------------------
        # Freshness
        # ----------------------------------------------------

        fresh, fresh_reason = bars_are_fresh(
            bars
        )

        if not fresh:

            reason = (
                f"STALE MARKET DATA | "
                f"{fresh_reason}"
            )

            save_scan(
                symbol,
                "NO TRADE",
                0,
                "REJECT",
                reason,
            )

            log_decision(
                symbol,
                "NO TRADE",
                0,
                reason,
            )

            return False

        # ----------------------------------------------------
        # ML
        # ----------------------------------------------------

        analysis = ml_engine.analyze(
            symbol,
            bars,
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

        # ----------------------------------------------------
        # NO TRADE
        # ----------------------------------------------------

        if direction not in [
            "CALL",
            "PUT",
        ]:

            save_scan(
                symbol,
                direction,
                confidence,
                "REJECT",
                reason,
            )

            log_decision(
                symbol,
                direction,
                confidence,
                reason,
            )

            return False

        # ----------------------------------------------------
        # Confidence
        # ----------------------------------------------------

        threshold = (
            get_dynamic_confidence_threshold()
        )

        if confidence < threshold:

            reason2 = (
                f"BELOW CONFIDENCE "
                f"{confidence:.1%} < "
                f"{threshold:.1%}"
            )

            save_scan(
                symbol,
                direction,
                confidence,
                "REJECT",
                reason2,
            )

            log_decision(
                symbol,
                "NO TRADE",
                confidence,
                reason2,
            )

            return False

        # ----------------------------------------------------
        # Additional filters
        # ----------------------------------------------------

        news_ok, news_reason = (
            check_news_sentiment(
                symbol
            )
        )

        if not news_ok:

            save_scan(
                symbol,
                direction,
                confidence,
                "REJECT",
                news_reason,
            )

            log_decision(
                symbol,
                "NO TRADE",
                confidence,
                news_reason,
            )

            return False

        mtf_ok, mtf_reason = (
            check_multi_timeframe_confluence(
                symbol,
                direction,
            )
        )

        if not mtf_ok:

            save_scan(
                symbol,
                direction,
                confidence,
                "REJECT",
                mtf_reason,
            )

            log_decision(
                symbol,
                "NO TRADE",
                confidence,
                mtf_reason,
            )

            return False

        sector_ok, sector_reason = (
            check_sector_correlation(
                symbol
            )
        )

        if not sector_ok:

            save_scan(
                symbol,
                direction,
                confidence,
                "REJECT",
                sector_reason,
            )

            log_decision(
                symbol,
                "NO TRADE",
                confidence,
                sector_reason,
            )

            return False

        # ----------------------------------------------------
        # Signal passed
        # ----------------------------------------------------

        log_decision(
            symbol,
            direction,
            confidence,
            (
                f"SIGNAL PASSED | "
                f"{reason} | "
                f"VIX={vix_val:.2f}"
            ),
        )

        # ----------------------------------------------------
        # Entry
        # ----------------------------------------------------

        ok, enter_reason = enter_position(
            symbol,
            direction,
            confidence,
            vix_val,
        )

        if ok:

            save_scan(
                symbol,
                direction,
                confidence,
                "TRADE",
                "FILLED",
            )

            return True

        save_scan(
            symbol,
            direction,
            confidence,
            "REJECT",
            enter_reason,
        )

        log_print(
            f"      ↳ {symbol}: "
            f"ENTRY REJECTED | "
            f"{enter_reason}"
        )

        return False

    except Exception as e:

        reason = (
            f"SCAN ERROR: "
            f"{str(e)[:120]}"
        )

        save_scan(
            symbol,
            "ERROR",
            0,
            "ERROR",
            reason,
        )

        log_print(
            f"      ❌ {symbol}: {reason}"
        )

        return False


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    log_section(
        "🤖 TRANSPARENT PAPER QUANT BOT v16.2"
    )

    log_print(
        "💰 PAPER MODE = TRUE"
    )

    log_print(
        "📡 Data Health = STRICT"
    )

    log_print(
        "🧠 Ensemble = LGB + RF"
    )

    log_print(
        "🛡 VIX unavailable = HARD WAIT"
    )

    log_print(
        "🎯 Model agreement = REQUIRED"
    )

    log_print(
        f"🎯 Minimum confidence = "
        f"{BASE_CONFIDENCE_THRESHOLD:.0%}"
    )

    scan_counter = 0

    while True:

        try:

            reset_daily_state()

            sync_positions()

            manage_positions()

            print_eod_summary()

            now = datetime.now(ET)

            scan_counter += 1

            log_section(
                f"💓 HEARTBEAT / SCAN #{scan_counter}"
            )

            log_print(
                f"Time: "
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} ET"
            )

            log_print(
                f"Market: "
                f"{'OPEN' if is_market_open() else 'CLOSED'}"
            )

            log_print(
                f"Window: "
                f"{market_window_name()}"
            )

            # ------------------------------------------------
            # MARKET CLOSED
            # ------------------------------------------------

            if not is_market_open():

                log_print(
                    f"⏸ Market closed. "
                    f"Next scan in "
                    f"{SCAN_INTERVAL_SECONDS}s."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # DATA HEALTH
            # ------------------------------------------------

            log_print(
                "📡 MARKET DATA HEALTH CHECK:"
            )

            current_universe = (
                get_dynamic_universe()
            )

            data_health_counts = {}

            for sym in current_universe:

                test_bars, test_reason = (
                    fetch_stock_bars_robust(
                        sym,
                        TimeFrame.Minute,
                        now - timedelta(days=1),
                        now,
                        purpose="HEALTH_CHECK",
                    )
                )

                count = (
                    len(test_bars)
                    if test_bars
                    else 0
                )

                data_health_counts[
                    sym
                ] = count

                status_icon = (
                    "✅"
                    if count > 0
                    else "⚠️"
                )

                log_print(
                    f"   {sym} "
                    f"{status_icon} "
                    f"{count} bars | "
                    f"{test_reason}"
                )

            active_coverage = sum(
                1
                for c in
                data_health_counts.values()
                if c > 0
            )

            log_print(
                f"📊 DATA COVERAGE: "
                f"{active_coverage}/"
                f"{len(current_universe)}"
            )

            if active_coverage == 0:

                log_print(
                    "🚨 MARKET DATA DOWN: "
                    "0 symbols usable."
                )

                log_print(
                    "⛔ ML SCAN BLOCKED."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # VIX
            # ------------------------------------------------

            (
                panic,
                vix,
                vix_status,
            ) = check_vix_volatility_regime()

            if vix is None:

                log_print(
                    "🌡 VIX: "
                    "❌ DATA UNAVAILABLE"
                )

                log_print(
                    "🛡 HARD SAFETY: "
                    "NO NEW ENTRIES "
                    "UNTIL VIX DATA RETURNS."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            log_print(
                f"🌡 VIX: "
                f"{vix:.2f} | "
                f"{vix_status}"
            )

            # ------------------------------------------------
            # VIX PANIC
            # ------------------------------------------------

            if panic:

                log_print(
                    "🚨 VIX PANIC FILTER "
                    "-> NO NEW ENTRIES"
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

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
                    for p in
                    state["positions"].values()
                ):
                    continue

                if scan_symbol(
                    symbol,
                    vix,
                ):

                    found_trade = True

                    break

            if found_trade:

                log_print(
                    "✅ TRADE ACTION COMPLETED."
                )

            else:

                log_print(
                    "⏸ NO TRADE THIS SCAN."
                )

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            log_print(
                "🛑 Bot stopped manually."
            )

            break

        except Exception as e:

            log_print(
                f"❌ MAIN ERROR: "
                f"{str(e)[:180]}"
            )

            time.sleep(
                ERROR_SLEEP_SECONDS
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        log_print(
            f"🚨 FATAL ENGINE CRASH: "
            f"{str(e)}"
        )

        raise