
# -*- coding: utf-8 -*-
"""
صَفاء التحليل v70.0
Smart Profit Runner + Opening Range Breakout
SPY 0-DTE | Paper First

استراتيجية الدخول:
1. حساب ميل السوق من ES + GAP + VIX + اتجاه 20 يوم.
2. لا دخول عند الميل المحايد.
3. كسر أول 30 دقيقة.
4. تأكيد VWAP.
5. حجم تداول >= 1.2 من المتوسط.

استراتيجية الأرباح:
1. +30%: نقل الوقف إلى سعر الدخول.
2. +50%: تفعيل ملاحقة القمة.
3. Trailing Stop = أعلى Bid * 0.80.
4. لا يوجد هدف ربح ثابت.
5. الخروج عند الوقف أو انعكاس VWAP قبل تفعيل الملاحقة
   أو نهاية الجلسة أو حد الخسارة اليومي.

تنبيه:
الوقف برمجي وليس أمراً مستقلاً لدى الوسيط.
لا يوجد ضمان للربح أو لسعر التنفيذ.
"""

import os
import sys
import csv
import json
import time
import logging

from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests


# =========================================================
# 1. الإعدادات
# =========================================================

NAME = "صَفاء التحليل"
VERSION = "v70.0"

NY = ZoneInfo("America/New_York")
SYMBOL = "SPY"


def env_f(name, default):
    try:
        return float(os.getenv(name, default))
    except (ValueError, TypeError):
        return float(default)


def env_i(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except (ValueError, TypeError):
        return int(default)


def env_b(name, default):
    return str(os.getenv(name, default)).strip().lower() in (
        "1", "true", "yes", "on"
    )


# -------------------------
# إدارة المخاطر
# -------------------------

RISK_PER_TRADE = env_f("RISK_PER_TRADE", "0.015")

MAX_PREMIUM_PCT = env_f("MAX_PREMIUM_PCT", "0.10")

STOP_LOSS_PCT = env_f("STOP_LOSS_PCT", "0.30")

DAILY_LOSS_LIMIT = env_f("DAILY_LOSS_LIMIT", "0.03")

MAX_CONSEC_LOSSES = env_i("MAX_CONSEC_LOSSES", "3")

MAX_TRADES_PER_DAY = env_i("MAX_TRADES_PER_DAY", "6")


# -------------------------
# استراتيجية الدخول
# -------------------------

GAP_THRESHOLD = env_f("GAP_THRESHOLD", "0.0015")

ES_THRESHOLD = 0.0015

ES_STRONG = 0.004

VIX_THRESHOLD = 0.05

BIAS_SCORE_MIN = env_i("BIAS_SCORE_MIN", "1")

OPEN_RANGE_MINUTES = 30

VOLUME_MULT = env_f("VOLUME_MULT", "1.2")

MAX_SPREAD_PCT = env_f("MAX_SPREAD_PCT", "0.10")

MIN_OPTION_ASK = env_f("MIN_OPTION_ASK", "0.30")


# -------------------------
# Smart Profit Runner
# -------------------------

BREAKEVEN_TRIGGER = env_f(
    "BREAKEVEN_TRIGGER", "0.30"
)

TRAIL_ACTIVATION = env_f(
    "TRAIL_ACTIVATION", "0.50"
)

TRAIL_DISTANCE = env_f(
    "TRAIL_DISTANCE", "0.20"
)

# 20% تراجع من أعلى سعر للعقد


# -------------------------
# أوقات التداول
# -------------------------

SIGNAL_START = dtime(10, 0)

SIGNAL_END = dtime(14, 30)

EOD_CLOSE_TIME = dtime(15, 45)


# -------------------------
# Alpaca
# -------------------------

API_KEY = (
    os.getenv("ALPACA_API_KEY")
    or os.getenv("APCA_API_KEY_ID")
    or ""
).strip()

API_SECRET = (
    os.getenv("ALPACA_API_SECRET")
    or os.getenv("APCA_API_SECRET_KEY")
    or ""
).strip()

DATA_URL = os.getenv(
    "ALPACA_DATA_URL",
    "https://data.alpaca.markets"
).rstrip("/")

TRADING_URL = os.getenv(
    "ALPACA_TRADING_URL",
    "https://paper-api.alpaca.markets"
).rstrip("/")

STOCK_FEED = os.getenv(
    "ALPACA_STOCK_FEED", "iex"
)

OPTIONS_FEED = os.getenv(
    "ALPACA_OPTIONS_FEED", "indicative"
)

ALLOW_LIVE = env_b("ALLOW_LIVE", "false")


# -------------------------
# الملفات
# -------------------------

DATA_DIR = Path(os.getenv("DATA_DIR", "."))

DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "safaa_v70_state.json"

JOURNAL_FILE = DATA_DIR / "safaa_v70_journal.csv"

BIAS_LOG = DATA_DIR / "safaa_v70_bias_log.csv"


HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(NAME)


# =========================================================
# 2. الأدوات العامة
# =========================================================

def now_ny():
    return datetime.now(NY)


def today_str():
    return now_ny().date().isoformat()


def stamp():
    return now_ny().isoformat(timespec="seconds")


def api(method, url, params=None, body=None):

    try:

        r = requests.request(
            method,
            url,
            headers=HEADERS,
            params=params,
            json=body,
            timeout=15
        )

        data = r.json() if r.text else {}

        if not r.ok:

            log.warning(
                f"API {method} "
                f"{url.split('.markets')[-1]} "
                f"-> {r.status_code}: {str(data)[:250]}"
            )

        return r.ok, data

    except Exception as e:

        log.warning(f"API connection error: {e}")

        return False, {}


def load_json(path, default):

    try:

        if path.exists():

            with open(path, "r", encoding="utf-8") as f:

                return json.load(f)

    except Exception as e:

        log.warning(f"تعذر قراءة {path}: {e}")

    return default


def save_json(path, data):

    tmp = Path(str(path) + ".tmp")

    with open(tmp, "w", encoding="utf-8") as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(path)


def new_day_state(equity):

    return {
        "date": today_str(),
        "start_equity": equity,
        "realized_pnl": 0.0,
        "consec_losses": 0,
        "trades_today": 0,
        "halted": False,
        "bias": None,
        "gap_pct": None,
        "bias_score": None,
        "bias_factors": None,
        "bias_evaluated": False,
        "last_bar_checked": None,
        "position": None,
    }


state = load_json(STATE_FILE, {})


def save_state():
    save_json(STATE_FILE, state)


# =========================================================
# 3. سجل الصفقات
# =========================================================

def log_trade(pos, exit_price, pnl, reason):

    new_file = not JOURNAL_FILE.exists()

    with open(
        JOURNAL_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as f:

        w = csv.writer(f)

        if new_file:

            w.writerow([
                "time",
                "contract",
                "direction",
                "qty",
                "entry",
                "exit",
                "pnl_usd",
                "pnl_pct",
                "peak",
                "max_gain_pct",
                "reason"
            ])

        total_cost = (
            pos["entry"] * 100 * pos["qty"]
        )

        pct = (
            pnl / total_cost
            if total_cost > 0 else 0
        )

        max_gain = (
            pos["peak"] / pos["entry"] - 1
        ) * 100 if pos["entry"] > 0 else 0

        w.writerow([
            stamp(),
            pos["contract"],
            pos["direction"],
            pos["qty"],
            f"{pos['entry']:.2f}",
            f"{exit_price:.2f}",
            f"{pnl:.2f}",
            f"{pct * 100:.1f}%",
            f"{pos['peak']:.2f}",
            f"{max_gain:.1f}%",
            reason
        ])


# =========================================================
# 4. بيانات السوق
# =========================================================

def market_clock():

    ok, d = api(
        "GET",
        f"{TRADING_URL}/v2/clock"
    )

    return bool(d.get("is_open")) if ok else False


def get_equity():

    ok, d = api(
        "GET",
        f"{TRADING_URL}/v2/account"
    )

    return float(d.get("equity", 0)) if ok else 0.0


def parse_t(t):

    return datetime.fromisoformat(
        t.replace("Z", "+00:00")
    ).astimezone(NY)


def fetch_bars(start, end, timeframe="1Min"):

    bars = []

    token = None

    while True:

        params = {
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
            "feed": STOCK_FEED,
            "adjustment": "raw",
        }

        if token:
            params["page_token"] = token

        ok, d = api(
            "GET",
            f"{DATA_URL}/v2/stocks/{SYMBOL}/bars",
            params=params
        )

        if not ok:
            break

        bars += d.get("bars") or []

        token = d.get("next_page_token")

        if not token:
            break

    return bars


def prev_close():

    end = now_ny()

    bars = fetch_bars(
        end - timedelta(days=10),
        end,
        "1Day"
    )

    today = now_ny().date()

    prior = [
        b for b in bars
        if parse_t(b["t"]).date() < today
    ]

    return float(prior[-1]["c"]) if prior else None


_bars_cache = {
    "t": 0,
    "bars": []
}


def session_bars(max_age=20):

    if time.time() - _bars_cache["t"] < max_age:

        return _bars_cache["bars"]

    n = now_ny()

    start = n.replace(
        hour=9,
        minute=30,
        second=0,
        microsecond=0
    )

    bars = fetch_bars(start, n)

    cur_min = n.replace(
        second=0,
        microsecond=0
    )

    bars = [
        b for b in bars
        if parse_t(b["t"]) < cur_min
    ]

    _bars_cache.update({
        "t": time.time(),
        "bars": bars
    })

    return bars


def vwap_of(bars):

    pv = 0.0

    vol = 0.0

    for b in bars:

        tp = (
            float(b["h"])
            + float(b["l"])
            + float(b["c"])
        ) / 3

        v = float(b["v"])

        pv += tp * v

        vol += v

    return pv / vol if vol > 0 else None


def compute_bias():

    pc = prev_close()

    if not pc:
        return "neutral", None

    n = now_ny()

    pre = fetch_bars(
        n.replace(
            hour=4,
            minute=0,
            second=0,
            microsecond=0
        ),
        n.replace(
            hour=9,
            minute=29,
            second=0,
            microsecond=0
        )
    )

    if pre:

        ref = float(pre[-1]["c"])

    else:

        rth = session_bars(max_age=0)

        if not rth:
            return "neutral", None

        ref = float(rth[0]["o"])

    gap = (ref - pc) / pc

    bias = (
        "bull"
        if gap >= GAP_THRESHOLD
        else "bear"
        if gap <= -GAP_THRESHOLD
        else "neutral"
    )

    return bias, gap


# =========================================================
# 5. حساب الميل
# =========================================================

def yahoo_chart(symbol, interval, rng):

    try:

        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={
                "interval": interval,
                "range": rng,
                "includePrePost": "true"
            },
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            timeout=10
        )

        r.raise_for_status()

        j = r.json()["chart"]["result"][0]

        closes = j["indicators"]["quote"][0]["close"]

        return [
            (datetime.fromtimestamp(t, NY), c)
            for t, c in zip(j["timestamp"], closes)
            if c is not None
        ]

    except Exception as e:

        log.warning(f"تعذر جلب {symbol}: {e}")

        return []


def es_overnight_pct():

    pts = yahoo_chart(
        "ES=F",
        "5m",
        "5d"
    )

    if not pts:
        return None

    if (
        now_ny() - pts[-1][0]
    ).total_seconds() > 1800:

        return None

    today = now_ny().date()

    ref = [
        c for t, c in pts
        if t.date() < today
        and dtime(15, 55) <= t.time() <= dtime(16, 0)
    ]

    if not ref:
        return None

    return (
        pts[-1][1] - ref[-1]
    ) / ref[-1]


def vix_change_pct():

    today = now_ny().date()

    days = [
        c for t, c in yahoo_chart(
            "^VIX",
            "1d",
            "15d"
        )
        if t.date() < today
    ]

    if len(days) < 2:
        return None

    return (
        days[-1] - days[-2]
    ) / days[-2]


def spy_trend_score():

    today = now_ny().date()

    bars = fetch_bars(
        now_ny() - timedelta(days=60),
        now_ny(),
        "1Day"
    )

    closes = [
        float(b["c"])
        for b in bars
        if parse_t(b["t"]).date() < today
    ]

    if len(closes) < 20:
        return 0

    avg = sum(closes[-20:]) / 20

    return 1 if closes[-1] > avg else -1


def compute_bias_scored():

    _, gap = compute_bias()

    es = es_overnight_pct()

    vix = vix_change_pct()

    f = {}

    f["ES"] = (
        0 if es is None
        else 2 if es >= ES_STRONG
        else 1 if es >= ES_THRESHOLD
        else -2 if es <= -ES_STRONG
        else -1 if es <= -ES_THRESHOLD
        else 0
    )

    f["GAP"] = (
        0 if gap is None
        else 1 if gap >= GAP_THRESHOLD
        else -1 if gap <= -GAP_THRESHOLD
        else 0
    )

    f["VIX"] = (
        0 if vix is None
        else -1 if vix >= VIX_THRESHOLD
        else 1 if vix <= -VIX_THRESHOLD
        else 0
    )

    f["TREND"] = spy_trend_score()

    score = sum(f.values())

    bias = (
        "bull"
        if score >= BIAS_SCORE_MIN
        else "bear"
        if score <= -BIAS_SCORE_MIN
        else "neutral"
    )

    f["raw"] = {
        "es_pct": es,
        "gap_pct": gap,
        "vix_pct": vix
    }

    return bias, gap, score, f


def ensure_bias():

    if state.get("bias") is not None:
        return

    if now_ny().time() < dtime(9, 35):
        return

    bias, gap, score, factors = compute_bias_scored()

    state["bias"] = bias

    state["gap_pct"] = gap

    state["bias_score"] = score

    state["bias_factors"] = factors

    state["bias_evaluated"] = False

    save_state()

    log.info(
        f"🧭 ميل اليوم: {bias} "
        f"| النقاط: {score:+d} "
        f"| {factors}"
    )


def evaluate_bias():

    if state.get("bias_evaluated"):
        return

    if state.get("bias") is None:
        return

    if now_ny().time() < dtime(15, 55):
        return

    bars = session_bars(max_age=0)

    if len(bars) < 30:
        return

    o = float(bars[0]["o"])

    c = float(bars[-1]["c"])

    actual = "bull" if c > o else "bear"

    bias = state["bias"]

    hit = "" if bias == "neutral" else int(bias == actual)

    is_new = not BIAS_LOG.exists()

    with open(
        BIAS_LOG,
        "a",
        newline="",
        encoding="utf-8"
    ) as fh:

        w = csv.writer(fh)

        if is_new:

            w.writerow([
                "date",
                "bias",
                "score",
                "actual",
                "hit",
                "open",
                "close",
                "factors"
            ])

        w.writerow([
            state["date"],
            bias,
            state.get("bias_score"),
            actual,
            hit,
            o,
            c,
            json.dumps(
                state.get("bias_factors"),
                ensure_ascii=False
            )
        ])

    state["bias_evaluated"] = True

    save_state()

    with open(BIAS_LOG, encoding="utf-8") as fh:

        scored = [
            r for r in csv.DictReader(fh)
            if r["hit"] != ""
        ]

    hits = sum(int(r["hit"]) for r in scored)

    acc = (
        f"{hits}/{len(scored)} "
        f"({hits / len(scored) * 100:.0f}%)"
        if scored else "لا بيانات بعد"
    )

    log.info(
        f"📈 تقييم الميل: {bias} "
        f"| الفعلي: {actual} "
        f"| الدقة: {acc}"
    )


# =========================================================
# 6. الخيارات والأوامر
# =========================================================

def option_quote(occ):

    ok, d = api(
        "GET",
        f"{DATA_URL}/v1beta1/options/quotes/latest",
        params={
            "symbols": occ,
            "feed": OPTIONS_FEED
        }
    )

    q = (
        (d.get("quotes") or {}).get(occ)
        if ok else None
    )

    if not q:
        return None

    bid = float(q.get("bp", 0))

    ask = float(q.get("ap", 0))

    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    return bid, ask


def pick_contract(direction, underlying_price):

    ctype = "call" if direction == "up" else "put"

    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": ctype,
        "expiration_date": today_str(),
        "strike_price_gte": f"{underlying_price - 3:.2f}",
        "strike_price_lte": f"{underlying_price + 3:.2f}",
        "limit": 100,
    }

    ok, d = api(
        "GET",
        f"{TRADING_URL}/v2/options/contracts",
        params=params
    )

    cs = (
        d.get("option_contracts") or []
        if ok else []
    )

    if not cs:
        return None

    cs.sort(
        key=lambda c: abs(
            float(c["strike_price"]) - underlying_price
        )
    )

    return cs[0]["symbol"]


def place_order(
    symbol,
    qty,
    side,
    otype="market",
    limit=None
):

    body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": otype,
        "time_in_force": "day"
    }

    if limit is not None:
        body["limit_price"] = f"{limit:.2f}"

    ok, d = api(
        "POST",
        f"{TRADING_URL}/v2/orders",
        body=body
    )

    return d.get("id") if ok else None


def wait_fill(order_id, timeout=20):

    end = time.time() + timeout

    while time.time() < end:

        ok, d = api(
            "GET",
            f"{TRADING_URL}/v2/orders/{order_id}"
        )

        if ok:

            st = d.get("status")

            if st == "filled":

                return (
                    int(float(d["filled_qty"])),
                    float(d["filled_avg_price"])
                )

            if st in (
                "canceled",
                "expired",
                "rejected"
            ):
                break

        time.sleep(1)

    # إلغاء الكمية المتبقية
    api(
        "DELETE",
        f"{TRADING_URL}/v2/orders/{order_id}"
    )

    time.sleep(1)

    ok, d = api(
        "GET",
        f"{TRADING_URL}/v2/orders/{order_id}"
    )

    if ok and float(d.get("filled_qty") or 0) > 0:

        return (
            int(float(d["filled_qty"])),
            float(d["filled_avg_price"])
        )

    return 0, 0.0


# =========================================================
# 7. حساب الربح والخسارة اليومية
# =========================================================

def position_unrealized_pnl(pos=None, quote=None):

    if pos is None:
        pos = state.get("position")

    if not pos or pos["qty_open"] <= 0:
        return 0.0

    if quote is None:
        quote = option_quote(pos["contract"])

    if not quote:
        return 0.0

    bid = quote[0]

    return (
        bid - pos["entry"]
    ) * 100 * pos["qty_open"]


def total_daily_pnl(pos=None, quote=None):

    if pos is None:
        pos = state.get("position")

    pnl = float(state.get("realized_pnl", 0.0))

    if pos:

        pnl += float(pos.get("pnl_usd", 0.0))

        pnl += position_unrealized_pnl(
            pos,
            quote
        )

    return pnl


def check_halt(pos=None, quote=None):

    daily_pnl = total_daily_pnl(
        pos,
        quote
    )

    start_equity = float(
        state.get("start_equity", 0)
    )

    if start_equity <= 0:
        return False

    daily_limit = (
        -DAILY_LOSS_LIMIT * start_equity
    )

    if daily_pnl <= daily_limit:

        if not state.get("halted"):

            log.warning(
                f"🛑 حد الخسارة اليومي "
                f"| PNL={daily_pnl:.2f}$ "
                f"| الحد={daily_limit:.2f}$"
            )

        state["halted"] = True

    if (
        state.get("consec_losses", 0)
        >= MAX_CONSEC_LOSSES
    ):

        state["halted"] = True

    save_state()

    return bool(state["halted"])


# =========================================================
# 8. الدخول
# =========================================================

def try_entry():

    n = now_ny()

    ensure_bias()

    bias = state.get("bias")

    # لا تداول عند الميل المحايد
    if bias not in ("bull", "bear"):

        return

    bars = session_bars()

    if len(bars) < OPEN_RANGE_MINUTES + 2:
        return

    open_end = n.replace(
        hour=9,
        minute=30,
        second=0,
        microsecond=0
    ) + timedelta(minutes=OPEN_RANGE_MINUTES)

    or_bars = [
        b for b in bars
        if parse_t(b["t"]) < open_end
    ]

    rest = [
        b for b in bars
        if parse_t(b["t"]) >= open_end
    ]

    if not or_bars or not rest:
        return

    last = bars[-1]

    if state.get("last_bar_checked") == last["t"]:
        return

    state["last_bar_checked"] = last["t"]

    save_state()

    or_high = max(
        float(b["h"]) for b in or_bars
    )

    or_low = min(
        float(b["l"]) for b in or_bars
    )

    close = float(last["c"])

    avg_vol = (
        sum(float(b["v"]) for b in bars[:-1])
        / max(1, len(bars) - 1)
    )

    last_vol = float(last["v"])

    vol_ok = (
        avg_vol > 0
        and last_vol >= VOLUME_MULT * avg_vol
    )

    vw = vwap_of(bars)

    if vw is None or not vol_ok:
        return

    direction = None

    if (
        close > or_high
        and close > vw
        and bias == "bull"
    ):

        direction = "up"

    elif (
        close < or_low
        and close < vw
        and bias == "bear"
    ):

        direction = "down"

    if not direction:
        return

    log.info(
        f"⚡ إشارة {direction.upper()} "
        f"| السعر={close:.2f} "
        f"| OR={or_low:.2f}-{or_high:.2f} "
        f"| VWAP={vw:.2f} "
        f"| Volume={last_vol:.0f}"
    )

    open_position(
        direction,
        close
    )


def open_position(direction, underlying_price):

    occ = pick_contract(
        direction,
        underlying_price
    )

    if not occ:

        log.info("لا يوجد عقد مناسب")

        return

    q = option_quote(occ)

    if not q:

        log.info("لا يوجد سعر صالح للعقد")

        return

    bid, ask = q

    mid = (bid + ask) / 2

    if mid <= 0:
        return

    spread = (ask - bid) / mid

    if (
        spread > MAX_SPREAD_PCT
        or ask < MIN_OPTION_ASK
    ):

        log.info(
            f"⛔ تجاهل العقد "
            f"| Spread={spread * 100:.1f}% "
            f"| Ask={ask:.2f}"
        )

        return

    equity = get_equity()

    if equity <= 0:
        return

    risk_per_contract = (
        ask * 100 * STOP_LOSS_PCT
    )

    qty_by_risk = int(
        (equity * RISK_PER_TRADE)
        // risk_per_contract
    )

    qty_by_premium = int(
        (equity * MAX_PREMIUM_PCT)
        // (ask * 100)
    )

    qty = min(
        qty_by_risk,
        qty_by_premium
    )

    if qty < 1:

        log.info(
            f"⛔ لا دخول: رأس المال لا يسمح "
            f"| Ask={ask:.2f}"
        )

        return

    oid = place_order(
        occ,
        qty,
        "buy",
        "limit",
        ask
    )

    if not oid:
        return

    filled, avg = wait_fill(
        oid,
        timeout=20
    )

    if filled < 1:

        log.info("لم يتنفذ أمر الشراء")

        return

    state["position"] = {
        "contract": occ,
        "direction": direction,
        "qty": filled,
        "qty_open": filled,
        "entry": avg,
        "peak": avg,
        "stop": avg * (1 - STOP_LOSS_PCT),
        "trail_active": False,
        "breakeven_active": False,
        "pnl_usd": 0.0,
        "last_exit": avg,
        "entry_time": stamp(),
    }

    state["trades_today"] += 1

    save_state()

    log.info(
        f"✅ دخول {direction.upper()} "
        f"| {occ} "
        f"| الكمية={filled} "
        f"| الدخول={avg:.2f} "
        f"| الوقف الأولي={avg * (1 - STOP_LOSS_PCT):.2f}"
    )


# =========================================================
# 9. تنفيذ البيع
# =========================================================

def sell(pos, qty, reason, limit=None):

    if qty <= 0:
        return False

    if qty > pos["qty_open"]:
        qty = pos["qty_open"]

    if limit is not None:

        oid = place_order(
            pos["contract"],
            qty,
            "sell",
            "limit",
            limit
        )

    else:

        oid = place_order(
            pos["contract"],
            qty,
            "sell",
            "market"
        )

    if not oid:
        return False

    filled, avg = wait_fill(
        oid,
        timeout=20
    )

    if filled < 1:

        log.warning(
            f"لم يتم تنفيذ البيع: {reason}"
        )

        return False

    pnl = (
        avg - pos["entry"]
    ) * 100 * filled

    pos["pnl_usd"] += pnl

    pos["qty_open"] -= filled

    pos["last_exit"] = avg

    log.info(
        f"💰 SELL {filled} "
        f"| السعر={avg:.2f} "
        f"| {reason} "
        f"| PNL={pnl:+.2f}$ "
        f"| المتبقي={pos['qty_open']}"
    )

    save_state()

    return True


def finish_trade(reason):

    pos = state.get("position")

    if not pos:
        return

    if pos["qty_open"] > 0:

        log.warning(
            "لا يمكن إنهاء الصفقة قبل تصفية الكمية المتبقية"
        )

        return

    pnl = float(pos["pnl_usd"])

    log_trade(
        pos,
        pos.get("last_exit", pos["entry"]),
        pnl,
        reason
    )

    state["realized_pnl"] += pnl

    state["consec_losses"] = (
        state["consec_losses"] + 1
        if pnl < 0
        else 0
    )

    state["position"] = None

    log.info(
        f"📊 انتهت الصفقة "
        f"| PNL={pnl:+.2f}$ "
        f"| اليوم={state['realized_pnl']:+.2f}$ "
        f"| خسارات متتالية={state['consec_losses']}"
    )

    check_halt()

    save_state()


# =========================================================
# 10. Smart Profit Runner
# =========================================================

def manage_position():

    pos = state.get("position")

    if not pos:
        return

    n = now_ny()

    eod = n.time() >= EOD_CLOSE_TIME

    q = option_quote(pos["contract"])

    if not q and not eod:
        return

    bid = q[0] if q else 0.0

    # ---------------------------------
    # تحديث أعلى سعر للعقد
    # ---------------------------------

    if bid > pos["peak"]:

        pos["peak"] = bid

        log.info(
            f"📈 قمة جديدة للعقد: {pos['peak']:.2f}"
        )

    entry = pos["entry"]

    if entry <= 0:
        return

    gain = (
        (bid - entry) / entry
        if bid > 0 else 0.0
    )

    peak_gain = (
        (pos["peak"] - entry) / entry
    )

    # ---------------------------------
    # وقف الخسارة اليومي
    # ---------------------------------

    halted = check_halt(pos, q)

    # ---------------------------------
    # نقطة التعادل +30%
    # ---------------------------------

    if (
        gain >= BREAKEVEN_TRIGGER
        and not pos["breakeven_active"]
    ):

        pos["stop"] = max(
            pos["stop"],
            entry
        )

        pos["breakeven_active"] = True

        log.info(
            "🛡️ BREAKEVEN ACTIVATED "
            f"| Stop={pos['stop']:.2f}"
        )

    # ---------------------------------
    # تفعيل ملاحقة القمة +50%
    # ---------------------------------

    if (
        peak_gain >= TRAIL_ACTIVATION
        and not pos["trail_active"]
    ):

        pos["trail_active"] = True

        log.info(
            "🚀 SMART PROFIT RUNNER ACTIVATED "
            f"| Peak Gain={peak_gain * 100:.1f}%"
        )

    # ---------------------------------
    # تحديث الوقف المتحرك
    # ---------------------------------

    if pos["trail_active"]:

        trailing_stop = (
            pos["peak"] * (1 - TRAIL_DISTANCE)
        )

        new_stop = max(
            entry,
            trailing_stop,
            pos["stop"]
        )

        if new_stop > pos["stop"]:

            pos["stop"] = new_stop

            log.info(
                f"🔒 TRAILING STOP UPDATED "
                f"| Peak={pos['peak']:.2f} "
                f"| Stop={pos['stop']:.2f} "
                f"| Protected Gain="
                f"{(pos['stop'] / entry - 1) * 100:.1f}%"
            )

    # ---------------------------------
    # عكس VWAP قبل تفعيل ملاحقة القمة
    # ---------------------------------

    vwap_exit = False

    if not pos["trail_active"]:

        bars = session_bars()

        if bars:

            vw = vwap_of(bars)

            close = float(bars[-1]["c"])

            if vw:

                if (
                    pos["direction"] == "up"
                    and close < vw
                ):

                    vwap_exit = True

                elif (
                    pos["direction"] == "down"
                    and close > vw
                ):

                    vwap_exit = True

    # ---------------------------------
    # تحديد سبب الخروج
    # ---------------------------------

    reason = None

    if eod:

        reason = "END OF DAY"

    elif halted:

        reason = "DAILY LOSS LIMIT"

    elif bid > 0 and bid <= pos["stop"]:

        if pos["trail_active"]:

            reason = "SMART TRAILING PROFIT"

        else:

            reason = "STOP LOSS / BREAKEVEN"

    elif vwap_exit:

        reason = "VWAP REVERSAL"

    # ---------------------------------
    # تنفيذ الخروج
    # ---------------------------------

    if reason and pos["qty_open"] > 0:

        log.warning(
            f"🔻 EXIT SIGNAL: {reason} "
            f"| Bid={bid:.2f} "
            f"| Entry={entry:.2f} "
            f"| Peak={pos['peak']:.2f}"
        )

        if sell(
            pos,
            pos["qty_open"],
            reason
        ):

            if pos["qty_open"] <= 0:

                finish_trade(reason)

                return

    save_state()


# =========================================================
# 11. اليوم الجديد
# =========================================================

def roll_day():

    global state

    if state.get("date") != today_str():

        eq = get_equity()

        if eq <= 0:
            return False

        old_pos = state.get("position")

        # لا نتجاهل مركزاً مفتوحاً من اليوم السابق
        if old_pos:

            state["halted"] = True

            log.error(
                "⚠️ يوجد مركز مفتوح من يوم سابق. "
                "تم منع فتح صفقات جديدة حتى معالجة المركز."
            )

            save_state()

            return True

        state = new_day_state(eq)

        save_state()

        log.info(
            f"📅 يوم جديد "
            f"| رصيد البداية={eq:,.2f}$"
        )

    return True


# =========================================================
# 12. الحلقة الرئيسية
# =========================================================

def run():

    if not API_KEY or not API_SECRET:

        log.error(
            "ALPACA_API_KEY / ALPACA_API_SECRET "
            "غير مضبوطة"
        )

        sys.exit(1)

    if (
        "paper" not in TRADING_URL
        and not ALLOW_LIVE
    ):

        log.error(
            "الرابط ليس Paper. "
            "التشغيل الحقيقي غير مسموح."
        )

        sys.exit(1)

    log.info(
        f"🚀 {NAME} {VERSION} "
        f"| {'PAPER' if 'paper' in TRADING_URL else 'LIVE'} "
        f"| Stock={STOCK_FEED} "
        f"| Options={OPTIONS_FEED}"
    )

    log.info(
        "💎 SMART PROFIT RUNNER "
        f"| Breakeven=+{BREAKEVEN_TRIGGER * 100:.0f}% "
        f"| Activation=+{TRAIL_ACTIVATION * 100:.0f}% "
        f"| Trailing={TRAIL_DISTANCE * 100:.0f}%"
    )

    while True:

        try:

            if not market_clock():

                time.sleep(30)

                continue

            if not roll_day():

                time.sleep(15)

                continue

            # إدارة الصفقة المفتوحة أولاً
            manage_position()

            # فحص حد الخسارة
            check_halt()

            # تحديث الميل
            ensure_bias()

            # تقييم الميل
            evaluate_bias()

            t = now_ny().time()

            can_enter = (
                state.get("position") is None
                and not state.get("halted", False)
                and state.get("trades_today", 0)
                < MAX_TRADES_PER_DAY
                and SIGNAL_START <= t <= SIGNAL_END
                and state.get("bias") in ("bull", "bear")
            )

            if can_enter:

                try_entry()

            time.sleep(5)

        except KeyboardInterrupt:

            log.info("تم إيقاف البوت يدوياً")

            break

        except Exception as e:

            log.error(
                f"خطأ غير متوقع: {e}",
                exc_info=True
            )

            time.sleep(10)


if __name__ == "__main__":

    run()
