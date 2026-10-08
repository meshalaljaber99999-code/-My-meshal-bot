FROM python:3.10-slim

# تثبيت حزم النظام المطلوبة لـ LightGBM
RUN apt-get update && apt-get install -y \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# نسخ ملف المتطلبات وتثبيتها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع
COPY . .

# تشغيل التطبيق عبر خادم الإنتاج Gunicorn
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8080"]
