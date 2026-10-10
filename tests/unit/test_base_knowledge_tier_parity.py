"""Base-knowledge admin UI: tier vocabulary parity with shared.knowledge_tiers,
row-template escaping, and the accessibility hooks the plan names.

Stdlib only (reads the sources), so it runs on the unit-min CI requirements.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from shared.knowledge_tiers import KNOWLEDGE_TIERS

FRONTEND = Path(__file__).resolve().parents[2] / "admin" / "frontend"
JS = (FRONTEND / "base-knowledge.js").read_text(encoding="utf-8")
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")

# Every ${...} in the row template must start with one of these helpers (each
# returns a constant or escaped string) or be a ternary of string literals on a
# local boolean.
ALLOWED = re.compile(
    r"^\s*(escapeHtml|Number|getCategoryColor|getAppliesToColor|priorityClass|descriptionHtml"
    r"|statusClass|statusLabel|toggleTarget|toggleClass|toggleLabel)\("
    r"|^\s*(enabled|isInstruction)\s*\?\s*'[^'$`]*'\s*:\s*'[^'$`]*'\s*$"
)
ROW_TEMPLATE = re.compile(r"knowledge\.map\(\s*entry\s*=>\s*`(.*?)`\s*\)\.join", re.S)
EXPRESSION = re.compile(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}")


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _elements():
    parser = _Elements()
    parser.feed(HTML)
    return parser.elements


def _by_id(element_id):
    return [(t, a) for t, a in _elements() if a.get("id") == element_id]


def _tab_html():
    start = HTML.index('id="tab-base-knowledge"')
    return start, HTML.index("<!-- Memory Management Tab -->", start)


def _radio_values():
    return [a["value"] for t, a in _elements()
            if t == "input" and a.get("type") == "radio" and a.get("name") == "knowledge-tier"]


def _filter_values():
    start, end = _tab_html()
    block = HTML[start:end]
    select = re.search(r'<select id="knowledge-applies-filter".*?</select>', block, re.S).group(0)
    return [v for v in re.findall(r'<option value="([^"]*)"', select) if v]


def _tier_label_keys():
    body = re.search(r"const TIER_LABELS = \{(.*?)\};", JS, re.S).group(1)
    return re.findall(r"^\s*(\w+):", body, re.M)


def test_radio_values_match_the_tiers():
    values = _radio_values()
    assert len(values) == 4  # floor
    assert "household" in values  # named member
    assert set(values) == set(KNOWLEDGE_TIERS)
    assert len(values) == len(set(values))


def test_filter_values_match_the_tiers():
    values = _filter_values()
    assert len(values) == 4 and "household" in values
    assert set(values) == set(KNOWLEDGE_TIERS)


def test_tier_labels_keys_match_the_tiers():
    keys = _tier_label_keys()
    assert len(keys) == 4 and "household" in keys
    assert set(keys) == set(KNOWLEDGE_TIERS)


def test_no_radio_is_checked_by_default():
    radios = [a for t, a in _elements()
              if t == "input" and a.get("type") == "radio" and a.get("name") == "knowledge-tier"]
    assert radios and all("checked" not in a for a in radios)


def test_no_entry_interpolation_remains():
    assert re.findall(r"\$\{\s*entry\.", JS) == []


def test_row_template_expressions_are_escaped_or_allowlisted():
    template = ROW_TEMPLATE.search(JS)
    assert template, "row template not found"
    exprs = EXPRESSION.findall(template.group(1))
    assert len(exprs) >= 5  # floor
    assert any(e.strip() == "escapeHtml(entry.key)" for e in exprs)  # named member
    offenders = [e.strip() for e in exprs if not ALLOWED.search(e)]
    assert offenders == []


def test_no_inline_handler_interpolates_data():
    assert not re.search(r"""on\w+\s*=\s*["'][^"']*\$\{""", JS)
    assert "onclick=" not in JS


def test_show_error_sets_text_not_markup():
    body = re.search(r"function showError\(.*?\n}\n", JS, re.S).group(0)
    assert "${message}" not in body
    assert ".textContent = message" in body


def test_required_elements_exist():
    for element_id in ("knowledge-owner-banner", "knowledge-instruction-warning",
                       "knowledge-category-display", "knowledge-key-display",
                       "knowledge-selection-toolbar", "knowledge-move-household",
                       "knowledge-owner-category-hint", "knowledge-tier-fieldset"):
        assert len(_by_id(element_id)) == 1, element_id


def test_banner_is_a_status_and_hidden_by_default():
    (_, banner), = _by_id("knowledge-owner-banner")
    assert banner.get("role") == "status" and "hidden" in banner["class"].split()


def test_move_button_is_aria_disabled_and_described_in_the_static_html():
    (_, button), = _by_id("knowledge-move-household")
    assert button.get("aria-disabled") == "true" and "disabled" not in button
    assert button.get("title") == "Select entries first"
    assert button.get("aria-describedby") == "knowledge-selected-count"
    assert len(_by_id("knowledge-selected-count")) == 1


def test_table_container_scrolls_horizontally():
    (_, container), = _by_id("base-knowledge-container")
    assert "overflow-x-auto" in container["class"].split()
    assert "overflow-hidden" not in container["class"].split()


def test_moving_owner_only_entries_asks_for_confirmation():
    body = re.search(r"async function moveSelectedToHousehold\(\).*?\n}\n", JS, re.S).group(0)
    assert "confirm(" in body and "Voice, SMS and anyone at home will hear them" in body


def test_modal_moves_focus_in_and_restores_it():
    assert "knowledgeModalOpener = document.activeElement" in JS
    assert "knowledgeModalOpener.focus()" in JS


def test_every_modal_field_has_a_label():
    label_targets = {a["for"] for t, a in _elements() if t == "label" and "for" in a}
    for field in ("knowledge-category", "knowledge-key", "knowledge-value", "knowledge-priority",
                  "knowledge-description", "knowledge-enabled", "knowledge-category-display",
                  "knowledge-key-display"):
        assert field in label_targets, field
        assert len(_by_id(field)) == 1
    for value in KNOWLEDGE_TIERS:
        assert f"knowledge-tier-{value}" in label_targets


def test_tier_fieldset_is_described_by_the_owner_category_hint():
    (_, fieldset), = _by_id("knowledge-tier-fieldset")
    assert fieldset.get("aria-describedby") == "knowledge-owner-category-hint"
    assert "Who hears this entry *" in HTML


def test_modal_close_button_is_labelled():
    start = HTML.index('id="knowledge-modal"')
    assert 'aria-label="Close"' in HTML[start:start + 1500]


def test_selection_is_capped_at_the_server_limit():
    assert "const MAX_BULK_TIER_IDS = 500;" in JS


def test_old_applies_to_wording_is_gone():
    assert "Applies To" not in JS
    start, end = _tab_html()
    assert "All Applies To" not in HTML[start:end]
    assert "applies_to to control" not in HTML
