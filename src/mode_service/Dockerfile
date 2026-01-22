FROM python:3.11-slim

WORKDIR /app

# Install curl for healthchecks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy shared libraries
COPY ../../shared /app/shared
RUN pip install --no-cache-dir -e /app/shared

# Copy service code
COPY main.py __init__.py .

# Create non-root user
RUN useradd -m -u 1000 athena && \
    chown -R athena:athena /app

USER athena

# Expose port
EXPOSE 8021

# Health check
HEALTHCHECK CMD curl -f http://localhost:8021/health || exit 1

# Environment variables
ENV PORT=8021 \
    PYTHONUNBUFFERED=1

# Start service
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8021", "--log-config", "/dev/null"]
