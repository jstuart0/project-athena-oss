# Admin Backend Dockerfile
# Build context should be admin/backend/ directory
# The shared module is copied during build via build-and-push.sh

FROM python:3.11-slim

WORKDIR /app

# Install kubectl for Kubernetes secret management
RUN apt-get update && apt-get install -y \
    curl \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy and install shared module first (for layer caching)
COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared

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
