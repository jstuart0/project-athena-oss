"""jarvis-web speaks through /api/voice/synthesize: the text is normalized first.

The image has no `shared` package; it gets src/shared/tts_normalizer.py as a
single file beside main.py. These tests prove the proxy normalizes, off the
event loop, and that the file the image runs is the file the corpus tests cover.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from . import _jarvis_web_harness as h
from .test_tts_normalizer import CORPUS

main = h.main
REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_MODULE = REPO_ROOT / "src" / "shared" / "tts_normalizer.py"
SHIM = REPO_ROOT / "apps" / "jarvis-web" / "backend" / "tts_normalizer.py"
DOCKERFILE = REPO_ROOT / "apps" / "jarvis-web" / "Dockerfile"
BACKEND_COPY = "COPY apps/jarvis-web/backend/ /app/backend/"
HELPER_COPY = "COPY src/shared/tts_normalizer.py /app/backend/tts_normalizer.py"

RAW = "Winds 25mph."
SPOKEN = "Winds 25 miles per hour."


@pytest.fixture(autouse=True)
def _reset():
    h.configure()
    yield
    h.configure()


def _capture_curl(monkeypatch):
    posted = []

    def _run(argv, *args, **kwargs):
        posted.append(json.loads(argv[argv.index("-d") + 1]))
        return subprocess.CompletedProcess(argv, 0, stdout=b"RIFF", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)
    return posted


def _synthesize(text):
    return h.client().post(
        "/api/voice/synthesize", json={"text": text}, headers={**h.via_proxy(h.LAN), **h.CSRF},
    )


# --- (a) the proxy posts the spoken form --------------------------------------------------


def test_proxy_posts_the_normalized_text(monkeypatch):
    posted = _capture_curl(monkeypatch)
    assert _synthesize(RAW).status_code == 200
    assert posted == [{"text": SPOKEN}]


def test_text_with_nothing_speakable_is_refused_before_the_voice_service(monkeypatch):
    posted = _capture_curl(monkeypatch)
    resp = _synthesize("😀")
    assert resp.status_code == 422
    assert posted == []


# --- (b)(c)(d)(e) packaging ---------------------------------------------------------------


def _image_layout(tmp_path) -> Path:
    """/app/backend as the Dockerfile builds it: the backend directory, then the
    COPY lines that follow it."""
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    layout = tmp_path / "backend"
    shutil.copytree(REPO_ROOT / "apps/jarvis-web/backend", layout, ignore=shutil.ignore_patterns("__pycache__"))
    pattern = re.compile(r"^COPY\s+(src/shared/[\w.]+\.py)\s+/app/backend/([\w.]+\.py)\s*$")
    for line in lines[lines.index(BACKEND_COPY) + 1:]:
        match = pattern.match(line)
        if match:
            shutil.copyfile(REPO_ROOT / match.group(1), layout / match.group(2))
    return layout


def _run_isolated(layout: Path, code: str) -> subprocess.CompletedProcess:
    # -I: no PYTHONPATH, no user site, no cwd on sys.path: what the image has.
    prelude = f"import sys; sys.path.insert(0, {str(layout)!r})\n"
    return subprocess.run([sys.executable, "-I", "-c", prelude + code], cwd=layout, capture_output=True, text=True, timeout=60)


def test_normalizer_loads_in_the_image_layout_without_the_shared_package(tmp_path):
    layout = _image_layout(tmp_path)
    proc = _run_isolated(layout, (
        "import tts_normalizer\n"
        "assert tts_normalizer.__file__.startswith(sys.path[0]), tts_normalizer.__file__\n"
        "assert tts_normalizer.normalize_for_tts('Winds 25mph.') == 'Winds 25 miles per hour.'\n"
        "assert 'shared' not in sys.modules\n"
    ))
    assert proc.returncode == 0, proc.stderr[-600:]


def test_dockerfile_copies_exactly_the_shared_file_after_the_backend_copy():
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    copies = [i for i, line in enumerate(lines) if line.startswith("COPY") and "tts_normalizer" in line]
    assert len(copies) == 1
    assert lines[copies[0]] == HELPER_COPY, "source must be exactly src/shared/tts_normalizer.py"
    assert copies[0] > lines.index(BACKEND_COPY), "the COPY must come after the backend COPY, or the shim overwrites it"
    assert (REPO_ROOT / "src/shared/tts_normalizer.py").is_file()


def test_the_shim_holds_no_logic():
    tree = ast.parse(SHIM.read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.While, ast.Try))]
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert {"sys", "shared.tts_normalizer"} <= imports
    assert "globals().update" in ast.unparse(tree)


def test_the_shim_matches_the_client_throttle_shim_shape():
    def shape(path, name):
        text = path.read_text().replace(name, "NAME")
        return re.sub(r"\s+", " ", text)

    other = REPO_ROOT / "apps/jarvis-web/backend/client_throttle.py"
    assert shape(SHIM, "tts_normalizer") == shape(other, "client_throttle")


def test_the_module_is_stdlib_only_with_no_relative_imports():
    tree = ast.parse(SHARED_MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import would break the single-file COPY"
            imported.add((node.module or "").split(".")[0])
    imported.discard("__future__")
    assert imported, "floor: the module imports something"
    assert sorted(n for n in imported if n not in sys.stdlib_module_names) == []


# --- (f) content-equality parity -----------------------------------------------------------


def test_image_copy_gives_the_same_output_as_the_shared_module_over_the_corpus(tmp_path):
    from shared.tts_normalizer import normalize_for_tts

    layout = _image_layout(tmp_path)
    corpus_file = tmp_path / "corpus.json"
    corpus_file.write_text(json.dumps(list(CORPUS)))
    proc = _run_isolated(layout, (
        "import json, tts_normalizer\n"
        f"corpus = json.load(open({str(corpus_file)!r}))\n"
        "print(json.dumps([tts_normalizer.normalize_for_tts(t) for t in corpus]))\n"
    ))
    assert proc.returncode == 0, proc.stderr[-600:]
    image_output = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(CORPUS) >= 300
    assert image_output == [normalize_for_tts(t) for t in CORPUS]


def test_image_copy_is_the_shared_file_byte_for_byte(tmp_path):
    layout = _image_layout(tmp_path)
    assert (layout / "tts_normalizer.py").read_bytes() == SHARED_MODULE.read_bytes()


# --- (g) off the event loop -----------------------------------------------------------------


def test_a_long_digit_run_through_the_proxy_is_fast(monkeypatch):
    _capture_curl(monkeypatch)
    started = time.perf_counter()
    assert _synthesize("1" * 5000).status_code == 200
    assert time.perf_counter() - started < 1.0


def test_normalization_does_not_block_the_event_loop(monkeypatch):
    """A slow normalizer runs in a worker thread: a trivial request finishes
    while it is still going."""
    _capture_curl(monkeypatch)

    def slow(text):
        time.sleep(0.5)
        return text

    monkeypatch.setattr(main, "normalize_for_tts", slow)
    headers = {**h.via_proxy(h.LAN), **h.CSRF}

    async def scenario():
        transport = httpx.ASGITransport(app=main.app, client=(h.PROXY, 50000))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{h.HOST}") as client:
            finished = []

            async def synth():
                await client.post("/api/voice/synthesize", json={"text": "hi"}, headers=headers)
                finished.append("synthesize")

            async def trivial():
                await asyncio.sleep(0.1)
                await client.get("/health")
                finished.append("trivial")

            await asyncio.gather(synth(), trivial())
            return finished

    assert asyncio.run(scenario()) == ["trivial", "synthesize"]


# --- the channel jarvis-web tells the orchestrator ---------------------------------------------


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("claimed", ["voice", "text", "chat", None])
def test_chat_routes_pin_the_chat_channel(monkeypatch, path, claimed):
    out = h.install_outbound(monkeypatch)
    body = {"message": "hi"}
    if claimed is not None:
        body["interface_type"] = claimed
    resp = h.client().post(path, json=body, headers={**h.via_proxy(h.LAN), **h.CSRF})
    assert resp.status_code == 200
    assert [b["interface_type"] for b in out.orchestrator_bodies()] == ["chat"]
