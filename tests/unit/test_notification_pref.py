"""Unit tests for orchestrator.nodes.notification_pref.notification_pref_node.

Covers all behavioral branches in the function:
 1.  Opt-out happy path (morning_greeting) → morning-specific answer.
 2.  Opt-in happy path (morning_greeting) → morning opt-in answer.
 3.  Generic opt-out (alerts) → 3 POSTs, comma-joined rule names in answer.
 4.  404 from service (rule not found) → "couldn't find that notification rule".
 5.  httpx.ConnectError → "isn't available right now"; state.error set.
 6.  Generic Exception → "had trouble" answer; state.error set.
 7.  Both opt-out and opt-in keywords + "don't" → resolves to opt-out.
 8.  Ambiguous keywords (no "don't"/"not") → defaults to opt-out.
 9.  timing_tracker present → track_sync called with correct args.
10.  timing_tracker absent (None) → completes without raising; node_timings set.
11.  state.room is None → payload uses "office".
12.  state.room override → payload contains the supplied room.

Note: notification_pref_node is async; tests use asyncio.run() to avoid
requiring pytest-asyncio (consistent with test_send_sms_node.py pattern).

Patching strategy: httpx is a lazy import inside the function body (not a
module-level attribute of notification_pref).  We therefore patch
"httpx.AsyncClient" (the canonical httpx namespace) rather than
"orchestrator.nodes.notification_pref.httpx.AsyncClient".
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "src")

from orchestrator.nodes import notification_pref_node
from orchestrator.state import OrchestratorState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    *,
    query: str = "stop the morning notifications",
    room: str | None = None,
    timing_tracker=None,
) -> OrchestratorState:
    """Return a minimal OrchestratorState for notification_pref_node testing."""
    state = OrchestratorState(query=query)
    state.room = room
    state.timing_tracker = timing_tracker
    return state


def _ok_response(status: str = "opted_out") -> MagicMock:
    """Return a mock httpx.Response with status_code=200."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": status}
    return resp


def _not_found_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "Not found"
    return resp


def _patch_client(responses):
    """
    Patch httpx.AsyncClient so that client.post() returns items from `responses`
    in order (one per call).  httpx is lazily imported inside the function body,
    so we patch the canonical "httpx.AsyncClient" rather than trying to reach it
    through the module attribute (which doesn't exist until first call).
    """
    if not isinstance(responses, list):
        responses = [responses]

    post_mock = AsyncMock(side_effect=responses)

    cm_mock = MagicMock()
    cm_mock.__aenter__ = AsyncMock(return_value=MagicMock(post=post_mock))
    cm_mock.__aexit__ = AsyncMock(return_value=False)

    return patch("httpx.AsyncClient", return_value=cm_mock)


# ---------------------------------------------------------------------------
# Test 1: Opt-out happy path (morning_greeting)
# ---------------------------------------------------------------------------

def test_opt_out_morning_greeting_answer():
    """Successful opt-out for morning_greeting → morning-specific answer."""
    state = _make_state(query="stop the morning notifications")
    with _patch_client(_ok_response("opted_out")):
        result = asyncio.run(notification_pref_node(state))
    assert "turned off the morning notifications" in result.answer


def test_opt_out_morning_greeting_node_timings():
    state = _make_state(query="stop the morning notifications")
    with _patch_client(_ok_response("opted_out")):
        result = asyncio.run(notification_pref_node(state))
    assert "notification_pref" in result.node_timings
    assert result.node_timings["notification_pref"] >= 0.0


# ---------------------------------------------------------------------------
# Test 2: Opt-in happy path (morning_greeting)
# ---------------------------------------------------------------------------

def test_opt_in_morning_greeting_answer():
    """Successful opt-in for morning_greeting → morning-specific opt-in answer."""
    state = _make_state(query="turn morning updates back on")
    with _patch_client(_ok_response("opted_in")):
        result = asyncio.run(notification_pref_node(state))
    assert "turned morning notifications back on" in result.answer


# ---------------------------------------------------------------------------
# Test 3: Generic opt-out (alerts) → 3 POSTs, comma-joined names
# ---------------------------------------------------------------------------

def test_generic_opt_out_alerts_answer():
    """'stop the alerts' triggers 3 alert rules; answer lists them comma-joined."""
    state = _make_state(query="stop the alerts")
    responses = [
        _ok_response("opted_out"),
        _ok_response("opted_out"),
        _ok_response("opted_out"),
    ]
    with _patch_client(responses):
        result = asyncio.run(notification_pref_node(state))
    assert "disabled notifications for:" in result.answer
    assert "fridge open alert" in result.answer
    assert "door unlocked alert" in result.answer
    assert "tesla charge alert" in result.answer


# ---------------------------------------------------------------------------
# Test 4: 404 → "couldn't find that notification rule"
# ---------------------------------------------------------------------------

def test_404_rule_not_found_answer():
    """404 from the service → 'couldn't find that notification rule'."""
    state = _make_state(query="stop the morning notifications")
    with _patch_client(_not_found_response()):
        result = asyncio.run(notification_pref_node(state))
    assert "couldn't find that notification rule" in result.answer


# ---------------------------------------------------------------------------
# Test 5: httpx.ConnectError → "isn't available right now"; error set
# ---------------------------------------------------------------------------

def test_connect_error_answer():
    """ConnectError → 'isn't available right now'."""
    import httpx

    state = _make_state(query="stop the morning notifications")
    with patch("httpx.AsyncClient", side_effect=httpx.ConnectError("refused")):
        result = asyncio.run(notification_pref_node(state))
    assert "isn't available right now" in result.answer


def test_connect_error_sets_state_error():
    import httpx

    state = _make_state(query="stop the morning notifications")
    with patch("httpx.AsyncClient", side_effect=httpx.ConnectError("refused")):
        result = asyncio.run(notification_pref_node(state))
    assert result.error is not None
    assert "unreachable" in result.error.lower()


# ---------------------------------------------------------------------------
# Test 6: Generic exception → "had trouble"; error set
# ---------------------------------------------------------------------------

def test_generic_exception_answer():
    """Unexpected exception → 'had trouble updating' answer."""
    state = _make_state(query="stop the morning notifications")
    with patch("httpx.AsyncClient", side_effect=RuntimeError("boom")):
        result = asyncio.run(notification_pref_node(state))
    assert "had trouble" in result.answer


def test_generic_exception_sets_state_error():
    state = _make_state(query="stop the morning notifications")
    with patch("httpx.AsyncClient", side_effect=RuntimeError("boom")):
        result = asyncio.run(notification_pref_node(state))
    assert result.error is not None
    assert "boom" in result.error


# ---------------------------------------------------------------------------
# Test 7: Both opt-out and opt-in keywords + "don't" → opt-out wins
# ---------------------------------------------------------------------------

def test_dont_present_resolves_to_opt_out():
    """When 'don't' is present, the ambiguity resolves to opt-out."""
    state = _make_state(query="I don't want morning updates")
    with _patch_client(_ok_response("opted_out")):
        result = asyncio.run(notification_pref_node(state))
    assert "turned off the morning notifications" in result.answer


# ---------------------------------------------------------------------------
# Test 8: Ambiguous query (no clear keyword) → defaults to opt-out
# ---------------------------------------------------------------------------

def test_ambiguous_defaults_to_opt_out():
    """No strong opt-in or opt-out keyword → defaults to opt-out."""
    # "morning" is present, no clear opt-in or opt-out keyword.
    # is_opt_out == is_opt_in == False → fallback fires; no "don't"/"not" → is_opt_out = True.
    state = _make_state(query="morning notifications please")
    with _patch_client(_ok_response("opted_out")):
        result = asyncio.run(notification_pref_node(state))
    assert "turned off the morning notifications" in result.answer


# ---------------------------------------------------------------------------
# Test 9: timing_tracker present → track_sync called
# ---------------------------------------------------------------------------

def test_timing_tracker_called():
    """When timing_tracker is set, track_sync is called with the correct prefix."""
    tracker = MagicMock()
    state = _make_state(query="stop the morning notifications", timing_tracker=tracker)
    with _patch_client(_ok_response("opted_out")):
        asyncio.run(notification_pref_node(state))
    tracker.track_sync.assert_called_once()
    call_args = tracker.track_sync.call_args[0]
    assert call_args[0] == "graph"
    assert call_args[1] == "notification_pref"
    assert isinstance(call_args[2], float)


# ---------------------------------------------------------------------------
# Test 10: timing_tracker absent (None) → completes; node_timings set
# ---------------------------------------------------------------------------

def test_no_timing_tracker_still_sets_node_timings():
    """With timing_tracker=None, node completes without raising and sets node_timings."""
    state = _make_state(query="stop the morning notifications", timing_tracker=None)
    with _patch_client(_ok_response("opted_out")):
        result = asyncio.run(notification_pref_node(state))
    assert "notification_pref" in result.node_timings
    assert result.node_timings["notification_pref"] >= 0.0


# ---------------------------------------------------------------------------
# Test 11: state.room is None → payload uses "office"
# ---------------------------------------------------------------------------

def test_room_defaults_to_office():
    """When state.room is None, POST payload contains room='office'."""
    state = _make_state(query="stop the morning notifications", room=None)

    captured_payloads = []

    async def capture_post(url, json=None):
        captured_payloads.append(json)
        return _ok_response("opted_out")

    cm_mock = MagicMock()
    cm_mock.__aenter__ = AsyncMock(return_value=MagicMock(post=capture_post))
    cm_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=cm_mock):
        asyncio.run(notification_pref_node(state))

    assert captured_payloads, "Expected at least one POST"
    assert captured_payloads[0]["room"] == "office"


# ---------------------------------------------------------------------------
# Test 12: state.room override → payload contains supplied room
# ---------------------------------------------------------------------------

def test_room_override_propagated():
    """When state.room='kitchen', POST payload contains room='kitchen'."""
    state = _make_state(query="stop the morning notifications", room="kitchen")

    captured_payloads = []

    async def capture_post(url, json=None):
        captured_payloads.append(json)
        return _ok_response("opted_out")

    cm_mock = MagicMock()
    cm_mock.__aenter__ = AsyncMock(return_value=MagicMock(post=capture_post))
    cm_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=cm_mock):
        asyncio.run(notification_pref_node(state))

    assert captured_payloads, "Expected at least one POST"
    assert captured_payloads[0]["room"] == "kitchen"
