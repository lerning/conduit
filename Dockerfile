# Conduit gateway — deployed as its OWN Fly app so any project can call it
# without sharing a deployment lifecycle. Private-only (6PN): reachable at
# conduit-gateway.internal:8200, never exposed publicly.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway/ ./gateway/

# Ledger lives on the mounted volume so the daily spend cap survives restarts.
ENV CONDUIT_DB_PATH=/data/conduit.db
EXPOSE 8200

# Bind IPv6 (`::`), not 0.0.0.0. Fly's private network (6PN) is IPv6-ONLY, so a
# service bound to IPv4 refuses connections from sibling apps resolving
# conduit-gateway.internal. Dual-stack Linux accepts IPv4 on `::` too.
CMD ["uvicorn", "gateway.app:app", "--host", "::", "--port", "8200"]
