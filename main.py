# ============================================================
# SPX / STOCK OPTIONS PAPER BOT v16.5
# PATIENT / TRANSPARENT / DATA-FIRST
# ============================================================
#
# PAPER ONLY - NO LIVE TRADING
#
# FIXES:
#   1) Scan lookback increased to 72 hours
#   2) UTC normalization before timestamp filtering
#   3) Detailed Alpaca / Yahoo bar-count diagnostics
#   4) Explicit data-source reporting
#   5) Minimum 250 usable raw bars retained
#   6) Separate insufficient-data and stale-data checks
#   7) Training-row diagnostics after feature cleaning
#   8) Yahoo 1m fallback limited by Yahoo's available history
#   9) No fabricated option delta
#  10) Unfilled orders are cancelled after timeout
#
# IMPORTANT:
# This version scans STOCKS and their options.
# It does not directly analyze the SPX index or SPX options.
# ============================================================

import os
import time
import sqlite3
import warnings
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from sklearn.ensemble import (
    RandomForestClassifier,
    HistGradientBoostingClassifier,
)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.historical import (
    StockHistoricalDataClient,
    OptionHistoricalDataClient,
)

from alpaca.data.requests import (
    StockBarsRequest,
    OptionSnapshotRequest,
)

from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, OptionsFeed

warnings.filterwarnings("ignore")


# ============================================================
# CONFIG
# ============================================================

PAPER_MODE = True

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY "
        "environment variables. Configure them in your "
        "deployment environment."
    )

TZ = ZoneInfo("America/New_York")


# ============================================================
# UNIVERSE
# ============================================================

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


# ============================================================
# DATA CONFIG
# ============================================================

STOCK_FEED = DataFeed.IEX
OPTIONS_FEED = OptionsFeed.INDICATIVE

TIMEFRAME = TimeFrame.Minute

# Minimum number of raw minute bars.
MIN_ML_BARS = 250

# Main fix: request a longer history for the scan.
SCAN_LOOKBACK_HOURS = 72

# Health check should use the same adequate lookback.
HEALTH_LOOKBACK_HOURS = 72

# During regular market hours, latest usable bar age limit.
MAX_DATA_AGE_MINUTES = 10

# Yahoo Finance intraday 1-minute fallback.
YF_1M_PERIOD = "7d"


# ============================================================
# ML CONFIG
# ============================================================

CONFIDENCE_THRESHOLD = 0.75
MIN_PROBABILITY_MARGIN = 0.08
REQUIRE_MODEL_AGREEMENT = True
FUTURE_HORIZON_BARS = 2


# ============================================================
# OPTIONS CONFIG
# ============================================================

MIN_DTE = 1
MAX_DTE = 14

MIN_DELTA = 0.40
MAX_DELTA = 0.60

MAX_OPTION_PREMIUM = 15.00
MAX_SPREAD_PCT = 0.10


# ============================================================
# PAPER TRADING RISK CONFIG
# ============================================================

BASE_CONTRACTS_PER_TRADE = 2
MAX_CONTRACTS_PER_TRADE = 8

MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 2

MAX_DAILY_LOSS_USD = -200
MAX_VIX = 30

STOP_LOSS_PCT = -0.30
TARGET_PROFIT_USD = 70

SCAN_INTERVAL_SECONDS = 60
ORDER_FILL_TIMEOUT_SECONDS = 10
SYMBOL_COOLDOWN_MINUTES = 20
ERROR_SLEEP_SECONDS = 20

DB_FILE = "institutional_bot_v165.db"


# ============================================================
# CLIENTS - PAPER MODE ONLY
# ============================================================

if not PAPER_MODE:
    raise RuntimeError(
        "SAFETY BLOCK: this script is configured for PAPER MODE only."
    )

trading_client = TradingClient(
    API_KEY,
    API_SECRET,
    paper=True,
)

stock_data_client = StockHistoricalDataClient(
    API_KEY,
    API_SECRET,
)

option_data_client = OptionHistoricalDataClient(
    API_KEY,
    API_SECRET,
)


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            option_symbol TEXT,
            side TEXT,
            qty INTEGER,
            premium REAL,
            delta REAL,
            confidence REAL,
            margin REAL,
            reason TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            status TEXT,
            bars INTEGER,
            confidence REAL,
            reason TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# LOGGING
# ============================================================

def now_et():
    return datetime.now(TZ)


def log(msg):
    print(
        f"[{now_et().strftime('%Y-%m-%d %H:%M:%S ET')}] {msg}",
        flush=True,
    )


def db_scan_log(symbol, status, bars, confidence, reason):
    conn = None

    try:
        conn = sqlite3.connect(DB_FILE)

        conn.execute(
            """
            INSERT INTO scan_log
            (timestamp, symbol, status, bars, confidence, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                now_et().isoformat(),
                symbol,
                status,
                int(bars or 0),
                float(confidence or 0),
                str(reason),
            ),
        )

        conn.commit()

    except Exception as e:
        log(f"DB scan log error: {e}")

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# DATAFRAME NORMALIZATION
# ============================================================

def normalize_dataframe(df):
    if df is None:
        return None

    if not isinstance(df, pd.DataFrame):
        return None

    if df.empty:
        return None

    df = df.copy()

    # yfinance may return MultiIndex columns.
    if isinstance(df.columns, pd.MultiIndex):
        new_cols = []

        for col in df.columns:
            if isinstance(col, tuple):
                # Usually the first level contains OHLCV names.
                new_cols.append(col[0])
            else:
                new_cols.append(col)

        df.columns = new_cols

    # Remove duplicate column names if present.
    df = df.loc[:, ~pd.Index(df.columns).duplicated(keep="last")]

    required = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]

    missing = [
        col for col in required
        if col not in df.columns
    ]

    if missing:
        log(f"DATA NORMALIZATION | missing columns={missing}")
        return None

    df = df[required].copy()

    for col in required:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(subset=required)

    if df.empty:
        return None

    try:
        # Force every timestamp to UTC.
        df.index = pd.to_datetime(
            df.index,
            utc=True,
            errors="coerce",
        )

        df = df.loc[~df.index.isna()]

    except Exception:
        return None

    if df.empty:
        return None

    df = df.sort_index()

    # Remove duplicate minute bars.
    df = df.loc[
        ~df.index.duplicated(keep="last")
    ]

    return df


# ============================================================
# DATA QUALITY
# ============================================================

def assess_data_quality(bars):
    if bars is None:
        return {
            "status": "DOWN",
            "count": 0,
            "age_minutes": None,
            "reason": "NO DATA",
        }

    count = len(bars)

    if count == 0:
        return {
            "status": "DOWN",
            "count": 0,
            "age_minutes": None,
            "reason": "EMPTY DATA",
        }

    try:
        last_ts = pd.Timestamp(bars.index[-1])

        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        else:
            last_ts = last_ts.tz_convert("UTC")

        now_utc = pd.Timestamp.now(tz="UTC")

        age_minutes = (
            now_utc - last_ts
        ).total_seconds() / 60.0

        # A timestamp in the future should not be accepted.
        if age_minutes < -1:
            return {
                "status": "STALE",
                "count": count,
                "age_minutes": age_minutes,
                "reason": "LATEST BAR TIMESTAMP IS IN THE FUTURE",
            }

        age_minutes = max(0.0, age_minutes)

    except Exception:
        return {
            "status": "STALE",
            "count": count,
            "age_minutes": None,
            "reason": "TIMESTAMP INVALID",
        }

    current = now_et()

    market_open = current.replace(
        hour=9,
        minute=30,
        second=0,
        microsecond=0,
    )

    market_close = current.replace(
        hour=16,
        minute=0,
        second=0,
        microsecond=0,
    )

    # Stale data during regular weekday trading hours
    # must not pass the entry gate.
    if (
        current.weekday() < 5
        and market_open <= current <= market_close
        and age_minutes > MAX_DATA_AGE_MINUTES
    ):
        return {
            "status": "STALE",
            "count": count,
            "age_minutes": age_minutes,
            "reason": f"STALE DATA ({age_minutes:.1f}m old)",
        }

    if count < MIN_ML_BARS:
        return {
            "status": "INSUFFICIENT",
            "count": count,
            "age_minutes": age_minutes,
            "reason": (
                f"INSUFFICIENT BARS ({count}) "
                f"| NEED {MIN_ML_BARS} "
                f"| AGE={age_minutes:.1f}m"
            ),
        }

    return {
        "status": "READY",
        "count": count,
        "age_minutes": age_minutes,
        "reason": (
            f"READY | {count} bars | "
            f"age={age_minutes:.1f}m"
        ),
    }


# ============================================================
# STOCK DATA - ALPACA
# ============================================================

def fetch_alpaca_bars(symbol, start, end):
    try:
        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TIMEFRAME,
            start=start,
            end=end,
            feed=STOCK_FEED,
        )

        result = stock_data_client.get_stock_bars(request)
        df = result.df

        if df is None or df.empty:
            log(f"{symbol}: ALPACA RETURNED ZERO ROWS")
            return None

        # Multi-symbol response often has a symbol/time MultiIndex.
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.xs(symbol, level=0)
            except Exception as e:
                log(
                    f"{symbol}: ALPACA INDEX EXTRACTION WARNING | "
                    f"{str(e)[:120]}"
                )

        rename = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }

        df = df.rename(columns=rename)

        raw_count = len(df)

        df = normalize_dataframe(df)

        normalized_count = (
            len(df) if df is not None else 0
        )

        log(
            f"{symbol}: ALPACA BARS | "
            f"raw={raw_count} | normalized={normalized_count}"
        )

        return df

    except Exception as e:
        log(
            f"{symbol}: Alpaca IEX failed -> "
            f"{str(e)[:180]}"
        )
        return None


# ============================================================
# STOCK DATA - YAHOO FALLBACK
# ============================================================

def fetch_yahoo_bars(symbol):
    try:
        log(
            f"{symbol}: YAHOO REQUEST | "
            f"interval=1m | period={YF_1M_PERIOD}"
        )

        df = yf.download(
            symbol,
            period=YF_1M_PERIOD,
            interval="1m",
            auto_adjust=False,
            progress=False,
            prepost=False,
            threads=False,
        )

        raw_count = (
            len(df) if isinstance(df, pd.DataFrame) else 0
        )

        log(
            f"{symbol}: YAHOO RAW ROWS={raw_count}"
        )

        df = normalize_dataframe(df)

        if df is None:
            log(f"{symbol}: YAHOO NORMALIZATION FAILED")
            return None

        log(
            f"{symbol}: YAHOO NORMALIZED ROWS={len(df)}"
        )

        return df

    except Exception as e:
        log(
            f"{symbol}: Yahoo fallback failed -> "
            f"{str(e)[:180]}"
        )
        return None


# ============================================================
# ROBUST STOCK FETCH
# ============================================================

def fetch_stock_bars_robust(
    symbol,
    lookback_hours=SCAN_LOOKBACK_HOURS,
    purpose="SCAN",
):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_hours)

    # Normalize filter timestamps to UTC explicitly.
    start_ts = pd.Timestamp(start)

    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")

    end_ts = pd.Timestamp(end)

    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")

    log(
        f"{symbol}: FETCH START | "
        f"purpose={purpose} | "
        f"lookback={lookback_hours}h | "
        f"from={start_ts.isoformat()} | "
        f"to={end_ts.isoformat()}"
    )

    # --------------------------------------------------------
    # 1. Alpaca
    # --------------------------------------------------------

    alpaca_df = fetch_alpaca_bars(
        symbol,
        start,
        end,
    )

    alpaca_count = (
        len(alpaca_df)
        if alpaca_df is not None
        else 0
    )

    log(
        f"{symbol}: ALPACA NORMALIZED BARS={alpaca_count}"
    )

    if alpaca_df is not None and not alpaca_df.empty:
        alpaca_df = alpaca_df.copy()

        alpaca_df.index = pd.to_datetime(
            alpaca_df.index,
            utc=True,
        )

        alpaca_df = alpaca_df.sort_index()

        alpaca_df = alpaca_df.loc[
            ~alpaca_df.index.duplicated(keep="last")
        ]

        # Ensure bars are inside the requested window.
        alpaca_before = len(alpaca_df)

        alpaca_df = alpaca_df.loc[
            (alpaca_df.index >= start_ts)
            & (alpaca_df.index <= end_ts)
        ].copy()

        log(
            f"{symbol}: ALPACA WINDOW FILTER | "
            f"before={alpaca_before} | after={len(alpaca_df)}"
        )

        alpaca_quality = assess_data_quality(alpaca_df)

        log(
            f"{symbol}: ALPACA QUALITY | "
            f"{alpaca_quality['status']} | "
            f"{alpaca_quality['reason']}"
        )

        if alpaca_quality["status"] == "READY":
            log(
                f"{symbol}: DATA SOURCE=ALPACA IEX | "
                f"FINAL BARS={len(alpaca_df)}"
            )

            return alpaca_df, alpaca_quality["reason"]

    # --------------------------------------------------------
    # 2. Yahoo fallback
    # --------------------------------------------------------

    yahoo_df = fetch_yahoo_bars(symbol)

    if yahoo_df is None or yahoo_df.empty:
        log(
            f"{symbol}: BOTH SOURCES FAILED OR EMPTY | "
            f"ALPACA={alpaca_count} | YAHOO=0"
        )

        return None, "NO DATA FROM ALPACA OR YAHOO"

    yahoo_df = yahoo_df.copy()

    # Force UTC before comparisons to prevent timezone errors.
    yahoo_df.index = pd.to_datetime(
        yahoo_df.index,
        utc=True,
    )

    yahoo_df = yahoo_df.sort_index()

    yahoo_df = yahoo_df.loc[
        ~yahoo_df.index.duplicated(keep="last")
    ]

    yahoo_raw_count = len(yahoo_df)

    # Filter to requested window, including the end boundary.
    yahoo_df = yahoo_df.loc[
        (yahoo_df.index >= start_ts)
        & (yahoo_df.index <= end_ts)
    ].copy()

    yahoo_filtered_count = len(yahoo_df)

    log(
        f"{symbol}: YAHOO FILTER | "
        f"before={yahoo_raw_count} | "
        f"after={yahoo_filtered_count} | "
        f"removed={yahoo_raw_count - yahoo_filtered_count}"
    )

    if yahoo_df.empty:
        log(
            f"{symbol}: YAHOO HAS NO BARS IN REQUESTED WINDOW"
        )

        return None, "YAHOO EMPTY AFTER TIME FILTER"

    yahoo_quality = assess_data_quality(yahoo_df)

    log(
        f"{symbol}: YAHOO QUALITY | "
        f"{yahoo_quality['status']} | "
        f"{yahoo_quality['reason']}"
    )

    if yahoo_quality["status"] == "READY":
        log(
            f"{symbol}: DATA SOURCE=YAHOO | "
            f"FINAL BARS={len(yahoo_df)}"
        )

        return yahoo_df, yahoo_quality["reason"]

    log(
        f"{symbol}: YAHOO NOT READY | "
        f"FINAL BARS={len(yahoo_df)} | "
        f"REASON={yahoo_quality['reason']}"
    )

    return yahoo_df, yahoo_quality["reason"]


# ============================================================
# FEATURES
# ============================================================

def calculate_rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def build_features(bars):
    if bars is None or bars.empty:
        return None

    df = bars.copy()

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # Trend
    df["ema9"] = close.ewm(
        span=9,
        adjust=False,
    ).mean()

    df["ema20"] = close.ewm(
        span=20,
        adjust=False,
    ).mean()

    df["ema50"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    # RSI
    df["rsi"] = calculate_rsi(close, 14)

    # Returns
    df["return_1"] = close.pct_change(1)
    df["return_3"] = close.pct_change(3)
    df["return_5"] = close.pct_change(5)
    df["return_10"] = close.pct_change(10)

    # EMA distances
    df["ema9_dist"] = close / df["ema9"] - 1
    df["ema20_dist"] = close / df["ema20"] - 1
    df["ema50_dist"] = close / df["ema50"] - 1

    # ATR
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    df["atr"] = true_range.rolling(14).mean()
    df["atr_pct"] = df["atr"] / close

    # Volume Z-score
    vol_mean = volume.rolling(30).mean()
    vol_std = volume.rolling(30).std()

    df["volume_z"] = (
        (volume - vol_mean)
        / vol_std.replace(0, np.nan)
    )

    # Breakout
    rolling_high = high.rolling(20).max().shift(1)
    rolling_low = low.rolling(20).min().shift(1)

    df["breakout_up"] = (
        close > rolling_high
    ).astype(int)

    df["breakout_down"] = (
        close < rolling_low
    ).astype(int)

    # Trend regime
    df["trend_up"] = (
        (df["ema9"] > df["ema20"])
        & (df["ema20"] > df["ema50"])
    ).astype(int)

    df["trend_down"] = (
        (df["ema9"] < df["ema20"])
        & (df["ema20"] < df["ema50"])
    ).astype(int)

    return df


# ============================================================
# TRAINING DATA
# ============================================================

FEATURE_COLUMNS = [
    "ema9_dist",
    "ema20_dist",
    "ema50_dist",
    "rsi",
    "return_1",
    "return_3",
    "return_5",
    "return_10",
    "atr_pct",
    "volume_z",
    "breakout_up",
    "breakout_down",
    "trend_up",
    "trend_down",
]


def make_training_dataset(feature_df):
    if feature_df is None or feature_df.empty:
        log("ML DATASET ERROR | feature dataframe is empty")
        return None, None

    df = feature_df.copy()

    df["future_return"] = (
        df["Close"].shift(-FUTURE_HORIZON_BARS)
        / df["Close"]
        - 1
    )

    # 1 = UP, 0 = NEUTRAL, -1 = DOWN
    threshold = 0.0005

    df["target"] = np.where(
        df["future_return"] > threshold,
        1,
        np.where(
            df["future_return"] < -threshold,
            -1,
            0,
        ),
    )

    rows_before_cleaning = len(df)

    df = df.dropna(
        subset=FEATURE_COLUMNS + ["target"]
    )

    rows_after_cleaning = len(df)

    X = df[FEATURE_COLUMNS].replace(
        [np.inf, -np.inf],
        np.nan,
    )

    y = df["target"]

    valid = X.notna().all(axis=1)

    X = X.loc[valid]
    y = y.loc[valid]

    log(
        f"ML DATASET | "
        f"before_cleaning={rows_before_cleaning} | "
        f"after_cleaning={rows_after_cleaning} | "
        f"valid_rows={len(X)} | "
        f"features={len(FEATURE_COLUMNS)}"
    )

    # Keep the original hard requirement of at least 150
    # cleaned training rows, while logging the 250-bar
    # preference separately.
    if len(X) < 150:
        log(
            f"ML DATASET REJECTED | "
            f"valid_rows={len(X)} | minimum=150"
        )
        return None, None

    if len(X) < MIN_ML_BARS - 30:
        log(
            f"ML DATASET WARNING | "
            f"valid_rows={len(X)} | "
            f"preferred minimum={MIN_ML_BARS - 30}"
        )

    return X, y


# ============================================================
# MODEL ENGINE
# ============================================================

class MultiModelEnsembleEngine:

    def __init__(self):
        self.lgb_like = HistGradientBoostingClassifier(
            max_iter=80,
            learning_rate=0.05,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=42,
        )

        self.rf = RandomForestClassifier(
            n_estimators=120,
            max_depth=7,
            min_samples_leaf=4,
            class_weight="balanced_subsample",
            random_state=42,
            n_jobs=-1,
        )

    def train(self, X, y):
        split = int(len(X) * 0.80)

        if split < 100:
            log(
                f"MODEL TRAINING FAILED | "
                f"training split too small ({split})"
            )
            return False

        X_train = X.iloc[:split]
        y_train = y.iloc[:split]

        if y_train.nunique() < 2:
            log(
                "MODEL TRAINING FAILED | "
                "training data contains fewer than 2 classes"
            )
            return False

        try:
            self.lgb_like.fit(X_train, y_train)
            self.rf.fit(X_train, y_train)
            return True

        except Exception as e:
            log(f"MODEL TRAINING ERROR | {str(e)[:180]}")
            return False

    @staticmethod
    def probabilities(model, X):
        raw = model.predict_proba(X)[0]
        classes = model.classes_

        result = {
            -1: 0.0,
            0: 0.0,
            1: 0.0,
        }

        for cls, prob in zip(classes, raw):
            result[int(cls)] = float(prob)

        return result

    def predict(self, X):
        p1 = self.probabilities(self.lgb_like, X)
        p2 = self.probabilities(self.rf, X)

        direction1 = max(p1, key=p1.get)
        direction2 = max(p2, key=p2.get)

        agreement = direction1 == direction2

        combined = {
            k: (p1[k] + p2[k]) / 2
            for k in [-1, 0, 1]
        }

        direction = max(combined, key=combined.get)

        sorted_probs = sorted(
            combined.values(),
            reverse=True,
        )

        confidence = sorted_probs[0]
        second = sorted_probs[1] if len(sorted_probs) > 1 else 0.0
        margin = confidence - second

        return {
            "direction": direction,
            "confidence": confidence,
            "margin": margin,
            "agreement": agreement,
            "model1": p1,
            "model2": p2,
            "combined": combined,
        }


# ============================================================
# ML SIGNAL
# ============================================================

def get_ml_signal(bars):
    quality = assess_data_quality(bars)

    if quality["status"] != "READY":
        return {
            "trade": False,
            "direction": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "reason": quality["reason"],
        }

    log(
        f"ML INPUT | raw_bars={len(bars)} | "
        f"minimum={MIN_ML_BARS}"
    )

    features = build_features(bars)

    if features is None:
        return {
            "trade": False,
            "direction": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "reason": "FEATURE BUILD FAILED",
        }

    log(
        f"ML FEATURES | rows={len(features)} | "
        f"columns={len(features.columns)}"
    )

    X, y = make_training_dataset(features)

    if X is None or y is None:
        return {
            "trade": False,
            "direction": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "reason": "TRAINING DATA INSUFFICIENT",
        }

    if len(X) < 150:
        return {
            "trade": False,
            "direction": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "reason": f"INSUFFICIENT TRAINING ROWS ({len(X)})",
        }

    engine = MultiModelEnsembleEngine()

    if not engine.train(X, y):
        return {
            "trade": False,
            "direction": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "reason": "MODEL TRAINING FAILED",
        }

    latest = features[FEATURE_COLUMNS].replace(
        [np.inf, -np.inf],
        np.nan,
    ).dropna()

    if latest.empty:
        return {
            "trade": False,
            "direction": 0,
            "confidence": 0.0,
            "margin": 0.0,
            "reason": "LATEST FEATURES INVALID",
        }

    X_live = latest.tail(1)
    result = engine.predict(X_live)

    direction = result["direction"]
    confidence = result["confidence"]
    margin = result["margin"]
    agreement = result["agreement"]

    if direction == 0:
        return {
            "trade": False,
            "direction": 0,
            "confidence": confidence,
            "margin": margin,
            "reason": f"NEUTRAL | confidence={confidence:.1%}",
        }

    if confidence < CONFIDENCE_THRESHOLD:
        return {
            "trade": False,
            "direction": direction,
            "confidence": confidence,
            "margin": margin,
            "reason": (
                f"CONFIDENCE LOW | "
                f"{confidence:.1%} < {CONFIDENCE_THRESHOLD:.1%}"
            ),
        }

    if margin < MIN_PROBABILITY_MARGIN:
        return {
            "trade": False,
            "direction": direction,
            "confidence": confidence,
            "margin": margin,
            "reason": (
                f"PROBABILITY MARGIN LOW | "
                f"{margin:.1%} < {MIN_PROBABILITY_MARGIN:.1%}"
            ),
        }

    if REQUIRE_MODEL_AGREEMENT and not agreement:
        return {
            "trade": False,
            "direction": direction,
            "confidence": confidence,
            "margin": margin,
            "reason": "MODEL DISAGREEMENT",
        }

    return {
        "trade": True,
        "direction": direction,
        "confidence": confidence,
        "margin": margin,
        "reason": (
            f"PASS | "
            f"direction={'CALL' if direction == 1 else 'PUT'} | "
            f"confidence={confidence:.1%} | "
            f"margin={margin:.1%} | "
            f"agreement={agreement}"
        ),
    }


# ============================================================
# MARKET / ACCOUNT
# ============================================================

def get_market_clock():
    try:
        return trading_client.get_clock()
    except Exception as e:
        log(f"Clock error: {e}")
        return None


def market_is_open():
    clock = get_market_clock()

    if clock is None:
        return False

    return bool(clock.is_open)


def get_account():
    try:
        return trading_client.get_account()
    except Exception as e:
        log(f"Account error: {e}")
        return None


def get_open_positions():
    try:
        return trading_client.get_all_positions()
    except Exception as e:
        log(f"Positions error: {e}")
        return []


def get_open_position_count():
    return len(get_open_positions())


def get_today_trade_count():
    try:
        orders = trading_client.get_orders(filter=None)
        today = now_et().date()
        count = 0

        for order in orders:
            created = getattr(order, "created_at", None)

            if created is None:
                continue

            try:
                created_ts = pd.Timestamp(created)

                if created_ts.tzinfo is None:
                    created_ts = created_ts.tz_localize("UTC")

                order_date = created_ts.tz_convert(TZ).date()

            except Exception:
                continue

            if order_date == today:
                count += 1

        return count

    except Exception as e:
        log(f"Trade count error: {e}")
        return 0


# ============================================================
# VIX
# ============================================================

def get_vix():
    try:
        df = yf.download(
            "^VIX",
            period="2d",
            interval="5m",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

        df = normalize_dataframe(df)

        if df is None or df.empty:
            return None

        return float(df["Close"].iloc[-1])

    except Exception as e:
        log(f"VIX unavailable: {str(e)[:150]}")
        return None


def vix_filter():
    vix = get_vix()

    if vix is None:
        return False, "VIX UNKNOWN"

    if vix > MAX_VIX:
        return False, f"VIX TOO HIGH {vix:.2f} > {MAX_VIX}"

    return True, f"VIX OK {vix:.2f}"


# ============================================================
# DAILY LOSS
# ============================================================

def get_daily_pnl():
    account = get_account()

    if account is None:
        return None

    try:
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        return equity - last_equity

    except Exception:
        return None


def daily_loss_filter():
    pnl = get_daily_pnl()

    if pnl is None:
        return False, "DAILY PNL UNKNOWN"

    if pnl <= MAX_DAILY_LOSS_USD:
        return False, f"DAILY LOSS LIMIT {pnl:.2f}"

    return True, f"DAILY PNL {pnl:+.2f}"


# ============================================================
# OPTION CONTRACT SEARCH
# ============================================================

def get_option_contracts(
    underlying,
    option_type,
    underlying_price,
):
    try:
        from alpaca.trading.requests import GetOptionContractsRequest

        today = now_et().date()

        exp_min = today + timedelta(days=MIN_DTE)
        exp_max = today + timedelta(days=MAX_DTE)

        strike_low = underlying_price * 0.92
        strike_high = underlying_price * 1.08

        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status="active",
            expiration_date_gte=exp_min,
            expiration_date_lte=exp_max,
            type=option_type,
            strike_price_gte=strike_low,
            strike_price_lte=strike_high,
            limit=1000,
        )

        result = trading_client.get_option_contracts(req)

        contracts = getattr(
            result,
            "option_contracts",
            None,
        )

        return contracts if contracts is not None else []

    except Exception as e:
        log(
            f"{underlying}: contract search failed -> "
            f"{str(e)[:180]}"
        )
        return []


# ============================================================
# OPTION SNAPSHOT
# ============================================================

def get_option_snapshot(symbol):
    try:
        req = OptionSnapshotRequest(
            symbol_or_symbols=[symbol],
            feed=OPTIONS_FEED,
        )

        result = option_data_client.get_option_snapshot(req)

        if hasattr(result, "get"):
            snap = result.get(symbol)
        else:
            snap = None

        if snap is None:
            try:
                snap = result[symbol]
            except Exception:
                snap = None

        return snap

    except Exception as e:
        log(
            f"{symbol}: snapshot failed -> "
            f"{str(e)[:160]}"
        )
        return None


# ============================================================
# SAFE ATTRIBUTE
# ============================================================

def attr(obj, name, default=None):
    if obj is None:
        return default

    try:
        value = getattr(obj, name)
        return default if value is None else value

    except Exception:
        return default


# ============================================================
# OPTION SELECTION
# ============================================================

def select_option_contract(
    underlying,
    direction,
    underlying_price,
):
    option_type = "call" if direction == 1 else "put"

    contracts = get_option_contracts(
        underlying,
        option_type,
        underlying_price,
    )

    if not contracts:
        return None, "NO CONTRACTS"

    candidates = []

    for contract in contracts:
        symbol = attr(contract, "symbol")

        if not symbol:
            continue

        expiration = attr(contract, "expiration_date")
        strike = attr(contract, "strike_price")

        if expiration is None or strike is None:
            continue

        try:
            if isinstance(expiration, datetime):
                exp_date = expiration.date()
            else:
                exp_date = expiration

            dte = (exp_date - now_et().date()).days

        except Exception:
            continue

        if not MIN_DTE <= dte <= MAX_DTE:
            continue

        try:
            strike = float(strike)
        except Exception:
            continue

        snap = get_option_snapshot(symbol)

        if snap is None:
            continue

        quote = attr(snap, "latest_quote")
        greeks = attr(snap, "greeks")

        # Require a real Greek value from the data source.
        delta = attr(greeks, "delta")

        if delta is None:
            log(f"{symbol}: REJECT | NO REAL DELTA")
            continue

        try:
            delta = float(delta)
        except Exception:
            continue

        abs_delta = abs(delta)

        if not MIN_DELTA <= abs_delta <= MAX_DELTA:
            continue

        bid = attr(quote, "bid_price")
        ask = attr(quote, "ask_price")

        if bid is None or ask is None:
            continue

        try:
            bid = float(bid)
            ask = float(ask)
        except Exception:
            continue

        if bid <= 0 or ask <= 0 or ask < bid:
            continue

        mid = (bid + ask) / 2

        if mid <= 0:
            continue

        spread_pct = (ask - bid) / mid

        if spread_pct > MAX_SPREAD_PCT:
            continue

        if ask > MAX_OPTION_PREMIUM:
            continue

        candidates.append({
            "symbol": symbol,
            "type": option_type,
            "strike": strike,
            "expiration": exp_date,
            "dte": dte,
            "delta": delta,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct,
        })

    if not candidates:
        return (
            None,
            "NO OPTION PASSED REAL DELTA / QUOTE FILTER",
        )

    def score(item):
        delta_score = abs(abs(item["delta"]) - 0.50)
        spread_score = item["spread_pct"]
        dte_score = abs(item["dte"] - 5) * 0.01

        return delta_score * 2 + spread_score + dte_score

    candidates.sort(key=score)

    return candidates[0], "OPTION SELECTED"


# ============================================================
# SAVE TRADE
# ============================================================

def save_trade(
    symbol,
    option,
    qty,
    confidence,
    margin,
    direction,
):
    conn = None

    try:
        conn = sqlite3.connect(DB_FILE)

        conn.execute(
            """
            INSERT INTO trade_history
            (
                timestamp,
                symbol,
                option_symbol,
                side,
                qty,
                premium,
                delta,
                confidence,
                margin,
                reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_et().isoformat(),
                symbol,
                option["symbol"],
                "CALL" if direction == 1 else "PUT",
                qty,
                option["ask"],
                option["delta"],
                confidence,
                margin,
                "v16.5 PAPER",
            ),
        )

        conn.commit()

    except Exception as e:
        log(f"Trade DB error: {e}")

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# PAPER ORDER
# ============================================================

def submit_option_order(
    option,
    confidence,
    margin,
    direction,
):
    if not PAPER_MODE:
        raise RuntimeError(
            "SAFETY BLOCK: PAPER_MODE must remain True."
        )

    qty = min(
        BASE_CONTRACTS_PER_TRADE,
        MAX_CONTRACTS_PER_TRADE,
    )

    symbol = option["symbol"]
    ask = option["ask"]

    limit_price = round(ask, 2)

    log(
        f"ORDER PREP | "
        f"{symbol} | "
        f"qty={qty} | "
        f"limit=${limit_price:.2f} | "
        f"delta={option['delta']:.3f} | "
        f"confidence={confidence:.1%} | "
        f"margin={margin:.1%} | PAPER"
    )

    try:
        order_req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )

        order = trading_client.submit_order(
            order_data=order_req
        )

        order_id = str(order.id)

        log(
            f"ORDER SUBMITTED | "
            f"{symbol} | id={order_id} | PAPER"
        )

        deadline = time.time() + ORDER_FILL_TIMEOUT_SECONDS

        while time.time() < deadline:
            time.sleep(2)

            try:
                current = trading_client.get_order_by_id(
                    order_id
                )

                status = str(current.status).lower()

                if "filled" in status:
                    filled_qty = int(
                        float(
                            getattr(
                                current,
                                "filled_qty",
                                qty,
                            ) or qty
                        )
                    )

                    log(
                        f"ORDER FILLED | "
                        f"{symbol} | qty={filled_qty}"
                    )

                    save_trade(
                        symbol,
                        option,
                        filled_qty,
                        confidence,
                        margin,
                        direction,
                    )

                    return True

                if any(
                    state in status
                    for state in (
                        "canceled",
                        "rejected",
                        "expired",
                    )
                ):
                    log(
                        f"ORDER ENDED | "
                        f"status={status}"
                    )
                    return False

            except Exception as e:
                log(
                    f"Order status error: {str(e)[:150]}"
                )

        # Cancel the unfilled order after the timeout.
        try:
            trading_client.cancel_order_by_id(order_id)

            log(
                f"ORDER CANCEL REQUESTED | "
                f"not filled within "
                f"{ORDER_FILL_TIMEOUT_SECONDS}s"
            )

        except Exception as e:
            log(
                f"URGENT: cancel request failed | "
                f"{str(e)[:180]}"
            )

        return False

    except Exception as e:
        log(
            f"ORDER ERROR | {str(e)[:200]}"
        )
        return False


# ============================================================
# COOLDOWN
# ============================================================

last_trade_by_symbol = {}


def symbol_on_cooldown(symbol):
    ts = last_trade_by_symbol.get(symbol)

    if ts is None:
        return False

    age = (now_et() - ts).total_seconds() / 60

    return age < SYMBOL_COOLDOWN_MINUTES


def mark_symbol_trade(symbol):
    last_trade_by_symbol[symbol] = now_et()


# ============================================================
# GLOBAL ENTRY FILTER
# ============================================================

def global_filters():
    if not market_is_open():
        return False, "MARKET CLOSED"

    open_count = get_open_position_count()

    if open_count >= MAX_OPEN_POSITIONS:
        return False, (
            f"MAX OPEN POSITIONS "
            f"{open_count}/{MAX_OPEN_POSITIONS}"
        )

    trade_count = get_today_trade_count()

    if trade_count >= MAX_TRADES_PER_DAY:
        return False, (
            f"MAX DAILY TRADES "
            f"{trade_count}/{MAX_TRADES_PER_DAY}"
        )

    ok, reason = daily_loss_filter()

    if not ok:
        return False, reason

    ok, reason = vix_filter()

    if not ok:
        return False, reason

    return True, "GLOBAL FILTERS PASS"


# ============================================================
# SCAN SYMBOL
# ============================================================

def scan_symbol(symbol):
    log("--------------------------------------------------")
    log(f"SCAN {symbol}")

    if symbol_on_cooldown(symbol):
        log(f"{symbol}: WAIT | SYMBOL COOLDOWN")
        return False

    bars, data_reason = fetch_stock_bars_robust(
        symbol,
        lookback_hours=SCAN_LOOKBACK_HOURS,
        purpose="SCAN",
    )

    count = len(bars) if bars is not None else 0

    log(
        f"{symbol}: SCAN DATA SUMMARY | "
        f"bars={count} | "
        f"minimum={MIN_ML_BARS} | "
        f"reason={data_reason}"
    )

    quality = assess_data_quality(bars)

    if quality["status"] != "READY":
        log(
            f"{symbol}: ML=NO TRADE | "
            f"{quality['status']} | "
            f"{quality['reason']}"
        )

        db_scan_log(
            symbol,
            quality["status"],
            count,
            0,
            quality["reason"],
        )

        return False

    signal = get_ml_signal(bars)

    direction = signal["direction"]
    confidence = signal["confidence"]
    margin = signal["margin"]

    log(
        f"{symbol}: "
        f"ML={'TRADE' if signal['trade'] else 'NO TRADE'} | "
        f"direction={direction} | "
        f"confidence={confidence:.1%} | "
        f"margin={margin:.1%} | "
        f"{signal['reason']}"
    )

    db_scan_log(
        symbol,
        "SIGNAL" if signal["trade"] else "WAIT",
        count,
        confidence,
        signal["reason"],
    )

    if not signal["trade"]:
        return False

    try:
        underlying_price = float(
            bars["Close"].iloc[-1]
        )

    except Exception:
        log(f"{symbol}: WAIT | PRICE INVALID")
        return False

    option, option_reason = select_option_contract(
        symbol,
        direction,
        underlying_price,
    )

    if option is None:
        log(f"{symbol}: WAIT | {option_reason}")
        return False

    log(
        f"{symbol}: OPTION PASS | "
        f"{option['symbol']} | "
        f"{option['type'].upper()} | "
        f"strike={option['strike']} | "
        f"DTE={option['dte']} | "
        f"delta={option['delta']:.3f} | "
        f"bid={option['bid']:.2f} | "
        f"ask={option['ask']:.2f} | "
        f"spread={option['spread_pct']:.1%}"
    )

    filled = submit_option_order(
        option,
        confidence,
        margin,
        direction,
    )

    if filled:
        mark_symbol_trade(symbol)
        log(f"{symbol}: TRADE COMPLETE")
        return True

    log(f"{symbol}: ORDER NOT FILLED -> WAIT")
    return False


# ============================================================
# HEALTH CHECK
# ============================================================

def health_check():
    log("================ DATA HEALTH ================")

    for symbol in DEFAULT_TARGET_UNDERLYINGS:
        try:
            bars, reason = fetch_stock_bars_robust(
                symbol,
                lookback_hours=HEALTH_LOOKBACK_HOURS,
                purpose="HEALTH",
            )

            count = len(bars) if bars is not None else 0

            quality = assess_data_quality(bars)

            if quality["status"] == "READY":
                log(
                    f"{symbol}: READY | "
                    f"{count} bars | "
                    f"{reason}"
                )

            elif quality["status"] == "INSUFFICIENT":
                log(
                    f"{symbol}: INSUFFICIENT | "
                    f"{count} bars | "
                    f"need {MIN_ML_BARS} | "
                    f"{reason}"
                )

            elif quality["status"] == "STALE":
                log(
                    f"{symbol}: STALE | "
                    f"{count} bars | "
                    f"{reason}"
                )

            else:
                log(
                    f"{symbol}: DOWN | "
                    f"{reason}"
                )

        except Exception as e:
            log(
                f"{symbol}: HEALTH ERROR | "
                f"{str(e)[:150]}"
            )

    log("=============================================")


# ============================================================
# MAIN
# ============================================================

def main():
    log("==================================================")
    log("SPX / STOCK OPTIONS PAPER BOT v16.5")
    log("PATIENT / TRANSPARENT / DATA-FIRST")
    log("PAPER MODE = TRUE")
    log(f"MIN ML BARS = {MIN_ML_BARS}")
    log(f"SCAN LOOKBACK = {SCAN_LOOKBACK_HOURS} HOURS")
    log(f"HEALTH LOOKBACK = {HEALTH_LOOKBACK_HOURS} HOURS")
    log(f"CONFIDENCE >= {CONFIDENCE_THRESHOLD:.1%}")
    log(
        f"PROBABILITY MARGIN >= "
        f"{MIN_PROBABILITY_MARGIN:.1%}"
    )
    log(f"DELTA = {MIN_DELTA:.2f} - {MAX_DELTA:.2f}")
    log(f"MAX SPREAD = {MAX_SPREAD_PCT:.1%}")
    log("UNDERLYINGS = " + ", ".join(DEFAULT_TARGET_UNDERLYINGS))
    log("==================================================")

    last_health_check = 0
    consecutive_errors = 0

    while True:
        try:
            # Health check every 10 minutes.
            if time.time() - last_health_check > 600:
                health_check()
                last_health_check = time.time()

            # Global gate.
            ok, reason = global_filters()

            if not ok:
                log(f"GLOBAL WAIT | {reason}")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            log(f"GLOBAL PASS | {reason}")

            trade_done = False

            for symbol in DEFAULT_TARGET_UNDERLYINGS:
                try:
                    if scan_symbol(symbol):
                        trade_done = True
                        break

                except Exception as e:
                    log(
                        f"{symbol}: SCAN ERROR | "
                        f"{str(e)[:200]}"
                    )

                    consecutive_errors += 1
                    time.sleep(2)

            if trade_done:
                log("TRADE FOUND -> waiting for next cycle")
            else:
                log("PATIENT MODE: لا توجد فرصة مؤكدة.")

            consecutive_errors = 0
            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log("BOT STOPPED BY USER")
            break

        except Exception as e:
            consecutive_errors += 1

            log(
                f"MAIN ERROR #{consecutive_errors}: "
                f"{str(e)[:250]}"
            )

            time.sleep(ERROR_SLEEP_SECONDS)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()