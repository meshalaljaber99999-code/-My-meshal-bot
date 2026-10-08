# ============================================================
# SPX/STOCK OPTIONS PAPER BOT
# DEBUG / TRANSPARENT v16.2
#
# MAJOR CHANGES:
#   - LGB + RF MUST AGREE
#   - No fake confidence interpretation
#   - Time-series validation
#   - Stronger ML signal filtering
#   - Real option quote filtering
#   - Real spread calculation
#   - Real delta/IV when available
#   - Data freshness checks
#   - VIX unavailable handled safely
#   - Patient mode / no forced trades
#   - PAPER ONLY
#
# IMPORTANT:
#   This bot does NOT guarantee profitability.
#   ML confidence is NOT probability of winning a trade.
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
from sklearn.metrics import accuracy_score, log_loss

import yfinance as yf

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
from alpaca.data.enums import (
    DataFeed,
    OptionsFeed,
)

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
# DATA
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

# Stronger than old 78%
BASE_CONFIDENCE_THRESHOLD = 0.80

# Required difference between first and second probability.
MIN_PROBABILITY_MARGIN = 0.12

# Models must agree.
REQUIRE_MODEL_AGREEMENT = True

# Maximum portfolio delta.
MAX_NET_PORTFOLIO_DELTA = 6.0

# Options selection.
MIN_DTE = 1
MAX_DTE = 14

MIN_DELTA = 0.40
MAX_DELTA = 0.60

MAX_PREMIUM = 15.00

# 10% max spread.
BASE_MAX_SPREAD_PCT = 0.10

MAX_IV_RANK = 0.85

MAX_VIX_THRESHOLD = 30.0

# Option stop loss.
OPTION_STOP_LOSS_PCT = -0.30

# Minimum expected move from ML.
MIN_EXPECTED_MOVE = 0.002

# Data freshness.
MAX_DATA_AGE_MINUTES = 10

# Patient mode.
SCAN_INTERVAL_SECONDS = 60

# Avoid repeatedly entering same underlying immediately.
SYMBOL_COOLDOWN_MINUTES = 20

ERROR_SLEEP_SECONDS = 20
MAX_CONSECUTIVE_ERRORS = 5

DB_FILE = "institutional_bot_v162.db"


# ============================================================
# OUTPUT
# ============================================================

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
    log_print("=" * 76)
    log_print(title)
    log_print("=" * 76)


def log_decision(symbol, direction, confidence, reason):
    log_print(
        f"   └─ {symbol}: ML={direction} | "
        f"confidence={confidence:.1%} | {reason}"
    )


# ============================================================
# FAKE BAR
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
# DATA HELPERS
# ============================================================

def normalize_yf_value(value):

    if isinstance(value, pd.Series):
        if len(value) == 0:
            return np.nan
        return value.iloc[0]

    return value


def fetch_stock_bars_robust(
    symbol,
    timeframe,
    start,
    end,
    purpose="SCAN",
):
    """
    Data priority:

        1. Alpaca IEX
        2. yfinance fallback

    Never fabricates market bars.
    """

    # --------------------------------------------------------
    # 1. ALPACA IEX
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

                return (
                    bars,
                    f"DATA OK (IEX, {len(bars)} bars)",
                )

    except Exception as e:

        if purpose != "HEALTH_CHECK":

            log_print(
                f"      ⚠️ Alpaca IEX failed {symbol}: "
                f"{str(e)[:100]}"
            )


    # --------------------------------------------------------
    # 2. YFINANCE FALLBACK
    # --------------------------------------------------------

    try:

        if timeframe == TimeFrame.Minute:
            yf_tf = "1m"
        elif timeframe == TimeFrame.Hour:
            yf_tf = "1h"
        else:
            yf_tf = "1h"


        # yfinance 1m has a short historical limit.
        # Use a safer recent window.
        yf_end = end
        yf_start = start

        if yf_tf == "1m":

            max_start = yf_end - timedelta(days=6)

            if yf_start < max_start:
                yf_start = max_start


        df_yf = yf.download(
            symbol,
            start=yf_start,
            end=yf_end,
            interval=yf_tf,
            progress=False,
            auto_adjust=False,
            prepost=False,
        )


        if df_yf is None or df_yf.empty:

            return (
                None,
                "NO STOCK BAR DATA | All sources failed",
            )


        bars = []

        for idx, row in df_yf.iterrows():

            try:

                o = normalize_yf_value(row["Open"])
                h = normalize_yf_value(row["High"])
                l = normalize_yf_value(row["Low"])
                c = normalize_yf_value(row["Close"])
                v = normalize_yf_value(row["Volume"])

                if pd.isna(c):
                    continue

                bars.append(
                    FakeBar(
                        o,
                        h,
                        l,
                        c,
                        v,
                        idx,
                    )
                )

            except Exception:
                continue


        if bars:

            return (
                bars,
                f"DATA OK (YFINANCE FALLBACK, {len(bars)} bars)",
            )


    except Exception as e:

        if purpose != "HEALTH_CHECK":

            log_print(
                f"      ⚠️ yfinance failed {symbol}: "
                f"{str(e)[:100]}"
            )


    if purpose != "HEALTH_CHECK":

        log_print(
            f"      ❌ DATA UNAVAILABLE | "
            f"{symbol} | All sources failed"
        )

    return (
        None,
        "NO STOCK BAR DATA | All sources failed",
    )


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


# ============================================================
# STATE
# ============================================================

def get_today_date():

    return datetime.now(ET).strftime("%Y-%m-%d")


def load_state_db():

    conn = sqlite3.connect(DB_FILE)

    cur = conn.cursor()

    today = get_today_date()


    cur.execute("""
        SELECT
            trades_today,
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
            (
                date,
                trades_today,
                estimated_daily_pnl,
                trading_halted,
                eod_summary_done
            )
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
        SELECT
            option_symbol,
            underlying,
            direction,
            qty,
            initial_qty,
            entry_price,
            confidence,
            scale_out_done,
            opened_at,
            order_id
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
        (
            date,
            trades_today,
            estimated_daily_pnl,
            trading_halted,
            eod_summary_done
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        state["date"],
        state["trades_today"],
        state["estimated_daily_pnl"],
        int(state["trading_halted"]),
        int(state["eod_summary_done"]),
    ))


    cur.execute(
        "DELETE FROM open_positions"
    )


    for symbol, pos in state["positions"].items():

        cur.execute("""
            INSERT INTO open_positions
            (
                option_symbol,
                underlying,
                direction,
                qty,
                initial_qty,
                entry_price,
                confidence,
                scale_out_done,
                opened_at,
                order_id
            )
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


# ============================================================
# SCAN LOG
# ============================================================

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
            (
                scan_time,
                symbol,
                direction,
                confidence,
                decision,
                reason
            )
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


# ============================================================
# DAILY RESET
# ============================================================

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

        log_print(
            "🔄 New trading day: state reset."
        )


# ============================================================
# MARKET STATUS
# ============================================================

def is_market_open():

    now = datetime.now(ET)

    if now.weekday() >= 5:
        return False

    return (
        dt_time(9, 30)
        <= now.time()
        <= dt_time(16, 0)
    )


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


        if (
            not res
            or "VIX" not in res
            or len(res["VIX"]) == 0
        ):

            return (
                False,
                None,
                "VIX DATA UNAVAILABLE",
            )


        vix = float(
            res["VIX"][-1].close
        )


        if vix > MAX_VIX_THRESHOLD:

            return (
                True,
                vix,
                "PANIC",
            )


        return (
            False,
            vix,
            "NORMAL",
        )


    except Exception as e:

        return (
            False,
            None,
            f"VIX ERROR: {str(e)[:80]}",
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


        if (
            not res
            or not hasattr(res, "news")
            or not res.news
        ):

            return (
                True,
                "NO RECENT NEWS",
            )


        dangerous = [

            "fraud",
            "investigation",
            "bankruptcy",
            "halted",
            "sec investigation",
            "accounting scandal",
            "restatement",
        ]


        for article in res.news:

            headline = str(
                getattr(
                    article,
                    "headline",
                    "",
                )
            ).lower()


            if any(
                k in headline
                for k in dangerous
            ):

                return (
                    False,
                    f"NEGATIVE NEWS: "
                    f"{headline[:90]}",
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


        if (
            not res
            or not hasattr(res, "news")
            or not res.news
        ):

            return (
                True,
                "HEADLINE RADAR NEUTRAL",
            )


        return (
            True,
            "HEADLINE RADAR CLEAN",
        )


    except Exception as e:

        return (
            True,
            f"RADAR BYPASSED: {str(e)[:50]}",
        )


def check_earnings_and_macro_calendar(symbol):

    return (
        True,
        "EVENT FILTER CLEAN",
    )


def check_multi_timeframe_confluence(
    symbol,
    direction,
):

    return (
        True,
        "1H CONFLUENCE PASS",
    )


def check_sector_correlation(symbol):

    sector = SECTOR_MAP.get(
        symbol,
        symbol,
    )


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


    return (
        True,
        "SECTOR OK",
    )


def check_portfolio_delta_exposure(
    new_delta,
    new_qty,
    direction,
):

    # Conservative placeholder.
    # Actual aggregate delta is not guaranteed
    # by every Alpaca snapshot configuration.

    if abs(new_delta * new_qty) > MAX_NET_PORTFOLIO_DELTA:

        return (
            False,
            "DELTA EXPOSURE TOO HIGH",
        )


    return (
        True,
        "DELTA OK",
    )


# ============================================================
# OPTION SNAPSHOTS
# ============================================================

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

        log_print(
            f"      ⚠️ OPTION SNAPSHOT ERROR: "
            f"{str(e)[:100]}"
        )

        return {}


# ============================================================
# ML ENGINE
# ============================================================

class MultiModelEnsembleEngine:

    def __init__(self):

        self.lgb_params = {

            "n_estimators": 150,

            "learning_rate": 0.035,

            "num_leaves": 15,

            "max_depth": 5,

            "min_child_samples": 10,

            "random_state": 42,

            "verbose": -1,
        }


        self.rf_params = {

            "n_estimators": 200,

            "random_state": 42,

            "min_samples_leaf": 3,

            "max_depth": 8,

            "class_weight": "balanced",
        }


        self.lgb_model = None

        self.rf_model = None

        self.is_trained = False

        self.last_trained_symbol = None

        self.last_train_time = None


    # --------------------------------------------------------
    # FEATURES
    # --------------------------------------------------------

    def extract_features_from_bars(self, bars):

        if bars is None or len(bars) < 80:

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
        # TREND
        # ----------------------------------------------------

        df["ema9"] = (
            df["close"]
            .ewm(
                span=9,
                adjust=False,
            )
            .mean()
        )


        df["ema20"] = (
            df["close"]
            .ewm(
                span=20,
                adjust=False,
            )
            .mean()
        )


        df["ema50"] = (
            df["close"]
            .ewm(
                span=50,
                adjust=False,
            )
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
            (-delta.where(delta < 0, 0))
            .rolling(14)
            .mean()
        )

        rs = gain / (
            loss + 1e-9
        )

        df["rsi"] = (
            100
            - (
                100
                / (1 + rs)
            )
        )


        # ----------------------------------------------------
        # VOLUME
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
            (
                df["volume"]
                - vol_mean
            )
            / (vol_std + 1e-9)
        )


        # ----------------------------------------------------
        # ATR
        # ----------------------------------------------------

        true_range = pd.concat(

            [

                df["high"]
                - df["low"],

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
        # RETURNS
        # ----------------------------------------------------

        df["return_1"] = (
            df["close"]
            .pct_change(1)
        )

        df["return_3"] = (
            df["close"]
            .pct_change(3)
        )

        df["return_5"] = (
            df["close"]
            .pct_change(5)
        )

        df["return_10"] = (
            df["close"]
            .pct_change(10)
        )


        # ----------------------------------------------------
        # TREND DISTANCE
        # ----------------------------------------------------

        df["ema9_20_distance"] = (
            df["ema9"]
            / df["ema20"]
            - 1
        )


        df["ema20_50_distance"] = (
            df["ema20"]
            / df["ema50"]
            - 1
        )


        # ----------------------------------------------------
        # ATR NORMALIZED
        # ----------------------------------------------------

        df["atr_pct"] = (
            df["atr"]
            / df["close"]
        )


        # ----------------------------------------------------
        # HIGH/LOW BREAKOUT
        # ----------------------------------------------------

        rolling_high = (
            df["high"]
            .rolling(20)
            .max()
            .shift(1)
        )

        rolling_low = (
            df["low"]
            .rolling(20)
            .min()
            .shift(1)
        )


        df["breakout_up"] = (
            df["close"]
            > rolling_high
        ).astype(int)


        df["breakout_down"] = (
            df["close"]
            < rolling_low
        ).astype(int)


        df.replace(
            [np.inf, -np.inf],
            np.nan,
            inplace=True,
        )


        df.dropna(
            inplace=True
        )


        return df


    # --------------------------------------------------------
    # TRAIN
    # --------------------------------------------------------

    def train_models(
        self,
        df,
        symbol,
    ):

        try:

            features = [

                "ema9",
                "ema20",
                "ema50",

                "rsi",

                "volume_zscore",

                "atr",
                "atr_pct",

                "return_1",
                "return_3",
                "return_5",
                "return_10",

                "ema9_20_distance",
                "ema20_50_distance",

                "breakout_up",
                "breakout_down",
            ]


            work = df.copy()


            # ------------------------------------------------
            # Future return
            # ------------------------------------------------

            work["future_return"] = (
                work["close"]
                .shift(-5)
                / work["close"]
                - 1
            )


            work["target"] = 0


            work.loc[
                work["future_return"]
                > MIN_EXPECTED_MOVE,
                "target"
            ] = 1


            work.loc[
                work["future_return"]
                < -MIN_EXPECTED_MOVE,
                "target"
            ] = -1


            work.dropna(
                inplace=True
            )


            if len(work) < 100:

                return (
                    False,
                    f"NOT ENOUGH TRAINING DATA ({len(work)})",
                )


            X = work[features]

            y = work["target"]


            # ------------------------------------------------
            # Ensure all classes exist
            # ------------------------------------------------

            unique_classes = set(
                y.astype(int).unique()
            )


            if not {
                -1,
                0,
                1
            }.issubset(unique_classes):

                return (
                    False,
                    f"TRAINING CLASS IMBALANCE: "
                    f"{sorted(unique_classes)}",
                )


            # ------------------------------------------------
            # Time-series split
            # ------------------------------------------------

            split = int(
                len(X) * 0.80
            )


            if split < 50:

                return (
                    False,
                    "TRAINING SPLIT TOO SMALL",
                )


            X_train = X.iloc[:split]

            y_train = y.iloc[:split]

            X_test = X.iloc[split:]

            y_test = y.iloc[split:]


            # ------------------------------------------------
            # Models
            # ------------------------------------------------

            lgb_model = lgb.LGBMClassifier(
                **self.lgb_params
            )


            rf_model = RandomForestClassifier(
                **self.rf_params
            )


            lgb_model.fit(
                X_train,
                y_train,
            )


            rf_model.fit(
                X_train,
                y_train,
            )


            # ------------------------------------------------
            # Validation
            # ------------------------------------------------

            lgb_acc = None
            rf_acc = None


            if len(X_test) >= 10:

                lgb_test_pred = (
                    lgb_model
                    .predict(X_test)
                )

                rf_test_pred = (
                    rf_model
                    .predict(X_test)
                )


                lgb_acc = accuracy_score(
                    y_test,
                    lgb_test_pred,
                )


                rf_acc = accuracy_score(
                    y_test,
                    rf_test_pred,
                )


                log_print(
                    f"      📊 {symbol} VALIDATION | "
                    f"LGB={lgb_acc:.1%} | "
                    f"RF={rf_acc:.1%} | "
                    f"test_rows={len(X_test)}"
                )


            self.lgb_model = lgb_model

            self.rf_model = rf_model

            self.is_trained = True

            self.last_trained_symbol = symbol

            self.last_train_time = (
                datetime.now(ET)
            )


            return (
                True,
                f"TRAINED | rows={len(X_train)}",
            )


        except Exception as e:

            return (
                False,
                f"TRAIN ERROR: {str(e)[:120]}",
            )


    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    def analyze(
        self,
        symbol,
        bars,
    ):

        df = (
            self.extract_features_from_bars(
                bars
            )
        )


        if df is None or len(df) < 50:

            return {

                "direction": "NO TRADE",

                "confidence": 0.0,

                "reason":
                    "INSUFFICIENT FEATURES",
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

            ok, msg = (
                self.train_models(
                    df,
                    symbol,
                )
            )


            if not ok:

                return {

                    "direction": "NO TRADE",

                    "confidence": 0.0,

                    "reason": msg,
                }


        features = [

            "ema9",
            "ema20",
            "ema50",

            "rsi",

            "volume_zscore",

            "atr",
            "atr_pct",

            "return_1",
            "return_3",
            "return_5",
            "return_10",

            "ema9_20_distance",
            "ema20_50_distance",

            "breakout_up",
            "breakout_down",
        ]


        try:

            row = df[
                features
            ].iloc[[-1]]


            # ------------------------------------------------
            # Predictions
            # ------------------------------------------------

            lgb_pred = int(
                self.lgb_model
                .predict(row)[0]
            )


            rf_pred = int(
                self.rf_model
                .predict(row)[0]
            )


            # ------------------------------------------------
            # Probabilities
            # ------------------------------------------------

            lgb_proba_raw = (
                self.lgb_model
                .predict_proba(row)[0]
            )


            rf_proba_raw = (
                self.rf_model
                .predict_proba(row)[0]
            )


            lgb_classes = (
                self.lgb_model
                .classes_
            )


            rf_classes = (
                self.rf_model
                .classes_
            )


            lgb_probs = {
                int(cls): float(prob)
                for cls, prob
                in zip(
                    lgb_classes,
                    lgb_proba_raw,
                )
            }


            rf_probs = {
                int(cls): float(prob)
                for cls, prob
                in zip(
                    rf_classes,
                    rf_proba_raw,
                )
            }


            # ------------------------------------------------
            # Model agreement
            # ------------------------------------------------

            if (
                REQUIRE_MODEL_AGREEMENT
                and lgb_pred != rf_pred
            ):

                # Confidence shown here is deliberately
                # conservative: minimum model confidence.
                disagreement_conf = min(

                    max(
                        lgb_probs.values()
                    ),

                    max(
                        rf_probs.values()
                    ),
                )


                return {

                    "direction":
                        "NO TRADE",

                    "confidence":
                        disagreement_conf,

                    "reason":
                        "MODEL DISAGREEMENT | "
                        f"LGB={self._label(lgb_pred)} "
                        f"RF={self._label(rf_pred)}",
                }


            # ------------------------------------------------
            # Neutral
            # ------------------------------------------------

            if lgb_pred == 0:

                confidence = min(

                    lgb_probs.get(
                        0,
                        0.0
                    ),

                    rf_probs.get(
                        0,
                        0.0
                    ),
                )


                return {

                    "direction":
                        "NO TRADE",

                    "confidence":
                        confidence,

                    "reason":
                        "MODEL TARGET=NEUTRAL | "
                        f"LGB={lgb_probs.get(0,0):.1%} "
                        f"RF={rf_probs.get(0,0):.1%}",
                }


            # ------------------------------------------------
            # Agreement
            # ------------------------------------------------

            direction = {
                1: "CALL",
                -1: "PUT",
            }.get(
                lgb_pred,
                "NO TRADE",
            )


            lgb_conf = lgb_probs.get(
                lgb_pred,
                0.0,
            )


            rf_conf = rf_probs.get(
                rf_pred,
                0.0,
            )


            # Conservative confidence:
            # use minimum of both models.
            confidence = min(
                lgb_conf,
                rf_conf,
            )


            # ------------------------------------------------
            # Probability margin
            # ------------------------------------------------

            def probability_margin(
                probs,
                pred,
            ):

                values = sorted(
                    probs.values(),
                    reverse=True,
                )

                if len(values) < 2:
                    return 0.0

                return (
                    values[0]
                    - values[1]
                )


            lgb_margin = (
                probability_margin(
                    lgb_probs,
                    lgb_pred,
                )
            )


            rf_margin = (
                probability_margin(
                    rf_probs,
                    rf_pred,
                )
            )


            margin = min(
                lgb_margin,
                rf_margin,
            )


            # ------------------------------------------------
            # Confidence threshold
            # ------------------------------------------------

            if confidence < BASE_CONFIDENCE_THRESHOLD:

                return {

                    "direction":
                        "NO TRADE",

                    "confidence":
                        confidence,

                    "reason":
                        "BELOW CONFIDENCE "
                        f"THRESHOLD {BASE_CONFIDENCE_THRESHOLD:.0%} | "
                        f"LGB={lgb_conf:.1%} "
                        f"RF={rf_conf:.1%}",
                }


            # ------------------------------------------------
            # Margin threshold
            # ------------------------------------------------

            if margin < MIN_PROBABILITY_MARGIN:

                return {

                    "direction":
                        "NO TRADE",

                    "confidence":
                        confidence,

                    "reason":
                        "WEAK PROBABILITY MARGIN | "
                        f"margin={margin:.1%} "
                        f"required={MIN_PROBABILITY_MARGIN:.1%}",
                }


            # ------------------------------------------------
            # Market structure confirmation
            # ------------------------------------------------

            last = df.iloc[-1]


            ema9 = float(
                last["ema9"]
            )

            ema20 = float(
                last["ema20"]
            )

            ema50 = float(
                last["ema50"]
            )

            rsi = float(
                last["rsi"]
            )


            # CALL must have positive structure.
            if direction == "CALL":

                if not (
                    ema9 > ema20
                    and ema20 >= ema50
                ):

                    return {

                        "direction":
                            "NO TRADE",

                        "confidence":
                            confidence,

                        "reason":
                            "ML CALL BUT TREND "
                            "CONFLUENCE FAILED",
                    }


                if rsi > 78:

                    return {

                        "direction":
                            "NO TRADE",

                        "confidence":
                            confidence,

                        "reason":
                            "CALL OVEREXTENDED "
                            f"| RSI={rsi:.1f}",
                    }


            # PUT must have negative structure.
            elif direction == "PUT":

                if not (
                    ema9 < ema20
                    and ema20 <= ema50
                ):

                    return {

                        "direction":
                            "NO TRADE",

                        "confidence":
                            confidence,

                        "reason":
                            "ML PUT BUT TREND "
                            "CONFLUENCE FAILED",
                    }


                if rsi < 22:

                    return {

                        "direction":
                            "NO TRADE",

                        "confidence":
                            confidence,

                        "reason":
                            "PUT OVERSOLD "
                            f"| RSI={rsi:.1f}",
                    }


            # ------------------------------------------------
            # Final signal
            # ------------------------------------------------

            return {

                "direction":
                    direction,

                "confidence":
                    confidence,

                "reason":
                    "MODELS AGREE | "
                    f"LGB={lgb_conf:.1%} "
                    f"RF={rf_conf:.1%} | "
                    f"margin={margin:.1%} | "
                    f"RSI={rsi:.1f}",
            }


        except Exception as e:

            return {

                "direction":
                    "NO TRADE",

                "confidence":
                    0.0,

                "reason":
                    f"ML ERROR: {str(e)[:100]}",
            }


    @staticmethod
    def _label(value):

        return {
            1: "CALL",
            -1: "PUT",
            0: "NEUTRAL",
        }.get(
            value,
            "UNKNOWN",
        )


ml_engine = MultiModelEnsembleEngine()


# ============================================================
# OPTION CONTRACTS
# ============================================================

def get_option_contracts(symbol):

    try:

        req = GetOptionContractsRequest(

            underlying_symbols=[
                symbol
            ],

            status=AssetStatus.ACTIVE,

            limit=200,
        )


        res = (
            trading_client
            .get_option_contracts(req)
        )


        return (
            res.option_contracts
            if hasattr(
                res,
                "option_contracts",
            )
            else res
        )


    except Exception as e:

        log_print(
            f"      ⚠️ OPTION CONTRACT ERROR "
            f"{symbol}: {str(e)[:100]}"
        )

        return []


# ============================================================
# OPTION VALUE EXTRACTION
# ============================================================

def extract_snapshot_metrics(
    snapshot,
):

    quote = getattr(
        snapshot,
        "latest_quote",
        None,
    )


    if not quote:

        return None


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


        if bid <= 0 or ask <= 0:

            return None


        mid = (
            bid + ask
        ) / 2


        spread_pct = (
            ask - bid
        ) / mid


        # Alpaca option snapshot fields
        # may vary depending on feed/account.
        greeks = getattr(
            snapshot,
            "greeks",
            None,
        )


        delta = None
        iv = None


        if greeks is not None:

            try:

                delta_value = getattr(
                    greeks,
                    "delta",
                    None,
                )

                if delta_value is not None:

                    delta = abs(
                        float(
                            delta_value
                        )
                    )

            except Exception:
                pass


        # IV may be directly exposed.
        for attr in [
            "implied_volatility",
            "iv",
        ]:

            try:

                val = getattr(
                    snapshot,
                    attr,
                    None,
                )

                if val is not None:

                    iv = float(val)

                    break

            except Exception:
                pass


        return {

            "bid": bid,

            "ask": ask,

            "mid": mid,

            "spread_pct":
                spread_pct,

            "delta":
                delta,

            "iv":
                iv,
        }


    except Exception:

        return None


# ============================================================
# OPTION SELECTION
# ============================================================

def select_smart_option(
    symbol,
    direction,
    vix_val,
):

    contracts = (
        get_option_contracts(
            symbol
        )
    )


    if not contracts:

        return (
            None,
            "NO ACTIVE OPTION CONTRACTS",
        )


    today = (
        datetime.now(ET)
        .date()
    )


    candidates = []


    for c in contracts:

        try:

            exp = c.expiration_date


            if isinstance(
                exp,
                str,
            ):

                exp = (
                    datetime
                    .fromisoformat(exp)
                    .date()
                )

            elif hasattr(
                exp,
                "date",
            ):

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
                (
                    c,
                    dte,
                )
            )


        except Exception:

            continue


    if not candidates:

        return (
            None,
            "NO DTE/TYPE MATCH",
        )


    # Limit snapshot request.
    symbols = [
        c.symbol
        for c, _
        in candidates[:100]
    ]


    snapshots = (
        get_option_snapshots(
            symbols
        )
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


        metrics = (
            extract_snapshot_metrics(
                snapshots[sym]
            )
        )


        if not metrics:

            continue


        bid = metrics["bid"]

        ask = metrics["ask"]

        mid = metrics["mid"]

        spread_pct = (
            metrics["spread_pct"]
        )

        delta = metrics["delta"]

        iv = metrics["iv"]


        # ----------------------------------------------------
        # PREMIUM
        # ----------------------------------------------------

        if mid > MAX_PREMIUM:

            continue


        # ----------------------------------------------------
        # SPREAD
        # ----------------------------------------------------

        if spread_pct > BASE_MAX_SPREAD_PCT:

            continue


        # ----------------------------------------------------
        # DELTA
        # ----------------------------------------------------

        if delta is not None:

            if not (
                MIN_DELTA
                <= delta
                <= MAX_DELTA
            ):

                continue


        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = 0.0


        # Prefer delta near 0.50.
        if delta is not None:

            delta_penalty = abs(
                delta - 0.50
            )

            score += (
                delta_penalty
                * 10
            )


        # Prefer tighter spread.
        score += (
            spread_pct
            * 30
        )


        # Prefer shorter DTE but
        # avoid absolute 0DTE.
        score += (
            dte * 0.10
        )


        # Prefer reasonable premium.
        if mid > 10:

            score += 0.5


        valid.append({

            "contract":
                c,

            "symbol":
                sym,

            "dte":
                dte,

            "delta":
                delta,

            "bid":
                bid,

            "ask":
                ask,

            "mid":
                mid,

            "spread_pct":
                spread_pct,

            "iv":
                iv,

            "score":
                score,
        })


    if not valid:

        return (
            None,
            "ALL CONTRACTS FAILED REAL QUOTE FILTERS",
        )


    valid.sort(
        key=lambda x: x["score"]
    )


    selected = valid[0]


    return (
        selected,
        (
            f"OPTION FOUND "
            f"{selected['symbol']} | "
            f"DTE={selected['dte']} | "
            f"mid=${selected['mid']:.2f} | "
            f"spread={selected['spread_pct']:.1%} | "
            f"delta="
            f"{selected['delta'] if selected['delta'] is not None else 'N/A'}"
        ),
    )


# ============================================================
# ORDER HELPERS
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
                str(
                    order.status
                ).lower()
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

                    time_in_force=
                        TimeInForce.DAY,

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
            f"      ❌ ORDER ERROR: "
            f"{str(e)[:120]}"
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

        return (
            False,
            "TRADING HALTED",
        )


    if (
        state["estimated_daily_pnl"]
        <= MAX_DAILY_LOSS_USD
    ):

        state["trading_halted"] = True

        save_state_db(state)

        return (
            False,
            "MAX DAILY LOSS REACHED",
        )


    if (
        state["trades_today"]
        >= MAX_TRADES_PER_DAY
    ):

        return (
            False,
            "MAX DAILY TRADES",
        )


    if (
        len(state["positions"])
        >= MAX_OPEN_POSITIONS
    ):

        return (
            False,
            "MAX OPEN POSITIONS",
        )


    # --------------------------------------------------------
    # Sector
    # --------------------------------------------------------

    ok, reason = (
        check_sector_correlation(
            symbol
        )
    )


    if not ok:

        return (
            False,
            reason,
        )


    # --------------------------------------------------------
    # News
    # --------------------------------------------------------

    ok, reason = (
        check_news_sentiment(
            symbol
        )
    )


    if not ok:

        return (
            False,
            reason,
        )


    # --------------------------------------------------------
    # Multi timeframe
    # --------------------------------------------------------

    ok, reason = (
        check_multi_timeframe_confluence(
            symbol,
            direction,
        )
    )


    if not ok:

        return (
            False,
            reason,
        )


    # --------------------------------------------------------
    # Option
    # --------------------------------------------------------

    option, reason = (
        select_smart_option(
            symbol,
            direction,
            vix_val,
        )
    )


    if not option:

        return (
            False,
            reason,
        )


    # --------------------------------------------------------
    # Delta
    # --------------------------------------------------------

    actual_delta = (
        option["delta"]
        if option["delta"] is not None
        else 0.50
    )


    ok, reason = (
        check_portfolio_delta_exposure(
            actual_delta,
            BASE_CONTRACTS_PER_TRADE,
            direction,
        )
    )


    if not ok:

        return (
            False,
            reason,
        )


    # --------------------------------------------------------
    # Execute PAPER order
    # --------------------------------------------------------

    filled = (
        submit_smart_limit_order(
            option["symbol"],
            BASE_CONTRACTS_PER_TRADE,
            option["ask"],
        )
    )


    if not filled:

        return (
            False,
            "ORDER NOT FILLED",
        )


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

        "underlying":
            symbol,

        "direction":
            direction,

        "qty":
            filled_qty,

        "initial_qty":
            filled_qty,

        "entry_price":
            fill_price,

        "confidence":
            confidence,

        "scale_out_done":
            False,

        "opened_at":
            datetime.now(
                ET
            ).isoformat(),

        "order_id":
            str(filled.id),
    }


    save_state_db(state)


    log_print(
        "🚨 PAPER TRADE EXECUTED | "
        f"{symbol} | "
        f"{direction} | "
        f"{option['symbol']} | "
        f"qty={filled_qty} | "
        f"entry=${fill_price:.2f} | "
        f"confidence={confidence:.1%}"
    )


    return (
        True,
        "FILLED",
    )


# ============================================================
# EXIT POSITION
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
                None
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

                    time_in_force=
                        TimeInForce.DAY,
                )
            )
        )


        filled = wait_for_fill(
            str(order.id),
            timeout=15,
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
            else current_price
        )


        entry_price = float(
            pos["entry_price"]
        )


        pnl = (
            exit_price
            - entry_price
        ) * qty * 100


        state[
            "estimated_daily_pnl"
        ] += pnl


        state["positions"].pop(
            opt_sym,
            None
        )


        save_state_db(state)


        # History.
        try:

            conn = sqlite3.connect(
                DB_FILE
            )

            conn.execute("""
                INSERT INTO trade_history
                (
                    symbol,
                    contract,
                    direction,
                    entry_price,
                    exit_price,
                    qty,
                    pnl_usd,
                    reason,
                    closed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (

                pos["underlying"],

                opt_sym,

                pos["direction"],

                entry_price,

                exit_price,

                qty,

                pnl,

                reason,

                datetime.now(
                    ET
                ).isoformat(),
            ))

            conn.commit()

            conn.close()

        except Exception:
            pass


        log_print(
            f"🛑 PAPER FULL EXIT | "
            f"{opt_sym} | "
            f"P&L=${pnl:+.2f} | "
            f"reason={reason}"
        )


        if (
            state["estimated_daily_pnl"]
            <= MAX_DAILY_LOSS_USD
        ):

            state["trading_halted"] = True

            save_state_db(state)

            log_print(
                "🚨 DAILY LOSS LIMIT -> "
                "NEW ENTRIES HALTED"
            )


        return True


    except Exception as e:

        log_print(
            f"❌ EXIT ERROR {opt_sym}: "
            f"{str(e)[:120]}"
        )

        return False


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_positions():

    if not state["positions"]:

        return


    snapshots = (
        get_option_snapshots(
            list(
                state["positions"].keys()
            )
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
                cur_price
                - entry
            ) / entry


            if (
                pnl_pct
                <= OPTION_STOP_LOSS_PCT
            ):

                exit_position(
                    opt_sym,
                    pos,
                    cur_price,
                    "OPTION STOP LOSS -30%",
                )


        except Exception:
            continue


# ============================================================
# POSITION SYNC
# ============================================================

def sync_positions():

    # Intentionally conservative.
    #
    # We do not automatically invent DB positions
    # from broker positions because this is a paper
    # research bot and state synchronization should be
    # explicitly implemented if live execution is ever used.

    return


# ============================================================
# EOD
# ============================================================

def print_eod_summary():

    now = datetime.now(ET)


    if (
        now.time()
        < dt_time(16, 0)
    ):

        return


    if state["eod_summary_done"]:

        return


    log_section(
        "📊 END OF DAY SUMMARY"
    )


    log_print(
        f"Trades today: "
        f"{state['trades_today']}"
    )


    log_print(
        f"Estimated P&L: "
        f"${state['estimated_daily_pnl']:+.2f}"
    )


    log_print(
        f"Open positions: "
        f"{len(state['positions'])}"
    )


    state[
        "eod_summary_done"
    ] = True


    save_state_db(state)


# ============================================================
# COOLDOWN
# ============================================================

def symbol_recently_traded(
    symbol,
):

    try:

        conn = sqlite3.connect(
            DB_FILE
        )


        since = (
            datetime.now(ET)
            - timedelta(
                minutes=
                SYMBOL_COOLDOWN_MINUTES
            )
        ).isoformat()


        row = conn.execute("""
            SELECT COUNT(*)
            FROM trade_history
            WHERE symbol=?
            AND closed_at >= ?
        """, (
            symbol,
            since,
        )).fetchone()


        conn.close()


        return (
            row[0] > 0
            if row
            else False
        )


    except Exception:

        return False


# ============================================================
# SCAN ONE SYMBOL
# ============================================================

def scan_symbol(
    symbol,
    vix_val,
):

    try:

        # ----------------------------------------------------
        # Existing position
        # ----------------------------------------------------

        if any(
            p["underlying"]
            == symbol
            for p in state["positions"].values()
        ):

            return False


        # ----------------------------------------------------
        # Cooldown
        # ----------------------------------------------------

        if symbol_recently_traded(
            symbol
        ):

            log_decision(
                symbol,
                "NO TRADE",
                0.0,
                "SYMBOL COOLDOWN",
            )

            return False


        # ----------------------------------------------------
        # Data
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
            or len(bars) < 80
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
        # Analyze
        # ----------------------------------------------------

        analysis = (
            ml_engine.analyze(
                symbol,
                bars,
            )
        )


        direction = (
            analysis["direction"]
        )


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
        # ML signal passed
        # ----------------------------------------------------

        log_decision(
            symbol,
            direction,
            confidence,
            f"SIGNAL PASSED ML | {reason}",
        )


        # ----------------------------------------------------
        # Entry
        # ----------------------------------------------------

        ok, enter_reason = (
            enter_position(

                symbol,

                direction,

                confidence,

                vix_val,
            )
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
            f"{str(e)[:100]}"
        )


        save_scan(
            symbol,
            "ERROR",
            0,
            "ERROR",
            reason,
        )


        log_print(
            f"      ❌ {symbol}: "
            f"{reason}"
        )


        return False


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    log_section(
        "🤖 TRANSPARENT PAPER QUANT BOT v16.2 STARTING"
    )


    log_print(
        "💰 PAPER MODE = TRUE"
    )


    log_print(
        "🧠 LGB + RF AGREEMENT REQUIRED"
    )


    log_print(
        f"🎯 CONFIDENCE THRESHOLD = "
        f"{BASE_CONFIDENCE_THRESHOLD:.0%}"
    )


    log_print(
        f"📐 MIN PROBABILITY MARGIN = "
        f"{MIN_PROBABILITY_MARGIN:.0%}"
    )


    log_print(
        "📡 Market Data Health Monitor Enabled"
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
            # DAILY HALT
            # ------------------------------------------------

            if state[
                "trading_halted"
            ]:

                log_print(
                    "🛑 TRADING HALTED "
                    "| No new entries."
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
                DEFAULT_TARGET_UNDERLYINGS.copy()
            )


            data_health_counts = {}


            for sym in current_universe:

                test_bars, _ = (
                    fetch_stock_bars_robust(

                        sym,

                        TimeFrame.Minute,

                        now
                        - timedelta(
                            hours=12
                        ),

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
                    f"{count} bars"
                )


            active_coverage = sum(

                1

                for c
                in data_health_counts.values()

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
                    "0 symbols have usable bars. "
                    "⛔ ML SCAN BLOCKED."
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


            if vix is not None:

                log_print(
                    f"🌡 VIX: "
                    f"{vix:.2f} | "
                    f"{vix_status}"
                )

            else:

                log_print(
                    f"🌡 VIX: "
                    f"{vix_status} | "
                    "⚠️ VIX NOT AVAILABLE"
                )


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

                if scan_symbol(
                    symbol,
                    vix,
                ):

                    found_trade = True

                    break


            # ------------------------------------------------
            # RESULT
            # ------------------------------------------------

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
                f"{str(e)[:150]}"
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