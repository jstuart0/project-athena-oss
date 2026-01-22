FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY ../../shared /app/shared
RUN pip install --no-cache-dir -e /app/shared
COPY main.py __init__.py .
RUN useradd -m -u 1000 athena && chown -R athena:athena /app
USER athena
EXPOSE 8018
HEALTHCHECK CMD curl -f http://localhost:8018/health || exit 1
ENV PORT=8018 PYTHONUNBUFFERED=1
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8018", "--log-config", "/dev/null"]
