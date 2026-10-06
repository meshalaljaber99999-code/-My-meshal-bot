# ============================================================
# PROFESSIONAL ALPACA OPTIONS BOT
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
from alpaca.data.enums import OptionsFeed


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

# نستخدم Indicative في البداية حتى نختبر البنية.
# عند توفر OPRA يمكن تحويلها إلى OPRA.
OPTIONS_FEED = OptionsFeed.INDICATIVE


# ============================================================
# STRATEGY
# ============================================================

STRATEGY = {

    "name": "Professional Options Strategy V2",

    # نريد الاثنين
    "underlyings": [
        "SPY",
        "SPX",
    ],

    "signal": {

        # AUTO = إشارة تجريبية مؤقتة
        # لاحقًا سيأتي هنا AI Strategy Engine
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

        # 10% max bid/ask spread
        "max_spread_pct": 0.10,

        "min_open_interest": 100,
    },

    "risk": {

        # أقصى مخاطرة من الحساب لكل صفقة
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

    now = datetime.now(NY_TZ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[{now}] {message}",
        flush=True
    )


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

        return bool(clock.is_open)

    except Exception as e:

        log(f"CLOCK ERROR: {e}")

        return False


def is_safe_trading_time():

    now = datetime.now(NY_TZ).time()

    market_open = dt_time(9, 30)

    first_15_end = dt_time(9, 45)

    if market_open <= now < first_15_end:

        return False

    return True


# ============================================================
# SPY SIGNAL
# ============================================================

def get_spy_direction():

    now = datetime.now(NY_TZ)

    start = now - timedelta(minutes=5)

    try:

        request = StockBarsRequest(

            symbol_or_symbols="SPY",

            timeframe=TimeFrame.Minute,

            start=start,

            end=now,
        )

        response = stock_data_client.get_stock_bars(
            request
        )

        bars = response.data.get(
            "SPY",
            []
        )

        if len(bars) < 2:

            return "NEUTRAL", 0.0

        first = float(
            bars[0].close
        )

        last = float(
            bars[-1].close
        )

        if first <= 0:

            return "NEUTRAL", 0.0

        move = (
            last - first
        ) / first

        threshold = STRATEGY[
            "signal"
        ][
            "minimum_move_pct"
        ]

        if move >= threshold:

            return "BULLISH", move

        if move <= -threshold:

            return "BEARISH", move

        return "NEUTRAL", move

    except Exception as e:

        log(f"SPY SIGNAL ERROR: {e}")

        return "NEUTRAL", 0.0


# ============================================================
# SIGNAL ENGINE
# ============================================================

def get_signal():

    mode = STRATEGY[
        "signal"
    ][
        "mode"
    ]

    if mode == "CALL":

        return "CALL", 0.0

    if mode == "PUT":

        return "PUT", 0.0

    direction, move = get_spy_direction()

    if direction == "BULLISH":

        return "CALL", move

    if direction == "BEARISH":

        return "PUT", move

    return None, move


# ============================================================
# OPTION CONTRACTS
# ============================================================

def get_contracts(
    underlying,
    direction,
):

    today = datetime.now(
        NY_TZ
    ).date()

    min_dte = STRATEGY[
        "option"
    ][
        "min_dte"
    ]

    max_dte = STRATEGY[
        "option"
    ][
        "max_dte"
    ]

    expiration_min = (
        today +
        timedelta(days=min_dte)
    )

    expiration_max = (
        today +
        timedelta(days=max_dte)
    )

    contract_type = (

        ContractType.CALL

        if direction == "CALL"

        else ContractType.PUT
    )

    try:

        request = GetOptionContractsRequest(

            underlying_symbols=[
                underlying
            ],

            status=AssetStatus.ACTIVE,

            expiration_date_gte=
                expiration_min,

            expiration_date_lte=
                expiration_max,

            type=contract_type,

            limit=500,
        )

        response = (
            trading_client
            .get_option_contracts(
                request
            )
        )

        return response.option_contracts

    except Exception as e:

        log(
            f"CONTRACT ERROR "
            f"{underlying}: {e}"
        )

        return []


# ============================================================
# OPTION CHAIN
# ============================================================

def get_chain(
    underlying,
    direction,
):

    today = datetime.now(
        NY_TZ
    ).date()

    min_dte = STRATEGY[
        "option"
    ][
        "min_dte"
    ]

    max_dte = STRATEGY[
        "option"
    ][
        "max_dte"
    ]

    contract_type = (

        ContractType.CALL

        if direction == "CALL"

        else ContractType.PUT
    )

    try:

        request = OptionChainRequest(

            underlying_symbol=
                underlying,

            feed=OPTIONS_FEED,

            type=contract_type,

            expiration_date_gte=
                today +
                timedelta(days=min_dte),

            expiration_date_lte=
                today +
                timedelta(days=max_dte),
        )

        return (
            option_data_client
            .get_option_chain(
                request
            )
        )

    except Exception as e:

        log(
            f"CHAIN ERROR "
            f"{underlying}: {e}"
        )

        return {}


# ============================================================
# SELECT CONTRACT
# ============================================================

def select_best_option(
    underlying,
    direction,
):

    contracts = get_contracts(
        underlying,
        direction
    )

    if not contracts:

        log(
            f"❌ No contracts: "
            f"{underlying}"
        )

        return None

    chain = get_chain(
        underlying,
        direction
    )

    if not chain:

        log(
            f"❌ No chain: "
            f"{underlying}"
        )

        return None

    contract_map = {
        c.symbol: c
        for c in contracts
    }

    min_delta = STRATEGY[
        "option"
    ][
        "min_delta"
    ]

    max_delta = STRATEGY[
        "option"
    ][
        "max_delta"
    ]

    max_premium = STRATEGY[
        "option"
    ][
        "max_premium"
    ]

    max_spread = STRATEGY[
        "option"
    ][
        "max_spread_pct"
    ]

    min_oi = STRATEGY[
        "option"
    ][
        "min_open_interest"
    ]

    candidates = []

    for symbol, snapshot in chain.items():

        contract = contract_map.get(
            symbol
        )

        if not contract:
            continue

        try:

            quote = (
                snapshot.latest_quote
            )

            greeks = (
                snapshot.greeks
            )

            if not quote or not greeks:

                continue

            bid = float(
                quote.bid_price or 0
            )

            ask = float(
                quote.ask_price or 0
            )

            if bid <= 0 or ask <= 0:

                continue

            if ask > max_premium:

                continue

            spread = (
                (ask - bid) / ask
            )

            if spread > max_spread:

                continue

            delta = abs(
                float(
                    greeks.delta or 0
                )
            )

            if not (
                min_delta
                <= delta
                <= max_delta
            ):

                continue

            try:

                oi = int(
                    float(
                        getattr(
                            contract,
                            "open_interest",
                            0
                        ) or 0
                    )
                )

            except Exception:

                oi = 0

            if oi < min_oi:

                continue

            expiration = (
                contract.expiration_date
            )

            if hasattr(
                expiration,
                "date"
            ):

                expiration = (
                    expiration.date()
                )

            dte = (
                expiration -
                datetime.now(
                    NY_TZ
                ).date()
            ).days

            target_delta = (
                min_delta +
                max_delta
            ) / 2

            score = 0

            score -= abs(
                delta -
                target_delta
            ) * 100

            score -= spread * 100

            score += math.log(
                max(oi, 1)
            )

            candidates.append({

                "symbol": symbol,

                "bid": bid,

                "ask": ask,

                "delta": delta,

                "oi": oi,

                "dte": dte,

                "strike": float(
                    contract.strike_price
                ),

                "expiration":
                    str(expiration),

                "score": score,
            })

        except Exception:

            continue

    if not candidates:

        log(
            f"❌ No valid option "
            f"for {underlying}"
        )

        return None

    candidates.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    best = candidates[0]

    log("🎯 SELECTED OPTION")

    log(
        f"Symbol: "
        f"{best['symbol']}"
    )

    log(
        f"Strike: "
        f"{best['strike']}"
    )

    log(
        f"DTE: "
        f"{best['dte']}"
    )

    log(
        f"Bid/Ask: "
        f"{best['bid']:.2f}/"
        f"{best['ask']:.2f}"
    )

    log(
        f"Delta: "
        f"{best['delta']:.3f}"
    )

    log(
        f"OI: "
        f"{best['oi']}"
    )

    return best


# ============================================================
# POSITIONS
# ============================================================

def get_open_option_positions():

    try:

        positions = (
            trading_client
            .get_all_positions()
        )

        result = []

        for position in positions:

            asset_class = str(
                getattr(
                    position,
                    "asset_class",
                    ""
                )
            ).lower()

            symbol = str(
                position.symbol
            )

            if (
                "option"
                in asset_class
                or len(symbol) >= 15
            ):

                result.append(
                    position
                )

        return result

    except Exception as e:

        log(
            f"POSITION ERROR: {e}"
        )

        return []


# ============================================================
# RISK
# ============================================================

def calculate_quantity(
    option_price,
    equity,
):

    risk_pct = STRATEGY[
        "risk"
    ][
        "risk_per_trade_pct"
    ]

    max_contracts = STRATEGY[
        "risk"
    ][
        "max_contracts_per_trade"
    ]

    budget = (
        equity *
        risk_pct /
        100
    )

    contract_cost = (
        option_price *
        100
    )

    if contract_cost <= 0:

        return 0

    quantity = math.floor(
        budget /
        contract_cost
    )

    quantity = min(
        quantity,
        max_contracts
    )

    return max(
        quantity,
        0
    )


def risk_allows_trade():

    equity = get_equity()

    if equity <= 0:

        return False

    risk = STRATEGY[
        "risk"
    ]

    max_loss = (
        equity *
        risk[
            "max_daily_loss_pct"
        ] /
        100
    )

    if (
        daily_realized_pnl
        <= -max_loss
    ):

        log(
            "🛑 MAX DAILY LOSS"
        )

        return False

    if (
        trades_today
        >= risk[
            "max_trades_per_day"
        ]
    ):

        log(
            "🛑 MAX TRADES/DAY"
        )

        return False

    if (
        len(
            get_open_option_positions()
        )
        >= risk[
            "max_open_positions"
        ]
    ):

        log(
            "🛑 MAX OPEN POSITIONS"
        )

        return False

    return True


# ============================================================
# BUY
# ============================================================

def submit_buy(
    symbol,
    quantity,
    price,
):

    try:

        request = LimitOrderRequest(

            symbol=symbol,

            qty=quantity,

            side=OrderSide.BUY,

            time_in_force=
                TimeInForce.DAY,

            limit_price=
                round(price, 2),
        )

        order = (
            trading_client
            .submit_order(request)
        )

        log(
            f"📤 BUY "
            f"{symbol} "
            f"x{quantity} "
            f"@ {price:.2f}"
        )

        return order

    except Exception as e:

        log(
            f"❌ BUY ERROR: {e}"
        )

        return None


# ============================================================
# WAIT FILL
# ============================================================

def wait_for_fill(
    order,
    timeout=20,
):

    start = time.time()

    while (
        time.time() -
        start
        < timeout
    ):

        try:

            current = (
                trading_client
                .get_order_by_id(
                    order.id
                )
            )

            status = str(
                getattr(
                    current.status,
                    "value",
                    current.status
                )
            ).lower()

            if status == "filled":

                price = float(
                    current.filled_avg_price
                )

                qty = int(
                    float(
                        current.filled_qty
                    )
                )

                log(
                    f"✅ FILLED "
                    f"{qty} @ {price:.2f}"
                )

                return price, qty

            if status in (
                "canceled",
                "expired",
                "rejected",
            ):

                return None, 0

        except Exception as e:

            log(
                f"ORDER CHECK ERROR: {e}"
            )

        time.sleep(1)

    try:

        trading_client.cancel_order_by_id(
            order.id
        )

    except Exception:

        pass

    return None, 0


# ============================================================
# SELL
# ============================================================

def submit_sell(
    symbol,
    quantity,
):

    try:

        request = MarketOrderRequest(

            symbol=symbol,

            qty=quantity,

            side=OrderSide.SELL,

            time_in_force=
                TimeInForce.DAY,
        )

        order = (
            trading_client
            .submit_order(request)
        )

        log(
            f"📤 SELL "
            f"{symbol} "
            f"x{quantity}"
        )

        return order

    except Exception as e:

        log(
            f"❌ SELL ERROR: {e}"
        )

        return None


# ============================================================
# MONITOR
# ============================================================

def monitor_position(
    symbol,
    quantity,
    entry_price,
):

    global daily_realized_pnl

    stop = (
        entry_price *
        (
            1 -
            STRATEGY[
                "exit"
            ][
                "stop_loss_pct"
            ] /
            100
        )
    )

    target = (
        entry_price *
        (
            1 +
            STRATEGY[
                "exit"
            ][
                "take_profit_pct"
            ] /
            100
        )
    )

    highest = entry_price

    log(
        f"🛡️ MONITOR "
        f"{symbol}"
    )

    while True:

        try:

            request = (
                OptionLatestQuoteRequest(
                    symbol_or_symbols=symbol,
                    feed=OPTIONS_FEED,
                )
            )

            quotes = (
                option_data_client
                .get_option_latest_quote(
                    request
                )
            )

            quote = quotes.get(
                symbol
            )

            if quote:

                bid = float(
                    quote.bid_price or 0
                )

                if bid > 0:

                    current = bid

                    if current > highest:

                        highest = current

                        if STRATEGY[
                            "exit"
                        ][
                            "trailing_enabled"
                        ]:

                            trailing = (
                                highest *
                                (
                                    1 -
                                    STRATEGY[
                                        "exit"
                                    ][
                                        "trailing_stop_pct"
                                    ] /
                                    100
                                )
                            )

                            stop = max(
                                stop,
                                trailing
                            )

                    log(
                        f"📊 {symbol} "
                        f"Bid={current:.2f} "
                        f"SL={stop:.2f} "
                        f"TP={target:.2f}"
                    )

                    if current >= target:

                        exit_price = current

                        submit_sell(
                            symbol,
                            quantity
                        )

                        pnl = (
                            exit_price -
                            entry_price
                        ) * quantity * 100

                        daily_realized_pnl += pnl

                        log(
                            f"💰 TP "
                            f"PnL=${pnl:.2f}"
                        )

                        return

                    if current <= stop:

                        exit_price = current

                        submit_sell(
                            symbol,
                            quantity
                        )

                        pnl = (
                            exit_price -
                            entry_price
                        ) * quantity * 100

                        daily_realized_pnl += pnl

                        log(
                            f"🛑 SL "
                            f"PnL=${pnl:.2f}"
                        )

                        return

            if not is_market_open():

                log(
                    "🔔 MARKET CLOSED"
                )

                return

            time.sleep(2)

        except Exception as e:

            log(
                f"MONITOR ERROR: {e}"
            )

            time.sleep(2)


# ============================================================
# SCAN
# ============================================================

def scan():

    global trades_today

    reset_daily_state()

    if not is_market_open():

        return

    if not is_safe_trading_time():

        return

    if not risk_allows_trade():

        return

    direction, move = get_signal()

    if not direction:

        return

    log(
        f"🔎 SIGNAL "
        f"{direction} "
        f"move={move * 100:.3f}%"
    )

    for underlying in STRATEGY[
        "underlyings"
    ]:

        option = (
            select_best_option(
                underlying,
                direction
            )
        )

        if not option:

            continue

        equity = get_equity()

        qty = calculate_quantity(
            option["ask"],
            equity
        )

        if qty <= 0:

            log(
                "⚠️ Risk budget "
                "cannot buy 1 contract."
            )

            continue

        order = submit_buy(
            option["symbol"],
            qty,
            option["ask"]
        )

        if not order:

            continue

        filled_price, filled_qty = (
            wait_for_fill(order)
        )

        if not filled_price:

            continue

        trades_today += 1

        log(
            f"🚀 TRADE "
            f"#{trades_today}"
        )

        monitor_position(
            option["symbol"],
            filled_qty,
            filled_price
        )

        break


# ============================================================
# STARTUP
# ============================================================

def startup():

    log(
        "================================"
    )

    log(
        "🚀 PROFESSIONAL OPTIONS BOT"
    )

    log(
        "================================"
    )

    log(
        f"Paper: {PAPER_MODE}"
    )

    log(
        f"Feed: {OPTIONS_FEED.value}"
    )

    log(
        f"Underlyings: "
        f"{STRATEGY['underlyings']}"
    )

    account = get_account()

    if account:

        log(
            f"Cash: {account.cash}"
        )

        log(
            f"Equity: {account.equity}"
        )

        log(
            f"Buying Power: "
            f"{account.buying_power}"
        )

        log(
            f"Options Buying Power: "
            f"{getattr(account, 'options_buying_power', 'N/A')}"
        )

    else:

        log(
            "❌ ACCOUNT CONNECTION FAILED"
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    startup()

    while True:

        try:

            scan()

            time.sleep(10)

        except KeyboardInterrupt:

            log(
                "🛑 BOT STOPPED"
            )

            break

        except Exception as e:

            log(
                f"🔥 MAIN ERROR: {e}"
            )

            time.sleep(10)