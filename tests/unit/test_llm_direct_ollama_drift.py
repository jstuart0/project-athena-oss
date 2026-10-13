"""Every direct Ollama request outside the router is a known site.

`shared/llm_router.py` is where a request to Ollama gets its context planned,
checked and counted. Twelve other places build an `/api/generate` or `/api/chat`
URL themselves. They are listed here, each with the reason it does not go
through the router, so a thirteenth fails this test until it is either moved
onto the router or added with a reason.

A "site" is a string literal (or an f-string part) containing one of the two
paths that is a call argument or the right-hand side of an assignment. Not
sites: docstrings and comments, decorator arguments (route declarations), dict
keys (route maps) and the arguments of string-method calls such as
``.replace``.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src", "apps", "admin/backend/app")
ROUTER = "src/shared/llm_router.py"
PATHS = ("/api/generate", "/api/chat")
STRING_METHODS = frozenset({
    "replace", "startswith", "endswith", "removeprefix", "removesuffix", "split", "rsplit", "strip", "lstrip",
    "rstrip", "find", "rfind", "index", "count", "partition", "format", "join",
})

# (file, enclosing function) -> sites in that function, and why it is direct.
ALLOWED = {
    ("src/shared/ollama_client.py", "generate"): (1, "OllamaClient (the gateway's fallback chat client): a general-purpose client with its own caller-chosen options"),
    ("src/shared/ollama_client.py", "chat"): (1, "OllamaClient (the gateway's fallback chat client): a general-purpose client with its own caller-chosen options"),
    ("src/orchestrator/validator.py", "_cross_check"): (1, "response-validator cross-check: a verdict prompt, not an answer"),
    ("src/orchestrator/validator.py", "_attempt_fix"): (1, "response-validator repair prompt, bounded by the response being repaired"),
    ("src/orchestrator/validator.py", "_get_better_response"): (1, "response-validator retry prompt, bounded by the response being replaced"),
    ("src/gateway/intent_prerouter.py", "classify_intent"): (1, "gateway intent pre-router: a ten-token label over a short query"),
    ("src/gateway/intent_prerouter.py", "handle_simple_intent"): (1, "gateway intent pre-router: a two-sentence reply to a simple query"),
    ("src/gateway/main.py", "classify_intent_llm"): (1, "gateway athena-vs-general classifier: a one-word label over a short query"),
    ("admin/backend/app/routes/voice_tests.py", "test_full_pipeline"): (1, "admin voice-test harness"),
    ("admin/backend/app/routes/voice_tests.py", "test_llm_processing"): (1, "admin voice-test harness"),
    ("admin/backend/app/routes/service_control.py", "load_ollama_model"): (1, "admin model load: a one-token generate that loads weights"),
    ("admin/backend/app/routes/service_control.py", "unload_ollama_model"): (1, "admin model unload: keep_alive 0, no prompt"),
}
EXPECTED_POPULATION = 12
EXPECTED_MEMBER = ("src/gateway/intent_prerouter.py", "classify_intent")


def _holds_path(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and any(p in n.value for p in PATHS)
        for n in ast.walk(node)
    )


def sites(source: str, *, relative: str):
    """[(file, enclosing function)] for each string expression that holds an
    Ollama path as a call argument or an assignment's right-hand side."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    def in_decorator(node):
        child = node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child in parent.decorator_list:
                return True
            child = parent
        return False

    for node in ast.walk(tree):
        values = []
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Attribute) and node.func.attr in STRING_METHODS
        ):
            values = [*node.args, *(k.value for k in node.keywords)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            values = [node.value]
        for value in values:
            if isinstance(value, (ast.Constant, ast.JoinedStr)) and _holds_path(value) and not in_decorator(value):
                found.append((relative, enclosing(node)))
    return found


def scan():
    found = []
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative == ROUTER or "/tests/" in relative or "/node_modules/" in relative:
                continue
            source = path.read_text(encoding="utf-8")
            try:
                found.extend(sites(source, relative=relative))
            except SyntaxError:
                # A file that isn't Python (src/jetson/athena_lite_llm.py is a
                # template) can't be scanned, so it must not hold an Ollama path.
                assert not any(p in source for p in PATHS), f"{relative} is unparseable and names an Ollama path"
    return found


def test_the_direct_sites_are_exactly_the_listed_ones():
    assert Counter(scan()) == Counter({key: count for key, (count, _) in ALLOWED.items()})


def test_population_floor_and_named_member():
    found = scan()
    assert len(found) == EXPECTED_POPULATION == sum(count for count, _ in ALLOWED.values())
    assert EXPECTED_MEMBER in found


def test_every_allowed_site_carries_a_reason():
    assert all(reason.strip() for _, reason in ALLOWED.values())


class TestScannerRules:
    """Planted cases: what is a site and what is not."""

    @staticmethod
    def found(source):
        return sites(source, relative="x.py")

    def test_a_post_call_with_the_path_is_a_site(self):
        assert self.found("async def f(c):\n    await c.post('/api/generate', json={})\n") == [("x.py", "f")]

    def test_an_fstring_url_is_a_site(self):
        assert self.found("async def g(c, u):\n    await c.post(f'{u}/api/chat')\n") == [("x.py", "g")]

    def test_an_assignment_is_a_site(self):
        assert self.found("def h(u):\n    url = f'{u}/api/generate'\n    return url\n") == [("x.py", "h")]

    def test_a_new_direct_call_is_one_more_site(self):
        source = "async def a(c):\n    await c.post('/api/generate')\n    await c.stream('POST', '/api/chat')\n"
        assert self.found(source) == [("x.py", "a"), ("x.py", "a")]

    def test_a_docstring_a_comment_a_route_decorator_and_a_dict_key_are_not_sites(self):
        source = (
            '"""Talks to /api/chat."""\n'
            "@app.post('/api/chat')\n"
            "async def route():\n"
            "    '''POST /api/generate'''\n"
            "    # c.post('/api/generate')\n"
            "    return {'POST /api/chat': 'relay'}\n"
            "ROUTES = {'POST /api/chat/stream': 1}\n"
        )
        assert self.found(source) == []

    def test_a_string_method_argument_is_not_a_site(self):
        assert self.found("def r(u):\n    return u.replace('/api/chat', '/api/health')\n") == []

    def test_a_call_that_does_not_mention_the_paths_is_not_a_site(self):
        assert self.found("async def f(c):\n    await c.post('/api/embeddings')\n") == []
