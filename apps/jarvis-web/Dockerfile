# Build stage for frontend (just copy files)
FROM python:3.11-slim AS builder

WORKDIR /app

# Install Python dependencies
# Build context is repo root; Dockerfile lives at apps/jarvis-web/Dockerfile
COPY apps/jarvis-web/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim

# Install curl for voice API calls and ffmpeg for audio conversion
RUN apt-get update && apt-get install -y curl ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY apps/jarvis-web/backend/ /app/backend/
# Shared admin URL helper (single source of truth — see CLAUDE.md OSS-First)
# Copied as a sibling of main.py so `from admin_url import get_admin_url` resolves
# against WORKDIR /app/backend in sys.path. Source of truth: src/shared/admin_url.py.
COPY src/shared/admin_url.py /app/backend/admin_url.py
COPY apps/jarvis-web/frontend/ /app/frontend/

# Set working directory to backend
WORKDIR /app/backend

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/health')" || exit 1

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
