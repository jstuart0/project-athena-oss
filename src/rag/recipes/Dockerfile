FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY ../../shared /app/shared
RUN pip install --no-cache-dir -e /app/shared
COPY main.py __init__.py .
RUN useradd -m -u 1000 athena && chown -R athena:athena /app
USER athena
EXPOSE 8020
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 CMD curl -f http://localhost:8020/health || exit 1
ENV PORT=8020 REDIS_URL=redis://localhost:6379/0 PYTHONUNBUFFERED=1
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8020", "--log-config", "/dev/null"]
