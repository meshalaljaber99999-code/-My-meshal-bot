# ============================================================
# PROFESSIONAL ALPACA OPTIONS BOT (FIXED FEED)
# SPY + SPX / SPXW
# PAPER ONLY
# ============================================================

import os
import sys
import time
import math
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderSide,
    TimeInForce,
)

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient

from alpaca.data.requests import (
    StockBarsRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)

from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import OptionsFeed, DataFeed


# ============================================================
# CONFIG
# ============================================================

def clean_env(value):
    if not value:
        return value
    return "".join(c for c in value if ord(c) < 128).strip()


API_KEY = clean_env(os.getenv("API_KEY"))
SECRET_KEY = clean_env(os.getenv("SECRET_KEY"))

if not API_KEY or not SECRET_KEY:
    sys.exit("ERROR: API_KEY / SECRET_KEY غير موجودة.")


# ============================================================
# SAFETY
# ============================================================

PAPER_MODE = True

# استخدام Delayed للخيارات لتجنب مشاكل الاشتراكات في الحساب التجريبي
OPTIONS_FEED = OptionsFeed.DELAYED


# ============================================================
# STRATEGY
# ============================================================

STRATEGY = {

    "name": "Professional Options Strategy V2",

    "underlyings": [
        "SPY",
        "SPX",
    ],

    "signal": {
        "mode": "AUTO",
        "lookback_minutes": 1,
        "minimum_move_pct": 0.001,
    },

    "option": {
        "min_dte": 1,
        "max_dte": 14,
        "min_delta": 0.40,
        "max_delta": 0.60,
        "max_premium": 15.00,
        "max_spread_pct": 0.10,
        "min_open_interest": 100,
    },

    "risk": {
        "risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "max_trades_per_day": 5,
        "max_open_positions": 2,
        "max_contracts_per_trade": 5,
    },

    "exit": {
        "stop_loss_pct": 30.0,
        "take_profit_pct": 60.0,
        "trailing_enabled": True,
        "trailing_stop_pct": 15.0,
    },
}


# ============================================================
# CLIENTS
# ============================================================

trading_client = TradingClient(
    API_KEY,
    SECRET_KEY,
    paper=PAPER_MODE,
)

stock_data_client = StockHistoricalDataClient(
    API_KEY,
    SECRET_KEY,
)

option_data_client = OptionHistoricalDataClient(
    API_KEY,
    SECRET_KEY,
)

NY_TZ = ZoneInfo("America/New_York")


# ============================================================
# STATE
# ============================================================

trades_today = 0
daily_realized_pnl = 0.0
state_date = datetime.now(NY_TZ).date()


# ============================================================
# LOG
# ============================================================

def log(message):
    now = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# ACCOUNT
# ============================================================

def get_account():
    try:
        return trading_client.get_account()
    except Exception as e:
        log(f"ACCOUNT ERROR: {e}")
        return None


def get_equity():
    account = get_account()
    if not account:
        return 0.0
    try:
        return float(account.equity)
    except Exception:
        return 0.0


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_state():
    global trades_today
    global daily_realized_pnl
    global state_date

    today = datetime.now(NY_TZ).date()

    if today != state_date:
        trades_today = 0
        daily_realized_pnl = 0.0
        state_date = today
        log("🔄 Daily state reset.")


# ============================================================
# MARKET
# ============================================================

def is_market_open():
    try:
        clock = trading_client.get_clock()
        return clock.is_open
    except Exception as e:
        log(f"CLOCK ERROR: {e}")
        return False
