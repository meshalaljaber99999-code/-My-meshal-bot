import alpaca_trade_api as tradeapi
import os
import time

# قراءة المفاتيح من إعدادات Railway (هذا هو الطريقة الآمنة)
# ستضيف المفاتيح في منصة Railway في تبويب Variables
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
BASE_URL = "https://paper-api.alpaca.markets"

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version='v2')

print("نظام التداول بدأ العمل...")

try:
    account = api.get_account()
    print(f"تم الاتصال بنجاح! الرصيد الحالي: {account.cash}")
except Exception as e:
    print(f"حدث خطأ في الاتصال: {e}")

while True:
    # هنا ستضع منطق التداول الخاص بك لاحقاً
    print("البوت يراقب السوق...")
    time.sleep(60)
