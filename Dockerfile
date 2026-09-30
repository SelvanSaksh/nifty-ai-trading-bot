# Multi-stage build for smaller image
FROM python:3.11-slim AS builder

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
# Fyers official data socket. --no-deps: its pinned aiohttp==3.9.3 would
# otherwise conflict with the aiohttp pin in requirements.txt; its runtime
# deps (requests, websocket-client, setuptools/pkg_resources) come from there.
RUN pip install --no-cache-dir --user --no-deps fyers-apiv3==3.1.18

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Create non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Copy only necessary files from builder
COPY --from=builder /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH

# Copy application code
COPY . .

# Create data directory and set permissions
RUN mkdir -p /app/data && chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Health check
# Container-internal probe: the live URL (https://api.trading.quantumvora.com)
# is served by nginx, which waits for this container to become healthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

# ONE worker only: the trading engine must be a singleton (see utils/engine_lock.py).
# Extra workers would each spin up their own engine/state; scale the API safely by
# running additional containers — the engine lock elects exactly one leader.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]