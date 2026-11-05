# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v113.0
SPY 0-DTE | PAPER FIRST | DIAGNOSTIC EXECUTION
=========================================================
"""

import os, sys, json, time, uuid, math, logging, hashlib, threading
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import requests

# =========================================================
# 1) CONFIGURATION
# =========================================================
VERSION = "113.0"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR / "safaa_v113_state.json"
LEDGER_FILE = BASE_DIR / "safaa_v113_ledger.json"
INIT_FILE = BASE_DIR / "safaa_v113_initialized.flag"
LOCK_FILE = BASE_DIR / "safaa_v113.lock"

API_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
TRADING_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
OPTIONS_DATA_URL = os.getenv("APCA_OPTIONS_DATA_URL", "https://data.alpaca.markets").rstrip("/")

ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
CONFIRM_LIVE = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"
UNDERLYING = "SPY"
PAPER_REQUIRED = not (ALLOW_LIVE and CONFIRM_LIVE)

# Risk controls
RISK_PER_TRADE = 0.02
MAX_DAILY_DRAWDOWN = 0.03
MAX_PREMIUM_FRACTION = 0.02
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 4
MAX_CONSECUTIVE_LOSSES = 2

# Contract selection
TARGET_DELTA_MIN, TARGET_DELTA_MAX, TARGET_DELTA = 0.40, 0.65, 0.52
MAX_SPREAD_PERCENT = 0.12
MIN_OPTION_ASK, MAX_OPTION_ASK = 0.20, 8.00

# Position management
STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT = 0.30, 0.60
TRAIL_ACTIVATION_PERCENT, TRAIL_DISTANCE_PERCENT = 0.35, 0.18

# Trading hours - New York
ENTRY_START, ENTRY_END = (9, 45), (14, 0)
FORCE_EXIT_TIME = (15, 40)
SCAN_SECONDS, REQUEST_TIMEOUT, MAX_API_RETRIES = 20, 15, 3

# Execution safety
MAX_ENTRY_SLIPPAGE_PERCENT = 0.08
ORDER_LOOKUP_WAIT_SECONDS, RECONCILIATION_INTERVAL = 3, 20
FILL_SYNC_GRACE_SECONDS, FILL_SYNC_RETRIES = 12, 3

# Strategy
FAST_EMA, SLOW_EMA = 9, 21
RSI_PERIOD, VOLUME_LOOKBACK, MIN_SIGNAL_SCORE = 14, 20, 3

# Market data
MIN_SPY_BARS, SPY_DATA_RETRIES, SPY_RETRY_DELAY = 40, 3, 2.5
SPY_MAX_BAR_AGE_SECONDS, SPY_LOOKBACK_HOURS = 180, 6

# Diagnostics & Safety
DIAGNOSTICS_ENABLED, LOG_REJECTED_CONTRACTS = True, True
MAX_CONTRACT_DIAGNOSTIC_LINES = 30
REQUIRE_SINGLE_INSTANCE = True

# =========================================================
# 2) LOGGING
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(BASE_DIR / "safaa_v113.log", encoding="utf-8")]
)
log = logging.getLogger("SAFAA_V113")

def diagnostic(stage, message, *args):
    if DIAGNOSTICS_ENABLED: log.info(f"[DIAGNOSTIC:{stage}] " + message, *args)

def reject(stage, reason, *args):
    log.warning(f"[REJECT:{stage}] " + reason, *args)

# =========================================================
# 3) BASIC VALIDATION
# =========================================================
if not API_KEY or not API_SECRET: raise RuntimeError("مفاتيح Alpaca غير موجودة. أضف APCA_API_KEY_ID و APCA_API_SECRET_KEY.")
if ALLOW_LIVE and not CONFIRM_LIVE: raise RuntimeError("تم طلب Live دون تأكيد مستقل.")
if not ALLOW_LIVE and "paper" not in TRADING_URL.lower(): raise RuntimeError("إعداد Paper مطلوب. عنوان التداول لا يبدو Paper.")
if ALLOW_LIVE and CONFIRM_LIVE: log.critical("LIVE TRADING ENABLED. REAL MONEY IS AT RISK.")
if REQUIRE_SINGLE_INSTANCE: log.warning("يجب تشغيل نسخة واحدة فقط من البوت على الحساب.")

# =========================================================
# 4) HTTP SESSION
# =========================================================
SESSION = requests.Session()
SESSION.headers.update({"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET, "Content-Type": "application/json"})

class APIError(Exception): pass
class SafetyHalt(Exception): pass
class MarketDataUnavailable(Exception): pass

def api_request(method, url, params=None, payload=None, timeout=REQUEST_TIMEOUT):
    method = method.upper()
    attempts = 1 if method == "POST" else MAX_API_RETRIES
    last_error = None
    for attempt in range(attempts):
        try:
            response = SESSION.request(method=method, url=url, params=params, json=payload, timeout=timeout)
            if response.status_code >= 400:
                detail = response.json() if response.text else response.text[:1000]
                raise APIError(f"{method} {url} HTTP {response.status_code}: {detail}")
            return response.json() if response.text else {}
        except APIError: raise
        except requests.RequestException as exc:
            last_error = exc
            if method == "POST": raise APIError(f"نتيجة POST غير مؤكدة. لن تتم إعادة إرسال الأمر تلقائياً. {exc}")
            if attempt + 1 < attempts: time.sleep(1.5 * (attempt + 1))
    raise APIError(str(last_error))

def trading_api(method, path, params=None, payload=None): return api_request(method, f"{TRADING_URL}{path}", params=params, payload=payload)
def data_api(method, path, params=None): return api_request(method, f"{DATA_URL}{path}", params=params)
def options_data_api(method, path, params=None): return api_request(method, f"{OPTIONS_DATA_URL}{path}", params=params)

# =========================================================
# 5) TIME / STORAGE HELPERS
# =========================================================
def utc_now(): return datetime.now(UTC).isoformat()
def ny_now(): return datetime.now(NY)
def today_ny(): return ny_now().date().isoformat()
def make_id(prefix): return f"{prefix}-{uuid.uuid4().hex[:20]}"
def sha256_text(value): return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

def atomic_write_json(path, data):
    temp_path = Path(str(path) + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)

def read_json(path):
    with open(path, "r", encoding="utf-8") as file: return json.load(file)

# =========================================================
# 6) INSTANCE LOCK
# =========================================================
_INSTANCE_LOCK_HANDLE = None
def acquire_instance_lock():
    global _INSTANCE_LOCK_HANDLE
    try: import fcntl
    except ImportError: raise SafetyHalt("تعذر استخدام قفل النظام.")
    _INSTANCE_LOCK_HANDLE = open(LOCK_FILE, "a+", encoding="utf-8")
    try: fcntl.flock(_INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SafetyHalt("هناك نسخة أخرى تعمل على المسار نفسه.")
    _INSTANCE_LOCK_HANDLE.seek(0)
    _INSTANCE_LOCK_HANDLE.truncate()
    _INSTANCE_LOCK_HANDLE.write(str(os.getpid()))
    _INSTANCE_LOCK_HANDLE.flush()

# =========================================================
# 7) STATE AND LEDGER
# =========================================================
def default_state():
    return {
        "version": VERSION, "created_at": utc_now(), "updated_at": utc_now(), "halted": False, "halt_reason": None,
        "trading_day": today_ny(), "starting_equity": None, "daily_pnl": 0.0, "trades_today": 0, "consecutive_losses": 0,
        "last_scan_candle": None, "last_reconciliation": None, "candle_orders": {}, "active_client_order_id": None,
        "last_known_positions": {}, "last_known_orders": {}, "position_meta": {}
    }

def default_ledger(): return {"version": VERSION, "orders": {}, "fills": {}, "events": []}

STATE, LEDGER = None, None
STATE_MUTEX = threading.RLock()

def save_state():
    with STATE_MUTEX:
        STATE["updated_at"] = utc_now()
        atomic_write_json(STATE_FILE, STATE)

def save_ledger():
    with STATE_MUTEX: atomic_write_json(LEDGER_FILE, LEDGER)

def halt_bot(reason):
    STATE["halted"], STATE["halt_reason"] = True, str(reason)
    save_state()
    log.critical("TRADING HALTED: %s", reason)

def ensure_not_halted():
    if STATE.get("halted"): raise SafetyHalt(STATE.get("halt_reason") or "البوت متوقف لأسباب السلامة.")

def initialize_storage():
    global STATE, LEDGER
    if INIT_FILE.exists():
        if not STATE_FILE.exists(): raise SafetyHalt("ملف الحالة مفقود.")
        if not LEDGER_FILE.exists(): raise SafetyHalt("دفتر الصفقات مفقود.")
        try:
            STATE, LEDGER = read_json(STATE_FILE), read_json(LEDGER_FILE)
        except Exception as exc: raise SafetyHalt(f"تعذر قراءة الملفات: {exc}")
        if not isinstance(STATE, dict) or not isinstance(LEDGER, dict) or "orders" not in LEDGER or "fills" not in LEDGER:
            raise SafetyHalt("بنية الملفات غير صالحة أو غير مكتملة.")
        for key, value in default_state().items():
            if key not in STATE: STATE[key] = value
        STATE["version"] = VERSION
        save_state()
        log.info("تم تحميل الحالة والدفتر السابقين.")
        return

    account = trading_api("GET", "/v2/account")
    if account.get("status") != "ACTIVE": raise SafetyHalt(f"حالة الحساب ليست ACTIVE: {account.get('status')}")
    STATE, LEDGER = default_state(), default_ledger()
    STATE["starting_equity"] = float(account.get("equity", 0) or 0)
    save_state()
    save_ledger()
    with open(INIT_FILE, "w", encoding="utf-8") as file:
        file.write(f"Initialized {utc_now()} | version={VERSION}\n")
        file.flush()
        os.fsync(file.fileno())
    log.info("تم إنشاء الحالة لأول مرة. Equity=%.2f", STATE["starting_equity"])

# =========================================================
# 8) ACCOUNT HELPERS
# =========================================================
def get_account(): return trading_api("GET", "/v2/account")
def get_account_equity():
    equity = float(get_account().get("equity", 0) or 0)
    if not math.isfinite(equity) or equity <= 0: raise SafetyHalt("قيمة Equity غير صالحة.")
    return equity
def get_clock(): return trading_api("GET", "/v2/clock")
def market_is_open(): return bool(get_clock().get("is_open", False))

def refresh_daily_baseline():
    current_day = today_ny()
    if STATE.get("trading_day") == current_day: return
    if STATE.get("active_client_order_id"): raise SafetyHalt("تغير يوم التداول مع وجود أمر غير محسوم.")
    equity = get_account_equity()
    STATE.update({"trading_day": current_day, "starting_equity": equity, "daily_pnl": 0.0, "trades_today": 0, "consecutive_losses": 0})
    save_state()
    log.info("بداية يوم تداول جديد. Equity=%.2f", equity)

def check_daily_risk():
    ensure_not_halted()
    start_equity = float(STATE.get("starting_equity") or 0)
    if start_equity <= 0: raise SafetyHalt("قيمة رأس المال المرجعية غير صالحة.")
    current_equity = get_account_equity()
    pnl = current_equity - start_equity
    STATE["daily_pnl"] = pnl
    save_state()
    max_loss = start_equity * MAX_DAILY_DRAWDOWN
    if pnl <= -max_loss:
        halt_bot(f"تم بلوغ حد الخسارة اليومية: {pnl:.2f} USD")
        raise SafetyHalt("تجاوز حد الخسارة اليومية.")
    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        halt_bot("تم بلوغ حد الخسائر المتتالية.")
        raise SafetyHalt("توقف بعد خسائر متتالية.")

# =========================================================
# 9) MARKET DATA
# =========================================================
def get_spy_bars(limit=120):
    last_error = None
    feed = os.getenv("ALPACA_STOCK_FEED", "iex").strip().lower()
    if feed not in ("iex", "sip", "boats", "otc"): raise MarketDataUnavailable(f"مصدر بيانات غير صالح: {feed}")
    
    for attempt in range(1, SPY_DATA_RETRIES + 1):
        try:
            request_now = datetime.now(UTC)
            params = {
                "timeframe": "1Min", "limit": int(limit), "adjustment": "raw",
                "feed": feed, "sort": "desc",
                "start": (request_now - timedelta(hours=SPY_LOOKBACK_HOURS)).isoformat(),
                "end": request_now.isoformat()
            }
            bars = data_api("GET", f"/v2/stocks/{UNDERLYING}/bars", params=params).get("bars", [])
            if not isinstance(bars, list) or not bars: raise MarketDataUnavailable("لم تصل شموع SPY.")
            
            normalized, seen, completed = [], set(), []
            now = datetime.now(UTC)
            
            for bar in bars:
                try:
                    timestamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).replace(tzinfo=UTC)
                    candle_id = timestamp.isoformat()
                    if candle_id in seen: continue
                    seen.add(candle_id)
                    values = {"open": float(bar["o"]), "high": float(bar["h"]), "low": float(bar["l"]), "close": float(bar["c"]), "volume": float(bar["v"])}
                    if not all(math.isfinite(v) for v in values.values()) or min(values.values()) <= 0 or values["high"] < values["low"] or values["volume"] < 0: continue
                    normalized.append({"timestamp": bar["t"], "_datetime": timestamp, **values})
                except Exception: continue
                
            normalized.sort(key=lambda x: x["_datetime"])
            for bar in normalized:
                if bar["_datetime"] + timedelta(minutes=1) <= now: completed.append(bar)
                
            if len(completed) < MIN_SPY_BARS: raise MarketDataUnavailable(f"شموع غير كافية: {len(completed)}/{MIN_SPY_BARS}")
            age = (now - (completed[-1]["_datetime"] + timedelta(minutes=1))).total_seconds()
            if age < -5: raise MarketDataUnavailable("توقيت الشمعة يقع في المستقبل.")
            if age > SPY_MAX_BAR_AGE_SECONDS: raise MarketDataUnavailable(f"بيانات SPY قديمة: {age:.1f}s")
            
            for bar in completed: bar.pop("_datetime", None)
            log.info("SPY DATA OK | Feed=%s | Candles=%s | Last=%s | Age=%.1fs | Attempt=%s/%s", feed, len(completed), completed[-1]["timestamp"], age, attempt, SPY_DATA_RETRIES)
            return completed
            
        except (MarketDataUnavailable, APIError) as exc:
            last_error = exc
            log.warning("SPY DATA RETRY %s/%s: %s", attempt, SPY_DATA_RETRIES, exc)
        except Exception as exc:
            last_error = exc
            log.exception("خطأ غير متوقع في بيانات SPY.")
        if attempt < SPY_DATA_RETRIES: time.sleep(SPY_RETRY_DELAY * attempt)
    raise MarketDataUnavailable(f"تعذر جلب بيانات SPY. آخر خطأ: {last_error}")

# =========================================================
# 10) TECHNICAL INDICATORS
# =========================================================
def ema(values, period):
    if len(values) < period: return None
    multiplier = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]: current = (value * multiplier) + (current * (1.0 - multiplier))
    return current

def calculate_rsi(values, period=RSI_PERIOD):
    if len(values) <= period: return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def calculate_vwap(bars):
    cumulative_pv, cumulative_volume = 0.0, 0.0
    for bar in bars:
        tp = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        vol = max(bar["volume"], 0)
        cumulative_pv += tp * vol
        cumulative_volume += vol
    return (cumulative_pv / cumulative_volume) if cumulative_volume > 0 else None

def average(values): return sum(values) / len(values) if values else None

def calculate_atr(bars, period=14):
    if len(bars) <= period: return None
    ranges = []
    for i in range(1, len(bars)):
        curr, prev = bars[i], bars[i - 1]
        ranges.append(max(curr["high"] - curr["low"], abs(curr["high"] - prev["close"]), abs(curr["low"] - prev["close"])))
    return average(ranges[-period:])

def calculate_market_indicators(bars):
    closes, volumes = [b["close"] for b in bars], [b["volume"] for b in bars]
    fast, slow, rsi, vwap, atr = ema(closes, FAST_EMA), ema(closes, SLOW_EMA), calculate_rsi(closes), calculate_vwap(bars), calculate_atr(bars)
    if None in (fast, slow, rsi, vwap, atr): raise APIError("تعذر حساب المؤشرات الفنية.")
    last, prev = bars[-1], bars[-2]
    avg_vol = average(volumes[-(VOLUME_LOOKBACK + 1):-1])
    volume_ratio = (last["volume"] / avg_vol) if avg_vol and avg_vol > 0 else 0.0
    return {"price": last["close"], "previous_close": prev["close"], "fast_ema": fast, "slow_ema": slow, "rsi": rsi, "vwap": vwap, "atr": atr, "volume_ratio": volume_ratio, "timestamp": last["timestamp"]}

# =========================================================
# 11) MARKET SIGNAL
# =========================================================
def get_market_signal(ind):
    p, f, s, r, v, prev, vr = ind["price"], ind["fast_ema"], ind["slow_ema"], ind["rsi"], ind["vwap"], ind["previous_close"], ind["volume_ratio"]
    bull, bear = 0, 0
    if f > s: bull += 1
    elif f < s: bear += 1
    if p > v: bull += 1
    elif p < v: bear += 1
    if p > prev: bull += 1
    elif p < prev: bear += 1
    if 50 <= r <= 68: bull += 1
    elif 32 <= r < 50: bear += 1
    if vr >= 1.10:
        if p > prev: bull += 1
        elif p < prev: bear += 1

    if bull >= MIN_SIGNAL_SCORE and bull > bear: return {"direction": "CALL", "score": bull, "bullish_score": bull, "bearish_score": bear}
    elif bear >= MIN_SIGNAL_SCORE and bear > bull: return {"direction": "PUT", "score": bear, "bullish_score": bull, "bearish_score": bear}
    return {"direction": "NEUTRAL", "score": max(bull, bear), "bullish_score": bull, "bearish_score": bear}

# =========================================================
# 12) CANDLE / TIME HELPERS
# =========================================================
def make_candle_id(timestamp): return sha256_text(f"{UNDERLYING}|{timestamp}")[:24]
def entry_time_allowed():
    now = ny_now()
    if now.weekday() >= 5: return False
    return (ENTRY_START[0] * 60 + ENTRY_START[1]) <= (now.hour * 60 + now.minute) <= (ENTRY_END[0] * 60 + ENTRY_END[1])
def force_exit_time_reached(): return (ny_now().hour * 60 + ny_now().minute) >= (FORCE_EXIT_TIME[0] * 60 + FORCE_EXIT_TIME[1])

# =========================================================
# 13-25) OPTIONS HANDLING (Combined logic for brevity)
# =========================================================
def get_today_expiration(): return today_ny()
def get_option_contracts(option_type):
    return trading_api("GET", "/v2/options/contracts", params={"underlying_symbols": UNDERLYING, "status": "active", "expiration_date": get_today_expiration(), "type": option_type, "limit": 1000}).get("option_contracts", [])

def get_option_snapshots(symbols):
    snapshots = {}
    for start in range(0, len(symbols), 50):
        data = options_data_api("GET", "/v1beta1/options/snapshots", params={"symbols": ",".join(symbols[start:start + 50])}).get("snapshots", {})
        if not isinstance(data, dict): raise APIError("استجابة snapshots غير صالحة.")
        snapshots.update(data)
    return snapshots

def safe_float(value):
    try:
        res = float(value)
        return res if math.isfinite(res) else None
    except (TypeError, ValueError): return None

def parse_market_timestamp(value):
    if not value: return None
    try: return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=UTC).astimezone(UTC)
    except Exception: return None

def extract_quote_and_delta(snapshot):
    q = snapshot.get("latestQuote", {}) if isinstance(snapshot.get("latestQuote"), dict) else {}
    g = snapshot.get("greeks", {}) if isinstance(snapshot.get("greeks"), dict) else {}
    return {"bid": safe_float(q.get("bp")), "ask": safe_float(q.get("ap")), "bid_size": safe_float(q.get("bs")), "ask_size": safe_float(q.get("as")), "timestamp": parse_market_timestamp(q.get("t")), "delta": safe_float(g.get("delta"))}

def quote_freshness(quote):
    ts = quote.get("timestamp")
    if not ts: return False, None, "QUOTE_TIMESTAMP_MISSING"
    age = (datetime.now(UTC) - ts).total_seconds()
    if age < -5: return False, age, "QUOTE_TIMESTAMP_FUTURE"
    if age > float(os.getenv("MAX_OPTION_QUOTE_AGE_SECONDS", "60")): return False, age, "QUOTE_STALE"
    return True, age, "QUOTE_FRESH"

def validate_contract_metadata(contract, option_type):
    if not contract.get("symbol"): return False, "SYMBOL_MISSING"
    if str(contract.get("type", "")).lower() not in (option_type, ""): return False, "WRONG_TYPE"
    if str(contract.get("expiration_date", "")) != get_today_expiration(): return False, "WRONG_EXPIRATION"
    if str(contract.get("status", "active")).lower() not in ("active", ""): return False, "CONTRACT_NOT_ACTIVE"
    return True, "OK"

def calculate_spread_percent(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid: return None
    return (ask - bid) / ((bid + ask) / 2.0)

def discover_option_candidates(option_type):
    stats = {k: 0 for k in ["discovered", "snapshot_missing", "quote_missing", "quote_stale", "invalid_bid", "invalid_ask", "ask_below_min", "ask_above_max", "spread_too_wide", "delta_missing", "delta_outside_range", "wrong_type", "wrong_expiration", "passed", "budget_rejected"]}
    contracts = get_option_contracts(option_type)
    if not contracts: return [], stats
    
    valid_contracts = []
    for c in contracts:
        v, r = validate_contract_metadata(c, option_type)
        if v: valid_contracts.append(c)
        elif r == "WRONG_TYPE": stats["wrong_type"] += 1
        elif r == "WRONG_EXPIRATION": stats["wrong_expiration"] += 1
            
    symbols = [c["symbol"] for c in valid_contracts]
    if not symbols: return [], stats
    
    snapshots, candidates = get_option_snapshots(symbols), []
    for contract in valid_contracts:
        stats["discovered"] += 1
        snapshot = snapshots.get(contract["symbol"])
        if not snapshot: stats["snapshot_missing"] += 1; continue
        qd = extract_quote_and_delta(snapshot)
        bid, ask, delta = qd["bid"], qd["ask"], qd["delta"]
        
        if bid is None or ask is None: stats["quote_missing"] += 1; continue
        fresh, age, r_fresh = quote_freshness(qd)
        if not fresh: stats["quote_stale"] += 1; continue
        if bid <= 0: stats["invalid_bid"] += 1; continue
        if ask <= 0 or ask < bid: stats["invalid_ask"] += 1; continue
        if ask < MIN_OPTION_ASK: stats["ask_below_min"] += 1; continue
        if ask > MAX_OPTION_ASK: stats["ask_above_max"] += 1; continue
        
        spread = calculate_spread_percent(bid, ask)
        if spread is None or spread > MAX_SPREAD_PERCENT: stats["spread_too_wide"] += 1; continue
        if delta is None: stats["delta_missing"] += 1; continue
        if not (TARGET_DELTA_MIN <= abs(delta) <= TARGET_DELTA_MAX): stats["delta_outside_range"] += 1; continue
        if qd["bid_size"] is not None and qd["bid_size"] <= 0: continue
        if qd["ask_size"] is not None and qd["ask_size"] <= 0: continue
        
        stats["passed"] += 1
        candidates.append({"symbol": contract["symbol"], "type": option_type, "expiration": contract.get("expiration_date"), "strike": safe_float(contract.get("strike_price")), "bid": bid, "ask": ask, "spread_percent": spread, "delta": delta, "absolute_delta": abs(delta), "quote_age_seconds": age, "bid_size": qd["bid_size"], "ask_size": qd["ask_size"], "snapshot": snapshot})
        
    candidates.sort(key=lambda c: (abs(c["absolute_delta"] - TARGET_DELTA) * 0.65) + ((c["spread_percent"] / MAX_SPREAD_PERCENT) * 0.35))
    return candidates, stats

def calculate_entry_budget():
    eq = get_account_equity()
    return {"equity": eq, "risk_budget": eq * RISK_PER_TRADE, "premium_budget": eq * MAX_PREMIUM_FRACTION, "allowed_budget": min(eq * RISK_PER_TRADE, eq * MAX_PREMIUM_FRACTION)}

def calculate_contract_quantity(candidate, budget):
    if candidate["ask"] <= 0: return 0
    cost = candidate["ask"] * 100.0
    qty = int(math.floor(budget["allowed_budget"] / cost))
    return qty if (qty >= 1 and qty * cost <= budget["allowed_budget"] + 0.000001) else 0

def select_option_contract(option_type):
    candidates, stats = discover_option_candidates(option_type)
    if not candidates: return None
    budget = calculate_entry_budget()
    for c in candidates:
        qty = calculate_contract_quantity(c, budget)
        if qty > 0:
            c.update({"quantity": qty, "estimated_cost": c["ask"] * 100.0 * qty, "budget": budget})
            log.info("SELECTED CONTRACT | %s | Bid=%.2f Ask=%.2f Delta=%.3f Qty=%s", c["symbol"], c["bid"], c["ask"], c["delta"], qty)
            return c
        stats["budget_rejected"] += 1
    return None

def refresh_selected_contract(candidate):
    snapshot = get_option_snapshots([candidate["symbol"]]).get(candidate["symbol"])
    if not snapshot: raise APIError("تعذر تحديث Snapshot للعقد")
    qd = extract_quote_and_delta(snapshot)
    fresh, age, r = quote_freshness(qd)
    if not fresh or qd["bid"] is None or qd["ask"] is None or qd["ask"] < qd["bid"]: raise APIError("بيانات العقد غير صالحة قبل الدخول.")
    spread = calculate_spread_percent(qd["bid"], qd["ask"])
    if spread is None or spread > MAX_SPREAD_PERCENT: raise APIError("اتساع السبريد قبل الدخول.")
    slippage = (qd["ask"] - candidate["ask"]) / candidate["ask"]
    if slippage > MAX_ENTRY_SLIPPAGE_PERCENT: raise APIError(f"ارتفاع سعر الدخول: {slippage * 100:.2f}%")
    if qd["delta"] is None or not (TARGET_DELTA_MIN <= abs(qd["delta"]) <= TARGET_DELTA_MAX): raise APIError("Delta خارج النطاق")
    
    candidate.update({"bid": qd["bid"], "ask": qd["ask"], "delta": qd["delta"], "absolute_delta": abs(qd["delta"]), "spread_percent": spread, "quote_age_seconds": age, "snapshot": snapshot})
    qty = calculate_contract_quantity(candidate, candidate["budget"])
    if qty <= 0: raise APIError("السعر المحدث لا يسمح بشراء عقد.")
    candidate.update({"quantity": qty, "estimated_cost": qd["ask"] * 100.0 * qty})
    return candidate

# =========================================================
# 26-31) ORDER MANAGEMENT
# =========================================================
def get_orders(status="open", limit=500): return trading_api("GET", "/v2/orders", params={"status": status, "limit": limit, "direction": "desc", "nested": "true"})
def get_positions(): return trading_api("GET", "/v2/positions")
def get_order_by_id(order_id): return trading_api("GET", f"/v2/orders/{order_id}")
def get_order_by_client_id(cid): return trading_api("GET", "/v2/orders:by_client_order_id", params={"client_order_id": cid})

def ledger_event(event_type, data=None):
    LEDGER.setdefault("events", []).append({"id": make_id("EVT"), "type": event_type, "timestamp": utc_now(), "data": data or {}})
    if len(LEDGER["events"]) > 10000: LEDGER["events"] = LEDGER["events"][-10000:]
    save_ledger()

def register_order_intent(cid, symbol, qty, side, price, signal):
    if cid in LEDGER["orders"]: raise SafetyHalt("محاولة تسجيل client_order_id مكرر.")
    intent = {"client_order_id": cid, "broker_order_id": None, "symbol": symbol, "quantity": int(qty), "side": side, "limit_price": float(price), "signal": signal, "status": "INTENT_CREATED", "created_at": utc_now(), "updated_at": utc_now(), "filled_qty": 0.0, "filled_avg_price": None, "submitted": False, "terminal": False, "submission_unknown": False, "last_broker_status": None, "last_error": None}
    LEDGER["orders"][cid] = intent
    save_ledger()
    ledger_event("ORDER_INTENT_CREATED", {"client_order_id": cid, "symbol": symbol, "quantity": qty, "side": side})
    return intent

def update_order_record(cid, **updates):
    record = LEDGER["orders"].get(cid)
    if not record: raise SafetyHalt("سجل الأمر غير موجود.")
    record.update(updates)
    record["updated_at"] = utc_now()
    save_ledger()
    return record

TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected"}
def order_is_terminal(status): return str(status).lower() in TERMINAL_ORDER_STATUSES
def normalize_order(order): return {"id": order.get("id"), "client_order_id": order.get("client_order_id"), "symbol": order.get("symbol"), "status": str(order.get("status", "")).lower(), "qty": safe_float(order.get("qty")) or 0.0, "filled_qty": safe_float(order.get("filled_qty")) or 0.0, "filled_avg_price": safe_float(order.get("filled_avg_price")), "side": order.get("side"), "limit_price": safe_float(order.get("limit_price")), "submitted_at": order.get("submitted_at")}

def apply_broker_order_update(order):
    norm = normalize_order(order)
    cid = norm["client_order_id"]
    if not cid: raise SafetyHalt("أمر لدى الوسيط بلا client_order_id.")
    record = LEDGER["orders"].get(cid)
    if not record: raise SafetyHalt(f"أمر غير مسجل: {cid}")
    if norm["filled_qty"] + 0.000001 < float(record.get("filled_qty", 0) or 0): raise SafetyHalt(f"تراجع كمية التنفيذ: {cid}")
    record.update({"broker_order_id": norm["id"], "status": norm["status"], "last_broker_status": norm["status"], "filled_qty": norm["filled_qty"], "filled_avg_price": norm["filled_avg_price"], "submitted": True, "terminal": order_is_terminal(norm["status"]), "submission_unknown": False, "updated_at": utc_now()})
    save_ledger()
    return norm

def create_client_order_id(symbol, candle_id): return f"SF113-{sha256_text(f'{VERSION}|{symbol}|{candle_id}')[:18]}"
def active_entry_intent_exists(): return any(r.get("side") == "buy" and (r.get("submission_unknown") or not r.get("terminal", False) or r.get("status", "").lower() in ("new", "accepted", "pending_new", "partially_filled")) for r in LEDGER["orders"].values())

def lookup_order_after_submit(cid):
    last_error = None
    for attempt in range(ORDER_LOOKUP_WAIT_SECONDS):
        try:
            o = get_order_by_client_id(cid)
            if o and o.get("id"): return o
        except Exception as exc: last_error = exc
        time.sleep(1)
    log.warning("ORDER LOOKUP INCONCLUSIVE | ClientID=%s | LastError=%s", cid, last_error)
    return None

def submit_limit_order_once(candidate, signal):
    ensure_not_halted()
    if active_entry_intent_exists(): raise SafetyHalt("يوجد أمر شراء سابق غير محسوم.")
    sym, qty, limit_price = candidate["symbol"], int(candidate["quantity"]), round(float(candidate["ask"]), 2)
    if qty <= 0 or limit_price <= 0 or (limit_price * 100 * qty) > candidate["budget"]["allowed_budget"] + 0.000001: raise APIError("قيم العقد غير صالحة للميزانية.")
    cid = create_client_order_id(sym, make_candle_id(candidate.get("signal_timestamp", utc_now())))
    if cid in LEDGER["orders"]: raise SafetyHalt("تم تسجيل نية سابقة.")
    
    register_order_intent(cid, sym, qty, "buy", limit_price, signal)
    STATE["active_client_order_id"] = cid
    save_state()
    
    payload = {"symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "time_in_force": "day", "limit_price": f"{limit_price:.2f}", "client_order_id": cid}
    log.info("SUBMITTING BUY | %s | Qty=%s | Limit=$%.2f | ClientID=%s", sym, qty, limit_price, cid)
    try:
        res = trading_api("POST", "/v2/orders", payload=payload)
        if not res.get("id"): raise APIError("لا يوجد ID")
        apply_broker_order_update(res)
        ledger_event("ORDER_SUBMITTED", {"client_order_id": cid, "broker_order_id": res.get("id")})
        return res
    except APIError as exc:
        update_order_record(cid, status="SUBMISSION_UNRESOLVED", submission_unknown=True, last_error=str(exc))
        found = lookup_order_after_submit(cid)
        if found: return apply_broker_order_update(found)
        halt_bot(f"نتيجة إرسال الأمر غير محسومة: {cid}")
        raise SafetyHalt("لم نتمكن من تأكيد حالة الأمر.")
    except Exception as exc:
        update_order_record(cid, status="SUBMISSION_UNRESOLVED", submission_unknown=True, last_error=str(exc))
        halt_bot(f"خطأ إرسال: {exc}")
        raise SafetyHalt("تم إيقاف التداول لحماية الحساب.")

def refresh_order_status(cid):
    r = LEDGER["orders"].get(cid)
    if not r: raise SafetyHalt("لا يوجد سجل للأمر.")
    try:
        o = get_order_by_id(r["broker_order_id"]) if r.get("broker_order_id") else get_order_by_client_id(cid)
        if not o: return None
        return apply_broker_order_update(o)
    except Exception: return None

# =========================================================
# 34-38) FILLS & RECONCILIATION
# =========================================================
def fetch_fill_activities(after=None, max_pages=10):
    acts, params = [], {"activity_types": "FILL", "direction": "asc", "page_size": 100}
    if after: params["after"] = after
    for _ in range(max_pages):
        res = trading_api("GET", "/v2/account/activities", params=params)
        if not res: break
        acts.extend(res)
        if len(res) < 100 or not res[-1].get("id"): break
        params["page_token"] = res[-1].get("id")
    return acts

def apply_fill_activity(act):
    aid = str(act.get("id", ""))
    if not aid or aid in LEDGER["fills"]: return False
    cid = act.get("client_order_id") or next((k for k, v in LEDGER["orders"].items() if v.get("broker_order_id") == act.get("order_id")), None)
    if not cid or cid not in LEDGER["orders"]: return False
    q, p, s = safe_float(act.get("qty")), safe_float(act.get("price")), str(act.get("side", "")).lower()
    if not q or not p or s not in ("buy", "sell"): raise SafetyHalt("بيانات تنفيذ تالفة.")
    rec = {"activity_id": aid, "order_id": act.get("order_id"), "client_order_id": cid, "symbol": act.get("symbol"), "qty": q, "price": p, "side": s, "timestamp": act.get("transaction_time"), "recorded_at": utc_now()}
    LEDGER["fills"][aid] = rec
    save_ledger()
    ledger_event("FILL_RECORDED", rec)
    return True

def reconcile_order_fills(cid):
    rec = LEDGER["orders"].get(cid)
    if not rec: raise SafetyHalt("لا يوجد أمر.")
    broker_f = float(rec.get("filled_qty", 0) or 0)
    if broker_f <= 0: return True
    for attempt in range(FILL_SYNC_RETRIES):
        try:
            for act in fetch_fill_activities(after=rec.get("submitted_at")):
                if act.get("order_id") == rec.get("broker_order_id"): apply_fill_activity(act)
            local_f = sum(float(f.get("qty", 0)) for f in LEDGER["fills"].values() if f.get("client_order_id") == cid and f.get("side") == rec.get("side"))
            if abs(broker_f - local_f) <= 0.000001:
                rec["fill_sync_pending"], rec["fill_sync_checked"] = False, True
                save_ledger()
                return True
        except Exception: pass
        if attempt + 1 < FILL_SYNC_RETRIES: time.sleep(FILL_SYNC_GRACE_SECONDS)
    rec["fill_sync_pending"], rec["last_fill_sync_attempt"] = True, utc_now()
    save_ledger()
    return False

def sync_all_pending_fills():
    pending = [cid for cid, r in LEDGER["orders"].items() if float(r.get("filled_qty", 0) or 0) > 0 and (r.get("fill_sync_pending") or not r.get("fill_sync_checked"))]
    return all(reconcile_order_fills(cid) for cid in pending)

def reconcile_all_orders():
    try: broker_orders = get_orders(status="all", limit=500)
    except Exception as exc: log.error("تعذر جلب أوامر الوسيط: %s", exc); return False
    known_ids = set(LEDGER["orders"].keys())
    active_statuses = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace", "held", "stopped", "calculated"}
    
    for bo in broker_orders:
        cid, status = bo.get("client_order_id"), str(bo.get("status", "")).lower()
        if not cid: continue
        if cid not in known_ids:
            if status not in active_statuses:
                log.debug("تجاهل أمر تاريخي غير نشط وغير مسجل محلياً | %s | %s", cid, status) # FIXED: Changed from info to debug
                continue
            log.critical("UNKNOWN ACTIVE BROKER ORDER | %s | %s", cid, status)
            halt_bot(f"أمر نشط غير معروف: {cid}")
            return False
        apply_broker_order_update(bo)
        
    for cid, rec in LEDGER["orders"].items():
        if rec.get("submission_unknown") and not rec.get("broker_order_id"):
            found = lookup_order_after_submit(cid)
            if found: apply_broker_order_update(found)
            else: return False
            
    if not sync_all_pending_fills(): return False
    STATE["last_reconciliation"] = utc_now()
    save_state()
    return True

# =========================================================
# 39-50) POSITIONS & EXIT ENGINE
# =========================================================
def get_option_positions(): return [p for p in get_positions() if str(p.get("asset_class", "")).lower() == "us_option" or str(p.get("symbol", "")).startswith("SPY")]
def position_quantity(position): return safe_float(position.get("qty")) or 0.0

def reconcile_positions():
    broker_pos = {p["symbol"]: {"qty": position_quantity(p), "avg_entry_price": safe_float(p.get("avg_entry_price"))} for p in get_option_positions() if p.get("symbol") and abs(position_quantity(p)) > 0}
    known_syms = {r["symbol"] for r in LEDGER["orders"].values() if float(r.get("filled_qty", 0) or 0) > 0 and r.get("symbol")}
    for sym, bp in broker_pos.items():
        if sym not in known_syms:
            halt_bot(f"مركز خيارات غير معروف: {sym}")
            return False
    STATE["last_known_positions"] = broker_pos
    save_state()
    return True

def count_filled_entries_today():
    c = sum(1 for r in LEDGER["orders"].values() if r.get("side") == "buy" and r.get("created_at") and parse_market_timestamp(r["created_at"]) and parse_market_timestamp(r["created_at"]).astimezone(NY).date().isoformat() == today_ny() and float(r.get("filled_qty", 0) or 0) > 0)
    STATE["trades_today"] = c
    save_state()
    return c

def can_open_new_trade():
    ensure_not_halted()
    if not market_is_open(): return reject("MARKET", "السوق مغلق.") or False
    if not entry_time_allowed(): return reject("TIME", "خارج الدخول.") or False
    if active_entry_intent_exists(): return reject("ORDER", "أمر دخول غير محسوم.") or False
    if not reconcile_all_orders(): return reject("RECON", "فشل مصالحة الأوامر.") or False
    if not reconcile_positions(): return reject("POS", "فشل مصالحة المراكز.") or False
    if len(get_option_positions()) >= MAX_OPEN_POSITIONS: return reject("LIMIT", "الحد الأقصى للمراكز.") or False
    if count_filled_entries_today() >= MAX_TRADES_PER_DAY: return reject("TRADES", "الحد اليومي.") or False
    check_daily_risk()
    return True

def calc_stop(p): return round(p * (1 - STOP_LOSS_PERCENT), 2)
def calc_target(p): return round(p * (1 + TAKE_PROFIT_PERCENT), 2)
def calc_trail(p, h): return round(max(h * (1 - TRAIL_DISTANCE_PERCENT), p), 2) if h >= p * (1 + TRAIL_ACTIVATION_PERCENT) else None

def init_pos_meta(sym, entry, qty):
    STATE["position_meta"][sym] = {"entry_price": float(entry), "quantity": float(qty), "highest_price": float(entry), "stop_price": calc_stop(entry), "target_price": calc_target(entry), "created_at": utc_now(), "last_update": utc_now()}
    save_state()

def update_position_metadata_from_broker():
    bps = {p["symbol"]: {"qty": position_quantity(p), "avg": safe_float(p.get("avg_entry_price"))} for p in get_option_positions() if p.get("symbol") and position_quantity(p) > 0}
    for sym, d in bps.items():
        if not d["avg"]: raise SafetyHalt(f"سعر غير صالح: {sym}")
        if sym not in STATE["position_meta"]: init_pos_meta(sym, d["avg"], d["qty"])
        else: STATE["position_meta"][sym].update({"quantity": d["qty"], "entry_price": d["avg"], "last_update": utc_now()})
    for sym in list(STATE["position_meta"].keys()):
        if sym not in bps: STATE["position_meta"].pop(sym, None)
    save_state()

def request_position_exit(sym, qty, reason):
    if qty <= 0 or any(r.get("symbol") == sym and r.get("side") == "sell" and (r.get("submission_unknown") or not r.get("terminal", False)) for r in LEDGER["orders"].values()): return None
    bp = next((p for p in get_option_positions() if p.get("symbol") == sym), None)
    if not bp or position_quantity(bp) <= 0: return None
    qty = min(qty, position_quantity(bp))
    q = extract_quote_and_delta(get_option_snapshots([sym]).get(sym)) if sym in get_option_snapshots([sym]) else None
    if not q or q["bid"] is None: raise APIError(f"لا أسعار للإغلاق: {sym}")
    lp = round(max(0.01, q["bid"]), 2)
    cid = f"SF113-EXIT-{sha256_text(f'{sym}|{qty}|{utc_now()}')[:16]}"
    register_order_intent(cid, sym, qty, "sell", lp, "POSITION_EXIT")
    try:
        res = trading_api("POST", "/v2/orders", payload={"symbol": sym, "qty": str(qty), "side": "sell", "type": "limit", "time_in_force": "day", "limit_price": f"{lp:.2f}", "client_order_id": cid})
        if not res.get("id"): raise APIError("لا ID للإغلاق")
        apply_broker_order_update(res)
        ledger_event("EXIT_SUBMITTED", {"cid": cid, "sym": sym, "reason": reason})
        log.info("EXIT ACCEPTED | %s | Qty=%s", sym, qty)
        return res
    except Exception as exc:
        update_order_record(cid, status="SUBMISSION_UNRESOLVED", submission_unknown=True, last_error=str(exc))
        halt_bot(f"إغلاق غير محسوم: {sym}")
        raise SafetyHalt(f"تعذر الإغلاق: {exc}")

def manage_single_position(p):
    sym, qty = p.get("symbol"), position_quantity(p)
    if not sym or qty <= 0: return
    meta = STATE["position_meta"].get(sym)
    if not meta: return
    q = extract_quote_and_delta(get_option_snapshots([sym]).get(sym)) if sym in get_option_snapshots([sym]) else None
    if not q or q["bid"] is None: return
    bid, entry = q["bid"], meta["entry_price"]
    if bid > meta.get("highest_price", entry): meta["highest_price"] = bid; save_state()
    stop, target, trail = calc_stop(entry), calc_target(entry), calc_trail(entry, meta["highest_price"])
    log.info("POS | %s | Entry=%.2f | Bid=%.2f | PnL=%.2f%% | Stop=%.2f | Target=%.2f | Trail=%s", sym, entry, bid, (bid-entry)/entry*100, stop, target, f"{trail:.2f}" if trail else "OFF")
    
    if force_exit_time_reached(): request_position_exit(sym, qty, "END_OF_DAY")
    elif bid <= stop: request_position_exit(sym, qty, "STOP_LOSS")
    elif bid >= target: request_position_exit(sym, qty, "TAKE_PROFIT")
    elif trail and bid <= trail: request_position_exit(sym, qty, "TRAILING_STOP")

def manage_all_positions():
    update_position_metadata_from_broker()
    for p in get_option_positions(): manage_single_position(p)

# =========================================================
# 51-57) MAIN ENTRY
# =========================================================
def scan_for_entry():
    if not can_open_new_trade(): return False
    bars = get_spy_bars()
    ind = calculate_market_indicators(bars)
    sig = get_market_signal(ind)
    log.info("SPY %.2f | Sig=%s | Score=%s | RSI=%.1f | EMA9=%.2f EMA21=%.2f", ind["price"], sig["direction"], sig["score"], ind["rsi"], ind["fast_ema"], ind["slow_ema"])
    if sig["direction"] == "NEUTRAL" or sig["score"] < MIN_SIGNAL_SCORE: return False
    cid = make_candle_id(ind["timestamp"])
    if STATE.get("last_scan_candle") == cid: return False
    STATE["last_scan_candle"] = cid; save_state()
    c = select_option_contract("call" if sig["direction"] == "CALL" else "put")
    if not c: return False
    c["signal_timestamp"], c["signal"] = ind["timestamp"], sig["direction"]
    c = refresh_selected_contract(c)
    if not can_open_new_trade(): return False
    return bool(submit_limit_order_once(c, sig["direction"]))

def manage_pending_orders():
    for cid, r in list(LEDGER["orders"].items()):
        if r.get("terminal", False) or r.get("submission_unknown"): continue
        o = refresh_order_status(cid)
        if o and o["status"] in ("partially_filled", "filled"): reconcile_order_fills(cid)
        if o and o["status"] in ("canceled", "expired", "rejected", "filled") and r.get("side") == "buy":
            STATE["active_client_order_id"] = None
            save_state()

def refresh_active_order_state():
    STATE["active_client_order_id"] = next((c for c, r in LEDGER["orders"].items() if r.get("side") == "buy" and not r.get("terminal", False)), None)
    save_state()

TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}, timeout=10); return True
    except Exception: return False

def status_message():
    acc = get_account()
    return f"صَفاء التحليل v{VERSION}\nالحساب: {acc.get('status')}\nEquity: ${safe_float(acc.get('equity',0)):.2f}\nالمراكز: {len(get_option_positions())}\nالبوت: {'متوقف' if STATE.get('halted') else 'يعمل'}"

def startup_reconciliation(): # FIXED: Removed bad try...except block that caused IndentationError
    log.info("بدء فحوص ما قبل التشغيل.")
    account = get_account()
    log.info("ACCOUNT | Status=%s | Equity=%s | Paper=%s", account.get("status"), account.get("equity"), "YES" if "paper" in TRADING_URL.lower() else "NO")
    if account.get("status") != "ACTIVE": raise SafetyHalt("الحساب غير نشط.")
    if not reconcile_all_orders(): raise SafetyHalt("فشلت مصالحة الأوامر عند بدء التشغيل.")
    if not reconcile_positions(): raise SafetyHalt("فشلت مصالحة المراكز عند بدء التشغيل.")
    update_position_metadata_from_broker()
    refresh_active_order_state()
    log.info("اكتملت فحوص ما قبل التشغيل.")

def run_bot():
    log.info("صَفاء التحليل v%s بدأ التشغيل.", VERSION)
    acquire_instance_lock()
    initialize_storage()
    startup_reconciliation()
    send_telegram(status_message())
    last_recon = 0
    while True:
        try:
            ensure_not_halted()
            refresh_daily_baseline()
            now = time.time()
            manage_pending_orders()
            refresh_active_order_state()
            if now - last_recon >= RECONCILIATION_INTERVAL:
                reconcile_all_orders()
                reconcile_positions()
                last_recon = now
            manage_all_positions()
            if force_exit_time_reached(): time.sleep(SCAN_SECONDS); continue
            if entry_time_allowed(): scan_for_entry()
            time.sleep(SCAN_SECONDS)
        except SafetyHalt as exc: halt_bot(str(exc)); send_telegram(f"🚨 صَفاء توقف:\n{exc}"); break
        except (MarketDataUnavailable, APIError) as exc: log.error("TEMP ERROR: %s", exc); time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt: break
        except Exception as exc: halt_bot(f"خطأ: {exc}"); send_telegram(f"🚨 خطأ قاتل:\n{exc}"); break
    log.warning("انتهى التشغيل.")

if __name__ == "__main__":
    try: run_bot()
    except Exception as exc: log.exception("STARTUP FAILED"); sys.exit(1)
