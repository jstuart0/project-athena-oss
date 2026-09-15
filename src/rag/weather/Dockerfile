# Auto-generated Dockerfile for weather RAG service
# Build context should be src/ directory:
#   docker build -f rag/weather/Dockerfile -t athena-rag-weather .

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Pin the installer before any pip install in this stage — editable or
# locked — consumes it (Decision 6; A9b asserts the ordering, not just
# presence). Exact versions are pip-audit-clean and current PyPI HEAD.
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

# Copy and install shared module first (for layer caching)
COPY shared /app/shared
# --no-deps: shared's 9 dependencies come from the hashed lock below, not an
# unhashed transitive resolve here. --no-build-isolation: use the pinned
# setuptools above, not whatever setuptools>=61.0 (shared's own build
# backend floor) would otherwise resolve fresh.
RUN pip install --no-cache-dir --no-deps --no-build-isolation -e /app/shared

# Copy and install service requirements
COPY rag/weather/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY rag/weather/main.py /app/
COPY rag/weather/ /app/rag_service/

# Create non-root user
RUN useradd -m -u 1000 athena && chown -R athena:athena /app
USER athena

# Configure service
ENV PORT=8010
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
