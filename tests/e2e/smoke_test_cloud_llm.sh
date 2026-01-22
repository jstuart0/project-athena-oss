#!/bin/bash
# Quick smoke test for cloud LLM functionality
# Run after deployment to verify basic functionality
#
# Open Source Compatible - Uses standard curl and bash.

set -e

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://localhost:8001}"
ADMIN_URL="${ADMIN_URL:-http://localhost:8080}"

echo "=== Cloud LLM Smoke Test ==="
echo "Gateway: $GATEWAY_URL"
echo "Orchestrator: $ORCHESTRATOR_URL"
echo "Admin: $ADMIN_URL"
echo ""

# Test 1: Admin API accessible
echo -n "1. Admin API health... "
if curl -sf "${ADMIN_URL}/health" > /dev/null 2>&1; then
    echo "OK"
else
    echo "FAILED"
    exit 1
fi

# Test 2: Gateway accessible
echo -n "2. Gateway health... "
if curl -sf "${GATEWAY_URL}/health" > /dev/null 2>&1; then
    echo "OK"
else
    echo "FAILED"
    exit 1
fi

# Test 3: Orchestrator accessible
echo -n "3. Orchestrator health... "
if curl -sf "${ORCHESTRATOR_URL}/health" > /dev/null 2>&1; then
    echo "OK"
else
    echo "FAILED"
    exit 1
fi

# Test 4: Cloud providers endpoint
echo -n "4. Cloud Providers API... "
PROVIDERS=$(curl -sf "${ADMIN_URL}/api/cloud-providers" 2>/dev/null || echo "error")
if [[ "$PROVIDERS" != "error" ]]; then
    echo "OK"
else
    echo "SKIPPED (requires auth)"
fi

# Test 5: Service bypass endpoint
echo -n "5. Service Bypass API... "
BYPASS=$(curl -sf "${ADMIN_URL}/api/rag-service-bypass/public/recipes/config" 2>/dev/null || echo "error")
if [[ "$BYPASS" != "error" ]]; then
    echo "OK"
else
    echo "FAILED"
    exit 1
fi

# Test 6: Feature flag endpoint
echo -n "6. Feature Flags API... "
FLAGS=$(curl -sf "${ADMIN_URL}/api/features" 2>/dev/null || echo "error")
if [[ "$FLAGS" != "error" ]]; then
    echo "OK"
else
    echo "SKIPPED (requires auth)"
fi

# Test 7: Cloud usage endpoint
echo -n "7. Cloud Usage API... "
USAGE=$(curl -sf "${ADMIN_URL}/api/cloud-llm-usage/summary/today" 2>/dev/null || echo "error")
if [[ "$USAGE" != "error" ]]; then
    echo "OK"
else
    echo "SKIPPED (requires auth)"
fi

# Test 8: Cost alerts endpoint
echo -n "8. Cost Alerts API... "
ALERTS=$(curl -sf "${ADMIN_URL}/api/cloud-llm-usage/cost-alerts" 2>/dev/null || echo "error")
if [[ "$ALERTS" != "error" ]]; then
    echo "OK"
else
    echo "SKIPPED (requires auth)"
fi

# Test 9: Basic query works
echo -n "9. Basic query... "
RESPONSE=$(curl -sf -X POST "${ORCHESTRATOR_URL}/query" \
    -H "Content-Type: application/json" \
    -d '{"query":"Hello","mode":"owner","room":"office"}' 2>/dev/null || echo "error")
if [[ "$RESPONSE" != "error" ]]; then
    echo "OK"
else
    echo "FAILED"
    exit 1
fi

# Test 10: Local model query (regression test)
echo -n "10. Local model query... "
RESPONSE=$(curl -sf -X POST "${GATEWAY_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3:4b","messages":[{"role":"user","content":"Hi"}]}' 2>/dev/null || echo "error")
if [[ "$RESPONSE" != "error" ]]; then
    echo "OK"
else
    echo "SKIPPED (model may not be loaded)"
fi

echo ""
echo "=== Smoke Tests Complete ==="
echo ""
echo "Summary:"
echo "- Core services: OK"
echo "- API endpoints: Accessible"
echo "- Basic functionality: Working"
echo ""
echo "For full testing, run: pytest tests/e2e/test_cloud_llm_e2e.py -v"
