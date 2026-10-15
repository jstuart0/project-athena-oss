"""jarvis-web's image has no ``shared`` package: ``household_settings`` arrives
as a copied file over a committed local-dev shim, and ``household`` imports
only that. Modelled on test_jarvis_web_tts_normalization.py.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "apps" / "jarvis-web" / "backend"
DOCKERFILE = REPO_ROOT / "apps" / "jarvis-web" / "Dockerfile"
BACKEND_COPY = "COPY apps/jarvis-web/backend/ /app/backend/"
COPY_LINE = re.compile(r"^COPY\s+(src/shared/[\w.]+\.py)\s+/app/backend/([\w.]+\.py)\s*$")


def _shared_copies():
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    after = lines[lines.index(BACKEND_COPY) + 1:]
    return [m.groups() for m in map(COPY_LINE.match, after) if m]


def _is_shim(path: Path) -> bool:
    return "Local-dev shim" in path.read_text().split('"""')[1]


def test_household_settings_is_copied_exactly_once_after_the_backend_copy():
    copies = [c for c in _shared_copies() if c[1] == "household_settings.py"]
    assert copies == [("src/shared/household_settings.py", "household_settings.py")]
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    index = lines.index("COPY src/shared/household_settings.py /app/backend/household_settings.py")
    assert index > lines.index(BACKEND_COPY), "the shim would overwrite the copy"


def test_the_dockerfile_copy_set_equals_the_shim_set():
    shims = {p.name for p in BACKEND.glob("*.py") if _is_shim(p)}
    copied = {dest for _src, dest in _shared_copies()}
    assert shims == copied
    assert {"household_settings.py", "service_key.py", "tts_normalizer.py", "admin_url.py", "client_throttle.py"} <= shims
    for src, _dest in _shared_copies():
        assert (REPO_ROOT / src).is_file()


def test_household_is_jarvis_only_code_not_a_shim():
    assert not _is_shim(BACKEND / "household.py")


def test_the_household_shim_holds_no_logic():
    tree = ast.parse((BACKEND / "household_settings.py").read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.While, ast.Try))]
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert {"sys", "shared.household_settings"} <= imports
    assert "globals().update" in ast.unparse(tree)


def test_household_imports_nothing_from_shared():
    tree = ast.parse((BACKEND / "household.py").read_text())
    modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not [m for m in modules if m == "shared" or m.startswith("shared.")]
    assert "household_settings" in modules


def _image_layout(tmp_path: Path) -> Path:
    layout = tmp_path / "backend"
    shutil.copytree(BACKEND, layout, ignore=shutil.ignore_patterns("__pycache__"))
    for src, dest in _shared_copies():
        shutil.copyfile(REPO_ROOT / src, layout / dest)
    return layout


def test_the_reader_loads_and_resolves_in_the_image_layout_without_shared(tmp_path):
    layout = _image_layout(tmp_path)
    code = (
        f"import sys; sys.path.insert(0, {str(layout)!r})\n"
        "import household_settings, household\n"
        "assert household_settings.__file__.startswith(sys.path[0]), household_settings.__file__\n"
        "cfg = household.settings()\n"
        "assert (cfg.default_room, cfg.climate_entity, cfg.min_temp, cfg.max_temp) == ('guest', 'climate.thermostat', 65, 75)\n"
        "household.install_for_test(sources={'jarvis_web': {'default_room': 'Den'}})\n"
        "assert household.settings().default_room == 'Den'\n"
        "assert 'shared' not in sys.modules\n"
    )
    # -I: no PYTHONPATH, no user site, no cwd on sys.path: what the image has.
    proc = subprocess.run([sys.executable, "-I", "-c", code], cwd=layout, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-800:]
