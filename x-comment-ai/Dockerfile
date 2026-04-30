FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TWSCRAPE_DB_PATH=/data/accounts.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# Install build deps for any wheels that need compiling, then drop them
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Persistent volume for the twscrape SQLite DB.
# On Fly.io / Render / Railway, mount a volume at /data so cookies survive restarts.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Honor $PORT (Render sets this) with a fallback to 8000 (Fly / local dev).
CMD sh -c 'uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}'
