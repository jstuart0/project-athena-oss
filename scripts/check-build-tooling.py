#!/usr/bin/env python3
r"""
check-build-tooling.py — assert that every dependency install in a Dockerfile
stage is preceded, in the same stage, by the pinned build-tooling triplet:

    pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

Shared by gate A9b (all 29 images, Phase 6) and A19b (jarvis-web's builder
stage only, Phase 2) — one implementation, so the two gates cannot drift into
asserting different things.

Usage:
    python3 scripts/check-build-tooling.py                 # default 29-image population
    python3 scripts/check-build-tooling.py --file <path>   # one or more Dockerfiles
    python3 scripts/check-build-tooling.py --root <dir>     # run relative to <dir> (default: repo root)

Deliberately NOT a naive substring/regex grep for the triplet anywhere in the
file. Shapes a naive matcher gets wrong, each verified against this
implementation:
  1. A whole-line `# ...` comment containing the triplet is not a pin.
  2. A `\` line continuation splits the consumer across physical lines (the
     generator template's own shape: `RUN pip install --upgrade pip && \`
     newline `pip install -r requirements.txt`) — an anchor on `^\s*RUN\s`
     alone misses the continuation line and silently skips the stage.
  3. `RUN # pip install --upgrade pip==26.2.1 ...` — the `#` immediately
     after RUN comments out the entire shell command; nothing is installed,
     but a matcher that only excludes whole-line comments still matches this.
  4. `RUN pip3 install -r requirements.txt` (or `pip3.11`, etc.) — matching
     only the literal `pip install` bypasses the whole check for a stage
     that spells its installer differently.
  5. A stage that COPYs in a requirements file or the shared package but
     whose install command matches no recognized pattern at all: silently
     skipping it (as if it installed nothing) is exculpatory precisely when
     it shouldn't be, so this is now a hard failure instead of a pass.
"""

import argparse
import glob
import os
import re
import sys

# Dockerfiles are never legitimately multi-MB; a bigger one is a signal to
# look at by hand, not a size the folding logic below needs to handle.
MAX_DOCKERFILE_BYTES = 1_000_000

INSTR = r"^\s*(?:RUN|ONBUILD\s+RUN)\s"
# Matches pip, pip3, pip3.11, etc. — not just the literal "pip install".
PIP_INSTALL = r"\bpip[0-9.]*\s+install\b"
TRIPLET = re.compile(
    INSTR + r".*" + PIP_INSTALL + r".*--upgrade\b"
    r".*pip==26\.2\.1\s+setuptools==84\.0\.0\s+wheel==0\.48\.0",
    re.S,
)
CONSUMER = re.compile(
    INSTR + r".*" + PIP_INSTALL + r".*(?:-r\s+\S*requirements\S*\.txt|-e\s+/app/shared)",
    re.S,
)
# A stage that copies these in is declaring intent to install dependencies,
# regardless of which installer spelling it then uses.
COPY_DEPS_SIGNAL = re.compile(
    r"^\s*COPY\s+.*(?:requirements\S*\.txt|/app/shared\b)",
    re.M,
)

DEFAULT_FILES = [
    "admin/backend/Dockerfile",
    "apps/chat-embed/Dockerfile",
    "apps/jarvis-web/Dockerfile",
    "src/gateway/Dockerfile",
    "src/mode_service/Dockerfile",
    "src/orchestrator/Dockerfile",
]


def _read_lines(path):
    size = os.path.getsize(path)
    if size > MAX_DOCKERFILE_BYTES:
        raise ValueError(f"{size} bytes exceeds the {MAX_DOCKERFILE_BYTES}-byte Dockerfile size cap")
    # errors="replace" so an invalid byte sequence degrades to a mangled
    # character instead of an unhandled UnicodeDecodeError — a bad Dockerfile
    # encoding should produce a FAIL line, not a traceback.
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def logical(path):
    """Fold backslash continuations into logical instructions; drop
    whole-line comments. Returns [(physical_line_no, text), ...] with
    physical_line_no 0-based, pointing at the instruction's first line.

    O(n) in file size regardless of continuation-chain length: each physical
    line is appended to a list once and every logical instruction is joined
    exactly once. A prior version rebuilt the growing buffer string on every
    continuation line (`buf = buf[:-1] + " " + raw`), which was O(n^2) on a
    long backslash-continuation chain, with no cap on chain length or file size."""
    out = []
    parts = []
    start = None
    for n, raw in enumerate(_read_lines(path)):
        line = raw.rstrip()
        continues = line.endswith("\\")
        text = line[:-1] if continues else line
        if not parts:
            if text.lstrip().startswith("#"):
                continue
            start = n
            parts.append(text)
        else:
            parts.append(text.lstrip())
        if continues:
            continue
        out.append((start, " ".join(parts)))
        parts = []
    if parts:
        out.append((start, " ".join(parts)))
    return out


def is_inert_run(text):
    """True when a RUN instruction's entire shell command is commented out —
    `RUN # pip install ...` — which installs nothing despite containing the
    triplet or a consumer pattern textually."""
    m = re.match(r"^\s*(?:ONBUILD\s+)?RUN\s+(.*)$", text, re.S)
    if not m:
        return False
    return m.group(1).lstrip().startswith("#")


def check_file(path):
    """Return a list of failure strings for one Dockerfile (empty = clean)."""
    try:
        instrs = [(n, x) for n, x in logical(path) if not is_inert_run(x)]
    except OSError as e:
        return [f"{path}: could not read file ({e})"]
    except ValueError as e:
        return [f"{path}: {e}"]

    bad = []
    starts = [i for i, (n, x) in enumerate(instrs) if x.lstrip().startswith("FROM ")] or [0]
    for a, b in zip(starts, starts[1:] + [len(instrs)]):
        stage = instrs[a:b]
        cons = [i for i, (n, x) in enumerate(stage) if CONSUMER.search(x)]
        sl = stage[0][0]
        if not cons:
            stage_text = "\n".join(x for _, x in stage)
            if COPY_DEPS_SIGNAL.search(stage_text):
                bad.append(
                    f"{path} stage@L{sl + 1}: copies a requirements file or shared "
                    f"package but no recognized pip-install command was found "
                    f"(unpinned-installer risk — not a clean pass)"
                )
            continue
        pins = [i for i, (n, x) in enumerate(stage) if TRIPLET.search(x)]
        if not pins:
            bad.append(f"{path} stage@L{sl + 1}: installs deps with NO pinned triplet")
            continue
        if min(pins) > min(cons):
            bad.append(
                f"{path} stage@L{sl + 1}: triplet at L{stage[min(pins)][0] + 1} AFTER "
                f"first consumer at L{stage[min(cons)][0] + 1}"
            )
    for n, x in instrs:
        if re.search(r"--upgrade\s+pip(?:\s|$)", x) and not TRIPLET.search(x):
            bad.append(f"{path}:{n + 1}: unbounded --upgrade pip")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", help="Dockerfile to check (repeatable)")
    ap.add_argument("--root", default=".", help="directory to run relative to (default: cwd)")
    args = ap.parse_args()
    os.chdir(args.root)

    files = args.file or (DEFAULT_FILES + sorted(glob.glob("src/rag/*/Dockerfile")))

    bad = []
    for f in files:
        if not os.path.exists(f):
            continue
        bad.extend(check_file(f))

    for m in bad:
        print("FAIL:", m)
    if not bad:
        print(f"TOOLING TRIPLET OK ({len(files)} file(s) checked)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
