"""Shipped profiles give the smart-home model a context that holds its prompt,
and a fresh install actually gets it (D18, D21).

Runs the real startup seed (`seed_oss_defaults`, which applies the
`ollama_qwen_small` profile in `fill_missing_only` mode) over an SQLite
database that already holds the rows migration 050 seeds, then reads the
`ModelConfiguration` rows back.
"""
from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database
from app.database import Base
from app.models import ModelConfiguration
from app.services import oss_profiles
from app.routes.model_config import ModelConfigResponse
from shared.config import _clear_cache_for_tests
from shared.llm_context import ContextMarks, ContextSettings, plan_context

DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
PROFILE = "ollama_qwen_small"
SH_INTENT_PROMPT_BYTES = 12919  # the captured sh_intent prompt
MIGRATION_050_ROWS = {"qwen3:4b": {"num_ctx": 4096}, "llama3.2:3b": {"num_ctx": 4096}, "phi3:mini": {"num_ctx": 4096}, "_default": {}}


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch):
    monkeypatch.delenv("LLM_SERVER_DEFAULT_CONTEXT", raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    @contextlib.contextmanager
    def context():
        yield session

    monkeypatch.setattr(database, "get_db_context", context)
    yield session
    session.close()
    engine.dispose()


def seed_migration_050(db):
    for name, options in MIGRATION_050_ROWS.items():
        db.add(
            ModelConfiguration(
                model_name=name,
                backend_type="auto" if name == "_default" else "ollama",
                temperature=0.7,
                max_tokens=4096,
                timeout_seconds=90,
                keep_alive_seconds=-1,
                ollama_options=options,
            )
        )
    db.commit()


def startup(monkeypatch, default_model):
    monkeypatch.setenv("ATHENA_DEFAULT_MODEL", default_model)
    monkeypatch.setattr(database, "OSS_DEFAULT_MODEL", default_model)
    database.seed_oss_defaults()


def options_of(db, model):
    db.expire_all()
    row = db.query(ModelConfiguration).filter(ModelConfiguration.model_name == model).first()
    return None if row is None else dict(row.ollama_options or {})


def test_every_shipped_ollama_row_holds_the_smart_home_prompt(monkeypatch):
    monkeypatch.setenv("ATHENA_DEFAULT_MODEL", DEFAULT_MODEL)
    checked = {}
    for name, profile in oss_profiles.profile_registry().items():
        for model, spec in profile["model_configurations"].items():
            if spec.get("backend_type") == "ollama":
                checked[f"{name}[{model}]"] = spec["ollama_options"]["num_ctx"]
    assert len(checked) >= 3
    assert f"{PROFILE}[{DEFAULT_MODEL}]" in checked
    assert set(checked.values()) == {8192}, checked


def test_shipped_rows_set_the_context_only_never_output_caps_or_short_timeouts(monkeypatch):
    # Output length comes from callers (answer_max_tokens); a row's num_predict
    # would override it for every caller of that model.
    monkeypatch.setenv("ATHENA_DEFAULT_MODEL", DEFAULT_MODEL)
    seen = 0
    for name, profile in oss_profiles.profile_registry().items():
        for model, spec in profile["model_configurations"].items():
            assert "max_tokens" not in spec, (name, model)
            if spec.get("backend_type") != "ollama":
                continue
            seen += 1
            options = spec["ollama_options"]
            assert "num_predict" not in options, (name, model)
            assert not [key for key in options if key.startswith("mirostat")], (name, model)
            assert set(options) <= {"num_ctx", "num_batch"}, (name, model)
            assert spec["timeout_seconds"] >= 90, (name, model)
    assert seen >= 3


def test_profile_versions_moved(monkeypatch):
    registry = oss_profiles.profile_registry()
    assert registry[PROFILE]["version"] == "1.3.0"
    assert registry["mlx_qwen_hybrid"]["version"] == "1.2.0"
    assert registry["safe_fallback"]["version"] == "1.2.0"


def test_a_fresh_install_gets_8192_on_its_default_model_row(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    options = options_of(db, DEFAULT_MODEL)
    assert options is not None and options["num_ctx"] == 8192
    # The standalone _default row is not applied by the profile (apply_profile
    # only uses the profile's _default as a fallback spec): pinned so a change
    # to the apply path shows up here.
    assert options_of(db, "_default") == {}


def test_a_default_model_seeded_at_4096_is_unchanged_and_the_planner_still_raises(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, "qwen3:4b")
    assert options_of(db, "qwen3:4b") == {"num_ctx": 4096}
    plan = plan_context(
        prompt_text="a" * SH_INTENT_PROMPT_BYTES,
        row_ctx=options_of(db, "qwen3:4b")["num_ctx"],
        num_predict=384,
        max_tokens=400,
        require_full=True,
        marks=ContextMarks(),
        model="qwen3:4b",
        settings=ContextSettings(mode="enforce", ceiling=16384, server_default=0),
    )
    assert (plan.send, plan.outcome) == (8192, "raised")


def test_an_operator_2048_row_without_ownership_is_never_touched(db, monkeypatch):
    seed_migration_050(db)
    operator = {"num_ctx": 2048, "num_batch": 128}
    db.add(ModelConfiguration(model_name=DEFAULT_MODEL, backend_type="ollama", ollama_options=dict(operator)))
    db.commit()
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL) == operator
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    assert options_of(db, DEFAULT_MODEL) == operator


def test_a_profile_owned_row_at_2048_is_upgraded_by_reconcile(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    path = oss_profiles.ownership_key("ModelConfiguration", DEFAULT_MODEL, "ollama_options")
    assert oss_profiles.get_ownership_map(db)[path]["managed"] is True
    row = db.query(ModelConfiguration).filter(ModelConfiguration.model_name == DEFAULT_MODEL).one()
    row.ollama_options = {"num_ctx": 2048, "num_batch": 256}  # what profile 1.1.0 wrote
    db.commit()
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    assert options_of(db, DEFAULT_MODEL)["num_ctx"] == 8192


NEW_ROW_OPTIONS = {"num_ctx": 8192, "num_batch": 256}


def default_row(db):
    db.expire_all()
    return db.query(ModelConfiguration).filter(ModelConfiguration.model_name == DEFAULT_MODEL).one()


def test_the_first_apply_on_an_install_without_a_default_model_row_changes_context_only(db, monkeypatch):
    seed_migration_050(db)
    oss_profiles.set_setting_text(db, oss_profiles.ACTIVE_PROFILE_KEY, "safe_fallback", "an operator's choice")
    assert options_of(db, DEFAULT_MODEL) is None
    startup(monkeypatch, DEFAULT_MODEL)
    row = default_row(db)
    assert row.ollama_options == NEW_ROW_OPTIONS  # no num_predict, no mirostat
    # everything else is what the model already ran on: the _default fallback row
    assert (float(row.temperature), row.max_tokens, row.timeout_seconds, row.keep_alive_seconds) == (0.7, 4096, 90, -1)
    assert row.timeout_seconds >= 90


def test_startup_leaves_an_existing_active_profile_alone(db, monkeypatch):
    seed_migration_050(db)
    oss_profiles.set_setting_text(db, oss_profiles.ACTIVE_PROFILE_KEY, "safe_fallback", "an operator's choice")
    startup(monkeypatch, DEFAULT_MODEL)
    assert oss_profiles.get_setting_text(db, oss_profiles.ACTIVE_PROFILE_KEY) == "safe_fallback"


def test_startup_sets_the_active_profile_when_there_is_none(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    assert oss_profiles.get_setting_text(db, oss_profiles.ACTIVE_PROFILE_KEY) == PROFILE


def test_an_explicit_apply_still_makes_the_profile_active(db, monkeypatch):
    seed_migration_050(db)
    oss_profiles.set_setting_text(db, oss_profiles.ACTIVE_PROFILE_KEY, "safe_fallback", "an operator's choice")
    startup(monkeypatch, DEFAULT_MODEL)
    oss_profiles.apply_profile(db, PROFILE, "fill_missing_only")
    assert oss_profiles.get_setting_text(db, oss_profiles.ACTIVE_PROFILE_KEY) == PROFILE


def test_reconcile_on_a_managed_row_replaces_its_options(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    default_row(db).ollama_options = {"num_ctx": 2048, "num_predict": 256, "mirostat": 2}
    db.commit()
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    assert options_of(db, DEFAULT_MODEL) == NEW_ROW_OPTIONS


def test_reconcile_on_a_managed_row_leaves_max_tokens_untouched(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    row = default_row(db)
    row.max_tokens = 1234
    row.ollama_options = {"num_ctx": 2048}
    db.commit()
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    row = default_row(db)
    assert row.ollama_options == NEW_ROW_OPTIONS and row.max_tokens == 1234
    owned = [path for path in oss_profiles.get_ownership_map(db) if path.endswith(".max_tokens") and path.startswith("ModelConfiguration:")]
    assert owned == []


def test_reconcile_leaves_a_detached_field_untouched(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    path = oss_profiles.ownership_key("ModelConfiguration", DEFAULT_MODEL, "ollama_options")
    ownership = oss_profiles.get_ownership_map(db)
    oss_profiles.mark_detached(ownership, path, PROFILE)
    oss_profiles.set_ownership_map(db, ownership)
    db.commit()
    operator = {"num_ctx": 4096, "num_predict": 512}
    default_row(db).ollama_options = dict(operator)
    db.commit()
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    assert options_of(db, DEFAULT_MODEL) == operator


@pytest.mark.parametrize("mode", ["fill_missing_only", "reconcile_profile"])
def test_an_apply_never_shortens_an_existing_timeout(db, monkeypatch, mode):
    seed_migration_050(db)
    db.add(
        ModelConfiguration(
            model_name=DEFAULT_MODEL, backend_type="ollama", timeout_seconds=120, ollama_options={}, max_tokens=512
        )
    )
    db.add(ModelConfiguration(model_name="qwen3:8b", backend_type="ollama", timeout_seconds=300, ollama_options={"num_ctx": 2048}))
    db.commit()
    startup(monkeypatch, DEFAULT_MODEL)
    oss_profiles.apply_profile(db, PROFILE, mode)
    db.expire_all()
    rows = {r.model_name: r for r in db.query(ModelConfiguration).all()}
    assert rows[DEFAULT_MODEL].timeout_seconds == 120 and rows[DEFAULT_MODEL].max_tokens == 512
    assert rows["qwen3:8b"].timeout_seconds == 300
    assert all(r.timeout_seconds >= 90 for r in rows.values())


def test_components_sharing_a_model_with_no_backend_row_create_one_backend(db, monkeypatch):
    monkeypatch.setenv("ATHENA_DEFAULT_MODEL", DEFAULT_MODEL)
    oss_profiles.set_setting_json(
        db,
        "_oss_cached_capabilities",
        [
            {
                "backend_key": "ollama:http://ollama.test:11434",
                "backend_type": "ollama",
                "endpoint_url": "http://ollama.test:11434",
                "display_name": "Ollama",
                "healthy": True,
                "status": "configured",
                "supports_install_action": True,
                "supports_hot_reload": False,
                "models": [
                    {
                        "name": DEFAULT_MODEL,
                        "backend_type": "ollama",
                        "endpoint_url": "http://ollama.test:11434",
                        "family": "qwen3",
                        "status": "configured",
                        "installability_state": "configured",
                        "supports_runtime_options": True,
                        "supports_hot_reload": False,
                        "backend_key": "ollama:http://ollama.test:11434",
                    }
                ],
                "errors": [],
            }
        ],
        "cached capabilities",
    )
    db.commit()
    result = oss_profiles.apply_profile(db, PROFILE, "fill_missing_only")
    assignments = result["assignment_plan"]["component_assignments"]
    selected = [d for d in (assignments.values() if isinstance(assignments, dict) else assignments) if d["selected_model"] == DEFAULT_MODEL]
    assert len(selected) >= 2
    assert db.query(oss_profiles.LLMBackend).filter(oss_profiles.LLMBackend.model_name == DEFAULT_MODEL).count() == 1


def set_server_default(monkeypatch, value):
    monkeypatch.setenv("LLM_SERVER_DEFAULT_CONTEXT", str(value))
    _clear_cache_for_tests()


def set_default_row_options(db, options):
    row = db.query(ModelConfiguration).filter(ModelConfiguration.model_name == "_default").one()
    row.ollama_options = options
    db.commit()


def test_a_known_server_default_is_never_lowered_by_a_new_row(db, monkeypatch):
    set_server_default(monkeypatch, 65536)
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL) == {"num_ctx": 65536, "num_batch": 256}


def test_an_unknown_server_default_uses_the_profile_value(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL)["num_ctx"] == 8192


def test_a_small_server_default_does_not_lower_the_profile_value(db, monkeypatch):
    set_server_default(monkeypatch, 4096)
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL)["num_ctx"] == 8192


def test_a_new_row_keeps_the_default_rows_options_and_its_larger_context(db, monkeypatch):
    seed_migration_050(db)
    set_default_row_options(db, {"num_ctx": 16384, "top_k": 20})
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL) == {"num_ctx": 16384, "top_k": 20, "num_batch": 256}


def test_a_larger_profile_value_beats_a_smaller_default_row_context(db, monkeypatch):
    seed_migration_050(db)
    set_default_row_options(db, {"num_ctx": 4096, "top_k": 20})
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL)["num_ctx"] == 8192
    assert options_of(db, DEFAULT_MODEL)["top_k"] == 20


def test_an_existing_row_is_never_touched_by_the_context_rule(db, monkeypatch):
    set_server_default(monkeypatch, 65536)
    seed_migration_050(db)
    db.add(ModelConfiguration(model_name=DEFAULT_MODEL, backend_type="ollama", ollama_options={"num_ctx": 2048}))
    db.commit()
    startup(monkeypatch, DEFAULT_MODEL)
    assert options_of(db, DEFAULT_MODEL) == {"num_ctx": 2048}
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    assert options_of(db, DEFAULT_MODEL) == {"num_ctx": 2048}


def test_the_new_rows_options_are_profile_owned_and_reconcile_keeps_the_larger_size(db, monkeypatch):
    set_server_default(monkeypatch, 65536)
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    path = oss_profiles.ownership_key("ModelConfiguration", DEFAULT_MODEL, "ollama_options")
    assert oss_profiles.get_ownership_map(db)[path]["managed"] is True
    oss_profiles.apply_profile(db, PROFILE, "reconcile_profile")
    # reconcile writes the shipped value back (an explicit operator action)
    assert options_of(db, DEFAULT_MODEL)["num_ctx"] == 8192


def test_reset_never_nulls_a_column_that_has_a_default(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    ownership = oss_profiles.get_ownership_map(db)
    for field in ("max_tokens", "timeout_seconds", "temperature", "keep_alive_seconds", "description"):
        ownership[oss_profiles.ownership_key("ModelConfiguration", DEFAULT_MODEL, field)] = {
            "profile": PROFILE, "managed": True, "detached": False, "updated_at": "x",
        }
    oss_profiles.set_ownership_map(db, ownership)
    db.commit()
    row = default_row(db)
    row.max_tokens = 1234
    db.commit()
    oss_profiles.reset_profile_record(db, "ModelConfiguration", DEFAULT_MODEL)
    row = default_row(db)
    assert row.max_tokens == 2048  # the column default, not None; timeout is the profile's own 90
    assert row.timeout_seconds == 90 and row.keep_alive_seconds == -1
    assert float(row.temperature) == 0.4  # the profile's own value, written by the reset
    assert row.description is None  # a column without a default may still go back to None
    assert row.ollama_options == NEW_ROW_OPTIONS


def test_the_public_model_config_response_survives_null_columns(db, monkeypatch):
    seed_migration_050(db)
    startup(monkeypatch, DEFAULT_MODEL)
    row = default_row(db)
    row.max_tokens = None
    row.timeout_seconds = None
    row.temperature = None
    row.keep_alive_seconds = None
    response = ModelConfigResponse(**row.to_dict())  # what the public route returns
    assert (response.max_tokens, response.timeout_seconds, response.temperature, response.keep_alive_seconds) == (2048, 60, 0.7, -1)
    assert response.ollama_options == NEW_ROW_OPTIONS


def test_startup_leaves_a_pending_reload_pending(db, monkeypatch):
    seed_migration_050(db)
    pending = {
        "state": "pending_reload",
        "required_action": "restart_required",
        "affected_services": ["gateway"],
        "last_failure": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    oss_profiles.set_runtime_sync_state(db, dict(pending))
    db.commit()
    startup(monkeypatch, DEFAULT_MODEL)
    assert oss_profiles.runtime_sync_state(db) == pending


def test_an_explicit_apply_still_records_its_own_sync_state(db, monkeypatch):
    seed_migration_050(db)
    oss_profiles.set_runtime_sync_state(db, {"state": "pending_reload", "required_action": "restart_required", "affected_services": ["gateway"], "last_failure": None, "updated_at": "x"})
    db.commit()
    startup(monkeypatch, DEFAULT_MODEL)
    oss_profiles.apply_profile(db, PROFILE, "fill_missing_only")
    assert oss_profiles.runtime_sync_state(db)["updated_at"] != "x"


class _Recorder:
    def __init__(self):
        self.entries = []

    def __getattr__(self, level):
        return lambda event, **fields: self.entries.append({"event": event, "level": level, **fields})

    def __bool__(self):
        return True


def test_a_failed_seed_logs_the_error_type_only(db, monkeypatch):
    seed_migration_050(db)
    recorder = _Recorder()
    monkeypatch.setattr(database, "logger", recorder)

    def boom(*args, **kwargs):
        raise RuntimeError("could not reach http://10.9.8.7:11434/secret-path")

    monkeypatch.setattr(oss_profiles, "apply_profile", boom)
    startup(monkeypatch, DEFAULT_MODEL)
    failed = [e for e in recorder.entries if e["event"] == "oss_profile_seed_failed"]
    assert failed == [{"event": "oss_profile_seed_failed", "level": "warning", "error_type": "RuntimeError"}]
