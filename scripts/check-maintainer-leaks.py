#!/usr/bin/env python3
"""D4 — assert the tracked tree carries none of the maintainer's identifying
strings (LAN IPs, home domain, home city, home-dir paths, a legacy k8s
namespace FQDN, home coordinates, the maintainer's name, or the maintainer's
host names).

Why a script, not a grep
------------------------
Eight independent regexes (`BUILTIN_RULES`), each individually calibrated
against real false positives found in this tree: the city rule deliberately
has no word boundary (it must catch `baltimore_md`), the name rule must
reject "blue jays" while catching both `Jay's` and the curly-quote `Jay's`,
and the host rule must reject "author"/"Macintosh"/"machine" while catching
"thor"/"Mac Studio"/"Mac mini". A single ad-hoc grep invocation cannot carry
this per-rule calibration, redaction behaviour, path-class WARN/FAIL split,
and allowlist semantics at once.

Path classes
------------
WARN-class paths (`*.md`, `docs/**`, `LICENSE`, `tests/**`, `**/tests/**`,
`**/test_*.py`, `bench/**`, `*.example` including `.env.example` /
`.env.secrets.example` / `config.env.example`) print a WARN line and never
affect the exit code. Every other tracked path is FAIL-class.

`--extra-patterns FILE` / env `MAINTAINER_LEAK_EXTRA_PATTERNS` add private,
`id<TAB>regex` rules that are FAIL-class in every path, including WARN-class
ones. That file must live outside this repository or be gitignored — it is
never itself committed, and matched text for its rules is never printed,
even under `--show-matches`.

Redaction
---------
By default, and always under `--format github`, no matched text is ever
printed for any rule -- output is `FAIL <path>:<line> [<rule>]` /
`WARN <path>:<line> [<rule>]`. `--show-matches` adds the trimmed source line
for built-in rules only; `--extra-patterns` rules always print `<redacted>`
regardless of `--show-matches`.

Non-goals (D4)
--------------
The tracked rules deliberately do not catch: a lone latitude with no
longitude (e.g. `39.1` alone); the hyphenated spelling `Charm-City`;
`/home/<user>` Linux home paths (only `/Users/...` is ruled); airport codes
such as `BWI`; or team/place names that don't contain the word "Baltimore".
Put those in a private `--extra-patterns` file instead.

Exit codes
----------
    0  no unsuppressed FAIL-class hits and no stale allowlist entries
    1  findings (unsuppressed FAIL-class hits) and/or stale allowlist entries
    2  could not run (bad args, not a git working tree, malformed allowlist
       or --extra-patterns file)

Exit 2 is never a pass, per scripts/check-frontend-cache-busters.py's
convention.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# The gate never scans its own two definition files (D5): they necessarily
# contain the patterns they define, and scanning them would self-match.
SELF_PATHS = frozenset(
    {
        "scripts/check-maintainer-leaks.py",
        "scripts/.maintainer-leak-allowlist",
    }
)

# D0.2: the only two allowlist entries permitted an empty required-substring.
SCRUBBER_MIGRATION_PATHS = frozenset(
    {
        "admin/backend/alembic/versions/053_clear_legacy_gateway_config_ips.py",
        "admin/backend/alembic/versions/058_clear_legacy_oidc_redirect_uri.py",
    }
)

ALLOWLIST_RELATIVE_PATH = "scripts/.maintainer-leak-allowlist"

# D4 rule table. All built-in rules apply uniformly across path classes; the
# path class (WARN vs FAIL) is decided separately by classify_path().
BUILTIN_RULES: list[tuple[str, re.Pattern]] = [
    ("maintainer-lan", re.compile(r"\b192\.168\.10\.(?:\d{1,3}|x)\b")),
    ("maintainer-domain", re.compile(r"\bxmojo\.net\b", re.IGNORECASE)),
    ("maintainer-city", re.compile(r"baltimore|charm city", re.IGNORECASE)),
    ("home-dir-path", re.compile(r"/Users/[A-Za-z][\w.-]*/")),
    ("legacy-namespace-fqdn", re.compile(r"\.athena-admin\.svc\b", re.IGNORECASE)),
    ("maintainer-coords", re.compile(r"-7[67]\.\d+|\b39\.\d{3,}\b")),
    (
        "maintainer-name",
        re.compile(r"\bjay['’]s\b|\bjay\s+stuart\b|\bjstuart\b", re.IGNORECASE),
    ),
    (
        "maintainer-host",
        re.compile(r"\bthor\b|\bmac[ -]?(?:studio|mini)\b", re.IGNORECASE),
    ),
]
BUILTIN_RULE_IDS = frozenset(rid for rid, _ in BUILTIN_RULES)


class ConfigError(Exception):
    """Raised for any condition that makes the gate unable to run (rc 2)."""


class AllowlistEntry(NamedTuple):
    glob: str
    rule: str
    substring: str
    reason: str
    matched: bool = False

    def matches(self, hit: "Hit") -> bool:
        if not fnmatch.fnmatch(hit.path, self.glob):
            return False
        if self.rule != "*" and self.rule != hit.rule:
            return False
        if self.substring != "" and self.substring not in hit.line_text:
            return False
        return True


class Hit(NamedTuple):
    path: str
    line: int
    rule: str
    line_text: str
    cls: str  # "FAIL" or "WARN"
    is_extra: bool


def classify_path(rel_path: str) -> str:
    p = PurePosixPath(rel_path)
    if p.suffix == ".md":
        return "WARN"
    if p.parts and p.parts[0] == "docs":
        return "WARN"
    if p.name == "LICENSE":
        return "WARN"
    if p.parts and p.parts[0] == "tests":
        return "WARN"
    if "tests" in p.parts[:-1]:
        return "WARN"
    if p.name.startswith("test_") and p.suffix == ".py":
        return "WARN"
    if p.parts and p.parts[0] == "bench":
        return "WARN"
    if p.suffix == ".example":
        return "WARN"
    return "FAIL"


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=False,
    )


def enumerate_tracked_files(root: Path) -> set[str]:
    if not root.is_dir():
        raise ConfigError(f"{root} is not a directory")
    check = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if check.returncode != 0 or check.stdout.strip() != b"true":
        raise ConfigError(f"{root} is not a git working tree")
    proc = _run_git(root, "ls-files", "-z")
    if proc.returncode != 0:
        raise ConfigError(f"git ls-files failed under {root}")
    return {
        chunk.decode("utf-8", errors="replace")
        for chunk in proc.stdout.split(b"\x00")
        if chunk
    }


def load_allowlist(root: Path) -> list[AllowlistEntry]:
    path = root / ALLOWLIST_RELATIVE_PATH
    if not path.is_file():
        return []
    entries: list[AllowlistEntry] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise ConfigError(
                f"{path}:{lineno}: expected 4 tab-separated fields "
                f"(glob, rule, substring, reason), got {len(fields)}"
            )
        glob, rule, substring, reason = fields
        if substring == "" and glob not in SCRUBBER_MIGRATION_PATHS:
            raise ConfigError(
                f"{path}:{lineno}: empty substring is only permitted for the "
                f"two scrubber migrations, got glob={glob!r}"
            )
        entries.append(AllowlistEntry(glob, rule, substring, reason))
    return entries


def load_extra_patterns(path: Path) -> list[tuple[str, re.Pattern]]:
    if not path.is_file():
        raise ConfigError(f"--extra-patterns file not found: {path}")
    rules: list[tuple[str, re.Pattern]] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise ConfigError(f"{path}:{lineno}: expected 'id<TAB>regex'")
        rule_id, pattern = parts
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"{path}:{lineno}: invalid regex {pattern!r}: {exc}")
        rules.append((rule_id, regex))
    return rules


def normalize_path_arg(raw: str, root: Path) -> str:
    p = Path(raw)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return PurePosixPath(raw).as_posix()
    return PurePosixPath(raw).as_posix()


def scan_file(root: Path, rel_path: str, extra_rules: list[tuple[str, re.Pattern]]) -> list[Hit]:
    if rel_path in SELF_PATHS:
        return []
    if rel_path.lower().endswith(".svg"):
        return []
    full = root / rel_path
    try:
        raw = full.read_bytes()
    except OSError:
        return []
    if b"\x00" in raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    path_class = classify_path(rel_path)

    hits: list[Hit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule_id, regex in BUILTIN_RULES:
            if regex.search(line):
                hits.append(Hit(rel_path, lineno, rule_id, line.strip(), path_class, False))
        for rule_id, regex in extra_rules:
            if regex.search(line):
                hits.append(Hit(rel_path, lineno, rule_id, line.strip(), "FAIL", True))
    return hits


def apply_allowlist(
    entries: list[AllowlistEntry], hits: list[Hit]
) -> tuple[list[Hit], list[AllowlistEntry]]:
    used = [False] * len(entries)
    kept: list[Hit] = []
    for hit in hits:
        suppressed = False
        for i, entry in enumerate(entries):
            if entry.matches(hit):
                used[i] = True
                suppressed = True
        if not suppressed:
            kept.append(hit)
    stale = [entry for entry, was_used in zip(entries, used) if not was_used]
    return kept, stale


def format_hit(hit: Hit, fmt: str, show_matches: bool) -> str:
    if fmt == "github":
        kind = "error" if hit.cls == "FAIL" else "warning"
        return f"::{kind} file={hit.path},line={hit.line}::[{hit.rule}]"
    base = f"{hit.cls} {hit.path}:{hit.line} [{hit.rule}]"
    if show_matches:
        base += " <redacted>" if hit.is_extra else f" {hit.line_text}"
    return base


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Scan the tracked tree for maintainer-identifying strings "
            "(LAN IPs, home domain, home city, home-dir paths, a legacy "
            "namespace FQDN, home coordinates, the maintainer's name, and "
            "the maintainer's host names)."
        ),
        epilog=(
            "Non-goals: the tracked rules deliberately do not catch a lone "
            "latitude with no longitude, the hyphenated spelling "
            "'Charm-City', /home/<user> Linux paths, airport codes, or "
            "team/place names that don't contain 'Baltimore'. Put those in "
            "a private --extra-patterns file that lives outside this "
            "repository or is gitignored."
        ),
    )
    ap.add_argument("--root", default=None, help="Repo root to scan (default: this repo).")
    ap.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="Restrict the scan to these paths. A missing or untracked path is skipped silently.",
    )
    ap.add_argument(
        "--extra-patterns",
        default=None,
        help=(
            "Path to a private 'id<TAB>regex' file. Rules from this file are "
            "FAIL-class in every path and never print matched text. This "
            "file must live outside the repository or be gitignored. May "
            "also be set via MAINTAINER_LEAK_EXTRA_PATTERNS."
        ),
    )
    ap.add_argument("--count-only", action="store_true", help="Print only the unsuppressed FAIL-class hit count.")
    ap.add_argument("--format", choices=["text", "github"], default="text")
    ap.add_argument(
        "--show-matches",
        action="store_true",
        help="Also print the matched line for built-in rules (never for --extra-patterns rules).",
    )
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        root = Path(args.root).resolve() if args.root else REPO_ROOT
        tracked = enumerate_tracked_files(root)
        allowlist = load_allowlist(root)
        extra_source = args.extra_patterns or os.environ.get("MAINTAINER_LEAK_EXTRA_PATTERNS")
        extra_rules = load_extra_patterns(Path(extra_source)) if extra_source else []
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.paths:
        wanted = set()
        for raw in args.paths:
            rel = normalize_path_arg(raw, root)
            if rel in tracked:
                wanted.add(rel)
        scan_targets = sorted(wanted)
    else:
        scan_targets = sorted(tracked)

    all_hits: list[Hit] = []
    for rel in scan_targets:
        all_hits.extend(scan_file(root, rel, extra_rules))

    kept_hits, stale_entries = apply_allowlist(allowlist, all_hits)
    kept_fails = [h for h in kept_hits if h.cls == "FAIL"]
    kept_warns = [h for h in kept_hits if h.cls == "WARN"]

    if args.count_only:
        print(len(kept_fails))
    else:
        ordered = sorted(kept_fails, key=lambda h: (h.path, h.line, h.rule)) + sorted(
            kept_warns, key=lambda h: (h.path, h.line, h.rule)
        )
        for hit in ordered:
            print(format_hit(hit, args.format, args.show_matches))
        for entry in stale_entries:
            print(
                "stale allowlist entry: "
                f"{entry.glob}\t{entry.rule}\t{entry.substring}\t{entry.reason}"
            )

    return 1 if (kept_fails or stale_entries) else 0


if __name__ == "__main__":
    sys.exit(main())
