# ============================================================
# ALPACA OPTIONS BOT V3.5
# PAPER ONLY
#
# Supports:
#   - SPY Options
#   - SPX / SPXW Index Options
#
# DATA:
#   - Stocks: IEX
#   - Options: INDICATIVE
#
# V3.5 CHANGES:
#   - Shows exact SPY 5-minute movement
#   - Shows SPY current price
#   - Shows signal direction / reason
#   - Shows option-selection diagnostics
#   - Shows estimated position size
#   - Keeps OPRA fix
#   - Keeps SPX/SPXW support
#   - Keeps state recovery
# ============================================================

import os
import sys
import time
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
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
    StockLatestQuoteRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)

from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import OptionsFeed, DataFeed


# ============================================================
# SAFETY
# ============================================================

PAPER_MODE = True

API_KEY = (
    os.getenv("APCA_API_KEY_ID")
    or os.getenv("API_KEY")
)

API_SECRET = (
    os.getenv("APCA_API_SECRET_KEY")
    or os.getenv("SECRET_KEY")
)

if not API_KEY or not API_SECRET:
    print("❌ API keys are missing.")
    sys.exit(1)

if not PAPER_MODE:
    print("❌ SAFETY ERROR: This bot is PAPER ONLY.")
    sys.exit(1)


# ============================================================
# CLIENTS
# ============================================================

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
# DATA FEEDS
# ============================================================

OPTIONS_FEED = OptionsFeed.INDICATIVE
STOCK_FEED = DataFeed.IEX


# ============================================================
# TIMEZONE
# ============================================================

NY = ZoneInfo("America/New_York")


# ============================================================
# UNDERLYINGS
# ============================================================

UNDERLYINGS = [
    "SPY",
    "SPX",
]


# ============================================================
# SPX PRODUCT
#
# ANY  = SPX + SPXW
# SPXW = SPXW only
# SPX  = SPX monthly only
# ============================================================

SPX_PRODUCT_MODE = "ANY"


# ============================================================
# SIGNAL
# ============================================================

SIGNAL_LOOKBACK_MINUTES = 5

# 0.0010 = 0.10%
MIN_SIGNAL_MOVE = 0.0010


# ============================================================
# OPTIONS FILTERS
# ============================================================

MIN_DTE = 1
MAX_DTE = 14

MIN_DELTA = 0.40
MAX_DELTA = 0.60

MAX_PREMIUM_SPY = 15.00
MAX_PREMIUM_SPX = 15.00

MAX_SPREAD_PCT = 0.10

MIN_OPEN_INTEREST = 100


# ============================================================
# RISK
# ============================================================

# IMPORTANT:
# This is capital allocation, not stop-loss risk.
CAPITAL_ALLOCATION_PCT = 0.01

DAILY_LOSS_LIMIT_PCT = 0.03

MAX_TRADES_PER_DAY = 5

MAX_OPEN_POSITIONS = 2

MAX_CONTRACTS_PER_TRADE = 5


# ============================================================
# EXIT
# ============================================================

STOP_LOSS_PCT = 0.30

TAKE_PROFIT_PCT = 0.60

TRAILING_STOP_PCT = 0.15


# ============================================================
# LOOP
# ============================================================

SCAN_INTERVAL_SECONDS = 60

ERROR_SLEEP_SECONDS = 30

SYMBOL_DELAY_SECONDS = 2


# ============================================================
# STATE
# ============================================================

STATE_FILE = Path("bot_state.json")

DEFAULT_STATE = {
    "date": None,
    "trades_today": 0,
    "estimated_daily_pnl": 0.0,
    "position_state": {},
}

state = DEFAULT_STATE.copy()


# ============================================================
# LOG
# ============================================================

def log(message):
    now = datetime.now(NY).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[{now}] {message}",
        flush=True
    )


# ============================================================
# SAFE FLOAT
# ============================================================

def safe_float(
    value,
    default=None,
):
    try:
        if value is None:
            return default

        return float(value)

    except Exception:
        return default


# ============================================================
# LOAD STATE
# ============================================================

def load_state():
    global state

    try:

        if not STATE_FILE.exists():
            state = DEFAULT_STATE.copy()
            return

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            loaded = json.load(f)

        state = {
            "date": loaded.get("date"),
            "trades_today": loaded.get(
                "trades_today",
                0
            ),
            "estimated_daily_pnl": loaded.get(
                "estimated_daily_pnl",
                loaded.get(
                    "daily_realized_pnl",
                    0.0
                )
            ),
            "position_state": loaded.get(
                "position_state",
                {}
            ),
        }

        log("💾 State restored.")

    except Exception as e:

        log(
            f"⚠️ State load error: {e}"
        )

        state = DEFAULT_STATE.copy()


# ============================================================
# SAVE STATE
# ============================================================

def save_state():

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                state,
                f,
                indent=2,
                default=str,
            )

    except Exception as e:

        log(
            f"⚠️ State save error: {e}"
        )


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_state_if_needed():

    today = datetime.now(
        NY
    ).date().isoformat()

    if state.get("date") != today:

        state["date"] = today

        state["trades_today"] = 0

        state["estimated_daily_pnl"] = 0.0

        save_state()

        log(
            "🔄 Daily state reset."
        )


# ============================================================
# MARKET OPEN
# ============================================================

def is_market_open():

    now = datetime.now(NY)

    if now.weekday() >= 5:
        return False

    current_time = now.time()

    market_open = datetime.strptime(
        "09:30",
        "%H:%M",
    ).time()

    market_close = datetime.strptime(
        "16:00",
        "%H:%M",
    ).time()

    return (
        market_open
        <= current_time
        <= market_close
    )


# ============================================================
# ACCOUNT
# ============================================================

def get_account():

    try:

        return trading_client.get_account()

    except Exception as e:

        log(
            f"❌ Account error: {e}"
        )

        return None


# ============================================================
# ACCOUNT INFO
# ============================================================

def log_account():

    account = get_account()

    if not account:
        return

    equity = safe_float(
        account.equity,
        0
    )

    buying_power = safe_float(
        account.buying_power,
        0
    )

    options_level = getattr(
        account,
        "options_trading_level",
        None
    )

    log(
        f"ACCOUNT | "
        f"Status={account.status} | "
        f"Equity=${equity:,.2f} | "
        f"BuyingPower=${buying_power:,.2f} | "
        f"OptionsLevel={options_level}"
    )


# ============================================================
# SPY LATEST PRICE
# ============================================================

def get_spy_price():

    try:

        request = StockLatestQuoteRequest(
            symbol_or_symbols=["SPY"],
            feed=STOCK_FEED,
        )

        quotes = (
            stock_data_client
            .get_stock_latest_quote(request)
        )

        quote = quotes["SPY"]

        bid = safe_float(
            quote.bid_price,
            0
        )

        ask = safe_float(
            quote.ask_price,
            0
        )

        if bid > 0 and ask > 0:
            return (bid + ask) / 2

        if ask > 0:
            return ask

        if bid > 0:
            return bid

        return None

    except Exception as e:

        log(
            f"⚠️ SPY price error: {e}"
        )

        return None


# ============================================================
# GET SPY BARS
# ============================================================

def get_spy_bars():

    try:

        now = datetime.now(NY)

        start = (
            now
            - timedelta(
                minutes=SIGNAL_LOOKBACK_MINUTES
                + 10
            )
        )

        request = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute,
            start=start,
            end=now,
            feed=STOCK_FEED,
        )

        bars = (
            stock_data_client
            .get_stock_bars(request)
        )

        return bars["SPY"]

    except Exception as e:

        log(
            f"⚠️ SPY bars error: {e}"
        )

        return []


# ============================================================
# SPY SIGNAL
# ============================================================

def get_spy_signal():

    bars = get_spy_bars()

    if len(bars) < (
        SIGNAL_LOOKBACK_MINUTES + 1
    ):

        log(
            f"⚠️ Not enough SPY bars "
            f"({len(bars)} received)."
        )

        return None, None, None, None

    closes = [
        safe_float(
            bar.close,
            0
        )
        for bar in bars
    ]

    old_price = closes[
        -(SIGNAL_LOOKBACK_MINUTES + 1)
    ]

    current_close = closes[-1]

    if old_price <= 0:

        return (
            None,
            None,
            None,
            None,
        )

    move = (
        current_close
        - old_price
    ) / old_price

    if move >= MIN_SIGNAL_MOVE:

        return (
            "CALL",
            move,
            old_price,
            current_close,
        )

    if move <= -MIN_SIGNAL_MOVE:

        return (
            "PUT",
            move,
            old_price,
            current_close,
        )

    return (
        None,
        move,
        old_price,
        current_close,
    )


# ============================================================
# SIGNAL
# ============================================================

def get_signal(underlying):

    # SPY = direct signal
    if underlying == "SPY":

        return get_spy_signal()

    # SPX = SPY proxy
    if underlying == "SPX":

        return get_spy_signal()

    return (
        None,
        None,
        None,
        None,
    )


# ============================================================
# PRINT SIGNAL STATUS
# ============================================================

def print_signal_status(
    underlying,
    signal,
    move,
    old_price,
    current_price,
):

    if move is None:

        log(
            f"{underlying}: "
            f"Unable to calculate signal."
        )

        return

    direction = "FLAT"

    if move > 0:
        direction = "UP"

    elif move < 0:
        direction = "DOWN"

    threshold_pct = (
        MIN_SIGNAL_MOVE * 100
    )

    move_pct = move * 100

    if signal == "CALL":

        reason = (
            f"CALL threshold reached "
            f"(≥ +{threshold_pct:.2f}%)"
        )

    elif signal == "PUT":

        reason = (
            f"PUT threshold reached "
            f"(≤ -{threshold_pct:.2f}%)"
        )

    else:

        remaining = (
            MIN_SIGNAL_MOVE
            - abs(move)
        )

        remaining_pct = (
            max(remaining, 0)
            * 100
        )

        reason = (
            f"Below threshold | "
            f"Need ≈ {remaining_pct:.3f}% more movement"
        )

    log(
        f"{underlying}: "
        f"{'🚨 SIGNAL' if signal else '⚪ No signal'} | "
        f"Direction={direction} | "
        f"5m Move={move_pct:+.3f}% | "
        f"From=${old_price:.2f} "
        f"To=${current_price:.2f} | "
        f"{reason}"
    )


# ============================================================
# GET OPTION CONTRACTS
# ============================================================

def get_option_contracts(
    underlying,
    contract_type,
):

    try:

        today = datetime.now(
            NY
        ).date()

        expiration_gte = (
            today
            + timedelta(
                days=MIN_DTE
            )
        )

        expiration_lte = (
            today
            + timedelta(
                days=MAX_DTE
            )
        )

        request = GetOptionContractsRequest(
            underlying_symbols=[
                underlying
            ],

            status=AssetStatus.ACTIVE,

            expiration_date_gte=(
                expiration_gte
            ),

            expiration_date_lte=(
                expiration_lte
            ),

            type=contract_type,

            limit=1000,
        )

        response = (
            trading_client
            .get_option_contracts(
                request
            )
        )

        return getattr(
            response,
            "option_contracts",
            []
        )

    except Exception as e:

        log(
            f"⚠️ Option contracts error "
            f"{underlying}: {e}"
        )

        return []


# ============================================================
# OPTION CHAIN
#
# NO LIMIT HERE.
#
# This fixes the previous Alpaca-py issue.
# ============================================================

def get_option_chain(
    underlying,
    contract_type,
):

    try:

        today = datetime.now(
            NY
        ).date()

        expiration_gte = (
            today
            + timedelta(
                days=MIN_DTE
            )
        )

        expiration_lte = (
            today
            + timedelta(
                days=MAX_DTE
            )
        )

        request = OptionChainRequest(
            underlying_symbol=underlying,

            # IMPORTANT:
            # Never use OPRA here.
            feed=OPTIONS_FEED,

            type=contract_type,

            expiration_date_gte=(
                expiration_gte
            ),

            expiration_date_lte=(
                expiration_lte
            ),
        )

        return (
            option_data_client
            .get_option_chain(
                request
            )
        )

    except Exception as e:

        error_text = str(e)

        log(
            f"⚠️ Option chain error "
            f"{underlying}: {error_text}"
        )

        lower = error_text.lower()

        if "opra agreement" in lower:

            log(
                "❌ OPRA error detected."
            )

            log(
                "ℹ️ Code is explicitly using "
                "OptionsFeed.INDICATIVE."
            )

        if (
            "index options" in lower
            and (
                "enable" in lower
                or "sandbox" in lower
            )
        ):

            log(
                "❌ SPX Index Options are not "
                "enabled for this environment."
            )

        return {}


# ============================================================
# OPTION QUOTE
# ============================================================

def get_option_quote(symbol):

    try:

        request = OptionLatestQuoteRequest(
            symbol_or_symbols=[symbol],

            # IMPORTANT
            feed=OPTIONS_FEED,
        )

        quotes = (
            option_data_client
            .get_option_latest_quote(
                request
            )
        )

        quote = quotes.get(symbol)

        if quote is None:
            return None

        bid = safe_float(
            quote.bid_price,
            0
        )

        ask = safe_float(
            quote.ask_price,
            0
        )

        if bid <= 0 and ask <= 0:
            return None

        if bid <= 0:
            bid = ask

        if ask <= 0:
            ask = bid

        mid = (
            bid + ask
        ) / 2

        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
        }

    except Exception as e:

        log(
            f"⚠️ Quote error "
            f"{symbol}: {e}"
        )

        return None


# ============================================================
# GET DELTA
# ============================================================

def get_delta(snapshot):

    try:

        greeks = getattr(
            snapshot,
            "greeks",
            None
        )

        if greeks is None:
            return None

        return safe_float(
            getattr(
                greeks,
                "delta",
                None
            )
        )

    except Exception:

        return None


# ============================================================
# OPEN INTEREST
# ============================================================

def get_open_interest(snapshot):

    try:

        return safe_float(
            getattr(
                snapshot,
                "open_interest",
                None
            ),
            0
        )

    except Exception:

        return 0


# ============================================================
# SNAPSHOT QUOTE
# ============================================================

def get_snapshot_quote(snapshot):

    try:

        quote = getattr(
            snapshot,
            "latest_quote",
            None
        )

        if quote is None:
            return None

        bid = safe_float(
            getattr(
                quote,
                "bid_price",
                None
            ),
            0
        )

        ask = safe_float(
            getattr(
                quote,
                "ask_price",
                None
            ),
            0
        )

        if bid <= 0 and ask <= 0:
            return None

        if bid <= 0:
            bid = ask

        if ask <= 0:
            ask = bid

        mid = (
            bid + ask
        ) / 2

        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
        }

    except Exception:

        return None


# ============================================================
# SPX ROOT FILTER
# ============================================================

def spx_root_allowed(contract):

    if SPX_PRODUCT_MODE == "ANY":
        return True

    root = getattr(
        contract,
        "root_symbol",
        None
    )

    if root is None:
        return False

    root = str(root).upper()

    if SPX_PRODUCT_MODE == "SPXW":
        return root == "SPXW"

    if SPX_PRODUCT_MODE == "SPX":
        return root == "SPX"

    return True


# ============================================================
# SELECT OPTION
# ============================================================

def select_option_contract(
    underlying,
    signal,
):

    if signal not in (
        "CALL",
        "PUT",
    ):

        return None

    contract_type = (
        ContractType.CALL
        if signal == "CALL"
        else ContractType.PUT
    )

    log(
        f"🔎 Searching "
        f"{underlying} {signal} options..."
    )

    contracts = get_option_contracts(
        underlying,
        contract_type
    )

    if not contracts:

        log(
            f"⚠️ No contracts returned "
            f"for {underlying}."
        )

        return None

    log(
        f"📋 Contracts received: "
        f"{len(contracts)}"
    )

    chain = get_option_chain(
        underlying,
        contract_type
    )

    if not chain:

        log(
            f"⚠️ Empty option chain "
            f"for {underlying}."
        )

        return None

    log(
        f"📡 Chain snapshots: "
        f"{len(chain)}"
    )

    # --------------------------------------------------------
    # Underlying price
    # --------------------------------------------------------

    underlying_price = get_spy_price()

    if not underlying_price:

        return None

    candidates = []

    today = datetime.now(
        NY
    ).date()

    # --------------------------------------------------------
    # Counters for diagnostics
    # --------------------------------------------------------

    count_expiration = 0
    count_delta = 0
    count_oi = 0
    count_quote = 0
    count_premium = 0
    count_spread = 0
    count_root = 0

    # --------------------------------------------------------
    # Loop
    # --------------------------------------------------------

    for contract in contracts:

        try:

            symbol = getattr(
                contract,
                "symbol",
                None
            )

            if not symbol:
                continue

            # ------------------------------------------------
            # SPX / SPXW
            # ------------------------------------------------

            if (
                underlying == "SPX"
                and not spx_root_allowed(
                    contract
                )
            ):

                continue

            count_root += 1

            # ------------------------------------------------
            # Expiration
            # ------------------------------------------------

            expiration = getattr(
                contract,
                "expiration_date",
                None
            )

            if expiration is None:
                continue

            if hasattr(
                expiration,
                "date"
            ):

                expiration = (
                    expiration.date()
                )

            if isinstance(
                expiration,
                str
            ):

                try:

                    expiration = (
                        datetime.strptime(
                            expiration[:10],
                            "%Y-%m-%d"
                        ).date()
                    )

                except Exception:

                    continue

            dte = (
                expiration - today
            ).days

            if dte < MIN_DTE:
                continue

            if dte > MAX_DTE:
                continue

            count_expiration += 1

            # ------------------------------------------------
            # Strike
            # ------------------------------------------------

            strike = safe_float(
                getattr(
                    contract,
                    "strike_price",
                    None
                )
            )

            if strike is None:
                continue

            # ------------------------------------------------
            # Snapshot
            # ------------------------------------------------

            snapshot = chain.get(
                symbol
            )

            if snapshot is None:
                continue

            # ------------------------------------------------
            # Delta
            # ------------------------------------------------

            delta = get_delta(
                snapshot
            )

            if delta is None:
                continue

            absolute_delta = abs(
                delta
            )

            if (
                absolute_delta
                < MIN_DELTA
                or
                absolute_delta
                > MAX_DELTA
            ):

                continue

            count_delta += 1

            # ------------------------------------------------
            # Open Interest
            # ------------------------------------------------

            open_interest = (
                get_open_interest(
                    snapshot
                )
            )

            if (
                open_interest
                < MIN_OPEN_INTEREST
            ):

                continue

            count_oi += 1

            # ------------------------------------------------
            # Quote
            # ------------------------------------------------

            quote = (
                get_snapshot_quote(
                    snapshot
                )
            )

            if quote is None:
                continue

            count_quote += 1

            bid = quote["bid"]
            ask = quote["ask"]
            mid = quote["mid"]

            if mid <= 0:
                continue

            # ------------------------------------------------
            # Premium
            # ------------------------------------------------

            max_premium = (
                MAX_PREMIUM_SPY
                if underlying == "SPY"
                else MAX_PREMIUM_SPX
            )

            if mid > max_premium:

                continue

            count_premium += 1

            # ------------------------------------------------
            # Spread
            # ------------------------------------------------

            spread_pct = (
                ask - bid
            ) / mid

            if (
                spread_pct
                > MAX_SPREAD_PCT
            ):

                continue

            count_spread += 1

            # ------------------------------------------------
            # Score
            # ------------------------------------------------

            delta_score = abs(
                absolute_delta
                - 0.50
            )

            spread_score = (
                spread_pct
            )

            oi_score = (
                1
                / max(
                    open_interest,
                    1
                )
            )

            # Prefer lower DTE slightly
            dte_score = (
                dte * 0.001
            )

            score = (
                delta_score * 10
                + spread_score * 5
                + oi_score
                + dte_score
            )

            candidates.append(
                {
                    "symbol": symbol,
                    "underlying": underlying,
                    "signal": signal,
                    "strike": strike,
                    "expiration": str(
                        expiration
                    ),
                    "dte": dte,
                    "delta": delta,
                    "open_interest":
                        open_interest,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_pct":
                        spread_pct,
                    "root_symbol":
                        getattr(
                            contract,
                            "root_symbol",
                            None
                        ),
                    "score": score,
                }
            )

        except Exception:

            continue

    # --------------------------------------------------------
    # Diagnostics
    # --------------------------------------------------------

    log(
        f"🔬 Filter results | "
        f"Root={count_root} | "
        f"DTE={count_expiration} | "
        f"Delta={count_delta} | "
        f"OI={count_oi} | "
        f"Quote={count_quote} | "
        f"Premium={count_premium} | "
        f"Spread={count_spread}"
    )

    # --------------------------------------------------------
    # No candidates
    # --------------------------------------------------------

    if not candidates:

        log(
            f"⚠️ No suitable "
            f"{underlying} {signal} contract."
        )

        return None

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x["score"]
    )

    best = candidates[0]

    log(
        f"🎯 SELECTED | "
        f"{best['symbol']} | "
        f"Root={best['root_symbol']} | "
        f"Strike={best['strike']} | "
        f"DTE={best['dte']} | "
        f"Delta={best['delta']:.3f} | "
        f"Bid=${best['bid']:.2f} | "
        f"Ask=${best['ask']:.2f} | "
        f"Mid=${best['mid']:.2f} | "
        f"Spread={best['spread_pct']:.1%} | "
        f"OI={best['open_interest']:.0f}"
    )

    return best


# ============================================================
# GET OPTION POSITIONS
# ============================================================

def get_option_positions():

    try:

        positions = (
            trading_client
            .get_all_positions()
        )

        option_positions = []

        for position in positions:

            asset_class = str(
                getattr(
                    position,
                    "asset_class",
                    ""
                )
            ).lower()

            if "option" not in asset_class:
                continue

            qty = safe_float(
                getattr(
                    position,
                    "qty",
                    0
                ),
                0
            )

            if not qty:
                continue

            option_positions.append(
                position
            )

        return option_positions

    except Exception as e:

        log(
            f"⚠️ Position error: {e}"
        )

        return []


# ============================================================
# UNDERLYING FROM SYMBOL
# ============================================================

def get_position_underlying(
    symbol
):

    symbol = str(
        symbol
    ).upper()

    if symbol.startswith("SPY"):
        return "SPY"

    if symbol.startswith("SPX"):
        return "SPX"

    return None


# ============================================================
# INITIALIZE / RECOVER POSITIONS
# ============================================================

def initialize_position_state():

    positions = (
        get_option_positions()
    )

    if not positions:
        return

    changed = False

    for position in positions:

        symbol = str(
            getattr(
                position,
                "symbol",
                ""
            )
        )

        if not symbol:
            continue

        if (
            symbol
            in state["position_state"]
        ):
            continue

        qty = safe_float(
            getattr(
                position,
                "qty",
                0
            ),
            0
        )

        avg_entry = safe_float(
            getattr(
                position,
                "avg_entry_price",
                0
            ),
            0
        )

        quote = get_option_quote(
            symbol
        )

        current_mid = (
            quote["mid"]
            if quote
            else avg_entry
        )

        state[
            "position_state"
        ][symbol] = {

            "underlying":
                get_position_underlying(
                    symbol
                ),

            "entry_price":
                avg_entry,

            "highest_price":
                max(
                    avg_entry,
                    current_mid
                ),

            "qty":
                qty,

            "entry_time":
                datetime.now(
                    NY
                ).isoformat(),

            "order_id":
                None,
        }

        changed = True

        log(
            f"🔄 POSITION RECOVERED | "
            f"{symbol} | "
            f"Qty={qty} | "
            f"Entry=${avg_entry:.2f}"
        )

    if changed:
        save_state()


# ============================================================
# OPEN POSITION COUNT
# ============================================================

def open_position_count():

    return len(
        get_option_positions()
    )


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_quantity(
    premium
):

    if (
        premium is None
        or premium <= 0
    ):

        return 0

    account = get_account()

    if not account:
        return 0

    equity = safe_float(
        account.equity,
        0
    )

    buying_power = safe_float(
        account.buying_power,
        0
    )

    allocation = (
        equity
        * CAPITAL_ALLOCATION_PCT
    )

    contract_cost = (
        premium * 100
    )

    if contract_cost <= 0:
        return 0

    quantity = math.floor(
        allocation
        / contract_cost
    )

    max_by_bp = math.floor(
        buying_power
        / contract_cost
    )

    quantity = min(
        quantity,
        max_by_bp,
        MAX_CONTRACTS_PER_TRADE,
    )

    log(
        f"💵 Position sizing | "
        f"Premium=${premium:.2f} | "
        f"Contract cost≈${contract_cost:.2f} | "
        f"Allocation≈${allocation:.2f} | "
        f"Qty={quantity}"
    )

    return max(
        quantity,
        0
    )


# ============================================================
# EXECUTE ENTRY
# ============================================================

def execute_entry(
    contract
):

    if not contract:
        return False

    symbol = contract["symbol"]

    premium = contract["mid"]

    quantity = calculate_quantity(
        premium
    )

    if quantity <= 0:

        log(
            f"⚠️ No entry: calculated "
            f"quantity is 0 for {symbol}."
        )

        return False

    if (
        open_position_count()
        >= MAX_OPEN_POSITIONS
    ):

        log(
            "⛔ Maximum open positions reached."
        )

        return False

    if (
        state["trades_today"]
        >= MAX_TRADES_PER_DAY
    ):

        log(
            "⛔ Maximum trades today reached."
        )

        return False

    try:

        order_request = (
            MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )

        order = (
            trading_client
            .submit_order(
                order_request
            )
        )

        order_id = str(
            getattr(
                order,
                "id",
                ""
            )
        )

        state[
            "trades_today"
        ] += 1

        state[
            "position_state"
        ][symbol] = {

            "underlying":
                contract[
                    "underlying"
                ],

            "entry_price":
                premium,

            "highest_price":
                premium,

            "qty":
                quantity,

            "entry_time":
                datetime.now(
                    NY
                ).isoformat(),

            "order_id":
                order_id,
        }

        save_state()

        log(
            f"🟢 PAPER ENTRY | "
            f"{symbol} | "
            f"Qty={quantity} | "
            f"Estimated Entry=${premium:.2f} | "
            f"OrderID={order_id}"
        )

        return True

    except Exception as e:

        error_text = str(e)

        log(
            f"❌ Entry failed | "
            f"{symbol} | "
            f"{error_text}"
        )

        if (
            "index options"
            in error_text.lower()
        ):

            log(
                "ℹ️ Check SPX Index Options "
                "enablement."
            )

        return False


# ============================================================
# CLOSE POSITION
# ============================================================

def close_position(
    position,
    reason,
    current_price,
):

    symbol = str(
        getattr(
            position,
            "symbol",
            ""
        )
    )

    if not symbol:
        return False

    qty = safe_float(
        getattr(
            position,
            "qty",
            0
        ),
        0
    )

    avg_entry = safe_float(
        getattr(
            position,
            "avg_entry_price",
            0
        ),
        0
    )

    try:

        trading_client.close_position(
            symbol
        )

        estimated_pnl = (
            current_price
            - avg_entry
        ) * qty * 100

        state[
            "estimated_daily_pnl"
        ] += estimated_pnl

        if (
            symbol
            in state["position_state"]
        ):

            del state[
                "position_state"
            ][symbol]

        save_state()

        log(
            f"🔴 PAPER EXIT | "
            f"{symbol} | "
            f"Reason={reason} | "
            f"Entry=${avg_entry:.2f} | "
            f"Exit≈${current_price:.2f} | "
            f"Estimated P&L≈${estimated_pnl:.2f}"
        )

        return True

    except Exception as e:

        log(
            f"❌ Close failed | "
            f"{symbol} | "
            f"{e}"
        )

        return False


# ============================================================
# MANAGE POSITIONS
# ============================================================

def manage_positions():

    positions = (
        get_option_positions()
    )

    if not positions:
        return

    active_symbols = set()

    for position in positions:

        symbol = str(
            getattr(
                position,
                "symbol",
                ""
            )
        )

        if not symbol:
            continue

        active_symbols.add(
            symbol
        )

        avg_entry = safe_float(
            getattr(
                position,
                "avg_entry_price",
                0
            ),
            0
        )

        if avg_entry <= 0:
            continue

        quote = get_option_quote(
            symbol
        )

        if not quote:
            continue

        current_price = quote[
            "mid"
        ]

        if current_price <= 0:
            continue

        position_data = (
            state[
                "position_state"
            ].get(symbol)
        )

        if position_data is None:

            position_data = {

                "underlying":
                    get_position_underlying(
                        symbol
                    ),

                "entry_price":
                    avg_entry,

                "highest_price":
                    avg_entry,

                "qty":
                    safe_float(
                        getattr(
                            position,
                            "qty",
                            0
                        ),
                        0
                    ),

                "entry_time":
                    datetime.now(
                        NY
                    ).isoformat(),

                "order_id":
                    None,
            }

            state[
                "position_state"
            ][symbol] = (
                position_data
            )

        entry_price = safe_float(
            position_data.get(
                "entry_price",
                avg_entry
            ),
            avg_entry
        )

        highest_price = safe_float(
            position_data.get(
                "highest_price",
                entry_price
            ),
            entry_price
        )

        if (
            current_price
            > highest_price
        ):

            highest_price = (
                current_price
            )

            position_data[
                "highest_price"
            ] = highest_price

        pnl_pct = (
            current_price
            - entry_price
        ) / entry_price

        trailing_price = (
            highest_price
            * (
                1
                - TRAILING_STOP_PCT
            )
        )

        log(
            f"📊 POSITION | "
            f"{symbol} | "
            f"Entry=${entry_price:.2f} | "
            f"Now=${current_price:.2f} | "
            f"P&L={pnl_pct:+.1%} | "
            f"High=${highest_price:.2f}"
        )

        # ----------------------------------------------------
        # STOP LOSS
        # ----------------------------------------------------

        if (
            pnl_pct
            <= -STOP_LOSS_PCT
        ):

            close_position(
                position,
                "STOP LOSS",
                current_price
            )

            continue

        # ----------------------------------------------------
        # TAKE PROFIT
        # ----------------------------------------------------

        if (
            pnl_pct
            >= TAKE_PROFIT_PCT
        ):

            close_position(
                position,
                "TAKE PROFIT",
                current_price
            )

            continue

        # ----------------------------------------------------
        # TRAILING STOP
        # ----------------------------------------------------

        if (
            highest_price
            > entry_price
            and current_price
            <= trailing_price
        ):

            close_position(
                position,
                "TRAILING STOP",
                current_price
            )

            continue

    # --------------------------------------------------------
    # Clean stale state
    # --------------------------------------------------------

    for symbol in list(
        state[
            "position_state"
        ].keys()
    ):

        if symbol not in active_symbols:

            del state[
                "position_state"
            ][symbol]

    save_state()


# ============================================================
# DAILY LOSS
# ============================================================

def daily_loss_limit_hit():

    account = get_account()

    if not account:
        return False

    equity = safe_float(
        account.equity,
        0
    )

    if equity <= 0:
        return False

    loss_limit = (
        equity
        * DAILY_LOSS_LIMIT_PCT
    )

    pnl = safe_float(
        state.get(
            "estimated_daily_pnl",
            0
        ),
        0
    )

    if pnl <= -loss_limit:

        log(
            f"⛔ DAILY LOSS LIMIT | "
            f"Estimated P&L=${pnl:.2f} | "
            f"Limit=${-loss_limit:.2f}"
        )

        return True

    return False


# ============================================================
# PROCESS UNDERLYING
# ============================================================

def process_underlying(
    underlying
):

    if (
        state[
            "trades_today"
        ]
        >= MAX_TRADES_PER_DAY
    ):

        return

    if (
        open_position_count()
        >= MAX_OPEN_POSITIONS
    ):

        return

    (
        signal,
        move,
        old_price,
        current_price,
    ) = get_signal(
        underlying
    )

    print_signal_status(
        underlying,
        signal,
        move,
        old_price,
        current_price,
    )

    if signal is None:
        return

    log(
        f"🚨 SIGNAL CONFIRMED | "
        f"{underlying} | "
        f"{signal}"
    )

    contract = (
        select_option_contract(
            underlying,
            signal
        )
    )

    if contract is None:
        return

    execute_entry(
        contract
    )


# ============================================================
# STARTUP
# ============================================================

def startup_test():

    log("=" * 75)

    log(
        "🚀 ALPACA OPTIONS BOT V3.5"
    )

    log("=" * 75)

    log(
        "🛡️ PAPER MODE = TRUE"
    )

    log(
        "📡 STOCK FEED = IEX"
    )

    log(
        "📡 OPTIONS FEED = INDICATIVE"
    )

    log(
        "📈 UNDERLYINGS = SPY + SPX"
    )

    log(
        f"📅 SPX PRODUCT = "
        f"{SPX_PRODUCT_MODE}"
    )

    log(
        f"🎯 SIGNAL = "
        f"{SIGNAL_LOOKBACK_MINUTES}m "
        f"/ "
        f"{MIN_SIGNAL_MOVE:.2%}"
    )

    log(
        "⚠️ SPX direction uses SPY "
        "as proxy."
    )

    account = get_account()

    if not account:

        log(
            "❌ Alpaca connection failed."
        )

        return False

    log_account()

    # --------------------------------------------------------
    # Test SPY option chain
    # --------------------------------------------------------

    chain = get_option_chain(
        "SPY",
        ContractType.CALL
    )

    if chain:

        log(
            f"✅ INDICATIVE option chain "
            f"working | "
            f"{len(chain)} snapshots"
        )

    else:

        log(
            "⚠️ SPY option chain "
            "returned empty."
        )

    log("=" * 75)

    return True


# ============================================================
# MAIN
# ============================================================

def main():

    load_state()

    reset_daily_state_if_needed()

    if not startup_test():

        sys.exit(1)

    initialize_position_state()

    log(
        "🤖 BOT STARTED"
    )

    while True:

        try:

            reset_daily_state_if_needed()

            # ------------------------------------------------
            # Manage existing positions first
            # ------------------------------------------------

            manage_positions()

            # ------------------------------------------------
            # Daily loss protection
            # ------------------------------------------------

            if daily_loss_limit_hit():

                log(
                    "⏸️ Trading paused "
                    "for daily loss protection."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # Market closed
            # ------------------------------------------------

            if not is_market_open():

                log(
                    "💤 Market closed."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # Max positions
            # ------------------------------------------------

            if (
                open_position_count()
                >= MAX_OPEN_POSITIONS
            ):

                log(
                    "⏸️ Max open positions."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # Max trades
            # ------------------------------------------------

            if (
                state[
                    "trades_today"
                ]
                >= MAX_TRADES_PER_DAY
            ):

                log(
                    "⏸️ Max trades today."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # SPY
            # ------------------------------------------------

            process_underlying(
                "SPY"
            )

            time.sleep(
                SYMBOL_DELAY_SECONDS
            )

            # ------------------------------------------------
            # SPX
            # ------------------------------------------------

            if (
                open_position_count()
                < MAX_OPEN_POSITIONS
                and
                state[
                    "trades_today"
                ]
                < MAX_TRADES_PER_DAY
            ):

                process_underlying(
                    "SPX"
                )

            save_state()

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            log(
                "🛑 BOT STOPPED MANUALLY"
            )

            save_state()

            break

        except Exception as e:

            log(
                f"❌ MAIN LOOP ERROR: {e}"
            )

            save_state()

            time.sleep(
                ERROR_SLEEP_SECONDS
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()