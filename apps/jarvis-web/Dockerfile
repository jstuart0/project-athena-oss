# Build stage for frontend (just copy files)
FROM python:3.11-slim AS builder

WORKDIR /app

# Pin the installer before it consumes the hashed lock below — hash-checking
# mode still resolves the PEP 517 build backend for any sdist with whatever
# setuptools/wheel happen to be current unless the environment's own pip is
# pinned first. No --no-build-isolation here: this stage installs no
# editable package, so there is no isolated build to constrain.
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

# Install Python dependencies
# Build context is repo root; Dockerfile lives at apps/jarvis-web/Dockerfile
COPY apps/jarvis-web/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim

# Install curl for voice API calls and ffmpeg for audio conversion
RUN apt-get update && apt-get install -y curl ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Remove this stage's own factory-installed pip/setuptools/wheel before
# copying the builder's site-packages over it. Docker's COPY onto an
# existing directory MERGES rather than replaces — without this, the base
# image's pip==24.0 / setuptools==79.0.1 (installed by `python:3.11-slim`
# itself) survive on disk alongside the pinned 26.2.1/84.0.0 from the
# builder, and importlib.metadata-based tooling (pip-audit, or anything at
# runtime that queries package versions) can resolve the STALE, vulnerable
# one — silently defeating the builder-stage pin. Verified: without this
# line, pip-audit against the finished image reports pip 24.0
# (CVE-2026-13346 et al.) and setuptools 79.0.1 (CVE-2026-59890) even though
# the builder stage installed 26.2.1/84.0.0.
RUN rm -rf /usr/local/lib/python3.11/site-packages/* \
    /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11 /usr/local/bin/wheel

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
