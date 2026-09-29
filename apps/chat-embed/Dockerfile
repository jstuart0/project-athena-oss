FROM python:3.11-slim

WORKDIR /app

# Apply Debian security updates before anything else installs on top of the
# base layer (Trivy: perl-base, gzip, libpcre2-8-0 CVEs ship stale in
# python:3.11-slim until this runs). APT_CACHE_BUST forces a fresh
# apt-get run even when Docker's layer cache would otherwise reuse a
# stale result from before newer Debian security patches were published —
# the base image digest not changing doesn't mean apt's package index
# hasn't. build-and-push.sh passes today's date on every build. ATHENA-126.
ARG APT_CACHE_BUST
RUN echo "$APT_CACHE_BUST" >/dev/null && apt-get update && apt-get -y --no-install-recommends upgrade && rm -rf /var/lib/apt/lists/*

# Pin the installer before it consumes the lock below (Decision 6; A9b
# asserts the ordering, not just presence). Exact versions are
# pip-audit-clean and current PyPI HEAD.
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

# Build context is the repository root (it needs the shared client-address
# module): docker build -f apps/chat-embed/Dockerfile .
COPY apps/chat-embed/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/chat-embed/main.py .
# Shared client-address resolution and limiter (single source of truth:
# src/shared/client_throttle.py), a sibling of main.py.
COPY src/shared/client_throttle.py ./client_throttle.py

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
