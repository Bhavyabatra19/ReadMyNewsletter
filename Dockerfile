FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATABASE_PATH=/data/readmynewsletter.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Persist the SQLite database + generated secret on a mounted volume.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Single process runs the web app AND the in-process daily scheduler.
# For multi-worker setups, run gunicorn instead and drive refreshes with
# `python worker.py` from cron (see README).
CMD ["python", "app.py"]
