"""Pure classifiers for telemetry: install class, release channel, deployment
shape, enablement, locality, and the structured model fields. No I/O; every
input is injected so each rule is table-testable."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import structlog

from shared.config import resolve_endpoint, resolve_mode

from app.services.telemetry.schema import (
    KNOWN_CLOUD_LLM_HOSTS,
    LOCAL_HOST_NAMES,
    LOCAL_HOST_SUFFIXES,
    MODEL_FAMILIES,
    MOE_SIZE_RE,
    PUBLIC_HF_PUBLISHERS,
    QUANT_TOKEN_RE,
    SIZE_RE,
    TOKEN_SPLIT_RE,
    TOOL_CALLING_COMPONENTS,
    VERSION_RE,
    ModelFields,
)

logger = structlog.get_logger()

Resolver = Callable[[str], List[str]]

TRUE_WORDS = frozenset({"on", "true", "1", "yes"})
OFF_WORDS = frozenset({"off", "false", "0", "no"})
DNT_ON_WORDS = frozenset({"0", "false", "no"})
MODES = frozenset({"production", "self_hosted_real", "dev", "test", "ci"})
ENDPOINT_MAX_CHARS = 200
TELEMETRY_KEYS = ("ATHENA_TELEMETRY", "DO_NOT_TRACK", "ATHENA_TELEMETRY_ENDPOINT", "ATHENA_TELEMETRY_MODE")

_VERSION = re.compile(VERSION_RE)
_NUMERIC_CORE = re.compile(r"^(\d+\.\d+\.\d+)")
_VENDOR_PREFIXES = ("openai/", "anthropic/", "google/")
_OLLAMA_LIBRARY_PREFIX = "registry.ollama.ai/library/"
_FAMILY_BOUNDARY = "-_./"

# r3.1/r3.2: text after the matched family keeps source "ollama-library" only
# when every token is a size, quant, version or generic descriptor token.
_SIZE_TOKEN = re.compile(r"^(\d+(\.\d+)?[bm]|\d+x\d+(\.\d+)?b|[ae]\d+(\.\d+)?b)$")
_QUANT_SUFFIX_TOKEN = re.compile(r"^(k|m|s|l|xs|xl|xxs|f16|fp16|bf16|f32|fp32)$")
_VERSION_TOKEN = re.compile(r"^v?\d+$")
_DESCRIPTOR_TOKENS = frozenset({
    "latest", "instruct", "chat", "it", "base", "text", "cloud", "vision", "coder", "code", "thinking",
    "preview", "reasoning", "embed", "mini", "nano", "small", "medium", "large", "turbo", "tools",
})


@dataclass(frozen=True)
class EnvDecision:
    """The env-derived half of enablement (D3 rules 0-4) plus the resolved
    endpoint and mode. ``off_reason`` is None when rules 0-4 all pass;
    ``mode`` is None unless ATHENA_TELEMETRY_MODE is set to a known value."""
    off_reason: Optional[str]
    endpoint: str
    endpoint_display: str
    mode: Optional[str]
    warnings: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Version, install class, deployment
# ---------------------------------------------------------------------------

def release_channel(version: str) -> Tuple[str, str]:
    """(reported version, channel). A version outside the D4 grammar reports
    its numeric core (or 0.0.0) on the dev channel."""
    version = (version or "").strip()
    if not _VERSION.match(version):
        core = _NUMERIC_CORE.match(version)
        return (core.group(1) if core else "0.0.0"), "dev"
    suffix = version[len(_NUMERIC_CORE.match(version).group(1)):]
    if suffix.startswith("dev"):
        return version, "dev"
    if suffix[:1] in ("r", "a", "b"):
        return version, "prerelease"
    return version, "stable"


def install_class(env: Mapping[str, str], channel: str, loaded_modules: Iterable[str]) -> str:
    mode = (env.get("ATHENA_TELEMETRY_MODE") or "").strip().lower()
    if mode in MODES:
        return mode
    if mode:
        logger.warning("telemetry_mode_unrecognized", message="ATHENA_TELEMETRY_MODE is not one of "
                       + ", ".join(sorted(MODES)) + "; detecting the install class instead")
    if (env.get("CI") or "").strip().lower() in ("1", "true"):
        return "ci"
    if env.get("PYTEST_CURRENT_TEST") or "pytest" in loaded_modules:
        return "test"
    if channel != "stable":
        return "dev"
    return "self_hosted_real"


def db_is_ephemeral(url: Optional[str]) -> bool:
    """True for an in-memory SQLite database (every DEV_MODE instance) or no
    database URL at all: an identity there can't outlive the process."""
    url = (url or "").strip()
    if not url:
        return True
    if not url.lower().startswith("sqlite"):
        return False
    rest = url.split("://", 1)[1] if "://" in url else ""
    return rest in ("", "/") or ":memory:" in rest or "mode=memory" in rest


def deployment_shape(env: Mapping[str, str], exists: Callable[[str], bool]) -> str:
    if env.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if exists("/.dockerenv") or exists("/run/.containerenv"):
        return "container"
    return "bare"


def platform_arch(system: str, machine: str) -> Tuple[str, str]:
    system = (system or "").lower()
    machine = (machine or "").lower()
    plat = system if system in ("linux", "darwin", "windows") else "other"
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        arch = "other"
    return plat, arch


# ---------------------------------------------------------------------------
# Endpoints and hosts
# ---------------------------------------------------------------------------

def _normalize_host(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def endpoint_host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    return _normalize_host(host)


def endpoint_origin(url: str) -> str:
    """``scheme://host[:port]``, lowercased, default ports elided (D21)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is None or (scheme, port) in (("https", 443), ("http", 80)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def display_endpoint(url: str) -> str:
    """The endpoint as it may be shown or logged: userinfo stripped,
    truncated to 200 characters."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            netloc = parts.netloc.rsplit("@", 1)[1]
            url = parts._replace(netloc=netloc).geturl()
    except ValueError:
        url = re.sub(r"//[^/@]*@", "//", url)
    return url[:ENDPOINT_MAX_CHARS]


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint_valid(url: str) -> bool:
    if len(url) > ENDPOINT_MAX_CHARS or "?" in url or "#" in url:
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    if not host or "@" in parts.netloc:
        return False
    scheme = parts.scheme.lower()
    if scheme == "https":
        return True
    return scheme == "http" and _is_loopback_host(_normalize_host(host))


def _label_match(host: str, entry: str) -> bool:
    return host == entry or host.endswith("." + entry)


def _address_is_local(address: str) -> Optional[bool]:
    try:
        return not ipaddress.ip_address(_normalize_host(address.split("%", 1)[0])).is_global
    except ValueError:
        return None


def classify_host(host: str, resolver: Optional[Resolver]) -> str:
    """D10 rule 3 for one endpoint host."""
    host = _normalize_host(host or "")
    if not host:
        return "unknown"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return "remote" if literal.is_global else "local"
    if host in LOCAL_HOST_NAMES or "." not in host or any(_label_match(host, s) for s in LOCAL_HOST_SUFFIXES):
        return "local"
    if any(_label_match(host, entry) for entry in KNOWN_CLOUD_LLM_HOSTS):
        return "cloud"
    if _label_match(host, "amazonaws.com") and host.split(".", 1)[0] == "bedrock-runtime":
        return "cloud"
    if resolver is None:
        return "unknown"
    try:
        addresses = list(resolver(host) or [])
    except Exception:
        return "unknown"
    verdicts = [_address_is_local(a) for a in addresses]
    if not verdicts or any(v is None for v in verdicts):
        return "unknown"
    return "local" if all(verdicts) else "remote"


def _is_cloud_tag(raw: str) -> bool:
    if raw.endswith("-cloud"):
        return True
    if ":" in raw:
        tag = raw.rsplit(":", 1)[1]
        return tag == "cloud" or tag.endswith("-cloud")
    return False


def locality(backend: str, component: str, raw_model: Optional[str], endpoint_url: Optional[str],
             resolver: Optional[Resolver]) -> str:
    """D10 (CB3): computed on the raw model name and the resolved endpoint."""
    raw = (raw_model or "").strip().lower()
    if _is_cloud_tag(raw):
        return "cloud"
    if backend in ("anthropic", "google"):
        return "cloud"
    if backend == "openai":
        if component in TOOL_CALLING_COMPONENTS and not raw.startswith("openai/") and endpoint_url:
            host = endpoint_host(endpoint_url)
            return classify_host(host, resolver) if host else "unknown"
        return "cloud"
    host = endpoint_host(endpoint_url)
    if not host:
        return "unknown"
    return classify_host(host, resolver)


# ---------------------------------------------------------------------------
# D42 model fields
# ---------------------------------------------------------------------------

def _match_family(base: str) -> Tuple[Optional[str], str]:
    """(longest family F with base == F or base starting F+boundary, the
    text after F)."""
    best = None
    for family in MODEL_FAMILIES:
        if base == family or (base.startswith(family) and base[len(family)] in _FAMILY_BOUNDARY):
            if best is None or len(family) > len(best):
                best = family
    if best is None:
        return None, ""
    return best, base[len(best):]


def _size_bucket(raw: str) -> str:
    sizes = []
    for number, unit in SIZE_RE.findall(raw):
        value = float(number)
        sizes.append(value / 1000.0 if unit == "m" else value)
    for experts, each in MOE_SIZE_RE.findall(raw):
        sizes.append(int(experts) * float(each))
    if not sizes:
        return "unknown"
    s = max(sizes)
    if s <= 3.5:
        return "le3b"
    if s < 9.5:
        return "4-9b"
    if s < 20.5:
        return "10-20b"
    if s < 40.5:
        return "21-40b"
    if s < 80.5:
        return "41-80b"
    return "gt80b"


def _quantized(raw: str) -> bool:
    return any(QUANT_TOKEN_RE.match(t) for t in TOKEN_SPLIT_RE.split(raw) if t)


def _recognized_trailer(text: str) -> bool:
    for token in TOKEN_SPLIT_RE.split(text):
        if not token:
            continue
        if (_SIZE_TOKEN.match(token) or QUANT_TOKEN_RE.match(token) or _QUANT_SUFFIX_TOKEN.match(token)
                or _VERSION_TOKEN.match(token) or token in _DESCRIPTOR_TOKENS):
            continue
        return False
    return True


def classify_model(raw: Optional[str], backend: str, locality_value: str) -> ModelFields:
    """D42 + r3.2: only the family (from a curated public list), a size
    bucket, a quantized flag and a source ever leave; the raw name doesn't."""
    s = (raw or "").strip().lower()
    size = _size_bucket(s)
    quant = _quantized(s)

    def fields(family: Optional[str], source: str) -> ModelFields:
        return ModelFields(family=family or "custom", size_bucket=size, quantized=quant, source=source)

    if not s:
        return fields(None, "custom")

    source = None
    base = s
    for prefix in _VENDOR_PREFIXES:
        if s.startswith(prefix):
            source, base = "vendor-api", s[len(prefix):]
            break
    if source is None and (backend in ("anthropic", "google") or (backend == "openai" and locality_value == "cloud")):
        source = "vendor-api"
    if source is None and base.startswith(_OLLAMA_LIBRARY_PREFIX):
        base = base[len(_OLLAMA_LIBRARY_PREFIX):]
    if source is None and "/" in base:
        segments = base.split("/")
        first = segments[0]
        if first in ("hf.co", "huggingface.co"):
            org = segments[1] if len(segments) > 1 else ""
            repo = segments[2] if len(segments) > 2 else ""
            if org in PUBLIC_HF_PUBLISHERS and repo:
                family, _rest = _match_family(repo.split(":", 1)[0])
                return fields(family, "hf-public-publisher")
            return fields(None, "custom")
        if "." in first or ":" in first or first == "localhost":
            return fields(None, "custom-registry")
        return fields(None, "custom")

    if source == "vendor-api":
        family, _rest = _match_family(base.split(":", 1)[0])
        return fields(family, "vendor-api")

    name, _, tag = base.partition(":")
    family, rest = _match_family(name)
    if family is None:
        return fields(None, "custom")
    if _recognized_trailer(rest) and _recognized_trailer(tag):
        return fields(family, "ollama-library")
    return fields(family, "custom")


# ---------------------------------------------------------------------------
# D3 enablement
# ---------------------------------------------------------------------------

def _values(reading, key: str) -> List[str]:
    out = []
    for source in (reading.process, reading.dotenv):
        value = source.get(key)
        if value is not None:
            out.append(str(value))
    return out


def parse_telemetry_env(reading, default_endpoint: str) -> EnvDecision:
    """D3 rules 0-4 over a TelemetryEnvReading (duck-typed: ``process``,
    ``dotenv``, ``dotenv_error``). Opt-outs are a fail-closed union of the
    two sources; endpoint and mode prefer a non-empty process value."""
    warnings: List[str] = []
    endpoint = resolve_endpoint(reading, default_endpoint)
    mode_raw = resolve_mode(reading)
    mode = mode_raw if mode_raw in MODES else None
    if mode_raw and mode is None:
        warnings.append("ATHENA_TELEMETRY_MODE is not a known install class; it is ignored")

    def decision(reason: Optional[str], shown: Optional[str] = None) -> EnvDecision:
        return EnvDecision(off_reason=reason, endpoint=endpoint,
                           endpoint_display=display_endpoint(shown if shown is not None else endpoint),
                           mode=mode, warnings=tuple(warnings))

    if reading.dotenv_error:
        warnings.append(f".env could not be read ({reading.dotenv_error}); telemetry is off")
        return decision("env_unreadable")

    unrecognized = False
    for value in _values(reading, "ATHENA_TELEMETRY"):
        word = value.strip().lower()
        if not word or word in TRUE_WORDS:
            continue
        if word in OFF_WORDS:
            return decision("env_athena_telemetry")
        unrecognized = True
    if unrecognized:
        warnings.append("ATHENA_TELEMETRY has an unrecognized value; telemetry is off")
        return decision("env_athena_telemetry_unrecognized")

    for value in _values(reading, "DO_NOT_TRACK"):
        word = value.strip().lower()
        if word and word not in DNT_ON_WORDS:
            return decision("env_do_not_track")

    if any(not v.strip() for v in _values(reading, "ATHENA_TELEMETRY_ENDPOINT")):
        return decision("endpoint_unset", shown="")

    if not _endpoint_valid(endpoint):
        return decision("endpoint_invalid")
    return decision(None)


def enable_state(decision: EnvDecision, install_class_value: str, db_ephemeral: bool,
                 admin_disabled: bool) -> Tuple[bool, str, bool]:
    """(enabled, reason, env_locked). Rules 0-6 are env-locked: the Admin UI
    toggle can't override them."""
    if decision.off_reason:
        return False, decision.off_reason, True
    if install_class_value in ("ci", "test") and decision.mode is None:
        return False, f"install_class_{install_class_value}", True
    if db_ephemeral:
        return False, "ephemeral_database", True
    if admin_disabled:
        return False, "admin_setting", False
    return True, "enabled", False
