
# ============================================================
# ALPACA AI OPTIONS BOT v17.0
# PART 1/4 — CONFIG, CLIENTS, LOGGING, DATABASE, MARKET DATA
# PAPER MODE ONLY — AUTOMATED OPTIONS TRADING
# ============================================================

import os
import sys
import time
import json
import math
import sqlite3
import logging
import traceback
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, List, Tuple, Any

import requests
import numpy as np
import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.data.historical import (
    StockHistoricalDataClient,
    OptionHistoricalDataClient,
)
from alpaca.data.requests import (
    StockBarsRequest,
    OptionChainRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, OptionsFeed


# =========================
# 1. SAFETY / CONFIGURATION
# =========================

BOT_NAME = "ALPACA AI OPTIONS BOT v17.0"
PAPER_MODE = True  # DO NOT CHANGE TO FALSE

API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
API_SECRET = os.getenv("ALPACA_API_SECRET", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIMEZONE_NY = ZoneInfo("America/New_York")
TIMEFRAME = TimeFrame.Minute
HISTORY_DAYS = 10
BAR_LIMIT = 10000
MIN_TRAIN_ROWS = 150
MAX_DATA_AGE_MINUTES = 10

# Underlying stocks — this version does NOT trade SPX/SPXW.
TRADE_SYMBOLS = [
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN",
    "AMD", "META", "GOOGL", "NFLX",
]

MARKET_CONFIRMATION_SYMBOLS = ["SPY", "QQQ"]

# Data source. IEX may have less coverage than SIP.
STOCK_FEED_NAME = os.getenv("ALPACA_STOCK_FEED", "iex").lower()
OPTIONS_FEED_NAME = os.getenv("ALPACA_OPTIONS_FEED", "opra").lower()

if STOCK_FEED_NAME == "sip":
    STOCK_FEED = DataFeed.SIP
else:
    STOCK_FEED = DataFeed.IEX

# For actual option-entry validation, prefer OPRA.
# If the account cannot access OPRA, the bot should WAIT
# rather than silently treating indicative quotes as executable.
OPTIONS_FEED = (
    OptionsFeed.OPRA
    if OPTIONS_FEED_NAME == "opra"
    else OptionsFeed.INDICATIVE
)

# ML / signal filters — deliberately conservative initially.
HORIZON_BARS = 2
TARGET_MOVE_PCT = 0.0005
MIN_PROBABILITY = 0.75
MIN_PROBABILITY_MARGIN = 0.08
MIN_MODEL_AGREEMENT = True

# Option selection filters.
MIN_DTE = 0
MAX_DTE = 7
MIN_ABS_DELTA = 0.40
MAX_ABS_DELTA = 0.60
MAX_OPTION_PREMIUM = 15.00
MAX_SPREAD_PERCENT = 0.10
MIN_OPTION_PREMIUM = 0.50
MAX_QUOTE_AGE_SECONDS = 60

# Risk limits. Dollar amounts are per account, not guarantees.
BASE_CONTRACTS = 1
MAX_CONTRACTS = 2
MAX_OPEN_POSITIONS = 2
MAX_TRADES_PER_DAY = 5
MAX_DAILY_LOSS_USD = 200.00
STOP_LOSS_PCT = 0.30
TAKE_PROFIT_PCT = 0.40
MAX_HOLD_MINUTES = 45

# Execution controls.
POLL_SECONDS = 30
ORDER_FILL_TIMEOUT_SECONDS = 15
SYMBOL_COOLDOWN_MINUTES = 20
MAX_ENTRY_NOTIONAL_USD = 1500.00

DB_PATH = "alpaca_ai_bot_v17.db"
LOG_PATH = "alpaca_ai_bot_v17.log"


# =========================
# 2. LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("alpaca_ai_bot")


def log(message: str, level: str = "info") -> None:
    """Unified console + file logger."""
    fn = getattr(logger, level.lower(), logger.info)
    fn(str(message))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ny_now() -> datetime:
    return datetime.now(TIMEZONE_NY)


def parse_datetime(value: Any) -> Optional[datetime]:
    """Convert API timestamps to timezone-aware UTC datetimes."""
    if value is None:
        return None

    try:
        result = pd.Timestamp(value).to_pydatetime()
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except Exception:
        return None


def data_age_seconds(timestamp: Any) -> Optional[float]:
    ts = parse_datetime(timestamp)
    if ts is None:
        return None
    return max(0.0, (utc_now() - ts).total_seconds())


# =========================
# 3. ALPACA CLIENTS
# =========================

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "Missing ALPACA_API_KEY or ALPACA_API_SECRET. "
        "Set them as environment variables; do not paste keys into code."
    )

if not PAPER_MODE:
    raise RuntimeError(
        "SAFETY STOP: This program is designed for PAPER MODE only."
    )

trading_client = TradingClient(
    api_key=API_KEY,
    secret_key=API_SECRET,
    paper=True,
)

stock_data_client = StockHistoricalDataClient(
    api_key=API_KEY,
    secret_key=API_SECRET,
)

option_data_client = OptionHistoricalDataClient(
    api_key=API_KEY,
    secret_key=API_SECRET,
)


# =========================
# 4. TELEGRAM
# =========================

def telegram_send(message: str) -> bool:
    """Telegram is optional; failure never bypasses trading safety."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM | not configured; message skipped", "warning")
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if response.status_code != 200:
            log(
                f"TELEGRAM ERROR | HTTP {response.status_code}: "
                f"{response.text[:250]}",
                "error",
            )
            return False
        return True
    except Exception as exc:
        log(f"TELEGRAM ERROR | {exc}", "error")
        return False


# =========================
# 5. SQLITE JOURNAL
# =========================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                category TEXT NOT NULL,
                symbol TEXT,
                reason TEXT,
                details_json TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                symbol TEXT NOT NULL,
                underlying TEXT,
                side TEXT,
                qty INTEGER,
                submitted_at TEXT NOT NULL,
                status TEXT,
                filled_qty REAL DEFAULT 0,
                avg_fill_price REAL,
                note TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                option_symbol TEXT NOT NULL,
                underlying TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                realized_pnl REAL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS symbol_cooldowns (
                symbol TEXT PRIMARY KEY,
                last_entry_utc TEXT NOT NULL
            )
        """)


def record_event(
    category: str,
    symbol: Optional[str] = None,
    reason: str = "",
    details: Optional[dict] = None,
) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO events
                (ts_utc, category, symbol, reason, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now().isoformat(),
                    category,
                    symbol,
                    reason,
                    json.dumps(details or {}, default=str),
                ),
            )
    except Exception as exc:
        log(f"DB EVENT ERROR | {exc}", "error")


def record_order(
    order_id: str,
    client_order_id: str,
    symbol: str,
    underlying: str,
    side: str,
    qty: int,
    status: str,
    filled_qty: float = 0,
    avg_fill_price: Optional[float] = None,
    note: str = "",
) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_orders (
                    order_id, client_order_id, symbol, underlying,
                    side, qty, submitted_at, status, filled_qty,
                    avg_fill_price, note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status=excluded.status,
                    filled_qty=excluded.filled_qty,
                    avg_fill_price=excluded.avg_fill_price,
                    note=excluded.note
                """,
                (
                    order_id,
                    client_order_id,
                    symbol,
                    underlying,
                    side,
                    qty,
                    utc_now().isoformat(),
                    status,
                    filled_qty,
                    avg_fill_price,
                    note,
                ),
            )
    except Exception as exc:
        log(f"DB ORDER ERROR | {exc}", "error")


# =========================
# 6. STOCK MARKET DATA
# =========================

def fetch_stock_bars(
    symbol: str,
    history_days: int = HISTORY_DAYS,
    limit: int = BAR_LIMIT,
) -> pd.DataFrame:
    """
    Return real Alpaca stock bars.
    No SPY x 10 substitution is used.
    """
    start = utc_now() - timedelta(days=history_days)
    end = utc_now()

    try:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TIMEFRAME,
            start=start,
            end=end,
            limit=limit,
            feed=STOCK_FEED,
        )

        response = stock_data_client.get_stock_bars(request)
        frame = response.df

        if frame is None or frame.empty:
            record_event(
                "DATA_WAIT", symbol,
                "NO_BARS",
                {"feed": STOCK_FEED_NAME},
            )
            log(
                f"DATA WAIT | {symbol} | no bars | "
                f"feed={STOCK_FEED_NAME}",
                "warning",
            )
            return pd.DataFrame()

        # Alpaca may return a MultiIndex (symbol, timestamp).
        if isinstance(frame.index, pd.MultiIndex):
            try:
                frame = frame.xs(symbol, level=0)
            except KeyError:
                log(f"DATA WAIT | {symbol} | symbol missing from response",
                    "warning")
                return pd.DataFrame()

        frame = frame.copy()
        frame.index = pd.to_datetime(frame.index, utc=True)
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = frame.sort_index()

        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(frame.columns)
        if missing:
            log(
                f"DATA WAIT | {symbol} | missing columns={sorted(missing)}",
                "warning",
            )
            return pd.DataFrame()

        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame = frame[
            (frame["close"] > 0)
            & (frame["high"] >= frame["low"])
        ]

        if frame.empty:
            log(f"DATA WAIT | {symbol} | no valid bars", "warning")
            return pd.DataFrame()

        last_ts = frame.index[-1].to_pydatetime()
        age = data_age_seconds(last_ts)

        log(
            f"DATA | {symbol} | bars={len(frame)} | "
            f"last={last_ts.isoformat()} | "
            f"age_sec={age:.0f} | feed={STOCK_FEED_NAME}"
        )

        if age is None or age > MAX_DATA_AGE_MINUTES * 60:
            record_event(
                "DATA_WAIT",
                symbol,
                "STALE_DATA",
                {
                    "bars": len(frame),
                    "last_timestamp": last_ts.isoformat(),
                    "age_seconds": age,
                    "feed": STOCK_FEED_NAME,
                },
            )
            log(
                f"DATA WAIT | {symbol} | STALE DATA "
                f"| age={age} seconds",
                "warning",
            )
            return pd.DataFrame()

        return frame

    except Exception as exc:
        record_event(
            "DATA_ERROR",
            symbol,
            str(exc),
            {"feed": STOCK_FEED_NAME},
        )
        log(
            f"DATA ERROR | {symbol} | {type(exc).__name__}: {exc}",
            "error",
        )
        return pd.DataFrame()


# =========================
# 7. BASIC MARKET STATUS
# =========================

def market_is_open() -> bool:
    """Use Alpaca's clock rather than assuming local time."""
    try:
        clock = trading_client.get_clock()
        is_open = bool(clock.is_open)
        log(
            f"MARKET | open={is_open} | "
            f"next_open={clock.next_open} | next_close={clock.next_close}"
        )
        return is_open
    except Exception as exc:
        record_event("MARKET_STATUS_ERROR", reason=str(exc))
        log(
            f"MARKET STATUS UNKNOWN | {exc} | trading disabled",
            "error",
        )
        return False


# =========================
# 8. INITIALIZATION
# =========================

def initialize_part1() -> None:
    init_database()

    log("=" * 65)
    log(BOT_NAME)
    log("SAFETY | PAPER MODE ENABLED")
    log(f"STOCK FEED | {STOCK_FEED_NAME}")
    log(f"OPTIONS FEED | {OPTIONS_FEED_NAME}")
    log(f"SYMBOLS | {', '.join(TRADE_SYMBOLS)}")
    log("PART 1 INITIALIZED | no orders are sent by this part")
    log("=" * 65)

    try:
        account = trading_client.get_account()
        log(
            f"ACCOUNT CHECK | status={account.status} | "
            f"equity={account.equity} | "
            f"buying_power={account.buying_power}"
        )
    except Exception as exc:
        raise RuntimeError(
            f"Alpaca account connection failed: {exc}"
        ) from exc


if __name__ == "__main__":
    initialize_part1()
    log(
        "PART 1 TEST COMPLETE. "
        "Do not expect trading yet; remaining parts are required."
    )

# ============================================================
# PART 2/4 — FEATURES, ML ENSEMBLE, MARKET CONFIRMATION
# NO TELEGRAM — NO ORDER EXECUTION IN THIS PART
# ============================================================

from sklearn.ensemble import (
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, roc_auc_score


# =========================
# 1. MODEL CONFIGURATION
# =========================

FEATURE_COLUMNS = [
    "return_1",
    "return_3",
    "return_5",
    "ema9_dist",
    "ema20_dist",
    "ema50_dist",
    "ema9_20",
    "rsi14",
    "atr_pct",
    "volume_z",
    "range_pct",
    "body_pct",
    "close_location",
    "breakout_10",
    "breakdown_10",
    "trend_strength",
    "vwap_dist",
    "momentum_accel",
]

MIN_VALID_FEATURE_ROWS = 150
MIN_CLASS_SAMPLES = 10
VALIDATION_FRACTION = 0.20

# The output is a probability estimate, not a guaranteed win rate.
MIN_MODEL_PROBABILITY = MIN_PROBABILITY
MIN_MODEL_MARGIN = MIN_PROBABILITY_MARGIN


# =========================
# 2. FEATURE ENGINEERING
# =========================

def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # Explicitly handle uninterrupted upward/downward sequences.
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)

    return rsi


def calculate_atr(
    frame: pd.DataFrame,
    period: int = 14,
) -> pd.Series:
    previous_close = frame["close"].shift(1)

    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()


def add_features(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Build causal features from completed historical bars.
    No future values are used as input features.
    """
    if frame is None or frame.empty:
        return pd.DataFrame()

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()

    df = frame.copy().sort_index()

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float)

    df["return_1"] = close.pct_change(1)
    df["return_3"] = close.pct_change(3)
    df["return_5"] = close.pct_change(5)

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    df["ema9_dist"] = close / ema9 - 1
    df["ema20_dist"] = close / ema20 - 1
    df["ema50_dist"] = close / ema50 - 1
    df["ema9_20"] = ema9 / ema20 - 1

    df["rsi14"] = calculate_rsi(close, 14) / 100.0

    df["atr"] = calculate_atr(df, 14)
    df["atr_pct"] = df["atr"] / close.replace(0, np.nan)

    volume_mean = volume.rolling(30, min_periods=10).mean()
    volume_std = volume.rolling(30, min_periods=10).std()

    df["volume_z"] = (
        (volume - volume_mean)
        / volume_std.replace(0, np.nan)
    )

    bar_range = (high - low).replace(0, np.nan)
    df["range_pct"] = (high - low) / close
    df["body_pct"] = (close - open_) / close

    df["close_location"] = (
        (close - low) / bar_range
    ).clip(0, 1)

    previous_high = high.shift(1).rolling(10).max()
    previous_low = low.shift(1).rolling(10).min()

    df["breakout_10"] = (
        close / previous_high.replace(0, np.nan) - 1
    )

    df["breakdown_10"] = (
        close / previous_low.replace(0, np.nan) - 1
    )

    df["trend_strength"] = (
        (ema9 - ema20) / close.replace(0, np.nan)
    )

    # Intraday VWAP approximation using available bars.
    # Reset at each New York calendar session.
    index_ny = df.index.tz_convert(TIMEZONE_NY)
    session_dates = pd.Series(
        index_ny.date,
        index=df.index,
    )

    typical_price = (high + low + close) / 3
    pv = typical_price * volume

    session_pv = pv.groupby(session_dates).cumsum()
    session_volume = volume.groupby(session_dates).cumsum()

    vwap = session_pv / session_volume.replace(0, np.nan)
    df["vwap_dist"] = close / vwap - 1

    df["momentum_accel"] = df["return_1"].diff()

    df.replace([np.inf, -np.inf], np.nan, inplace=True)

    return df


# =========================
# 3. LABEL GENERATION
# =========================

def make_training_frame(
    features: pd.DataFrame,
    horizon: int = HORIZON_BARS,
    target_move_pct: float = TARGET_MOVE_PCT,
) -> pd.DataFrame:
    """
    Classes:
       1 = future return above positive threshold
      -1 = future return below negative threshold
       0 = neutral

    Rows without a complete future horizon are removed rather
    than incorrectly labeled neutral.
    """
    if features is None or features.empty:
        return pd.DataFrame()

    df = features.copy()

    future_close = df["close"].shift(-horizon)
    future_return = future_close / df["close"] - 1

    df["future_return"] = future_return
    df["target"] = np.select(
        [
            future_return > target_move_pct,
            future_return < -target_move_pct,
        ],
        [1, -1],
        default=0,
    )

    # Crucial: the last horizon rows have no known target.
    df = df.loc[future_return.notna()].copy()

    df = df.replace([np.inf, -np.inf], np.nan)

    return df


# =========================
# 4. MODEL TRAINING
# =========================

def train_model_ensemble(
    features: pd.DataFrame,
) -> Tuple[Optional[dict], str]:
    """
    Chronological holdout validation:
    train on older rows, validate on newer rows.
    The validation portion is never used to fit the models.
    """
    dataset = make_training_frame(features)

    if dataset.empty:
        return None, "NO LABELED DATA"

    dataset = dataset.dropna(subset=FEATURE_COLUMNS)

    if len(dataset) < MIN_VALID_FEATURE_ROWS:
        return None, (
            f"INSUFFICIENT TRAINING ROWS "
            f"{len(dataset)}/{MIN_VALID_FEATURE_ROWS}"
        )

    X = dataset[FEATURE_COLUMNS].astype(float)
    y = dataset["target"].astype(int)

    split_at = int(len(dataset) * (1 - VALIDATION_FRACTION))

    if split_at < MIN_CLASS_SAMPLES or len(dataset) - split_at < 20:
        return None, "INSUFFICIENT CHRONOLOGICAL VALIDATION DATA"

    X_train, X_val = X.iloc[:split_at], X.iloc[split_at:]
    y_train, y_val = y.iloc[:split_at], y.iloc[split_at:]

    class_counts = y_train.value_counts()

    if len(class_counts) < 2:
        return None, "TRAINING DATA HAS ONLY ONE CLASS"

    if int(class_counts.min()) < MIN_CLASS_SAMPLES:
        return None, "TOO FEW EXAMPLES IN ONE OR MORE CLASSES"

    model_a = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("model", HistGradientBoostingClassifier(
            max_iter=150,
            learning_rate=0.06,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=42,
        )),
    ])

    model_b = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("model", RandomForestClassifier(
            n_estimators=250,
            max_depth=7,
            min_samples_leaf=8,
            class_weight="balanced_subsample",
            random_state=42,
            n_jobs=-1,
        )),
    ])

    try:
        model_a.fit(X_train, y_train)
        model_b.fit(X_train, y_train)

        pred_a = model_a.predict(X_val)
        pred_b = model_b.predict(X_val)

        accuracy_a = accuracy_score(y_val, pred_a)
        accuracy_b = accuracy_score(y_val, pred_b)

        # AUC is computed as a one-vs-rest macro score only when
        # the validation data supports it. Missing classes make
        # this diagnostic unavailable, not automatically zero.
        auc_a = None
        auc_b = None

        try:
            if len(np.unique(y_val)) >= 2:
                probs_a = model_a.predict_proba(X_val)
                probs_b = model_b.predict_proba(X_val)

                auc_a = roc_auc_score(
                    y_val,
                    probs_a,
                    labels=model_a.classes_,
                    multi_class="ovr",
                    average="macro",
                )
                auc_b = roc_auc_score(
                    y_val,
                    probs_b,
                    labels=model_b.classes_,
                    multi_class="ovr",
                    average="macro",
                )
        except Exception as exc:
            log(f"ML AUC UNAVAILABLE | {exc}", "warning")

        latest = X.iloc[[-1]]

        probabilities_a = model_a.predict_proba(latest)[0]
        probabilities_b = model_b.predict_proba(latest)[0]

        classes_a = list(model_a.classes_)
        classes_b = list(model_b.classes_)

        probs_a = {
            int(cls): float(prob)
            for cls, prob in zip(classes_a, probabilities_a)
        }
        probs_b = {
            int(cls): float(prob)
            for cls, prob in zip(classes_b, probabilities_b)
        }

        # Use the common class labels to combine the two models.
        common_classes = sorted(set(probs_a) & set(probs_b))

        if len(common_classes) < 2:
            return None, "MODEL CLASS LABELS DO NOT MATCH"

        ensemble = {
            cls: (probs_a[cls] + probs_b[cls]) / 2
            for cls in common_classes
        }

        ensemble_total = sum(ensemble.values())
        if ensemble_total <= 0:
            return None, "INVALID ENSEMBLE PROBABILITIES"

        ensemble = {
            cls: prob / ensemble_total
            for cls, prob in ensemble.items()
        }

        ranked = sorted(
            ensemble.items(),
            key=lambda item: item[1],
            reverse=True,
        )

        predicted_class, confidence = ranked[0]
        second_probability = ranked[1][1]
        probability_margin = confidence - second_probability

        model_a_class = max(probs_a, key=probs_a.get)
        model_b_class = max(probs_b, key=probs_b.get)

        model_agreement = model_a_class == model_b_class

        result = {
            "predicted_class": int(predicted_class),
            "confidence": float(confidence),
            "probability_margin": float(probability_margin),
            "model_a_class": int(model_a_class),
            "model_b_class": int(model_b_class),
            "model_agreement": bool(model_agreement),
            "probabilities": ensemble,
            "validation_accuracy_a": float(accuracy_a),
            "validation_accuracy_b": float(accuracy_b),
            "validation_auc_a": (
                float(auc_a) if auc_a is not None else None
            ),
            "validation_auc_b": (
                float(auc_b) if auc_b is not None else None
            ),
            "training_rows": int(len(X_train)),
            "validation_rows": int(len(X_val)),
        }

        log(
            f"ML | rows={len(dataset)} "
            f"| val_acc_A={accuracy_a:.3f} "
            f"| val_acc_B={accuracy_b:.3f} "
            f"| val_auc_A={auc_a} "
            f"| val_auc_B={auc_b} "
            f"| prediction={predicted_class} "
            f"| confidence={confidence:.3f} "
            f"| margin={probability_margin:.3f} "
            f"| agreement={model_agreement}"
        )

        return {
            "model_a": model_a,
            "model_b": model_b,
            "result": result,
            "feature_columns": FEATURE_COLUMNS,
        }, "OK"

    except Exception as exc:
        log(
            f"ML TRAINING ERROR | {type(exc).__name__}: {exc}",
            "error",
        )
        log(traceback.format_exc(), "error")
        return None, f"MODEL ERROR: {exc}"


# =========================
# 5. SIGNAL DECISION
# =========================

def classify_model_signal(
    model_result: dict,
) -> Tuple[Optional[str], str]:
    """
    Convert model output into a CALL or PUT candidate.
    This is only a candidate signal, not an order.
    """
    predicted_class = model_result["predicted_class"]
    confidence = model_result["confidence"]
    margin = model_result["probability_margin"]
    agreement = model_result["model_agreement"]

    if not agreement:
        return None, "MODEL DISAGREEMENT"

    if confidence < MIN_MODEL_PROBABILITY:
        return None, (
            f"CONFIDENCE LOW "
            f"{confidence:.3f} < {MIN_MODEL_PROBABILITY:.3f}"
        )

    if margin < MIN_MODEL_MARGIN:
        return None, (
            f"PROBABILITY MARGIN LOW "
            f"{margin:.3f} < {MIN_MODEL_MARGIN:.3f}"
        )

    if predicted_class == 1:
        return "CALL", "ML BULLISH"

    if predicted_class == -1:
        return "PUT", "ML BEARISH"

    return None, "ML NEUTRAL"


# =========================
# 6. MARKET CONFIRMATION
# =========================

def market_direction(frame: pd.DataFrame) -> Optional[str]:
    """
    A simple directional confirmation based on EMA9/EMA20 and
    the latest return. This is a filter, not an entry strategy.
    """
    if frame is None or frame.empty or len(frame) < 25:
        return None

    close = frame["close"].astype(float)

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()

    last_close = float(close.iloc[-1])
    last_return = float(close.pct_change().iloc[-1])

    if not math.isfinite(last_close) or not math.isfinite(last_return):
        return None

    if ema9.iloc[-1] > ema20.iloc[-1] and last_return > 0:
        return "BULLISH"

    if ema9.iloc[-1] < ema20.iloc[-1] and last_return < 0:
        return "BEARISH"

    return "NEUTRAL"


def get_market_confirmation() -> Tuple[Optional[str], str]:
    """
    SPY and QQQ must both have usable, fresh data.
    Conflicting directions result in WAIT.
    """
    directions = {}

    for symbol in MARKET_CONFIRMATION_SYMBOLS:
        bars = fetch_stock_bars(symbol)

        if bars.empty:
            return None, f"MARKET DATA UNAVAILABLE: {symbol}"

        direction = market_direction(bars)

        if direction is None:
            return None, f"MARKET DIRECTION UNAVAILABLE: {symbol}"

        directions[symbol] = direction

    spy_direction = directions.get("SPY")
    qqq_direction = directions.get("QQQ")

    log(
        f"MARKET CONFIRMATION | "
        f"SPY={spy_direction} | QQQ={qqq_direction}"
    )

    if spy_direction == qqq_direction:
        if spy_direction in ("BULLISH", "BEARISH"):
            return spy_direction, "SPY/QQQ AGREE"

    return None, "SPY/QQQ CONFLICT OR NEUTRAL"


def validate_signal_with_market(
    signal: Optional[str],
    market_bias: Optional[str],
) -> Tuple[bool, str]:
    if signal not in ("CALL", "PUT"):
        return False, "NO DIRECTIONAL ML SIGNAL"

    expected_bias = "BULLISH" if signal == "CALL" else "BEARISH"

    if market_bias is None:
        return False, "MARKET CONFIRMATION UNAVAILABLE"

    if market_bias != expected_bias:
        return False, (
            f"MARKET CONFLICT | signal={signal} "
            f"| market={market_bias}"
        )

    return True, "ML + MARKET CONFIRMATION PASS"


# =========================
# 7. PART 2 SELF-CHECK
# =========================

def part2_self_check() -> None:
    log("PART 2 LOADED | feature engineering and ML available")
    log(
        f"ML FILTERS | confidence>={MIN_MODEL_PROBABILITY:.2f} "
        f"| margin>={MIN_MODEL_MARGIN:.2f} "
        f"| horizon={HORIZON_BARS} bars"
    )
    log(
        "MARKET FILTERS | SPY + QQQ must agree directionally"
    )
    log(
        "IMPORTANT | validation metrics are diagnostics, "
        "not proof of profitability"
    )


part2_self_check()

# ============================================================
# PART 3/4 — OPTION CHAIN, QUOTE VALIDATION, DELTA, RISK
# PAPER MODE ONLY — NO ORDER SUBMISSION IN THIS PART
# ============================================================

from datetime import time as clock_time
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
)


# =========================
# 1. ACCOUNT / RISK HELPERS
# =========================

def get_account_equity() -> Optional[float]:
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        if not math.isfinite(equity) or equity <= 0:
            return None
        return equity
    except Exception as exc:
        log(f"RISK | account equity unavailable: {exc}", "error")
        return None


def get_open_option_positions() -> List[Any]:
    try:
        positions = trading_client.get_all_positions()
        result = []

        for position in positions:
            asset_class = str(
                getattr(position, "asset_class", "")
            ).lower()

            symbol = str(getattr(position, "symbol", ""))

            # Options symbols commonly begin with O: in market data,
            # but Alpaca position symbols can use OSI formatting.
            is_option = (
                "option" in asset_class
                or len(symbol) >= 15 and symbol[:1].isalpha()
                and any(ch.isdigit() for ch in symbol[6:])
            )

            if is_option and float(
                getattr(position, "qty", 0) or 0
            ) != 0:
                result.append(position)

        return result

    except Exception as exc:
        log(f"RISK | positions unavailable: {exc}", "error")
        return []


def get_open_position_count() -> Optional[int]:
    """
    Return None if the API request fails.
    Never interpret a failed request as zero positions.
    """
    try:
        positions = trading_client.get_all_positions()
        count = 0

        for position in positions:
            symbol = str(getattr(position, "symbol", ""))
            asset_class = str(
                getattr(position, "asset_class", "")
            ).lower()

            if "option" in asset_class or (
                len(symbol) >= 15 and any(ch.isdigit() for ch in symbol[6:])
            ):
                try:
                    if float(position.qty) != 0:
                        count += 1
                except Exception:
                    continue

        return count

    except Exception as exc:
        log(f"RISK | cannot count positions: {exc}", "error")
        return None


def get_today_order_count() -> Optional[int]:
    """
    Count today's orders submitted by this account.
    This intentionally counts orders, not just filled trades,
    so canceled/rejected orders may consume the daily limit.
    """
    try:
        orders = trading_client.get_orders()
        today_ny = ny_now().date()
        count = 0

        for order in orders:
            submitted = parse_datetime(
                getattr(order, "submitted_at", None)
            )
            if submitted and submitted.astimezone(
                TIMEZONE_NY
            ).date() == today_ny:
                count += 1

        return count

    except Exception as exc:
        log(f"RISK | cannot count today's orders: {exc}", "error")
        return None


def get_daily_pnl() -> Optional[float]:
    """
    Conservative session estimate based on equity snapshots
    saved by this bot. If today's baseline is missing, return None.
    This is not a complete realized-P&L ledger.
    """
    today = ny_now().date().isoformat()

    try:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_equity (
                    session_date TEXT PRIMARY KEY,
                    starting_equity REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            row = conn.execute(
                """
                SELECT starting_equity
                FROM daily_equity
                WHERE session_date = ?
                """,
                (today,),
            ).fetchone()

        equity = get_account_equity()
        if equity is None:
            return None

        if row is None:
            # Establishing a baseline at first observation cannot
            # reconstruct losses that occurred earlier today.
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO daily_equity
                    (session_date, starting_equity, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (today, equity, utc_now().isoformat()),
                )
            log(
                "RISK | daily equity baseline created; "
                "daily P&L available after next observation",
                "warning",
            )
            return None

        return equity - float(row["starting_equity"])

    except Exception as exc:
        log(f"RISK | daily P&L unavailable: {exc}", "error")
        return None


def risk_gate() -> Tuple[bool, str]:
    if not PAPER_MODE:
        return False, "PAPER MODE SAFETY CHECK FAILED"

    if not market_is_open():
        return False, "MARKET CLOSED OR CLOCK UNKNOWN"

    open_count = get_open_position_count()
    if open_count is None:
        return False, "OPEN POSITIONS UNKNOWN"

    if open_count >= MAX_OPEN_POSITIONS:
        return False, (
            f"MAX OPEN POSITIONS {open_count}/{MAX_OPEN_POSITIONS}"
        )

    today_orders = get_today_order_count()
    if today_orders is None:
        return False, "TODAY'S ORDER COUNT UNKNOWN"

    if today_orders >= MAX_TRADES_PER_DAY:
        return False, (
            f"DAILY ORDER LIMIT {today_orders}/{MAX_TRADES_PER_DAY}"
        )

    daily_pnl = get_daily_pnl()
    if daily_pnl is None:
        return False, "DAILY P&L BASELINE UNKNOWN"

    if daily_pnl <= -abs(MAX_DAILY_LOSS_USD):
        return False, (
            f"DAILY LOSS LIMIT | pnl={daily_pnl:.2f}"
        )

    return True, "RISK GATE PASS"


# =========================
# 2. UNDERLYING LAST PRICE
# =========================

def get_underlying_last_price(
    symbol: str,
) -> Tuple[Optional[float], str]:
    bars = fetch_stock_bars(symbol)

    if bars.empty:
        return None, "UNDERLYING DATA UNAVAILABLE"

    price = float(bars["close"].iloc[-1])

    if not math.isfinite(price) or price <= 0:
        return None, "INVALID UNDERLYING PRICE"

    return price, "OK"


# =========================
# 3. OPTION CONTRACT SEARCH
# =========================

def get_option_contract_candidates(
    underlying: str,
    side: str,
    underlying_price: float,
) -> Tuple[List[Any], str]:
    """
    Search contracts expiring today through MAX_DTE days ahead.
    The exact contract list and availability depend on Alpaca
    account permissions and the supported API schema.
    """
    if side not in ("CALL", "PUT"):
        return [], "INVALID OPTION SIDE"

    if underlying_price <= 0:
        return [], "INVALID UNDERLYING PRICE"

    today = ny_now().date()
    end_date = today + timedelta(days=MAX_DTE)

    contract_type = (
        ContractType.CALL if side == "CALL" else ContractType.PUT
    )

    try:
        request = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=today,
            expiration_date_lte=end_date,
            type=contract_type,
        )

        response = trading_client.get_option_contracts(request)
        contracts = list(getattr(response, "option_contracts", []) or [])

        if not contracts:
            return [], "NO CONTRACTS RETURNED"

        filtered = []

        for contract in contracts:
            symbol = getattr(contract, "symbol", None)
            strike = getattr(contract, "strike_price", None)
            expiry = getattr(contract, "expiration_date", None)
            ctype = str(getattr(contract, "type", "")).lower()
            tradable = getattr(contract, "tradable", True)

            if not symbol or strike is None or expiry is None:
                continue

            if not tradable:
                continue

            try:
                strike = float(strike)
                expiry_date = pd.Timestamp(expiry).date()
            except Exception:
                continue

            dte = (expiry_date - today).days

            if dte < MIN_DTE or dte > MAX_DTE:
                continue

            if expiry_date < today:
                continue

            # Avoid contracts far away from the underlying.
            if abs(strike - underlying_price) / underlying_price > 0.08:
                continue

            if "call" in ctype and side != "CALL":
                continue
            if "put" in ctype and side != "PUT":
                continue

            filtered.append(contract)

        filtered.sort(
            key=lambda c: abs(
                float(c.strike_price) - underlying_price
            )
        )

        if not filtered:
            return [], "NO CONTRACTS PASSED EXPIRY/STRIKE FILTER"

        return filtered, "OK"

    except Exception as exc:
        log(
            f"OPTION CHAIN ERROR | {underlying} | "
            f"{type(exc).__name__}: {exc}",
            "error",
        )
        return [], f"OPTION CHAIN ERROR: {exc}"


# =========================
# 4. OPTION SNAPSHOT / QUOTE
# =========================

def get_option_snapshot(
    option_symbol: str,
) -> Tuple[Optional[Any], str]:
    try:
        request = OptionSnapshotRequest(
            symbol_or_symbols=option_symbol,
            feed=OPTIONS_FEED,
        )

        response = option_data_client.get_option_snapshot(request)

        if response is None:
            return None, "EMPTY OPTION SNAPSHOT"

        if isinstance(response, dict):
            snapshot = response.get(option_symbol)
        else:
            snapshot = response

        if snapshot is None:
            return None, "OPTION SNAPSHOT MISSING"

        return snapshot, "OK"

    except Exception as exc:
        log(
            f"OPTION SNAPSHOT ERROR | {option_symbol} | {exc}",
            "warning",
        )
        return None, f"OPTION SNAPSHOT ERROR: {exc}"


def extract_quote_fields(
    snapshot: Any,
) -> Tuple[Optional[float], Optional[float], Optional[datetime]]:
    """
    Read bid/ask and quote timestamp from the snapshot.
    Missing timestamp is treated as unknown and rejected.
    """
    quote = getattr(snapshot, "latest_quote", None)

    if quote is None and isinstance(snapshot, dict):
        quote = snapshot.get("latest_quote")

    if quote is None:
        return None, None, None

    bid = getattr(quote, "bid_price", None)
    ask = getattr(quote, "ask_price", None)
    timestamp = getattr(quote, "timestamp", None)

    if isinstance(quote, dict):
        bid = quote.get("bid_price", bid)
        ask = quote.get("ask_price", ask)
        timestamp = quote.get("timestamp", timestamp)

    try:
        bid = float(bid)
        ask = float(ask)
    except (TypeError, ValueError):
        return None, None, parse_datetime(timestamp)

    return bid, ask, parse_datetime(timestamp)


def extract_option_delta(snapshot: Any) -> Optional[float]:
    """
    Read delta only if supplied by the API.
    No synthetic delta proxy is used.
    """
    greeks = getattr(snapshot, "greeks", None)

    if greeks is None and isinstance(snapshot, dict):
        greeks = snapshot.get("greeks")

    if greeks is None:
        return None

    delta = getattr(greeks, "delta", None)
    if isinstance(greeks, dict):
        delta = greeks.get("delta", delta)

    try:
        delta = float(delta)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(delta) or abs(delta) > 1:
        return None

    return delta


# =========================
# 5. CONTRACT SELECTION
# =========================

def select_option_contract(
    underlying: str,
    side: str,
    underlying_price: float,
) -> Tuple[Optional[dict], str]:
    contracts, reason = get_option_contract_candidates(
        underlying,
        side,
        underlying_price,
    )

    if not contracts:
        return None, reason

    rejected = {
        "snapshot": 0,
        "delta": 0,
        "quote": 0,
        "spread": 0,
        "premium": 0,
        "quote_age": 0,
    }

    valid = []

    for contract in contracts[:100]:
        option_symbol = str(contract.symbol)

        snapshot, snapshot_reason = get_option_snapshot(option_symbol)
        if snapshot is None:
            rejected["snapshot"] += 1
            log(
                f"OPTION REJECT | {option_symbol} | {snapshot_reason}"
            )
            continue

        delta = extract_option_delta(snapshot)

        if delta is None:
            rejected["delta"] += 1
            log(
                f"OPTION REJECT | {option_symbol} | DELTA UNKNOWN"
            )
            continue

        if side == "CALL" and delta <= 0:
            rejected["delta"] += 1
            continue

        if side == "PUT" and delta >= 0:
            rejected["delta"] += 1
            continue

        if not MIN_ABS_DELTA <= abs(delta) <= MAX_ABS_DELTA:
            rejected["delta"] += 1
            continue

        bid, ask, quote_ts = extract_quote_fields(snapshot)

        if (
            bid is None
            or ask is None
            or bid <= 0
            or ask <= 0
            or ask < bid
        ):
            rejected["quote"] += 1
            log(
                f"OPTION REJECT | {option_symbol} | INVALID BID/ASK"
            )
            continue

        if quote_ts is None:
            rejected["quote_age"] += 1
            log(
                f"OPTION REJECT | {option_symbol} | QUOTE TIME UNKNOWN"
            )
            continue

        quote_age = (utc_now() - quote_ts).total_seconds()

        if quote_age < -5 or quote_age > MAX_QUOTE_AGE_SECONDS:
            rejected["quote_age"] += 1
            log(
                f"OPTION REJECT | {option_symbol} | "
                f"QUOTE STALE age={quote_age:.1f}s"
            )
            continue

        mid = (bid + ask) / 2

        if mid <= 0:
            rejected["quote"] += 1
            continue

        spread_pct = (ask - bid) / mid

        if spread_pct > MAX_SPREAD_PERCENT:
            rejected["spread"] += 1
            continue

        if ask < MIN_OPTION_PREMIUM or ask > MAX_OPTION_PREMIUM:
            rejected["premium"] += 1
            continue

        valid.append({
            "underlying": underlying,
            "side": side,
            "symbol": option_symbol,
            "strike": float(contract.strike_price),
            "expiry": str(contract.expiration_date),
            "dte": (
                pd.Timestamp(contract.expiration_date).date()
                - ny_now().date()
            ).days,
            "delta": delta,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct,
            "quote_age_seconds": quote_age,
            "underlying_price": underlying_price,
        })

    log(
        f"OPTION DIAGNOSTICS | {underlying} {side} | "
        f"candidates={len(contracts)} | valid={len(valid)} | "
        f"rejected={rejected}"
    )

    if not valid:
        return None, "NO OPTION PASSED DELTA/QUOTE/SPREAD/PREMIUM FILTERS"

    # Prefer a tighter spread and a delta nearer the center of
    # the permitted band. This ranks candidates; it doesn't predict profit.
    valid.sort(
        key=lambda c: (
            c["spread_pct"],
            abs(abs(c["delta"]) - 0.50),
            abs(c["strike"] - underlying_price),
        )
    )

    return valid[0], "OK"


# =========================
# 6. POSITION SIZE
# =========================

def calculate_contract_quantity(
    ask_price: float,
) -> Tuple[int, str]:
    """
    Options contract notional estimate:
    ask * 100 * contracts.
    The premium paid can be lost in full.
    """
    if not math.isfinite(ask_price) or ask_price <= 0:
        return 0, "INVALID OPTION ASK"

    equity = get_account_equity()

    if equity is None:
        return 0, "ACCOUNT EQUITY UNKNOWN"

    # Keep the initial position small and bounded.
    max_risk_budget = min(
        MAX_ENTRY_NOTIONAL_USD,
        equity * 0.01,
    )

    per_contract_cost = ask_price * 100

    if per_contract_cost > max_risk_budget:
        return 0, (
            f"ONE CONTRACT EXCEEDS RISK BUDGET "
            f"| cost={per_contract_cost:.2f} "
            f"| budget={max_risk_budget:.2f}"
        )

    qty = int(max_risk_budget // per_contract_cost)
    qty = min(qty, BASE_CONTRACTS, MAX_CONTRACTS)

    if qty < 1:
        return 0, "POSITION SIZE BELOW ONE CONTRACT"

    return qty, "OK"


# =========================
# 7. PRE-ENTRY VALIDATION
# =========================

def validate_contract_before_entry(
    contract: dict,
) -> Tuple[bool, str]:
    required = [
        "underlying",
        "side",
        "symbol",
        "strike",
        "expiry",
        "delta",
        "bid",
        "ask",
        "quote_age_seconds",
    ]

    for key in required:
        if key not in contract or contract[key] is None:
            return False, f"MISSING CONTRACT FIELD: {key}"

    if contract["side"] not in ("CALL", "PUT"):
        return False, "INVALID SIDE"

    if contract["ask"] <= 0 or contract["bid"] <= 0:
        return False, "INVALID QUOTE"

    if contract["ask"] < contract["bid"]:
        return False, "CROSSED QUOTE"

    if contract["quote_age_seconds"] > MAX_QUOTE_AGE_SECONDS:
        return False, "STALE OPTION QUOTE"

    if contract["spread_pct"] > MAX_SPREAD_PERCENT:
        return False, "SPREAD TOO WIDE"

    if contract["ask"] > MAX_OPTION_PREMIUM:
        return False, "OPTION PREMIUM TOO HIGH"

    if not MIN_ABS_DELTA <= abs(contract["delta"]) <= MAX_ABS_DELTA:
        return False, "DELTA OUTSIDE RANGE"

    if contract["side"] == "CALL" and contract["delta"] <= 0:
        return False, "CALL DELTA SIGN INVALID"

    if contract["side"] == "PUT" and contract["delta"] >= 0:
        return False, "PUT DELTA SIGN INVALID"

    return True, "CONTRACT VALIDATION PASS"


# =========================
# 8. PART 3 SELF-CHECK
# =========================

def part3_self_check() -> None:
    log("PART 3 LOADED | option selection and risk controls available")
    log(
        f"OPTION FILTERS | delta={MIN_ABS_DELTA:.2f}-"
        f"{MAX_ABS_DELTA:.2f} | "
        f"max_spread={MAX_SPREAD_PERCENT:.1%} | "
        f"max_ask=${MAX_OPTION_PREMIUM:.2f}"
    )
    log(
        f"RISK LIMITS | max_positions={MAX_OPEN_POSITIONS} | "
        f"max_orders/day={MAX_TRADES_PER_DAY} | "
        f"max_loss=${MAX_DAILY_LOSS_USD:.2f}"
    )
    log("PART 3 | no orders are submitted by this part")


part3_self_check()


# ============================================================
# PART 4/4 — PAPER EXECUTION, EXIT MANAGEMENT, MAIN LOOP
# PAPER ONLY | NO TELEGRAM | NO LIVE ORDER EXECUTION
# ============================================================

import re

from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


# =========================
# 1. SYMBOL / COOLDOWN HELPERS
# =========================

def is_option_symbol(symbol: str) -> bool:
    """Recognize standard OCC option symbols."""
    return bool(
        re.fullmatch(r"[A-Z]{1,6}\d{6}[CP]\d{8}", symbol or "")
    )


def cooldown_remaining(symbol: str) -> float:
    try:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT last_entry_utc
                FROM symbol_cooldowns
                WHERE symbol = ?
                """,
                (symbol,),
            ).fetchone()

        if row is None:
            return 0.0

        last_entry = parse_datetime(row["last_entry_utc"])
        if last_entry is None:
            return 0.0

        elapsed = (utc_now() - last_entry).total_seconds()
        remaining = SYMBOL_COOLDOWN_MINUTES * 60 - elapsed

        return max(0.0, remaining)

    except Exception as exc:
        log(f"COOLDOWN ERROR | {symbol} | {exc}", "error")
        return float("inf")


def set_cooldown(symbol: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_cooldowns (symbol, last_entry_utc)
            VALUES (?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                last_entry_utc=excluded.last_entry_utc
            """,
            (symbol, utc_now().isoformat()),
        )


def has_open_sell_order(option_symbol: str) -> bool:
    try:
        orders = trading_client.get_orders()
        for order in orders:
            if str(getattr(order, "symbol", "")) != option_symbol:
                continue

            side = str(getattr(order, "side", "")).lower()
            status = str(getattr(order, "status", "")).lower()

            if side.endswith("sell") and status in (
                "new",
                "accepted",
                "pending_new",
                "partially_filled",
                "held",
                "accepted_for_bidding",
            ):
                return True

        return False

    except Exception as exc:
        log(
            f"ORDER CHECK ERROR | {option_symbol} | {exc}",
            "error",
        )
        # Fail closed: do not submit another sell if order status
        # cannot be established.
        return True


# =========================
# 2. ORDER STATUS HELPERS
# =========================

def order_status_text(order: Any) -> str:
    return str(getattr(order, "status", "unknown")).lower()


def wait_for_order(
    order_id: str,
    timeout_seconds: int = ORDER_FILL_TIMEOUT_SECONDS,
) -> Optional[Any]:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            order = trading_client.get_order_by_id(order_id)
            status = order_status_text(order)

            if status in (
                "filled",
                "canceled",
                "cancelled",
                "rejected",
                "expired",
            ):
                return order

        except Exception as exc:
            log(f"ORDER STATUS ERROR | {order_id} | {exc}", "error")

        time.sleep(1)

    try:
        return trading_client.get_order_by_id(order_id)
    except Exception as exc:
        log(f"ORDER FINAL STATUS UNKNOWN | {order_id} | {exc}", "error")
        return None


def cancel_remainder_and_refresh(order_id: str) -> Optional[Any]:
    """
    Cancel any unfilled remainder, then re-read the order.
    Never assume a cancellation succeeded without checking.
    """
    try:
        trading_client.cancel_order_by_id(order_id)
    except Exception as exc:
        log(
            f"CANCEL REQUEST | {order_id} | {exc}",
            "warning",
        )

    time.sleep(1)

    try:
        return trading_client.get_order_by_id(order_id)
    except Exception as exc:
        log(
            f"POST-CANCEL STATUS UNKNOWN | {order_id} | {exc}",
            "error",
        )
        return None


# =========================
# 3. TRADE JOURNAL
# =========================

def journal_entry(
    option_symbol: str,
    underlying: str,
    side: str,
    qty: int,
    entry_price: float,
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_trades (
                option_symbol, underlying, side, qty,
                entry_price, entry_time
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                option_symbol,
                underlying,
                side,
                qty,
                entry_price,
                utc_now().isoformat(),
            ),
        )


def journal_exit(
    option_symbol: str,
    exit_price: float,
    exit_reason: str,
    qty_closed: int,
) -> None:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT id, qty, entry_price
            FROM bot_trades
            WHERE option_symbol = ?
              AND exit_time IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (option_symbol,),
        ).fetchone()

        if row is None:
            log(
                f"JOURNAL WARNING | no open journal row for "
                f"{option_symbol}",
                "warning",
            )
            return

        original_qty = int(row["qty"])
        entry_price = float(row["entry_price"])
        qty_closed = min(max(0, qty_closed), original_qty)

        realized_pnl = (
            (exit_price - entry_price) * 100 * qty_closed
        )

        if qty_closed >= original_qty:
            conn.execute(
                """
                UPDATE bot_trades
                SET exit_price = ?, exit_time = ?,
                    exit_reason = ?, realized_pnl = ?
                WHERE id = ?
                """,
                (
                    exit_price,
                    utc_now().isoformat(),
                    exit_reason,
                    realized_pnl,
                    row["id"],
                ),
            )
        else:
            # Partial close: account for the closed portion, then
            # leave the residual quantity open in the journal.
            conn.execute(
                """
                UPDATE bot_trades
                SET qty = ?, realized_pnl = COALESCE(realized_pnl, 0) + ?
                WHERE id = ?
                """,
                (
                    original_qty - qty_closed,
                    realized_pnl,
                    row["id"],
                ),
            )


# =========================
# 4. PAPER BUY EXECUTION
# =========================

def submit_paper_buy(
    contract: dict,
    underlying: str,
    side: str,
    qty: int,
) -> Tuple[bool, str]:
    """
    Limit-buy in Paper mode only.
    A partial fill is journaled only for the confirmed filled quantity.
    """
    if PAPER_MODE is not True:
        return False, "SAFETY STOP: PAPER MODE DISABLED"

    valid, reason = validate_contract_before_entry(contract)
    if not valid:
        return False, reason

    if qty < 1 or qty > MAX_CONTRACTS:
        return False, "INVALID CONTRACT QUANTITY"

    estimated_cost = contract["ask"] * 100 * qty

    if estimated_cost > MAX_ENTRY_NOTIONAL_USD:
        return False, (
            f"ENTRY COST EXCEEDS LIMIT "
            f"| ${estimated_cost:.2f}"
        )

    # Final pre-submit quote recheck for the exact same contract.
    snapshot, snapshot_reason = get_option_snapshot(contract["symbol"])
    if snapshot is None:
        return False, f"FINAL QUOTE CHECK FAILED: {snapshot_reason}"

    bid, ask, quote_ts = extract_quote_fields(snapshot)
    delta = extract_option_delta(snapshot)

    if (
        bid is None or ask is None or quote_ts is None
        or delta is None
    ):
        return False, "FINAL QUOTE/DELTA UNKNOWN"

    age = (utc_now() - quote_ts).total_seconds()

    if age < -5 or age > MAX_QUOTE_AGE_SECONDS:
        return False, f"FINAL QUOTE STALE | {age:.1f}s"

    if bid <= 0 or ask < bid or ask <= 0:
        return False, "FINAL QUOTE INVALID"

    spread_pct = (ask - bid) / ((ask + bid) / 2)

    if spread_pct > MAX_SPREAD_PERCENT:
        return False, "FINAL SPREAD TOO WIDE"

    if not MIN_ABS_DELTA <= abs(delta) <= MAX_ABS_DELTA:
        return False, "FINAL DELTA OUTSIDE RANGE"

    if side == "CALL" and delta <= 0:
        return False, "FINAL CALL DELTA INVALID"

    if side == "PUT" and delta >= 0:
        return False, "FINAL PUT DELTA INVALID"

    # Avoid chasing if the ask has moved materially since selection.
    if ask > MAX_OPTION_PREMIUM:
        return False, "FINAL ASK ABOVE PREMIUM LIMIT"

    if ask > contract["ask"] * 1.03:
        return False, "FINAL ASK MOVED MORE THAN 3%"

    # Recheck global limits immediately before submission.
    allowed, risk_reason = risk_gate()
    if not allowed:
        return False, f"RISK GATE: {risk_reason}"

    request = LimitOrderRequest(
        symbol=contract["symbol"],
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=round(ask, 2),
    )

    try:
        order = trading_client.submit_order(order_data=request)

        order_id = str(order.id)
        client_order_id = str(
            getattr(order, "client_order_id", "")
        )

        log(
            f"PAPER BUY SUBMITTED | {contract['symbol']} | "
            f"qty={qty} | limit=${ask:.2f} | id={order_id}"
        )

        final_order = wait_for_order(order_id)

        if final_order is None:
            log(
                f"CRITICAL | order state unknown: {order_id}. "
                "Do not submit a duplicate order.",
                "error",
            )
            record_event(
                "ORDER_UNKNOWN",
                underlying,
                "BUY ORDER STATUS UNKNOWN",
                {"order_id": order_id, "symbol": contract["symbol"]},
            )
            return False, "ORDER STATUS UNKNOWN — MANUAL CHECK REQUIRED"

        status = order_status_text(final_order)

        if status not in ("filled", "canceled", "cancelled",
                          "rejected", "expired"):
            final_order = cancel_remainder_and_refresh(order_id)

            if final_order is None:
                return False, (
                    "ORDER STATUS UNKNOWN AFTER CANCEL — "
                    "MANUAL CHECK REQUIRED"
                )

            status = order_status_text(final_order)

        filled_qty = float(
            getattr(final_order, "filled_qty", 0) or 0
        )
        avg_fill_price = getattr(
            final_order, "filled_avg_price", None
        )

        try:
            avg_fill_price = (
                float(avg_fill_price)
                if avg_fill_price is not None
                else None
            )
        except (TypeError, ValueError):
            avg_fill_price = None

        record_order(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=contract["symbol"],
            underlying=underlying,
            side="BUY",
            qty=qty,
            status=status,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            note="PAPER BUY",
        )

        if filled_qty > 0 and avg_fill_price is not None:
            journal_entry(
                contract["symbol"],
                underlying,
                side,
                int(filled_qty),
                avg_fill_price,
            )
            set_cooldown(underlying)

            log(
                f"BUY FILL | {contract['symbol']} | "
                f"filled={filled_qty}/{qty} | "
                f"avg=${avg_fill_price:.2f} | status={status}"
            )

        if status == "filled" and filled_qty >= qty:
            return True, (
                f"FILLED {filled_qty} CONTRACT(S) "
                f"AT ${avg_fill_price:.2f}"
            )

        if filled_qty > 0:
            return True, (
                f"PARTIAL FILL {filled_qty}/{qty}; "
                "remainder status checked"
            )

        return False, f"NOT FILLED | status={status}"

    except Exception as exc:
        log(
            f"PAPER BUY ERROR | {contract['symbol']} | "
            f"{type(exc).__name__}: {exc}",
            "error",
        )
        record_event(
            "ORDER_ERROR",
            underlying,
            str(exc),
            {"symbol": contract["symbol"], "side": side},
        )
        return False, f"ORDER ERROR: {exc}"


# =========================
# 5. POSITION EXIT MANAGEMENT
# =========================

def get_option_positions_safe() -> Optional[List[Any]]:
    try:
        positions = trading_client.get_all_positions()
        return [
            p for p in positions
            if is_option_symbol(str(getattr(p, "symbol", "")))
        ]
    except Exception as exc:
        log(f"EXIT | positions unavailable: {exc}", "error")
        return None


def submit_paper_exit(
    position: Any,
    bid: float,
    reason: str,
) -> None:
    if PAPER_MODE is not True:
        log("EXIT SAFETY STOP | paper mode disabled", "error")
        return

    symbol = str(position.symbol)

    if has_open_sell_order(symbol):
        log(f"EXIT WAIT | {symbol} | existing sell order")
        return

    try:
        current_qty = float(position.qty)

        # This bot manages long option positions only.
        if current_qty <= 0:
            log(
                f"EXIT REVIEW REQUIRED | {symbol} | "
                f"non-long position qty={current_qty}",
                "warning",
            )
            return

        qty = int(current_qty)

        if qty < 1 or bid <= 0:
            log(f"EXIT WAIT | {symbol} | invalid qty/bid")
            return

        # Sell limit at current bid. This is not a guarantee of fill.
        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=round(bid, 2),
        )

        order = trading_client.submit_order(order_data=request)

        log(
            f"PAPER EXIT SUBMITTED | {symbol} | "
            f"qty={qty} | limit=${bid:.2f} | reason={reason} "
            f"| order_id={order.id}"
        )

        final_order = wait_for_order(str(order.id))

        if final_order is None:
            log(
                f"EXIT STATUS UNKNOWN | {symbol} | "
                "manual account check required",
                "error",
            )
            return

        status = order_status_text(final_order)

        if status not in ("filled", "canceled", "cancelled",
                          "rejected", "expired"):
            final_order = cancel_remainder_and_refresh(str(order.id))

            if final_order is None:
                log(
                    f"EXIT CANCEL STATUS UNKNOWN | {symbol}",
                    "error",
                )
                return

            status = order_status_text(final_order)

        filled_qty = int(float(
            getattr(final_order, "filled_qty", 0) or 0
        ))

        avg_exit = getattr(final_order, "filled_avg_price", None)

        try:
            avg_exit = float(avg_exit) if avg_exit is not None else None
        except (TypeError, ValueError):
            avg_exit = None

        record_order(
            order_id=str(order.id),
            client_order_id=str(
                getattr(order, "client_order_id", "")
            ),
            symbol=symbol,
            underlying="UNKNOWN",
            side="SELL",
            qty=qty,
            status=status,
            filled_qty=filled_qty,
            avg_fill_price=avg_exit,
            note=f"PAPER EXIT: {reason}",
        )

        if filled_qty > 0 and avg_exit is not None:
            journal_exit(
                symbol,
                avg_exit,
                reason,
                filled_qty,
            )

            log(
                f"EXIT FILL | {symbol} | "
                f"filled={filled_qty}/{qty} | "
                f"avg=${avg_exit:.2f} | reason={reason}"
            )
        else:
            log(
                f"EXIT NOT FILLED | {symbol} | status={status}",
                "warning",
            )

    except Exception as exc:
        log(
            f"EXIT ERROR | {symbol} | "
            f"{type(exc).__name__}: {exc}",
            "error",
        )


def manage_open_positions() -> None:
    positions = get_option_positions_safe()

    if positions is None:
        log("EXIT MANAGER WAIT | cannot confirm positions", "error")
        return

    for position in positions:
        symbol = str(position.symbol)

        try:
            qty = float(position.qty)
            entry_price = float(position.avg_entry_price)

            if qty <= 0 or entry_price <= 0:
                log(
                    f"EXIT REVIEW | {symbol} | "
                    "not a long position or entry price unavailable",
                    "warning",
                )
                continue

            snapshot, reason = get_option_snapshot(symbol)

            if snapshot is None:
                log(f"EXIT WAIT | {symbol} | {reason}")
                continue

            bid, ask, quote_ts = extract_quote_fields(snapshot)

            if bid is None or quote_ts is None or bid <= 0:
                log(f"EXIT WAIT | {symbol} | invalid bid/timestamp")
                continue

            quote_age = (utc_now() - quote_ts).total_seconds()

            if quote_age < -5 or quote_age > MAX_QUOTE_AGE_SECONDS:
                log(
                    f"EXIT WAIT | {symbol} | "
                    f"stale quote age={quote_age:.1f}s"
                )
                continue

            pnl_pct = (bid / entry_price) - 1.0

            # Time held uses the broker position's available timestamp
            # only if present; otherwise do not fabricate an entry time.
            opened_at = parse_datetime(
                getattr(position, "created_at", None)
            )

            if opened_at is None:
                with db_connect() as conn:
                    row = conn.execute(
                        """
                        SELECT entry_time
                        FROM bot_trades
                        WHERE option_symbol = ?
                          AND exit_time IS NULL
                        ORDER BY id DESC LIMIT 1
                        """,
                        (symbol,),
                    ).fetchone()

                if row:
                    opened_at = parse_datetime(row["entry_time"])

            hold_minutes = None
            if opened_at is not None:
                hold_minutes = (
                    utc_now() - opened_at
                ).total_seconds() / 60

            log(
                f"POSITION | {symbol} | qty={qty} | "
                f"entry=${entry_price:.2f} | bid=${bid:.2f} | "
                f"pnl={pnl_pct:.1%} | held_min={hold_minutes}"
            )

            if pnl_pct <= -abs(STOP_LOSS_PCT):
                submit_paper_exit(position, bid, "STOP LOSS")

            elif pnl_pct >= TAKE_PROFIT_PCT:
                submit_paper_exit(position, bid, "TAKE PROFIT")

            elif (
                hold_minutes is not None
                and hold_minutes >= MAX_HOLD_MINUTES
            ):
                submit_paper_exit(position, bid, "MAX HOLD TIME")

        except Exception as exc:
            log(
                f"POSITION MANAGER ERROR | {symbol} | {exc}",
                "error",
            )


# =========================
# 6. FULL SCAN CYCLE
# =========================

def scan_one_symbol(symbol: str) -> None:
    log(f"SCAN START | {symbol}")

    remaining = cooldown_remaining(symbol)

    if remaining == float("inf"):
        log(f"WAIT | {symbol} | cooldown state unknown")
        return

    if remaining > 0:
        log(
            f"WAIT | {symbol} | cooldown "
            f"{remaining / 60:.1f} minutes remaining"
        )
        return

    bars = fetch_stock_bars(symbol)

    if bars.empty:
        log(f"WAIT | {symbol} | stock data unavailable")
        return

    if len(bars) < MIN_TRAIN_ROWS:
        log(
            f"WAIT | {symbol} | insufficient bars "
            f"{len(bars)}/{MIN_TRAIN_ROWS}"
        )
        return

    features = add_features(bars)

    if features.empty:
        log(f"WAIT | {symbol} | feature calculation failed")
        return

    model_bundle, model_reason = train_model_ensemble(features)

    if model_bundle is None:
        log(f"WAIT | {symbol} | {model_reason}")
        return

    model_result = model_bundle["result"]

    signal, signal_reason = classify_model_signal(model_result)

    if signal is None:
        log(
            f"WAIT | {symbol} | {signal_reason} | "
            f"confidence={model_result['confidence']:.3f}"
        )
        return

    market_bias, market_reason = get_market_confirmation()

    confirmed, confirmation_reason = validate_signal_with_market(
        signal,
        market_bias,
    )

    if not confirmed:
        log(
            f"WAIT | {symbol} | {confirmation_reason} "
            f"| {market_reason}"
        )
        return

    allowed, risk_reason = risk_gate()

    if not allowed:
        log(f"WAIT | {symbol} | {risk_reason}")
        return

    underlying_price = float(bars["close"].iloc[-1])

    contract, contract_reason = select_option_contract(
        symbol,
        signal,
        underlying_price,
    )

    if contract is None:
        log(
            f"WAIT | {symbol} | no contract | {contract_reason}"
        )
        return

    contract_ok, validation_reason = validate_contract_before_entry(
        contract
    )

    if not contract_ok:
        log(
            f"WAIT | {symbol} | {validation_reason}"
        )
        return

    qty, qty_reason = calculate_contract_quantity(contract["ask"])

    if qty < 1:
        log(f"WAIT | {symbol} | {qty_reason}")
        return

    log(
        f"ENTRY CANDIDATE | {symbol} | {signal} | "
        f"confidence={model_result['confidence']:.3f} | "
        f"margin={model_result['probability_margin']:.3f} | "
        f"market={market_bias} | "
        f"option={contract['symbol']} | "
        f"strike={contract['strike']} | "
        f"delta={contract['delta']:.3f} | "
        f"bid=${contract['bid']:.2f} | "
        f"ask=${contract['ask']:.2f} | "
        f"spread={contract['spread_pct']:.1%} | qty={qty}"
    )

    success, execution_reason = submit_paper_buy(
        contract,
        symbol,
        signal,
        qty,
    )

    log(
        f"ENTRY RESULT | {symbol} | "
        f"success={success} | {execution_reason}"
    )


def run_scan_cycle() -> None:
    log("=" * 70)
    log(f"SCAN CYCLE | {ny_now().isoformat()}")

    # Manage existing risk before looking for new positions.
    manage_open_positions()

    allowed, reason = risk_gate()

    if not allowed:
        log(f"GLOBAL WAIT | {reason}")
        return

    for symbol in TRADE_SYMBOLS:
        try:
            scan_one_symbol(symbol)
        except Exception as exc:
            log(
                f"SCAN ERROR | {symbol} | "
                f"{type(exc).__name__}: {exc}",
                "error",
            )
            log(traceback.format_exc(), "error")


# =========================
# 7. STARTUP / MAIN LOOP
# =========================

def main() -> None:
    if PAPER_MODE is not True:
        raise RuntimeError("SAFETY STOP: PAPER MODE IS REQUIRED")

    initialize_part1()
    part2_self_check()
    part3_self_check()

    log("BOT STARTED | PAPER MODE | TELEGRAM DISABLED")
    log("WARNING | validate SDK compatibility before live deployment")
    log("WARNING | this program must not be used for live trading")

    while True:
        try:
            run_scan_cycle()
        except KeyboardInterrupt:
            log("BOT STOPPED BY USER")
            break
        except Exception as exc:
            log(
                f"MAIN LOOP ERROR | {type(exc).__name__}: {exc}",
                "error",
            )
            log(traceback.format_exc(), "error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
