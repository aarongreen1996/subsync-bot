FROM python:3.11-slim

WORKDIR /app

# Copy requirements first (cached layer - only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL application files - never cached, always fresh
ARG CACHE_BUST=1
COPY . .

EXPOSE 8080

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "120"]
