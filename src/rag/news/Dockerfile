# News RAG Service Dockerfile
#
# Multi-stage build for optimized container image.
# Based on Python 3.11 slim for smaller image size.
#
# Build:
#   docker build -t athena/news-rag:latest .
#
# Run:
#   docker run -p 8015:8015 \
#     -e NEWSAPI_KEY=your_key \
#     -e REDIS_URL=redis://redis:6379/0 \
#     athena/news-rag:latest
#

FROM python:3.11-slim as base

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy shared package
COPY ../../shared /app/shared
RUN pip install --no-cache-dir -e /app/shared

# Copy service code
COPY main.py .
COPY __init__.py .

# Create non-root user for security
RUN useradd -m -u 1000 athena && \
    chown -R athena:athena /app

USER athena

# Expose service port
EXPOSE 8015

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8015/health || exit 1

# Environment variables (with defaults)
ENV PORT=8015 \
    REDIS_URL=redis://localhost:6379/0 \
    PYTHONUNBUFFERED=1

# Start the service
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8015", "--log-config", "/dev/null"]
