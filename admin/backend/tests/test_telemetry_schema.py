"""Payload v1: the committed JSON Schema, the shared fixtures, and strict
unknown-key rejection at every object node."""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from app.services.telemetry.schema import Payload, json_schema_text
from tests._telemetry_support import FIXTURES, SCHEMA_FILE, error_paths, iter_object_nodes, load_fixture


def test_committed_json_schema_matches_model():
    """docs/telemetry/payload-v1.schema.json is generated from the model.

    If this fails after a pydantic upgrade (the generator's output changed,
    not the model), regenerate the file:
        cd admin/backend && python -c "from app.services.telemetry.schema import json_schema_text; \
open('../../docs/telemetry/payload-v1.schema.json','w').write(json_schema_text())"
    and copy it to the collector's fixtures (scripts/sync-athena-fixtures.sh).
    """
    assert SCHEMA_FILE.exists(), f"{SCHEMA_FILE} is missing"
    assert SCHEMA_FILE.read_text(encoding="utf-8") == json_schema_text()


def test_json_schema_text_is_the_model_schema():
    assert json_schema_text() == json.dumps(Payload.model_json_schema(), indent=2, sort_keys=True) + "\n"


def _valid_fixtures():
    return sorted(p.name for p in FIXTURES.glob("valid-v1-*.json"))


def _invalid_fixtures():
    return sorted(p.name for p in FIXTURES.glob("invalid-v1-*.json"))


def test_valid_fixture_population():
    names = _valid_fixtures()
    assert len(names) >= 3, names
    assert "valid-v1-unknown-feature-name.json" in names
    assert "valid-v1-full.json" in names


def test_valid_fixtures_accepted():
    names = _valid_fixtures()
    assert names
    for name in names:
        Payload.model_validate_json((FIXTURES / name).read_bytes())


def test_unknown_feature_name_is_accepted_on_the_wire():
    payload = load_fixture("valid-v1-unknown-feature-name.json")
    assert "future_flag_xyz" in payload["features"]["flags_enabled"]
    Payload.model_validate(payload)


def test_invalid_fixture_population():
    names = _invalid_fixtures()
    assert len(names) >= 13, names
    assert "invalid-v1-proto-key.json" in names
    assert "invalid-v1-components-17.json" in names
    for name in names:
        assert (FIXTURES / (name[: -len(".json")] + ".expect")).exists(), f"{name} has no .expect sidecar"


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("invalid-v1-*.json")) or ["<none>"])
def test_invalid_fixture_rejected_at_expect_path(name):
    assert name != "<none>", "no invalid fixtures"
    expect = (FIXTURES / (name[: -len(".json")] + ".expect")).read_text(encoding="utf-8").strip()
    with pytest.raises(ValidationError) as info:
        Payload.model_validate_json((FIXTURES / name).read_bytes())
    assert error_paths(info.value) == {expect}


def test_components_cap_is_sixteen():
    payload = load_fixture("valid-v1-full.json")
    component = payload["llm"]["components"][0]
    payload["llm"]["components"] = [dict(component) for _ in range(16)]
    Payload.model_validate(payload)
    payload["llm"]["components"].append(dict(component))
    with pytest.raises(ValidationError) as info:
        Payload.model_validate(payload)
    assert error_paths(info.value) == {"llm.components"}


def test_extra_keys_forbidden_at_every_object_node():
    base = load_fixture("valid-v1-full.json")
    Payload.model_validate(base)
    injected = set()
    rejected = set()
    for path, _node in list(iter_object_nodes(base)):
        mutated = copy.deepcopy(base)
        target = mutated
        if path:
            for part in path.replace("[", ".[").split("."):
                target = target[int(part[1:-1])] if part.startswith("[") else target[part]
        target["x_extra"] = 1
        key_path = f"{path}.x_extra" if path else "x_extra"
        injected.add(key_path)
        try:
            Payload.model_validate(mutated)
        except ValidationError as exc:
            if key_path in error_paths(exc):
                rejected.add(key_path)
    assert len(injected) >= 10
    assert "llm.components[0].x_extra" in injected
    assert injected == rejected
