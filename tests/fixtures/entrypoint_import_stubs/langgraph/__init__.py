"""Minimal stand-in for the real `langgraph` package, used ONLY by
tests/unit/test_orchestrator_entrypoint_import.py's subprocess-based import
check. The real package (per requirements.txt / src/orchestrator/requirements.txt)
is a heavy ML-adjacent dependency not installed in the lightweight venv that
runs `pytest tests/unit`; every existing orchestrator unit test already
works around this the same way (sys.modules.setdefault stubbing), but this
test spawns a FRESH subprocess to reproduce the container's real import
order, so the stub has to exist on disk and be found via PYTHONPATH instead.
"""
