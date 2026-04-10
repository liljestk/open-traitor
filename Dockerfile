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


# Create data dirs (match what the app expects at runtime)
RUN mkdir -p data/trades data/news data/journal data/audit logs && \
    chown -R trader:trader /app

# Expose health check port
EXPOSE 8080

# Health check via HTTP endpoint
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "src.main", "--mode", "daemon"]
