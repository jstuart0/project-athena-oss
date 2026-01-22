# Project Athena Installation Issues

## Issues Found During Deployment (January 2026)

This document captures bugs and issues discovered during the first OSS deployment
that should be fixed before public release.

---

## Critical: Code Bugs (Must Fix)

### 1. Missing `import os` Statements

**Files affected:**
- `admin/backend/app/routes/services.py`
- `admin/backend/app/routes/voice_config.py`
- `admin/backend/app/routes/component_models.py`
- `admin/backend/app/routes/dashboard.py`
- `admin/backend/app/routes/tool_calling.py`

**Error:** `NameError: name 'os' is not defined`

**Fix:** Add `import os` at the top of each file.

---

### 2. Missing `requirements.txt` Files for RAG Services

**Services missing requirements.txt:**
- `src/rag/amtrak/`
- `src/rag/brightdata/`
- `src/rag/directions/`
- `src/rag/media/`
- `src/rag/onecall/`
- `src/rag/seatgeek_events/`
- `src/rag/serpapi_events/`
- `src/rag/tesla/`
- `src/rag/transportation/`

**Error:** Docker build fails - file not found

**Fix:** Create requirements.txt with standard dependencies:
```
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
pydantic>=2.0.0
python-dotenv>=1.0.0
httpx>=0.24.0
redis>=5.0.0
structlog>=23.2.0
```

---

### 3. Dockerfile Module Copy Issues

**admin/backend/Dockerfile:**
- Missing shared module copy/install

**Fix:**
```dockerfile
COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared
```

**orchestrator/Dockerfile:**
- Missing sms module copy

**Fix:**
```dockerfile
COPY sms/ /app/sms/
```

---

### 4. Invalid Dockerfile Syntax in RAG Services

**Problem:** Original Dockerfiles used shell syntax that Docker doesn't support:
```dockerfile
COPY rag/weather/__init__.py /app/ 2>/dev/null || true
```

**Fix:** Remove `2>/dev/null || true` - Docker COPY doesn't support shell redirects.

---

### 5. Missing `pyproject.toml` for Shared Module

**Problem:** `src/shared/` needs to be pip-installable

**Fix:** Create `src/shared/pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "athena-shared"
version = "1.0.0"
dependencies = [
    "structlog>=23.2.0",
    "pydantic>=2.0.0",
]

[tool.setuptools.packages.find]
where = ["."]
```

---

## High: Configuration Issues

### 6. Kubernetes REDIS_PORT Environment Variable Conflict

**Problem:** Kubernetes auto-injects `REDIS_PORT=tcp://10.x.x.x:6379` from service
discovery, which breaks code expecting `REDIS_PORT=6379`.

**Error:** `ValueError: invalid literal for int() with base 10: 'tcp://...'`

**Fix Options:**
1. Explicitly set REDIS_HOST/REDIS_PORT in deployment manifests
2. Update code to parse the TCP URL format
3. Rename the Redis service to avoid auto-injection

---

### 7. Health Check Path Mismatch (admin-backend)

**Problem:** Manifest used `/api/health`, actual endpoint is `/health`

**Fix:** Update liveness/readiness probes to use `/health`

---

### 8. IngressRoute Missing StripPrefix Middleware

**Problem:** Routes like `/api/health` forwarded to backend as-is, but backend
expects `/health`.

**Fix:** Add StripPrefix middleware:
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: strip-api-prefix
spec:
  stripPrefix:
    prefixes:
      - /api
```

---

### 9. Health Probe Timeouts Too Aggressive

**Problem:** Orchestrator takes time to check RAG services at startup, causing
liveness probe failures and restarts.

**Fix:** Increase timeouts:
```yaml
livenessProbe:
  initialDelaySeconds: 60
  timeoutSeconds: 30
  failureThreshold: 5
readinessProbe:
  initialDelaySeconds: 30
  timeoutSeconds: 30
```

---

## Medium: Usability Issues

### 10. Hardcoded Values in Scripts and Manifests

**Scripts need parameterization:**
- `build-and-push.sh`: Registry URL hardcoded
- `create-secrets.sh`: Database host hardcoded
- `deploy.sh`: Cluster context hardcoded

**Manifests need templating:**
- Domain names (xmojo.net)
- Container registry URL
- TLS secret names
- Database connection strings

**Recommendation:** Create a `config.env` or use Kustomize overlays.

---

### 11. No Database Setup Script

**Problem:** User must manually create the `athena_prod` database.

**Fix:** Add database initialization script or include in deployment:
```bash
psql -h $DB_HOST -U $DB_USER -c "CREATE DATABASE athena_prod;"
```

---

### 12. Resource Requests Too High for Small Clusters

**Problem:** Default CPU/memory requests prevent scheduling on resource-constrained
clusters.

**Fix:** Provide "minimal" resource profiles:
```yaml
# Minimal profile
requests:
  memory: "128Mi"
  cpu: "50m"
```

---

## Low: Documentation Gaps

### 13. Missing Installation Guide

Need comprehensive `INSTALL.md` covering:
- Prerequisites (kubectl, docker, cluster access)
- Configuration file setup
- Step-by-step deployment
- Verification steps
- Troubleshooting common issues

### 14. Missing Architecture Diagram

Need visual overview of:
- Service dependencies
- Network topology
- Data flow

### 15. Missing Environment Variable Reference

Need complete list of all environment variables:
- Required vs optional
- Default values
- Where they're used

---

## Recommendations for OSS Release

1. **Fix all Critical bugs** before release
2. **Create config template** (`config.env.example`) with all customizable values
3. **Add Kustomize support** for easy environment customization
4. **Write INSTALL.md** with copy-paste commands
5. **Add pre-flight check script** that validates prerequisites
6. **Create minimal resource profile** for testing/development
7. **Add integration tests** that verify deployment health
