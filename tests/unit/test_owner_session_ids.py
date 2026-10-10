"""One rule across both images: an id starting with "own-" is an owner session.

jarvis-web mints the ids and the orchestrator classifies them; if either side
changes the prefix or the class mapping, this fails. Stdlib only on the
orchestrator side (session_keys.py imports nothing from the app).
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCH = REPO_ROOT / "src" / "orchestrator" / "session_keys.py"
JARVIS = REPO_ROOT / "apps" / "jarvis-web" / "backend" / "main.py"


def _constant(path, name):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value.value
    raise AssertionError(f"{name} not in {path}")


def _keys():
    spec = importlib.util.spec_from_file_location("_session_keys_cross_image", ORCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_images_use_the_same_owner_prefix_and_the_prefix_means_owner():
    prefix = _constant(ORCH, "OWNER_SESSION_PREFIX")
    assert prefix == _constant(JARVIS, "OWNER_SESSION_PREFIX") == "own-"
    keys = _keys()
    assert keys.id_class(prefix + "abc.def") == keys.CALLER_CLASS_OWNER
    assert keys.id_class("abc.def") == keys.CALLER_CLASS_OTHER


def test_both_images_use_the_same_guest_prefix_and_namespaces():
    assert _constant(ORCH, "GUEST_SESSION_PREFIX") == _constant(JARVIS, "GUEST_SESSION_PREFIX") == "gst-"
    keys = _keys()
    assert keys.id_class("gst-abc.def") == keys.CALLER_CLASS_GUEST
    assert keys.session_storage_key("gst-x").startswith("athena:guest_session:")
    assert keys.context_storage_key("gst-x").startswith("athena:guest_context:")
    # jarvis-web mints a guest id the orchestrator keeps for a guest and refuses to anyone else
    signed = "gst-" + "a" * 32 + "." + "b" * 24
    assert keys.usable_session_id(signed, keys.CALLER_CLASS_GUEST) == signed
    assert keys.usable_session_id(signed, keys.CALLER_CLASS_OTHER) != signed


def test_the_owner_prefix_selects_the_owner_namespaces():
    keys = _keys()
    sid = _constant(ORCH, "OWNER_SESSION_PREFIX") + "x"
    assert keys.session_storage_key(sid).startswith("athena:owner_session:")
    assert keys.context_storage_key(sid).startswith("athena:owner_context:")


def test_a_jarvis_style_signed_owner_id_is_kept_for_an_owner_and_discarded_for_anyone_else():
    keys = _keys()
    signed = "own-" + "a" * 32 + "." + "b" * 24
    assert keys.usable_session_id(signed, keys.CALLER_CLASS_OWNER) == signed
    assert keys.usable_session_id(signed, keys.CALLER_CLASS_OTHER) != signed
    assert keys.usable_session_id(signed, keys.CALLER_CLASS_PUBLIC) != signed
    plain = "a" * 32 + "." + "b" * 24
    assert keys.usable_session_id(plain, keys.CALLER_CLASS_OTHER) == plain
    assert keys.usable_session_id(plain, keys.CALLER_CLASS_OWNER) != plain
