# Admin Backend Dockerfile
# Build context should be admin/backend/ directory
# The shared module is copied during build via build-and-push.sh

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

# Install kubectl for Kubernetes secret management
RUN apt-get update && apt-get install -y \
    curl \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Pin the installer before any pip install in this stage — editable or
# locked — consumes it (Decision 6; A9b asserts the ordering, not just
# presence). Exact versions are pip-audit-clean and current PyPI HEAD.
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

# Copy and install shared module first (for layer caching)
COPY shared/ /app/shared/
# --no-deps: shared's 9 dependencies come from the hashed lock below, not an
# unhashed transitive resolve here. --no-build-isolation: use the pinned
# setuptools above, not whatever setuptools>=61.0 (shared's own build
# backend floor) would otherwise resolve fresh.
RUN pip install --no-cache-dir --no-deps --no-build-isolation -e /app/shared

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Expose port
EXPOSE 8080

# Run the application
CMD ["python", "main.py"]
