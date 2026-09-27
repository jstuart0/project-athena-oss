"""Red/green contract for ATHENA-88 phase 4 (F91): RAG API-key env-name
alignment between service code, the manifest generator, create-secrets.sh,
the committed manifest, and the new drift checker.

Plan: .mozart/plans/active/2026-09-26-deliver-athena-voice-intent-defects.md,
Phase 4. Test contract: same directory,
2026-09-26-deliver-athena-voice-intent-defects.test-contract.md, Phase 4
C1-C8 (r1/r2 amendments).
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import re
import shutil
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / "scripts" / "check-rag-key-env.py"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate-rag-manifests.py"
ENV_EXAMPLE_CHECKER_PATH = REPO_ROOT / "scripts" / "check-env-example.py"
CREATE_SECRETS_PATH = REPO_ROOT / "scripts" / "create-secrets.sh"
COMMITTED_MANIFEST_PATH = REPO_ROOT / "manifests" / "athena-prod" / "rag-services.yaml"
RAG_SRC_ROOT = REPO_ROOT / "src" / "rag"

CREDENTIAL_SUFFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*(_API_KEY|_KEY|_TOKEN|_SECRET|_CLIENT_ID)$")


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_checker():
    return _load_module(CHECKER_PATH, "check_rag_key_env")


def _load_generator(monkeypatch=None, root: Path = REPO_ROOT):
    if monkeypatch is not None:
        monkeypatch.delenv("REGISTRY", raising=False)
        monkeypatch.delenv("TAG", raising=False)
    return _load_module(root / "scripts" / "generate-rag-manifests.py", "generate_rag_manifests")


def _load_env_example_checker(root: Path = REPO_ROOT):
    return _load_module(root / "scripts" / "check-env-example.py", "check_env_example")


def _credential_names(names) -> set:
    return {n for n in names if CREDENTIAL_SUFFIX_RE.match(n) and n != "SERVICE_API_KEY"}


def _block_for(manifest_text: str, key_name: str):
    return re.search(rf"- name: {re.escape(key_name)}\n(?:.*\n){{0,5}}", manifest_text)


# ---------------------------------------------------------------------------
# 1. test_generator_names_equal_code_names_per_service
# ---------------------------------------------------------------------------

def test_generator_names_equal_code_names_per_service(monkeypatch):
    generator = _load_generator(monkeypatch)
    env_example_checker = _load_env_example_checker()

    assert len(generator.SERVICES) == 23

    src_dirs_on_disk = {
        p.name for p in RAG_SRC_ROOT.iterdir() if p.is_dir() and (p / "main.py").exists()
    }
    assert {s.src_dir for s in generator.SERVICES} == src_dirs_on_disk

    mismatches = []
    named = {}
    for service in generator.SERVICES:
        code_names = _credential_names(
            env_example_checker.collect_getenv_names(RAG_SRC_ROOT / service.src_dir)
        )
        generator_names = set(service.key_envs)
        if service.name in ("dining", "news", "seatgeek"):
            named[service.name] = generator_names
        if code_names != generator_names:
            mismatches.append((service.name, sorted(generator_names), sorted(code_names)))

    assert mismatches == []
    assert named["dining"] == {"GOOGLE_PLACES_API_KEY"}
    assert named["news"] == set()
    assert named["seatgeek"] == {"SEATGEEK_CLIENT_ID", "SEATGEEK_CLIENT_SECRET"}


# ---------------------------------------------------------------------------
# 2. test_create_secrets_api_keys_equal_generator_union
# ---------------------------------------------------------------------------

def test_create_secrets_api_keys_equal_generator_union(monkeypatch):
    generator = _load_generator(monkeypatch)
    union = set()
    for service in generator.SERVICES:
        union |= set(service.key_envs)
    assert len(union) == 17

    secrets_text = CREATE_SECRETS_PATH.read_text()
    match = re.search(
        r"create secret generic athena-api-keys(.*?)--dry-run=client", secrets_text, re.DOTALL
    )
    assert match is not None
    documented = set(re.findall(r"--from-literal=([A-Z0-9_]+)=", match.group(1)))
    assert documented == union


# ---------------------------------------------------------------------------
# 3. test_every_deployment_has_required_service_api_key
# ---------------------------------------------------------------------------

def test_every_deployment_has_required_service_api_key(monkeypatch):
    generator = _load_generator(monkeypatch)
    assert len(generator.SERVICES) == 23

    required_count = 0
    optional_count = 0
    for service in generator.SERVICES:
        manifest_text = generator.generate_deployment(service.name, service.port, service.key_envs)

        service_block = _block_for(manifest_text, "SERVICE_API_KEY")
        assert service_block is not None, service.name
        assert "optional: true" not in service_block.group(0)
        required_count += 1

        for key_env in service.key_envs:
            block = _block_for(manifest_text, key_env)
            assert block is not None, f"{service.name}:{key_env}"
            assert "optional: true" in block.group(0)
            optional_count += 1

    assert required_count == 23
    assert optional_count == 21


# ---------------------------------------------------------------------------
# 4. test_committed_manifest_matches_generator
# ---------------------------------------------------------------------------

def test_committed_manifest_matches_generator(monkeypatch):
    generator = _load_generator(monkeypatch)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        generator.main()

    generated_lines = [
        line for line in buf.getvalue().splitlines() if not line.startswith("# Generated:")
    ]
    committed_lines = [
        line for line in COMMITTED_MANIFEST_PATH.read_text().splitlines()
        if not line.startswith("# Generated:")
    ]
    assert generated_lines == committed_lines


# ---------------------------------------------------------------------------
# Fixture builder for tests 5-6 (synthetic drift trees)
# ---------------------------------------------------------------------------

def _write_fake_generator(root: Path, services) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    lines = [
        "import os",
        "from collections import namedtuple",
        "",
        "RagService = namedtuple('RagService', ['name', 'port', 'key_envs', 'src_dir'])",
        "",
        "SERVICES = [",
    ]
    for name, port, key_envs, src_dir in services:
        lines.append(f"    RagService({name!r}, {port}, {tuple(key_envs)!r}, {src_dir!r}),")
    lines.append("]")
    lines.append("")
    lines.append('REGISTRY = os.environ.get("REGISTRY", "YOUR_REGISTRY")')
    lines.append('TAG = os.environ.get("TAG", "latest")')
    lines.append("")
    lines.append("def generate_deployment(name, port, key_envs):")
    lines.append("    block = (")
    lines.append("        '        - name: SERVICE_API_KEY\\n'")
    lines.append("        '          valueFrom:\\n'")
    lines.append("        '            secretKeyRef:\\n'")
    lines.append("        '              name: athena-encryption\\n'")
    lines.append("        '              key: SERVICE_API_KEY'")
    lines.append("    )")
    lines.append("    for k in key_envs:")
    lines.append("        block += (")
    lines.append("            f'\\n        - name: {k}\\n'")
    lines.append("            f'          valueFrom:\\n'")
    lines.append("            f'            secretKeyRef:\\n'")
    lines.append("            f'              name: athena-api-keys\\n'")
    lines.append("            f'              key: {k}\\n'")
    lines.append("            f'              optional: true'")
    lines.append("        )")
    lines.append(f"    return f'--- # deployment {{name}} port {{port}}\\n' + block")
    lines.append("")
    lines.append("def generate_service(name, port):")
    lines.append(f"    return f'--- # service {{name}} port {{port}}'")
    lines.append("")
    lines.append("def main():")
    lines.append("    print('# Generated: FAKE')")
    lines.append("    for s in SERVICES:")
    lines.append("        print(generate_deployment(s.name, s.port, s.key_envs))")
    lines.append("        print(generate_service(s.name, s.port))")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    main()")
    (root / "scripts" / "generate-rag-manifests.py").write_text("\n".join(lines))


def _build_fixture_root(
    root: Path,
    *,
    dining_code_key: str = "GOOGLE_PLACES_API_KEY",
    include_zone_service: bool = False,
    manifest_missing_ref: bool = False,
    secrets_missing_name: bool = False,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "rag" / "dining").mkdir(parents=True, exist_ok=True)
    (root / "manifests" / "athena-prod").mkdir(parents=True, exist_ok=True)

    shutil.copy(ENV_EXAMPLE_CHECKER_PATH, root / "scripts" / "check-env-example.py")

    (root / "src" / "rag" / "dining" / "main.py").write_text(
        f'import os\nAPI_KEY = os.getenv("{dining_code_key}", "")\n'
    )
    services = [("dining", 8019, ("GOOGLE_PLACES_API_KEY",), "dining")]

    if include_zone_service:
        (root / "src" / "rag" / "zoneservice").mkdir(parents=True, exist_ok=True)
        (root / "src" / "rag" / "zoneservice" / "main.py").write_text(
            'import os\n'
            'ZONE = os.getenv("SOME_ZONE", "default")\n'
            'URL = os.getenv("SOME_URL", "")\n'
        )
        services.append(("zoneservice", 9000, (), "zoneservice"))

    _write_fake_generator(root, services)

    all_names = sorted({k for _, _, keys, _ in services for k in keys})
    if secrets_missing_name and all_names:
        all_names = all_names[1:]
    secrets_lines = ["kubectl create secret generic athena-api-keys \\"]
    for n in all_names:
        secrets_lines.append(f'    --from-literal={n}="${{{n}:-}}" \\')
    secrets_lines.append("    --dry-run=client -o yaml | kubectl apply -f -")
    (root / "scripts" / "create-secrets.sh").write_text("\n".join(secrets_lines))

    fake_gen = _load_module(root / "scripts" / "generate-rag-manifests.py", "fake_gen_fixture")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fake_gen.main()
    manifest_text = buf.getvalue()
    if manifest_missing_ref:
        manifest_text = re.sub(
            r"\n        - name: GOOGLE_PLACES_API_KEY\n(?:.*\n){0,5}",
            "\n",
            manifest_text,
            count=1,
        )
    (root / "manifests" / "athena-prod" / "rag-services.yaml").write_text(manifest_text)

    return root


# ---------------------------------------------------------------------------
# 5. test_checker_detects_drift
# ---------------------------------------------------------------------------

def test_checker_detects_drift(tmp_path):
    checker = _load_checker()

    root_a = _build_fixture_root(tmp_path / "a", dining_code_key="YELP_API_KEY")
    assert checker.main(["--root", str(root_a)]) == 1

    root_b = _build_fixture_root(tmp_path / "b", manifest_missing_ref=True)
    assert checker.main(["--root", str(root_b)]) == 1

    root_c = _build_fixture_root(tmp_path / "c", secrets_missing_name=True)
    assert checker.main(["--root", str(root_c)]) == 1

    # tessa: a fake service reading only non-credential SOME_ZONE/SOME_URL
    # must yield no finding -- the named negative for the credential-suffix
    # filter (a config var with a default, not a secret).
    root_d = _build_fixture_root(tmp_path / "d", include_zone_service=True)
    assert checker.main(["--root", str(root_d)]) == 0


# ---------------------------------------------------------------------------
# 6. test_checker_cli_exit_codes
# ---------------------------------------------------------------------------

def test_checker_cli_exit_codes(tmp_path):
    checker = _load_checker()
    assert checker.main(["--root", str(REPO_ROOT)]) == 0

    drift_root = _build_fixture_root(tmp_path / "drift", dining_code_key="YELP_API_KEY")
    assert checker.main(["--root", str(drift_root)]) == 1


# ---------------------------------------------------------------------------
# 7. test_checker_reuses_collect_getenv_names
# ---------------------------------------------------------------------------

def test_checker_reuses_collect_getenv_names():
    tree = ast.parse(CHECKER_PATH.read_text())

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "collect_getenv_names" in names

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    called_name = (
                        func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    )
                    assert called_name != "walk", (
                        f"{node.name} defines its own ast.walk call "
                        f"(should reuse check-env-example.py's collect_getenv_names)"
                    )


# ---------------------------------------------------------------------------
# 8. test_checker_main_diffs_committed_manifest
# ---------------------------------------------------------------------------

def _copy_real_tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(GENERATOR_PATH, root / "scripts" / "generate-rag-manifests.py")
    shutil.copy(ENV_EXAMPLE_CHECKER_PATH, root / "scripts" / "check-env-example.py")
    shutil.copy(CREATE_SECRETS_PATH, root / "scripts" / "create-secrets.sh")
    shutil.copytree(RAG_SRC_ROOT, root / "src" / "rag")
    (root / "manifests" / "athena-prod").mkdir(parents=True, exist_ok=True)
    shutil.copy(
        COMMITTED_MANIFEST_PATH, root / "manifests" / "athena-prod" / "rag-services.yaml"
    )
    return root


def test_checker_main_diffs_committed_manifest(tmp_path, capsys):
    root = _copy_real_tree(tmp_path)
    manifest_path = root / "manifests" / "athena-prod" / "rag-services.yaml"
    original = manifest_path.read_text()
    mutated = original.replace("GOOGLE_PLACES_API_KEY", "MUTATED_API_KEY", 1)
    assert mutated != original
    manifest_path.write_text(mutated)

    checker = _load_checker()
    rc = checker.main(["--root", str(root)])
    assert rc == 1

    output = capsys.readouterr().out
    assert "manifest drift" in output


# ---------------------------------------------------------------------------
# F42 (reconciliation round 1, codex r2 Low): the src/rag directory-set
# population check must run in the CLI checker itself, not only in
# test_generator_names_equal_code_names_per_service (pytest-only).
# ---------------------------------------------------------------------------

def test_checker_flags_new_rag_directory_with_no_services_entry(tmp_path):
    root = _copy_real_tree(tmp_path)
    (root / "src" / "rag" / "newservice").mkdir(parents=True)
    (root / "src" / "rag" / "newservice" / "main.py").write_text("import os\n")

    checker = _load_checker()
    rc = checker.main(["--root", str(root)])
    assert rc == 1


def test_checker_flags_stale_services_entry_with_no_directory(tmp_path):
    root = _copy_real_tree(tmp_path)
    generator_path = root / "scripts" / "generate-rag-manifests.py"
    text = generator_path.read_text()
    mutated = text.replace('"dining"),\n', '"dining_renamed"),\n', 1)
    assert mutated != text
    generator_path.write_text(mutated)

    checker = _load_checker()
    rc = checker.main(["--root", str(root)])
    assert rc == 1


def test_checker_real_repo_src_dir_population_passes():
    checker = _load_checker()
    findings = checker.check_src_dir_population(REPO_ROOT)
    assert findings == []


# ---------------------------------------------------------------------------
# F41 (reconciliation round 1, codex r2 Medium): stale RAG key names still
# advertised in operator-facing example/doc files.
# ---------------------------------------------------------------------------

def test_checker_flags_stale_name_in_env_secrets_example(tmp_path):
    root = _copy_real_tree(tmp_path)
    (root / ".env.secrets.example").write_text("YELP_API_KEY=\n")

    checker = _load_checker()
    findings = checker.check_stale_doc_references(root)
    assert any(".env.secrets.example" in f and "YELP_API_KEY" in f for f in findings)
    assert checker.main(["--root", str(root)]) == 1


def test_checker_flags_stale_name_in_markdown_table(tmp_path):
    root = _copy_real_tree(tmp_path)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "MODULES.md").write_text(
        "| Dining | 8019 | `YELP_API_KEY` | 5,000/day |\n"
    )

    checker = _load_checker()
    findings = checker.check_stale_doc_references(root)
    assert any("MODULES.md" in f and "YELP_API_KEY" in f for f in findings)


def test_checker_ignores_prose_mention_of_stale_name(tmp_path):
    """A comment explaining the historical drift (not an active
    declaration or a documented table value) must not false-positive."""
    root = _copy_real_tree(tmp_path)
    (root / ".env.secrets.example").write_text(
        "# Some modules historically read SerpAPI under SERPAPI_KEY.\n"
        "SERPAPI_API_KEY=\n"
    )

    checker = _load_checker()
    findings = checker.check_stale_doc_references(root)
    assert findings == []


def test_checker_real_repo_doc_files_clean():
    checker = _load_checker()
    findings = checker.check_stale_doc_references(REPO_ROOT)
    assert findings == []
