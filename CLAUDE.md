# CLAUDE.md - Project Athena

This file provides guidance to Claude Code when working with Project Athena.

## ⚠️ CRITICAL: OSS-First Development

**Every fix, feature, and architectural change must be designed to work for any Athena deployment — not just Jay's specific home lab setup.**

This is the public OSS repository. All code must be implementation-agnostic:

1. **No hardcoded values** — IPs, hostnames, credentials, or domains must come from env vars, `src/shared/config.py`, or the admin panel
2. **No assumption of Jay's infrastructure** — code can't assume specific ports, hostnames, or service locations
3. **Configuration over convention** — if a value might differ between deployments, it must be configurable
4. **Test generalizability** — ask "would this work for someone else deploying Athena from scratch?"
5. **Run the maintainer-leak gate before committing** — `python3 scripts/check-maintainer-leaks.py` (also enforced in CI by `.github/workflows/maintainer-leaks.yml` on every PR and push, no `paths:` filter). Allowlist entries in `scripts/.maintainer-leak-allowlist` are exact full-line matches, not substrings. Private/local patterns you don't want in the public repo go in a file outside this checkout (`--extra-patterns` / `MAINTAINER_LEAK_EXTRA_PATTERNS`), never committed here.

**Examples:**
- ❌ `ha_url = "http://192.0.2.10:8123"` as a fallback
- ✅ `ha_url = os.getenv("HA_URL")` with a log warning if missing
- ❌ Hardcoded service URL in a function body
- ✅ Service URL from env var or `config.py` constant

## Project Overview

Project Athena is an AI-powered smart home assistant with voice interface, RAG (Retrieval-Augmented Generation) services, and Home Assistant integration.

## Production Deployment Architecture

### Infrastructure

**Kubernetes Cluster:** Your K8s cluster
**Namespace:** athena-prod
**Container Registry:** Your container registry (e.g., `your-registry:5000`)

### LLM Inference Options

**Option 1: External Ollama (Recommended for Apple Silicon)**
- Run Ollama on a Mac with Apple Silicon for best inference performance
- Configure `OLLAMA_URL` in config.yaml to point to your Mac

**Option 2: In-Cluster Ollama**
- Deploy `manifests/athena-prod/ollama.yaml` for containerized inference
- Slower than Apple Silicon but works on any cluster

### Services Architecture

```
                    ┌─────────────────────────────────────────┐
                    │            External Access              │
                    │  athena.your-domain  │  chat.your-domain│
                    └─────────────────────────────────────────┘
                                      │
                              Ingress Controller
                                      │
        ┌─────────────────────────────┴─────────────────────────────┐
        │                                                           │
        ▼                                                           ▼
┌───────────────────┐                                    ┌──────────────────┐
│  Admin Frontend   │                                    │   Jarvis Web     │
│ (Admin web UI)    │                                    │ (Voice Interface)│
└───────────────────┘                                    └──────────────────┘
        │                                                           │
        ▼                                                           │
┌───────────────────┐                                               │
│  Admin Backend    │◄──────────────────────────────────────────────┤
│   (FastAPI)       │                                               │
└───────────────────┘                                               │
        │                                                           │
        ▼                                                           ▼
┌───────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│     Gateway       │────────►│   Orchestrator  │────────►│   Ollama (LLM)  │
│   (API Router)    │         │  (Query Engine) │         │                 │
└───────────────────┘         └─────────────────┘         └─────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
            ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
            │ RAG Weather │   │ RAG Sports  │   │  RAG News   │
            └─────────────┘   └─────────────┘   └─────────────┘
                              ... 23 RAG services total ...
```

### Service Ports

| Service | Port | Description |
|---------|------|-------------|
| Admin Backend | 8080 | API and admin functions |
| Admin Frontend | 80 | Admin web UI (vanilla JS + nginx) |
| Gateway | 8000 | API gateway/router |
| Orchestrator | 8001 | Query orchestration |
| Mode Service | 8022 | Mode management |
| Jarvis Web backend | 3001 | Chat/voice proxy to orchestrator |
| Redis | 6379 | Caching |
| Ollama | 11434 | LLM inference |
| Control Agent | 8099 | Service control (runs on the Control Agent host) |

### Orchestrator pipeline architecture

`src/orchestrator/main.py` is the LangGraph pipeline entry point (8,758 lines as of ATHENA-10). The orchestrator package has 14 sibling modules extracted from the original god-object:

**Sibling modules at `src/orchestrator/`**

| Module | Purpose |
|---|---|
| `state.py` | Canonical definitions for `OrchestratorState`, `IntentCategory`, `ModelTier`, `ConversationContext`. Do not redefine these in `main.py` or elsewhere. |
| `helpers.py` | 17 stateless helpers. Helpers that need runtime singletons call `_runtime.get_X()` at call time (Pattern 1). |
| `mode_permission.py` | Mode/permission resolution and the HA-write permission guard (ATHENA-69). Entry points: `resolve_request_authorization` (server-derived mode/permissions, D6/D7), `handle_owner_mode_utterance` (the sole PIN-utterance path, D24), `authorize_ha_write`/`authorize_automation_config`/`authorize_sequence` (pure authorization checks), `ha_permission_scope`/`current_ha_scope` (per-request `ContextVar` scope), `PermissionEnforcingHAClient`/`ensure_permission_enforcing` (the write-interception wrapper), plus the original 6 mode helpers (`get_current_mode`, `detect_owner_mode_command`, `extract_pin_from_query`, `activate_owner_override`, `check_intent_permission`, `check_entity_permission`) and `OWNER_MODE_PATTERNS`. See "HA write authorization (ATHENA-69)" below. |
| `urls.py` | 25 service URL constants (23 RAG + `MODE_SERVICE_URL` + `NOTIFICATIONS_SERVICE_URL`); canonical env spelling `RAG_<NAME>_URL`, legacy `<NAME>_RAG_URL` accepted with a warning. |
| `metrics.py` | 11 Prometheus metric objects (`request_counter`, `request_duration`, `node_duration`, `tool_call_breakdown`, `validation_counter`, `hallucination_counter`, `validation_layer_duration`, `ha_write_denied_total`, `state_question_routed_total`, `ha_write_fanout_confirm_total`, `intent_gate_refused_total`). |
| `utterance_kind.py` | Stdlib-only `classify_utterance(q) -> UtteranceClassification` (`STATE_QUESTION` / `IMPERATIVE` / `UNKNOWN`, plus room, device type, `needs_referent`). Computed once per turn in `route_control_node` and passed down; English-only. |
| `write_fanout.py` | The write fan-out gate (ATHENA-128): `gate`/`gate_many` (bind-and-check before any task list or `call_service(`), `take_block`, `pending_carrier` (sole setter of `PermissionScope.can_carry_pending`), `confirmed`, `caller_fingerprint`, `rewording`, `BARE_AFFIRMATION_RE`/`BARE_NEGATION_RE`/`normalize_reply`. |

**`nodes/` package at `src/orchestrator/nodes/`**

`nodes/__init__.py` exports all 11 node functions via `__all__`. Each node module also contains one or more proxy classes that defer singleton access to `_runtime.get_X()` at call time.

| Module | Node function |
|---|---|
| `route_info.py` | `route_info_node` |
| `send_sms.py` | `send_sms_node` |
| `notification_pref.py` | `notification_pref_node` |
| `synthesize.py` | `synthesize_node` |
| `validate.py` | `validate_node` |
| `finalize.py` | `finalize_node` |
| `intent_refused.py` | `intent_refused_node` (the intent gate's refusal; `intent_gate_refusal` in `mode_permission.py` decides, and the graph router, the streaming runner and `/query` all ask it) |
| `route_control.py` | `route_control_node` |
| `route_music.py` | `route_music_node` |
| `route_tv.py` | `route_tv_node` |
| `retrieve.py` | `retrieve_node` |

**`nodes/_runtime.py` — the runtime accessor**

`_runtime.py` lives at `src/orchestrator/nodes/_runtime.py`. Import it as `from orchestrator.nodes import _runtime`.

- `_runtime.set_X(value)` — called from `main.py`'s lifespan, once per process, to register each singleton.
- `_runtime.get_X()` — called at node/helper invocation time, never at import time.
- `_runtime.is_ready() -> bool` — returns `True` when all required singletons are set.
- `_runtime.missing_required() -> list[str]` — names of required singletons not yet set (used by lifespan readiness assertion).
- `_runtime.required_singletons() -> tuple[str, ...]` — public accessor for the required-singleton tuple (do not read `_runtime._REQUIRED_SINGLETONS` directly).
- `_runtime.reset_for_test()` — resets all slots and strict-mode flag; use in test teardown.

**Invariants (enforced throughout; violations are bugs)**

- **R2-C3**: sibling modules (`helpers.py`, `mode_permission.py`, `urls.py`, `metrics.py`) and node modules MUST NOT `from orchestrator.main import ...`. Use `_runtime.get_X()` for runtime singletons; import from `helpers.py`, `mode_permission.py`, `state.py`, `urls.py`, or `metrics.py` for everything else.
- **R2-H1 (lifespan construction)**: lifespan constructs each client as a local variable first, then calls `_runtime.set_X(local)`. Never invert this (e.g., `_runtime.set_X(SomeClass(_runtime.get_other()))` is forbidden).

**`validate_node` training-knowledge bypass (ATHENA-39)**

`validate_node` bypasses the Layer 4 LLM fact-check when `is_training_knowledge_fallback` is true:

```
is_training_knowledge_path = (intent == GENERAL_INFO) OR (conversation_history OR history_summary populated)
is_training_knowledge_fallback = is_training_knowledge_path AND not retrieved_data AND not base_knowledge_populated AND intent != WEBSEARCH
```

This matches exactly the two synthesize-node branches that explicitly permit training-knowledge synthesis (`synthesize.py:129-144` for `GENERAL_INFO` and `synthesize.py:152-166` for any intent with conversation context). First-turn current-domain queries (WEATHER, SPORTS, STOCKS, NEWS, etc.) with no RAG data do NOT match and continue to run Layer 4 — `synthesize.py:167-179` tells the LLM not to invent specifics for that branch, so Layer 4 protection is correctly aligned. WEBSEARCH is carved out even with conversation context because the user explicitly requested fresh data. Layer 1 (length) and Layer 2 (pattern detection) still run on the bypass path; `hallucination_counter` does NOT increment on bypass because the `*_unsupported` counters were specifically counting cases where Layer 4 then ran. Observable via `validation_counter{passed="true", reason="training_knowledge_fallback"}`. When adding new intents that should always have retrieval, add an explicit WEBSEARCH-style carve-out to the bypass condition.

**What stays in `main.py` for now**

`classify_node` (2,473 lines), `tool_call_node`, route handlers, and streaming functions remain in `main.py`. Extraction is deferred: `classify_node` to Campaign 2, `tool_call_node` to Campaign 1.3, route handlers to Campaign 1.5.

When adding a new node, helper, or utility:
- New stateless helpers → `helpers.py`
- New mode/permission logic → `mode_permission.py`
- New service URL → `urls.py`
- New Prometheus metric → `metrics.py`
- New pipeline node → `src/orchestrator/nodes/<node_name>.py`, exported from `nodes/__init__.py`
- New data type shared across the pipeline → `state.py`

**Ingress authentication (ATHENA-89, D10)**

Orchestrator ingress routes (`/query*`, `/v1/chat/completions`, `/sessions*`, `/session/{id}/warmup`, `/admin/*` — 13 routes) are gated by `orchestrator/ingress_auth.py::require_service_caller`; mode via `ORCHESTRATOR_INGRESS_AUTH` (`warn`/`enforce`); every in-cluster caller must send `X-Service-Key` and is checked by `tests/unit/test_orchestrator_callers_send_service_key.py`.

**HA write authorization (ATHENA-69)**

Every Home Assistant write is authorized against the request's server-derived permissions before it's sent — see `docs/CONFIGURATION.md` "Mode and permissions" for the full model. Invariants (enforced by `tests/unit/test_ha_permission_wiring.py`; violations are bugs):

1. New HA writers receive the lifespan `ha_client` or call `ensure_permission_enforcing` themselves — a raw, unwrapped `HomeAssistantClient` must never reach a write.
2. New HA-writing nodes open `ha_permission_scope(state.permissions, …)` before dispatching — the guard denies-by-default outside an open scope.
3. `request.mode` is a narrowing hint only — a client can request guest inside an owner house, never claim owner; `resolve_request_authorization` is the sole authority (see the mode_permission.py row above).
4. `caller_trust` gates only the owner-PIN override utterance (`handle_owner_mode_utterance`) and who may be addressed by name (see "Who the assistant addresses by name" below), and is set exclusively by server code (gateway, `wyoming_bridge.py`, `livekit_integration.py`, `sms_webhook.py`, jarvis-web's `chat`/`chat_stream`, which derives `web_authenticated`/`web_local`/`web_guest_net` from `Caller.caller_class`) — never copied from a request body field. `web_guest_net` (jarvis-web's guest network) is not PIN-trusted.
5. Mode-service routes require `X-Service-Key` (`MODE_SERVICE_INGRESS_AUTH`, `enforce`/`warn`).
6. The owner PIN is verified only by admin-backend (`POST /api/internal/guest-mode/verify-pin`) — the mode service and the orchestrator never hash or compare a PIN themselves.
7. jarvis-web fails closed. A browser gets one of three outcomes: the home network (`web_local`: the proxy-appended hop or, with `JARVIS_DIRECT_CLIENTS`, the TCP peer in `JARVIS_LOCAL_NETWORKS`, no Cloudflare headers, `Host` in `JARVIS_ALLOWED_HOSTS`; or an attested edge `home`), signed in (`web_authenticated`: an attested edge identity in `JARVIS_HOUSEHOLD_GROUPS`, or an owner/operator Bearer), or `401 sign_in_required` with no upstream call. The guest network (`JARVIS_GUEST_NETWORKS`) is always guest mode and sends `caller_trust="web_guest_net"`. The edge's identity, groups and name headers (`JARVIS_EDGE_IDENTITY_HEADER`/`_GROUPS_HEADER`/`_NAME_HEADER`) may not be a reserved header (`X-Jarvis-Edge-Class`, `X-Jarvis-Edge-Attestation`, `X-Service-Key`, `X-Jarvis-Relay-Key`, `X-Jarvis-Relay-Client`, `Authorization`, `Cookie`, `Host`, `X-Forwarded-For`, `CF-Connecting-IP`) or equal each other (startup refusal); the name header is read only for an attested household member and gives `Caller.speaker_first_name` (first word, letters/marks and `. - '` only, never logged; `capabilities.display_name` is still the login). Owner-only routes accept `web_authenticated`, and `web_local` only while the resolved mode is owner (`403 guest_stay_active` otherwise); an owner at home during a stay signs in on the sign-in host. In edge mode the class headers count only with the attestation secret from a `TRUSTED_PROXY_CIDRS` peer, and home/guest only when the network rule corroborates them. On the chat routes a present `X-Jarvis-Relay-Key` is resolved first and is always the public audience (a wrong key is 401, no fall-through). `caller_auth.py` is the only place that decides any of this; `ROUTE_CLASSIFICATION` covers every route (`TestJarvisWebRoutesClassified`). `JARVIS_PUBLIC_MODE` was removed (`household` exits at startup).
8. A STATE_QUESTION (per `classify_utterance`, computed once in `route_control_node`) runs under that node's single scope opened with `read_only=True`. The guard denies every write first under it. `read_only` holds only through the reuse chain, so code reachable from `execute_intent` must use the reuse-aware `… if current_ha_scope() is None else nullcontext()` form, never an unconditional `with ha_permission_scope(`. The only unconditional openers are the three graph-entry nodes `route_control`/`route_music`/`route_tv`, and each passes `read_only=` from its own `classify_utterance` result (AST drift test `TestScopeReopenDrift` in `tests/unit/test_state_question_routing.py`). Every `SmartHomeController` method that calls `call_service` binds and checks `write_fanout.gate(...)` before its first write -- the single-target handlers (climate, bed warmer, motion control, scene activation) with one entity, so commands pass and only a real question is stopped; the drift test's allowlist (`tests/unit/test_write_fanout_confirmation.py`) is empty and any new entry needs a stated reason. The room light paths (single room, multi-room, room group) gate on the physical lights a write reaches -- the leaf lights beneath the written ids, absent group members included -- not on the number of ids written, so a grouped room counts as its bulbs (`_finalize_light_targets` supplies `gate_ids`). The same closed-world rule covers `SequenceExecutor` (its direct-entity step writes are gated, so a sequence can never carry a question's write), and a routed STATE_QUESTION never enters the sequence branch. The dynamic automation agent's write tools (`ha_service`, `create_automation`, `delete_automation`, `notify`) each bind and check `write_fanout.question_refusal` first: a real STATE_QUESTION gets the rewording as the tool result (no count threshold, so commands are unaffected), also checked closed-world over `AutomationAgent`. An unidentified-device question stores a read-only clarification context (`awaiting_state_question`, ttl 60) so the reply is answered as that question, never as a continuation of an earlier write. A pending confirmation replays only for the same caller fingerprint, under the replaying turn's own scope; `caller_trust` is only a fingerprint input, never an authority. The fingerprint's device input is `voice_device_id` (the gateway sends HA Assist's device id there) or else `device_id`; `voice_device_id` is never used for the guest-session-by-device lookup, and a caller with neither a trust tag nor a device id gets no fingerprint (never a follow-up surface). A turn from a different caller never overwrites another caller's pending (no context store, no new pending). The replay nonce claim fails closed when Redis is configured. `supports_followup` is set as a literal `True` only by the follow-up-capable senders (gateway HA conversation route, LiveKit, jarvis-web `chat`/`chat_stream`, SMS; Wyoming never), checked by `test_orchestrator_query_callers_tag_supports_followup`. At the orchestrator boundary any `X-Service-Key` caller is trusted server code for it (the same class as `caller_trust`); setting it only changes the phrasing (prompt vs the command to say), because a replay needs a matching fingerprint on the same session and full re-authorization.

**Room light writes (ATHENA-205)**: room light commands resolve through `SmartHomeController._resolve_room_lights` (the `HA_LIGHT_GROUPS` entry for the room first, else `HAEntityManager.find_lights_by_room`'s anchored name match with `HA_ROOM_LIGHT_EXCLUDE_ENTITIES`) and then `_finalize_light_targets`, on all three room paths and the status query's lookup. The finalizer checks every leaf id (existing or absent) against the request's permissions; a group with a denied leaf is degraded to its permitted leaves, so a guest never reaches a restricted bulb through a group. It then covers what is left so no bulb is written twice (multi-room and room-group dedupe across all rooms together), and the fan-out gate counts the leaf lights. All-denied returns `permission_refusal_message`; partial denial is recorded (`record_precheck_denial`, non-halting) only after the gate passes, so a confirmation prompt is never replaced by a partial refusal, and `_finish` then returns the partial refusal. Whole-house and scene-fallback light writes don't use the finalizer yet. The status answer reads outside the guard and may name a restricted light.

**SMS stay phases**: admin-backend's SMS webhook sends `context.stay_phase` (`current`/`recent`/`upcoming`, from an exact E.164 match on a confirmed stay). `resolve_request_authorization(..., sms_stay_phase=)` is the sole authority: for `caller_trust == "sms"` and any phase but `current` (missing or unknown included) it applies `off_stay_overlay` — every HA write denied, `stay_read_only` (admin-DB writers — automation create/archive, notification preferences, SMS send, memory create/forget — check `is_stay_read_only` and refuse with `STAY_READ_ONLY_REFUSAL`), and intents/tools narrowed to `OFF_STAY_ALLOWED_*` (the intent gate refuses self-gated intents for it too, and `offered_tools` narrows what's offered and executable). A new admin-DB writer reachable from an SMS turn must check `is_stay_read_only`.

**State-question kill switch (ATHENA-128)**: the admin feature `state_question_routing_kill_switch` is seeded **disabled**; **enabling** it reverts state-question routing (UNKNOWN is substituted for routing decisions only — `scope.utterance` keeps the real classification, so the gate's IMPERATIVE/explicit-scope exemptions still apply). It's inverted because `get_feature_config` reports `enabled: False` for a missing flag or an admin outage. `route_music`/`route_tv` honour it for the scope (no read-only scope when it's on), but a real question there is still only read: TV answers from state and music with what's playing, each adding the command form. With it on, the gate blocks every write whose `scope.utterance` is a real STATE_QUESTION from one entity up, whatever the limits: a confirmation where carriable, the rewording otherwise, so a question never writes silently while a misread command still works after "yes". The read-only guard and the fan-out limits stay active; the limits' own off switch is `HA_WRITE_FANOUT_CONFIRM_THRESHOLD=0` + `HA_WRITE_FANOUT_HARD_LIMIT=0`. Both limits live in the shared `AthenaConfig`, so every service that loads it reads them; a non-zero hard limit below the threshold logs one ERROR and both fall back to the defaults (6/18) rather than failing startup.

One narrow, deliberate exception to invariant 1: `music_handler.py` holds a private `self._ha_raw` (the unwrapped client) used only for one bulk `/api/states` read (`get_playing_rooms_from_ha`/`check_music_assistant_players`) — reads are out of the guard's scope entirely (D10), and `tests/unit/test_ha_permission_guard.py` asserts `self._ha_raw` never calls `.call_service(` anywhere in `src/` and that its only attribute accesses are `.url`/`.headers` at that one site.

**Memory vector store**: all `athena_memories` Qdrant and embedding access goes through `admin/backend/app/services/memory_vectors.py` (one process-wide embed lock; routes call it via `run_in_threadpool`). Postgres is the truth: rows carry `vector_status` (`pending` until `store_vector` confirms the point) and semantic results are built from live `stored` rows, never from Qdrant payloads. Rebuilds hold the `settings_lease` key `memory_vectors.reindex.lock` (dry runs `memory_vectors.reindex.dryrun`). Enforced in CI by `.github/workflows/admin-memory.yml` (drift guard, one-process memory suite, real Qdrant 1.19/1.12 and Postgres tier, image RSS gate).

**Who the assistant addresses by name**

`helpers.build_query_context(request, guest_info, *, server_mode, degraded)` is the only place identity enters `state.context` (both keywords are required, so a new caller can't fail open), and `addressee_kind`/`resolve_addressee` are the only readers; all three prompt builders (tool_call, `synthesize_node`, `build_synthesis_prompt_for_streaming`) pass exactly one of `owner_name`/`guest_name`/`household_first_name` from `resolve_addressee` to `build_core_assistant_prompt`. Rules:
- The staying guest's name only for a caller_trust in `REQUEST_GUEST_NAME_TRUST` (`web_guest_net`, `sms`), only while the house's own mode is guest and not degraded. The request's and the device's identity values are recorded separately before any merge; the device leg (`DEVICE_GUEST_NAME_TRUST`) is empty for now (voice/device guest naming is a follow-up), so a device-fingerprinted guest keeps its guest-scoped session/permissions/cache from `guest_info` but is not named.
- `speaker_first_name` only for `web_authenticated`, re-cleaned with the same rule as jarvis-web (`tests/fixtures/speaker_first_name_vectors.json` pins both); rendered as a JSON-quoted data field (`json.dumps(..., ensure_ascii=False)`), never inside a "You are speaking with" sentence. The guest's name is likewise a quoted data field after `assistant_profile.clean_guest_name` (letters/marks and `. - '`, at most 4 words / 64 characters; a failing name addresses nobody).
- The owner line comes only from base-knowledge `owner_name`, in owner mode; `addressee_kind` returns `None` whenever the mode service is degraded (never `owner`), and `build_knowledge_context(..., degraded=...)` (a required keyword) then drops every name-like key and owner-category row.
- The semantic cache skips read and write when `addressee_kind(...)` is `guest` or `household`, or the mode service is degraded.
- A new `caller_trust` value must be classified in `tests/unit/test_trust_classification.py` (it fails until it is).
- Base knowledge: a static `guest_name` entry is never rendered; `owner_name`/`name` render only for `user_mode == "owner"`.
- `automation_agent` receives `guest_name=None` from `route_control` (there's no `guest_name` on `OrchestratorState`); its guest prompt branch is dormant.
- SMS session ids are `sms_` + an HMAC of the number keyed on `SERVICE_API_KEY` (`sms_webhook.sms_session_id`); rotating the key resets every SMS conversation.

**Spoken output (`shared.output_channel`)**

An answer is formatted for speech ("25 miles per hour") only when the server knows it will be spoken; text keeps "mph". `shared.output_channel` is the one seam: `channel_for_interface_type`, `render_for_channel`/`render_answer`, `render_sink_text`, `@renders_spoken_answer`, `classify_openai_caller`. Rules (violations are bugs):

1. Every Athena TTS sink renders: Wyoming `_synthesize`, LiveKit `TTSClient.synthesize`, jarvis-web `/api/voice/synthesize`, the automation agent's `tts.speak`, and `/ha/conversation`. A new sink fails the population test in `tests/unit/test_output_channel.py` until it renders (sinks cap their input at `SPEECH_SINK_MAX_CHARS`).
2. `/query` renders **only** in the `@renders_spoken_answer` decorator on `process_query`, never in its body, so the cache and sessions hold raw text. The streaming routes render through `render_answer` (speech is normalized whole, then sent).
3. The gateway classifies OpenAI-compatible callers in order: a `/v1/voice/*` route template, then `OPENAI_SPEECH_CLIENT_NETWORKS` (client address from `resolve_rate_client`; a network overlapping `TRUSTED_PROXY_CIDRS` is ignored), else text. A client string never decides it; `extra_body.interface_type` is built from the server's decision.
4. Every `/query` caller sends a literal `interface_type` (`TestOrchestratorQueryCallersTagInterfaceType`). jarvis-web always sends `chat` and normalizes only in `/api/voice/synthesize`, off the event loop.
5. Semantic-cache keys end with `iface_<type>` and store raw answers.
6. `normalize_for_tts` stays idempotent (a spoken answer is rendered again at its sink) and linear-time (bounded quantifiers; `tests/unit/test_tts_normalizer.py` has the fuzz and performance cases).
7. The channel never feeds authorization: `shared.output_channel` is not imported by `mode_permission.py`, `caller_auth.py` or `semantic_cache.py`.

**Local clock (`shared.local_time`)**

Wall-clock decisions use `local_now()`/`local_today()`/`local_day_bounds_utc()` (and `local_tz()`), which read `DEFAULT_TIMEZONE` and never the process `TZ`. Real elapsed time is computed between UTC-converted values (`a.astimezone(timezone.utc) - b.astimezone(timezone.utc)`): subtracting two aware values that share a `ZoneInfo` is wall-clock arithmetic and is off by an hour across DST. Every image that imports it pins `tzdata`. `tests/unit/test_local_clock_drift.py` counts every remaining process-TZ clock read (naive `now()`/`today()`, `date.today()`, `utcnow()`, `utcfromtimestamp()`, `fromtimestamp()` without a zone) per `(path, function)` against a reasoned allowlist; a new one fails CI (`.github/workflows/source-guards.yml`) until it's switched or classified.

**Logs and audit rows never carry names, addresses, locations, phone numbers or whole payloads**: log presence, ids or key names (`has_guest_name=`, `guest_id=`, `address_set=`, `location_source=`, `phone_last4=x[-4:]`, `changed_fields=sorted(update_data.keys())`, `arg_keys=payload_keys(arguments)`); `AuditLog` values go through `redact_phone_fields`. `tests/unit/test_no_pii_in_logs.py` enforces it over logger calls and `AuditLog(...)` constructors in `src/`, `apps/` and `admin/backend/app/` with a counted allowlist (the admin audit actor and its client IP, and a few ticketed RAG location logs). `configure_logging` keeps `uvicorn.access`/`httpx`/`httpcore` at WARNING, since their INFO lines are full URLs with query strings.

**Guest-mode booking source (ATHENA-127)**

While guest mode is enabled, the mode service decides guest vs owner from
`calendar_events` (fed by `calendar_sources`, incl. Lodgify), not from
polling the legacy single-iCal `calendar_url` field directly. `MODE_BOOKINGS_SOURCE`
(`auto` default / `admin` / `ical`) picks the source(s): in `auto`, admin
is required and a configured legacy `calendar_url` is advisory-additive
only — its last-good bookings add guest time in every state except
`never_loaded` (even `expired`), but its own freshness never degrades the
house; it's fetched https-only through `shared.url_safety.safe_get`.
Precedence: an active unexpired override wins; `enabled == false` →
owner; any active considered booking → guest; the required source `fresh`
or `stale` → owner; otherwise → `degraded`. `MODE_BOOKINGS_MAX_AGE_SECONDS`
(default 21600s / 6h) bounds how long a source's last success is trusted
before it's `expired`; while merely `stale` (a fetch failure after a prior
success), an owner may still see a residual: a booking created, moved
earlier, or extended after that last success is unknown for up to
`max_age`, and the house can briefly read owner while a guest is actually
present. There is no PIN-override escape from `degraded` during an
admin-backend outage — PIN verification itself requires admin-backend.
Feed blocks (`Blocked`/`Closed Period`/`Not available`/etc.) are classified
`status='blocked'` and never count as a stay. Date-only and floating
(no `Z`/`TZID`) booking times are localised in `DEFAULT_TIMEZONE` — see
`src/shared/booking_window.py`. Mode-service sibling modules import as
`mode_service.<name>` (e.g. `mode_service.bookings`), never a bare or
relative import — the image copies the package to `/app/mode_service/`
and runs uvicorn from `/app`, so anything else fails at container boot,
not at test time. See `docs/CONFIGURATION.md` "Guest-mode booking source"
for the full model.

**Calendar sync invariants.** `run_source_sync` in
`admin/backend/app/services/calendar_sync.py` is the only code that writes
feed-derived `calendar_events`; the scheduler, sync-all and
`POST /api/calendar-sources/{id}/sync` all call it, and it holds the
`system_settings` lease `calendar_sync.lock.<source id>` for the whole run
(core in `app/services/settings_lease.py`, shared with service control
and the memory vector rebuild),
with a compare-and-swap renew immediately before commit. Don't add another
insert site (`CalendarEvent(` appears once across `calendar_sync.py` and
`calendar_sources.py`). A source that is `lodgify` by type or feed host and
has an enabled Lodgify API key never writes from iCal: an API failure or
an unreadable key writes nothing. Keys are source-scoped: a sync never
updates or re-parents another source's row; a colliding UID is stored as
`src:<source id>:<sha256[:32]>`, a missing one as
`ical-nouid:<source id>:<uuid>`, and only an orphaned `lodgify_api_sync`
row may be adopted (`source_id` set). Every calendar-sources route except
`GET /types` depends on `require_user_permission` (a drift test walks the
app's routes). `CalendarSource.to_dict_safe()` never carries `ical_url`;
only `GET /api/calendar-sources/{id}` returns it (audited), and
`/test-url` takes the URL in the body, never the query string. Calendar logs and status strings carry `safe_error(exc)` (class and
HTTP status) only, never exception text.

---

**Install telemetry**

admin-backend sends a pseudonymous `first_boot` event and then a daily heartbeat to the maintainers' collector (`admin/backend/app/services/telemetry/`: `schema.py` wire shape and allowlists, `classify.py` pure rules, `collect.py` DB reads, `sender.py` loop/lease/POST; `app/routes/telemetry.py`; `admin/frontend/telemetry.js`). See `docs/CONFIGURATION.md` "Telemetry" for the full model. Invariants (enforced by the `test_telemetry_*` tests and the leak gate; violations are bugs):

1. admin-backend is the only sender. No other service reads or sends telemetry.
2. Every payload model is `extra="forbid"`. A new **field** needs a `schema_version` bump, and the collector must accept the new version before any release sends it. A new **value** in an open-vocabulary list (a feature flag, component, intent, RAG service) needs no bump.
3. `EXPECTED_PATHS` (`tests/test_telemetry_privacy.py`), the golden fixture (`tests/fixtures/telemetry/valid-v1-full.json`, regenerated with `UPDATE_GOLDEN=1`), `docs/telemetry/payload-v1.schema.json` and the "What's sent" table in `docs/CONFIGURATION.md` change together, and the fixtures are copied to the collector.
4. The default endpoint literal lives only in `TELEMETRY_DEFAULT_ENDPOINT` (`src/shared/config.py`), its one allowlisted line.
5. The opt-outs (`ATHENA_TELEMETRY`, `DO_NOT_TRACK`) are a fail-closed union of the process env and `.env`, read at call time; an unreadable `.env`, or a telemetry line in it that isn't a `NAME=value` assignment, means off. Nothing (no installation ID, no install key) is created while telemetry is off, including by `GET /api/telemetry/status`.
6. There is no free-text model field. A model leaves only as `family` (from `MODEL_FAMILIES`, else `custom`), `size_bucket`, `quantized` and `source`; `MODEL_FAMILIES` and the other client allowlists are the privacy gate. The wire `family` is a closed enum of that list; the collector stores only its own shipped copy and records anything else as `custom`, so a new family reads as `custom` until the collector ships it.
7. The local install key is never sent, logged, stored in the payload or returned by the API; the wire carries only its HMAC bound to the endpoint's origin.

### Control Agent

The Control Agent runs on a host alongside Ollama (e.g., an Apple Silicon Mac or bare-metal node). It provides HTTP endpoints to manage Ollama and other services.

**Location:** `src/control_agent/` (copied to the control-agent host at `~/<path>/control_agent`)

**Starting the Control Agent on its host:**
```bash
ssh <ssh-user>@<control-agent-host>
cd ~/<path>/control_agent
CONTROL_AGENT_SERVICES_FILE=/path/to/services.json \
    nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8099 > /tmp/control_agent.log 2>&1 &
```

**`CONTROL_AGENT_SERVICES_FILE` (ATHENA-99, D46, OSS-First)**: path to a JSON file naming exactly which bare Python/uvicorn processes, watchdog exclusions, and Docker containers this Control Agent instance may manage. Unset (the default): the Control Agent manages nothing -- the 60s watchdog and the startup registry sync are no-ops, and every `is_port_allowed`/`is_container_allowed` check returns false. There is no built-in process list; a deployment that wants the watchdog/registry-sync/Docker-control features must opt in explicitly with this file. See `src/control_agent/services.example.json` for the schema (`processes` keyed by port, `watchdog_exclude`, `containers`) and `docs/CONFIGURATION.md`. A malformed file logs an ERROR per problem and falls back to managing nothing -- it never crashes the process. `processes` can be empty while `containers` is non-empty (a container-only deployment, e.g. managing `whisper-wyoming`/`athena-piper-tts` with no bare processes at all).

**Verify it's running:**
```bash
curl http://192.0.2.10:8099/health
curl http://192.0.2.10:8099/ollama/health
```

**K8s Configuration:**
The admin-backend needs `CONTROL_AGENT_URL=http://<your-control-agent-host>:8099` environment variable set.

**OSS-First default — opt-in required:**
The Control Agent is disabled by default (`CONTROL_AGENT_ENABLED=false`). Set `CONTROL_AGENT_ENABLED=true` in your private overlay or `.env` only if a Control Agent process is actually running on a host. OSS deployers without a Control Agent no longer see connection errors from admin-backend or orchestrator startup. Deployment-specific host/IP values (SSH targets, private kubeconfig overlay entries) belong in `CLAUDE.local.md` (untracked; see `.gitignore`), not here.

**Inbound authentication (ATHENA-110)**: every mutating Control Agent route (`/process/*`, `/docker/*`, `/ollama/start|stop|restart`, `/huggingface/download|import-to-ollama|downloaded`-DELETE, `/watchdog/*`) requires an `X-Service-Key` header matching this host's `SERVICE_API_KEY` env var — a missing/wrong key gets 401, an unset key gets 503 on every mutating route (fail-closed; the Control Agent has no `DEV_MODE` bypass). Read-only routes are unaffected. The start command above is unchanged; set `SERVICE_API_KEY` in the same environment the Control Agent runs in, matching the value admin-backend and the orchestrator already use for `/api/*` service-to-service auth. See `docs/CONFIGURATION.md` for the full route list and `src/control_agent/auth.py` for the dependency.

**Download-progress callback**: the Control Agent posts progress to admin-backend's service-only route with `X-Service-Key`, and attaches the key only when the callback host is an exact `ALLOWED_CALLBACK_HOSTS` entry (`src/control_agent/huggingface.py`, which repeats the `shared/service_key.py` rules inline because the host gets only `src/control_agent/`). admin-backend builds the callback from `CONTROL_AGENT_CALLBACK_BASE_URL` (unset: its own loopback, with a warning; only right when both share a host).

**Ollama control via the Control Agent (ATHENA-118)**: `resolve_manager`/`resolve_ollama_manager` only ever dispatch an Ollama start/stop/restart through the Control Agent when the Control Agent's own host equals the configured Ollama host (both normalized the same way as every other row) — a Control Agent running elsewhere on the network never gets Ollama lifecycle calls routed to it, even if `CONTROL_AGENT_ENABLED=true`. If the hosts don't match, Ollama falls through to Kubernetes resolution (if opted in) or `manager: 'none'` ("Managed on its host" in the admin UI).

## Development Commands

### Building Images

**IMPORTANT: Target Architecture**
The Kubernetes cluster runs on `linux/amd64`. When building from Apple Silicon (M1/M2/M3/M4), you MUST specify `--platform linux/amd64` or images will fail with "exec format error".

```bash
# Build all images for linux/amd64 and push to registry
./scripts/build-and-push.sh

# Build single image (ALWAYS include --platform linux/amd64)
docker build --platform linux/amd64 -t YOUR_REGISTRY/athena-orchestrator:latest -f src/orchestrator/Dockerfile src/
docker push YOUR_REGISTRY/athena-orchestrator:latest

# Force rebuild without cache
docker build --platform linux/amd64 --no-cache -t YOUR_REGISTRY/athena-orchestrator:latest -f src/orchestrator/Dockerfile src/
```

**Service Dockerfile Locations:**
| Service | Dockerfile Path | Build Context |
|---------|-----------------|---------------|
| Admin Backend | `admin/backend/Dockerfile` | `admin/backend/` |
| Admin Frontend | `admin/frontend/Dockerfile` | `admin/frontend/` |
| Orchestrator | `src/orchestrator/Dockerfile` | `src/` |
| Gateway | `src/gateway/Dockerfile` | `src/` |
| Mode Service | `src/mode_service/Dockerfile` | `src/` |
| Jarvis Web | `apps/jarvis-web/Dockerfile` | repo root (the Dockerfile copies `apps/jarvis-web/` and four single files from `src/shared/` beside `main.py`, each over a committed local-dev shim: `admin_url.py`, `client_throttle.py`, `service_key.py`, `tts_normalizer.py`; there's no `shared` package in the image) |
| RAG Services | `src/rag/<service>/Dockerfile` | `src/` (every RAG Dockerfile does `COPY shared /app/shared`; see `scripts/build-and-push.sh`'s `build_push_src`) |

### Kubernetes Operations

```bash
# Always verify context first
kubectl config current-context

# Deploy all manifests
# WARNING: manifests/athena-prod/ is an OSS template with placeholder
# values. This command is safe against a FRESH, unconfigured namespace
# only. Against an already-configured namespace it silently reverts every
# ConfigMap/Secret key back to the placeholder defaults. To change a
# running deployment's config, patch or edit the live resource
# (`kubectl patch`/`kubectl edit`) or apply a private overlay — never
# `kubectl apply -f manifests/athena-prod/` (or the directory) against a
# namespace that already has house-specific values.
# Prefer `./scripts/deploy.sh deploy` over this raw kubectl command: it
# refuses (non-zero exit) if any manifest it is about to apply still
# contains an unconfigured YOUR_*/CONFIGURE_ME*-class placeholder (image
# registry, storage class, etc.), unless you pass --allow-placeholders
# (fresh, unconfigured namespace only). The raw `kubectl apply -f` below
# has no such guard.
kubectl apply -f manifests/athena-prod/

# Check deployment status
kubectl -n athena-prod get pods
kubectl -n athena-prod get pods -w  # Watch

# View logs
kubectl -n athena-prod logs -f deploy/athena-orchestrator

# Port forward for local testing
kubectl -n athena-prod port-forward svc/athena-admin-backend 8080:8080
```

### Database Operations

```bash
# Connect to database
psql -h YOUR_DB_HOST -U athena -d athena

# Run migrations (from admin-backend pod)
kubectl -n athena-prod exec -it deploy/athena-admin-backend -- alembic upgrade head
```

## Configuration

### LLM Model Configuration

Models are configured via the Admin UI at **LLM Components** page. All 11 components can be independently configured:

- **Orchestrator Components:** intent_classifier, intent_discovery, response_synthesis, tool_calling_simple/complex/super_complex, conversation_summarizer
- **Validation Components:** fact_check_validation, response_validator_primary/secondary
- **Control Components:** smart_home_control

### Environment Variables

Key configuration in `manifests/athena-prod/config.yaml`:

- `OLLAMA_URL` - LLM inference endpoint
- `ATHENA_DEFAULT_MODEL` - Default model for seeding
- `ATHENA_DOMAIN` / `CHAT_DOMAIN` - Your domain names
- `ADMIN_API_URL` - Admin backend URL; resolution order (`ADMIN_API_URL` → `ADMIN_BACKEND_URL` → `ADMIN_INTERNAL_URL` [deprecated] → `LOCAL_DEV=true` → K8s auto-discovery → `""`) is centralized in `src/shared/admin_url.py::get_admin_url()` — do not add new `os.getenv("ADMIN_*_URL")` calls outside that module
- Centralized configuration: 64 env vars (`OLLAMA_URL`, `LLM_SERVICE_URL`, `REDIS_URL`, `DATABASE_URL`, `SERVICE_API_KEY`, `DEFAULT_TIMEZONE`, `DEFAULT_CITY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_VALIDATE_ISS`, `DEV_MODE`, `DEMO_MODE`, `CONTROL_AGENT_ENABLED`, `CONTROL_AGENT_CALLBACK_BASE_URL`, `SERVICE_CONTROL_K8S_ENABLED`, `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_LOCKOUT_THRESHOLD`, `LOGIN_LOCKOUT_MINUTES`, `LOGIN_MINIMUM_DELAY_MS`, `SERVICE_REGISTRY_WRITE_PER_MINUTE`, `SERVICE_REGISTRY_ENDPOINT_URL`, `SERVICE_REGISTRY_NAME`, `HEALTH_POLL_INTERVAL_SECONDS`, `HEALTH_POLL_TIMEOUT_SECONDS`, `HEALTH_POLL_CONCURRENCY`, `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`, `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`, `CONTENT_FETCHER_ALLOW_BROWSER_FETCH`, `SESSION_MAX_COUNT`, `NEW_CONVERSATION_PER_MINUTE_PER_IP`, `TRUSTED_PROXY_CIDRS`, `NEW_CONVERSATION_RESET_GRACE_SECONDS`, `ORCHESTRATOR_INGRESS_AUTH`, `MUSIC_ASSISTANT_URL`, `SEARXNG_BASE_URL`, `TRANSIT_REGION_NAME`, `TRANSIT_GTFS_FEEDS`, `TRANSIT_STATIC_SERVICES`, `COMMUNITY_EVENTS_SOURCES`, `DEFAULT_AMTRAK_STATION`, `HA_SATELLITE_ROOM_MAP`, `HA_TV_ENTITIES`, `HA_MUSIC_PLAYERS`, `HA_BED_WARMER_ENTITIES`, `HA_LIGHT_GROUPS`, `HA_ROOM_LIGHT_EXCLUDE_ENTITIES`, `HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES`, `GUEST_BASELINE_RESTRICTED_ENTITIES`, `GUEST_BASELINE_ALLOWED_INTENTS`, `GUEST_BASELINE_ALLOWED_DOMAINS`, `MODE_SERVICE_INGRESS_AUTH`, `MODE_OVERRIDE_LOCKOUT_THRESHOLD`, `MODE_OVERRIDE_LOCKOUT_MINUTES`, `LIVEKIT_USER_TOKEN_TTL_MINUTES`, `OVERRIDE_MAX_TIMEOUT_MINUTES`, `MODE_BOOKINGS_SOURCE`, `MODE_BOOKINGS_MAX_AGE_SECONDS`, `MODE_SERVICE_URL`, `HA_WRITE_FANOUT_CONFIRM_THRESHOLD`, `HA_WRITE_FANOUT_HARD_LIMIT`, `JARVIS_WEB_URL`, `SMS_DEFAULT_COUNTRY_CODE`, `TWILIO_ALLOW_UNSIGNED`, `OPENAI_SPEECH_CLIENT_NETWORKS`) are read via `get_config()` from `src/shared/config.py::AthenaConfig` (pydantic-settings BaseSettings). New env vars: prefer adding fields to `AthenaConfig` over inline `os.getenv` — see `CONTRIBUTING.md`. The four install-telemetry variables (`ATHENA_TELEMETRY`, `DO_NOT_TRACK`, `ATHENA_TELEMETRY_ENDPOINT`, `ATHENA_TELEMETRY_MODE`) are the exception: they're not `AthenaConfig` fields but are read per call by `read_telemetry_env()` in the same module, as a union of the process env and `.env`.
  - `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` — comma-separated CIDRs/hostnames that the sitescraper SSRF guard will allow. Empty by default (all private IPs blocked). Warning: wide CIDRs like `10.0.0.0/8` bypass the guard for the entire RFC-1918 10/8 range; scope narrowly.
  - `CONTENT_FETCHER_ALLOW_BROWSER_FETCH` — `true` to enable Playwright-based browser fetching in ContentFetcher (default `false`). Playwright fetches bypass the SSRF guard for user-supplied URLs; only enable in isolated environments where the sitescraper allowlist is already locked down.
  - `HA_URL` — Home Assistant base URL for artist-search endpoints in `music_config.py`. No hardcoded default (previously a hardcoded maintainer IP). The server logs a warning at startup if unset; HA artist-search endpoints will return errors until this is set.
- **Admin-backend startup gates** (ATHENA-12, Campaign 2): the admin-backend will not start under the following misconfigurations — all raise `SystemExit` before any DB or auth initialization:
  - `DEV_MODE=true` with a non-SQLite `DATABASE_URL` on a **non-private host** or **inside a K8s pod** → `SystemExit` (xander:6). Soft-warning carve-out: if `KUBERNETES_SERVICE_HOST` is unset AND the DB host resolves to loopback/RFC1918/ULA, admin-backend emits `logger.warning("dev_mode_with_local_non_sqlite_database", ...)` and continues — this covers developers running a local Postgres instance. K8s pods always raise SystemExit even when the ClusterIP is RFC1918 (xander M4 guard). Link-local (169.254/16, fe80::/10) is NOT in the carve-out and continues to raise SystemExit. Helper: `is_local_host(hostname)` in `admin/backend/app/utils/url_validators.py`; wrapper `_is_local_database_url(url)` in `admin/backend/main.py`.
  - `DEMO_MODE=true` with `DEV_MODE=false` (xander:16)
  - `OIDC_CLIENT_ID` set to `""`, `"demo-mode"`, or `"CONFIGURE_ME_OIDC_CLIENT_ID"` in production (xander:13/17)
  - `OIDC_ISSUER` empty, missing, or matching the `CONFIGURE_ME` placeholder in production (pre-existing gate from ATHENA-2, still in force)
  - IdP unreachable or `.well-known/openid-configuration` missing `issuer` field at startup (MED-E discovery-doc gate)
  - DB-loaded runtime issuer empty or placeholder after `configure_oauth_client()` (MED-A runtime-issuer gate)
  - `SERVICE_API_KEY` empty or set to `dev-service-key-change-in-production` in production (ATHENA-21, Campaign 5) — production startup raises `SystemExit` via the `_INSECURE_DEFAULTS` loop in `admin/backend/main.py:427-475` (`kind="secret"` treats empty AND placeholder as fatal). Bypass: set `DEV_MODE=true` for local development. Helper-level: `verify_service_or_oidc` returns 503 to any caller that sends `X-Service-Key` while `SERVICE_API_KEY` is unset (the dispatcher does not silently fall through to OIDC).
  For local development: set `DEV_MODE=true` (uses SQLite in-memory; bypasses OIDC gates). For production: `OIDC_ISSUER` must point to a reachable, conformant OIDC IdP with an `issuer` field in its discovery document.
- **Service-registry architecture (ATHENA-1, Campaign 4)**: The admin DB (`athena_service_registry` table) is the source of truth for all service definitions. The Control Agent is a health augmenter, not the authority — on startup it POSTs its local `PROCESS_SERVICES` manifest to `POST /api/service-registry/services` (authenticated with `X-Service-Key`), which upserts entries into `athena_service_registry` via `sync_registry_loop`. A background async health poller (`admin/backend/app/services/health_poller.py`) runs on a `HEALTH_POLL_INTERVAL_SECONDS` (default 30s) schedule and writes `health_status`/`last_health_check`/`last_error`/`last_response_time_ms` back to the table; the admin UI reads the cache instead of blocking on live pings. The unified service catalog is `GET /api/service-registry/services` (requires auth); on-demand refresh is `POST /api/service-registry/services/poll-now`. OSS deployers without a Control Agent have a fully functional service registry — CA is purely additive. Five new env vars govern this subsystem: `SERVICE_REGISTRY_WRITE_PER_MINUTE` (default 60), `HEALTH_POLL_INTERVAL_SECONDS` (default 30), `HEALTH_POLL_TIMEOUT_SECONDS` (default 5), `HEALTH_POLL_CONCURRENCY` (default 8), `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` (CIDRs/hostnames that override the RFC1918/loopback/ULA SSRF block — empty by default, required for K8s operators with in-cluster services on private subnets). **Health-poller leader election (ATHENA-18)**: when admin-backend runs with `replicas: 2`, the background health poller uses a Redis SETNX-based lease (`athena:health_poller:leader`, `LEASE_TTL_SECONDS=40`) so only one replica polls and writes per cycle; a per-cycle heartbeat task renews the lease every `HEARTBEAT_INTERVAL_SECONDS=20` and cancels the in-flight poll if the lease cannot be renewed (strict-abort policy). Invariant: `LEASE_TTL_SECONDS (40) > HEALTH_POLL_INTERVAL_SECONDS` — setting `HEALTH_POLL_INTERVAL_SECONDS > 40` fails startup with `SystemExit FATAL`. Shared-Redis multi-tenant deployments must isolate lease keys by namespace; this is currently unsupported in code (the key is hardcoded). No new env vars — uses the existing `REDIS_URL`.
- **Service Control managers (ATHENA-118)**: `GET /api/service-control` returns one envelope (`services`, `counts`, `control_agent`, `kubernetes`) instead of a bare list. Each row's run state (`running`/`stopped`/`disabled`) is derived from health + `enabled` (`app/utils/service_state.py::derive_run_state`), never from the deprecated `is_running` column (unwritten since ATHENA-118; kept only because `main.py`'s startup schema gate lists it). `app/services/service_managers.py::resolve_manager` picks a manager per row, in order: **Control Agent** (only when the row's host equals the CA's own host — the retired-stack port/name collisions this gate exists to prevent are exactly what let an operator alias an obscure row onto `athena-orchestrator`), else **Kubernetes** (opt-in: `SERVICE_CONTROL_K8S_ENABLED`, default false; the host reduces to a bare RFC1123 label or `<label>.<namespace>[.svc[.cluster.local]]`, matched against the live Deployment list), else `none`. Kubernetes control is scale-only (`app/services/k8s_control.py`): `deployments` get/list plus `deployments/scale` get/patch, name-scoped to `optional/admin-backend-rbac.yaml`'s `resourceNames` (computed from every `manifests/athena-prod/*.yaml` Deployment minus `athena-admin-backend`/`athena-admin-frontend`, which are never controllable, in app code and in RBAC). There is no `deployments` PATCH anywhere — restart is scale-to-0, a bounded 60s wait for pods to actually terminate, then scale back to a remembered count (`system_settings` key `service_control.replicas.<deployment>`, clamped 1-10, a 0 is never stored) inside a shielded `finally`, so no exception/timeout/cancellation can strand a Deployment at 0 while the process survives — brief downtime, not a rolling restart (rolling would need `deployments` PATCH, deliberately not granted). A cross-replica lease (`system_settings` key `service_control.lock.<deployment>`, 90s TTL, atomic INSERT + conditional-UPDATE takeover when expired) serializes actions across admin-backend replicas; contention is 409 `action_in_progress`. An expired `restart` lease with the Deployment still at 0 replicas surfaces as `manager_note: 'restart_interrupted'` — press Start to recover; there's no automatic recovery. Two enabled/disabled rows resolving to the same Kubernetes target block each other (`manager_note: 'target_collision:<name>'`) so an alias can't hijack a critical target. Critical targets — the named set (`athena-gateway`, `athena-orchestrator`, `athena-mode-service`, `athena-jarvis-web`, `redis`, `qdrant`, `ollama`) union any Kubernetes-resolved row whose group isn't `rag` (fail-safe for a renamed core service), union Ollama under **any** manager — require the owner-only `manage_infrastructure` permission (`User.ROLE_PERMISSIONS['owner']`) plus a server-enforced typed confirmation (`confirm_name` in the request body) that must equal the **resolved target's** name, never an alias row's own name. The owner gate is checked before action-availability, so a non-owner always gets 403 `insufficient_role`, never a 409 about an unavailable action. Every lifecycle POST is audited (`old_value`/`new_value`, including refused 403/409s once resolution has run) and rate-limited on a dedicated `service_control` budget. Ollama control agent routes (`/ollama/start|stop|restart`) are registered before the generic `/{service_name}/{action}` routes — Starlette matches in registration order, and the parametrized route would otherwise silently swallow them. The Ollama card (`GET /ollama/health`, `POST /ollama/{action}`) resolves through `resolve_ollama_manager`, which runs the **same** `resolve_manager` every registry row uses — there is no separate Ollama-specific manager-resolution path. Every admin-backend HTTP call to the resolved Ollama URL (health probe, `/api/tags`, `/api/ps`, model load/unload, and the two `GET`/`POST /api/settings/ollama-url` reachability probes) is gated by the runtime `check_ssrf_safe` allowlist (`HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`); `POST /api/settings/ollama-url` additionally validates the URL at the write boundary (scheme, length, IMDS/link-local/multicast/unspecified/`.svc`/`.cluster.local` blocklist, loopback allowed only outside a K8s pod) **before** that reachability probe ever runs.
- **Auth-hardening startup behavior** (ATHENA-14, Campaign 3): `POST /api/auth/local-login` is defended by three layers applied on top of the existing PBKDF2-600k password hash. (1) Per-IP rate limit via fastapi-limiter (Redis-backed; custom identifier uses `request.client.host` only, ignoring `X-Forwarded-For` to defeat IP-rotation bypass; default 5 req/min/IP, configurable via `LOGIN_RATE_LIMIT_PER_MINUTE`). (2) Per-username DB lockout: `users.failed_login_count` is incremented atomically on each wrong-password attempt; the account locks for `LOGIN_LOCKOUT_MINUTES` (default 30) once `LOGIN_LOCKOUT_THRESHOLD` (default 10) is reached; lockout is idempotent — past-threshold attempts do not extend the window. (3) `LOGIN_MINIMUM_DELAY_MS` (default 400 ms) wall-time floor applied to every failure path so all four branches (not-found, inactive, locked, wrong-password) take the same wall time, closing timing enumeration. All four failure branches return the same 401 generic response (`"Invalid username or password"`) — `403 Account inactive` no longer emitted (behavioral change from pre-ATHENA-14). In `DEV_MODE=true` the rate limiter no-ops; lockout and timing floor remain active.

### Admin Frontend Escaping (ATHENA-66)

`admin/frontend/escape-html.js` is the **only** file that may define
`escapeHtml`/`escapeJsAttr` — enforced by `scripts/check-escape-html-uniqueness.py`
and frozen against runtime overwrite via `Object.defineProperty`. Full
contract (which primitive for which context, the six contexts
`escapeJsAttr` is wrong for, the callee-sink and handler-text-builder
rules, and the "add a new frontend file" checklist) lives in
`admin/frontend/README.md` — read that before touching any `on*=` handler
or adding a new script tag. `.github/workflows/frontend-escaping.yml`
enforces the invariants on every PR; this line points at the scripts
rather than restating rules it cannot enforce (a prior CLAUDE.md invariant
in this file was found violated with zero CI signal — the enforcement
belongs in the gate, not the prose).

## File Structure

```
os-project-athena/
├── admin/
│   ├── backend/          # FastAPI admin backend
│   └── frontend/         # Admin web UI (vanilla JS + nginx)
├── apps/                 # User-facing web apps (chat embed proxy + Jarvis voice/chat web UI)
│   ├── chat-embed/       # CORS-relay proxy for embedding Athena in external sites
│   └── jarvis-web/       # Jarvis voice + chat web interface (LiveKit-based)
├── src/
│   ├── control_agent/    # Service watchdog (runs on the host with Ollama)
│   ├── gateway/          # API gateway
│   ├── jetson/           # NVIDIA Jetson edge deployment
│   ├── mode_service/     # Mode management
│   ├── orchestrator/     # Query orchestration
│   ├── rag/              # RAG services (23 services)
│   ├── shared/           # Shared Python modules
│   └── sms/              # SMS notification service
├── manifests/
│   └── athena-prod/      # Kubernetes manifests
├── scripts/              # Build and deployment scripts
└── thoughts/             # Planning documents
```

## Important Notes

- **ALWAYS build with `--platform linux/amd64`** when building from Apple Silicon - the K8s cluster is AMD64 and images will fail with "exec format error" otherwise
- **NEVER break existing functionality** when adding new features. Changes should be additive and backwards-compatible. If a feature like Service Control relies on the Control Agent, new code must work with that pattern, not bypass it
- **ALWAYS consolidate and expose functionality through the Admin UI** when possible. The Admin UI should be the central management interface for all system operations. When adding new features, health checks, configuration options, or service controls, make sure they are accessible and manageable through the Admin UI rather than requiring command-line access or direct API calls
- All services use `imagePullPolicy: Always` during development
- RAG services without required API keys will start but return errors for queries
- Every admin-backend route needs a credential check or an entry in `INTENTIONALLY_PUBLIC` (13 routes, each with its reason) in `admin/backend/tests/test_route_auth_population.py` (the `admin-backend-auth` CI job); `/public` and `/internal` in a path are names, not access. `admin/backend/tests/test_reviewed_routes_auth.py` pins how each reviewed route answers each credential. An authenticated route must also carry a permission-bearing dependency (`require_user_permission(perm)`, `require_service_or_user_permission(perm)`, another guard exposing `required_permission`, or a service-only guard); the routes that authenticate with a bare `get_current_user`/`verify_service_or_oidc` are frozen in `admin/backend/tests/route_auth_legacy.py`, compared exactly, so no new route can join them. A service caller of a gated route sends `X-Service-Key` (`tests/unit/test_admin_guest_route_callers_send_service_key.py`), and a service call to `/api/voice-automations` also sends `X-Athena-Caller-Mode` (+ `X-Athena-Guest-Name` and `X-Athena-Guest-Stay`, the stay's calendar event id, in guest mode; a guest scope is keyed on the stay, never the name). Log lengths, not head slices of text (`query_len=len(query)`, never `query[:50]`); `tests/unit/test_no_pii_in_logs.py` enforces it.
- A service call to admin-backend takes the key from `src/shared/service_key.py`: `service_key_headers()` passed per call (never a client default, and no `verify=False` or `follow_redirects=True` on a call that carries the key), then `note_admin_refusal(status, "<route template>")` with a string-literal template, never the requested URL (`tests/unit/test_admin_guest_route_callers_send_service_key.py`, `tests/unit/test_reviewed_route_callers_wire.py`, `tests/unit/test_service_key_headers.py`). jarvis-web copies that single file and never calls `service_key_headers()` (same scanner: `test_trees_without_shared_config_never_use_the_header_helper`). A value placed in an admin URL's path goes through `shared.admin_url.path_segment` (`path_segments` for a `{name:path}` parameter): `tests/unit/test_admin_call_path_segments.py`.
- admin-backend's `AuthRejectionMiddleware` (`app/utils/auth_rejections.py`) is registered first, so it's the innermost middleware, and logs `admin_auth_rejected` by route template for a request no guard accepted. A guard records acceptance in `request.state.auth_kind` only after it accepts; a handler that refuses afterwards calls `withdraw_acceptance`. `admin/backend/tests/test_auth_rejection_log.py` enforces it.
- RAG service additions must pass `make smoke-rags SERVICE=<image-name>` before merge; CI enforces this on PRs touching `src/rag/**` or `src/shared/**`
- The orchestrator timeout is 120 seconds to accommodate slower LLM inference
- qwen3 models have `/no_think` optimization enabled to reduce response time

## Plane Project
- Workspace: agile-solutions-group
- Project ID: 4f49cfbf-1257-45da-8c67-f56fc2ad5ad8
- Project Name: Project Athena
- Identifier: ATHENA
