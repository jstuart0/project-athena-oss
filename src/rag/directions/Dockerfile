# Auto-generated Dockerfile for directions RAG service
# Build context should be src/ directory:
#   docker build -f rag/directions/Dockerfile -t athena-rag-directions .

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install shared module first (for layer caching)
COPY shared /app/shared
RUN pip install --no-cache-dir -e /app/shared

# Copy and install service requirements
COPY rag/directions/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY rag/directions/main.py /app/
COPY rag/directions/ /app/rag_service/

# Create non-root user
RUN useradd -m -u 1000 athena && chown -R athena:athena /app
USER athena

# Configure service
ENV PORT=8030
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8030

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8030/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8030"]
