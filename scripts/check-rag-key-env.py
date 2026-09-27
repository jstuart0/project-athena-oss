#!/usr/bin/env python3
"""
RAG API-key env-var name parity + drift guard (ATHENA-88 / F91, D11; F40/F41
reconciliation additions).

Checks six things, all against a repo rooted at --root (default: this
repo's root):

1. Per-service parity: the env-var names each src/rag/<src_dir>/*.py
   actually reads (os.getenv/os.environ, credential-suffix filtered) equal
   the names scripts/generate-rag-manifests.py's SERVICES table declares
   for that service.
2. scripts/create-secrets.sh's `athena-api-keys` --from-literal names equal
   the union of every service's key_envs.
3. The required/optional ref shape: SERVICE_API_KEY is always required (no
   `optional: true`); every athena-api-keys ref is `optional: true`.
4. The committed manifests/athena-prod/rag-services.yaml equals the
   generator's own output (modulo the `# Generated:` timestamp line), with
   REGISTRY and TAG removed from the environment so the defaults are
   pinned.
5. (F42) The src/rag/* directory set (every dir containing main.py) equals
   SERVICES' src_dir set — catches a new RAG service shipped with no
   manifest entry, not just a name mismatch on existing ones.
6. (F41) Operator-facing example/doc files (.env.secrets.example,
   config.env.example, docs/INSTALLATION.md, docs/MODULES.md,
   docs/CONFIGURATION.md) contain none of the stale pre-F91 RAG key names.

Reuses check-env-example.py::collect_getenv_names via importlib — no
second AST walker for os.getenv/os.environ reads.

Usage:
    python3 scripts/check-rag-key-env.py [--root PATH]
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

# Same suffix set the plan specifies, minus SERVICE_API_KEY (injected on
# every deployment unconditionally — it's not a per-service RAG credential).
CREDENTIAL_SUFFIX_RE = re.compile(
    r"^[A-Z][A-Z0-9_]*(_API_KEY|_KEY|_TOKEN|_SECRET|_CLIENT_ID)$"
)
EXCLUDED_NAMES = frozenset({"SERVICE_API_KEY"})


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_collect_getenv_names(root: Path):
    module = _load_module(root / "scripts" / "check-env-example.py", "check_env_example")
    return module.collect_getenv_names


def _load_generator(root: Path) -> ModuleType:
    return _load_module(root / "scripts" / "generate-rag-manifests.py", "generate_rag_manifests")


def _credential_names(names: set) -> set:
    return {n for n in names if CREDENTIAL_SUFFIX_RE.match(n) and n not in EXCLUDED_NAMES}


def check_per_service(root: Path) -> list:
    collect_getenv_names = _load_collect_getenv_names(root)
    generator = _load_generator(root)
    findings = []

    for service in generator.SERVICES:
        service_dir = root / "src" / "rag" / service.src_dir
        code_names = (
            _credential_names(collect_getenv_names(service_dir))
            if service_dir.exists()
            else set()
        )
        generator_names = set(service.key_envs)
        if code_names != generator_names:
            findings.append(
                f"{service.name}: generator declares {sorted(generator_names)}, "
                f"code reads {sorted(code_names)}"
            )

    return findings


def check_create_secrets(root: Path) -> list:
    generator = _load_generator(root)
    union = set()
    for service in generator.SERVICES:
        union |= set(service.key_envs)

    secrets_path = root / "scripts" / "create-secrets.sh"
    secrets_text = secrets_path.read_text()
    match = re.search(
        r"create secret generic athena-api-keys(.*?)--dry-run=client",
        secrets_text,
        re.DOTALL,
    )
    documented = (
        set(re.findall(r"--from-literal=([A-Z0-9_]+)=", match.group(1))) if match else set()
    )

    findings = []
    missing = union - documented
    extra = documented - union
    if missing:
        findings.append(f"create-secrets.sh missing names: {sorted(missing)}")
    if extra:
        findings.append(f"create-secrets.sh has stale/extra names: {sorted(extra)}")
    return findings


def _block_for(manifest_text: str, key_name: str):
    return re.search(rf"- name: {re.escape(key_name)}\n(?:.*\n){{0,5}}", manifest_text)


def check_ref_shape(root: Path) -> list:
    generator = _load_generator(root)
    findings = []

    for service in generator.SERVICES:
        # Trailing "\n" guards the _block_for regex when a ref happens to be
        # the very last thing generate_deployment returns (its own template
        # never trails a newline on the closing readinessProbe line either).
        manifest_text = generator.generate_deployment(
            service.name, service.port, service.key_envs
        ) + "\n"

        service_block = _block_for(manifest_text, "SERVICE_API_KEY")
        if service_block is None:
            findings.append(f"{service.name}: SERVICE_API_KEY ref missing")
        elif "optional: true" in service_block.group(0):
            findings.append(f"{service.name}: SERVICE_API_KEY ref must not be optional")

        for key_env in service.key_envs:
            block = _block_for(manifest_text, key_env)
            if block is None:
                findings.append(f"{service.name}: {key_env} ref missing")
            elif "optional: true" not in block.group(0):
                findings.append(f"{service.name}: {key_env} ref must be optional: true")

    return findings


def check_manifest_drift(root: Path) -> list:
    generator_path = root / "scripts" / "generate-rag-manifests.py"
    manifest_path = root / "manifests" / "athena-prod" / "rag-services.yaml"

    if not manifest_path.exists():
        return [f"committed manifest not found at {manifest_path}"]

    env = {k: v for k, v in os.environ.items() if k not in ("REGISTRY", "TAG")}
    result = subprocess.run(
        [sys.executable, str(generator_path)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    generated_lines = [
        line for line in result.stdout.splitlines() if not line.startswith("# Generated:")
    ]
    committed_lines = [
        line for line in manifest_path.read_text().splitlines()
        if not line.startswith("# Generated:")
    ]

    if generated_lines != committed_lines:
        return [
            "manifest drift: manifests/athena-prod/rag-services.yaml does not "
            "match `python3 scripts/generate-rag-manifests.py` output "
            "(REGISTRY/TAG unset). Regenerate and commit."
        ]
    return []


def check_src_dir_population(root: Path) -> list:
    """
    ATHENA-88 / F42 (codex r2 Low): a new src/rag/*/main.py directory with
    no corresponding SERVICES entry is otherwise invisible to this checker
    (check_per_service only iterates SERVICES). Compares the directory set
    to SERVICES.src_dir directly.
    """
    generator = _load_generator(root)
    rag_root = root / "src" / "rag"
    if not rag_root.exists():
        return [f"src/rag not found under {root}"]

    dirs_on_disk = {
        p.name for p in rag_root.iterdir() if p.is_dir() and (p / "main.py").exists()
    }
    declared = {service.src_dir for service in generator.SERVICES}

    findings = []
    missing_from_services = dirs_on_disk - declared
    stale_in_services = declared - dirs_on_disk
    if missing_from_services:
        findings.append(
            f"src/rag directories with no SERVICES entry: {sorted(missing_from_services)}"
        )
    if stale_in_services:
        findings.append(
            f"SERVICES src_dir entries with no matching src/rag directory: "
            f"{sorted(stale_in_services)}"
        )
    return findings


# ATHENA-88 / F41 (codex r2 Medium): the pre-F91 wrong names. Any of these
# still active (as an env declaration or a documented table/code-span
# value) in an operator-facing example/doc file means an operator would
# configure a key the code never reads.
STALE_RAG_KEY_NAMES = frozenset({
    "YELP_API_KEY",
    "NEWSAPI_KEY",
    "SEATGEEK_API_KEY",
    "SERPAPI_KEY",
    "BRIGHTDATA_API_KEY",
    "GOOGLE_MAPS_API_KEY",
    "TESLA_API_KEY",
})

OPERATOR_FACING_DOC_FILES = (
    ".env.secrets.example",
    "config.env.example",
    "docs/INSTALLATION.md",
    "docs/MODULES.md",
    "docs/CONFIGURATION.md",
)

# An active or commented env-style declaration: "NAME=" or "# NAME=" at the
# start of a line (same shape check-env-example.py's own ENV_LINE uses).
_ENV_DECLARATION_RE = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)
# A markdown code-span name, e.g. a table cell: `NEWSAPI_KEY`.
_BACKTICK_NAME_RE = re.compile(r"`([A-Z][A-Z0-9_]*)`")


def check_stale_doc_references(root: Path) -> list:
    findings = []
    for rel_path in OPERATOR_FACING_DOC_FILES:
        path = root / rel_path
        if not path.exists():
            continue
        text = path.read_text()
        names_found = set(_ENV_DECLARATION_RE.findall(text)) | set(_BACKTICK_NAME_RE.findall(text))
        stale = names_found & STALE_RAG_KEY_NAMES
        if stale:
            findings.append(f"{rel_path}: stale RAG key name(s) still present: {sorted(stale)}")
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="Repo root to check.")
    args = parser.parse_args(argv)
    root = Path(args.root)

    findings = []
    findings += check_per_service(root)
    findings += check_create_secrets(root)
    findings += check_ref_shape(root)
    findings += check_manifest_drift(root)
    findings += check_src_dir_population(root)
    findings += check_stale_doc_references(root)

    if findings:
        print("RAG key-env drift detected:")
        for finding in findings:
            print(f"  - {finding}")
        return 1

    print("RAG key-env check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
