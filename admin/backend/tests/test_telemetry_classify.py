"""Pure telemetry classifiers, table-driven: the model-field corpus (D42 +
r3.2), locality (D10/CB3), install class and version grammar (D4),
enablement precedence and parsing (D3), and the buckets."""
from __future__ import annotations

import itertools
import json
import random
import re
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from app.services.telemetry import classify
from app.services.telemetry.schema import (
    FAMILY_RE,
    INTENT_PERCENTS,
    MODEL_FAMILIES,
    MODEL_SOURCES,
    SIZE_BUCKETS,
    bucket_count,
    bucket_rate,
    intent_mix,
)

DEFAULT = "https://collector.example.org/v1/ping"


# ---------------------------------------------------------------------------
# D42 model fields
# ---------------------------------------------------------------------------

# (raw, backend, component, family, size_bucket, quantized, source). The
# component decides openai locality (CB3); locality feeds source rule 2.
CORPUS = [
    ("qwen3:4b-instruct-2507-q4_K_M", "ollama", "intent_classifier", "qwen3", "4-9b", True, "ollama-library"),
    ("llama3.1:8b", "ollama", "intent_classifier", "llama3.1", "4-9b", False, "ollama-library"),
    ("gpt-oss:120b-cloud", "ollama", "intent_classifier", "gpt-oss", "gt80b", False, "ollama-library"),
    ("registry.ollama.ai/library/qwen3:8b", "ollama", "intent_classifier", "qwen3", "4-9b", False, "ollama-library"),
    ("mixtral:8x7b", "ollama", "intent_classifier", "mixtral", "41-80b", False, "ollama-library"),
    ("qwen3:30b-a3b", "ollama", "intent_classifier", "qwen3", "21-40b", False, "ollama-library"),
    ("qwen2.5:0.5b", "ollama", "intent_classifier", "qwen2.5", "le3b", False, "ollama-library"),
    ("smollm2:135m", "ollama", "intent_classifier", "smollm2", "le3b", False, "ollama-library"),
    ("deepseek-r1:671b", "ollama", "intent_classifier", "deepseek-r1", "gt80b", False, "ollama-library"),
    ("openai/gpt-4o-mini", "openai", "intent_classifier", "gpt-4o", "unknown", False, "vendor-api"),
    ("gpt-4o-mini", "openai", "intent_classifier", "gpt-4o", "unknown", False, "vendor-api"),
    ("claude-sonnet-4-20250514", "anthropic", "intent_classifier", "claude-sonnet", "unknown", False, "vendor-api"),
    ("my-azure-deployment", "openai", "intent_classifier", "custom", "unknown", False, "vendor-api"),
    ("hf.co/bartowski/qwen2.5-7b-instruct-gguf:q4_k_m", "ollama", "intent_classifier",
     "qwen2.5", "4-9b", True, "hf-public-publisher"),
    ("hf.co/google/jays-house-tuned", "ollama", "intent_classifier", "custom", "unknown", False, "hf-public-publisher"),
    ("hf.co/sentinel-user/m", "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
    ("jays-house-tuned:latest", "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
    # r3.1/r3.2: unrecognized text after the family keeps the family but the
    # source is custom.
    ("llama-jays-house", "ollama", "intent_classifier", "llama", "unknown", False, "custom"),
    ("qwen3:q4_ann_smi_th", "ollama", "intent_classifier", "qwen3", "unknown", True, "custom"),
    ("qwen3-02139", "ollama", "intent_classifier", "qwen3", "unknown", False, "ollama-library"),
    ("llama3-5551234567", "ollama", "intent_classifier", "llama3", "unknown", False, "ollama-library"),
    ("nas:5000/sentinel/m", "ollama", "intent_classifier", "custom", "unknown", False, "custom-registry"),
    ("registry:5000/private/model", "ollama", "intent_classifier", "custom", "unknown", False, "custom-registry"),
    ("registry.sentinel.example.com/private/m:latest", "ollama", "intent_classifier",
     "custom", "unknown", False, "custom-registry"),
    ("localhost/m", "ollama", "intent_classifier", "custom", "unknown", False, "custom-registry"),
    ("sentinel-user/m:latest", "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
    ("smithfamily/athena:latest", "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
    ("http://x", "ollama", "intent_classifier", "custom", "unknown", False, "custom-registry"),
    ("a@b", "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
    ("", "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
    (None, "ollama", "intent_classifier", "custom", "unknown", False, "custom"),
]


def _locality_for(backend, component, raw):
    endpoint = None if backend in ("openai", "anthropic", "google") else "http://localhost:11434"
    return classify.locality(backend, component, raw, endpoint, resolver=lambda host: ["127.0.0.1"])


@pytest.mark.parametrize("raw,backend,component,family,size,quant,source", CORPUS)
def test_classify_model_corpus(raw, backend, component, family, size, quant, source):
    fields = classify.classify_model(raw, backend, _locality_for(backend, component, raw))
    assert (fields.family, fields.size_bucket, fields.quantized, fields.source) == (family, size, quant, source)
    assert fields.family in MODEL_FAMILIES | {"custom"}
    assert fields.size_bucket in SIZE_BUCKETS
    assert fields.source in MODEL_SOURCES
    assert isinstance(fields.quantized, bool)


def test_model_families_charset():
    ordered = sorted(MODEL_FAMILIES)
    assert len(ordered) == len(set(ordered))
    assert len(ordered) >= 70
    assert "qwen3" in ordered and "custom" not in ordered
    for family in ordered:
        assert re.fullmatch(FAMILY_RE, family), family
        assert re.fullmatch(r"^[a-z0-9.\-]{1,24}$", family), family


def _allowed_output_text():
    words = set(MODEL_FAMILIES) | {"custom"} | set(SIZE_BUCKETS) | set(MODEL_SOURCES)
    words |= {"family", "size_bucket", "quantized", "source", "true", "false"}
    return words


def test_classify_model_property_no_input_text_leaks():
    rng = random.Random(0)
    families = sorted(MODEL_FAMILIES)
    pieces = ["jays", "house", "smith", "02139", "5551234567", "sentinel", "example.com", "nas:5000",
              "user@example.org", "10.1.2.3", "q4_k_m", "8b", "latest", "instruct", "hf.co", "bartowski",
              "google", "registry.local", "tuned", "9x7b", "0.5b", "cloud"]
    backends = ["ollama", "openai", "anthropic", "google", "mlx", "openai_compatible"]
    allowed = _allowed_output_text()
    for _ in range(500):
        parts = [rng.choice(families) if rng.random() < 0.5 else rng.choice(pieces)]
        for _ in range(rng.randint(0, 4)):
            parts.append(rng.choice(pieces + families + [str(rng.randint(0, 99999))]))
        seps = [rng.choice(["-", ":", "/", "_", ".", ""]) for _ in parts]
        raw = "".join(p + s for p, s in zip(parts, seps)).strip("/")
        backend = rng.choice(backends)
        fields = classify.classify_model(raw, backend, rng.choice(["local", "cloud", "remote", "unknown"]))
        assert fields.family in MODEL_FAMILIES | {"custom"}
        assert fields.size_bucket in SIZE_BUCKETS
        assert fields.source in MODEL_SOURCES
        text = json.dumps(fields.__dict__)
        lowered = raw.lower()
        for n in range(4, len(lowered) + 1):
            for i in range(0, len(lowered) - n + 1):
                sub = lowered[i:i + n]
                if sub in text:
                    assert any(sub in word for word in allowed), (raw, sub, text)


# ---------------------------------------------------------------------------
# D10 / CB3 locality
# ---------------------------------------------------------------------------

def _never(host):
    raise AssertionError(f"resolver must not be called for {host}")


@pytest.mark.parametrize("host", [
    "localhost", "[::1]", "[fd00::1]", "10.1.2.3", "100.101.102.103", "host.docker.internal", "ollama",
    "x.svc", "x.cluster.local", "x.local", "x.lan", "x.internal", "x.home.arpa", "x.localdomain", "OLLAMA.LAN.",
])
def test_local_hosts(host):
    assert classify.classify_host(host, _never) == "local"


@pytest.mark.parametrize("host", ["api.groq.com", "x.openai.azure.com", "bedrock-runtime.us-east-1.amazonaws.com",
                                  "API.OPENAI.COM.", "ollama.com"])
def test_cloud_hosts(host):
    assert classify.classify_host(host, _never) == "cloud"


@pytest.mark.parametrize("host", ["evilopenai.azure.com", "api.openai.com.attacker.example", "s3.amazonaws.com"])
def test_lookalike_hosts_fall_to_the_resolver(host):
    calls = []

    def resolver(h):
        calls.append(h)
        return ["8.8.4.4"]

    assert classify.classify_host(host, resolver) == "remote"
    assert calls == [host.lower()]


@pytest.mark.parametrize("host", ["[2606:4700::1]", "8.8.8.8"])
def test_global_literals_are_remote_without_resolving(host):
    assert classify.classify_host(host, _never) == "remote"


def test_resolver_results():
    assert classify.classify_host("box.example", lambda h: ["10.0.0.4"]) == "local"
    assert classify.classify_host("box.example", lambda h: ["10.0.0.4", "8.8.8.8"]) == "remote"

    def timeout(h):
        raise TimeoutError()

    assert classify.classify_host("box.example", timeout) == "unknown"
    assert classify.classify_host("box.example", lambda h: []) == "unknown"
    assert classify.classify_host("box.example", None) == "unknown"


def test_endpoint_host_normalizes():
    assert classify.endpoint_host("https://api.groq.com/openai/v1") == "api.groq.com"
    assert classify.endpoint_host("http://[FD00::1]:1234") == "fd00::1"
    assert classify.endpoint_host("http://OLLAMA.LAN.:11434") == "ollama.lan"
    assert classify.endpoint_host(None) is None
    assert classify.endpoint_host("not a url") is None


LAN = "http://10.0.0.5:1234"


@pytest.mark.parametrize("backend,component,model,endpoint,expected", [
    ("openai", "tool_calling_simple", "qwen3:8b", LAN, "local"),
    ("openai", "tool_calling_complex", "qwen3:8b", LAN, "local"),
    ("openai", "tool_calling_super_complex", "qwen3:8b", LAN, "local"),
    ("openai", "tool_calling_simple", "openai/gpt-4o", LAN, "cloud"),
    ("openai", "tool_calling_simple", "qwen3:8b", None, "cloud"),
    ("openai", "response_synthesis", "qwen3:8b", LAN, "cloud"),
    ("openai", "intent_classifier", "qwen3:8b", LAN, "cloud"),
    ("anthropic", "tool_calling_simple", "claude-sonnet", LAN, "cloud"),
    ("google", "tool_calling_simple", "gemini-2.5", LAN, "cloud"),
    ("openai", "intent_classifier", "openai/gpt-4o", None, "cloud"),
    # Rule 0 on every backend
    ("ollama", "intent_classifier", "gpt-oss:120b-cloud", "http://localhost:11434", "cloud"),
    ("openai_compatible", "intent_classifier", "x:cloud", LAN, "cloud"),
    ("mlx", "intent_classifier", "m-cloud", "http://localhost:8080", "cloud"),
    ("ollama", "intent_classifier", "qwen3:4b", "http://localhost:11434", "local"),
    ("openai_compatible", "intent_classifier", "m", "https://api.groq.com/openai/v1", "cloud"),
    ("ollama", "intent_classifier", "qwen3:4b", None, "unknown"),
])
def test_locality_rows(backend, component, model, endpoint, expected):
    assert classify.locality(backend, component, model, endpoint, resolver=_never) == expected


# ---------------------------------------------------------------------------
# D4 install class, release channel, deployment, platform
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env,channel,modules,expected", [
    ({"ATHENA_TELEMETRY_MODE": "production"}, "stable", [], "production"),
    ({"ATHENA_TELEMETRY_MODE": "production", "CI": "true"}, "stable", ["pytest"], "production"),
    ({"ATHENA_TELEMETRY_MODE": "self_hosted_real"}, "dev", ["pytest"], "self_hosted_real"),
    ({"ATHENA_TELEMETRY_MODE": "dev"}, "stable", [], "dev"),
    ({"ATHENA_TELEMETRY_MODE": "test"}, "stable", [], "test"),
    ({"ATHENA_TELEMETRY_MODE": "ci"}, "stable", [], "ci"),
    ({"CI": "true"}, "stable", [], "ci"),
    ({"CI": "1"}, "stable", [], "ci"),
    ({"CI": "false"}, "stable", [], "self_hosted_real"),
    ({"PYTEST_CURRENT_TEST": "x"}, "stable", [], "test"),
    ({}, "stable", ["pytest"], "test"),
    ({}, "dev", [], "dev"),
    ({}, "prerelease", [], "dev"),
    ({}, "stable", ["os", "sys"], "self_hosted_real"),
])
def test_install_class(env, channel, modules, expected):
    assert classify.install_class(env, channel, modules) == expected


def test_unknown_mode_warns_and_auto_detects():
    with capture_logs() as logs:
        assert classify.install_class({"ATHENA_TELEMETRY_MODE": "banana", "CI": "1"}, "stable", []) == "ci"
    assert [e for e in logs if e["log_level"] == "warning" and e["event"] == "telemetry_mode_unrecognized"]


@pytest.mark.parametrize("version,expected", [
    ("0.5.0", ("0.5.0", "stable")),
    ("0.6.0rc1", ("0.6.0rc1", "prerelease")),
    ("0.6.0a2", ("0.6.0a2", "prerelease")),
    ("0.6.0b1", ("0.6.0b1", "prerelease")),
    ("0.6.0dev3", ("0.6.0dev3", "dev")),
    ("0.5.0post1", ("0.5.0post1", "stable")),
    ("0.5.0-foo", ("0.5.0", "dev")),
    ("garbage", ("0.0.0", "dev")),
    ("", ("0.0.0", "dev")),
])
def test_release_channel(version, expected):
    assert classify.release_channel(version) == expected


@pytest.mark.parametrize("url,expected", [
    ("sqlite:///:memory:", True),
    ("sqlite://", True),
    ("sqlite:///file::memory:?cache=shared", True),
    ("sqlite:///x.db", False),
    ("postgresql://u:p@db:5432/athena", False),
    ("", True),
    (None, True),
])
def test_db_is_ephemeral(url, expected):
    assert classify.db_is_ephemeral(url) is expected


def test_deployment_shape():
    assert classify.deployment_shape({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, lambda p: False) == "kubernetes"
    assert classify.deployment_shape({}, lambda p: p == "/.dockerenv") == "container"
    assert classify.deployment_shape({}, lambda p: p == "/run/.containerenv") == "container"
    assert classify.deployment_shape({}, lambda p: False) == "bare"


@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", ("linux", "x86_64")),
    ("Linux", "amd64", ("linux", "x86_64")),
    ("Darwin", "arm64", ("darwin", "aarch64")),
    ("Linux", "aarch64", ("linux", "aarch64")),
    ("Windows", "AMD64", ("windows", "x86_64")),
    ("FreeBSD", "riscv64", ("other", "other")),
])
def test_platform_arch(system, machine, expected):
    assert classify.platform_arch(system, machine) == expected


def test_endpoint_origin():
    assert classify.endpoint_origin("https://Collector.Example.ORG/v1/ping") == "https://collector.example.org"
    assert classify.endpoint_origin("https://collector.example.org:443/v1/ping") == "https://collector.example.org"
    assert classify.endpoint_origin("http://127.0.0.1:8787/v1/ping") == "http://127.0.0.1:8787"
    assert classify.endpoint_origin("http://localhost:80/v1/ping") == "http://localhost"


# ---------------------------------------------------------------------------
# D3 enablement: precedence and parsing
# ---------------------------------------------------------------------------

def _reading(process=None, dotenv=None, error=None):
    return SimpleNamespace(process=dict(process or {}), dotenv=dict(dotenv or {}), dotenv_error=error)


def _state(process=None, dotenv=None, error=None, klass="self_hosted_real", ephemeral=False, admin=False):
    decision = classify.parse_telemetry_env(_reading(process, dotenv, error), DEFAULT)
    return classify.enable_state(decision, klass, ephemeral, admin)


RULES = [
    ("env_unreadable", dict(error="PermissionError")),
    ("env_athena_telemetry", dict(process={"ATHENA_TELEMETRY": "off"})),
    ("env_do_not_track", dict(process={"DO_NOT_TRACK": "1"})),
    ("endpoint_unset", dict(dotenv={"ATHENA_TELEMETRY_ENDPOINT": ""})),
    ("endpoint_invalid", dict(process={"ATHENA_TELEMETRY_ENDPOINT": "http://evil.example/v1/ping"})),
    ("install_class_ci", dict(klass="ci")),
    ("ephemeral_database", dict(ephemeral=True)),
    ("admin_setting", dict(admin=True)),
]


def _merge(*specs):
    out = {"process": {}, "dotenv": {}}
    for spec in specs:
        for key, value in spec.items():
            if key in ("process", "dotenv"):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    return out


@pytest.mark.parametrize("reason,spec", RULES)
def test_each_rule_alone(reason, spec):
    enabled, got, locked = _state(**_merge(spec))
    assert (enabled, got) == (False, reason)
    assert locked is (reason != "admin_setting")


def test_all_clear_is_on():
    assert _state() == (True, "enabled", False)


@pytest.mark.parametrize("i", range(len(RULES) - 1))
def test_pairwise_precedence(i):
    (first, a), (_second, b) = RULES[i], RULES[i + 1]
    enabled, got, _locked = _state(**_merge(a, b))
    assert (enabled, got) == (False, first)


def test_install_class_test_is_off():
    assert _state(klass="test")[:2] == (False, "install_class_test")


def test_explicit_mode_lifts_the_install_class_rule():
    assert _state(process={"ATHENA_TELEMETRY_MODE": "ci"}, klass="ci") == (True, "enabled", False)


def test_unknown_mode_is_not_explicit():
    assert _state(process={"ATHENA_TELEMETRY_MODE": "banana"}, klass="ci")[:2] == (False, "install_class_ci")


@pytest.mark.parametrize("value,expected", [
    (" off ", (False, "env_athena_telemetry")),
    ("No", (False, "env_athena_telemetry")),
    ("FALSE", (False, "env_athena_telemetry")),
    ("0", (False, "env_athena_telemetry")),
    ("disabled", (False, "env_athena_telemetry_unrecognized")),
    ("on", (True, "enabled")),
    ("TRUE", (True, "enabled")),
    ("yes", (True, "enabled")),
    ("1", (True, "enabled")),
    ("", (True, "enabled")),
])
def test_athena_telemetry_parsing(value, expected):
    assert _state(process={"ATHENA_TELEMETRY": value})[:2] == expected


def test_unrecognized_value_warns():
    decision = classify.parse_telemetry_env(_reading({"ATHENA_TELEMETRY": "disabled"}), DEFAULT)
    assert decision.off_reason == "env_athena_telemetry_unrecognized"
    assert decision.warnings


@pytest.mark.parametrize("value,expected", [
    ("0", (True, "enabled")),
    ("false", (True, "enabled")),
    ("no", (True, "enabled")),
    ("yes", (False, "env_do_not_track")),
    ("1", (False, "env_do_not_track")),
    ("2", (False, "env_do_not_track")),
    ("", (True, "enabled")),
])
def test_do_not_track_parsing(value, expected):
    assert _state(process={"DO_NOT_TRACK": value})[:2] == expected


@pytest.mark.parametrize("endpoint,expected", [
    ("http://evil.example/v1/ping", (False, "endpoint_invalid")),
    ("http://127.0.0.1:8787/v1/ping", (True, "enabled")),
    ("http://[::1]:8787/v1/ping", (True, "enabled")),
    ("http://localhost:8787/v1/ping", (True, "enabled")),
    ("https://u:p@x.example/v1/ping", (False, "endpoint_invalid")),
    ("https://x.example/v1/ping?q=1", (False, "endpoint_invalid")),
    ("https://x.example/v1/ping#frag", (False, "endpoint_invalid")),
    ("https://x.example/" + "a" * 200, (False, "endpoint_invalid")),
    ("ftp://x.example/v1/ping", (False, "endpoint_invalid")),
    ("https:///v1/ping", (False, "endpoint_invalid")),
    ("https://x.example/v1/ping", (True, "enabled")),
])
def test_endpoint_rows(endpoint, expected):
    assert _state(process={"ATHENA_TELEMETRY_ENDPOINT": endpoint})[:2] == expected


def test_endpoint_display_strips_userinfo():
    decision = classify.parse_telemetry_env(
        _reading({"ATHENA_TELEMETRY_ENDPOINT": "https://user:pw@evil.example/v1/ping"}), DEFAULT)
    assert decision.off_reason == "endpoint_invalid"
    assert decision.endpoint_display == "https://evil.example/v1/ping"
    assert "pw" not in decision.endpoint_display


def test_default_endpoint_used_when_unset():
    decision = classify.parse_telemetry_env(_reading(), DEFAULT)
    assert decision.endpoint == DEFAULT and decision.off_reason is None


def test_process_endpoint_wins_over_dotenv():
    decision = classify.parse_telemetry_env(
        _reading({"ATHENA_TELEMETRY_ENDPOINT": "https://a.example/p"},
                 {"ATHENA_TELEMETRY_ENDPOINT": "https://b.example/p"}), DEFAULT)
    assert decision.endpoint == "https://a.example/p"
    decision = classify.parse_telemetry_env(_reading({}, {"ATHENA_TELEMETRY_ENDPOINT": "https://b.example/p"}), DEFAULT)
    assert decision.endpoint == "https://b.example/p"


def test_opt_out_is_a_union_of_both_sources():
    assert _state(process={"ATHENA_TELEMETRY": "on"}, dotenv={"ATHENA_TELEMETRY": "off"})[:2] == \
        (False, "env_athena_telemetry")
    assert _state(process={"ATHENA_TELEMETRY": "", "DO_NOT_TRACK": ""}, dotenv={"DO_NOT_TRACK": "1"})[:2] == \
        (False, "env_do_not_track")
    assert _state(dotenv={"ATHENA_TELEMETRY_ENDPOINT": ""})[:2] == (False, "endpoint_unset")


def test_env_off_with_admin_setting_absent_is_env_locked():
    assert _state(process={"ATHENA_TELEMETRY": "off"}, admin=False) == (False, "env_athena_telemetry", True)


# ---------------------------------------------------------------------------
# Buckets (T11)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (0, "0"), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6-20"), (20, "6-20"), (21, "21-50"), (50, "21-50"),
    (51, "51-200"), (200, "51-200"), (201, "201-1000"), (1000, "201-1000"), (1001, "1001+"),
])
def test_bucket_count(n, expected):
    assert bucket_count(n) == expected


@pytest.mark.parametrize("c,expected", [
    (0, "0"), (29, "<1"), (30, "1-9"), (299, "1-9"), (300, "10-49"), (1499, "10-49"), (1500, "50-199"),
    (5999, "50-199"), (6000, "200+"),
])
def test_bucket_rate(c, expected):
    assert bucket_rate(c) == expected


def test_intent_mix_needs_a_hundred():
    assert intent_mix({"control": 99}) is None
    assert intent_mix({"control": 100}) == {"control": 100}


@pytest.mark.parametrize("counts,expected", [
    ({"control": 25, "weather": 75}, {"control": 30, "weather": 70}),
    ({"control": 35, "weather": 65}, {"control": 40, "weather": 60}),
    ({"control": 45, "weather": 55}, {"control": 50, "weather": 50}),
    ({"control": 45, "weather": 25, "other": 30}, {"control": 50, "weather": 30, "other": 20}),
    ({"control": 2, "weather": 98}, {"weather": 100}),
    ({"control": 2, "weather": 88, "sports": 10}, {"weather": 90, "sports": 10}),
    ({"control": 2, "other": 8, "weather": 90}, {"weather": 90, "other": 10}),
    ({"not_a_known_intent": 40, "control": 60}, {"control": 60, "other": 40}),
])
def test_intent_mix_rounding_and_remainder(counts, expected):
    assert intent_mix(counts) == expected


def test_intent_mix_composition_invariant():
    keys = ["control", "weather", "sports"]
    for total in range(100, 301, 5):
        for parts in (2, 3):
            for split in itertools.product(range(0, total + 1, 5), repeat=parts - 1):
                if sum(split) > total:
                    continue
                counts = dict(zip(keys, list(split) + [total - sum(split)]))
                counts = {k: v for k, v in counts.items() if v}
                mix = intent_mix(counts)
                assert mix is not None
                for value in mix.values():
                    assert value in INTENT_PERCENTS, (counts, mix)
                assert 90 <= sum(mix.values()) <= 110, (counts, mix)
