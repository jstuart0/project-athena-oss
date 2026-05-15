"""Regression guards for audit Campaign 1 (C1).

Two guards:

1. test_no_maintainer_ip_in_model_defaults — ensures no future PR re-introduces
   a maintainer IP (192.168.x.x) or maintainer domain (*.xmojo.net) as a
   column default in admin/backend/app/models.py.

2. test_html_labels_have_association — parses admin/frontend/*.html with
   BeautifulSoup and asserts that every <label> that is NOT a wrapping label
   (Pattern B: direct child is <input>/<select>/<textarea>) has either:
     - a `for=` attribute pointing at an input, OR
     - a nested <input>/<select>/<textarea> with `aria-labelledby=` or `aria-label=`.

   The test is currently marked xfail because Phase 4 (ruby:1 form-label
   associations) has not yet been implemented. Jackson will flip this to
   expected-pass when Phase 4 lands.

ATHENA-11 C1 campaign — see thoughts/shared/plans/2026-05-15-deliver-audit-deferred-cleanup-batch.md
"""

import re
from pathlib import Path

import pytest

# ── path setup ───────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADMIN_BACKEND = _REPO_ROOT / "admin" / "backend"
_ADMIN_FRONTEND = _REPO_ROOT / "admin" / "frontend"


# ── guard 1: no maintainer IP/domain in models.py column defaults ─────────────

def test_no_maintainer_ip_in_model_defaults():
    """Assert that models.py has no column default or server_default whose value
    contains a maintainer homelab IP (http://192.168.) or maintainer domain
    (https?://*.xmojo.net).

    This guards against a future PR accidentally reintroducing the literals that
    commits 4f6b159 and 5403a8a removed (bob:1 / ATHENA-11 Phase 3 + Phase 5).
    """
    models_path = _ADMIN_BACKEND / "app" / "models.py"
    text = models_path.read_text(encoding="utf-8")

    # Match occurrences of default= or server_default= followed by a string
    # literal that contains the maintainer's homelab IP range or domain.
    bad = re.findall(
        r'(?:default|server_default)\s*=\s*["\']?(?:http://192\.168\.|https?://[^"\']*xmojo\.net)',
        text,
    )
    assert not bad, (
        f"Maintainer IP/domain found in admin/backend/app/models.py column defaults:\n"
        + "\n".join(bad)
    )


# ── guard 2: HTML label associations ─────────────────────────────────────────

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

_HTML_FILES = list(_ADMIN_FRONTEND.glob("*.html"))

# Phase 4 (ruby:1) has not yet been implemented; this test is expected to fail
# until that phase lands. Jackson will remove the xfail marker when Phase 4 is
# complete and the test passes.
@pytest.mark.xfail(
    reason="Phase 4 (ruby:1 form-label associations) not yet implemented — remove marker when labels are fixed",
    strict=True,
)
@pytest.mark.skipif(
    not _BS4_AVAILABLE,
    reason="beautifulsoup4 not installed; add it to requirements.txt",
)
@pytest.mark.parametrize("path", _HTML_FILES, ids=lambda p: p.name)
def test_html_labels_have_association(path: Path):
    """Every <label> in an admin/frontend HTML file that does NOT wrap a form
    control as a direct child (Pattern B) must have a `for=` attribute, OR the
    nested input must carry `aria-labelledby=` or `aria-label=`.

    Pattern B (wrapping): <label><input .../> Text</label>
      → implicit association by HTML spec; `for=` is redundant. Skip.

    Pattern A (adjacent block): <label>Text</label> … <input id="x"/>
      → fix: add for="x" to the label.

    Pattern C (separated toggle): <label class="relative inline-flex …">
        (visual track only, no direct-child input; human text is a sibling)
      → fix: add aria-labelledby="text-element-id" to the nested input.
    """
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    failures = []

    for label in soup.find_all("label"):
        # Pattern B: direct child is a form control — implicit association, skip.
        direct_controls = [
            c
            for c in label.children
            if getattr(c, "name", None) in ("input", "select", "textarea")
        ]
        if direct_controls:
            continue

        # Pattern A: label has a `for=` attribute — correctly associated.
        if label.get("for"):
            continue

        # Pattern C (or unclassified): look for a nested input/select/textarea
        # with aria-labelledby or aria-label.
        nested = label.find(["input", "select", "textarea"])
        if nested and (nested.get("aria-labelledby") or nested.get("aria-label")):
            continue

        line = getattr(label, "sourceline", "?")
        failures.append(
            f"  <label> at line {line} has no for=, and no nested control with "
            f"aria-labelledby/aria-label: {str(label)[:120]}"
        )

    assert not failures, (
        f"{path.name}: {len(failures)} unassociated label(s):\n"
        + "\n".join(failures)
    )
