"""The published "What's sent" table is exactly the payload: set equality
between the schema's leaf paths and the backticked paths in the table, plus
the disclosures the README and CONFIGURATION.md must carry."""
from __future__ import annotations

import re

from app.services.telemetry.schema import Payload
from tests._telemetry_support import REPO, schema_leaf_paths

CONFIGURATION = REPO / "docs" / "CONFIGURATION.md"
README = REPO / "README.md"


def _section(path, heading):
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"{path.name} has no '## {heading}' section"
    return match.group(1)


def _table_paths(section):
    whats_sent = re.search(r"^### What's sent\s*$(.*?)(?=^### |\Z)", section, re.M | re.S)
    assert whats_sent, "the Telemetry section has no '### What's sent' table"
    paths = set()
    for line in whats_sent.group(1).splitlines():
        cell = re.match(r"^\|\s*`([^`]+)`\s*\|", line)
        if cell:
            paths.add(cell.group(1))
    return paths


def test_whats_sent_table_is_exactly_the_payload():
    documented = _table_paths(_section(CONFIGURATION, "Telemetry"))
    schema = set(schema_leaf_paths(Payload.model_json_schema()))
    assert len(schema) >= 35
    assert "llm.components[].locality" in schema
    assert documented == schema, (
        f"undocumented: {sorted(schema - documented)}; documented but not sent: {sorted(documented - schema)}")


def test_configuration_telemetry_section_discloses_the_essentials():
    section = _section(CONFIGURATION, "Telemetry")
    for needle in ("pseudonymous", "analytics mode", "ATHENA_TELEMETRY", "DO_NOT_TRACK", "ATHENA_TELEMETRY_ENDPOINT",
                   "ATHENA_TELEMETRY_MODE", "35 days", "400 days", "Reset telemetry identity", "isn't unlinkability",
                   "Never sent", "family", "size bucket"):
        assert needle in section, needle
    toc = CONFIGURATION.read_text(encoding="utf-8").split("\n---", 1)[0]
    assert "(#telemetry)" in toc


def test_readme_telemetry_section():
    section = _section(README, "Telemetry")
    for needle in ("ATHENA_TELEMETRY=off", "DO_NOT_TRACK=1", "pseudonymous", "docs/CONFIGURATION.md"):
        assert needle in section, needle
    security = _section(README, "Security & OSS-First")
    assert "telemetry" in security.lower()
