# ============================================================
# ALPACA OPTIONS BOT V3.3
# ============================================================
# PAPER TRADING ONLY
#
# Supports:
#   - SPY options
#   - SPX options
#   - SPXW weekly options (under SPX)
#
# Market data:
#   - INDICATIVE
#   - NO OPRA required
#
# IMPORTANT:
#   SPX spot index data is not used.
#   SPX signal uses SPY movement as a proxy.
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

from alpaca.data.historical.stock import (
    StockHistoricalDataClient,
)

from alpaca.data.historical.option import (
    OptionHistoricalDataClient,
)

from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)

from alpaca.data.timeframe import (
    TimeFrame,
)

from alpaca.data.enums import (
    OptionsFeed,
    DataFeed,
)


# ============================================================
# 1. API
# ============================================================

API_KEY = (
    os.getenv("APCA_API_KEY_ID")
    or os.getenv("API_KEY")
)

SECRET_KEY = (
    os.getenv("APCA_API_SECRET_KEY")
    or os.getenv("SECRET_KEY")
)

if not API_KEY or not SECRET_KEY:

    print("ERROR: API credentials not found.")

    sys.exit(1)


# ============================================================
# 2. HARD PAPER-ONLY LOCK
# ============================================================

PAPER_MODE = True

if PAPER_MODE is not True:

    print(
        "FATAL ERROR: "
        "This bot is PAPER TRADING ONLY."
    )

    sys.exit(1)


# ============================================================
# 3. DATA FEEDS
# ============================================================

# IMPORTANT:
# INDICATIVE does NOT require OPRA subscription.

OPTIONS_FEED = OptionsFeed.INDICATIVE

# IEX is available on Basic stock data.

STOCK_FEED = DataFeed.IEX


# ============================================================
# 4. TIMEZONE
# ============================================================

NY = ZoneInfo(
    "America/New_York"
)


# ============================================================
# 5. BOT SETTINGS
# ============================================================

BOT_NAME = (
    "ALPACA_OPTIONS_BOT_V3.3"
)


# Only SPY and SPX are underlyings.
#
# SPXW is NOT a separate underlying.
#
UNDERLYINGS = [
    "SPY",
    "SPX",
]


# ============================================================
# 6. SIGNAL SETTINGS
# ============================================================

SIGNAL_LOOKBACK_MINUTES = 5

# 0.10%
MINIMUM_MOVE_PCT = 0.0010


# ============================================================
# 7. OPTION SETTINGS
# ============================================================

MIN_DTE = 1

MAX_DTE = 14

MIN_DELTA = 0.40

MAX_DELTA = 0.60

# Maximum premium per share.
#
# $10 premium = approximately $1,000
# per contract because options = x100.
#
MAX_PREMIUM_SPY = 15.00

# SPX contracts can be much more expensive.
# Keep this configurable separately.
#
MAX_PREMIUM_SPX = 15.00

MAX_SPREAD_PCT = 0.10

MIN_OPEN_INTEREST = 100


# ============================================================
# 8. RISK SETTINGS
# ============================================================

# IMPORTANT:
# This is CAPITAL ALLOCATION,
# not actual stop-loss risk.
#
RISK_PER_TRADE_PCT = 0.01

MAX_DAILY_LOSS_PCT = 0.03

MAX_TRADES_PER_DAY = 5

MAX_OPEN_POSITIONS = 2

MAX_CONTRACTS_PER_TRADE = 5


# ============================================================
# 9. EXIT SETTINGS
# ============================================================

STOP_LOSS_PCT = 0.30

TAKE_PROFIT_PCT = 0.60

TRAILING_ENABLED = True

TRAILING_PCT = 0.15


# ============================================================
# 10. LOOP SETTINGS
# ============================================================

SCAN_INTERVAL_SECONDS = 60

ERROR_SLEEP_SECONDS = 30

SYMBOL_DELAY_SECONDS = 2


# ============================================================
# 11. STATE FILE
# ============================================================

STATE_FILE = Path(
    "bot_state.json"
)


# ============================================================
# 12. GLOBAL STATE
# ============================================================

trades_today = 0

daily_realized_pnl = 0.0

state_date = (
    datetime.now(NY)
    .date()
    .isoformat()
)

position_state = {}


# ============================================================
# 13. CLIENTS
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
# 14. LOGGING
# ============================================================

def log(message):

    now = datetime.now(
        NY
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[{now}] {message}",
        flush=True
    )


# ============================================================
# 15. SAFE FLOAT
# ============================================================

def safe_float(
    value,
    default=0.0
):

    try:

        if value is None:
            return default

        return float(value)

    except Exception:

        return default


# ============================================================
# 16. SAVE STATE
# ============================================================

def save_state():

    global trades_today
    global daily_realized_pnl
    global state_date
    global position_state

    data = {

        "date": state_date,

        "trades_today":
            trades_today,

        "daily_realized_pnl":
            daily_realized_pnl,

        "position_state":
            position_state,

    }

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                indent=2
            )

    except Exception as e:

        log(
            f"STATE SAVE ERROR: {e}"
        )


# ============================================================
# 17. LOAD STATE
# ============================================================

def load_state():

    global trades_today
    global daily_realized_pnl
    global state_date
    global position_state

    today = (
        datetime.now(NY)
        .date()
        .isoformat()
    )

    if not STATE_FILE.exists():

        state_date = today

        trades_today = 0

        daily_realized_pnl = 0.0

        position_state = {}

        save_state()

        return

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        saved_date = data.get(
            "date"
        )

        if saved_date == today:

            state_date = saved_date

            trades_today = int(
                data.get(
                    "trades_today",
                    0
                )
            )

            daily_realized_pnl = safe_float(
                data.get(
                    "daily_realized_pnl",
                    0
                )
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

        log(
            f"STATE LOAD ERROR: {e}"
        )

        state_date = today

        trades_today = 0

        daily_realized_pnl = 0.0

        position_state = {}


# ============================================================
# 18. DAILY RESET
# ============================================================

def reset_daily_state_if_needed():

    global state_date
    global trades_today
    global daily_realized_pnl

    today = (
        datetime.now(NY)
        .date()
        .isoformat()
    )

    if state_date != today:

        log(
            "NEW TRADING DAY"
        )

        state_date = today

        trades_today = 0

        daily_realized_pnl = 0.0

        save_state()


# ============================================================
# 19. ACCOUNT
# ============================================================

def get_account():

    return (
        trading_client
        .get_account()
    )


# ============================================================
# 20. EQUITY
# ============================================================

def get_equity():

    account = get_account()

    return safe_float(
        getattr(
            account,
            "equity",
            0
        )
    )


# ============================================================
# 21. BUYING POWER
# ============================================================

def get_buying_power():

    account = get_account()

    return safe_float(
        getattr(
            account,
            "buying_power",
            0
        )
    )


# ============================================================
# 22. DAILY LOSS LIMIT
# ============================================================

def daily_loss_limit_reached():

    equity = get_equity()

    if equity <= 0:

        return True

    max_loss = (
        equity
        * MAX_DAILY_LOSS_PCT
    )

    if daily_realized_pnl <= -max_loss:

        log(
            "DAILY LOSS LIMIT HIT | "
            f"PnL=${daily_realized_pnl:.2f} | "
            f"Limit=-${max_loss:.2f}"
        )

        return True

    return False


# ============================================================
# 23. MARKET OPEN
# ============================================================

def market_is_open():

    now = datetime.now(NY)

    if now.weekday() >= 5:

        return False

    current = now.time()

    return (
        current >= datetime.strptime(
            "09:30",
            "%H:%M"
        ).time()
        and
        current < datetime.strptime(
            "16:00",
            "%H:%M"
        ).time()
    )


# ============================================================
# 24. SPY CURRENT PRICE
# ============================================================

def get_spy_price():

    try:

        request = (
            StockLatestQuoteRequest(
                symbol_or_symbols=[
                    "SPY"
                ],
                feed=STOCK_FEED
            )
        )

        quotes = (
            stock_client
            .get_stock_latest_quote(
                request
            )
        )

        quote = quotes.get(
            "SPY"
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

        if bid > 0 and ask > 0:

            return (
                bid + ask
            ) / 2

        if ask > 0:
            return ask

        if bid > 0:
            return bid

        return None

    except Exception as e:

        log(
            f"SPY PRICE ERROR: {e}"
        )

        return None


# ============================================================
# 25. SPY SIGNAL
# ============================================================

def get_spy_signal():

    try:

        start = (
            datetime.now(NY)
            - timedelta(
                minutes=
                SIGNAL_LOOKBACK_MINUTES + 10
            )
        )

        request = StockBarsRequest(
            symbol_or_symbols=[
                "SPY"
            ],
            timeframe=TimeFrame.Minute,
            start=start,
            feed=STOCK_FEED,
        )

        bars = (
            stock_client
            .get_stock_bars(
                request
            )
        )

        data = bars.get(
            "SPY"
        )

        if not data:

            return None

        closes = []

        for bar in data:

            price = safe_float(
                getattr(
                    bar,
                    "close",
                    0
                )
            )

            if price > 0:

                closes.append(
                    price
                )

        needed = (
            SIGNAL_LOOKBACK_MINUTES
            + 1
        )

        if len(closes) < needed:

            return None

        old_price = closes[
            -needed
        ]

        current_price = closes[
            -1
        ]

        if old_price <= 0:

            return None

        move = (
            current_price
            - old_price
        ) / old_price

        log(
            "SPY SIGNAL | "
            f"5m Move={move * 100:.3f}%"
        )

        if (
            move
            >= MINIMUM_MOVE_PCT
        ):

            return "CALL"

        if (
            move
            <= -MINIMUM_MOVE_PCT
        ):

            return "PUT"

        return None

    except Exception as e:

        log(
            f"SPY SIGNAL ERROR: {e}"
        )

        return None


# ============================================================
# 26. SIGNAL FOR UNDERLYING
# ============================================================

def get_signal(
    underlying
):

    # --------------------------------------------------------
    # SPY
    # --------------------------------------------------------

    if underlying == "SPY":

        return get_spy_signal()

    # --------------------------------------------------------
    # SPX
    #
    # Alpaca does NOT provide SPX spot market data
    # in the initial index-options offering.
    #
    # Therefore:
    # SPY movement = SPX signal proxy.
    # --------------------------------------------------------

    if underlying == "SPX":

        signal = get_spy_signal()

        if signal:

            log(
                "SPX SIGNAL | "
                f"Using SPY proxy = {signal}"
            )

        return signal

    return None


# ============================================================
# 27. GET OPTION CONTRACTS
# ============================================================

def get_option_contracts(
    underlying,
    contract_type
):

    today = (
        datetime.now(NY)
        .date()
    )

    expiration_start = (
        today
        + timedelta(
            days=MIN_DTE
        )
    )

    expiration_end = (
        today
        + timedelta(
            days=MAX_DTE
        )
    )

    request = (
        GetOptionContractsRequest(
            underlying_symbols=[
                underlying
            ],

            status=AssetStatus.ACTIVE,

            expiration_date_gte=
                expiration_start,

            expiration_date_lte=
                expiration_end,

            type=contract_type,

            limit=1000,
        )
    )

    response = (
        trading_client
        .get_option_contracts(
            request
        )
    )

    return list(
        response.option_contracts
    )


# ============================================================
# 28. OPTION CHAIN
# ============================================================

def get_option_chain(
    underlying
):

    request = OptionChainRequest(
        underlying_symbol=underlying,

        feed=OPTIONS_FEED,

        limit=1000,
    )

    return (
        option_client
        .get_option_chain(
            request
        )
    )


# ============================================================
# 29. OPTION QUOTE
# ============================================================

def get_option_quote(
    symbol
):

    request = (
        OptionLatestQuoteRequest(
            symbol_or_symbols=[
                symbol
            ],

            feed=OPTIONS_FEED
        )
    )

    quotes = (
        option_client
        .get_option_latest_quote(
            request
        )
    )

    return quotes.get(
        symbol
    )


# ============================================================
# 30. SELECT OPTION
# ============================================================

def select_option_contract(
    underlying,
    signal
):

    if signal == "CALL":

        contract_type = (
            ContractType.CALL
        )

    elif signal == "PUT":

        contract_type = (
            ContractType.PUT
        )

    else:

        return None


    # --------------------------------------------------------
    # Maximum premium depends on underlying
    # --------------------------------------------------------

    if underlying == "SPX":

        max_premium = (
            MAX_PREMIUM_SPX
        )

    else:

        max_premium = (
            MAX_PREMIUM_SPY
        )


    # --------------------------------------------------------
    # Contract master
    # --------------------------------------------------------

    try:

        contracts = (
            get_option_contracts(
                underlying,
                contract_type
            )
        )

    except Exception as e:

        log(
            f"{underlying} "
            f"CONTRACT ERROR: {e}"
        )

        return None


    if not contracts:

        log(
            f"{underlying}: "
            "No contracts returned."
        )

        return None


    # --------------------------------------------------------
    # Option chain
    # --------------------------------------------------------

    try:

        chain = (
            get_option_chain(
                underlying
            )
        )

    except Exception as e:

        log(
            f"{underlying} "
            f"CHAIN ERROR: {e}"
        )

        return None


    if not chain:

        log(
            f"{underlying}: "
            "Empty option chain."
        )

        return None


    # --------------------------------------------------------
    # Current underlying reference
    #
    # SPY = actual price
    #
    # SPX = approximate ATM reference from
    # available strikes because SPX spot itself
    # is not supplied by Alpaca.
    # --------------------------------------------------------

    underlying_price = None

    if underlying == "SPY":

        underlying_price = (
            get_spy_price()
        )

    elif underlying == "SPX":

        # We do not invent a spot SPX price.
        # ATM preference is therefore disabled
        # for SPX.
        underlying_price = None


    candidates = []


    # ========================================================
    # LOOP CONTRACTS
    # ========================================================

    today = (
        datetime.now(NY)
        .date()
    )

    for contract in contracts:

        symbol = getattr(
            contract,
            "symbol",
            None
        )

        if not symbol:

            continue


        # ----------------------------------------------------
        # Ensure this is actually the requested underlying
        # ----------------------------------------------------

        contract_underlying = (
            getattr(
                contract,
                "underlying_symbol",
                None
            )
        )

        if (
            contract_underlying
            and
            contract_underlying
            != underlying
        ):

            continue


        # ----------------------------------------------------
        # Expiration
        # ----------------------------------------------------

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

            expiration_date = (
                expiration.date()
            )

        else:

            expiration_date = (
                expiration
            )


        dte = (
            expiration_date
            - today
        ).days


        if dte < MIN_DTE:

            continue

        if dte > MAX_DTE:

            continue


        # ----------------------------------------------------
        # Strike
        # ----------------------------------------------------

        strike = safe_float(
            getattr(
                contract,
                "strike_price",
                0
            )
        )

        if strike <= 0:

            continue


        # ----------------------------------------------------
        # Option chain snapshot
        # ----------------------------------------------------

        snapshot = chain.get(
            symbol
        )

        if snapshot is None:

            continue


        # ----------------------------------------------------
        # Greeks
        # ----------------------------------------------------

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

        abs_delta = abs(
            delta
        )


        if (
            abs_delta
            < MIN_DELTA
        ):

            continue

        if (
            abs_delta
            > MAX_DELTA
        ):

            continue


        # ----------------------------------------------------
        # Quote
        # ----------------------------------------------------

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


        if bid <= 0:

            continue

        if ask <= 0:

            continue

        if ask < bid:

            continue


        mid = (
            bid + ask
        ) / 2


        if mid <= 0:

            continue


        # ----------------------------------------------------
        # Premium filter
        # ----------------------------------------------------

        if mid > max_premium:

            continue


        # ----------------------------------------------------
        # Spread
        # ----------------------------------------------------

        spread_pct = (
            ask - bid
        ) / mid


        if (
            spread_pct
            > MAX_SPREAD_PCT
        ):

            continue


        # ----------------------------------------------------
        # Open Interest
        # ----------------------------------------------------

        open_interest = safe_float(
            getattr(
                contract,
                "open_interest",
                0
            )
        )


        if (
            open_interest
            < MIN_OPEN_INTEREST
        ):

            continue


        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = 0.0


        # Delta closer to 0.50
        score += (
            1
            - abs(
                abs_delta
                - 0.50
            )
        ) * 5


        # Tight spread
        score += (
            1
            - min(
                spread_pct,
                1
            )
        ) * 3


        # OI
        score += min(
            open_interest / 1000,
            2
        )


        # ATM preference for SPY only
        if (
            underlying_price
            and underlying_price > 0
        ):

            distance = abs(
                strike
                - underlying_price
            ) / underlying_price

            score += max(
                0,
                3
                - distance * 100
            )


        # Prefer nearer DTE
        score += max(
            0,
            1.5
            - dte * 0.05
        )


        # ----------------------------------------------------
        # SPXW detection
        #
        # We do NOT treat SPXW as underlying.
        #
        # The API returns SPXW weeklies under SPX.
        #
        # We keep the actual contract symbol/root from
        # Alpaca instead of inventing a separate underlying.
        # ----------------------------------------------------

        root_symbol = str(
            getattr(
                contract,
                "root_symbol",
                ""
            )
        )


        candidates.append({

            "symbol":
                symbol,

            "underlying":
                underlying,

            "root_symbol":
                root_symbol,

            "strike":
                strike,

            "expiration":
                str(
                    expiration_date
                ),

            "dte":
                dte,

            "delta":
                delta,

            "abs_delta":
                abs_delta,

            "bid":
                bid,

            "ask":
                ask,

            "mid":
                mid,

            "spread_pct":
                spread_pct,

            "open_interest":
                open_interest,

            "score":
                score,

        })


    # ========================================================
    # NO CANDIDATES
    # ========================================================

    if not candidates:

        log(
            f"{underlying}: "
            "No contract passed filters."
        )

        return None


    # ========================================================
    # SORT
    # ========================================================

    candidates.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )


    selected = candidates[0]


    # ========================================================
    # LOG SELECTION
    # ========================================================

    log(
        "SELECTED CONTRACT | "
        f"{underlying} | "
        f"{selected['symbol']} | "
        f"Strike={selected['strike']} | "
        f"DTE={selected['dte']} | "
        f"Delta={selected['delta']:.3f} | "
        f"Bid=${selected['bid']:.2f} | "
        f"Ask=${selected['ask']:.2f} | "
        f"Spread="
        f"{selected['spread_pct'] * 100:.2f}% | "
        f"OI={selected['open_interest']:.0f} | "
        f"Score={selected['score']:.2f}"
    )


    return selected


# ============================================================
# 31. GET OPEN OPTION POSITIONS
# ============================================================

def get_option_positions():

    try:

        positions = (
            trading_client
            .get_all_positions()
        )

    except Exception as e:

        log(
            f"POSITIONS ERROR: {e}"
        )

        return []


    result = []


    for position in positions:

        asset_class = str(
            getattr(
                position,
                "asset_class",
                ""
            )
        ).lower()


        if (
            "option"
            not in asset_class
        ):

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


        result.append(
            position
        )


    return result


# ============================================================
# 32. OPEN POSITION COUNT
# ============================================================

def get_open_position_count():

    return len(
        get_option_positions()
    )


# ============================================================
# 33. IDENTIFY UNDERLYING
# ============================================================

def get_position_underlying(
    symbol
):

    if not symbol:

        return None


    if symbol.startswith(
        "SPY"
    ):

        return "SPY"


    if symbol.startswith(
        "SPX"
    ):

        return "SPX"


    return None


# ============================================================
# 34. EXISTING POSITION
# ============================================================

def has_position_for_underlying(
    underlying
):

    positions = (
        get_option_positions()
    )


    for position in positions:

        symbol = getattr(
            position,
            "symbol",
            ""
        )


        pos_underlying = (
            get_position_underlying(
                symbol
            )
        )


        if (
            pos_underlying
            == underlying
        ):

            return True


    return False


# ============================================================
# 35. POSITION SIZE
# ============================================================

def calculate_contract_quantity(
    premium
):

    if premium <= 0:

        return 0


    equity = get_equity()

    buying_power = (
        get_buying_power()
    )


    if equity <= 0:

        return 0


    if buying_power <= 0:

        return 0


    # Capital allocation budget.
    budget = (
        equity
        * RISK_PER_TRADE_PCT
    )


    contract_cost = (
        premium
        * 100
    )


    if contract_cost <= 0:

        return 0


    qty_by_budget = math.floor(
        budget
        / contract_cost
    )


    qty_by_bp = math.floor(
        buying_power
        / contract_cost
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
# 36. EXECUTE ENTRY
# ============================================================

def execute_trade(
    underlying,
    selected,
    signal
):

    global trades_today


    symbol = (
        selected["symbol"]
    )

    premium = (
        selected["mid"]
    )


    # --------------------------------------------------------
    # Daily limit
    # --------------------------------------------------------

    if (
        trades_today
        >= MAX_TRADES_PER_DAY
    ):

        log(
            "MAX DAILY TRADES REACHED."
        )

        return False


    # --------------------------------------------------------
    # Position limit
    # --------------------------------------------------------

    if (
        get_open_position_count()
        >= MAX_OPEN_POSITIONS
    ):

        log(
            "MAX OPEN POSITIONS REACHED."
        )

        return False


    # --------------------------------------------------------
    # Daily loss
    # --------------------------------------------------------

    if daily_loss_limit_reached():

        return False


    # --------------------------------------------------------
    # Quantity
    # --------------------------------------------------------

    qty = (
        calculate_contract_quantity(
            premium
        )
    )


    if qty <= 0:

        log(
            f"{underlying}: "
            "quantity=0."
        )

        return False


    # --------------------------------------------------------
    # ENTRY
    # --------------------------------------------------------

    log(
        "ENTRY | "
        f"{underlying} | "
        f"{signal} | "
        f"{symbol} | "
        f"Qty={qty} | "
        f"Mid=${premium:.2f}"
    )


    try:

        order_request = (
            MarketOrderRequest(

                symbol=symbol,

                qty=qty,

                side=OrderSide.BUY,

                time_in_force=
                    TimeInForce.DAY,
            )
        )


        order = (
            trading_client
            .submit_order(
                order_data=
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


        log(
            "ORDER SUBMITTED | "
            f"{symbol} | "
            f"Qty={qty} | "
            f"OrderID={order_id}"
        )


        trades_today += 1


        # Save approximate initial state.
        position_state[symbol] = {

            "underlying":
                underlying,

            "entry_price":
                premium,

            "highest_price":
                premium,

            "qty":
                qty,

            "order_id":
                order_id,

            "created_at":
                datetime.now(
                    NY
                ).isoformat(),

        }


        save_state()


        return True


    except Exception as e:

        log(
            "ENTRY ERROR | "
            f"{symbol} | "
            f"{e}"
        )

        return False


# ============================================================
# 37. CURRENT OPTION MID
# ============================================================

def get_option_mid(
    symbol
):

    try:

        quote = (
            get_option_quote(
                symbol
            )
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


        if bid <= 0:

            return None


        if ask <= 0:

            return None


        if ask < bid:

            return None


        return (
            bid + ask
        ) / 2


    except Exception as e:

        log(
            f"QUOTE ERROR | "
            f"{symbol} | "
            f"{e}"
        )

        return None


# ============================================================
# 38. ACTUAL ENTRY
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


    state = (
        position_state
        .get(symbol)
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
# 39. RESTORE POSITIONS
# ============================================================

def initialize_position_state():

    global position_state


    positions = (
        get_option_positions()
    )


    active_symbols = set()


    for position in positions:

        symbol = getattr(
            position,
            "symbol",
            ""
        )


        if not symbol:

            continue


        active_symbols.add(
            symbol
        )


        entry = (
            get_actual_position_entry(
                position
            )
        )


        current_mid = (
            get_option_mid(
                symbol
            )
        )


        existing = (
            position_state
            .get(
                symbol,
                {}
            )
        )


        highest = safe_float(
            existing.get(
                "highest_price",
                0
            )
        )


        if (
            current_mid
            and
            current_mid > highest
        ):

            highest = (
                current_mid
            )


        if highest <= 0:

            highest = (
                current_mid
                if current_mid
                else entry
            )


        position_state[symbol] = {

            "underlying":
                get_position_underlying(
                    symbol
                ),

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


    # Remove stale state.
    stale = [

        symbol

        for symbol
        in position_state

        if symbol
        not in active_symbols

    ]


    for symbol in stale:

        del position_state[
            symbol
        ]


    save_state()


    log(
        "POSITION STATE RESTORED | "
        f"Open={len(positions)}"
    )


# ============================================================
# 40. CLOSE POSITION
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


    if not symbol:

        return False


    if qty <= 0:

        return False


    entry_price = (
        get_actual_position_entry(
            position
        )
    )


    current_mid = (
        get_option_mid(
            symbol
        )
    )


    log(
        "EXIT | "
        f"{symbol} | "
        f"Reason={reason} | "
        f"Qty={qty} | "
        f"Entry=${entry_price:.2f} | "
        f"Current="
        f"${current_mid if current_mid else 0:.2f}"
    )


    try:

        order = (
            trading_client
            .close_position(
                symbol
            )
        )


        log(
            "CLOSE ORDER SUBMITTED | "
            f"{symbol} | "
            f"OrderID="
            f"{getattr(order, 'id', '')}"
        )


        # ----------------------------------------------------
        # Estimated P&L only.
        # Actual fill reconciliation should be added later.
        # ----------------------------------------------------

        if (
            current_mid
            and entry_price > 0
        ):

            estimated_pnl = (

                current_mid
                - entry_price

            ) * qty * 100


            daily_realized_pnl += (
                estimated_pnl
            )


            log(
                "ESTIMATED P&L | "
                f"{symbol} | "
                f"${estimated_pnl:.2f}"
            )


        if symbol in position_state:

            del position_state[
                symbol
            ]


        save_state()


        return True


    except Exception as e:

        log(
            "EXIT ERROR | "
            f"{symbol} | "
            f"{e}"
        )

        return False


# ============================================================
# 41. MANAGE POSITIONS
# ============================================================

def manage_positions():

    positions = (
        get_option_positions()
    )


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


        entry_price = (
            get_actual_position_entry(
                position
            )
        )


        if entry_price <= 0:

            continue


        current_price = (
            get_option_mid(
                symbol
            )
        )


        if current_price is None:

            continue


        state = (
            position_state
            .setdefault(

                symbol,

                {

                    "underlying":
                        get_position_underlying(
                            symbol
                        ),

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
        )


        # Broker's average entry wins.
        state[
            "entry_price"
        ] = entry_price


        highest = safe_float(
            state.get(
                "highest_price",
                entry_price
            )
        )


        if (
            current_price
            > highest
        ):

            highest = (
                current_price
            )

            state[
                "highest_price"
            ] = highest


        pnl_pct = (

            current_price
            - entry_price

        ) / entry_price


        # ----------------------------------------------------
        # STOP LOSS
        # ----------------------------------------------------

        if (
            pnl_pct
            <= -STOP_LOSS_PCT
        ):

            close_position(
                position,
                "STOP_LOSS"
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
                "TAKE_PROFIT"
            )

            continue


        # ----------------------------------------------------
        # TRAILING STOP
        # ----------------------------------------------------

        if TRAILING_ENABLED:

            if (
                highest
                > entry_price
            ):

                trailing_stop = (
                    highest
                    * (
                        1
                        - TRAILING_PCT
                    )
                )


                if (
                    current_price
                    <= trailing_stop
                ):

                    close_position(
                        position,
                        "TRAILING_STOP"
                    )

                    continue


        log(
            "POSITION | "
            f"{symbol} | "
            f"Entry=${entry_price:.2f} | "
            f"Current=${current_price:.2f} | "
            f"PnL="
            f"{pnl_pct * 100:.2f}% | "
            f"High=${highest:.2f}"
        )


    save_state()


# ============================================================
# 42. PROCESS UNDERLYING
# ============================================================

def process_underlying(
    underlying
):

    if (
        trades_today
        >= MAX_TRADES_PER_DAY
    ):

        return


    if daily_loss_limit_reached():

        return


    if has_position_for_underlying(
        underlying
    ):

        log(
            f"{underlying}: "
            "Existing position."
        )

        return


    if (
        get_open_position_count()
        >= MAX_OPEN_POSITIONS
    ):

        return


    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    signal = get_signal(
        underlying
    )


    if signal is None:

        log(
            f"{underlying}: "
            "No signal."
        )

        return


    log(
        f"{underlying}: "
        f"SIGNAL={signal}"
    )


    # --------------------------------------------------------
    # CONTRACT
    # --------------------------------------------------------

    selected = (
        select_option_contract(
            underlying,
            signal
        )
    )


    if selected is None:

        return


    # --------------------------------------------------------
    # ORDER
    # --------------------------------------------------------

    execute_trade(
        underlying,
        selected,
        signal
    )


# ============================================================
# 43. ACCOUNT STATUS
# ============================================================

def check_account_status():

    try:

        account = (
            get_account()
        )


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


        option_level = getattr(
            account,
            "options_trading_level",
            None
        )


        log(
            "ACCOUNT | "
            f"Status={status} | "
            f"Equity=${equity:.2f} | "
            f"BuyingPower="
            f"${buying_power:.2f} | "
            f"OptionsLevel="
            f"{option_level}"
        )


        return True


    except Exception as e:

        log(
            f"ACCOUNT ERROR: {e}"
        )

        return False


# ============================================================
# 44. STARTUP
# ============================================================

def startup():

    print()
    print("=" * 70)

    print(
        "ALPACA OPTIONS BOT V3.3"
    )

    print("=" * 70)

    print(
        "MODE: PAPER ONLY"
    )

    print(
        "OPTIONS FEED: INDICATIVE"
    )

    print(
        "UNDERLYINGS: SPY + SPX"
    )

    print(
        "SPXW: INCLUDED UNDER SPX"
    )

    print("=" * 70)

    print()


    load_state()


    if not check_account_status():

        print(
            "Cannot access Alpaca."
        )

        sys.exit(1)


    initialize_position_state()


    log(
        f"Trades today: "
        f"{trades_today}"
    )


    log(
        "Estimated daily P&L: "
        f"${daily_realized_pnl:.2f}"
    )


# ============================================================
# 45. MAIN
# ============================================================

def main():

    startup()


    while True:

        try:

            # ------------------------------------------------
            # Reset
            # ------------------------------------------------

            reset_daily_state_if_needed()


            # ------------------------------------------------
            # Market
            # ------------------------------------------------

            if not market_is_open():

                log(
                    "MARKET CLOSED"
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue


            # ------------------------------------------------
            # Manage current positions FIRST
            # ------------------------------------------------

            manage_positions()


            # ------------------------------------------------
            # Daily loss
            # ------------------------------------------------

            if daily_loss_limit_reached():

                log(
                    "NEW ENTRIES DISABLED "
                    "FOR TODAY."
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue


            # ------------------------------------------------
            # Account
            # ------------------------------------------------

            check_account_status()


            # ------------------------------------------------
            # New entries
            # ------------------------------------------------

            for underlying in (
                UNDERLYINGS
            ):

                if (
                    trades_today
                    >= MAX_TRADES_PER_DAY
                ):

                    break


                if (
                    get_open_position_count()
                    >= MAX_OPEN_POSITIONS
                ):

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
                "BOT STOPPED"
            )

            save_state()

            break


        except Exception as e:

            log(
                "MAIN LOOP ERROR | "
                f"{e}"
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