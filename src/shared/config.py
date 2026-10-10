"""Canonical configuration for Project Athena.

Single source of truth for environment-variable-driven configuration across
every Athena service.  Backed by ``pydantic_settings.BaseSettings`` so that
type coercion, ``.env`` file loading, and validation come for free.

Resolution order (per pydantic-settings default):
    1. Constructor kwargs (used by tests)
    2. Environment variables (case-insensitive — ``OLLAMA_URL`` populates
       the ``ollama_url`` field automatically because pydantic-settings
       auto-uppercases snake_case field names)
    3. ``.env`` file (if present in CWD; disabled in tests via ``_TestConfig``)
    4. Field defaults

Lazy single-instantiation pattern
----------------------------------
``AthenaConfig`` is instantiated exactly once per process via the
``@lru_cache``-decorated ``get_config()`` factory.  This mirrors the
``get_admin_url()`` pattern from ``src/shared/admin_url.py`` and avoids
import-order traps where module-level eager instantiation would read env
vars before test fixtures have set them.

Tests can reset the cache with ``_clear_cache_for_tests()``.

Currently-modelled fields (Campaign 4 + ATHENA-11)
---------------------------------------------------
Fields below cover 12 env vars: 11 migrated in Campaign 4, plus
``CONTROL_AGENT_ENABLED`` added in ATHENA-11 Phase 6.  The
remaining ~220 env vars in the codebase are migrated per-PR as those areas
are touched — see ``CONTRIBUTING.md`` for the extension pattern.

LLM endpoint resolution
------------------------
``LLM_SERVICE_URL`` and ``OLLAMA_URL`` are loaded as two distinct fields.
The ``llm_endpoint`` computed property exposes the dominant precedence
``LLM_SERVICE_URL > OLLAMA_URL > default``.  Sites that historically read
only ``OLLAMA_URL`` should use ``ollama_url``; sites that read the paired
``LLM_SERVICE_URL or OLLAMA_URL`` expression should use ``llm_endpoint``.

admin_url — NOT env-loadable (Campaign 4 round-2 R2-C1)
----------------------------------------------------------
``admin_url`` is a ``@computed_field`` ``@property``, NOT a settings field.
Setting ``ADMIN_URL`` in the environment has zero effect on this attribute.
Resolution always delegates to ``get_admin_url()`` (Campaign 3 helper), which
is the canonical single source of truth for admin-backend URL resolution.

OSS-First
----------
All defaults match the existing ``os.getenv("FOO", default)`` resolutions in
the migrated call sites.  No field is required.  Empty values are returned
as-is; per-service code is responsible for validating critical inputs.
"""
from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional

from dotenv import dotenv_values

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.admin_url import get_admin_url

logger = logging.getLogger(__name__)

_FANOUT_CONFIRM_THRESHOLD_DEFAULT = 6
_FANOUT_HARD_LIMIT_DEFAULT = 18


class AthenaConfig(BaseSettings):
    """Centralized Athena configuration backed by pydantic-settings.

    See module docstring for the full resolution order and OSS-First contract.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # case_sensitive intentionally NOT set (default False).
        # pydantic-settings auto-uppercases snake_case field names when matching
        # env vars, so ``OLLAMA_URL`` populates ``ollama_url``, ``DEV_MODE``
        # populates ``dev_mode``, etc.  Setting case_sensitive=True would require
        # an explicit Field(validation_alias=...) on every field.
        extra="ignore",  # ignore env vars we don't model yet; don't crash
    )

    # ------------------------------------------------------------------
    # admin_url — Campaign 3 helper (NOT a settings field)
    # ------------------------------------------------------------------
    # Round-2 R2-C1: must NOT be a settings Field so that pydantic-settings
    # cannot load it from an ADMIN_URL env var.  Computed at access time
    # and always resolved via the canonical Campaign 3 helper.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_url(self) -> str:
        """Admin-backend URL, always resolved via the Campaign 3 helper.

        NOT env-loadable.  ``ADMIN_URL`` env var has no effect here.
        Set ``ADMIN_API_URL`` (or the other vars the helper checks) to
        influence the resolved value.  See ``src/shared/admin_url.py``
        for the full six-priority resolution chain.
        """
        return get_admin_url()

    # ------------------------------------------------------------------
    # LLM endpoints
    # ------------------------------------------------------------------
    ollama_url: str = Field(default="http://localhost:11434")
    llm_service_url: str = Field(default="")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_endpoint(self) -> str:
        """LLM endpoint with dominant precedence: LLM_SERVICE_URL > OLLAMA_URL > default.

        Use this at sites that previously had the paired expression
        ``LLM_SERVICE_URL or OLLAMA_URL`` (with OLLAMA_URL as the fallback).

        Sites that previously read only ``OLLAMA_URL`` (e.g. ``ollama_client.py``)
        should use ``ollama_url`` directly — they never consulted ``LLM_SERVICE_URL``
        and their behavior should be preserved.
        """
        return self.llm_service_url or self.ollama_url

    # ------------------------------------------------------------------
    # Cache / data store
    # ------------------------------------------------------------------
    # redis_url default targets the in-cluster DNS shortname (matches
    # admin/backend/main.py:109 production branch and manifests/athena-prod/
    # config.yaml).  Local-dev users should set REDIS_URL=redis://localhost:6379
    # in their .env file — see .env.example.  (Round-2 R2-H4.)
    redis_url: str = Field(default="redis://redis:6379/0")
    database_url: str = Field(default="")

    # ------------------------------------------------------------------
    # Service-to-service auth
    # ------------------------------------------------------------------
    service_api_key: str = Field(default="")

    # ------------------------------------------------------------------
    # Personalization
    # ------------------------------------------------------------------
    default_timezone: str = Field(default="UTC")
    default_city: str = Field(default="")

    # ------------------------------------------------------------------
    # OIDC — initial-load values only
    # ------------------------------------------------------------------
    # NOTE: runtime mutation of the OIDC module-level globals (oidc.py:48)
    # via configure_oauth_client() is unchanged.  These fields capture the
    # env-var values at service startup (the same slot that was previously
    # a module-level direct env read for OIDC_ISSUER).
    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")
    # Opt-out escape valve for deployers whose IdP returns a deliberately
    # mismatched issuer URL.  Default true (validation active).  When false,
    # only branch (d) of _enforce_oidc_runtime_gates() is softened to a
    # warning; branches (a), (b), (c) and the early env gate remain fatal.
    # Setting OIDC_VALIDATE_ISS=false also passes claims_options={"iss":
    # {"essential": False}} to authlib at the callback so the relaxation is
    # coherent end-to-end.  State is exposed on GET /api/auth/methods.
    oidc_validate_iss: bool = Field(default=True)

    @field_validator("oidc_issuer", "oidc_client_id", mode="before")
    @classmethod
    def _strip_oidc_whitespace(cls, v: object) -> object:
        """Strip surrounding whitespace from OIDC string values.

        Existing call sites (e.g. ``admin/backend/main.py:261, 249``) all
        call ``.strip()`` on the env-var value.  Centralizing the strip here
        removes that burden from every caller.
        """
        if isinstance(v, str):
            return v.strip()
        return v

    # ------------------------------------------------------------------
    # Mode flags
    # ------------------------------------------------------------------
    dev_mode: bool = Field(default=False)
    demo_mode: bool = Field(default=False)

    # ------------------------------------------------------------------
    # Control Agent — opt-in feature flag (Campaign 1 / ATHENA-11)
    # ------------------------------------------------------------------
    # When False (default), all admin-backend and orchestrator call sites
    # that talk to the Control Agent short-circuit with a per-endpoint
    # "disabled" response that matches each route's existing typed contract.
    # Set CONTROL_AGENT_ENABLED=true in your env only if you actually run
    # a Control Agent on a host (e.g., an Apple Silicon Mac alongside Ollama).
    # Valid values: true / false / 1 / 0. Do not set to a blank string.
    control_agent_enabled: bool = Field(default=False)
    # control_agent_callback_base_url: where the Control Agent's host reaches
    # admin-backend (scheme://host[:port], no path), e.g.
    # "https://admin.example.org". admin-backend hands it to the Control
    # Agent as the base of the download-progress callback, and the agent
    # sends its service key there only when that host is an entry of the
    # agent's own ALLOWED_CALLBACK_HOSTS. Empty (the default): admin-backend
    # falls back to its own loopback address and logs a warning, which is
    # only right when the agent runs on admin-backend's host.
    control_agent_callback_base_url: str = Field(default="")

    # ------------------------------------------------------------------
    # Service Control on Kubernetes — opt-in feature flag (ATHENA-118)
    # ------------------------------------------------------------------
    # When False (default), admin-backend never touches the filesystem for
    # a ServiceAccount token and the Kubernetes manager always resolves to
    # unavailable. Set SERVICE_CONTROL_K8S_ENABLED=true only after applying
    # manifests/athena-prod/optional/admin-backend-rbac.yaml and the
    # automount patch file (see docs/CONFIGURATION.md § Service Control on
    # Kubernetes) — the Role there grants ONLY `deployments` get/list and
    # `deployments/scale` get/patch on a fixed, name-scoped Deployment list;
    # there is no `deployments` patch (pod-template write), so this flag can
    # never grant code execution or secret access beyond what `get`/`list`
    # already exposes (Deployment specs, not Secret values).
    service_control_k8s_enabled: bool = Field(default=False)

    # ------------------------------------------------------------------
    # Local-login rate limiting + per-account lockout (Campaign 3 / ATHENA-14)
    # ------------------------------------------------------------------
    # login_rate_limit_per_minute: max POST /local-login attempts per IP per 60 s
    #   (fastapi-limiter token bucket, Redis-backed).
    # login_lockout_threshold: cumulative failures before the account is locked.
    # login_lockout_minutes: how long an account stays locked after threshold.
    # login_minimum_delay_ms: floor wall-time on every failure path (user-not-found,
    #   inactive, wrong-password, locked) to equalise timing and prevent enumeration.
    #   Default 400 ms: PBKDF2-600k natural cost is 150–400 ms; the floor ensures
    #   constant-time behaviour when hardware is fast (xander:38 / codex-r1 polish).
    login_rate_limit_per_minute: int = Field(default=5)
    login_lockout_threshold: int = Field(default=10)
    login_lockout_minutes: int = Field(default=30)
    login_minimum_delay_ms: int = Field(default=400)

    # service_registry_write_per_minute: separate rate-limit budget for
    # POST/toggle/refresh/DELETE on /api/service-registry/services/*.
    # Distinct bucket from login_rate_limit_per_minute so service-registry writes
    # and login attempts do not contend for the same token pool.
    # Default 60 (generous for the CA's startup upsert loop).
    # (xander HIGH-4 / ATHENA-1 Phase 2)
    service_registry_write_per_minute: int = Field(default=60)

    # service_registry_endpoint_url: explicit endpoint_url override for
    # shared.service_registry.register_service()'s self-registration POST
    # (xander diff-review Critical, 2026-09-28). Empty (default): the
    # client omits endpoint_url entirely -- the upsert route then leaves
    # an existing row's host/port/protocol untouched (ATHENA-109 partial-
    # update semantics), which is what every real deployment wants, since
    # the correct in-cluster host is the seeded K8s Service DNS name
    # (ATHENA-119's OSS_SERVICE_REGISTRY), never this process's own
    # "http://localhost:<port>" view of itself. Set only for a deployment
    # topology where the self-reported endpoint genuinely IS authoritative
    # (e.g. a bare-metal dev RAG service that was never seeded).
    service_registry_endpoint_url: str = Field(default="")

    # service_registry_name: overrides the connector-name normalisation
    # shared.service_registry.to_rag_registry_name()/to_rag_host_label()
    # apply by default (lower-case, strip "-"/"_", then the "-rag"/
    # "athena-rag-" convention) for a deployer whose seeded row doesn't
    # follow that convention. When set, it IS the base used to build both
    # the registry `name` and the `host_label` -- no further normalisation
    # is applied to it. Empty (default): derive the base from the
    # connector's own service_name as before.
    service_registry_name: str = Field(default="")

    # ------------------------------------------------------------------
    # Health poller (ATHENA-1 Phase 4)
    # ------------------------------------------------------------------
    # health_poll_interval_seconds: how often the background task polls all
    #   enabled services. Default 30s.
    # health_poll_timeout_seconds: per-request HTTP timeout for each /health
    #   ping. Default 5s.
    # health_poll_concurrency: max simultaneous outbound pings per cycle
    #   (asyncio.Semaphore). Default 8.
    # health_poll_allowed_private_hosts: comma-separated CIDRs or hostnames
    #   that override the RFC1918/loopback/ULA block. Empty default = fail-
    #   closed for OSS deployers. Example with a host subnet: 192.0.2.0/24 (or the
    #   specific pod/service CIDR) so the poller can reach in-cluster services.
    #   Example for K8s: "10.96.0.0/12,10.244.0.0/16"
    #   To allow loopback (local-dev only): add "127.0.0.0/8" to this list.
    #   health_poll_allow_loopback was removed in Phase 4 reconcile (ATHENA-1)
    #   because it was defined but never consumed — it was a silent no-op.
    health_poll_interval_seconds: int = Field(default=30)
    health_poll_timeout_seconds: int = Field(default=5)
    health_poll_concurrency: int = Field(default=8)
    health_poll_allowed_private_hosts: str = Field(default='')

    # ------------------------------------------------------------------
    # SSRF guard — user/admin-supplied URL fetch surfaces (ATHENA-59)
    # ------------------------------------------------------------------
    # sitescraper_allowed_private_hosts: comma-separated CIDRs or hostnames
    #   that override the RFC1918/loopback/ULA block for the sitescraper,
    #   fetch_ical_data, and ContentFetcher fetch surfaces. Default empty =
    #   fail-closed for OSS deployers (correct posture).
    #
    #   ⚠️  WIDE-CIDR WARNING (xander H-3): a wide CIDR entry (e.g.
    #   ``10.0.0.0/8`` or ``192.168.0.0/16``) re-opens the entire RFC1918
    #   range to SSRF and defeats the guard. Allowlist entries MUST be
    #   specific hosts or the tightest possible prefix (ideally /32 or a
    #   narrow /24). Avoid entries wider than /24 — they provide near-zero
    #   security benefit and a wide attack surface.
    #
    #   This allowlist is passed INTO ``validate_url_not_private`` per-call
    #   (D9); the validator never reads env directly.
    #   health_poller keeps its own separate ``HEALTH_POLL_ALLOWED_PRIVATE_HOSTS``
    #   — the two allowlists are independent and never merged.
    sitescraper_allowed_private_hosts: str = Field(default='')

    # content_fetcher_allow_browser_fetch: when False (default), the Playwright
    #   headless-browser fallback in ContentFetcher is disabled for
    #   user/admin-supplied URLs (e.g. sitescraper targets). This is the safe
    #   default because Playwright handles redirects internally and cannot be
    #   SSRF-guarded at the URL-validator layer until network egress policy
    #   lands. Set CONTENT_FETCHER_ALLOW_BROWSER_FETCH=true only if you need
    #   JS-rendered content AND accept the documented Playwright SSRF residual
    #   (R3, ATHENA-59): Playwright navigation/subresource SSRF remains
    #   unguarded at both app and network layers under flannel.
    content_fetcher_allow_browser_fetch: bool = Field(default=False)

    # ------------------------------------------------------------------
    # OpenAI-compatible session lifecycle (ATHENA-88 / F88)
    # ------------------------------------------------------------------
    # session_max_count: cap on concurrent per-conversation OpenAI sessions
    #   (both the in-memory fallback dict and the Redis creation-time
    #   index). Once exceeded, the oldest session is evicted via
    #   SessionManager.delete_session. Default 5000.
    # new_conversation_per_minute_per_ip: gateway-side sliding-window limit
    #   on *new* conversations (first-turn requests with no explicit
    #   session_id) per client IP, applied to /v1/chat/completions and
    #   /v1/responses. Default 120 (see F39 note below the field for why).
    session_max_count: int = Field(default=5000, ge=100)
    # new_conversation_per_minute_per_ip: F39 (codex r2 Medium) — raised
    # from 30 to 120. Behind Traefik, every HA satellite and Jarvis caller
    # can share one resolved key (trusted_proxy_cidrs below), so the old
    # per-source default of 30/min was really a whole-house budget; 120
    # gives normal multi-room household traffic headroom while still
    # bounding abuse.
    new_conversation_per_minute_per_ip: int = Field(default=120, ge=1)
    # trusted_proxy_cidrs: F39 — comma-separated CIDRs/hosts. The gateway's
    # new-conversation limiter trusts X-Forwarded-For's original-client
    # address only when the immediate TCP peer falls inside one of these
    # ranges (a reverse proxy's pod CIDR, typically), so an untrusted caller
    # can't spoof another source's rate-limit key via that header. Same
    # allowlist-by-CIDR pattern as health_poll_allowed_private_hosts /
    # sitescraper_allowed_private_hosts. Empty (default) means every
    # caller's peer address is used directly -- correct for OSS deployers
    # with no reverse proxy in front of the gateway, but a shared limiter
    # bucket for everyone behind one (the gateway logs
    # trusted_proxy_cidrs_unset once at startup as a nudge to set this).
    # Example for a flannel/kubeadm-default cluster's pod CIDR:
    # "10.244.0.0/16".
    # F44 (codex r2b Medium, reconciliation round 2): resolve_client_key
    # parses X-Forwarded-For right-to-left and returns the nearest hop NOT
    # in this CIDR set, so a trusted proxy that appends rather than
    # overwrites the header can't be used to smuggle an attacker-forged
    # left-most value. Keep this scoped to the actual reverse-proxy
    # subnet, not a broad cluster-wide default.
    trusted_proxy_cidrs: str = Field(default="")
    # openai_speech_client_networks: comma-separated CIDRs/addresses of OpenAI-
    # compatible clients (/v1/chat/completions, /v1/responses) whose answers are
    # spoken by a TTS that isn't Athena's, e.g. a Home Assistant conversation
    # agent with HA-side TTS. Such a caller gets speech-normalized text ("25
    # miles per hour"); every other caller gets text as written. The client
    # address is the one the new-conversation limiter uses (nearest
    # X-Forwarded-For hop outside trusted_proxy_cidrs), so set
    # trusted_proxy_cidrs too when the gateway sits behind a proxy. An entry
    # that overlaps trusted_proxy_cidrs is ignored with an ERROR. Empty
    # (default): no network rule; the /v1/voice routes still select speech.
    openai_speech_client_networks: str = Field(default="")
    # new_conversation_reset_grace_seconds: F38 (codex r2 Medium) — a
    # first-turn fingerprint reset is skipped when a session under the same
    # fingerprint was created within this many seconds. HA's known truncated
    # ASR retry path resends the same single-user-message opener; without
    # this grace window, resolve_openai_session's is_first_turn (message
    # count only) can't distinguish that retry from a genuinely new
    # conversation, and the reset fragments (or wipes, if the truncated text
    # happens to match the opener) a still-live conversation.
    new_conversation_reset_grace_seconds: int = Field(default=120, ge=0)
    # orchestrator_ingress_auth: D10. "enforce" (default) requires
    # X-Service-Key on the gated orchestrator routes (query/stream/session
    # routes); a wrong key is always 401, including in DEV_MODE. "warn"
    # logs orchestrator_unauthenticated_request and allows the request
    # through -- for finding callers a header rollout missed before
    # switching to enforce. Any other value behaves as "enforce" and logs
    # an ERROR once (invalid config, not a silent fallback).
    orchestrator_ingress_auth: str = Field(default="enforce")

    # ------------------------------------------------------------------
    # Optional third-party integrations (ATHENA-89 / D7)
    # ------------------------------------------------------------------
    # music_assistant_url: base URL for a Music Assistant instance (e.g.
    #   http://music-assistant.local:8095). Empty (default) means Music
    #   Assistant isn't configured; consumers must not fall back to a
    #   hardcoded host. The admin-stored MusicConfig.music_assistant_url
    #   column takes precedence over this env default when set.
    music_assistant_url: str = Field(default="")
    # searxng_base_url: base URL for a self-hosted SearXNG instance. Empty
    #   (default) means the SearXNG search provider is disabled and its
    #   admin status reads "not configured" with no network probe made.
    searxng_base_url: str = Field(default="")
    # jarvis_web_url: jarvis-web API base for the appliance/sensor/media
    #   lookups in smart_home_controller.py (ATHENA-128 3.5). Empty
    #   (default) means those lookups are skipped and each site takes its
    #   existing error/unavailable branch -- the effective in-cluster
    #   behaviour today, since the previous hardcoded localhost:3001 isn't
    #   reachable from the orchestrator pod. Example: http://jarvis-web:3001.
    jarvis_web_url: str = Field(default="")

    # ------------------------------------------------------------------
    # Region-configurable RAG services (ATHENA-89 / D2, D3)
    # ------------------------------------------------------------------
    # These are consumer-parsed JSON/plain strings, not typed dict/list
    # fields — a malformed value must only make the *consuming service*
    # report configured=False, never raise inside AthenaConfig() itself
    # (every RAG deployment shares one ConfigMap via envFrom, so a typed
    # field that failed validation here would crash-loop every pod, not
    # just the one that reads it). Same pattern as
    # health_poll_allowed_private_hosts / sitescraper_allowed_private_hosts.
    #
    # transit_region_name: a display label for the configured transit
    #   region (e.g. "Denver Metro"). Purely cosmetic; empty is fine.
    transit_region_name: str = Field(default="")
    # transit_gtfs_feeds: JSON object of {feed_id: {name, agency, url, type,
    #   free, description?, bounds?, max_bytes?, allow_private?}}. See
    #   .env.example for the schema and src/rag/transportation/main.py's
    #   load_transit_config for validation rules. Empty means no GTFS feeds
    #   are configured.
    transit_gtfs_feeds: str = Field(default="")
    # transit_static_services: JSON object of non-GTFS transit services
    #   (e.g. a ferry) with fixed schedules: {service_id: {name, type, free,
    #   hours, frequency_minutes, stops, agency_name?, description?}}. Empty
    #   means none are configured.
    transit_static_services: str = Field(default="")
    # community_events_sources: JSON array of community-event source
    #   definitions (name, type, url, and per-type fields — see
    #   .env.example and src/rag/community_events/main.py's
    #   load_event_sources). Empty means the service reports not configured.
    community_events_sources: str = Field(default="")
    # default_amtrak_station: a 3-letter Amtrak station code used as the
    #   default origin when a caller doesn't specify one. Empty means the
    #   amtrak service requires an explicit origin on every request.
    default_amtrak_station: str = Field(default="")
    # ha_satellite_room_map: JSON object mapping a Home Assistant
    #   assist_satellite entity ID to the room it's physically located in
    #   ({"assist_satellite.<id>": "<room>", ...}). Consumed by the gateway
    #   (parsed lazily, not at import time — see _get_ha_satellite_room_map
    #   in src/gateway/main.py) for both directions: entity->room (active-
    #   satellite room detection) and its inverse, room->entity (satellite
    #   announcements). Empty means satellite-based room detection and
    #   announcements are both disabled (OSS-First — this deployment's HA
    #   setup gives no way to guess a room from hardware we don't know
    #   about) rather than falling back to a friendly_name naming
    #   convention ("Voice - <Room> Assist") a given house's HA instance
    #   may not use at all.
    ha_satellite_room_map: str = Field(default="")
    # ha_tv_entities: fallback room -> Apple TV entity mapping used only
    #   when the admin API's Room TV Config is unreachable (see
    #   src/orchestrator/tv_handler.py's get_tv_configs). Accepts a JSON
    #   array of {"room", "media_player_entity_id", "remote_entity_id"}
    #   objects, or a comma-separated list of
    #   "room:media_player_entity_id[:remote_entity_id]" triples for a
    #   lighter .env-friendly form. Empty means no hardcoded fallback --
    #   every handler already treats an empty TV-config dict as "no TV
    #   entity configured" and returns a message naming that, so this is
    #   behavior-preserving when the admin API is also unreachable.
    ha_tv_entities: str = Field(default="")
    # ha_bed_warmer_entities: JSON object naming the 5 HA entities a
    #   Sunbeam-via-Tuya dual-zone bed-warmer/mattress-pad integration
    #   exposes: {"level_left", "level_right", "power_main",
    #   "power_side_a", "power_side_b"}. This is inherently
    #   house-specific hardware (DC14 item 1) -- empty means the
    #   bed_warmer intent handler reports the feature isn't configured
    #   instead of controlling a hardcoded device.
    ha_bed_warmer_entities: str = Field(default="")
    # ha_light_groups: JSON object mapping a room name to a light-group
    #   entity ID ({"<room>": "<light group entity>"}), used only by
    #   smart_home_controller.py's scene-activation-failed fallback (dim/
    #   turn on that room's lights when the named scene/script doesn't
    #   exist). Empty means the fallback for an absent room is skipped
    #   entirely (DC17 item 1, OSS-First) -- turning on every light in the
    #   house ("all") when a specific room's group isn't configured is a
    #   house-wide regression, not a safe default.
    ha_light_groups: str = Field(default="")
    # ha_music_players: fallback room -> Music Assistant media_player
    #   entity mapping, used only when the admin API's room_audio_config
    #   table is unreachable (see src/orchestrator/music_handler.py's
    #   get_room_configs). Accepts a JSON object {"room": "entity_id"} or
    #   a comma-separated list of "room:entity_id" pairs. Empty means no
    #   hardcoded fallback -- every caller already handles an empty
    #   room-config dict.
    ha_music_players: str = Field(default="")

    # ------------------------------------------------------------------
    # HA write authorization (ATHENA-69 / D4, D8, D22)
    # ------------------------------------------------------------------
    # Four JSON-array-of-strings fields, parsed via
    # shared.guest_policy.parse_json_array_env: unset (empty string) means
    # "use the built-in default list"; a malformed value also falls back to
    # the default (never an empty list, which would silently disable a
    # floor); an explicit "[]" is honoured as an intentional deployer
    # opt-out. Same pattern as ha_light_groups above.
    #
    # ha_permission_fallback_restricted_entities: entity-id regex patterns
    #   applied when the mode service is unreachable or rejecting (D4) --
    #   the "degraded" permission set. Default restricts the
    #   physical-security domains (locks, covers, alarm panels, cameras,
    #   automations, scripts, scenes) while leaving lights/climate/media
    #   usable. An explicit "[]" here means an outage grants unrestricted
    #   HA writes -- a deliberate, logged deployer choice
    #   (ha_permission_fallback_disabled), not a silent default.
    ha_permission_fallback_restricted_entities: str = Field(default="")
    # guest_baseline_restricted_entities: the floor unioned into every
    #   guest's restricted_entities regardless of admin config (D8) --
    #   same domain set as the fallback list above, plus this is *always*
    #   unioned in, never replaced by an admin-configured list.
    guest_baseline_restricted_entities: str = Field(default="")
    # guest_baseline_allowed_intents / guest_baseline_allowed_domains: used
    #   only when the admin-configured guest allowlist is empty (D22) -- an
    #   empty admin list means "use this baseline", never "allow
    #   everything".
    guest_baseline_allowed_intents: str = Field(default="")
    guest_baseline_allowed_domains: str = Field(default="")

    # mode_service_ingress_auth: D15. Same three-value contract as
    # orchestrator_ingress_auth above, applied to the mode service's
    # /mode* routes via shared.service_ingress_auth. "enforce" (default)
    # requires X-Service-Key; "warn" logs and allows through for staged
    # rollout; any other value behaves as "enforce" and logs an ERROR once.
    mode_service_ingress_auth: str = Field(default="enforce")

    # mode_bookings_source (ATHENA-127 D1): which booking source(s) the
    #   mode service reads. "auto" (default): admin is required, the legacy
    #   `calendar_url` iCal feed (if set) is advisory-additive -- its
    #   bookings can only add guest time, never remove it, and its own
    #   freshness never degrades the house. "admin": admin only. "ical":
    #   the legacy iCal URL is the only, required, source. An unrecognised
    #   value falls back to "auto" with one ERROR log
    #   (mode_bookings_source_unknown).
    mode_bookings_source: str = Field(default="auto")
    # mode_bookings_max_age_seconds (D6): how long a source's last
    #   successful fetch is trusted before that source is classified
    #   "expired". Clamped at use to [max(300, 2 x the iCal poll interval),
    #   604800]. The runtime lever for an admin outage (R2) --
    #   `kubectl set env deploy/athena-mode-service
    #   MODE_BOOKINGS_MAX_AGE_SECONDS=<n>` restarts the pod, so it only
    #   helps once the outage is already over.
    mode_bookings_max_age_seconds: int = Field(default=21600)
    # mode_service_url (D7): same env var `module_registry.py` already reads
    #   for the admin UI's modules page -- read here too, by the new
    #   GET /api/guest-mode/mode-status proxy (guest_mode.py). Empty means
    #   the proxy reports `{"reachable": false, "error":
    #   "mode_service_url_unset"}` rather than guessing a default.
    mode_service_url: str = Field(default="")

    # mode_override_lockout_threshold / mode_override_lockout_minutes: D16/
    #   D25. Read by the admin backend's per-tier owner-PIN lockout counter
    #   (POST /api/internal/guest-mode/verify-pin) -- after this many failed
    #   verifications in a trust tier within the lockout window, that tier
    #   is locked out for this many minutes. Not read by the mode service,
    #   which holds no PIN state after D25.
    mode_override_lockout_threshold: int = Field(default=5)
    mode_override_lockout_minutes: int = Field(default=30)

    # override_max_timeout_minutes: ATHENA-69 Pass H2 (xander delta review,
    #   High). Server-side ceiling on POST /mode/override's timeout_minutes,
    #   applied regardless of PIN outcome -- a caller (or a caller-supplied
    #   value alone, previously unbounded) could otherwise request an
    #   owner-mode override lasting effectively forever
    #   (timeout_minutes=999999). Requested values above this are clamped,
    #   never rejected outright (a too-long request still gets the
    #   maximum, not a hard failure).
    override_max_timeout_minutes: int = Field(default=240)

    # livekit_user_token_ttl_minutes: D27. TTL for browser-facing LiveKit
    #   room tokens minted for jarvis-web voice sessions (clamped 1-1440 by
    #   the gateway's generate_room_token). Server-side Athena tokens are
    #   unaffected -- they pass an explicit 24h TTL. A shorter TTL only
    #   bounds *joining* a room; it doesn't disconnect an already-connected
    #   participant.
    livekit_user_token_ttl_minutes: int = Field(default=30)

    # ha_write_fanout_confirm_threshold / ha_write_fanout_hard_limit
    #   (ATHENA-128, D7/D11): bounds on how many distinct HA entities a
    #   single utterance may write without naming that scope explicitly.
    #   0 disables the corresponding bound entirely (never confirm /
    #   never hard-block on count alone). A non-zero hard_limit must be
    #   >= threshold -- see the model_validator below.
    ha_write_fanout_confirm_threshold: int = Field(default=_FANOUT_CONFIRM_THRESHOLD_DEFAULT, ge=0)
    ha_write_fanout_hard_limit: int = Field(default=_FANOUT_HARD_LIMIT_DEFAULT, ge=0)

    # sms_default_country_code: the country calling code (1-3 digits, no
    #   "+") prefixed to a booking's national phone number when matching an
    #   incoming SMS to a stay (admin-backend's sms_webhook.to_e164). An
    #   invalid value logs one ERROR (sms_default_country_code_invalid) and
    #   falls back to "1". A mismatch fails closed: the stay doesn't match.
    sms_default_country_code: str = Field(default="1")
    # twilio_allow_unsigned: "true" (exactly) lets admin-backend accept
    #   Twilio webhooks with no signature check when TWILIO_AUTH_TOKEN is
    #   unset outside DEV_MODE; anything else answers them 503. Opting in
    #   means anyone who knows a guest's phone number can text as them. A
    #   string on purpose: pydantic's bool parsing would also accept
    #   1/yes/on. Ignored whenever TWILIO_AUTH_TOKEN is set.
    twilio_allow_unsigned: str = Field(default="")

    @field_validator("sms_default_country_code", mode="before")
    @classmethod
    def _validate_sms_country_code(cls, v: object) -> str:
        value = v if isinstance(v, str) else ""
        if 1 <= len(value) <= 3 and all(c in "0123456789" for c in value):
            return value
        logger.error(
            "sms_default_country_code_invalid: SMS_DEFAULT_COUNTRY_CODE must be 1-3 digits "
            "with no '+' (got %d characters); using 1",
            len(value),
        )
        return "1"

    @model_validator(mode="after")
    def _validate_fanout_limits(self) -> "AthenaConfig":
        # Every service loading the shared config reads these, so an
        # inverted pair must not fail startup stack-wide: log one ERROR and
        # fall back to the defaults for the pair (the gate stays on).
        if 0 < self.ha_write_fanout_hard_limit < self.ha_write_fanout_confirm_threshold:
            logger.error(
                "ha_write_fanout_limits_invalid: HA_WRITE_FANOUT_HARD_LIMIT=%d is below "
                "HA_WRITE_FANOUT_CONFIRM_THRESHOLD=%d (a non-zero hard limit must be >= the "
                "threshold; 0 disables it); using the defaults %d/%d",
                self.ha_write_fanout_hard_limit,
                self.ha_write_fanout_confirm_threshold,
                _FANOUT_HARD_LIMIT_DEFAULT,
                _FANOUT_CONFIRM_THRESHOLD_DEFAULT,
            )
            self.ha_write_fanout_confirm_threshold = _FANOUT_CONFIRM_THRESHOLD_DEFAULT
            self.ha_write_fanout_hard_limit = _FANOUT_HARD_LIMIT_DEFAULT
        return self

    # ------------------------------------------------------------------
    # Deferred fields — see CONTRIBUTING.md for the extension pattern
    # ------------------------------------------------------------------
    # Fields below are NOT yet migrated to AthenaConfig.  They are listed
    # here as a reference for future contributors.  Each field should follow
    # the same pattern as the fields above (plain Field with default, or
    # @computed_field for derived values).
    #
    # LOCAL_DEV — CIRCULAR IMPORT RISK.  admin_url.py reads LOCAL_DEV
    #   directly; adding it here would create a config → admin_url → config
    #   circular dependency.  Future path: extract LOCAL_DEV into
    #   src/shared/_env_flags.py, importable from both modules.
    #
    # CONTROL_AGENT_URL (4 sites), HA_URL / HA_TOKEN (10+13 sites),
    # OIDC_* aliases beyond OIDC_ISSUER / OIDC_CLIENT_ID — runtime-mutable
    # globals in oidc.py; deferred pending separate analysis.


@functools.lru_cache(maxsize=1)
def get_config() -> AthenaConfig:
    """Return the singleton ``AthenaConfig`` for this process.

    Cached: ``AthenaConfig`` is instantiated exactly once.  This mirrors the
    ``get_admin_url()`` pattern from ``src/shared/admin_url.py``.

    Tests can reset the cache with ``_clear_cache_for_tests()``.
    """
    config = AthenaConfig()
    logger.info(
        "athena_config_loaded",
        extra={
            "ollama_url_set": bool(config.ollama_url),
            "llm_service_url_set": bool(config.llm_service_url),
            "redis_url_set": bool(config.redis_url),
            "database_url_set": bool(config.database_url),
            "service_api_key_set": bool(config.service_api_key),
            "oidc_issuer_set": bool(config.oidc_issuer),
            "oidc_client_id_set": bool(config.oidc_client_id),
            "dev_mode": config.dev_mode,
            "demo_mode": config.demo_mode,
            "control_agent_enabled": config.control_agent_enabled,
        },
    )
    return config


def _clear_cache_for_tests() -> None:
    """Reset the ``get_config`` lru_cache.

    PRIVATE — production code must never call this.  Unit tests should call
    it from a ``@pytest.fixture(autouse=True)`` so each test starts with a
    fresh config instance that re-reads env vars.

    Important: modules that capture config values at *import time* into a
    module-level constant (e.g. ``REDIS_URL = get_config().redis_url``) will
    NOT be affected by this call — the constant already holds the value from
    the first import.  To test different env-var values against those modules,
    either use ``monkeypatch.setenv(...)`` BEFORE importing the affected module,
    or call ``importlib.reload(module)`` after ``_clear_cache_for_tests()``
    to force re-execution of the module-level statements.

    See the equivalent note in ``src/shared/admin_url.py::_clear_cache_for_tests``.
    """
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Install telemetry (admin-backend). These four variables are deliberately not
# AthenaConfig fields: get_config() is cached, and an opt-out must take effect
# on the next decision, so they're re-read from the process env and `.env` on
# every call. The opt-outs are a fail-closed union of the two sources.
# ---------------------------------------------------------------------------

TELEMETRY_DEFAULT_ENDPOINT = "https://telemetry-athena.xmojo.net/v1/ping"
TELEMETRY_ENV_KEYS = ("ATHENA_TELEMETRY", "DO_NOT_TRACK", "ATHENA_TELEMETRY_ENDPOINT", "ATHENA_TELEMETRY_MODE")


# A line in `.env` that names a telemetry variable must be a plain
# `NAME=value` (optionally `export NAME=value`). python-dotenv silently skips
# a line it can't parse (`ATHENA_TELEMETRY: off`, `DO_NOT_TRACK 1`), which
# would lose an opt-out, so such a line makes the whole reading fail closed.
_TELEMETRY_LINE = {
    key: re.compile(rf"^\s*(?:export\s+)?{key}(?![A-Za-z0-9_])") for key in TELEMETRY_ENV_KEYS
}
_TELEMETRY_ASSIGNMENT = {
    key: re.compile(rf"^\s*(?:export\s+)?{key}\s*=") for key in TELEMETRY_ENV_KEYS
}


class ParseError(ValueError):
    """A telemetry variable's line in `.env` isn't a `NAME=value` assignment."""


def _check_telemetry_lines(dotenv_path: str) -> None:
    with open(dotenv_path, encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            for key in TELEMETRY_ENV_KEYS:
                if _TELEMETRY_LINE[key].match(line) and not _TELEMETRY_ASSIGNMENT[key].match(line):
                    raise ParseError(key)


@dataclass(frozen=True)
class TelemetryEnvReading:
    """The four telemetry variables as found in each source, kept apart so an
    empty process value can never mask a `.env` opt-out. ``dotenv_error`` is
    the exception class name when `.env` exists but can't be read."""

    process: Dict[str, str]
    dotenv: Dict[str, str]
    dotenv_error: Optional[str] = None


def read_telemetry_env(
    environ: Optional[Mapping[str, str]] = None,
    dotenv_path: str = ".env",
    reader: Optional[Callable[[str], Mapping[str, Optional[str]]]] = None,
) -> TelemetryEnvReading:
    """Read the telemetry variables from the process env and `.env` (resolved
    against the working directory, as pydantic-settings resolves it for
    AthenaConfig). Uncached, and never raises: an absent `.env` is empty; an
    unreadable one, or one with a telemetry line that isn't a `NAME=value`
    assignment, is reported in ``dotenv_error`` (which means off)."""
    environ = os.environ if environ is None else environ
    process = {k: str(environ[k]) for k in TELEMETRY_ENV_KEYS if k in environ}
    dotenv: Dict[str, str] = {}
    error: Optional[str] = None
    try:
        if os.path.lexists(dotenv_path):
            values = (reader or dotenv_values)(dotenv_path)
            _check_telemetry_lines(dotenv_path)
            dotenv = {k: ("" if values[k] is None else str(values[k])) for k in TELEMETRY_ENV_KEYS if k in values}
    except Exception as exc:  # noqa: BLE001 - any read failure means "off"
        error = type(exc).__name__
    return TelemetryEnvReading(process=process, dotenv=dotenv, dotenv_error=error)


def _first_non_empty(reading: TelemetryEnvReading, key: str) -> Optional[str]:
    for source in (reading.process, reading.dotenv):
        value = (source.get(key) or "").strip()
        if value:
            return value
    return None


def resolve_endpoint(reading: TelemetryEnvReading, default: str = TELEMETRY_DEFAULT_ENDPOINT) -> str:
    """A non-empty process value, else `.env`, else the default. (An
    explicitly empty value disables telemetry; that's the enablement rule's
    job, not this resolver's.)"""
    return _first_non_empty(reading, "ATHENA_TELEMETRY_ENDPOINT") or default


def resolve_mode(reading: TelemetryEnvReading) -> str:
    """ATHENA_TELEMETRY_MODE, lowercased: process, else `.env`, else ""."""
    return (_first_non_empty(reading, "ATHENA_TELEMETRY_MODE") or "").lower()
