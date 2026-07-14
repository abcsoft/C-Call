# ── Stage 1: Build dependencies ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Production image ─────────────────────────────────────────────────
FROM python:3.11-slim

# Install only runtime OS deps; ffmpeg is required for audio conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Non-root service user
RUN groupadd --gid 1001 ccall && \
    useradd --uid 1001 --gid ccall --shell /sbin/nologin --no-create-home ccall

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source (secrets never COPY'd — injected via env/secrets at runtime)
COPY --chown=ccall:ccall . /app/

# Remove development/test artifacts that must not be in production image
RUN rm -rf \
    /app/.env \
    /app/.env.* \
    /app/tests/ \
    /app/backups/ \
    /app/instance/recordings/ \
    /app/instance/generated/ \
    /app/instance/sounds/ \
    /app/node_modules/ \
    /app/__pycache__ \
    /app/.git || true

# Ensure instance dir exists and is writable by service user
RUN mkdir -p /app/instance/generated /app/instance/sounds /app/instance/recordings && \
    chown -R ccall:ccall /app/instance

USER ccall

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5000/health/live || exit 1

# Default: web service. Override CMD in docker-compose for scheduler/worker roles.
CMD ["gunicorn", "--config", "gunicorn.conf.py", "run:app"]
