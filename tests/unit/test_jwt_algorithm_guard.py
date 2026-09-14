"""
Unit tests for scripts/check-jwt-algorithm-guard.py — the mechanical
precondition scripts/audit-images.sh gates its PYSEC-2026-1325 allowlist on.

This is the durable gate: the ad-hoc `git clone --no-local` probes run during
development proved each case once, by hand, against whatever the script
looked like at that moment. This file is what keeps proving it after every
future edit to the guard.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_file: str):
    name = module_file.replace("-", "_").rstrip(".py")
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPTS_DIR / module_file
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load("check-jwt-algorithm-guard.py")

BASE_OIDC = textwrap.dedent(
    """\
    from jose import jwt as _j

    JWT_ALGORITHM = "HS256"


    def encode_it(data, secret):
        return _j.encode(data, secret, algorithm=JWT_ALGORITHM)


    def decode_it(token, secret):
        return _j.decode(token, secret, algorithms=[JWT_ALGORITHM])
    """
)


def _scaffold(
    tmp_path: Path,
    oidc_extra: str = "",
    main_extra: str = "",
    app_extra_files: "dict[str, bytes | str] | None" = None,
    tests_extra_files: "dict[str, str] | None" = None,
) -> Path:
    """Minimal admin/backend + src/shared tree the guard's scan scope covers."""
    oidc_dir = tmp_path / "admin/backend/app/auth"
    oidc_dir.mkdir(parents=True)
    (oidc_dir / "oidc.py").write_text(BASE_OIDC + oidc_extra, encoding="utf-8")

    (tmp_path / "admin/backend/main.py").write_text("# main\n" + main_extra, encoding="utf-8")

    tests_dir = tmp_path / "admin/backend/tests"
    tests_dir.mkdir(parents=True)
    for name, content in (tests_extra_files or {}).items():
        (tests_dir / name).write_text(content, encoding="utf-8")

    shared_dir = tmp_path / "src/shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "__init__.py").write_text("", encoding="utf-8")

    for name, content in (app_extra_files or {}).items():
        p = tmp_path / "admin/backend/app" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")

    return tmp_path


def _rc(root: Path) -> "tuple[int, list, str | None]":
    errors, tool_error = guard.check(str(root))
    if tool_error:
        return 2, errors, tool_error
    if errors:
        return 1, errors, tool_error
    return 0, errors, tool_error


def test_real_repo_tree_passes():
    rc, errors, tool_error = _rc(REPO_ROOT)
    assert rc == 0, (errors, tool_error)


def test_shipped_form_from_scratch_tree_passes(tmp_path):
    root = _scaffold(tmp_path)
    rc, errors, tool_error = _rc(root)
    assert rc == 0, (errors, tool_error)


def test_case_a_import_jose_jwt_dotted_no_algorithms(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra='\nimport jose.jwt\n\n\ndef _case_a(t, k):\n    return jose.jwt.decode(t, k)\n',
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("no algorithms=" in e for e in errors)


def test_case_b_from_jose_jwt_import_decode_bare_call(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra='\nfrom jose.jwt import decode\n\n\ndef _case_b(t, k):\n    return decode(t, k)\n',
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("no algorithms=" in e for e in errors)


def test_case_c_jws_verify_with_ec_algorithm(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra=(
            '\nfrom jose import jws\n\n\n'
            'def _case_c(t, k):\n    return jws.verify(t, k, algorithms=["ES256"])\n'
        ),
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("disallowed algorithm 'ES256'" in e for e in errors)


def test_case_d_global_rebind(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra='\ndef _switch():\n    global JWT_ALGORITHM\n    JWT_ALGORITHM = "ES256"\n',
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("global JWT_ALGORITHM" in e for e in errors)
    assert any("expected exactly one JWT_ALGORITHM binding" in e for e in errors)


def test_case_e_jose_import_outside_app_dir(tmp_path):
    root = _scaffold(
        tmp_path,
        main_extra='\nfrom jose import jwt as _j\n\n\ndef _case_e(t, k):\n    return _j.decode(t, k)\n',
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("no algorithms=" in e for e in errors)


def test_case_f_non_utf8_file_is_tool_error(tmp_path):
    root = _scaffold(
        tmp_path,
        app_extra_files={"_bad_encoding.py": b"x = 1\n# bad byte: \xff\n"},
    )
    rc, errors, tool_error = _rc(root)
    assert rc == 2
    assert tool_error is not None
    assert "UTF-8" in tool_error or "utf-8" in tool_error.lower()


def test_case_g_getattr_dynamic_dispatch(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra='\ndef _case_g(t, k):\n    return getattr(_j, "decode")(t, k)\n',
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("getattr(...)" in e for e in errors)


def test_encode_with_non_hs256_algorithm_fails(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra='\ndef _case_enc(t, k):\n    return _j.encode(t, k, algorithm="RS256")\n',
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("algorithm= is 'RS256'" in e for e in errors)


def test_tracked_callable_referenced_without_call_fails(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra="\n_alias_ref = _j.decode\n",
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("referenced without being called" in e for e in errors)


def test_comment_or_string_containing_jwt_decode_does_not_false_positive(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra=(
            '\n# jwt.decode(t, k) — a comment, not code\n'
            '_note = "call jwt.decode(t, k) manually if needed"\n'
        ),
    )
    rc, errors, tool_error = _rc(root)
    assert rc == 0, (errors, tool_error)


def test_current_shipped_alias_form_passes(tmp_path):
    """`from jose import jwt as j` + `j.decode(..., algorithms=[JWT_ALGORITHM])`
    — the exact call shape this codebase ships in oidc.py."""
    root = _scaffold(tmp_path)
    rc, errors, tool_error = _rc(root)
    assert rc == 0, (errors, tool_error)


def test_tests_directory_is_excluded_from_scan(tmp_path):
    """A test fixture legitimately constructing a dangerous jose call must
    not turn the guard red — only admin/backend/tests/ is excluded."""
    root = _scaffold(
        tmp_path,
        tests_extra_files={
            "test_something.py": (
                'from jose import jwt as _j\n\n\n'
                'def test_rejects_es256_tokens():\n'
                '    _j.decode("t", "k", algorithms=["ES256"])\n'
            )
        },
    )
    rc, errors, tool_error = _rc(root)
    assert rc == 0, (errors, tool_error)


def test_attribute_assignment_to_jwt_algorithm_fails(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra=(
            '\nclass _Cfg:\n    pass\n\n\n'
            '_cfg = _Cfg()\n'
            '_cfg.JWT_ALGORITHM = "ES256"\n'
        ),
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("attribute assignment to JWT_ALGORITHM" in e for e in errors)


def test_double_star_kwargs_on_decode_fails(tmp_path):
    root = _scaffold(
        tmp_path,
        oidc_extra=(
            '\ndef _case_kwargs(t, k, extra):\n'
            '    return _j.decode(t, k, **extra)\n'
        ),
    )
    rc, errors, _ = _rc(root)
    assert rc == 1
    assert any("**kwargs" in e for e in errors)
