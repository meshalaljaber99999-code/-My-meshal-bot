FROM python:3.10-slim

WORKDIR /app

# تثبيتات النظام اللازمة لعمل LightGBM والتعلم الآلي
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "main.py"]
