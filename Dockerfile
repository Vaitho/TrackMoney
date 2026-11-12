FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DB_PATH=/data/bot.db

WORKDIR /app

# Dipendenze + Chromium (serve per i siti che mostrano il prezzo via JavaScript).
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY *.py ./
COPY handlers ./handlers

# Il bot gira come utente normale (uid 1000), non come root.
RUN useradd --create-home --uid 1000 bot \
    && mkdir /data \
    && chown bot:bot /data
USER bot

VOLUME /data
CMD ["python", "main.py"]
