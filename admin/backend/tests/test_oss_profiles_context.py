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
from shared.llm_context import ContextMarks, ContextSettings, plan_context

DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
PROFILE = "ollama_qwen_small"
SH_INTENT_PROMPT_BYTES = 12919  # the captured sh_intent prompt
MIGRATION_050_ROWS = {"qwen3:4b": {"num_ctx": 4096}, "llama3.2:3b": {"num_ctx": 4096}, "phi3:mini": {"num_ctx": 4096}, "_default": {}}


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
        db.add(ModelConfiguration(model_name=name, backend_type="ollama", ollama_options=options))
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


def test_profile_versions_moved(monkeypatch):
    registry = oss_profiles.profile_registry()
    assert registry[PROFILE]["version"] == "1.2.0"
    assert registry["mlx_qwen_hybrid"]["version"] == "1.1.0"
    assert registry["safe_fallback"]["version"] == "1.1.0"


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
