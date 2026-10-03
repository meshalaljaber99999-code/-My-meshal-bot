import os
import time
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest

# ============ الإعدادات ============
API_KEY = os.environ.get("API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY")

SYMBOL_UNDERLYING = "SPY"          # السهم/المؤشر الأساسي
TAKE_PROFIT_PCT = 4.0              # جني ربح عند 400% (أي x5 من سعر الشراء)
STOP_LOSS_PCT = 0.5                # وقف خسارة عند خسارة 50%
CHECK_INTERVAL_SECONDS = 1         # يفحص السعر كل ثانية
MAX_CONTRACT_PRICE = 2.0           # أقصى سعر للعقد وقت الشراء (دولار للسهم الواحد، أي 200 دولار للعقد)

# قفل أمان: دايماً Paper إلى أن تغيّره يدوياً بوعي تام
PAPER_MODE = True

# ============ الاتصال ============
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)


def get_account_info():
    account = trading_client.get_account()
    print(f"تم الاتصال. الرصيد: {account.cash} - القوة الشرائية: {account.buying_power}")
    return account


def find_cheap_call_option():
    """يبحث عن عقد Call رخيص على SPY، بين 7 و30 يوم لانتهاء الصلاحية."""
    today = datetime.now()
    min_expiry = today + timedelta(days=7)
    max_expiry = today + timedelta(days=30)

    request = GetOptionContractsRequest(
        underlying_symbols=[SYMBOL_UNDERLYING],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=min_expiry.strftime("%Y-%m-%d"),
        expiration_date_lte=max_expiry.strftime("%Y-%m-%d"),
        type=ContractType.CALL,
        limit=100,
    )
    contracts = trading_client.get_option_contracts(request).option_contracts

    if not contracts:
        print("ما فيه عقود متا


