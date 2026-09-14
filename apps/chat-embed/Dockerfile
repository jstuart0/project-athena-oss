FROM python:3.11-slim

WORKDIR /app

# Pin the installer before it consumes the lock below (Decision 6; A9b
# asserts the ordering, not just presence). Exact versions are
# pip-audit-clean and current PyPI HEAD.
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
