# =============================================================
# Stage 1 — Build the React/Vite frontend
# =============================================================
FROM node:24-slim AS frontend-builder

WORKDIR /frontend
COPY dashboard/frontend/package.json dashboard/frontend/package-lock.json ./
RUN npm ci --legacy-peer-deps
COPY dashboard/frontend/ ./
RUN npm run build

# =============================================================
# Stage 2 — Python runtime
# =============================================================
FROM python:3.13-slim

# Security: non-root user
RUN groupadd -r trader && useradd -r -g trader -d /app trader

WORKDIR /app

# Install system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ src/
COPY config/ config/

# Copy built frontend into the image so FastAPI can serve it
COPY --from=frontend-builder /frontend/dist src/dashboard/static/

# Create data dirs (match what the app expects at runtime)
RUN mkdir -p data/trades data/news data/journal data/audit logs && \
    chown -R trader:trader /app

# Switch to non-root user
USER trader

# Expose health check port
EXPOSE 8080

# Health check via HTTP endpoint
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "src.main", "--mode", "daemon"]
