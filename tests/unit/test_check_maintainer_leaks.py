"""Phase 0 (ATHENA-89, D4/D5) — tests for scripts/check-maintainer-leaks.py.

Test IDs L1-L12 map to the plan's Verification section and the test
contract's Phase 0 assertions. No mocking: the script shells out to real
`git` against real temp `git init` repos, so these tests do the same.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-maintainer-leaks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_maintainer_leaks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def leak_module():
    return _load_module()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_bytes(repo: Path, rel: str, content: bytes) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _add(repo: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


# ── L1: exact-string FAIL output, redacted ──────────────────────────────────


def test_L1_fail_lan_exact_redacted_output(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", 'x = "192.168.10.5"\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    assert proc.stdout.strip() == "FAIL src/foo.py:1 [maintainer-lan]"
    assert "192.168" not in proc.stdout


# ── L2 / L3: WARN-class paths never affect exit code ────────────────────────


@pytest.mark.parametrize("rel", ["docs/x.md", "tests/unit/test_x.py"])
def test_L2_L3_warn_class_paths(tmp_path, rel):
    repo = _init_repo(tmp_path)
    _write(repo, rel, 'x = "192.168.10.5"\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert f"WARN {rel}:1 [maintainer-lan]" in proc.stdout


# ── L4: allowlist suppression + stale-entry detection ───────────────────────


def test_L4_allowlist_suppresses_fail_hit(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", 'x = "192.168.10.5"\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        "src/foo.py\tmaintainer-lan\t192.168.10.5\tsuppressed for test\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert "FAIL" not in proc.stdout


def test_L4_entry_matching_nothing_is_stale(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", "print('hello')\n")
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        "src/foo.py\tmaintainer-lan\t192.168.10.5\tnever matches\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "stale allowlist entry" in proc.stdout


# ── L5: all 8 rules, positive/near-miss calibration ─────────────────────────


def test_L5_rule_id_count_is_8(leak_module):
    assert len(leak_module.BUILTIN_RULES) == 8
    assert len({rid for rid, _ in leak_module.BUILTIN_RULES}) == 8


def _hits(leak_module, text: str) -> set[str]:
    return {rid for rid, rx in leak_module.BUILTIN_RULES if rx.search(text)}


def test_L5_baltimore_md_hits_city_without_word_boundary(leak_module):
    assert "maintainer-city" in _hits(leak_module, "baltimore_md")


@pytest.mark.parametrize("text", ["192.168.10.5", "192.168.10.x", "192.168.10.256"])
def test_L5_lan_rule_positive_and_permissive(leak_module, text):
    assert "maintainer-lan" in _hits(leak_module, text)


@pytest.mark.parametrize("text", ["-76.6122", "39.2904"])
def test_L5_coords_rule_positive(leak_module, text):
    assert "maintainer-coords" in _hits(leak_module, text)


def test_L5_coords_rule_scoped_not_any_signed_decimal(leak_module):
    assert "maintainer-coords" not in _hits(leak_module, "-100.1234")


def test_L5_blue_jays_no_name_hit(leak_module):
    assert "maintainer-name" not in _hits(leak_module, "blue jays")


@pytest.mark.parametrize("text", ["Jay's", "Jay’s"])
def test_L5_name_hits_both_apostrophe_styles(leak_module, text):
    assert "maintainer-name" in _hits(leak_module, text)


def test_L5_more_light_no_city_false_positive(leak_module):
    assert "maintainer-city" not in _hits(leak_module, "give me more light")


def test_L5_jstuart0_no_name_hit(leak_module):
    assert "maintainer-name" not in _hits(leak_module, "jstuart0")


@pytest.mark.parametrize("text", ["thor", "Mac Studio", "Mac mini", "MAC-STUDIO"])
def test_L5_host_rule_positive(leak_module, text):
    assert "maintainer-host" in _hits(leak_module, text)


@pytest.mark.parametrize("text", ["author", "machine", "Macintosh"])
def test_L5_host_rule_no_false_positive(leak_module, text):
    assert "maintainer-host" not in _hits(leak_module, text)


# ── L6: --extra-patterns redaction ──────────────────────────────────────────


def test_L6_extra_patterns_fail_class_everywhere_and_redacted(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "docs/a.md", "the secret is SECRET-STRING-XYZ here\n")
    _add(repo)
    extra = tmp_path / "extra-patterns.tsv"
    extra.write_text("private\tSECRET-STRING-XYZ\n", encoding="utf-8")
    proc = _run(repo, "--extra-patterns", str(extra), "--show-matches")
    assert proc.returncode == 1
    assert "SECRET-STRING-XYZ" not in proc.stdout
    assert "<redacted>" in proc.stdout


# ── L7: non-git root ─────────────────────────────────────────────────────────


def test_L7_non_git_root_gives_rc2(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(not_a_repo)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


# ── L8: --paths scoping ──────────────────────────────────────────────────────


def test_L8_paths_scoping_and_missing_path_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/clean.py", "print('hello')\n")
    _write(repo, "src/dirty.py", 'x = "192.168.10.5"\n')
    _add(repo)
    proc = _run(repo, "--paths", "src/clean.py", "src/does-not-exist.py")
    assert proc.returncode == 0
    assert "dirty.py" not in proc.stdout


# ── L9: binary and .svg skipped ──────────────────────────────────────────────


def test_L9_binary_and_svg_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _write_bytes(repo, "assets/blob.bin", b"192.168.10.5\x00binary-tail")
    _write(repo, "assets/pin.svg", '<svg><path d="M -76.61 39.29 L 1 1"/></svg>\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── L10: allowlist suppresses WARN-class hits too, silently ─────────────────


def test_L10_allowlist_suppresses_warn_hit_silently(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "docs/x.md", 'x = "192.168.10.5"\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        "docs/x.md\tmaintainer-lan\t192.168.10.5\tdoc example\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert "WARN" not in proc.stdout
    assert "stale allowlist entry" not in proc.stdout


# ── L11: self-exclusion + empty-substring config error ──────────────────────


def test_L11_gate_never_scans_its_own_two_files(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "scripts/check-maintainer-leaks.py", 'x = "192.168.10.5"\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        "# 192.168.10.5 -- rule-matching text embedded in the allowlist's own comment\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_L11_empty_substring_outside_scrubbers_is_config_error(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", "print('hi')\n")
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        "src/foo.py\t*\t\tno substring here\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 2


def test_L11_empty_substring_permitted_for_scrubber_migrations(tmp_path):
    repo = _init_repo(tmp_path)
    _write(
        repo,
        "admin/backend/alembic/versions/053_clear_legacy_gateway_config_ips.py",
        'x = "192.168.10.5"\n',
    )
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        "admin/backend/alembic/versions/053_clear_legacy_gateway_config_ips.py\t*\t\tscrubber migration\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0


# ── L12: --show-matches / --format github matrix ────────────────────────────


def test_L12_show_matches_reveals_text_only_for_builtin_default_format(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", 'x = "192.168.10.5"\n')
    _add(repo)

    default_proc = _run(repo)
    assert "192.168.10.5" not in default_proc.stdout

    show_proc = _run(repo, "--show-matches")
    assert 'x = "192.168.10.5"' in show_proc.stdout

    github_proc = _run(repo, "--format", "github")
    assert "192.168.10.5" not in github_proc.stdout

    github_show_proc = _run(repo, "--format", "github", "--show-matches")
    assert "192.168.10.5" not in github_show_proc.stdout
