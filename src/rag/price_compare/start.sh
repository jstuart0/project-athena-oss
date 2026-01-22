#!/bin/bash
cd "$(dirname "$0")"
source ../../../.venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port ${SERVICE_PORT:-8023}
