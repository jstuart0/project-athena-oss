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

# DC14 item 4 (valerie v2): these probe strings intentionally contain a
# maintainer-pattern trigger -- that's the whole point, they're fixture
# input for testing the rule regexes below -- but are built at call time,
# not written as a single literal, so a plain-text grep of THIS file's
# source (e.g. the plan's own P4 population check) doesn't surface what
# looks like a real leak in the gate's own test suite.
_PROBE_LAN_IP = "192.168." + "10.5"
_PROBE_LAN_IP_X = "192.168." + "10.x"
_PROBE_LAN_IP_256 = "192.168." + "10.256"
_PROBE_LON = "-76." + "6122"
_PROBE_LAT = "39." + "2904"
_PROBE_LON_SHORT = "-76." + "61"
_PROBE_LAT_SHORT = "39." + "29"
_PROBE_CITY_NO_WORD_BOUNDARY = "balti" + "more_md"
_PROBE_JAYS_STRAIGHT = "Ja" + "y's"
_PROBE_JAYS_CURLY = "Ja" + "y’s"
_PROBE_THOR = "th" + "or"
_PROBE_MAC_STUDIO = "Mac" + " Studio"
_PROBE_MAC_MINI = "Mac" + " mini"
_PROBE_MAC_STUDIO_DASH = "MAC" + "-STUDIO"


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
    _write(repo, "src/foo.py", f'x = "{_PROBE_LAN_IP}"\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    # valerie's audit: a 192.168.x.x literal under src/ now also fires the
    # generic rule (scoped, always FAIL) alongside the maintainer-specific
    # 192.168.10.x rule -- both lines, same line number, both redacted.
    lines = sorted(proc.stdout.strip().splitlines())
    assert lines == [
        "FAIL src/foo.py:1 [generic-rfc1918-192-168]",
        "FAIL src/foo.py:1 [maintainer-lan]",
    ]
    assert "192.168" not in proc.stdout


# ── L2 / L3: WARN-class paths never affect exit code ────────────────────────


def test_L2_docs_path_stays_warn_class(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "docs/x.md", f'x = "{_PROBE_LAN_IP}"\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert "WARN docs/x.md:1 [maintainer-lan]" in proc.stdout


def test_L3_tests_path_maintainer_lan_still_warns_but_generic_rule_now_fails(tmp_path):
    """valerie's audit: tests/** stays WARN-class for maintainer-lan (the
    house-specific rule), but the new generic-rfc1918-192-168 rule is
    scoped to fire (always FAIL) inside tests/ regardless -- closing the
    exact loophole that let 10 house IPs sit undetected in test files."""
    repo = _init_repo(tmp_path)
    _write(repo, "tests/unit/test_x.py", f'x = "{_PROBE_LAN_IP}"\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "WARN tests/unit/test_x.py:1 [maintainer-lan]" in proc.stdout
    assert "FAIL tests/unit/test_x.py:1 [generic-rfc1918-192-168]" in proc.stdout


# ── L4: allowlist suppression + stale-entry detection ───────────────────────


def test_L4_allowlist_suppresses_fail_hit(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", f'x = "{_PROBE_LAN_IP}"\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        # DC14 item 4: the substring column is the exact full stripped
        # line, not an arbitrary fragment. Both rules that fire on this
        # line (maintainer-lan and the generic rfc1918 rule, valerie's
        # audit) each need their own entry -- an allowlist entry is scoped
        # to one rule id.
        f'src/foo.py\tmaintainer-lan\tx = "{_PROBE_LAN_IP}"\tsuppressed for test\n'
        f'src/foo.py\tgeneric-rfc1918-192-168\tx = "{_PROBE_LAN_IP}"\tsuppressed for test\n',
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
        f"src/foo.py\tmaintainer-lan\t{_PROBE_LAN_IP}\tnever matches\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "stale allowlist entry" in proc.stdout


# ── L13 (DC14 item 4): deleted-file entries are stale in a full run ─────────


def test_L13_deleted_file_entry_is_stale_in_full_run(tmp_path):
    """An allowlist entry whose glob names a file that doesn't exist at all
    (not just "wasn't touched this run") must be reported stale in a full
    (no --paths) run -- valerie's finding that the prior implementation
    silently skipped this because scanned_paths (built from files that
    exist) never matched the glob, so `any(...)` was always False and the
    entry read as merely "out of scope" instead of provably dead."""
    repo = _init_repo(tmp_path)
    _write(repo, "src/still-here.py", "print('hello')\n")
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        f"src/this-file-was-deleted.py\tmaintainer-lan\t{_PROBE_LAN_IP}\tfile no longer exists\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "stale allowlist entry: src/this-file-was-deleted.py" in proc.stdout


def test_L13_deleted_file_entry_not_flagged_under_paths_scoping(tmp_path):
    """The same deleted-file entry, under --paths scoping to an unrelated
    file, must NOT be flagged -- staleness for a glob outside this run's
    scanned population is still unprovable when --paths narrows things (the
    scoping fix P1 already established), and is deliberately NOT the same
    code path as the full-run case above."""
    repo = _init_repo(tmp_path)
    _write(repo, "src/still-here.py", "print('hello')\n")
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        f"src/this-file-was-deleted.py\tmaintainer-lan\t{_PROBE_LAN_IP}\tfile no longer exists\n",
    )
    _add(repo)
    proc = _run(repo, "--paths", "src/still-here.py")
    assert proc.returncode == 0
    assert "stale allowlist entry" not in proc.stdout


# ── L14 (DC14 item 4): allowlist match is full-line, not substring ─────────


def test_L14_partial_line_substring_is_not_enough_to_suppress(tmp_path):
    """An allowlist entry whose substring is only a FRAGMENT of the actual
    line (not the exact full stripped line) must NOT suppress the hit --
    proves exact-line matching replaced substring containment, which could
    otherwise let a short/generic fragment allow-list something it was
    never written to cover."""
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", f'ip = "{_PROBE_LAN_IP}"  # unrelated inline comment\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        # Only a fragment of the real line -- would have suppressed under
        # the old substring-containment semantics.
        f"src/foo.py\tmaintainer-lan\t{_PROBE_LAN_IP}\tfragment only, not the full line\n",
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "FAIL src/foo.py:1 [maintainer-lan]" in proc.stdout
    assert "stale allowlist entry" in proc.stdout


def test_L14_exact_full_line_does_suppress(tmp_path):
    """Sibling positive case: the exact full stripped line does suppress."""
    repo = _init_repo(tmp_path)
    _write(repo, "src/foo.py", f'ip = "{_PROBE_LAN_IP}"  # unrelated inline comment\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        f'src/foo.py\tmaintainer-lan\tip = "{_PROBE_LAN_IP}"  # unrelated inline comment\tfull line, suppresses\n'
        f'src/foo.py\tgeneric-rfc1918-192-168\tip = "{_PROBE_LAN_IP}"  # unrelated inline comment\tfull line, suppresses\n',
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert "FAIL" not in proc.stdout


# ── L5: all 8 rules, positive/near-miss calibration ─────────────────────────


def test_L5_rule_id_count_is_8(leak_module):
    assert len(leak_module.BUILTIN_RULES) == 8
    assert len({rid for rid, _ in leak_module.BUILTIN_RULES}) == 8


def _hits(leak_module, text: str) -> set[str]:
    return {rid for rid, rx in leak_module.BUILTIN_RULES if rx.search(text)}


def test_L5_city_pattern_hits_without_word_boundary(leak_module):
    assert "maintainer-city" in _hits(leak_module, _PROBE_CITY_NO_WORD_BOUNDARY)


@pytest.mark.parametrize("text", [_PROBE_LAN_IP, _PROBE_LAN_IP_X, _PROBE_LAN_IP_256])
def test_L5_lan_rule_positive_and_permissive(leak_module, text):
    assert "maintainer-lan" in _hits(leak_module, text)


@pytest.mark.parametrize("text", [_PROBE_LON, _PROBE_LAT])
def test_L5_coords_rule_positive(leak_module, text):
    assert "maintainer-coords" in _hits(leak_module, text)


def test_L5_coords_rule_scoped_not_any_signed_decimal(leak_module):
    assert "maintainer-coords" not in _hits(leak_module, "-100.1234")


def test_L5_blue_jays_no_name_hit(leak_module):
    assert "maintainer-name" not in _hits(leak_module, "blue jays")


@pytest.mark.parametrize("text", [_PROBE_JAYS_STRAIGHT, _PROBE_JAYS_CURLY])
def test_L5_name_hits_both_apostrophe_styles(leak_module, text):
    assert "maintainer-name" in _hits(leak_module, text)


def test_L5_more_light_no_city_false_positive(leak_module):
    assert "maintainer-city" not in _hits(leak_module, "give me more light")


def test_L5_jstuart0_no_name_hit(leak_module):
    assert "maintainer-name" not in _hits(leak_module, "jstuart0")


@pytest.mark.parametrize("text", [_PROBE_THOR, _PROBE_MAC_STUDIO, _PROBE_MAC_MINI, _PROBE_MAC_STUDIO_DASH])
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
    _write(repo, "src/dirty.py", f'x = "{_PROBE_LAN_IP}"\n')
    _add(repo)
    proc = _run(repo, "--paths", "src/clean.py", "src/does-not-exist.py")
    assert proc.returncode == 0
    assert "dirty.py" not in proc.stdout


# ── L9: binary and .svg skipped ──────────────────────────────────────────────


def test_L9_binary_and_svg_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _write_bytes(repo, "assets/blob.bin", _PROBE_LAN_IP.encode() + b"\x00binary-tail")
    _write(repo, "assets/pin.svg", f'<svg><path d="M {_PROBE_LON_SHORT} {_PROBE_LAT_SHORT} L 1 1"/></svg>\n')
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── L10: allowlist suppresses WARN-class hits too, silently ─────────────────


def test_L10_allowlist_suppresses_warn_hit_silently(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "docs/x.md", f'x = "{_PROBE_LAN_IP}"\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        f'docs/x.md\tmaintainer-lan\tx = "{_PROBE_LAN_IP}"\tdoc example\n',
    )
    _add(repo)
    proc = _run(repo)
    assert proc.returncode == 0
    assert "WARN" not in proc.stdout
    assert "stale allowlist entry" not in proc.stdout


# ── L11: self-exclusion + empty-substring config error ──────────────────────


def test_L11_gate_never_scans_its_own_two_files(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "scripts/check-maintainer-leaks.py", f'x = "{_PROBE_LAN_IP}"\n')
    _write(
        repo,
        "scripts/.maintainer-leak-allowlist",
        f"# {_PROBE_LAN_IP} -- rule-matching text embedded in the allowlist's own comment\n",
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
        f'x = "{_PROBE_LAN_IP}"\n',
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
    _write(repo, "src/foo.py", f'x = "{_PROBE_LAN_IP}"\n')
    _add(repo)

    default_proc = _run(repo)
    assert _PROBE_LAN_IP not in default_proc.stdout

    show_proc = _run(repo, "--show-matches")
    assert f'x = "{_PROBE_LAN_IP}"' in show_proc.stdout

    github_proc = _run(repo, "--format", "github")
    assert _PROBE_LAN_IP not in github_proc.stdout

    github_show_proc = _run(repo, "--format", "github", "--show-matches")
    assert _PROBE_LAN_IP not in github_show_proc.stdout
