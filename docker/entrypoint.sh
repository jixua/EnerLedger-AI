#!/bin/sh
set -eu

python - <<'PY'
import os
import sys
import time

from sqlalchemy import create_engine, text

from app.rag.config import settings

database_url = os.getenv("ALEMBIC_DATABASE_URL") or settings.DATABASE_URL
engine = create_engine(database_url, pool_pre_ping=True)

for attempt in range(1, 61):
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("[entrypoint] MySQL is ready", flush=True)
        break
    except Exception as exc:  # noqa: BLE001 - startup retry must cover driver/network errors
        if attempt == 60:
            print(f"[entrypoint] MySQL unavailable after 60 attempts: {exc}", file=sys.stderr)
            raise
        print(f"[entrypoint] waiting for MySQL ({attempt}/60): {exc}", flush=True)
        time.sleep(2)
else:
    raise RuntimeError("unreachable")
PY

alembic upgrade head

exec uvicorn app.main:app \
    --host "${APP_HOST:-0.0.0.0}" \
    --port "${APP_PORT:-8000}"
