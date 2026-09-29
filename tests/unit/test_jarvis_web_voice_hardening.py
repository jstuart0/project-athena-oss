"""jarvis-web's voice endpoints: the upload can't make ffmpeg fetch or
open anything, and each client has a budget (xander L-a, L-b).
"""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading

import pytest

from . import _jarvis_web_harness as h

main = h.main
caller_auth = h.caller_auth
REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _reset():
    h.configure()
    yield
    h.configure()


def _ffmpeg_argv(captured):
    return next(argv for argv in captured if argv and argv[0] == "ffmpeg")


def _transcribe(content, headers=None):
    return h.client().post(
        "/api/voice/transcribe",
        files={"audio": ("recording.webm", content, "audio/webm")},
        headers={**h.via_proxy(h.LAN), **h.CSRF, **(headers or {})},
    )


def test_ffmpeg_input_format_pinned_and_file_protocol_only(monkeypatch):
    """Named: the recording is decoded as WebM with only the file protocol,
    both set before -i (the page records audio/webm)."""
    captured = []

    def _run(argv, *a, **kw):
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout='{"text": "hi"}' if argv[0] == "curl" else b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)
    assert _transcribe(b"\x1aE\xdf\xa3webm").status_code == 200
    argv = _ffmpeg_argv(captured)
    i = argv.index("-i")
    pre = argv[:i]
    assert pre[pre.index("-f") + 1] == "webm"
    assert pre[pre.index("-protocol_whitelist") + 1] == "file"
    index = (h.FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "'audio/webm;codecs=opus'" in index and "new Blob(audioChunks, { type: 'audio/webm' })" in index


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_playlist_upload_fetches_nothing_and_opens_no_local_file(monkeypatch, tmp_path):
    """An upload that is an HLS playlist naming an http:// segment and a
    local file: ffmpeg (the real binary, with jarvis-web's own argv) makes
    no request and produces no audio from the local file. curl is stubbed."""
    hits = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sentinel = tmp_path / "sentinel.ts"
    REAL_RUN(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
              "-c:a", "aac", "-f", "mpegts", str(sentinel)], check=True)
    ffmpeg_results = []

    def _run(argv, *a, **kw):
        if argv[0] == "ffmpeg":
            result = REAL_RUN(argv, *a, **kw)
            ffmpeg_results.append((result, argv[-1]))
            return result
        return subprocess.CompletedProcess(argv, 0, stdout='{"text": ""}', stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    for segment in (f"http://127.0.0.1:{server.server_port}/seg.ts", str(sentinel)):
        playlist = f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{segment}\n#EXT-X-ENDLIST\n".encode()
        _transcribe(playlist)
    server.shutdown()
    assert hits == []
    assert len(ffmpeg_results) == 2
    for result, out in ffmpeg_results:
        assert result.returncode != 0
        assert not os.path.exists(out)


def test_voice_budget_per_client(monkeypatch):
    """Named: the 4th call in a minute from one client is 429 before any
    ffmpeg/curl runs; another client still has its own budget."""
    h.configure({**h.HOME_ENV, "JARVIS_VOICE_REQUESTS_PER_MINUTE": "3"})
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: calls.append(argv) or subprocess.CompletedProcess(
        argv, 0, stdout=b"RIFF", stderr=b""))
    c = h.client()
    lan = {**h.via_proxy(h.LAN), **h.CSRF}
    statuses = [c.post("/api/voice/synthesize", json={"text": "hi"}, headers=lan).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    assert len(calls) == 3
    blocked = _transcribe(b"x", headers={})
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "60"
    other = c.post("/api/voice/synthesize", json={"text": "hi"}, headers={**h.via_proxy("192.0.2.11"), **h.CSRF})
    assert other.status_code == 200


def test_voice_budget_default_is_30():
    assert caller_auth.load_settings({}, own_ips=()).voice_per_minute == 30


def test_tts_text_length_capped(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: calls.append(argv) or subprocess.CompletedProcess(
        argv, 0, stdout=b"RIFF", stderr=b""))
    c = h.client()
    lan = {**h.via_proxy(h.LAN), **h.CSRF}
    assert c.post("/api/voice/synthesize", json={"text": "x" * (main.TTS_MAX_CHARS + 1)}, headers=lan).status_code == 422
    assert c.post("/api/voice/synthesize", json={"text": ""}, headers=lan).status_code == 422
    assert calls == []
    assert c.post("/api/voice/synthesize", json={"text": "x" * main.TTS_MAX_CHARS}, headers=lan).status_code == 200
    assert main.TTS_MAX_CHARS == 5000
