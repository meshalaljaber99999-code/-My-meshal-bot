# استخدام صورة بايثون الرسمية
FROM python:3.10-slim

# تعيين مجلد العمل داخل الحاوية
WORKDIR /app

# نسخ ملف المتطلبات أولاً لتثبيتها والاستفادة من الـ Caching
COPY requirements.txt .

# تثبيت الحزم المطلوبة بدون تخزين مؤقت لتجنب الأخطاء
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع إلى الحاوية
COPY . .

# تعيين المنفذ الافتراضي للتطبيق
EXPOSE 8080

# تشغيل التطبيق باستخدام Gunicorn كخادم إنتاج
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8080"]
