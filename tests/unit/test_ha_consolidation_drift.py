"""One owner per concern for Home Assistant access: a second implementation fails here.

Every class scans the code that ships (``src`` minus the Jetson, admin-backend's
``app`` and ``main.py``, ``apps``) with the ``ast`` module, so it needs nothing but
the standard library and runs in the ``guards`` job. Each class states its
population, has a floor and a named member so it can't pass on an empty scan, and
has a negative control: the detector is run on planted source for each form it
claims to catch.

The admin-backend modules joined the shared path in the last phase, so the old
``PENDING_PHASE_3`` allowance is empty and ``test_no_admin_module_is_left_pending``
keeps it that way.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "admin/backend/app", "admin/backend/main.py", "apps")

HA_ENV_NAMES = ("HA_URL", "HA_TOKEN", "HA_WS_URL")
ADMIN_ROUTES = "admin/backend/app/routes/"

# Phase 3 brought the admin modules onto the shared path; nothing is pending.
# ``test_no_admin_module_is_left_pending`` fails if an entry is ever added back.
PENDING_PHASE_3: Dict[str, Set[Tuple[str, str]]] = {}

# The one place a ``ha_ws_session(profile=...)`` call may pass a parameter rather
# than a profile constant: admin's door, which refuses anything but PIPELINES.
PROFILE_PARAMETER_SITE = ("admin/backend/app/services/ha_access.py", "ws_session")


# -- scanning ------------------------------------------------------------------------------------


_SOURCES: Optional[List[Tuple[str, str]]] = None
_TREES: Dict[str, Optional[ast.AST]] = {}


def scanned_files() -> Iterator[Tuple[str, str]]:
    """``(relative path, source)`` for every shipped Python file (read once)."""
    global _SOURCES
    if _SOURCES is None:
        _SOURCES = []
        for entry in SCAN_ROOTS:
            base = ROOT / entry
            for path in [base] if base.is_file() else sorted(base.rglob("*.py")):
                rel = path.relative_to(ROOT).as_posix()
                if rel.startswith("src/jetson/") or "/tests/" in rel or "node_modules" in rel or "__pycache__" in rel:
                    continue
                _SOURCES.append((rel, path.read_text(encoding="utf-8", errors="ignore")))
    yield from _SOURCES


def shipped_tree(rel: str, source: str) -> Optional[ast.AST]:
    if rel not in _TREES:
        _TREES[rel] = parse(source)
    return _TREES[rel]


@lru_cache(maxsize=None)
def parse(source: str) -> Optional[ast.AST]:
    """The tree for ``source`` (memoized: the scans below share it), or None."""
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def scoped(tree: ast.AST) -> Iterator[Tuple[ast.AST, str]]:
    """Every node with its enclosing qualified function (``Class.method``,
    ``outer.inner`` or ``<module>``)."""

    def walk(node: ast.AST, stack: List[str]) -> Iterator[Tuple[ast.AST, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from walk(child, stack + [child.name])
            else:
                yield child, ".".join(stack) or "<module>"
                yield from walk(child, stack)

    yield from walk(tree, [])


def fold(node: ast.AST) -> Optional[str]:
    """The string a constant, a ``+`` of constants or an f-string of constants
    folds to, else None (so ``"HA_" + "TOKEN"`` is read as ``HA_TOKEN``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = fold(node.left), fold(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values if isinstance(v, ast.Constant)]
        return "".join(parts) if len(parts) == len(node.values) else None
    return None


def population(detector, files: Iterable[Tuple[str, str]]) -> Set[Tuple[str, str]]:
    """``{(file, qualified function)}`` over ``files`` for a detector that maps
    ``source -> iterable of qualified names``."""
    found: Set[Tuple[str, str]] = set()
    for rel, source in files:
        tree = parse(source)
        if tree is not None:
            found |= {(rel, qual) for qual in detector(tree)}
    return found


def shipped_population(detector, needles: Tuple[str, ...] = ()) -> Set[Tuple[str, str]]:
    """``population`` over the shipped files, skipping (without parsing) those that
    contain none of ``needles`` when the detector can only match text they hold."""
    found: Set[Tuple[str, str]] = set()
    for rel, source in scanned_files():
        if needles and not any(n in source for n in needles):
            continue
        tree = shipped_tree(rel, source)
        if tree is not None:
            found |= {(rel, qual) for qual in detector(tree)}
    return found


def planted(detector, source: str, file: str = "planted.py") -> Set[Tuple[str, str]]:
    return population(detector, [(file, source)])


# -- detectors --------------------------------------------------------------------------------------


def env_readers(tree: ast.AST) -> Set[str]:
    """Qualified functions that read an HA_* variable from the environment, in
    any import form: ``os.getenv``, ``getenv``, ``os.environ[...]``,
    ``os.environ.get``, ``environ.get`` or ``env.get`` (a mapping parameter)."""
    getenv_names, environ_names = {"getenv"}, {"environ", "env"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "getenv":
                    getenv_names.add(alias.asname or alias.name)
                if alias.name == "environ":
                    environ_names.add(alias.asname or alias.name)

    def is_environ(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in environ_names
        return isinstance(node, ast.Attribute) and node.attr == "environ"

    found: Set[str] = set()
    for node, qual in scoped(tree):
        key = None
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if isinstance(func, ast.Name) and func.id in getenv_names:
                key = fold(node.args[0])
            elif isinstance(func, ast.Attribute) and func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
                key = fold(node.args[0])
            elif isinstance(func, ast.Attribute) and func.attr == "get" and is_environ(func.value):
                key = fold(node.args[0])
        elif isinstance(node, ast.Subscript) and is_environ(node.value):
            key = fold(node.slice)
        if key in HA_ENV_NAMES:
            found.add(qual)
    return found


def ha_name_literals(tree: ast.AST) -> Set[str]:
    """Qualified functions holding a string that is exactly ``HA_URL``,
    ``HA_TOKEN`` or ``HA_WS_URL`` (after folding ``"HA_" + "TOKEN"``)."""
    found: Set[str] = set()
    for node, qual in scoped(tree):
        if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)) and fold(node) in HA_ENV_NAMES:
            found.add(qual)
    return found


def key_name_literals(tree: ast.AST) -> Set[str]:
    return {qual for node, qual in scoped(tree) if isinstance(node, ast.Constant) and node.value == "home-assistant"}


def bearer_sites(tree: ast.AST) -> Counter:
    """Qualified functions that build a ``Bearer ...`` string, with a count: an
    f-string or a ``+`` concatenation whose constant text contains ``Bearer``,
    or a constant starting with it. Where it lands (``headers=``, a subscript,
    ``request.headers[...] =``) doesn't matter: the string is the signal."""
    sites: Counter = Counter()
    for node, qual in scoped(tree):
        text = None
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            text = fold(node.left)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value if node.value.startswith("Bearer") else None
        if text and "Bearer" in text:
            sites[qual] += 1
    return sites


def auth_frame_sites(tree: ast.AST) -> Set[str]:
    """Qualified functions that build Home Assistant's WebSocket auth frame:
    a dict with an ``access_token`` key next to a ``type``, ``dict(type=...,
    access_token=...)``, or JSON text carrying both ``access_token`` and
    ``"type": "auth"``."""
    found: Set[str] = set()
    for node, qual in scoped(tree):
        if isinstance(node, ast.Dict):
            keys = {fold(k) for k in node.keys if k is not None}
            if "access_token" in keys and "type" in keys:
                found.add(qual)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
            names = {k.arg for k in node.keywords}
            if {"access_token", "type"} <= names:
                found.add(qual)
        elif isinstance(node, (ast.Constant, ast.JoinedStr)):
            text = fold(node) if isinstance(node, ast.Constant) else "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            if text and "access_token" in text and re.search(r"[\"']type[\"']\s*:\s*[\"']auth[\"']", text):
                found.add(qual)
    return found


def websocket_path_literals(tree: ast.AST) -> Set[str]:
    return {qual for node, qual in scoped(tree) if isinstance(node, ast.Constant) and node.value == "/api/websocket"}


def ws_url_reads(tree: ast.AST) -> Set[str]:
    """``.ws_url`` loads and ``websocket_url(`` calls."""
    found: Set[str] = set()
    for node, qual in scoped(tree):
        if isinstance(node, ast.Attribute) and node.attr == "ws_url" and isinstance(node.ctx, ast.Load):
            found.add(qual)
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) == "websocket_url" or getattr(node.func, "attr", None) == "websocket_url"):
            found.add(qual)
    return found


def websocket_library_users(tree: ast.AST) -> Set[str]:
    """Imports of the ``websockets`` package (any form), uses of
    ``websockets.<x>`` and ``ws_connect`` calls."""
    found: Set[str] = set()
    for node, qual in scoped(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "websockets" for a in node.names):
            found.add(qual)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "websockets":
            found.add(qual)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "websockets":
            found.add(qual)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "ws_connect":
            found.add(qual)
    return found


CONSTRUCTORS = (
    "HACredentialResolver", "StaticResolver", "HAAuthLatch", "HAChannel", "HATransport", "GatewayHA",
    "ha_ws_session", "prove_token",
)


def construction_sites(tree: ast.AST) -> Counter:
    """``Counter[(qualified function, constructor)]`` for every call of one of
    ``CONSTRUCTORS``, through ``Name(...)``, ``module.Name(...)``, an
    ``import ... as`` alias or a ``functools.partial(Name, ...)``."""
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in CONSTRUCTORS:
                    aliases[alias.asname or alias.name] = alias.name

    def resolve(func: ast.AST) -> Optional[str]:
        if isinstance(func, ast.Name):
            return aliases.get(func.id, func.id if func.id in CONSTRUCTORS else None)
        if isinstance(func, ast.Attribute) and func.attr in CONSTRUCTORS:
            return func.attr
        return None

    sites: Counter = Counter()
    for node, qual in scoped(tree):
        if not isinstance(node, ast.Call):
            continue
        name = resolve(node.func)
        if name is None and getattr(node.func, "attr", None) == "partial" and node.args:
            name = resolve(node.args[0])
        if name is None and getattr(node.func, "id", None) == "partial" and node.args:
            name = resolve(node.args[0])
        if name:
            sites[(qual, name)] += 1
    return sites


def construction_population() -> Counter:
    total: Counter = Counter()
    for rel, source in scanned_files():
        if not any(name in source for name in CONSTRUCTORS):
            continue
        tree = shipped_tree(rel, source)
        if tree is not None:
            for (qual, name), count in construction_sites(tree).items():
                total[(rel, qual, name)] += count
    return total


def channel_request_sites(tree: ast.AST) -> Set[str]:
    """``<receiver name>.request(`` where the receiver's name mentions ``channel``."""
    found: Set[str] = set()
    for node, qual in scoped(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "request":
            receiver = node.func.value
            name = receiver.id if isinstance(receiver, ast.Name) else receiver.attr if isinstance(receiver, ast.Attribute) else (
                getattr(receiver.func, "attr", getattr(receiver.func, "id", "")) if isinstance(receiver, ast.Call) else ""
            )
            if "channel" in name.lower():
                found.add(qual)
    return found


# ---------------------------------------------------------------------------------------------------
# One credential reader
# ---------------------------------------------------------------------------------------------------


class TestCredentialReaders:
    ALLOWED = {
        ("src/shared/ha_credentials.py", "resolve"),
        ("apps/jarvis-web/backend/main.py", "<module>"),  # D7: its image has no shared package; ticketed
    }

    def test_only_the_resolver_and_the_pinned_exception_read_the_environment(self):
        found = shipped_population(env_readers, ('HA_',))
        assert found == self.ALLOWED, sorted(found ^ self.ALLOWED)
        assert len(found) >= 2 and ("src/shared/ha_credentials.py", "resolve") in found

    def test_the_names_appear_as_strings_only_where_they_are_read(self):
        files = {rel for rel, _ in shipped_population(ha_name_literals, ('HA_',))}
        allowed = {rel for rel, _ in self.ALLOWED}
        assert files == allowed, sorted(files ^ allowed)

    def test_the_cached_config_has_no_field_for_them(self):
        tree = ast.parse((ROOT / "src/shared/config.py").read_text())
        fields = [
            n.target.id for n in ast.walk(tree)
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
        ]
        assert len(fields) > 50, "the scan must have found AthenaConfig's fields"
        assert [f for f in fields if re.search(r"ha_url|ha_token|ha_ws", f)] == []

    def test_negative_controls(self):
        assert planted(env_readers, 'import os\ndef f():\n    return os.getenv("HA_TOKEN")\n') == {("planted.py", "f")}
        assert planted(env_readers, 'from os import getenv\ndef g():\n    return getenv("HA_URL")\n') == {("planted.py", "g")}
        assert planted(env_readers, 'from os import getenv as grab\ndef g():\n    return grab("HA_URL")\n') == {("planted.py", "g")}
        assert planted(env_readers, 'import os\nX = os.environ["HA_WS_URL"]\n') == {("planted.py", "<module>")}
        assert planted(env_readers, 'import os\ndef h():\n    return os.environ.get("HA_TOKEN", "")\n') == {("planted.py", "h")}
        assert planted(env_readers, 'def k(env):\n    return env.get("HA_URL")\n') == {("planted.py", "k")}
        assert planted(env_readers, 'import os\ndef m():\n    return os.getenv("HA_" + "TOKEN")\n') == {("planted.py", "m")}
        assert planted(ha_name_literals, 'def n():\n    return "HA_" + "TOKEN"\n') == {("planted.py", "n")}
        assert planted(env_readers, 'import os\ndef quiet():\n    return os.getenv("SOMETHING_ELSE")\n') == set()
        field = ast.parse("class AthenaConfig:\n    ha_token: str = ''\n")
        assert [n.target.id for n in ast.walk(field) if isinstance(n, ast.AnnAssign)] == ["ha_token"]


# ---------------------------------------------------------------------------------------------------
# One name for the admin key
# ---------------------------------------------------------------------------------------------------


class TestKeyNameLiteral:
    def test_the_key_name_is_written_once(self):
        found = shipped_population(key_name_literals, ('home-assistant',))
        expected = {("src/shared/ha_credentials.py", "<module>")}
        assert found == expected, sorted(found ^ expected)

    def test_negative_control(self):
        planted_call = 'async def f(c):\n    return await c.get_external_api_key("home-assistant")\n'
        assert planted(key_name_literals, planted_call) == {("planted.py", "f")}


# ---------------------------------------------------------------------------------------------------
# One place builds the Bearer header
# ---------------------------------------------------------------------------------------------------


class TestBearerSites:
    HA = {("src/shared/ha_http.py", "HATransport.handle_async_request")}
    # jarvis-web calls Home Assistant with its own env token (D7, ticketed): pinned by (function, count).
    JARVIS_BEARER = {("apps/jarvis-web/backend/main.py", "get_ha_headers"): 2}
    NON_HA = {
        ("admin/backend/app/auth/oidc.py", "decode_access_token"): "Authentik OIDC",
        ("admin/backend/app/auth/oidc.py", "get_authentik_userinfo"): "Authentik OIDC",
        ("admin/backend/app/auth/oidc.py", "get_current_user"): "Authentik OIDC",
        ("admin/backend/app/routes/cloud_providers.py", "check_provider_health"): "a cloud LLM provider's API key",
        ("admin/backend/app/services/k8s_control.py", "K8sDeploymentClient._client"): "the Kubernetes API",
        ("admin/backend/app/utils/service_auth.py", "verify_service_or_oidc"): "reads an incoming Authorization header",
        ("apps/jarvis-web/backend/caller_auth.py", "_call_auth_me"): "admin-backend's auth/me",
        ("src/gateway/main.py", "validate_api_key"): "reads an incoming Authorization header",
        ("src/gateway/speaker_room.py", "trusted_voice_caller"): "reads an incoming Authorization header",
        ("src/orchestrator/search_providers/eventbrite.py", "EventbriteProvider.__init__"): "Eventbrite's API",
        ("src/rag/base_rag_service.py", "BaseRAGService._make_api_call"): "a RAG provider's API key",
        ("src/rag/brightdata/main.py", "lifespan"): "Bright Data's API",
    }

    def sites(self) -> Dict[Tuple[str, str], int]:
        found: Dict[Tuple[str, str], int] = {}
        for rel, source in scanned_files():
            if "Bearer" not in source:
                continue
            tree = shipped_tree(rel, source)
            if tree is not None:
                for qual, count in bearer_sites(tree).items():
                    found[(rel, qual)] = count
        return found

    def test_the_header_is_built_in_the_transport_and_nowhere_else_that_is_not_classified(self):
        found = self.sites()
        unclassified = set(found) - self.HA - set(self.JARVIS_BEARER) - set(self.NON_HA)
        assert unclassified == set(), f"a new Bearer site: classify it or route it through HATransport: {sorted(unclassified)}"
        assert self.HA <= set(found), "the transport must still build the header"
        assert {site: found[site] for site in self.JARVIS_BEARER} == self.JARVIS_BEARER

    def test_the_non_ha_allowlist_has_no_dead_entries(self):
        assert set(self.NON_HA) <= set(self.sites()), sorted(set(self.NON_HA) - set(self.sites()))

    def test_negative_controls(self):
        forms = {
            "headers kwarg": 'import httpx\ndef a(token):\n    return httpx.get(u, headers={"Authorization": f"Bearer {token}"})\n',
            "subscript": 'def b(request, token):\n    request.headers["Authorization"] = f"Bearer {token}"\n',
            "requests": 'import requests\ndef c(t):\n    return requests.get(u, headers={"Authorization": "Bearer " + t})\n',
            "aiohttp": 'import aiohttp\ndef d(t):\n    return aiohttp.ClientSession(headers={"Authorization": f"Bearer {t}"})\n',
        }
        for form, source in forms.items():
            tree = ast.parse(source)
            assert sum(bearer_sites(tree).values()) >= 1, form
        assert sum(bearer_sites(ast.parse("def e(t):\n    return {'X-Key': t}\n")).values()) == 0


# ---------------------------------------------------------------------------------------------------
# One WebSocket handshake
# ---------------------------------------------------------------------------------------------------


class TestWebSocketAuth:
    # Music Assistant, not Home Assistant: its auth frame uses ``token``, not ``access_token``.
    GATEWAY_MUSIC_ASSISTANT = {
        ("src/gateway/main.py", name)
        for name in ("<module>", "music_search", "music_play", "music_assistant_websocket_proxy", "sendspin_websocket_proxy")
    }
    JARVIS_WEBSOCKETS = {
        ("apps/jarvis-web/backend/main.py", name)
        for name in ("<module>", "music_assistant_websocket_proxy", "sendspin_websocket_proxy")
    }

    def test_the_auth_frame_is_built_in_ha_ws_only(self):
        found = shipped_population(auth_frame_sites, ('access_token',))
        expected = {("src/shared/ha_ws.py", "_handshake")}
        assert found <= expected and ("src/shared/ha_ws.py", "_handshake") in found, sorted(found ^ expected)

    def test_the_websocket_path_is_written_in_the_resolver_only(self):
        found = shipped_population(websocket_path_literals, ('/api/websocket',))
        expected = {("src/shared/ha_credentials.py", "<module>")}
        assert found <= expected and ("src/shared/ha_credentials.py", "<module>") in found, sorted(found ^ expected)

    def test_the_derived_websocket_url_is_read_by_the_handshake_only(self):
        found = shipped_population(ws_url_reads, ('ws_url', 'websocket_url'))
        files = {rel for rel, _ in found}
        allowed = {"src/shared/ha_credentials.py", "src/shared/ha_ws.py"} 
        assert files <= allowed and "src/shared/ha_ws.py" in files, sorted(files ^ allowed)

    def test_the_websockets_package_is_used_by_ha_ws_and_the_music_assistant_proxies_only(self):
        found = shipped_population(websocket_library_users, ('websockets', 'ws_connect'))
        expected = {("src/shared/ha_ws.py", "_default_connect")} | self.GATEWAY_MUSIC_ASSISTANT | self.JARVIS_WEBSOCKETS
        assert found <= expected, f"a new websocket user: {sorted(found - expected)}"
        assert ("src/shared/ha_ws.py", "_default_connect") in found
        assert self.GATEWAY_MUSIC_ASSISTANT <= found, sorted(self.GATEWAY_MUSIC_ASSISTANT - found)

    def test_negative_controls(self):
        assert planted(auth_frame_sites, 'def a(t):\n    return dict(type="auth", access_token=t)\n') == {("planted.py", "a")}
        assert planted(auth_frame_sites, 'def b(t):\n    return {"type": "auth", "access_token": t}\n') == {("planted.py", "b")}
        assert planted(auth_frame_sites, 'def c(t):\n    return f\'{{"type": "auth", "access_token": "{t}"}}\'\n') == {("planted.py", "c")}
        assert planted(auth_frame_sites, 'def fine(t):\n    return {"access_token": t, "token_type": "bearer"}\n') == set()
        assert planted(websocket_library_users, "from websockets import connect\n") == {("planted.py", "<module>")}
        assert planted(websocket_library_users, "import websockets.asyncio.client\n") == {("planted.py", "<module>")}
        assert planted(websocket_library_users, "def f(s):\n    return websockets.asyncio.client.connect(u)\n") == {("planted.py", "f")}
        assert planted(websocket_library_users, "def g(s):\n    return s.ws_connect(u)\n") == {("planted.py", "g")}
        assert planted(websocket_path_literals, 'def h():\n    return "/api/websocket"\n') == {("planted.py", "h")}
        assert planted(ws_url_reads, "def k(c):\n    return c.ws_url\n") == {("planted.py", "k")}


class TestWebSocketProfiles:
    PROFILE_NAMES = {"REGISTRY_READ", "PIPELINES", "PROOF"}

    def test_every_session_names_a_profile_constant(self):
        calls = 0
        for rel, source in scanned_files():
            tree = parse(source)
            if tree is None:
                continue
            for node, qual in scoped(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ha_ws_session":
                    calls += 1
                    profile = next((k.value for k in node.keywords if k.arg == "profile"), None)
                    named = getattr(profile, "id", getattr(profile, "attr", None))
                    allowed_parameter = (rel, qual) == PROFILE_PARAMETER_SITE and named == "profile"
                    assert named in self.PROFILE_NAMES or allowed_parameter, (rel, qual, named)
        assert calls >= 1

    def test_the_registry_reads_with_its_own_profile_and_holds_no_writer(self):
        source = (ROOT / "src/shared/ha_registry.py").read_text()
        assert "profile=REGISTRY_READ" in source
        for word in ("call_service", "assist_pipeline", "set_preferred"):
            assert word not in source, word

    def test_assist_pipeline_frames_are_named_in_ha_ws_and_the_pipelines_route_only(self):
        files = {
            rel for rel, source in scanned_files()
            if re.search(r"assist_pipeline/", source)
        }
        assert files == {"src/shared/ha_ws.py", "admin/backend/app/routes/ha_pipelines.py"}

    def test_the_registrys_allowlist_is_the_registry_read_profile_plus_auth(self):
        registry = ast.parse((ROOT / "src/shared/ha_registry.py").read_text())
        listed = next(
            n.value for n in registry.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_LIST_TYPES"
        )
        registry_types = {pair.elts[1].value for pair in listed.elts}
        ws = ast.parse((ROOT / "src/shared/ha_ws.py").read_text())
        profile = next(
            n.value for n in ws.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "REGISTRY_READ"
        )
        profile_types = {el.value for el in ast.walk(profile.args[1]) if isinstance(el, ast.Constant)}
        assert registry_types == profile_types == {
            "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list",
        }
        allowed = next(
            n.value for n in registry.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_ALLOWED_TYPES"
        )
        assert "auth" in {c.value for c in ast.walk(allowed) if isinstance(c, ast.Constant)}
        assert {c.value for c in ast.walk(allowed) if isinstance(c, ast.Constant)} == {"auth"}  # the rest is derived from _LIST_TYPES


# ---------------------------------------------------------------------------------------------------
# Construction sites: one resolver, one latch, one channel per process
# ---------------------------------------------------------------------------------------------------


class TestConstructionSites:
    PROCESS = "src/orchestrator/main.py", "src/gateway/main.py"
    EXPECTED: Counter = Counter({
        ("src/orchestrator/main.py", "lifespan", "HACredentialResolver"): 1,
        ("src/gateway/main.py", "lifespan", "HACredentialResolver"): 1,
        ("src/orchestrator/room_probe.py", "main", "StaticResolver"): 1,
        ("src/orchestrator/main.py", "lifespan", "HAAuthLatch"): 1,
        ("src/gateway/main.py", "lifespan", "HAAuthLatch"): 1,
        ("src/orchestrator/room_probe.py", "main", "HAAuthLatch"): 1,
        ("src/orchestrator/main.py", "_build_ha_channels", "HAChannel"): 1,
        ("src/gateway/main.py", "lifespan", "HAChannel"): 1,
        ("src/orchestrator/room_probe.py", "_live_states", "HAChannel"): 1,
        ("src/shared/ha_http.py", "HAChannel.__init__", "HATransport"): 1,
        ("src/gateway/main.py", "lifespan", "GatewayHA"): 1,
        ("src/shared/ha_registry.py", "snapshot_fetcher.fetch", "ha_ws_session"): 1,
        ("src/shared/ha_http.py", "HATransport._prove", "prove_token"): 1,
        ("admin/backend/app/services/ha_access.py", "init", "HACredentialResolver"): 1,
        ("admin/backend/app/services/ha_access.py", "init", "HAAuthLatch"): 1,
        ("admin/backend/app/services/ha_access.py", "init", "HAChannel"): 1,
        ("admin/backend/app/services/ha_access.py", "ws_session", "ha_ws_session"): 1,
    })
    def test_the_multiset_of_construction_sites_is_exact(self):
        found = construction_population()
        assert found == self.EXPECTED, {"missing": dict(self.EXPECTED - found), "extra": dict(found - self.EXPECTED)}

    def test_a_latch_is_never_built_without_redis_in_production(self):
        bad = []
        for rel, source in scanned_files():
            tree = parse(source)
            if tree is None or rel == "src/orchestrator/room_probe.py":
                continue
            for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for call in [n for n in ast.walk(function) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "HAAuthLatch"]:
                    redis = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "redis"), None)
                    none_names = {
                        t.id for a in ast.walk(function) if isinstance(a, ast.Assign) and isinstance(a.value, ast.Constant) and a.value.value is None
                        for t in a.targets if isinstance(t, ast.Name)
                    }
                    if redis is None or (isinstance(redis, ast.Constant) and redis.value is None) or (isinstance(redis, ast.Name) and redis.id in none_names):
                        bad.append((rel, function.name))
        assert bad == []

    def test_one_latch_per_process_reaches_the_channel_the_registry_and_the_publisher(self):
        for rel in self.PROCESS:
            tree = ast.parse((ROOT / rel).read_text())
            lifespan = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
            built = {
                t.id for n in ast.walk(lifespan) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", getattr(n.value.func, "id", "")) == "HAAuthLatch"
                for t in n.targets if isinstance(t, ast.Name)
            }
            assert len(built) == 1, (rel, built)
            passed = set()
            for call in [n for n in ast.walk(lifespan) if isinstance(n, ast.Call)]:
                name = getattr(call.func, "attr", getattr(call.func, "id", ""))
                if name in ("HAChannel", "_build_ha_channels") and len(call.args) > 1 and isinstance(call.args[1], ast.Name):
                    passed.add(call.args[1].id)
                if name in ("create_registry", "create_publisher"):
                    passed |= {k.value.id for k in call.keywords if k.arg == "latch" and isinstance(k.value, ast.Name)}
            assert passed == built, (rel, passed, built)

    def test_negative_controls(self):
        def counted(source):
            return construction_sites(ast.parse(source))

        assert counted("def f():\n    return HAAuthLatch(None)\n") == {("f", "HAAuthLatch"): 1}
        assert counted("import ha_auth as x\ndef f():\n    return x.HAAuthLatch(None)\n") == {("f", "HAAuthLatch"): 1}
        assert counted("from shared.ha_auth import HAAuthLatch as L\ndef f():\n    return L(None)\n") == {("f", "HAAuthLatch"): 1}
        assert counted("def f():\n    return shared.ha_auth.HAAuthLatch(None)\n") == {("f", "HAAuthLatch"): 1}
        assert counted("import functools\ndef f():\n    return functools.partial(HAAuthLatch, None)\n") == {("f", "HAAuthLatch"): 1}
        assert counted("def f():\n    a = HAAuthLatch(r)\n    b = HAAuthLatch(r)\n") == {("f", "HAAuthLatch"): 2}
        assert counted("def f(c):\n    return c.other()\n") == {}


# ---------------------------------------------------------------------------------------------------
# The orchestrator's write path, the gateway's surface, the registry readers
# ---------------------------------------------------------------------------------------------------


class TestOrchestratorHAWritePath:
    def test_the_channel_is_built_in_one_function_of_the_orchestrator(self):
        sites = {
            (rel, qual) for (rel, qual, name) in construction_population()
            if name == "HAChannel" and rel.startswith("src/orchestrator/")
        }
        assert sites == {("src/orchestrator/main.py", "_build_ha_channels"), ("src/orchestrator/room_probe.py", "_live_states")}

    def test_requests_on_a_channel_are_made_by_the_client_and_one_read_only_tool(self):
        found = shipped_population(channel_request_sites, ('.request(',))
        files = {rel for rel, _ in found if rel.startswith(("src/orchestrator/", "src/shared/"))}
        assert files == {"src/shared/ha_client.py", "src/orchestrator/room_probe.py"}
        probe = ast.parse((ROOT / "src/orchestrator/room_probe.py").read_text())
        requests = [n for n in ast.walk(probe) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "request"]
        assert [(a.args[0].value, a.args[1].value) for a in requests] == [("GET", "/api/states")]

    def test_no_runtime_slot_receives_a_channel(self):
        bad = []
        for rel, source in scanned_files():
            tree = parse(source)
            if tree is None or not rel.startswith("src/orchestrator/"):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "").startswith("set_") and getattr(node.func.value, "id", "") == "_runtime":
                    for arg in node.args:
                        if "channel" in ast.dump(arg).lower() or getattr(arg, "id", "") == "HAChannel":
                            bad.append((rel, node.lineno))
        assert bad == []

    def test_the_client_keeps_its_channel_where_the_permission_guard_cannot_reach_it(self):
        client = ast.parse((ROOT / "src/shared/ha_client.py").read_text())
        assigned = {t.attr for n in ast.walk(client) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Attribute)}
        assert "_channel" in assigned
        guard = ast.parse((ROOT / "src/orchestrator/mode_permission.py").read_text())
        allowlist = next(
            n.value for n in ast.walk(guard)
            if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_READ_ALLOWLIST" for t in n.targets)
        )
        assert "_channel" not in {c.value for c in ast.walk(allowlist) if isinstance(c, ast.Constant)}

    def test_negative_controls(self):
        assert planted(channel_request_sites, "async def f(channel):\n    await channel.request('POST', '/api/services/a/b')\n") == {("planted.py", "f")}
        assert planted(channel_request_sites, "async def g(self):\n    await self._channel.request('GET', '/api/states')\n") == {("planted.py", "g")}
        assert planted(channel_request_sites, "async def h(self):\n    await self.get_channel().request('GET', '/x')\n") == {("planted.py", "h")}
        assert planted(channel_request_sites, "async def k(client):\n    await client.request('GET', '/x')\n") == set()


class TestGatewayHASurface:
    ALLOWED_ATTRIBUTES = {"read_states", "announce", "configured", "aclose"}
    HOLDER_CONSTRUCTORS = ("HACredentialResolver", "HAChannel", "HAAuthLatch")

    @staticmethod
    def attribute_accesses(tree: ast.AST) -> Set[str]:
        return {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "gateway_ha"
        }

    def test_the_gateway_uses_two_operations_and_the_lifecycle(self):
        used = set()
        for path in sorted((ROOT / "src/gateway").rglob("*.py")):
            used |= self.attribute_accesses(ast.parse(path.read_text(encoding="utf-8", errors="ignore")))
        assert used and used <= self.ALLOWED_ATTRIBUTES, sorted(used - self.ALLOWED_ATTRIBUTES)

    def test_gateway_ha_makes_exactly_two_requests_with_literal_method_and_path(self):
        tree = ast.parse((ROOT / "src/gateway/gateway_ha.py").read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "request"]
        assert sorted((c.args[0].value, c.args[1].value) for c in calls) == [
            ("GET", "/api/states"), ("POST", "/api/services/assist_satellite/announce"),
        ]

    def holders(self, source: str) -> List[str]:
        """Assignments to ``app.state.<x>`` or to a ``global`` name whose value is
        a resolver, channel or latch (or a local bound to one)."""
        tree = ast.parse(source)
        bad: List[str] = []
        for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            def is_holder(value: ast.AST, locals_: Set[str]) -> bool:
                if isinstance(value, ast.Call):
                    return getattr(value.func, "id", getattr(value.func, "attr", "")) in self.HOLDER_CONSTRUCTORS
                return isinstance(value, ast.Name) and value.id in locals_

            locals_ = {
                t.id for n in ast.walk(function) if isinstance(n, ast.Assign) and is_holder(n.value, set())
                for t in n.targets if isinstance(t, ast.Name)
            }
            globals_ = {name for n in ast.walk(function) if isinstance(n, ast.Global) for name in n.names}
            for n in ast.walk(function):
                if isinstance(n, ast.Assign) and is_holder(n.value, locals_):
                    for target in n.targets:
                        if isinstance(target, ast.Attribute) and "state" in ast.dump(target.value):
                            bad.append(f"{function.name}: app.state.{target.attr}")
                        if isinstance(target, ast.Name) and target.id in globals_:
                            bad.append(f"{function.name}: global {target.id}")
        return bad

    def test_the_resolver_the_channel_and_the_latch_are_lifespan_locals_only(self):
        for path in sorted((ROOT / "src/gateway").rglob("*.py")):
            assert self.holders(path.read_text(encoding="utf-8", errors="ignore")) == [], path.name

    def test_negative_controls(self):
        assert self.holders("def f(app, resolver):\n    resolver = HACredentialResolver(s)\n    app.state.ha_resolver = resolver\n") == ["f: app.state.ha_resolver"]
        assert self.holders("def g():\n    global ha_channel\n    ha_channel = HAChannel(r, l)\n") == ["g: global ha_channel"]
        assert self.holders("def h():\n    global gateway_ha\n    gateway_ha = GatewayHA(c)\n") == []
        assert self.attribute_accesses(ast.parse("async def k():\n    await gateway_ha.call_service('a', 'b')\n")) == {"call_service"}


class TestOrchestratorRegistryReads:
    RECEIVERS = {"registry", "_registry", "ha_registry"}

    def reads(self, tree: ast.AST, attribute: str) -> Set[str]:
        found = set()
        for node, qual in scoped(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute:
                receiver = node.func.value
                if (isinstance(receiver, ast.Name) and receiver.id in self.RECEIVERS) or (
                    isinstance(receiver, ast.Attribute) and receiver.attr in self.RECEIVERS
                ) or (
                    isinstance(receiver, ast.Call) and getattr(receiver.func, "attr", "") == "get_ha_registry"
                ):
                    found.add(qual)
        return found

    def population(self, attribute: str) -> Set[Tuple[str, str]]:
        found = set()
        for rel, source in scanned_files():
            tree = parse(source)
            if tree is not None and rel.startswith("src/orchestrator/"):
                found |= {(rel, qual) for qual in self.reads(tree, attribute)}
        return found

    def test_no_orchestrator_reader_uses_current_on_the_registry(self):
        assert self.population("current") == set()

    def test_the_readers_that_ask_for_a_refresh_are_known(self):
        assert self.population("read") == {
            ("src/orchestrator/room_directory.py", "RoomDirectoryProvider.current"),
            ("src/orchestrator/smart_home_controller.py", "SmartHomeController._registry_snapshot"),
        }

    def test_the_probe_shim_answers_both_calls(self):
        tree = ast.parse((ROOT / "src/orchestrator/room_probe.py").read_text())
        shim = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_Snapshot")
        assert {f.name for f in shim.body if isinstance(f, ast.FunctionDef)} >= {"read", "current"}

    def test_negative_control(self):
        assert self.reads(ast.parse("def f(ha_registry):\n    return ha_registry.current()\n"), "current") == {"f"}
        assert self.reads(ast.parse("def g():\n    return _runtime.get_ha_registry().current()\n"), "current") == {"g"}
        assert self.reads(ast.parse("def h(room_directory):\n    return room_directory.current()\n"), "current") == set()


def test_no_admin_module_is_left_pending():
    assert PENDING_PHASE_3 == {}


class TestVocabularyOwner:
    NAMES = ("OUTCOMES", "REGISTRY_STATES", "FAILURE_CLASSES", "CALLERS")
    AUTH_CONSTANTS = (
        "BUDGET", "REQUEST_ALLOWANCE", "PROOF_TIMEOUT", "GATE_LEASE", "GATE_WAIT", "PROVEN_TTL", "CANARY_AFTER",
        "INFLIGHT_CAP", "PROBE_SCHEDULE_SECONDS",
    )

    @staticmethod
    def assigned(names: Tuple[str, ...]) -> Set[Tuple[str, str]]:
        found = set()
        for rel, source in scanned_files():
            tree = parse(source)
            if tree is None:
                continue
            for node in ast.walk(tree):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in names:
                        found.add((rel, target.id))
        return found

    def test_the_link_vocabulary_is_assigned_in_ha_link_only(self):
        files = {rel for rel, _ in self.assigned(self.NAMES)}
        assert files == {"src/shared/ha_link.py"}

    def test_the_budget_constants_are_assigned_in_ha_auth_only(self):
        found = self.assigned(self.AUTH_CONSTANTS)
        assert {rel for rel, _ in found} == {"src/shared/ha_auth.py"}
        assert {name for _, name in found} == set(self.AUTH_CONSTANTS)

    def test_the_registry_states_are_words_of_the_link_vocabulary(self):
        registry = ast.parse((ROOT / "src/shared/ha_registry.py").read_text())
        states = {
            n.value.value for n in registry.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "").startswith("STATE_") and isinstance(n.value, ast.Constant)
        }
        link = ast.parse((ROOT / "src/shared/ha_link.py").read_text())
        vocabulary = next(
            n.value for n in link.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "REGISTRY_STATES"
        )
        assert states and states <= {c.value for c in ast.walk(vocabulary) if isinstance(c, ast.Constant)}

    def test_the_admin_classifier_declares_no_vocabulary_of_its_own(self):
        path = "admin/backend/app/services/ha_status.py"
        source = (ROOT / path).read_text()
        words = ("auth_rejected", "forbidden", "unreachable", "refused", "disabled")
        declares = any(
            re.search(r"Literal\[[^\]]*" + word, source) for word in words
        )
        assert not declares, "ha_status.py must derive its words from shared.ha_link"


class TestJarvisWebPinned:
    PATH = "apps/jarvis-web/backend/main.py"

    def source(self) -> str:
        return (ROOT / self.PATH).read_text()

    def test_it_reads_exactly_the_url_and_the_token_at_module_level(self):
        tree = ast.parse(self.source())
        reads = {
            fold(a.args[0]) for n, qual in scoped(tree) if qual == "<module>"
            for a in [n] if isinstance(a, ast.Call) and a.args and fold(a.args[0]) in HA_ENV_NAMES
        }
        assert reads == {"HA_URL", "HA_TOKEN"}
        assert "HA_WS_URL" not in self.source()

    def test_it_never_reads_the_admin_key(self):
        assert not re.search(r"external-api-keys/[^\n]*home-assistant", self.source())
        assert "home-assistant" not in self.source()

    def test_its_bearer_and_websocket_footprint_is_the_pinned_one(self):
        tree = ast.parse(self.source())
        assert dict(bearer_sites(tree)) == {"get_ha_headers": 2}
        assert auth_frame_sites(tree) == set()
