"""The planning prompt's TODAY line is the property's date, not the process date."""
from __future__ import annotations

import asyncio

import pytest

from . import _public_audience_harness as h
from ._clock_fixture import DATE_DIVERGENCE, clock, property_today  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def test_planning_today_is_the_property_date(clock, monkeypatch):
    clock.use(DATE_DIVERGENCE)
    h.patch_tool_call_dependencies(monkeypatch)
    llm = h.CapturingLLM()
    h._runtime.set_llm_router(llm)
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    state = h.make_state(permissions=owner, mode="owner", query="plan my day")

    before = property_today(DATE_DIVERGENCE[1])
    asyncio.run(h.main.tool_call_node(state))
    after = property_today(DATE_DIVERGENCE[1])

    expected = {f"TODAY is: {d.strftime('%A, %B %d, %Y')}" for d in (before, after)}
    assert "TODAY is:" in llm.text()
    assert any(line in llm.text() for line in expected)
