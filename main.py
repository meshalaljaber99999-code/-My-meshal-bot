# ============================================================
# PROFESSIONAL ALPACA OPTIONS BOT V3.2
# SPY + SPX
# PAPER TRADING ONLY
# ============================================================

import os
import sys
import time
import json
import math
from datetime import datetime, timedelta, time as dt_time
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

from alpaca.data.historical import (
    StockHistoricalDataClient,
    OptionHistoricalDataClient,
)

from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)

from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import OptionsFeed, DataFeed


# ============================================================
# 1. ENVIRONMENT
# ============================================================

API_KEY = os.getenv("APCA_API_KEY_ID") or os.getenv("API_KEY")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY") or os.getenv("SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    print("ERROR: API_KEY / SECRET_KEY not found.")
    sys.exit(1)


# ============================================================
# HARD SAFETY LOCK
# ============================================================

PAPER_MODE = True

if PAPER_MODE is not True:
    print("FATAL: This bot is PAPER TRADING ONLY.")
    sys.exit(1)


# ============================================================
# DATA FEEDS
# ============================================================

OPTIONS_FEED = OptionsFeed.OPRA
STOCK_FEED = DataFeed.IEX


# ============================================================
# TIMEZONE
# ============================================================

NY = ZoneInfo("America/New_York")


# ============================================================
# BOT CONFIGURATION
# ============================================================

BOT_NAME = "ALPACA_OPTIONS_BOT_V3.2"

UNDERLYINGS = [
    "SPY",
    "SPX",
]

SIGNAL_MODE = "AUTO"

SIGNAL_LOOKBACK_MINUTES = 5

# 0.10%
MINIMUM_MOVE_PCT = 0.0010


# ============================================================
# OPTION SELECTION
# ============================================================

MIN_DTE = 1
MAX_DTE = 14

MIN_DELTA = 0.40
MAX_DELTA = 0.60

# Maximum option premium per share.
# $15 = $1,500 notional per contract.
MAX_PREMIUM = 15.00

# 10% bid/ask spread.
MAX_SPREAD_PCT = 0.10

MIN_OPEN_INTEREST = 100

PREFER_ATM = True


# ============================================================
# RISK
# ============================================================

RISK_PER_TRADE_PCT = 0.01

MAX_DAILY_LOSS_PCT = 0.03

MAX_TRADES_PER_DAY = 5

MAX_OPEN_POSITIONS = 2

MAX_CONTRACTS_PER_TRADE = 5


# ============================================================
# EXIT
# ============================================================

STOP_LOSS_PCT = 0.30

TAKE_PROFIT_PCT = 0.60

TRAILING_ENABLED = True

TRAILING_PCT = 0.15


# ============================================================
# LOOP
# ============================================================

SCAN_INTERVAL_SECONDS = 60

ERROR_SLEEP_SECONDS = 30

SYMBOL_DELAY_SECONDS = 1


# ============================================================
# STATE
# ============================================================

STATE_FILE = Path("bot_state.json")

trades_today = 0
daily_realized_pnl = 0.0
state_date = datetime.now(NY).date().isoformat()

# Example:
#
# position_state = {
#   "contract_symbol": {
#       "underlying": "SPY",
#       "entry_price": 1.20,
#       "highest_price": 1.45,
#       "qty": 2
#   }
# }
#
position_state = {}


# ============================================================
# CLIENTS
# ============================================================

trading_client = TradingClient(
    API_KEY,
    SECRET_KEY,
    paper=True,
)

stock_client = StockHistoricalDataClient(
    API_KEY,
    SECRET_KEY,
)

option_client = OptionHistoricalDataClient(
    API_KEY,
    SECRET_KEY,
)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# SAFE FLOAT
# ============================================================

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


# ============================================================
# STATE SAVE
# ============================================================

def save_state():
    global trades_today
    global daily_realized_pnl
    global state_date
    global position_state

    data = {
        "date": state_date,
        "trades_today": trades_today,
        "daily_realized_pnl": daily_realized_pnl,
        "position_state": position_state,
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    except Exception as e:
        log(f"STATE SAVE ERROR: {e}")


# ============================================================
# STATE LOAD
# ============================================================

def load_state():

    global trades_today
    global daily_realized_pnl
    global state_date
    global position_state

    today = datetime.now(NY).date().isoformat()

    if not STATE_FILE.exists():

        state_date = today
        trades_today = 0
        daily_realized_pnl = 0.0
        position_state = {}

        save_state()

        return

    try:

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        saved_date = data.get("date")

        if saved_date == today:

            state_date = saved_date

            trades_today = int(
                data.get("trades_today", 0)
            )

            daily_realized_pnl = safe_float(
                data.get("daily_realized_pnl", 0)
            )

            position_state = data.get(
                "position_state",
                {}
            )

        else:

            state_date = today

            trades_today = 0

            daily_realized_pnl = 0.0

            position_state = {}

            save_state()

    except Exception as e:

        log(f"STATE LOAD ERROR: {e}")

        state_date = today
        trades_today = 0
        daily_realized_pnl = 0.0
        position_state = {}


# ============================================================
# RESET DAILY STATE
# ============================================================

def reset_daily_state_if_needed():

    global state_date
    global trades_today
    global daily_realized_pnl

    today = datetime.now(NY).date().isoformat()

    if state_date != today:

        log("New trading day detected.")

        state_date = today

        trades_today = 0

        daily_realized_pnl = 0.0

        save_state()


# ============================================================
# ACCOUNT
# ============================================================

def get_account():

    return trading_client.get_account()


# ============================================================
# ACCOUNT EQUITY
# ============================================================

def get_equity():

    account = get_account()

    return safe_float(
        account.equity
    )


# ============================================================
# BUYING POWER
# ============================================================

def get_buying_power():

    account = get_account()

    return safe_float(
        account.buying_power
    )


# ============================================================
# DAILY LOSS LIMIT
# ============================================================

def daily_loss_limit_reached():

    equity = get_equity()

    if equity <= 0:
        return True

    max_loss = equity * MAX_DAILY_LOSS_PCT

    if daily_realized_pnl <= -max_loss:

        log(
            f"DAILY LOSS LIMIT HIT | "
            f"PnL=${daily_realized_pnl:.2f} | "
            f"Limit=-${max_loss:.2f}"
        )

        return True

    return False


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    now = datetime.now(NY)

    if now.weekday() >= 5:
        return False

    current = now.time()

    market_open = dt_time(9, 30)

    market_close = dt_time(16, 0)

    return (
        market_open
        <= current
        < market_close
    )


# ============================================================
# STOCK PRICE
# ============================================================

def get_stock_price(symbol):

    request = StockLatestQuoteRequest(
        symbol_or_symbols=symbol,
        feed=STOCK_FEED,
    )

    quotes = stock_client.get_stock_latest_quote(
        request
    )

    quote = quotes.get(symbol)

    if quote is None:
        return None

    bid = safe_float(
        quote.bid_price
    )

    ask = safe_float(
        quote.ask_price
    )

    if bid > 0 and ask > 0:

        return (bid + ask) / 2

    if ask > 0:
        return ask

    if bid > 0:
        return bid

    return None


# ============================================================
# STOCK SIGNAL
# ============================================================

def get_signal(symbol):

    # --------------------------------------------------------
    # SPY signal can use stock bars.
    #
    # SPX:
    # Alpaca's standard historical API documented here is for
    # stocks/options/etc.; therefore we do NOT pretend SPX is
    # a stock and query it through StockBarsRequest.
    #
    # For this V3.2 paper engine, SPX signal is derived from
    # the currently available SPX option chain / quote path.
    # --------------------------------------------------------

    if symbol == "SPX":

        return get_spx_signal_from_chain()

    start = datetime.now(NY) - timedelta(
        minutes=SIGNAL_LOOKBACK_MINUTES + 10
    )

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        feed=STOCK_FEED,
    )

    bars = stock_client.get_stock_bars(
        request
    )

    try:

        data = bars[symbol]

    except Exception:

        return None

    if not data:
        return None

    closes = [
        safe_float(bar.close)
        for bar in data
    ]

    closes = [
        x for x in closes
        if x > 0
    ]

    if len(closes) < SIGNAL_LOOKBACK_MINUTES + 1:

        return None

    old_price = closes[
        -(SIGNAL_LOOKBACK_MINUTES + 1)
    ]

    current_price = closes[-1]

    if old_price <= 0:
        return None

    move = (
        current_price - old_price
    ) / old_price

    log(
        f"{symbol} SIGNAL | "
        f"Move={move * 100:.3f}%"
    )

    if move >= MINIMUM_MOVE_PCT:

        return "CALL"

    if move <= -MINIMUM_MOVE_PCT:

        return "PUT"

    return None


# ============================================================
# SPX SIGNAL
# ============================================================

def get_spx_signal_from_chain():

    try:

        chain_request = OptionChainRequest(
            underlying_symbol="SPX",
            feed=OPTIONS_FEED,
        )

        chain = option_client.get_option_chain(
            chain_request
        )

        if not chain:
            return None

        # Use ATM-ish call/put quotes as a proxy for
        # current SPX direction when direct index bars
        # are not available through the SDK path.

        contracts = []

        for symbol, snapshot in chain.items():

            quote = getattr(
                snapshot,
                "latest_quote",
                None
            )

            if quote is None:
                continue

            bid = safe_float(
                getattr(
                    quote,
                    "bid_price",
                    0
                )
            )

            ask = safe_float(
                getattr(
                    quote,
                    "ask_price",
                    0
                )
            )

            if bid <= 0 or ask <= 0:
                continue

            mid = (bid + ask) / 2

            contracts.append(
                (
                    symbol,
                    mid
                )
            )

        if not contracts:
            return None

        # We intentionally do not manufacture an SPX
        # directional signal from option premium movement.
        #
        # Therefore, SPX is disabled as a signal source in
        # this conservative V3.2 unless a valid direct signal
        # source is supplied.

        log(
            "SPX SIGNAL: direct index signal unavailable "
            "through the standard stock-data path."
        )

        return None

    except Exception as e:

        log(
            f"SPX SIGNAL ERROR: {e}"
        )

        return None


# ============================================================
# GET OPTION CONTRACTS
# ============================================================

def get_option_contracts(
    underlying,
    contract_type
):

    today = datetime.now(NY).date()

    expiration_start = (
        today + timedelta(days=MIN_DTE)
    )

    expiration_end = (
        today + timedelta(days=MAX_DTE)
    )

    request = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=expiration_start,
        expiration_date_lte=expiration_end,
        type=contract_type,
        limit=1000,
    )

    response = trading_client.get_option_contracts(
        request
    )

    return list(response.option_contracts)


# ============================================================
# OPTION CHAIN
# ============================================================

def get_option_chain(
    underlying,
    expiration_gte=None,
    expiration_lte=None
):

    request = OptionChainRequest(
        underlying_symbol=underlying,
        feed=OPTIONS_FEED,
    )

    if expiration_gte is not None:
        request.expiration_date_gte = expiration_gte

    if expiration_lte is not None:
        request.expiration_date_lte = expiration_lte

    return option_client.get_option_chain(
        request
    )


# ============================================================
# OPTION QUOTE
# ============================================================

def get_option_quote(symbol):

    request = OptionLatestQuoteRequest(
        symbol_or_symbols=symbol,
        feed=OPTIONS_FEED,
    )

    quotes = option_client.get_option_latest_quote(
        request
    )

    return quotes.get(symbol)


# ============================================================
# CONTRACT SELECTION
# ============================================================

def select_option_contract(
    underlying,
    signal
):

    if signal == "CALL":

        contract_type = ContractType.CALL

    elif signal == "PUT":

        contract_type = ContractType.PUT

    else:

        return None

    try:

        contracts = get_option_contracts(
            underlying,
            contract_type
        )

    except Exception as e:

        log(
            f"{underlying} CONTRACT ERROR: {e}"
        )

        return None

    if not contracts:

        log(
            f"{underlying}: no active contracts found."
        )

        return None

    try:

        chain = get_option_chain(
            underlying
        )

    except Exception as e:

        log(
            f"{underlying} CHAIN ERROR: {e}"
        )

        return None

    if not chain:

        return None

    # --------------------------------------------------------
    # Determine approximate underlying price
    # --------------------------------------------------------

    underlying_price = None

    if underlying == "SPY":

        underlying_price = get_stock_price(
            "SPY"
        )

    elif underlying == "SPX":

        # Try to derive ATM from option chain strikes.
        underlying_price = None

    # --------------------------------------------------------
    # Build candidates
    # --------------------------------------------------------

    candidates = []

    for contract in contracts:

        symbol = getattr(
            contract,
            "symbol",
            None
        )

        if not symbol:
            continue

        expiration = getattr(
            contract,
            "expiration_date",
            None
        )

        strike = safe_float(
            getattr(
                contract,
                "strike_price",
                0
            )
        )

        if not expiration or strike <= 0:
            continue

        today = datetime.now(NY).date()

        if hasattr(expiration, "date"):
            expiration_date = expiration.date()
        else:
            expiration_date = expiration

        dte = (
            expiration_date - today
        ).days

        if dte < MIN_DTE:
            continue

        if dte > MAX_DTE:
            continue

        snapshot = chain.get(symbol)

        if snapshot is None:
            continue

        greeks = getattr(
            snapshot,
            "greeks",
            None
        )

        if greeks is None:
            continue

        delta = safe_float(
            getattr(
                greeks,
                "delta",
                None
            ),
            default=0
        )

        # For puts Alpaca delta is normally negative.
        # We compare absolute delta.
        abs_delta = abs(delta)

        if (
            abs_delta < MIN_DELTA
            or abs_delta > MAX_DELTA
        ):
            continue

        quote = getattr(
            snapshot,
            "latest_quote",
            None
        )

        if quote is None:
            continue

        bid = safe_float(
            getattr(
                quote,
                "bid_price",
                0
            )
        )

        ask = safe_float(
            getattr(
                quote,
                "ask_price",
                0
            )
        )

        if bid <= 0 or ask <= 0:
            continue

        if ask < bid:
            continue

        mid = (
            bid + ask
        ) / 2

        if mid <= 0:
            continue

        if mid > MAX_PREMIUM:
            continue

        spread_pct = (
            ask - bid
        ) / mid

        if spread_pct > MAX_SPREAD_PCT:
            continue

        open_interest = safe_float(
            getattr(
                contract,
                "open_interest",
                0
            )
        )

        if open_interest < MIN_OPEN_INTEREST:

            # Some API responses may not provide
            # OI in the contract master.
            #
            # We keep strict filtering here.
            continue

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = 0.0

        # Delta closer to 0.50 is preferred.
        score += (
            1.0
            - abs(abs_delta - 0.50)
        ) * 5.0

        # Spread
        score += (
            1.0
            - min(spread_pct, 1.0)
        ) * 3.0

        # Open interest
        score += min(
            open_interest / 1000.0,
            2.0
        )

        # ATM preference
        if (
            PREFER_ATM
            and underlying_price
            and underlying_price > 0
        ):

            distance = abs(
                strike - underlying_price
            ) / underlying_price

            score += max(
                0,
                3.0 - distance * 100
            )

        # Lower DTE is preferred but not dominant.
        score += max(
            0,
            1.5 - dte * 0.05
        )

        candidates.append(
            {
                "symbol": symbol,
                "strike": strike,
                "expiration": str(
                    expiration_date
                ),
                "dte": dte,
                "delta": delta,
                "abs_delta": abs_delta,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_pct": spread_pct,
                "open_interest": open_interest,
                "score": score,
            }
        )

    if not candidates:

        log(
            f"{underlying}: "
            f"no contract passed filters."
        )

        return None

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    selected = candidates[0]

    log(
        f"SELECTED {underlying} | "
        f"{selected['symbol']} | "
        f"Strike={selected['strike']} | "
        f"DTE={selected['dte']} | "
        f"Delta={selected['delta']:.3f} | "
        f"Bid={selected['bid']:.2f} | "
        f"Ask={selected['ask']:.2f} | "
        f"Spread={selected['spread_pct'] * 100:.2f}% | "
        f"OI={selected['open_interest']:.0f} | "
        f"Score={selected['score']:.2f}"
    )

    return selected


# ============================================================
# GET OPEN OPTION POSITIONS
# ============================================================

def get_option_positions():

    positions = trading_client.get_all_positions()

    result = []

    for position in positions:

        asset_class = str(
            getattr(
                position,
                "asset_class",
                ""
            )
        ).lower()

        symbol = getattr(
            position,
            "symbol",
            ""
        )

        # Use actual asset class rather than
        # symbol length.
        if "option" not in asset_class:
            continue

        qty = safe_float(
            getattr(
                position,
                "qty",
                0
            )
        )

        if qty == 0:
            continue

        result.append(position)

    return result


# ============================================================
# POSITION COUNT
# ============================================================

def get_open_position_count():

    return len(
        get_option_positions()
    )


# ============================================================
# UNDERLYING FROM OPTION SYMBOL
# ============================================================

def get_position_underlying(symbol):

    if not symbol:
        return None

    # SPX options generally start with SPX.
    if symbol.startswith("SPX"):
        return "SPX"

    if symbol.startswith("SPY"):
        return "SPY"

    return None


# ============================================================
# EXISTING POSITION CHECK
# ============================================================

def has_position_for_underlying(
    underlying
):

    positions = get_option_positions()

    for position in positions:

        symbol = getattr(
            position,
            "symbol",
            ""
        )

        pos_underlying = (
            get_position_underlying(symbol)
        )

        if pos_underlying == underlying:

            return True

    return False


# ============================================================
# CONTRACT QUANTITY
# ============================================================

def calculate_contract_quantity(
    premium
):

    if premium <= 0:
        return 0

    equity = get_equity()

    buying_power = get_buying_power()

    if equity <= 0:
        return 0

    if buying_power <= 0:
        return 0

    # This is capital allocation, not true
    # stop-loss risk.
    budget = (
        equity
        * RISK_PER_TRADE_PCT
    )

    contract_cost = (
        premium * 100
    )

    if contract_cost <= 0:
        return 0

    qty_by_budget = math.floor(
        budget / contract_cost
    )

    qty_by_bp = math.floor(
        buying_power / contract_cost
    )

    qty = min(
        qty_by_budget,
        qty_by_bp,
        MAX_CONTRACTS_PER_TRADE
    )

    return max(
        0,
        qty
    )


# ============================================================
# SUBMIT ENTRY
# ============================================================

def execute_trade(
    underlying,
    selected,
    signal
):

    global trades_today

    symbol = selected["symbol"]

    premium = selected["mid"]

    qty = calculate_contract_quantity(
        premium
    )

    if qty <= 0:

        log(
            f"{underlying}: "
            f"quantity=0 | "
            f"Premium=${premium:.2f}"
        )

        return False

    if trades_today >= MAX_TRADES_PER_DAY:

        log(
            "MAX DAILY TRADES REACHED."
        )

        return False

    if get_open_position_count() >= MAX_OPEN_POSITIONS:

        log(
            "MAX OPEN POSITIONS REACHED."
        )

        return False

    if daily_loss_limit_reached():

        return False

    log(
        f"ENTRY SIGNAL | "
        f"{underlying} | "
        f"{signal} | "
        f"{symbol} | "
        f"Qty={qty} | "
        f"Estimated Premium=${premium:.2f}"
    )

    try:

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        order = trading_client.submit_order(
            order_data=order_request
        )

        order_id = str(
            getattr(
                order,
                "id",
                ""
            )
        )

        log(
            f"ORDER SUBMITTED | "
            f"{symbol} | "
            f"Qty={qty} | "
            f"OrderID={order_id}"
        )

        trades_today += 1

        # Approximate state.
        # We update the exact entry after the order
        # can be observed from the broker.
        position_state[symbol] = {
            "underlying": underlying,
            "entry_price": premium,
            "highest_price": premium,
            "qty": qty,
            "order_id": order_id,
            "created_at": datetime.now(
                NY
            ).isoformat(),
        }

        save_state()

        return True

    except Exception as e:

        log(
            f"ENTRY ORDER ERROR | "
            f"{symbol} | {e}"
        )

        return False


# ============================================================
# CURRENT OPTION MID
# ============================================================

def get_option_mid(symbol):

    try:

        quote = get_option_quote(
            symbol
        )

        if quote is None:
            return None

        bid = safe_float(
            getattr(
                quote,
                "bid_price",
                0
            )
        )

        ask = safe_float(
            getattr(
                quote,
                "ask_price",
                0
            )
        )

        if bid <= 0 or ask <= 0:
            return None

        if ask < bid:
            return None

        return (
            bid + ask
        ) / 2

    except Exception as e:

        log(
            f"QUOTE ERROR {symbol}: {e}"
        )

        return None


# ============================================================
# GET ACTUAL POSITION ENTRY
# ============================================================

def get_actual_position_entry(
    position
):

    avg_entry = safe_float(
        getattr(
            position,
            "avg_entry_price",
            0
        )
    )

    if avg_entry > 0:
        return avg_entry

    symbol = getattr(
        position,
        "symbol",
        ""
    )

    state = position_state.get(
        symbol
    )

    if state:

        return safe_float(
            state.get(
                "entry_price",
                0
            )
        )

    return 0.0


# ============================================================
# REBUILD POSITION STATE
# ============================================================

def initialize_position_state():

    global position_state

    positions = get_option_positions()

    active_symbols = set()

    for position in positions:

        symbol = getattr(
            position,
            "symbol",
            ""
        )

        if not symbol:
            continue

        active_symbols.add(symbol)

        current_mid = get_option_mid(
            symbol
        )

        entry = get_actual_position_entry(
            position
        )

        existing = position_state.get(
            symbol,
            {}
        )

        highest = safe_float(
            existing.get(
                "highest_price",
                0
            )
        )

        if current_mid and current_mid > highest:
            highest = current_mid

        if highest <= 0:
            highest = (
                current_mid
                if current_mid
                else entry
            )

        position_state[symbol] = {
            "underlying":
                get_position_underlying(symbol),

            "entry_price":
                entry,

            "highest_price":
                highest,

            "qty":
                safe_float(
                    getattr(
                        position,
                        "qty",
                        0
                    )
                ),

            "restored":
                True,
        }

    # Remove state for positions no longer open.
    stale_symbols = [
        symbol
        for symbol in position_state
        if symbol not in active_symbols
    ]

    for symbol in stale_symbols:

        del position_state[symbol]

    save_state()

    log(
        f"POSITION STATE RESTORED | "
        f"Open positions={len(positions)}"
    )


# ============================================================
# EXIT POSITION
# ============================================================

def close_position(
    position,
    reason
):

    global daily_realized_pnl

    symbol = getattr(
        position,
        "symbol",
        ""
    )

    qty = safe_float(
        getattr(
            position,
            "qty",
            0
        )
    )

    if not symbol or qty <= 0:
        return False

    entry_price = get_actual_position_entry(
        position
    )

    current_mid = get_option_mid(
        symbol
    )

    log(
        f"EXIT SIGNAL | "
        f"{symbol} | "
        f"Reason={reason} | "
        f"Qty={qty} | "
        f"Entry=${entry_price:.2f} | "
        f"Current=${current_mid if current_mid else 0:.2f}"
    )

    try:

        order = trading_client.close_position(
            symbol
        )

        log(
            f"POSITION CLOSE SUBMITTED | "
            f"{symbol} | "
            f"OrderID={getattr(order, 'id', '')}"
        )

        # ----------------------------------------------------
        # Approximate realized P&L.
        #
        # We DO NOT treat this as final fill P&L.
        # Final P&L should be reconciled from broker fills.
        # ----------------------------------------------------

        if current_mid and entry_price > 0:

            estimated_pnl = (
                current_mid - entry_price
            ) * qty * 100

            daily_realized_pnl += (
                estimated_pnl
            )

            log(
                f"ESTIMATED EXIT P&L | "
                f"{symbol} | "
                f"${estimated_pnl:.2f}"
            )

        if symbol in position_state:

            del position_state[symbol]

        save_state()

        return True

    except Exception as e:

        log(
            f"EXIT ERROR | "
            f"{symbol} | {e}"
        )

        return False


# ============================================================
# MANAGE POSITIONS
# ============================================================

def manage_positions():

    positions = get_option_positions()

    if not positions:
        return

    for position in positions:

        symbol = getattr(
            position,
            "symbol",
            ""
        )

        if not symbol:
            continue

        entry_price = get_actual_position_entry(
            position
        )

        if entry_price <= 0:

            log(
                f"{symbol}: "
                f"invalid entry price."
            )

            continue

        current_price = get_option_mid(
            symbol
        )

        if current_price is None:
            continue

        state = position_state.setdefault(
            symbol,
            {
                "underlying":
                    get_position_underlying(symbol),

                "entry_price":
                    entry_price,

                "highest_price":
                    entry_price,

                "qty":
                    safe_float(
                        getattr(
                            position,
                            "qty",
                            0
                        )
                    ),
            }
        )

        # Always trust broker entry when available.
        state["entry_price"] = entry_price

        highest_price = safe_float(
            state.get(
                "highest_price",
                entry_price
            )
        )

        if current_price > highest_price:

            highest_price = current_price

            state["highest_price"] = (
                highest_price
            )

        pnl_pct = (
            current_price - entry_price
        ) / entry_price

        # ----------------------------------------------------
        # STOP LOSS
        # ----------------------------------------------------

        if pnl_pct <= -STOP_LOSS_PCT:

            close_position(
                position,
                "STOP_LOSS"
            )

            continue

        # ----------------------------------------------------
        # TAKE PROFIT
        # ----------------------------------------------------

        if pnl_pct >= TAKE_PROFIT_PCT:

            close_position(
                position,
                "TAKE_PROFIT"
            )

            continue

        # ----------------------------------------------------
        # TRAILING STOP
        # ----------------------------------------------------

        if TRAILING_ENABLED:

            if highest_price > entry_price:

                trailing_stop = (
                    highest_price
                    * (1 - TRAILING_PCT)
                )

                if current_price <= trailing_stop:

                    close_position(
                        position,
                        "TRAILING_STOP"
                    )

                    continue

        log(
            f"MANAGE | "
            f"{symbol} | "
            f"Entry=${entry_price:.2f} | "
            f"Current=${current_price:.2f} | "
            f"PnL={pnl_pct * 100:.2f}% | "
            f"High=${highest_price:.2f}"
        )

    save_state()


# ============================================================
# PROCESS UNDERLYING
# ============================================================

def process_underlying(
    underlying
):

    if trades_today >= MAX_TRADES_PER_DAY:

        log(
            "Daily trade limit reached."
        )

        return

    if daily_loss_limit_reached():

        return

    if has_position_for_underlying(
        underlying
    ):

        log(
            f"{underlying}: existing position."
        )

        return

    if get_open_position_count() >= MAX_OPEN_POSITIONS:

        log(
            "Maximum open positions reached."
        )

        return

    signal = get_signal(
        underlying
    )

    if signal is None:

        log(
            f"{underlying}: no signal."
        )

        return

    log(
        f"{underlying}: SIGNAL={signal}"
    )

    selected = select_option_contract(
        underlying,
        signal
    )

    if selected is None:

        return

    execute_trade(
        underlying,
        selected,
        signal
    )


# ============================================================
# BROKER STATUS
# ============================================================

def check_account_status():

    try:

        account = get_account()

        status = str(
            getattr(
                account,
                "status",
                ""
            )
        )

        equity = safe_float(
            getattr(
                account,
                "equity",
                0
            )
        )

        buying_power = safe_float(
            getattr(
                account,
                "buying_power",
                0
            )
        )

        log(
            f"ACCOUNT | "
            f"Status={status} | "
            f"Equity=${equity:.2f} | "
            f"BuyingPower=${buying_power:.2f}"
        )

        return True

    except Exception as e:

        log(
            f"ACCOUNT ERROR: {e}"
        )

        return False


# ============================================================
# STARTUP
# ============================================================

def startup():

    print()
    print("=" * 70)
    print(BOT_NAME)
    print("=" * 70)
    print("MODE: PAPER TRADING ONLY")
    print("UNDERLYINGS: SPY + SPX")
    print("OPTIONS FEED:", OPTIONS_FEED)
    print("STOCK FEED:", STOCK_FEED)
    print("=" * 70)
    print()

    load_state()

    if not check_account_status():

        print(
            "Unable to access Alpaca account."
        )

        sys.exit(1)

    initialize_position_state()

    log(
        f"Today's trades: {trades_today}"
    )

    log(
        f"Today's estimated realized P&L: "
        f"${daily_realized_pnl:.2f}"
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    startup()

    while True:

        try:

            reset_daily_state_if_needed()

            if not market_is_open():

                log(
                    "Market closed. "
                    "Waiting..."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # Manage existing positions FIRST.
            # ------------------------------------------------

            manage_positions()

            # ------------------------------------------------
            # Check daily loss.
            # ------------------------------------------------

            if daily_loss_limit_reached():

                log(
                    "Trading disabled for today "
                    "because daily loss limit was reached."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # Account status
            # ------------------------------------------------

            check_account_status()

            # ------------------------------------------------
            # New entries
            # ------------------------------------------------

            for underlying in UNDERLYINGS:

                if trades_today >= MAX_TRADES_PER_DAY:

                    break

                if get_open_position_count() >= MAX_OPEN_POSITIONS:

                    break

                process_underlying(
                    underlying
                )

                time.sleep(
                    SYMBOL_DELAY_SECONDS
                )

            save_state()

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            log(
                "Bot stopped manually."
            )

            save_state()

            break

        except Exception as e:

            log(
                f"MAIN LOOP ERROR: {e}"
            )

            save_state()

            time.sleep(
                ERROR_SLEEP_SECONDS
            )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()