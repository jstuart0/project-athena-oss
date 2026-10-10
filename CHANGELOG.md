# Changelog

All notable changes to Project Athena are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Upgrading

- **Database migration.** Run `alembic upgrade head` on admin-backend. `063` adds `voice_automations.calendar_event_id` (the stay a guest automation belongs to) and a unique index on `sms_incoming.twilio_sid`; duplicate SIDs from earlier Twilio retries keep the SID on their first row only. Existing guest automations have no stay, so guests no longer see them (the owner still does).
- **Admin API credentials.** 45 admin-backend routes that answered anonymously now need `X-Service-Key` or a signed-in user (see "Admin API authentication" in `docs/CONFIGURATION.md`). Every in-repo caller sends one. An external script or integration calling `/api/guests`, `/api/user-sessions`, `/api/room-groups`, `/api/voice-automations`, `/api/pipeline-events`, `/api/debug-logs`, `/api/sms/internal/*` or the settings routes needs one too, and a service call to `/api/voice-automations` must also send `X-Athena-Caller-Mode`.
- **More admin API credentials.** A further 93 admin-backend routes that answered anonymously now need a credential; they keep their `/public` and `/internal` paths. Every in-repo caller sends one. The table by route family is in "Admin API authentication" in `docs/CONFIGURATION.md`.
  - *Service key or a signed-in user* (50 routes): the configuration reads the services make (`/api/features/public`, `/api/llm-backends/public`, `/api/model-configs/public*`, `/api/component-models/public` and `/component/{name}`, the `/api/intent-routing` `/public` routes and per-intent strategy, `/api/tool-calling/*/public`, `/api/escalation/*/public`, `/api/gateway-config/public`, `/api/voice-config/internal/*` and `/health`, the `/api/voice-interfaces` `/public`, `/internal/config` and `/engines/public` routes, `/api/room-audio/internal*`, `/api/room-tv/internal*`, `/apps` and `/features`, `/api/follow-me/internal/config`, `/api/music-config/internal`, `/api/mcp-security/public` and `/check-domain`, `/api/directions-settings/public`, `/api/presets/public/active`, a service's URL in the registry, one cloud model's price) and the writes they make (`POST /api/alerts/public/create` and `/resolve-by-entity`, `POST /api/cloud-llm-usage`, `POST /api/llm-backends/metrics`, `POST /api/tool-proposals`, the escalation state and event writes).
  - *Signed-in user only* (42 routes; a service key is refused): `/api/modules*`, the `/api/cloud-llm-usage` cost reads, `/api/cloud-providers` (list, provider, health, price list), the `/api/ha-pipelines` reads, the `/api/voice-config` lists, active configuration and `/running-config`, the `/api/tool-proposals` reads, the `/api/rag-service-bypass` reads, a service's registry row, `/api/model-configs/presets`, `/api/llm-backends/public/mlx-applicability`, `/api/music-config/browser-playback`, `GET /api/alerts/public/active-by-type` and `GET /api/escalation/metrics/prometheus`.
  - *Service key only*: the Control Agent's download-progress callback, `POST /api/model-downloads/internal/{download_id}/progress`.
- **Scripts, scrapers and dashboards.** Anything outside this repository that read one of those routes anonymously now gets 401. Give it a signed-in user's Bearer token or a user API key (`X-API-Key`) for an owner or operator; never give it the service key. That includes a Prometheus scrape of `/api/escalation/metrics/prometheus` and a `curl` of `/api/modules`.
- **Service key format.** `SERVICE_API_KEY` must be visible ASCII with no space, tab or trailing newline (a Secret created from a file often ends in one). The callers of the routes above never send a key that fails this: they log `service_api_key_unusable` once, naming the variable and never the value, and their admin calls are then refused. Older call sites still hand the key to the HTTP client, which rejects it with an error.
- **HTTPS to the admin API.** Calls that carry the service key to admin-backend now verify its certificate. Plain in-cluster `http://` URLs are unaffected. If your admin URL is `https://` with a private CA, set `SSL_CERT_FILE` (or `SSL_CERT_DIR`) on each calling service to a **combined** bundle, the public roots plus your CA: that variable replaces the trust store for every outbound HTTPS call in that process. Without it the calls fail as connection errors and the service runs on defaults.
- **Control Agent downloads.** The progress callback now needs the service key. Refresh the Control Agent's copy on its host, set `SERVICE_API_KEY` there, and set the new `CONTROL_AGENT_CALLBACK_BASE_URL` on admin-backend to the address the agent's host reaches admin-backend at. The agent attaches the key only when the callback's host is an exact `ALLOWED_CALLBACK_HOSTS` entry; otherwise the callback goes out without it and is refused, and the download never shows progress. Left unset, admin-backend hands out `http://localhost:8080` and logs `control_agent_callback_base_url_unset`, which only works when the agent runs on admin-backend's host.
- **Room TV: review before upgrading admin-backend.** The assistant reads the TV app list and TV feature flags for the first time (see Fixed). On the Room TV page, check which apps are marked guest-allowed. Decide whether `auto_profile_select` (seeded on) should start off: with it on, an owner opening an app that has a profile screen waits about 1.5 seconds and a select press follows. `multi_tv_commands` (seeded off) now controls "open X everywhere". The page's feature toggles appear only after admin-backend is upgraded.
- **Rollout order.** The orchestrator first; then the gateway, jarvis-web, the directions RAG and admin-frontend; then refresh the Control Agent's copy on its host and restart it; admin-backend last, with `alembic upgrade head`. Roll back admin-backend first. Before upgrading admin-backend, check each upgraded service's log for connection or certificate errors on admin calls: that's where a trust-store problem shows. An older service against the new admin-backend isn't stopped, it runs on defaults until it's upgraded: the orchestrator and gateway use plain Ollama at `OLLAMA_URL` with default model options, feature flags read as their built-in defaults, and routing, tool-calling, escalation, voice, room audio and TV configuration, cost and metric writes, stuck-sensor alerts and tool proposals are lost. An older orchestrator also loses guest recognition by device, room groups, the house layout, emerging-intent discovery and voice-automation records. The older admin frontend loses the guest, room, debug-log, pipeline-event and Voice Config panels and pending tool proposals, and falls back to a session JWT on the Admin Jarvis WebSocket, which the new admin-backend refuses. An older Control Agent's progress callbacks are refused. Between the orchestrator and admin-backend upgrades, every SMS is answer-only, and TV launches log `admin_backend_refused` for `/api/room-tv/apps` (the older admin-backend can't serve that route); that stops once admin-backend is upgraded.
- **Twilio.** Set `TWILIO_AUTH_TOKEN` (and `TWILIO_WEBHOOK_BASE_URL`) wherever SMS is used: without the token, admin-backend now answers the SMS webhooks with 503 outside `DEV_MODE`. `TWILIO_ALLOW_UNSIGNED=true` restores unsigned acceptance, at the cost that anyone who knows a guest's number can text as that guest.
- **SMS numbers.** Bookings stored with a national (non-`+`) phone number are matched with `SMS_DEFAULT_COUNTRY_CODE` (default `1`). Set it if your guests' numbers aren't North American.
- **Admin API docs.** `/docs`, `/redoc` and `/openapi.json` on admin-backend are served only with `DEV_MODE=true`.
- **Log-based checks.** admin-backend no longer writes access lines, and admin-frontend's nginx writes no `/api` access line and no `/api` error line below `crit` (an unreachable admin-backend included); a check that looked for a request in either log needs another signal.
- **Room light commands.** A room's lights are now chosen by whole-word, start-of-name matching, and a group is written as itself, not as every light inside it. If a room's lights used names such as `family_*`, `work_*` or `bed_*` (the old synonyms for living room, office and bedroom), or lit up through a satellite's LED ring, set `HA_LIGHT_GROUPS` for that room (or `HA_ROOM_LIGHT_EXCLUDE_ENTITIES=[]` to keep the ring). A grouped room now counts as its bulbs at the write confirmation, so a room with more than 18 bulbs needs "all" or a confirmation where one group write used to pass silently.

### Added

- `SMS_DEFAULT_COUNTRY_CODE` and `TWILIO_ALLOW_UNSIGNED` (see Upgrading).
- `CONTROL_AGENT_CALLBACK_BASE_URL` on admin-backend: where the Control Agent's host reaches admin-backend, used as the base of the download-progress callback (see Upgrading).
- admin-backend logs `admin_auth_rejected` when it refuses a request for its credential, and each service logs `admin_backend_refused` when admin-backend refuses one of its calls (see "Admin API authentication" in `docs/CONFIGURATION.md`).
- CI: an admin-backend route with no credential check, and not on a reviewed list, fails the build. A new authenticated route must also carry a permission check (the existing routes that authenticate without one are frozen on a list), and the guest-data routes are pinned to their permission.
- `HA_ROOM_LIGHT_EXCLUDE_ENTITIES`: JSON array of regexes for lights that a room's name must not pick up (default: status LEDs and LED rings). `[]` turns it off.

### Changed

- An SMS from a stay that ended in the last 24 hours, or starts within 48, is answer-only: travel and checkout questions are answered, but nothing in the house is changed or read, and no automation, notification setting or text is created.
- Asking to delete an automation by voice archives it in Athena (it can be restored) and says the Home Assistant automation is still active; the permanent delete is an admin UI action. The assistant sees stored automation names only as quoted data.
- The Admin Jarvis WebSocket accepts only a single-use ticket; a session JWT is refused.
- A Twilio retry of an answered SMS gets the same reply without asking the assistant again; a retry of one still being answered waits for the reply, then answers 503 with `Retry-After`.
- In guest mode the assistant opens TV apps marked guest-allowed and refuses the rest; it used to refuse every app. The default guest permissions don't include TV control, so this applies where your guest-mode permissions add it.
- The select press that passes an app's profile screen (`auto_profile_select`) happens only for the owner. A guest, or anyone while the mode service is degraded, stops at the profile screen and picks by hand.
- "Open X everywhere" works when `multi_tv_commands` is on; it was always reported as disabled.
- The Control Agent sends the service key on its download-progress callback, and only to a callback host listed in its `ALLOWED_CALLBACK_HOSTS`.
- A tool proposal's author is set by admin-backend from the caller's credential; a `created_by` in the request is ignored.
- `HA_LIGHT_GROUPS` is now the authoritative group for room light commands and for "are the lights on", not only for the scene fallback. Keys match regardless of case, spaces or underscores.
- Room name matching is stricter: the room's words must open the light's id or name, and the synonym table no longer maps a room to a bare word that names another room, a floor or a piece of furniture (`bed`, `work`, `family`, `primary`, `downstairs`, `outdoor`/`outside` for porch). Use `HA_LIGHT_GROUPS` or a room group for those.
- The write fan-out gate counts bulbs (leaf lights), not written ids, for room, multi-room and room-group light commands, so a confirmation names the real number of lights.
- `athena_ha_write_denied_total` also counts a room light command that drops a light the request may not use (once per such command), in addition to the guard's own denials.

### Fixed

- The orchestrator can list and remove voice automations again (the admin API refused its calls). A guest turn is scoped to that guest's own automations; with no guest identity, automation requests are refused instead of reaching every stay's automations.
- An SMS matches a booking only on the exact phone number and only for a confirmed, not-deleted stay, so a number that merely contains the guest's digits, or a blocked or cancelled booking, no longer matches.
- The assistant's TV app list and TV feature flags load, the Room TV page shows its feature toggles, and "Discover Apple TVs" works. Those admin routes, and the public voice-interface list, were registered behind a route that answered in their place, so they had never been reachable.
- Opening a TV app is no longer reported as failed when the app opened but the profile-select press afterwards failed.
- Room light commands write each bulb at most once: a room's nested or overlapping groups no longer produce one call per matching entity, and multi-room and room-group commands no longer write a room's nested groups twice or only the largest group.
- A room command no longer includes a light that only mentions the room: another room's name (`master_bathroom_*` for "bathroom", `entrance_*` for "hall") or a voice satellite's status LED.

### Security

- Guest records, the current stay's guests, device-to-guest sessions, voice automations, pipeline transcripts, proxied debug logs and the voice-pipeline mode are no longer readable or writable without a credential. Note that `SERVICE_API_KEY` can read guest data: keep it in a Secret.
- A guest-scoped service call sees and changes only its own stay's voice automations, never another stay's (even one with the same guest name, like every "Airbnb Guest") or the owner's. Name-based bulk archive and restore are owner-only.
- No log line carries the start of a query, transcript, answer, response body, exception message or cache key; lengths are logged instead.
- admin-backend's log carries no request URL or WebSocket ticket, and admin-frontend's nginx logs no `/api` request line, in its access log or, when admin-backend is unreachable, its error log.
- No admin-backend route answers anonymously except the 13 listed as public in `docs/CONFIGURATION.md`. The 93 routes gated in this release exposed feature flags, model and backend endpoint URLs, voice, room and TV configuration, the MCP allowlist and cloud cost data, and accepted anonymous writes: alerts, cost and metric rows, escalation state, tool proposals and download progress.
- A refused credential is logged on both sides. admin-backend writes `admin_auth_rejected` with the route template, method, status, a reason code and the kind of credential presented; the calling service writes `admin_backend_refused` with the status and the route template. Neither line carries a concrete path, a query string, a header or key value, or a client address.
- Calls that carry the service key to admin-backend verify its TLS certificate and don't follow redirects, and a value placed in an admin URL's path is percent-encoded, so it can't select a different route.
- A service key that isn't plain ASCII is refused with 401 instead of a 500, and the callers of the newly gated routes never send a key that can't be an HTTP header value, so it can't end up in an HTTP client's error message there.
- On a route that accepts the service key or a user, a wrong key is refused even when a valid user credential comes with it. An empty `X-Service-Key` header counts as no key there; a signed-in-user route refuses any `X-Service-Key` header, empty or not.
- A guest can no longer reach a restricted light through a group that contains it: a room command replaces such a group with its permitted lights, and refuses when none are left. Known gaps, to follow up: whole-house and scene-fallback light writes still write groups as such, the gateway's single-entity fast path builds its own entity name, fan, cover, lock and media room matching still uses substring matching, and "are the lights on" answers may name a light a guest couldn't control.

---

## [0.6.0] - 2026-09-30 — Addressed as themselves, local-time clock, install telemetry

The assistant addresses each caller as themselves: a household member using jarvis-web during a guest stay is no longer called by the guest's name. Its clock and date windows follow `DEFAULT_TIMEZONE` instead of the pod's timezone, structured answers are no longer truncated, and names, addresses, locations and phone numbers stay out of logs. admin-backend builds again at a pinned embedding-model revision, and it now sends a pseudonymous daily install heartbeat, on by default, with a documented opt-out.

### Upgrading

- **Install telemetry is on by default.** After this upgrade admin-backend sends a `first_boot` event, then a heartbeat about once a day, no sooner than 5 minutes after it starts. It's pseudonymous, not anonymous: a random installation ID links an install's heartbeats. It carries the version, install class and deployment shape; each LLM component's model family, size bucket and local-vs-cloud placement; and coarse feature and usage buckets. It never includes hostnames, URLs, keys, names, queries or guest data. To keep it off, set `ATHENA_TELEMETRY=off` or `DO_NOT_TRACK=1` in admin-backend's environment or `.env` **before** the new version starts (for Kubernetes, the `athena-config` ConfigMap or your private overlay). An opt-out in either source wins, and a `.env` line naming either variable that isn't a plain `NAME=value` also means off. You can also turn it off afterwards on the **Install telemetry** card under System Configuration. See `docs/CONFIGURATION.md` "Telemetry" for every field sent and how long it's kept.
- **Rollout order: orchestrator before jarvis-web.** jarvis-web now sends `caller_trust: "web_guest_net"` for the guest network, and a 0.5.0 orchestrator rejects that value. Until jarvis-web is upgraded, no jarvis-web caller is addressed by the guest's name. To roll back, roll jarvis-web back first, then the orchestrator.
- **Flush the semantic cache once after the orchestrator upgrade**, so answers cached with a guest's name or a UTC date aren't served again. For example, run `redis-cli --scan --pattern 'athena_semantic:*' | xargs -r redis-cli del` against the orchestrator's Redis.
- **Set `DEFAULT_TIMEZONE` to the property's IANA zone** (for example `America/New_York`) before upgrading. The assistant's time and date, "today"/"tomorrow", event, sports and transit day windows, and scheduled "at 7:00" waits now read it instead of the process `TZ`. With the default `UTC`, a deployment that relied on the pod's `TZ` now answers in UTC. The orchestrator, gateway and the community-events, SeatGeek, transportation, Tesla and sports images ship the zone database (`tzdata`). An empty or unknown value falls back to UTC and logs `local_timezone_invalid`.
- **SMS conversations start afresh once**, when admin-backend is upgraded: SMS session ids are now an HMAC of the number keyed on `SERVICE_API_KEY` (see Security). Rotating `SERVICE_API_KEY` later resets them again.
- **Embedding model revision.** Building admin-backend downloads the embedding model at the revision pinned in its Dockerfile (`EMBEDDING_MODEL_REVISION`), never the latest upstream commit, and still needs Hugging Face access once. The model weights and name are unchanged (only its tokenizer file differs), so existing memory vectors stay valid. Memories that a failed batch left not searchable are embedded by the automatic background pass, or by **Rebuild vectors** on the Memories page. To re-pin, change `EMBEDDING_MODEL_REVISION` and `admin/backend/embedding-model.sha256` together.
- **jarvis-web edge headers.** jarvis-web refuses to start if `JARVIS_EDGE_IDENTITY_HEADER`, `JARVIS_EDGE_GROUPS_HEADER` or the new `JARVIS_EDGE_NAME_HEADER` names one of its reserved headers (`X-Jarvis-Edge-Class`, `X-Jarvis-Edge-Attestation`, `X-Service-Key`, `X-Jarvis-Relay-Key`, `X-Jarvis-Relay-Client`, `Authorization`, `Cookie`, `Host`, `X-Forwarded-For`, `CF-Connecting-IP`), or if two of them name the same header. The default `X-authentik-name` is already in the template's strip list. A custom name must be added to the edge's strip Middleware and listed in `JARVIS_EDGE_HEADERS_ACK_STRIPPED`.
- **Quieter access logs.** The orchestrator, gateway, mode service, RAG services and jarvis-web log `uvicorn.access`, `httpx` and `httpcore` at WARNING and up, so request lines (and their query strings) no longer appear at INFO. Use your ingress or proxy logs for per-request traffic.
- **Base knowledge.** A `guest_name` entry is now ignored, and `owner_name` is used only in owner mode. There's nothing to migrate; delete a stale `guest_name` entry whenever convenient.
- **New environment variables:** `ATHENA_TELEMETRY`, `DO_NOT_TRACK`, `ATHENA_TELEMETRY_ENDPOINT` (where heartbeats go; an empty value turns telemetry off) and `ATHENA_TELEMETRY_MODE` (overrides the detected install class) on admin-backend; `JARVIS_EDGE_NAME_HEADER` on jarvis-web. No database migrations.

### Added

- **Pseudonymous install telemetry.** admin-backend sends a `first_boot` event, then a daily heartbeat: version, install class, deployment shape, each LLM component's model family, size bucket and local-vs-cloud placement, and coarse feature and usage buckets. Model names are never sent, only a family from a public list, or `custom`. The **Install telemetry** card under System Configuration shows why it's on or off and the exact last payload sent, and lets an owner turn it off, send once (at most once per 10 minutes across replicas) or reset the installation ID. The payload schema is published in `docs/telemetry/payload-v1.schema.json`.
- `JARVIS_EDGE_NAME_HEADER` (default `X-authentik-name`): the signed-in household member's display name from the auth proxy. Only its first word is used, and it's never logged.
- CI: a version-consistency lane (every version site must equal the latest CHANGELOG release); a telemetry lane (unit tier with the admin route walk, and an integration tier on a digest-pinned Postgres 16); and source guards that fail on a new process-timezone clock read, a newly logged name, address, location or phone number, or a `caller_trust` value the orchestrator doesn't accept. The sports date-window test runs under the sports image's own lock.

### Changed

- jarvis-web's guest network sends its own `caller_trust` value, `web_guest_net`, which can never use the owner PIN.
- A degraded mode service addresses nobody by name, never frames the caller as the owner, and leaves names and owner facts out of the prompt. Its answers aren't cached.
- The staying guest's name is given to the model as a quoted data field after a length and character check, not inside an instruction sentence. Outside owner mode, no name-like or owner-only base-knowledge entry is used.
- Answers addressed to a named caller are no longer served from or stored in the semantic cache.
- A scheduled "at 1:45" during the repeated hour of a daylight-saving change waits for the next 1:45, and a time inside the spring-forward gap runs at the first moment after it.

### Fixed

- A household member using jarvis-web during a guest stay (at home or signed in) is no longer addressed by the staying guest's name. Only the guest network and SMS guests are addressed as the guest. A signed-in member is addressed by their own first name, and the owner by `owner_name` in owner mode.
- The assistant's time and date, "today"/"tomorrow", spoken-date years, event, sports and transit day windows, and scheduled "at 7:00" waits follow `DEFAULT_TIMEZONE` instead of the pod's process timezone. SeatGeek, sports and community-event windows cover whole local days, including across daylight-saving changes. Impossible dates like "February 30" no longer raise an error.
- The admin-backend image builds again. It downloads the embedding model at its pinned revision instead of the latest upstream commit, so a new upstream commit no longer fails every build, and the build checks that the model loads and embeds offline. The pin moves to a revision whose tokenizer pads each batch to its longest text. With the previous one, a batch that mixed a memory over about 126 tokens with shorter ones failed to embed, so rebuilding vectors or indexing several memories at once could mark the memory store unavailable.
- Itineraries and other structured answers are no longer cut off at their second `---` divider or repeated `Label: value` line, and sections under different headings (Day 1, Day 2, ...) are never treated as repeats. Thinking-mode repetition loops are still trimmed.
- The 0.5.0 release left `src/shared`, the gateway, jetson and the control agent at `0.4.0`, and admin-backend's API reported `2.0.0`. Every version site now reports the release version, and admin-backend reads it from `src/shared`.

### Security

- Guest, owner and member names, addresses, locations and full phone numbers are no longer written to logs. Presence flags, ids and last-four digits are logged instead, and admin audit rows for SMS settings and sends keep only a number's last four digits. Tool calls log their argument names, not their values, which can include API keys. Request URLs, query strings included, are no longer logged at INFO by uvicorn's access log or the HTTP client. LiveKit logs the participant's session id, not its client-supplied identity.
- SMS session ids no longer contain the guest's phone number. They're an HMAC keyed on `SERVICE_API_KEY`.

---

## [0.5.0] - 2026-09-30 — Permission before every write, fail-closed web access, calendar-backed guest mode

Every Home Assistant write is now authorized against server-derived permissions before it's sent, and a question about a device can no longer change it. jarvis-web and the website chatbot relay fail closed instead of serving the internet as the household. Guest mode follows the admin-managed calendar sources, and memories are stored in Postgres first with a vector index that rebuilds itself.

### Upgrading

- **Database migrations.** Run `alembic upgrade head` on admin-backend. `060` adds the `owner_pin_attempts` table (per-tier owner-PIN lockout). `061` seeds the `state_question_routing_kill_switch` feature row, disabled; an existing row is never overwritten. `062` adds `memories.vector_status`: existing memories start `pending` and semantic search serves them once the automatic background pass has embedded them. `062` waits at most 5 s for a lock on `memories` and fails rather than blocking.
- **Rollout order.** admin-backend (migrations, and the booking and PIN-verification endpoints the mode service now calls) → mode service → orchestrator → chat-embed → jarvis-web → gateway. Every service needs the same `SERVICE_API_KEY`. Between the admin-backend and orchestrator upgrades memory recall returns nothing: the new admin-backend requires `X-Service-Key` on memory calls and a 0.4.0 orchestrator doesn't send it. To stage the mode service's new ingress auth, uncomment `MODE_SERVICE_INGRESS_AUTH: "warn"` in the config for the rollout window only (the code default is `enforce`), then remove it. To roll back, set the mode service to `warn` **first** and only then roll back images: a 0.4.0 orchestrator that gets a `401` from an `enforce`-mode mode service falls back to owner mode instead of refusing.
- **Required configuration.** `SERVICE_API_KEY` on the mode service and `MODE_SERVICE_URL` on the orchestrator and gateway (and admin-backend, for the Guest Mode status panel). Without them every request resolves to the `degraded` mode and the gateway's fast path never runs. The shipped manifests set all three.
- **Gateway fast path.** Simple on/off commands bypass the orchestrator only for `light.` entities and only while the mode service confirms owner mode. Every other simple command now goes through the orchestrator and its permission checks.
- **Guest and mode permissions.**
  - Guests lose lock, cover, alarm, camera, automation, script and scene control by default (a built-in floor); previously only the admin-configured restriction list applied, and an empty list meant unrestricted.
  - An empty admin-configured guest allowlist (`allowed_intents`/`allowed_domains`) now means "use the built-in baseline", not "allow everything".
  - Guest music and TV control requests are permission-gated the same way lock/cover control already was.
  - A mode-service outage puts the house in a distinct `degraded` state (never owner, never unrestricted) instead of falling back to unrestricted owner. Physical control at the device, or the Home Assistant app, is the fallback during that window.
  - SMS-originated requests get guest permissions instead of the house's current mode.
  - The mode service now loads admin-backend's guest-mode settings on startup: with calendar guest mode enabled, every satellite is in guest mode during an active booking, and the guest allowlist excludes `control`, so even lights need the owner PIN.
  - The owner voice-PIN override requires a PIN set in the admin UI and is refused from public and unauthenticated surfaces. A PIN set before this release (unsalted SHA-256) reads as "not configured" and must be set again once on the Guest Mode page.
  - `POST /mode/override`'s timeout is clamped to `OVERRIDE_MAX_TIMEOUT_MINUTES` (default 240).
  - Browser-facing LiveKit room tokens expire after `LIVEKIT_USER_TOKEN_TTL_MINUTES` (default 30) instead of 24 hours; server-side Athena participant tokens are unchanged.
  - A guest asking something the classifier can't place ("unknown" intent) now gets an answer, still limited to the guest's allowed tools. Every guest intent refusal reads "Sorry, I can't do that in guest mode."
- **Voice commands.**
  - Device-state questions are answered read-only. The admin feature `state_question_routing_kill_switch` (seeded disabled by `061`) reverts the routing; even with it on, a question never changes a device without a confirmation.
  - Commands phrased like a status ("turn the office lights on", "set the temperature to 70") now execute; previously they got a status report and nothing happened.
  - One utterance that would write to more than `HA_WRITE_FANOUT_CONFIRM_THRESHOLD` (default 6) entities without saying "all" or naming the room gets a confirmation; a plain command is stopped above `HA_WRITE_FANOUT_HARD_LIMIT` (default 18). Set both to `0` on the orchestrator to disable the limits. A non-zero hard limit below the threshold logs an error and both fall back to the defaults.
  - With no "good night" or "leaving"/"goodbye" scene configured in Home Assistant, those phrases no longer turn off every light (and, for leaving, lock every lock); they answer with the commands to say instead. Configure the scene to keep the one-phrase behaviour.
- **jarvis-web.**
  - Configure the home network before upgrading, or every browser gets the sign-in page (`401 sign_in_required`). Behind a reverse proxy set `JARVIS_LOCAL_NETWORKS`, `JARVIS_ALLOWED_HOSTS` and `TRUSTED_PROXY_CIDRS`; for browsers reaching it directly set `JARVIS_DIRECT_CLIENTS=true` instead. Optional: `JARVIS_LOCAL_EXCLUDE`, `JARVIS_GUEST_NETWORKS`.
  - `JARVIS_PUBLIC_MODE` was removed: `household` stops jarvis-web at startup, and other values are ignored with a warning.
  - Sign-in from the internet needs edge mode (`optional/jarvis-web-edge-auth.yaml`): `JARVIS_EDGE_ATTESTATION_SECRET` (at least 32 characters) and `JARVIS_HOUSEHOLD_GROUPS`, plus the home-network settings above unless `JARVIS_EDGE_SIGN_IN_ONLY=true`. A placeholder, short or reused secret, or an edge setup that couldn't serve the household, stops jarvis-web at startup.
  - Owner-only routes answer `401` to an unauthenticated caller (was `403`) and `403 guest_stay_active` to a home-network caller during a guest stay.
  - CORS is off by default (`JARVIS_CORS_ORIGINS` lists exact origins; `*`/`null` are refused). WebSockets need an `Origin` listed in `JARVIS_ALLOWED_HOSTS`. Reload open tabs after upgrading: an old tab's controls get `403 reload_required`.
  - The only anonymous responses are `/api/health` (now just `{"status": ...}`), the sign-in page and the embed relay. The API docs are served only with `JARVIS_ENABLE_DOCS=true`.
  - `JARVIS_WEB_URL` is empty by default on the orchestrator, which skips its appliance, sensor and media lookups; set it (e.g. `http://jarvis-web:3001`) to keep them.
- **chat-embed (website chatbot).** Deploy it before the new jarvis-web, with `JARVIS_RELAY_KEY` (at least 32 characters) set to the same value on both; a new jarvis-web refuses an old chat-embed. An embedding widget must send back the `session_id` it was given to continue a conversation. Set `CORS_ORIGINS` to your site's origin (the default allows no browser) and `TRUSTED_PROXY_CIDRS` if a proxy fronts it (without a usable visitor address it answers `503`). `RATE_LIMIT_RPM` defaults to 20 (was 30). Use https for its upstream URLs unless jarvis-web is in-cluster or on loopback. Build it from the repository root (`docker build -f apps/chat-embed/Dockerfile .`).
- **Calendar sources and guest-mode bookings.**
  - **Rotate your iCal export URL** (for example, regenerate the Lodgify calendar export link): the calendar-sources list returned it to unauthenticated callers until now. A Lodgify source with an API key never reads the export.
  - Every calendar-sources route except the source-type list needs a signed-in user (or a user `X-API-Key`) and refuses `X-Service-Key` with `401`.
  - The list returns a masked feed URL (`https://host/…`); only `GET /api/calendar-sources/{id}` returns the full URL. Feed URLs must be `https://`.
  - `POST /api/calendar-sources/test-url` takes `{"url": ..., "source_type": ...}` as a JSON body; a `url` query parameter gets `422`.
  - A source's sync interval must be 5–1440 minutes; an existing smaller value syncs every 5 minutes.
  - With calendar guest mode enabled, the mode service reads bookings from admin-backend by default (`MODE_BOOKINGS_SOURCE=auto`; `admin` and `ical` are the alternatives). When those bookings can't be read and the last good copy is older than `MODE_BOOKINGS_MAX_AGE_SECONDS` (default 6 h), the house reports `degraded`.
  - A legacy calendar URL using `http://` no longer loads (https only, through the SSRF guard; private hosts need `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`). Its events longer than 60 days, or whose end isn't after their start, are dropped. With a Lodgify API key set, don't point that URL at the Lodgify export: its turnover-day slices add guest time.
  - Date-only and floating booking times are computed in `DEFAULT_TIMEZONE`; set it to the property's zone before upgrading. Existing rows keep their old instants until their source re-syncs, which also reclassifies owner blocks as `blocked`. Changing `DEFAULT_TIMEZONE` later needs a restart of admin-backend and the mode service, then a re-sync.
  - Duplicate rows from earlier syncs aren't removed; delete them on the Guest Mode page, then call `POST /api/calendar-sources/sync-guest-sessions` (signed in) to cancel their guest sessions.
  - Rebooking residual: if a stay is cancelled and the same dates are rebooked under the same title before a successful sync runs in between, the next sync matches the cancelled row and the stay reads as owner. Restore the booking on the Guest Mode page if that happens.
- **admin-backend memory.**
  - The image bakes in the embedding model and never downloads one at runtime; building it needs Hugging Face access once.
  - Raise the admin-backend memory request/limit to 512Mi/1Gi (was 128Mi/512Mi) before running the new image.
  - `/api/memories/internal/*` requires `X-Service-Key`. `GET /api/memories`, `POST /api/memories/search`, `GET /api/memories/guest-sessions/active` and `GET /api/memories/qdrant/health` require `X-Service-Key` or a signed-in owner or operator with `read`.
  - Memory text and search queries are limited to 8192 characters and summaries to 255.
- **Qdrant.** The manifest pins `qdrant/qdrant:v1.19.1` (was `v1.18.2`) and uses the `Recreate` update strategy. Snapshot the `qdrant-storage` PVC first. From an older pin, upgrade one minor version at a time; see the manifest's upgrade notes.
- **Semantic cache.** Entries are now partitioned by mode and guest, so existing entries become unreachable after the upgrade (one cold start).
- **Image builds.** `scripts/build-and-push.sh` refuses to push a tag the registry already has, including `latest`, unless `--force-tag` is passed; it also refuses when the registry check itself fails. `TAG` precedence is now `--tag` > caller-exported `TAG` > `config.env` > `latest`.

### Added

- Guest mode reads bookings from the admin-managed calendar sources (Lodgify and iCal, whatever the Calendar Sources page has configured) instead of only an in-process poll of a single legacy iCal URL, so the house switches to guest during every synced booking. The legacy URL still works as an optional, additive supplement. `MODE_BOOKINGS_SOURCE` (`auto`/`admin`/`ical`) selects the required source and `MODE_BOOKINGS_MAX_AGE_SECONDS` how long its last good fetch is trusted; when the required source has never loaded or has expired, the house goes `degraded` rather than `owner`. Per-source booking freshness is visible on `/health`, `/mode` (`bookings_sources`), and the admin Guest Mode page, which now shows the mode service's actual mode and reason, refreshes every 30 s, and warns when a legacy calendar URL is set but not being read. See `docs/CONFIGURATION.md`, "Guest-mode booking source".
- The Guest Mode page's Delete button works on calendar-synced bookings too, not just manually entered ones — useful for hiding a phantom or cancelled booking without waiting for the next sync.
- A write fan-out limit: above `HA_WRITE_FANOUT_CONFIRM_THRESHOLD` (default 6) distinct entities, or `HA_WRITE_FANOUT_HARD_LIMIT` (default 18) for a plain command, a request that doesn't say "all" (or name the room group or rooms) gets a confirmation on Home Assistant Assist, LiveKit, jarvis-web and SMS, and the exact wording to repeat on Wyoming satellites and the OpenAI-compatible endpoint. Confirmations expire after 60 seconds and only the caller that was asked can confirm.
- The memory vector collection is created and validated automatically: admin-backend creates it when it's missing, records the embedding model on it, and refuses to mix embedding models or use a collection of the wrong shape.
- The Memories page compares Postgres with the vector store by id (memories without a vector, vectors without a memory, memories not yet searchable) and shows whether they're in sync.
- An owner-only "Rebuild vectors" action on the Memories page embeds every memory that isn't searchable yet.
- `python -m app.services.memory_vectors reindex` rebuilds memory vectors from Postgres (run it as a one-off Pod; `--recreate` requires typing the collection name).
- A narrow, hard-coded public audience for anonymous callers (`caller_trust="web_public"`, used for an embedded website chatbot). It can ask only about the weather, news, recipes, what's streaming, and general questions; it can't control anything; it never receives a device-identified guest's name or any other request context except a location override; and it gets no base knowledge or home address, no memories, no semantic-cache reads or writes, and no web search by any path (tool, fallback, or retrieval). None of this follows the admin-configured guest profile, so widening what a rental guest may do never widens what an anonymous visitor may do. `caller_trust` also accepts `web_local` (a home-network browser); like `web_public`, it can't use the owner-PIN override.
- jarvis-web's guest network (`JARVIS_GUEST_NETWORKS`, for a rental's guest Wi-Fi) gets the UI and chat without sign-in, always in guest mode (even when the house is vacant or owner mode is forced), with view-only controls and only the reads the guest UI loads.
- jarvis-web supports sign-in from the internet through an auth proxy (edge mode): Traefik with an Authentik forward-auth outpost classifies each request as home, guest or signed-in and attests it with a shared secret (`JARVIS_EDGE_ATTESTATION_SECRET`, with `_PREVIOUS` for zero-downtime rotation). jarvis-web honours the class only with the right secret from a trusted proxy peer, and a home or guest class only when its own network rules and `Host` allowlist agree, so a misconfigured route can't make the internet "home". A signed-in user must be in a group listed in `JARVIS_HOUSEHOLD_GROUPS` (matched exactly on Authentik's `|`-separated groups header), or gets `403 not_household`. A placeholder, short or reused attestation value stops jarvis-web at startup, and so does edge mode that couldn't serve the household (no `TRUSTED_PROXY_CIDRS`, no usable `JARVIS_LOCAL_NETWORKS` entry, or no `JARVIS_ALLOWED_HOSTS`; `JARVIS_EDGE_SIGN_IN_ONLY=true` for a deployment with no home network), so a bad rollout stalls instead of answering the household with 401. A custom identity or groups header name must also be stripped at the edge (`JARVIS_EDGE_HEADERS_ACK_STRIPPED` listing exactly those names once it is). New optional manifests: `optional/jarvis-web-edge-auth.yaml` (routes, strip/forward-auth/attest Middlewares, an optional sign-in host that always requires sign-in, and the guest-network route) and `optional/networkpolicy-jarvis-web.yaml`; the standalone example uses the same shape. The template's forward-auth sets `trustForwardHeader: false`: with it on, a client behind a trusted proxy address can forge `X-Forwarded-Host` and pass the outpost without a session.
- jarvis-web accepts an embedded website chatbot's messages (relayed by chat-embed) only with `X-Jarvis-Relay-Key` equal to `JARVIS_RELAY_KEY` (off by default; at least 32 characters, shared with nothing else, or jarvis-web won't start). A relayed message is always the narrow public audience, whatever other evidence it carries, and a wrong key is `401` with no fall-through. Each relayed visitor (`X-Jarvis-Relay-Client`, required; `400` without it) gets `JARVIS_RELAY_REQUESTS_PER_MINUTE` (default 20) and all relayed traffic `JARVIS_RELAY_GLOBAL_PER_MINUTE` (default 300), per replica; over budget is `429` with `Retry-After: 60`, before any upstream call. Relayed conversations use `pub-` session ids that jarvis-web mints and binds to the visitor: sending back the returned id continues the conversation, and any other id (another visitor's included) starts a new one. chat-embed checks the binding itself before forwarding an id.
- The Jarvis page notices when its session ends (an auth proxy's sign-in redirect, or a 401): it shows one banner ("Your sign-in has expired." with Sign in again, or, on the home network, "This device doesn't look like it's on the home network anymore." with Try again), shows "Signed out", stops its health and mode polls, the Music Assistant and Sendspin reconnect loops and LiveKit, and keeps an unsent or failed message for after the reload. A network hiccup is still just offline. During a guest stay the controls and mode menu are view-only with a note saying so (plus a Household sign-in link when a sign-in host is configured); on the guest network the note says the controls are view-only there. The mode badge appears only once the mode is known, a signed-in user's name and a Sign out link are shown, and a rate-limited message is marked "Not sent" with a note in the conversation and the text kept in the input. Push-to-talk works for every home-network, guest-network and signed-in browser (its turns are refused whatever chat would refuse; `JARVIS_VOICE_REQUESTS_PER_MINUTE`, default 30, per client; spoken text capped at `JARVIS_TTS_MAX_CHARS`, default 5000; uploads decoded as WebM only), and the music socket and always-on voice (LiveKit) only start for callers who may control.
- `scripts/build-and-push.sh` refuses to push an image tag the registry already carries unless `--force-tag` is passed. It checks with `docker manifest inspect`, fails closed on any registry error other than a genuine "not found", and `REGISTRY_INSECURE=1` (documented in `config.env.example`) adds `--insecure` to that check only, for a private plain-HTTP registry.
- `.github/workflows/image-scan.yml` builds every Python image (enumerated by `scripts/list-python-images.sh` from `scripts/service-defs.sh`) and scans each with `scripts/trivy-scoped-scan.sh` on every PR touching a Dockerfile or requirements file. The scan script (Trivy `0.74.0`, pinned) replaces a blanket `.trivyignore`: a finding is allowed only when its vulnerability, package and version match one of two documented pip-vendored rows (`GHSA-6v7p-g79w-8964`/`msgpack`/`1.1.2`, `CVE-2025-47273`/`setuptools`/`70.3.0`) and no instance of that package at that version exists on disk; a malformed report exits `2` (scan error) rather than `1`.

### Changed

- The admin-backend image bundles a hash- and revision-verified embedding model and never downloads one at runtime (the build needs Hugging Face access once).
- Memory text and memory search queries are limited to 8192 characters and summaries to 255.
- The admin-backend memory request/limit is 512Mi/1Gi, to hold the embedding model.
- The Qdrant manifest pins `v1.19.1` (was `v1.18.2`), documents the minor-by-minor upgrade path, and uses the `Recreate` update strategy (one pod on a ReadWriteOnce volume).
- Notification preferences are no longer changed by a question or an unclear request. Only a command ("stop the morning notifications") or an explicit wish ("I don't want morning updates", "I'd like morning updates", "opt out", "unsubscribe") that names one direction changes a setting; a question gets instructions, and an unclear request is asked which way. Guests and anonymous callers can't change them at all.
- A guest asking something the classifier can't place ("unknown" intent) gets an answer instead of "that feature is not available in guest mode"; it can still reach only the guest's allowed tools. Every guest intent refusal reads "Sorry, I can't do that in guest mode."
- With no "good night" or "leaving"/"goodbye" scene configured in Home Assistant, those phrases no longer fall back to turning off every light (and, for leaving, locking every lock). They answer with the commands to say instead ("turn off all the lights", then "lock all the locks").
- jarvis-web's owner-only routes answer `401` to an unauthenticated caller (was `403`) and `403 guest_stay_active` to a home-network caller during a guest stay. `/api/welcome` returns `capabilities` (`household_read`, `control`, `control_reason`, `signed_in`, and `signin_url` during a stay when `JARVIS_SIGNIN_URL` is set).
- The Jarvis page's warnings meet contrast, the header fits a 375px screen, decorative symbols are hidden from screen readers, and view-only covers the Apple TV remote and app buttons.
- `scripts/build-and-push.sh` passes `--pull` on every `docker build`, so a build always starts from the current upstream base image tag.

### Fixed

- Asking about device state ("are the office lights currently on or off right now") could turn the devices off: the smart-home intent extractor sometimes read a question as a command, and any reply it couldn't parse became `turn_on`/`turn_off`. Questions are now answered from Home Assistant state under a read-only permission scope that refuses every write for that request, whatever the LLM returns. This includes questions that reach the music and TV handlers ("did you turn off the TV" used to power the TV off). An emergency revert is available as the admin feature `state_question_routing_kill_switch` (enable it to turn the new routing off); even then, a question never changes a device without a confirmation (thermostat, bed warmer, motion overrides and scenes included), and TV or music questions are answered from state. TV questions are answered from the TV's state, a question about an unrecognised device asks which one is meant instead of reporting on the lights (the reply is answered as that question and never continues an earlier command), and house-wide status reports no longer answer questions about a different device (e.g. "garage door status" got the door-lock report).
- Commands phrased like a status ("turn the office lights on", "leave the lights on", "set the temperature to 70") execute. Previously they got a house-wide status report back and nothing happened.
- Wyoming satellites spoke an empty answer to every request: the bridge read a `response` field, but the orchestrator returns its text in `answer`. The bridge now reads `answer`.
- The orchestrator's smart-home controller no longer calls a hardcoded `localhost:3001` for jarvis-web appliance, sensor and media lookups; set `JARVIS_WEB_URL` to enable them.
- A guest query whose intent isn't gated by a specific control node but is still blocked by the guest permission check (e.g. a Tesla-vehicle query) no longer 500s — the response the orchestrator was building for that refusal didn't match its own response model.
- A vector-store failure no longer discards a new memory. It's saved, marked not searchable, and made searchable again automatically when the store recovers, including after the vector collection itself is lost. Memories that failed to save while the vector collection was missing were never written anywhere and can't be recovered.
- Admin-created, edited and promoted memories never point at vectors that don't match their saved text.
- Semantic memory search returns only live memories, using their current saved text.
- When semantic search is unavailable, memory recall falls back to keyword matching, which keeps numbers and codes (a door code, a Wi-Fi password) whole.
- Forgetting a memory removes it from search and recall; the stored record is kept, marked deleted.
- "Sync all" on the Calendar Sources page now triggers a sync for every enabled source (it previously reported a count but did nothing).
- Date-only and floating (no explicit timezone) booking times from a calendar feed are localised to the configured property timezone instead of being silently treated as UTC — a real check-in/check-out time could previously be off by several hours depending on the deployment's timezone.
- A feed entry marked as a block (`Blocked`, `Closed Period`, `Not available`, etc. — an owner blocking dates for personal use) is no longer counted as a guest stay.
- Block labels are matched per platform and on the whole summary, so a guest whose name contains "Blocked" or "Unavailable" is a stay, and Lodgify's `Closed`, `Closed Block` and `Owner Block` are now blocks. `Reserved` is never a block.
- Editing a blocked booking on the Guest Mode page no longer clears its status; the Edit dialog offers "Blocked (not a stay)" and the API rejects unknown status values.
- The mode service's legacy iCal URL is fetched through the same SSRF guard as calendar sources (`https://` only; private hosts need `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`), and fetch failures no longer log the calendar URL.
- Calendar syncs no longer create duplicate or phantom bookings. A Lodgify source with an API key syncs only from the API: when the API call fails or the key can't be read, the sync fails and keeps the existing bookings instead of falling back to the iCal export, whose fresh-per-fetch UIDs used to add a full new set of rows (including turnover-day slices and owner blocks) on every fallback. iCal events are also matched to the source's existing rows by their local check-in and check-out dates, so a feed that changes UIDs on every fetch no longer adds rows.
- Two admin-backend replicas, or a manual sync during a scheduled one, can no longer sync the same calendar source at the same time: each sync holds a per-source lease, and a second request reports that a sync is already running.
- A synced booking you delete or cancel stays deleted or cancelled when the feed lists it again, and its guest session is cancelled. Guest sessions are no longer created for deleted bookings, and restoring a booking restores its session. Deleting a calendar source cancels its current and upcoming guest sessions. A failed guest-session update after a sync is shown on the source card instead of being logged and ignored.
- An event whose ID is already used by another calendar source (or by a row whose source was deleted) is kept, stored under a source-specific ID, instead of overwriting or moving that other source's booking. The source card says how many events this applied to.
- The Calendar Sources page's Edit button no longer opens the form with the masked URL when the full source can't be loaded, and a failed sync shows its reason instead of "undefined".
- The Guest Mode page had an "Owner PIN must be set again" notice but no way to set one. A dedicated Owner PIN section (6-digit input, confirmation input, client-side validation) now saves the PIN and shows whether one is configured. The server validates the format (exactly six ASCII digits) and returns `422 owner_pin_format` otherwise; previously a malformed PIN saved and was then permanently unverifiable. Both PIN inputs clear on every exit from the form.
- chat-embed's streaming chat works again: it sent a body jarvis-web rejected (`422`), so every streamed message failed. It now sends the relay key and the visitor's address (`JARVIS_RELAY_KEY`, `X-Jarvis-Relay-Client`), resolves visitors through `TRUSTED_PROXY_CIDRS` only (and refuses locally with `503` rather than share one bucket when it can't), limits each visitor to `RATE_LIMIT_RPM` (now 20, was 30), maps jarvis-web's `429` to its own `429`, ends every stream with exactly one `done` or `error` event (`reason: rate_limited` when limited), logs `jarvis_relay_rejected` when the relay keys don't match, and never forwards the browser's `Authorization` or cookies. Its CORS is off until `CORS_ORIGINS` lists your site (it allowed any origin), `*`/`null` are refused, and it no longer allows credentials. Its image now builds from the repository root.
- `scripts/deploy.sh` refuses to apply manifests that still carry an unconfigured placeholder (any `YOUR_*` or `CONFIGURE_ME*` token) before running any `kubectl` command, so a placeholder manifest can no longer overwrite a live, already-configured deployment. Pass `--allow-placeholders` to apply against a fresh, unconfigured namespace.
- `scripts/build-and-push.sh`: `config.env`'s `TAG=` no longer overrides a caller's `--tag` or exported `TAG` (a push could silently land on a stale tag); the effective registry and tag are printed before the first build. A shell scoping bug that built the tag-collision check's image name with an empty tag is fixed.
- RAG self-registration no longer overwrites a service registry row's host or type with `localhost`/`api` on startup — it sends only the fields it owns, with an optional `SERVICE_REGISTRY_ENDPOINT_URL` for an explicit endpoint. The registry upsert route treats endpoint-location fields as partial updates.
- RAG self-registration posts the `<name>-rag` registry name and a `host_label` hint, matching the convention seeded registry rows use; connectors registering with their bare short name (e.g. `weather`) were rejected with 422 and never landed in the registry. The upsert route also matches an existing row by host when the posted name doesn't, so a self-registration ping never duplicates or renames a row, and the registration log reports failure honestly.
- RAG self-registration normalises a connector's `SERVICE_NAME` (lower-case, strip `-`/`_`) before applying that convention, so hyphenated names (`price-compare`, `site-scraper`) match their seeded rows. `SERVICE_REGISTRY_NAME` overrides the derived base for a row that doesn't follow the convention. A static check fails CI when a connector's registration doesn't normalise to a seeded row or two names normalise to the same base, and the derived name and host label are checked against the admin route's own rules before anything is sent.
- DEV_MODE's service registry seed types RAG rows as `rag`; previously every seeded row was typed `api`.
- The service-control inventory cache is invalidated even when releasing the action lease raises.

### Security

- Home Assistant writes are authorized against the request's server-derived permissions before they're sent. Previously guest and device-identified-guest requests could lock, unlock, open, or close restricted devices; a client-supplied `mode` could claim owner; a mode-service outage granted owner; the mode service's override endpoint granted owner without a PIN and without authentication; anyone could exhaust the owner-PIN attempts from the public chat; and unauthenticated jarvis-web callers were served in the household's mode and could drive climate, media, appliances, music, and voice rooms directly.
- The mode service requires `X-Service-Key` on every route except `/health` (`MODE_SERVICE_INGRESS_AUTH`, staged via `warn` before `enforce`); previously any caller on the pod network could query or override mode. `POST /mode/override`'s timeout is clamped to `OVERRIDE_MAX_TIMEOUT_MINUTES` (default 240); it was unbounded. The owner PIN is verified only by admin-backend, with a per-caller-trust-tier lockout (`MODE_OVERRIDE_LOCKOUT_THRESHOLD`, `MODE_OVERRIDE_LOCKOUT_MINUTES`).
- The gateway's simple-command fast path, which calls Home Assistant directly, runs only for lights and only while the mode service confirms owner mode (fail-closed); anything else goes through the orchestrator's permission checks.
- Browser-facing LiveKit room tokens expire after `LIVEKIT_USER_TOKEN_TTL_MINUTES` (default 30) instead of 24 hours.
- A guest's intent allowlist is enforced on every entry path, before any routing: the streaming chat paths previously let a disallowed intent reach tools, retrieval and web search, and only `/query` checked it afterwards. Control, music, TV and notification-preference requests are still refused by their own handlers, with their specific wording, except that an anonymous caller never reaches those handlers at all. The control handler checks the control permission before anything else, so a caller without it (a guest, by default) can no longer get presence ("is anyone home"), sensor readings or device status from it. A refused request is never retried through web search.
- A tool call the model emits is executed only if the caller was entitled to be offered that tool.
- The semantic cache is partitioned by the effective mode and, for a device-identified guest, by guest: an owner's cached answer is no longer replayed to a guest, or one guest's to another.
- A conversation session is bound to whether its caller was anonymous: an anonymous caller presenting another caller's session id gets a fresh session instead of that session's history, and never overwrites it. Anonymous sessions always get a `pub-` id.
- jarvis-web fails closed: it serves a browser without sign-in only from the home network, and answers every other caller `401 sign_in_required` (with `WWW-Authenticate: Jarvis`) on everything except its health checks, static files and a fail-closed sign-in page, without calling the orchestrator, Home Assistant or admin-backend. Previously every unauthenticated caller, including the internet, could read sensors, occupancy, media, appliance and climate state, the current guest's name, and chat as a guest. The home network is `JARVIS_LOCAL_NETWORKS` (the address the trusted proxy in `TRUSTED_PROXY_CIDRS` appended, or the TCP peer with `JARVIS_DIRECT_CLIENTS=true`), minus `JARVIS_LOCAL_EXCLUDE`, with no Cloudflare headers, and only for host names in `JARVIS_ALLOWED_HOSTS` (DNS-rebinding guard). Entries that would let a proxy, the pod itself or a NAT gateway count as home are dropped at startup with an ERROR.
- jarvis-web's 29 direct device, mode and LiveKit write routes (HTTP and both WebSocket proxies) require a signed-in owner or operator (or the home network while the house is in owner mode); an unauthenticated caller gets `401 sign_in_required` (or a WebSocket close before accept) instead of being served as the household owner.
- jarvis-web's CORS is off by default (it reflected any origin with credentials); `JARVIS_CORS_ORIGINS` lists exact origins, and `*` / `null` are refused. Every mutating request from the page carries `X-Jarvis-Request: 1`; one without it gets `403 reload_required`, and WebSockets require an allowlisted `Origin`.
- jarvis-web's chat session ids are minted by jarvis-web and bound to the browser that started them (an httponly cookie), so presenting someone else's session id starts a fresh conversation instead of resuming theirs.
- Uncached jarvis-web Bearer checks are limited to 10 a minute per client, and its token cache is bounded.
- The orchestrator's reads of jarvis-web (appliances, sensors, media) send `X-Service-Key`, and only to an in-cluster or private `JARVIS_WEB_URL`; jarvis-web accepts that key for those household reads only, and never the placeholder key.
- admin-backend's memory routes are no longer open. `/api/memories/internal/{search,create,forget}` require `X-Service-Key` (401 when missing or wrong, 503 when the server has none configured), and `GET /api/memories`, `POST /api/memories/search`, `GET /api/memories/guest-sessions/active` (which returned the current guest's name, email and stay dates) and `GET /api/memories/qdrant/health` require `X-Service-Key` or a signed-in owner or operator with the `read` permission. The orchestrator's memory client sends its `SERVICE_API_KEY`, and the admin Memories page sends its sign-in token.
- Memory scope is decided in one place and fails closed: a guest without an identified guest session can read only global memories, can't delete any, and never creates one (previously such a turn could create owner-scoped memories and delete global ones); a guest with a session deletes only that session's memories; any mode other than `owner` is treated as a guest, and an internal memory call that omits `mode` is treated as a guest rather than the owner. A guest's semantic search without a session returns global memories instead of a 400.
- The Calendar Sources API's list, update, sync and guest-session sync routes require a signed-in user; they were open to anyone who could reach admin-backend, including the route that changes a source's feed URL. Every calendar-sources route except the source-type list needs `read` (list, view, test) or `write` (create, edit, delete, sync, Sync all, guest-session sync), and refuses an `X-Service-Key` header with 401. Creating, editing and deleting a source, and viewing its full feed URL, are recorded in the audit log.
- The calendar-sources list no longer returns feed URLs, which embed an access token; it returns `https://host/…` only, and just `GET /api/calendar-sources/{id}` (signed in, `read`) returns the full URL. Feed URLs must be `https://`, and the masked value can't be saved back over the real one.
- The Calendar Sources page's Test button sends the feed URL in the request body instead of the query string, which access and proxy logs record; `POST /api/calendar-sources/test-url` refuses a `url` query parameter with 422.
- Calendar sync errors and logs no longer include exception text, which could echo the feed URL, its token or the Lodgify API request; they name the error type and HTTP status only.
- The admin-backend session ID rotates on every login path (local, demo-mode, OIDC callback), closing a session-fixation gap where a cookie value set before authentication remained valid afterward.
- The Ollama SSRF gate also covers the remaining operator-configured Ollama probes — model discovery and the admin panel's Ollama test actions. A not-in-cluster carve-out keeps `localhost`/private-host Ollama working for bare-metal development; an in-cluster Ollama still needs its host allowlisted via `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`. Startup logs a non-fatal `ollama_url_blocked_by_ssrf_guard` warning when the configured Ollama URL would be blocked.
- Configured URLs (Ollama, Redis, Qdrant, and registry endpoints) are redacted to `scheme://host:port` before being written to admin-backend logs, so credentials embedded in a URL are never logged.
- RAG services send `X-Service-Key` when self-registering and unregistering with the service registry; both calls previously had no header and were rejected with 401.
- The gateway's per-source new-conversation limiter resolves the caller through one shared parser (`src/shared/client_throttle.py`), also used by jarvis-web and chat-embed. An unparseable `X-Forwarded-For` hop ends the walk at the immediate peer instead of becoming the rate key, so a garbage value can't mint a fresh bucket; a header longer than 2 KB or 20 hops is parsed from its right-hand end, so padding can't push the proxy-appended hops out of reach. The gateway reads every `X-Forwarded-For` header line, keys an IPv6 client on its /64, and treats an address carrying a zone id (`fe80::1%eth0`) as unparseable.
- Every `python:3.11-slim` Dockerfile (all 29 Python images) runs `apt-get upgrade` immediately after `FROM`, keyed on an `APT_CACHE_BUST` build argument that `scripts/build-and-push.sh` and the image-scan workflow set to the current date, so Debian security fixes land even when the base image digest hasn't moved. The RAG Dockerfile generator template carries the same steps, and every `setuptools==` pin is at or above `78.1.1` (CVE-2025-47273).

---

## [0.4.0] - 2026-09-28 — OSS readiness, service control, security hardening

The first release after the house-to-OSS migration. Every deployment-specific value is now configuration, the orchestrator and Control Agent require service authentication, and the admin panel manages services through a real manager (Kubernetes / Control Agent / none) instead of a stale flag.

### Upgrade notes (read before deploying)

- Run `alembic upgrade head`: the `rag_services` table is renamed to `athena_service_registry`; a data migration clears legacy maintainer OIDC values from seeded databases.
- `SERVICE_API_KEY` is required: by default the orchestrator rejects `/query`, session, and admin-maintenance calls without a matching `X-Service-Key` header (`ORCHESTRATOR_INGRESS_AUTH` defaults to `enforce`); set it to `warn` first if you need to find a caller your own integrations add before relying on the default. Every mutating Control Agent route also requires the header.
- `/api/base-knowledge/public` now requires service or OIDC authentication.
- The Control Agent manages nothing unless `CONTROL_AGENT_SERVICES_FILE` is set; `CONTROL_AGENT_ENABLED` defaults to `false`.
- Service Control: actions on a row with no manager now return 409 `action_not_available` (previously 200 `success:false`); critical targets require the owner role plus a typed confirmation; the restart "Quick Actions" macros are removed.
- Local login now returns 401 for every failure; the old 403 "account inactive" response is gone.

### Added

- Service Control for Kubernetes: per-row manager resolution, a unified `GET /api/service-control` envelope, an opt-in scale-only Kubernetes adapter (`SERVICE_CONTROL_K8S_ENABLED`, name-scoped Role under `manifests/athena-prod/optional/`), a cross-replica lease, a shielded restart (scale-to-0, bounded wait, guaranteed scale-back), typed confirmation, a target-collision guard, and the Ollama panel routed through the same resolver.
- Service registry as the single source of truth, with a background health poller, leader election across replicas, TCP health-check mode, a registry Edit modal, and partial-update upsert.
- Transit tool: `search_transit` now reaches a real transportation route; Memory & Context → Base Knowledge settings save and reload correctly.
- Orchestrator benchmark observability and a tool-calling A/B harness.
- Twilio SMS webhook signature validation.
- A maintainer-leak CI gate (`scripts/check-maintainer-leaks.py`) with a generic private-IP rule, plus frontend-escaping CI gates (uniqueness, callee-sink, and load-order checks).
- Configuration: 42 centralized `AthenaConfig` env vars, including `HA_SATELLITE_ROOM_MAP`, `HA_TV_ENTITIES`, `HA_MUSIC_PLAYERS`, `HA_BED_WARMER_ENTITIES`, `HA_LIGHT_GROUPS`, `MUSIC_ASSISTANT_URL`, `SEARXNG_BASE_URL`, `TRANSIT_*`, `COMMUNITY_EVENTS_SOURCES`, `DEFAULT_AMTRAK_STATION`, `TRUSTED_PROXY_CIDRS`, `ORCHESTRATOR_INGRESS_AUTH`, `SERVICE_CONTROL_K8S_ENABLED`.

### Changed

- Mission Control is the landing tab; its voice-health card, `/api/status`, and quick-stats are registry-driven instead of hard-coded to five named services.
- Dashboard health counts enabled services only; a disabled row no longer degrades the overall system status.
- Orchestrator refactor: state, helpers, mode/permission, URLs, metrics, and ten pipeline nodes extracted from `main.py`; added a validator training-knowledge bypass.
- Admin-frontend escaping consolidated into one `escape-html.js`.
- RAG services take every external dependency from configuration; OSS dependency cleanup across all 23 services.
- Control Agent process list, watchdog exclusions, and container allowlist now come from a services file, and processes are launched without a shell.

### Fixed

- Post-cutover voice defects: RAG URL parity, streaming think-disable parity, a temporal-location filter gap, missing LiveKit/numpy dependencies, sequence-timing false positives, and a dining keyword gap.
- Home Assistant's `/ha/conversation` route could 500 after the requested device command had already executed (a fast-path formatting bug, not a request failure).
- Gateway simple-command fast path: a failed Home Assistant call no longer returns a canned success; the request falls through to the orchestrator.
- The orchestrator's config loader and RAG client never sent the service key, so admin-panel configuration changes silently never took effect.
- Dashboard badges rendered "undefined"; System Configuration cards always read Offline.
- The Ollama models endpoint 500'd on a `/api/ps` failure.
- Guest-name XSS in the admin frontend.
- Skip-guard and dependency-remediation fixes.

### Security

- Orchestrator ingress authentication on 13 routes.
- Control Agent inbound authentication on 17 mutating routes.
- SSRF guard: a shared `safe_request` at every user/admin URL fetch chokepoint, full path+query validation, gates on quick-stats, voice-test probes, and every admin-backend Ollama request, and URL validation at the Ollama-URL write boundary.
- Auth hardening: per-IP rate limit, per-user lockout, constant-time failure paths, service-auth startup gates, and deferred auth hardening (an OIDC issuer-validation opt-out, short-lived WebSocket tickets).
- The public base-knowledge endpoint is now gated.
- No maintainer values in runtime code, enforced by a CI leak gate.

### Removed

- The service restart "Quick Actions" macros (`voice-pipeline-restart`, `full-stack-restart`, `llm-refresh`) and their unaudited direct route calls.
- The hard-coded Control Agent process list.
- Maintainer-identifying values (home network details, personal name, hostnames) from tracked runtime code.

### Deprecated

- `athena_service_registry.is_running` is no longer written; run state is derived from health instead.

### Details

#### Added: Service Control resolves a real manager per row (Control Agent / Kubernetes / none) instead of guessing from a stale `is_running` flag

- **Added — `GET /api/service-control` envelope**: `{services, counts, control_agent, kubernetes}` replaces the old bare list. Each row's run state (`running`/`stopped`/`disabled`) is derived from health + `enabled` (`app/utils/service_state.py`), never the `is_running` column, which is now unwritten (deprecated, kept only for the startup schema gate). **Behavior change**: a row with no manager now returns 409 `action_not_available` on a lifecycle action instead of 200 `success: false`.
- **Added — `app/services/service_managers.py::resolve_manager`**: per-row manager resolution, Control Agent (host-gated) then Kubernetes then `none`, with server-side grouping (`row.group`) and D10's action table.
- **Added — `app/services/k8s_control.py`**: a scale-only Kubernetes adapter (`deployments` list, `deployments/scale` get/patch — no `deployments` PATCH anywhere, so no pod-template write is ever possible through this path). Restart is scale-to-0, a bounded 60s wait, then a shielded scale-back in `finally` — brief downtime, not a rolling restart. Opt-in via `SERVICE_CONTROL_K8S_ENABLED` (default `false`) plus `manifests/athena-prod/optional/admin-backend-rbac.yaml` and its automount patch file.
- **Added — cross-replica lease** (`app/services/service_control_settings.py`, `system_settings` key `service_control.lock.<deployment>`, 90s TTL): serializes k8s actions across admin-backend replicas; contention is 409 `action_in_progress`. Remembered replica counts live in `service_control.replicas.<deployment>` (clamped 1-10, never stores a 0).
- **Added — owner gate**: a new `manage_infrastructure` permission (owner role only) plus a server-enforced typed confirmation (`confirm_name`, checked against the **resolved target's** name, never an alias row's own name) guard critical targets — the named core-service set, any Kubernetes-resolved **or Control-Agent-resolved** row outside the `rag` group (the same fail-safe applied identically to both managers), and Ollama under any manager. The operator types the Deployment label for Kubernetes, the container name or `process:<port>` for a Control-Agent-managed row, or `ollama` for Ollama — never a row's own display name. The gate is checked before action-availability, so a non-owner always gets 403 `insufficient_role`.
- **Added — target-collision guard**: two registry rows resolving to the same Kubernetes Deployment block each other (`manager_note: "target_collision:<name>"`).
- **Fixed** — `/api/service-control/ollama/{start,stop,restart}` are now registered before `/api/service-control/{service_name}/{action}`; the parametrized route previously would have silently shadowed them once resolution against a matching registry row succeeded.
- All 12 `POST` routes in `service_control.py` now share a dedicated `service_control` rate-limit budget and audit their outcome (including refused 403/409s once resolution has run).
- **Added (Phase 3) — the Ollama card now goes through the same resolver as every other row.** `GET /ollama/health` gains `manager`/`manager_target`/`manager_note`/`native_actions`/`allowed_actions`/`confirm_required`/`confirm_name`/`row_name` and probes the resolved Ollama URL directly (`/api/version` + `/api/ps`, 5s timeout) — no Control Agent call, `status ∈ {healthy, idle, offline, error, ssrf_blocked}`. `POST /ollama/{start,stop,restart}` dispatch through `resolve_ollama_manager` into the same audited core the generic routes use: CA-managed calls the Control Agent, Kubernetes-managed scales the Deployment, and `manager == 'none'` is refused with 409 `ollama_not_manageable` (audited, zero CA/k8s calls) rather than silently calling `launchd_service_action` regardless of resolvability. **Behavior change**: Ollama is critical under any manager, so these routes now require the owner-only `manage_infrastructure` permission plus a typed confirmation.
- **Added (Phase 3) — SSRF runtime gate on every admin-backend Ollama request**: `check_ssrf_safe` runs before `/api/version`, `/api/ps`, `/api/tags`, and `/api/generate` (load/unload) in `service_control.py`, and before the reachability probes in `settings.py`'s `GET`/`POST /api/settings/ollama-url`. `POST /api/settings/ollama-url` also validates the URL at the write boundary (scheme, length, IMDS/link-local/multicast/unspecified/`.svc`/`.cluster.local` host blocklist) **before** the reachability test, with a loopback carve-out for bare-metal local dev outside a K8s pod.
- **Fixed (Phase 3) — `GET /ollama/models` no longer 500s on a `/api/ps` failure**: a `/api/tags` failure is a hard 502 `models_endpoint_unreachable` (there's no model list to render); a `/api/ps` failure alone degrades every model to `loaded: false` with an `X-Athena-Ollama-Ps: unavailable` response header, rather than failing the whole request.
- **Changed (Phase 4) — the Service Control admin page now reads the unified envelope.** Core/RAG/Infrastructure tables render from server-computed `row.group` (never re-derived from the row name), with a manager badge (Control Agent / Kubernetes / Managed externally / Protected) and native-state text (e.g. `1/1 pods`) alongside the run-state badge. A critical action opens a typed-confirmation dialog (must type the resolved target's exact name before Proceed enables) instead of a plain confirm. **Behavior change**: a 409 from a lifecycle action now surfaces the server's actual reason (`detail.error`) as a toast, rather than a generic failure message.
- **Removed (Phase 4) — the service restart "Quick Actions" macros** (`voice-pipeline-restart`, `full-stack-restart`, `llm-refresh`): they issued unaudited direct calls to the old per-service routes with no manager awareness, resolution, or owner gating, and had no wiring in `index.html`. The restart-history timeline (previously computed but never rendered — no `#restart-timeline` element existed) is now visible on the page, reading `/api/audit` for `service_start`/`service_stop`/`service_restart` events.
- **Changed (Phase 4) — the Ollama panel's health/model refresh is now a single `refreshOllamaPanel()` entry point** (`Promise.allSettled` over both loaders), so the status card and the models table never render from a stale pairing of one fresh and one lagging fetch. Fixed an unescaped `ollamaHealth.host` interpolation in the process.

#### Changed: Mission Control's voice-health card is registry-driven, not hard-coded to 5 named services

- **Changed — `admin/backend/app/routes/dashboard.py::get_dashboard_data`** no longer hard-codes exactly Gateway, Orchestrator, and 3 named RAGs (arbitrary, from the initial OSS commit). Core services (`gateway`/`orchestrator`) read their service-registry row when one exists (cached health, no probe), falling back to a gated live probe of `GATEWAY_URL`/`ORCHESTRATOR_URL` only when no row is registered. Every **enabled** registry row with `service_type='rag'` is included; **disabled rows are excluded entirely** (not shown, not counted -- distinct from the dashboard health-count fix's "shown labeled disabled" convention, since this card only ever showed configured, active services in the first place). `unconfigured` counts toward "needs attention" (excluded from `healthy_count`) but keeps its own literal status rather than being relabeled `unhealthy` -- not configured and actively failing are different claims. `critical_services` entries now include `last_error`. The bottom Service Status grid renders the same registry-driven set. `mission-control.js` needed no changes -- it was already fully data-driven off the response shape, with no hardcoded count or name assumptions.
- **Note**: the OSS dev-mode seed (`admin/backend/app/database.py::seed_oss_service_registry`) still tags every seeded row `service_type='api'` (a pre-existing hardcoded literal, not derived from data), so a fresh `DEV_MODE` install's RAG rows won't match `service_type='rag'` until corrected via the admin UI or the Control Agent's startup sync (which does set it correctly, `"rag" if "-rag" in service_name else "core"` in `src/control_agent/main.py::sync_registry_loop`). Flagged as a separate, pre-existing seed-data inconsistency -- out of scope here.

#### Fixed: quick-stats had no SSRF gate at all; the SSRF check validated only the host, never the actual request path/query

- **Fixed — `GET /api/dashboard/quick-stats` live-probed Gateway/Orchestrator with no `check_ssrf_safe()` gate**: unlike `get_dashboard_data`, this endpoint's probe loop had no SSRF check at all. Now gated the same way, logging `dashboard_quick_stats_ssrf_blocked` (`url_status="ssrf_blocked"`) and skipping the request when blocked.
- **Fixed — `app.utils.rag_urls.check_ssrf_safe()` always validated the host with `path=""`**, so the CRLF/NUL/traversal path check it delegates to never actually inspected a real request path or query string. Now derives and validates the full path (including query string) from the given URL.
- **Fixed — `voice_tests.py::test_rag_query` validated only `base_url` before appending user-supplied text into the request URL**: the final URL (with user text now URL-encoded via `urllib.parse.quote`, closing a request-line-injection vector the raw interpolation left open) is built first, then validated in full, then requested. Blocked responses now carry the exact marker `ssrf_blocked` (`detail={"error": "ssrf_blocked", "reason": ...}`, still 403) instead of only a human-readable `"SSRF guard: ..."` string. `test_full_pipeline`'s RAG-enhancement step's blocked marker is likewise now the exact string `"ssrf_blocked"` (`results["rag_error"]`), with the reason in a separate `rag_error_reason` field.
- New tests: `check_ssrf_safe` unit tests (real traversal-block proof; a validator spy proving the full path+query reaches it, not an empty placeholder); `test_quick_stats_blocks_ssrf_unsafe_gateway_with_no_network_call` (asserts zero network calls and the `ssrf_blocked` marker via captured logs).

#### Fixed: Mission Control voice-health card and voice-test RAG probes live-probed operator-resolved URLs without the health poller's SSRF/runtime-DNS allowlist

- **Fixed — `admin/backend/app/routes/dashboard.py`'s voice-health card no longer live-probes RAG services at all**: it now reads each RAG's `health_status`/`last_error` straight from the service-registry cache (same cached state `GET /api/service-registry/services` reads, kept fresh by the background poller) -- `healthy`/`unhealthy`/`disabled`/`pending`/`unconfigured` states preserved, `not_configured` when no registry row exists. This removes both the SSRF surface (a registry/env-resolved host is operator data only at write time; DNS can change afterward) and the per-page-load probe fan-out the poller was already doing on its own interval.
- **Fixed — Gateway/Orchestrator's remaining live probes, and every remaining live probe in `admin/backend/app/routes/voice_tests.py` (`test_rag_query`, `test_full_pipeline`'s RAG-enhancement step), now validate the resolved URL first**: new `app.utils.rag_urls.check_ssrf_safe()` imports (not reimplements) `app.services.health_poller._validate_service_url` -- the same allowlist (`HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`, the k8s control-plane hostname block, CRLF/NUL/traversal path rejection) the background poller and the service-registry quick-checks already use. A blocked URL reports `ssrf_blocked` (403 for `test_rag_query`, a `results.rag_error` string for `test_full_pipeline`) instead of ever calling out. New tests: `admin/backend/tests/test_voice_tests_ssrf_guard.py` (blocked case asserts the transport is never constructed; allowed case is a regression that the probe still fires when the SSRF check passes); `admin/backend/tests/test_rag_url_resolution.py`'s dashboard tests rewritten for the cache-read behavior (healthy/unhealthy/disabled/not-configured/pending).
- Dropped a stale `(studio)` comment in `admin/frontend/system-config.js` (registry names are bare now, not the pre-migration `"gateway (studio)"` shape).

#### Fixed: `/ha/conversation` 500'd with NameError AFTER a device command had already executed

- **Fixed — `src/gateway/main.py::ha_conversation`** raised `NameError: name 'HAResponseContent' is not defined` on the `ha_simple_command_fastpath` and `ha_intent_prerouting` (HOME intent) fast paths -- both execute the requested Home Assistant command (e.g. turning on a light) and only fail while formatting the reply, so hank reproduced this live as "light turns on, response 500s". `HAResponseContent`/`HASpeechContent`/`HAPlainSpeech` were never defined anywhere in this codebase (`git log -S` traces them to the initial OSS commit and no later commit ever added them) -- `HAConversationResponse.response` is a plain `Dict[str, Any]`, not a nested pydantic model. New shared `_ha_response_payload(speech_text, language)` helper builds that dict directly, matching the shape the orchestrator-routed success path already built correctly (also refactored onto the same helper, removing the duplication). New tests: `tests/unit/test_gateway_nonstream_continuity.py` (`test_ATHENA_115_*`) drive `/ha/conversation` end-to-end through the fastpath, prerouted-HOME, and mocked-orchestrator-reply paths, asserting a 200 with HA's expected response shape.
- **Images affected**: gateway (`src/gateway/main.py`).

#### Fixed: `/ha/conversation` fast path reports the real HA outcome; no canned success on failure

- **Fixed — `src/gateway/simple_commands.py::execute_simple_command`**: the `turn_on`/`turn_off` branches posted to Home Assistant's service-call endpoint and ignored the response entirely — httpx does not raise on a non-2xx status unless `raise_for_status()` is called, so an HA 403 still returned the canned "I've turned off the office light." with `success=True` even though the call never reached HA. Both branches now check `resp.status_code` and treat `>=400` as failure, logging `simple_command_ha_call_failed` and returning `None` (the existing "execution failed" signal) so the request falls through to the orchestrator instead of echoing a false success.

> **Campaign:** `2026-09-27-deliver-athena-dashboard-health-and-tcp-poller`

#### Fixed: a disabled service dragged the dashboard's overall health down

- **Fixed — `overall_health` and `healthy_services` counted disabled rows** (`admin/backend/app/routes/service_registry.py::get_all_services`): a service an operator deliberately turned off (still carrying a stale `unhealthy` from before it was disabled) could flip the dashboard to "degraded"/"unhealthy" even though nothing live was actually failing. Both are now computed over **enabled** rows only. New response fields `enabled_services` / `disabled_services`; `total_services` is unchanged (still counts every row) for backward compatibility. A disabled row's `health_status` in the response is now always the literal `"disabled"`, overriding whatever the poller last cached before it was turned off.
- **Changed — dashboard cards** (`admin/frontend/app.js`, `admin/frontend/index.html`): the middle stat card now reads "Enabled Services" (`N` or `N (K disabled)` when any rows are disabled) instead of "Total Services". The Core/RAG/Database service grid only lists enabled rows; disabled rows are grouped separately in a muted "Disabled" section at the bottom showing each row's `last_error`, and are never counted toward any group or the overall health rollup.

> **Campaign:** `2026-09-27-deliver-athena-dashboard-health-and-tcp-poller`
> **Commits:** `7de9989`, `fe4544b`, `bd77dfd`, `a2d5e32`, `20d5be6`

#### Added: TCP health-check mode for registry rows; registry Edit modal; partial-update upsert

- **Added — `protocol=tcp` health-check mode** (`admin/backend/app/services/health_poller.py`): a raw TCP connect to `host:port` within `HEALTH_POLL_TIMEOUT_SECONDS`, for services that don't speak HTTP or have no health endpoint. For a row whose `name` contains `redis`, the poller additionally sends a Redis `PING\r\n` after connecting and requires a `+PONG` **or** `-NOAUTH` reply (a password-protected Redis still proves it's up and speaking the protocol) within the same timeout; any other reply is `unhealthy` / `last_error=tcp_bad_banner`. Every other `tcp` row is a plain connect with no banner read. Shares the same `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` SSRF allowlist as `http(s)` rows. See `docs/CONFIGURATION.md`'s "Service Registry Health Checks" section for the full state matrix.
- **Added — registry row Edit modal** (`admin/frontend/service-control.js`): operators can edit an existing row's protocol/host/port/display_name/cache settings in place, rather than delete-and-recreate.
- **Fixed — the registry upsert (`POST /api/service-registry/services`) was a full replace, not a partial update**: `service_type`, `cache_ttl`, `timeout`, `rate_limit`, and `enabled` are now each applied only if the caller actually passed them; an omitted field keeps the row's current value (INSERT-only defaults still apply when creating a new row). **Behavioral change**: the Control Agent's startup-upsert never sends `enabled` in its payload, so before this fix every CA restart silently reset `enabled` to `True` for every managed row -- an operator-disabled service came back enabled on the next CA restart. It now stays disabled, as the operator set it.
- **Fixed — `admin/frontend/integrations.js` read the service-registry envelope (`{services, total_services, ...}`) as if it were the bare array itself**: `ragStatus?.find(...)` silently resolved `undefined` against a plain object, so a RAG service integration never read "connected" via this fallback path. Now reads `(ragStatus?.services || []).find(...)`. Commit `20d5be6` restored this fix's test, dropped during an earlier commit split in the same campaign.
- **Fixed — `GET /services/{service_name}` (single-service lookup) didn't apply the disabled-row override**: it now reports `health_status: "disabled"` for a disabled row, matching `GET /services`'s list behavior instead of a stale cached value.

#### Fixed: orchestrator's config_loader never sent X-Service-Key, so admin-panel conversation/clarification config silently never took effect

- **Fixed — `src/orchestrator/config_loader.py`'s `ConversationConfig` built its own `httpx.AsyncClient` with no headers at all**: every `/api/internal/config/*` route requires `X-Service-Key` (`admin/backend/app/routes/internal.py`), so every fetch (`get_conversation_settings`, `get_clarification_settings`, `get_clarification_types`, `get_sports_teams`, `get_device_rules`, `get_all_config`) and the `/api/internal/analytics/log` POST 422'd and silently fell back to hardcoded defaults on every cache-refresh cycle -- 5x/hr in production logs from the orchestrator pod. Now attaches `X-Service-Key: <SERVICE_API_KEY>` at client-construction time (same pattern as `main.py`/`self_building_tools.py`), with a one-time WARNING when `SERVICE_API_KEY` is unset. New MockTransport tests: `tests/unit/test_orchestrator_config_loader_service_key.py`.
- **Extended** `tests/unit/test_orchestrator_callers_send_service_key.py`'s static AST scan to `src/orchestrator` (previously unscanned), with `/api/internal/config/*` and `/api/internal/analytics/log` added to its gated-route pattern. Excludes `main.py`/`smart_home_controller.py` (confirmed zero relevant call sites; excluded for scan performance, not correctness).
- **Fixed — `src/orchestrator/rag_client.py::fetch_service_urls_from_registry()` had the same bug**: its `httpx.AsyncClient` for `/api/internal/config/rag-services` also sent no `X-Service-Key`, 422'ing and silently falling back to the hardcoded `RAG_SERVICE_URL_MAP` constants -- found by the widened AST scan above (originally flagged as a scope item and excluded; now fixed in the same pattern as `config_loader.py`, exclusion removed). New MockTransport tests: `tests/unit/test_orchestrator_rag_client_service_key.py`.

#### Fixed: Mission Control never loaded on a plain visit — the default landing tab read 'dashboard', not 'mission-control'

- **Fixed — `admin/frontend/app.js`'s no-hash fallback defaulted to the wrong tab**: `index.html` declares Mission Control as the default landing page in two places (the sidebar button's pre-applied `sidebar-item-active` class and comment at `:1531-1534`, and the `#tab-mission-control` container's "(Default Landing Page)" comment at `:1882`), but `app.js`'s `const initialTab = hash || 'dashboard'` (present since the initial OSS commit, `794096b`) sent every plain visit to the separate "Service Status" Dashboard tab instead. `Athena.pages.MissionControl.init()` — and its one dependency, `GET /api/dashboard` — never ran unless a user explicitly clicked "Mission Control" or navigated to `#mission-control`, which is exactly what a 6-hour admin-backend log window with zero genuine `/api/dashboard` hits showed. Now defaults to `'mission-control'`; `#dashboard` deep links and the keyboard shortcut are unchanged. New static test: `tests/unit/test_admin_frontend_default_tab.py`.
- Cache-buster: `app.js` bumped `?v=20260927b` → `?v=20260927f` (main advanced to `e` via a sibling campaign while this one was in flight; `f` avoids the collision).

#### Fixed: Mission Control's voice-health card assumed every RAG shares one host, reporting all of them "unreachable" instead of "not configured"

- **Fixed — `admin/backend/app/routes/dashboard.py` probed Weather/Sports/Dining RAG health via a single `RAG_HOST` + hardcoded port** (`:8010`/`:8017`/`:8019`): in Kubernetes each RAG is its own Service, so this single-shared-host assumption is an OSS-First violation, and with `RAG_HOST` unset (the normal case for a per-Service deployment) it built malformed `:port/health` URLs and reported every RAG "unreachable". New `app.utils.rag_urls.resolve_rag_url` resolves each RAG independently: service-registry row → canonical `RAG_<NAME>_URL` env var (spelling matches `src/orchestrator/urls.py`) → legacy `RAG_HOST`/`RAG_SERVICE_HOST` + port (one-time WARNING) → `not_configured` (amber), no longer conflated with a genuine probe failure.
- **Same treatment for `admin/backend/app/routes/voice_tests.py`**: `_rag_probe_url` (the full-pipeline test's auto-detected RAG probe) and `test_rag_query` (the manual "Test RAG" panel) both resolved through the same module-level `RAG_SERVICE_HOST` single-host assumption; both now resolve via the shared helper. `test_rag_query` returns 503 with a clear "not configured" message instead of attempting a request against a broken URL.
- See `docs/CONFIGURATION.md`'s new "Mission Control voice-health card and RAG test probes" note for the full resolution order.

#### Fixed: System Configuration's Gateway/Orchestrator/Ollama cards always read Offline -- GET /api/status probed a pre-Kubernetes "Mac Studio"/"Mac Mini" topology

- **Fixed — `admin/backend/main.py::get_system_status`** probed `MAC_STUDIO_IP`/`MAC_MINI_IP` (both default `localhost`) directly for gateway/orchestrator/RAG/ollama and a separate voice-host port map -- a deployment topology from before gateway and orchestrator moved into Kubernetes and Ollama moved to an operator-chosen host, so every check reported `error`/timeout regardless of real health. The service registry (`athena_service_registry`, the same cached `health_status` `GET /api/service-registry/services` already reads) is now the source of truth for every registered service; Gateway, Orchestrator, Ollama, and SearXNG (no registry row by default -- the OSS seed list only covers RAG services) are each checked directly via their own env-configured URL only when the registry has no row for them. Disabled-row handling and `overall_health` reuse `service_registry._overall_health` (the enabled-only rollup) instead of a second, drifting implementation -- `overall_health`'s bottom label is now `"unhealthy"` (was `"critical"`), matching that shared helper. Dead `MAC_STUDIO_IP`/`MAC_MINI_IP`/`SERVICE_PORTS`/`MAC_MINI_PORTS`/`socket` import removed. New tests: `admin/backend/tests/test_system_status_registry.py`.
- **Changed — `admin/frontend/system-config.js`**: new `mapServiceStatusToUiBucket` classifies each `/api/status` entry into `healthy` / `unhealthy` / `neutral` / `offline`; `disabled`/`unconfigured`/`not configured`/`pending` now render as a neutral gray "Not Configured" state instead of red "Offline" -- an operator choice or a transient state is not the same claim as "this is down". New Node test: `tests/unit/test_admin_frontend_system_config_status_mapping.py`. Cache-buster: `system-config.js` `?v=20260913` → `?v=20260927`.
- **Note**: `voice-pipelines.js`'s STT/TTS component-health cards (which also read `GET /api/status`, matching on `whisper`/`piper`/`stt`/`tts`) now show "not configured" for those components rather than a stale probe result, since the old `MAC_MINI_PORTS` voice-host probing is gone and no registry rows exist for them by default. `renderComponentRow` already degrades gracefully for an absent service (gray "not configured"), so this is a behavior improvement, not a regression -- flagging it as an observed side effect of this fix, not a new capability.

#### Fixed: Control Agent had zero incoming auth on any route; process launch used a shell; services-file `dir` could escape PROJECT_ROOT

- **Fixed — every Control Agent route was reachable by anyone on the LAN with no authentication** (`src/control_agent/main.py`, new `src/control_agent/auth.py`): xander's review (F2, 2026-09-27-operate-athena-dashboard-registry-cleanup) found this Critical pre-existing gap — anyone able to reach port 8099 could stop Ollama or any managed process/container. New `require_service_caller` FastAPI dependency (mirrors `orchestrator/ingress_auth.py`'s constant-time `X-Service-Key` check, with no `DEV_MODE` bypass — the Control Agent has no such concept) is now applied to all 17 mutating routes: `/process/start|stop|restart/{port}`, `/docker/start|stop|restart/{container_name}`, `/ollama/start|stop|restart`, `/huggingface/download` (POST), `/huggingface/download/{job_id}` (DELETE), `/huggingface/import-to-ollama`, `/huggingface/downloaded` (DELETE), `/watchdog/enable|disable|exclude/{port}|include/{port}`. A missing or wrong key returns 401; an unset `SERVICE_API_KEY` returns 503 on every mutating route (fail-closed) with a one-time startup WARNING. Read-only routes (health/status/list/search endpoints, `/debug-logs/*`) are unchanged.
- **Fixed — admin-backend and the orchestrator now send `X-Service-Key` on every Control Agent call**: new `app.utils.service_auth.control_agent_headers()` helper wired into `service_control.py`'s three Control Agent client constructions (docker/process/ollama actions, plus the read-only containers-status and ollama-health clients) and `model_downloads.py`'s single `call_control_agent` helper (covers all `/huggingface/*` calls). The orchestrator's own gateway-keepalive caller (`ensure_gateway_running` in `src/orchestrator/main.py`) — previously building an unheadered client for `/process/status` and `/process/start` — now reuses its existing `_SERVICE_API_KEY`.
- **Fixed — process launch used `create_subprocess_shell` with a joined command string**: `start_process_by_port` now uses `asyncio.create_subprocess_exec(*cmd, cwd=working_dir, stdout=log_fh, stderr=STDOUT)` — an argv list, never a shell. Defence in depth: `CONTROL_AGENT_SERVICES_FILE` entries are now rejected at load time if any `cmd` element contains a shell metacharacter (`; & | $ \` < > \n`).
- **Fixed — `dir` in a services-file entry could point outside the Control Agent's checkout** (e.g. `dir: "/etc"` or `dir: "../../etc"`): `dir` must now be a relative path with no `..` component that resolves under `PROJECT_ROOT`; violations are rejected at load time with one ERROR log, same handling as any other malformed entry.
- **Added — services-file permission warning**: `CONTROL_AGENT_SERVICES_FILE` is checked for group/world-writable permissions at load time; a writable file logs one WARNING (still loads — warn, not refuse, since it names commands and containers this Control Agent may execute/control).
- See `docs/CONFIGURATION.md` and `CLAUDE.md`'s Control Agent section for the full route list and the admin-backend/orchestrator caller wiring.

> **Commits:** `c36dc83` (dashboard badges), `c146353` (Control Agent managed-services config)

#### Fixed: dashboard service badges render "undefined"; Control Agent no longer hard-codes a process list

- **Fixed — dashboard service badges rendered the literal text "undefined"** (`admin/frontend/app.js`): the service-registry API returns `health_status` (server-normalised: `NULL` → `pending`) and, after DC10, `unconfigured` — not `status`, a field two dashboard render sites read that the API has never sent. New `serviceStatus(service)` helper (`service?.health_status ?? service?.status ?? 'unknown'`) used by both the Dashboard tab's per-group cards and the RAG Services tab's registry cards; badge classes now cover `healthy` / `unhealthy`|`error` / `offline` / `disabled` / `pending` / `unconfigured` (amber, matching service-control.js's existing "Needs Setup") / `unknown` (gray). Status text now goes through `escapeHtml`.
- **Fixed — Control Agent hard-coded every house process, a Docker container whitelist, and a watchdog-exclude list** (`src/control_agent/main.py`): this is why the 60s watchdog relaunched a retired house stack as bare processes after its containers were stopped, and the startup registry sync re-registered them — the module always had *something* to manage regardless of what a deployment actually wanted. New `CONTROL_AGENT_SERVICES_FILE` (OSS-First: unset by default, so nothing is managed) points at a JSON file with three independent, all-optional keys: `processes` (today's port-keyed shape: `name`/`dir`/`cmd`/optional `health_path`/`enabled`), `watchdog_exclude` (ports), and `containers` (the Docker allowlist, replacing the previously hard-coded, partly-stale `ALLOWED_CONTAINERS` set). A malformed file or a single bad entry logs one ERROR and falls back to managing nothing for that piece — the process never crashes over a bad config file. See `src/control_agent/services.example.json` and `docs/CONFIGURATION.md`.
- **Breaking for Control Agent operators**: a deployment that relies on the Control Agent's watchdog, its startup registry sync, or its Docker container control must now set `CONTROL_AGENT_SERVICES_FILE` explicitly — the built-in process list and container whitelist are gone.

> **Commits:** `5b8b515` (Phase 1 — `search_transit` wiring), `85930aa` (Phase 2 — base-knowledge settings facade), `02fd52e` (P3 — `/public` auth gate, sanitization, transit sort/regex fixes)

#### Transit tool wiring: `search_transit` reaches a real transportation route

`search_transit` (defined in `rag_tools.py` since an earlier campaign) had no `endpoint_map`/`service_name_map` entry, so a call went out as `POST {transportation}/search` — a route the transportation service has never had. It's now wired to a new dispatch route, gated so it only reaches the LLM on transit-phrased queries, and registered so it can be toggled from the Admin UI.

- **Added — `GET /transit/query`** (`src/rag/transportation/main.py`): a single dispatch route reusing the 9 existing handlers directly (their own behavior is unchanged). Precedence: `stop_id` (resolved as given, then feed-prefixed across configured feeds/static services, then falls back to a name search, then 404) > `query` (name search, sorted by distance when `lat`/`lon` are also given, with an explicit `message` on zero matches rather than a bare empty list) > `lat`/`lon` alone (`nearby`) > `free_only` alone (`free`) > no params (`overview`). A feed that loaded with zero stops now gives a 503 naming the per-feed fetch error, instead of the misleading generic "not loaded yet".
- **Changed — orchestrator tool wiring** (`src/orchestrator/main.py`): `search_transit` added to `endpoint_map`, `service_name_map`, and the GET-dispatch tool list.
- **Added — keyword-gated reachability** (`src/orchestrator/helpers.py::is_transit_query`): `search_transit` is offered alongside `get_directions`/`search_restaurants` only when a `directions`-intent query reads as transit-phrased ("next bus", "light rail", "route 15 schedule", ...). A driving/walking query sees exactly the tools it saw before this change.
- **Added — `tool_registry` seed** (`admin/backend/alembic/versions/059_seed_search_transit_tool.py`): a data-only, `ON CONFLICT DO NOTHING` migration so `search_transit` can be toggled on the Admin UI tools page, matching the pattern already used for the other RAG tools. Run `alembic upgrade head` to apply.
- **Documentation**: `docs/CONFIGURATION.md`'s Transit section now states the feed `type` vocabulary the tool's `transit_type` filter matches by prefix, and that departures use container-local time.

#### Memory & Context → Base Knowledge saves and re-loads; `/public` gated behind service auth

The Admin UI's Memory & Context → Base Knowledge tab called a service-key-gated `/api/internal` path a browser could never authenticate against, so every save silently failed. It now has its own typed facade, and a related pre-existing gap — the read-only `/api/base-knowledge/public` route serving every base-knowledge row, including a home address, with **no authentication at all** — is closed in the same reconciliation pass (xander FIX, found in diff review).

- **Added — `GET`/`PUT /api/base-knowledge/settings`** (`admin/backend/app/routes/base_knowledge.py`): an 8-field facade over the two-store split — city/state live in the `location/default_location` entry (fanned out to every matching row on write, regardless of `applies_to`); the other six fields live in `system_settings`, never published on `/public`. One session, one commit; any exception rolls back both writes together. Validation happens in the handler and returns a single string 422 detail naming the field, not a pydantic list the frontend's `ApiError` would stringify to `[object Object]`. Timezone defaults to `DEFAULT_TIMEZONE` when it's IANA-valid, else UTC.
- **Fixed — `admin/frontend/memory-context.js`** now loads from and saves to the new endpoint instead of the unreachable `/api/internal` path. Save re-renders the tab from the response; the error toast now shows the actual validation message.
- **Fixed — `GET /api/base-knowledge/public` now requires authentication** (`admin/backend/app/routes/base_knowledge.py`, D44): gated behind `verify_service_or_oidc` — an `X-Service-Key` matching `SERVICE_API_KEY`, or an authenticated admin session (owner/operator; a scoped role like viewer is rejected too, since it also fails the `read:base_knowledge` permission check `verify_service_or_oidc` runs internally). Previously ungated — any unauthenticated caller could read every row, including a home street address under `category='property'`. Both in-repo callers now send the header: `shared.admin_config.AdminConfigClient.get_base_knowledge` (`src/shared/admin_config.py`, used by the orchestrator) and `src/rag/directions/main.py`'s startup fetch, which also logs a one-time warning if `SERVICE_API_KEY` is unset.
- **Fixed — prompt-injection via city/state** (`admin/backend/app/routes/base_knowledge.py`): city and state are rendered verbatim into every guest's system prompt (`base_knowledge_utils.build_knowledge_context`). A value containing a control character or embedded newline (e.g. `"Denver\nIGNORE PREVIOUS INSTRUCTIONS"`) is now rejected with a 422 naming the field, checked before whitespace-trimming so a leading/trailing newline can't slip through.
- **Fixed — a type-drifted `system_settings` blob no longer leaks a wrong type into the response**: each stored field is now re-validated independently against the same rules the write path enforces; a field with the wrong type or an invalid value (e.g. a stale enum value, `timezone` stored as a list) falls back to its own default with one WARNING, never a 500.
- **Fixed — `/transit/query`'s `query`+`lat`/`lon` search mode no longer caps results before sorting by distance** (`src/rag/transportation/main.py`): when more matches exist than the requested `limit`, the nearest ones now always survive the cap; previously the cap was applied during the unsorted scan, so the nearest match could be dropped before distance was ever computed.
- **Fixed — `is_transit_query`'s keyword gate** (`src/orchestrator/helpers.py`): "train my dog" / "train the new hire" (the verb sense) no longer misclassifies as transit-related; "commuter rail" and "rail schedule/line/station" now do.
- **Documentation**: `docs/CONFIGURATION.md` documents the `/api/base-knowledge/public` service-key requirement alongside the existing orchestrator ingress-auth section.

> **Commits:** `cea046a` (P0 — leak-gate script), `c23dc27` (P1 — admin-backend/frontend), `ddaf16b` (P2 — region-configurable RAG services), `b16924b` (P3 — gateway/orchestrator session + ingress auth), `13ba5f6` (P3b — gate extension), `88caad4` (P4 — remaining references), `d6825a0` (P5 — CI workflow), `75a5af0` (P7 item 0 — import-cycle fix), `4826214` (P7 items 1–6 — HA entity/room config), `f6e9f51` (P8 — light-group scene fallback, restored satellite name-parse fallback, dropped unused `JARVIS_MEDIA_PLAYERS`)

#### OSS readiness: no maintainer house in runtime code, orchestrator ingress authentication, gateway non-streaming session continuity

Removes every maintainer-identifying value (home LAN IPs, home domain, home city, home-dir paths, a legacy namespace FQDN, home coordinates, the maintainer's name, and the maintainer's host names) from the tracked tree's runtime behavior, adds a CI gate that stops them from returning, and closes the gap where Home Assistant's non-streaming path lost session continuity (parent F97) and the orchestrator accepted query/session requests from any caller.

- **Added — maintainer-leak CI gate** (`scripts/check-maintainer-leaks.py`, `scripts/.maintainer-leak-allowlist`, `.github/workflows/maintainer-leaks.yml`): scans every tracked file for eight rule classes (home LAN IPs, home domain, home city, home-dir paths, a legacy namespace FQDN, home coordinates, the maintainer's name, the maintainer's host names), FAIL-class outside docs/tests/examples and WARN-class inside them. An allowlist entry pins the exact source line it permits (a scrubber migration or a national reference table where the home city is one row among many, e.g. an airport-code lookup) and is itself flagged stale if the line it names no longer matches. Runs on every PR and push to `main` (no `paths:` filter, so a leak anywhere in the tree is caught). A private `--extra-patterns` file adds house-specific rules locally without ever committing them.
- **Changed — region-configurable transportation, community-events, and Amtrak RAG services** (`src/rag/transportation`, `src/rag/community_events`, `src/rag/amtrak`, `src/shared/config.py`): these three services shipped with a hardcoded home transit region. They now start with **no region configured** — `/health` reports `configured: false` and data routes return 503 until an operator sets `TRANSIT_REGION_NAME`/`TRANSIT_GTFS_FEEDS`/`TRANSIT_STATIC_SERVICES` (transportation), `COMMUNITY_EVENTS_SOURCES` (community events), or `DEFAULT_AMTRAK_STATION` (Amtrak, optional — the service still works with an explicit origin per request). GTFS feed and community-event fetches go through the shared SSRF guard (`shared.url_safety.safe_get`); a feed's optional geographic `bounds` box is opt-in and excludes results outside it. A blocked or failed fetch is never swallowed — it's recorded per feed/source and surfaced in `/health`'s message, naming the reason (private-host redirect, oversized response, etc.). See `docs/CONFIGURATION.md` and `docs/MODULES.md` for the full JSON schemas.
- **Fixed — MCP domain allowlist failed open on a fresh install** (`admin/backend/app/routes/mcp_security.py`, `src/shared/tool_registry.py`): with no `mcp_security` row, `check_domain` returned `domain in default_allowed or domain.endswith("")`, which is always true — every domain was implicitly allowed. The default allowlist is now `["localhost", "127.0.0.1"]` everywhere a default is created or returned, and an empty stored allowlist means the same (deny-all-remote). A bare `"*"` entry is never a wildcard: it's inert and dropped on write (`PUT` strips it and warns; a single-item `POST` of `"*"` alone is rejected with 400), never treated as "allow everything." The admin matcher is dot-anchored, so `*.example.com` no longer also matches `notexample.com`.
- **Added — orchestrator ingress authentication** (`src/orchestrator/ingress_auth.py`): the orchestrator's query routes (`/query`, `/query/stream`, `/query/stream/v2`, `/v1/chat/completions`), its four session routes (`GET /sessions`, `GET`/`DELETE /sessions/{session_id}`, `GET /sessions/{session_id}/export`), `GET /session/{session_id}/warmup`, and four `/admin/*` maintenance routes (13 routes total) now require an `X-Service-Key` header matching `SERVICE_API_KEY`. `ORCHESTRATOR_INGRESS_AUTH` (default `enforce`) controls this: `enforce` rejects an unauthenticated or wrong-key request with 401; `warn` logs `orchestrator_unauthenticated_request` and allows the request through, for finding a caller a rollout missed before switching to `enforce`. A present-but-wrong key is always 401, in every mode, including `DEV_MODE`. Every in-repo caller (gateway's orchestrator client, LiveKit integration, the Wyoming bridge, jarvis-web backend, admin-backend's SMS webhook) now sends the header. `/v1/models` stays ungated (read-only model metadata).
- **Fixed — gateway non-streaming session continuity for Home Assistant (parent F97)** (`src/gateway/main.py`, `src/orchestrator/main.py`): Home Assistant's `extended_openai_conversation` integration calls the OpenAI-compatible endpoint without `stream`, and the gateway's non-streaming path bridged that call through the orchestrator's bare `/query` route — dropping the full message history the orchestrator needs to resolve a per-conversation session, and losing `room`/`temperature` on the orchestrator side. Non-streaming `chat_completions` and `responses_api` now route to the orchestrator's `/v1/chat/completions` with `stream: false`, sharing one payload builder with the streaming path; the orchestrator's non-stream branch now forwards `room` and `temperature` instead of dropping them.
  - **Session identity precedence changed**: a session is now keyed on the top-level `user` field when present (Home Assistant's per-conversation id), falling back to the detected room only when `user` is absent, and to a room-less key with no `user` and no room. Room no longer enters the key when `user` is present, so a satellite-detection flap can't fragment an HA conversation. The gateway's `_detect_room_from_active_satellite` fallback changed from `"office"` to `"unknown"` on all four of its failure paths.
  - **`/v1/responses` now rejects `previous_response_id` with 400** (`"previous_response_id is not supported; send the full conversation in input"`) instead of silently dropping it, which made every turn look like a first turn. The converter also now accepts `input_text`/`output_text` content parts (previously only `text`) and skips `function_call`/`function_call_output`/reasoning items rather than misclassifying them.
  - **Session ids reset once**: the HMAC payload used to derive a session id changed shape, so every existing `oai-` session id changes on first use after this ships — a one-time conversation-context reset, not a recurring one.
- **Changed — Home Assistant entity and room mappings moved to configuration** (`src/gateway/main.py`, `src/orchestrator/tv_handler.py`, `apps/jarvis-web/backend/main.py`, `src/shared/config.py`): satellite→room mapping, TV entity fallbacks, music-player fallbacks, and a bed-warmer integration's entity ids were hardcoded to one house's Home Assistant instance. They're now read from `HA_SATELLITE_ROOM_MAP`, `HA_TV_ENTITIES`, `HA_MUSIC_PLAYERS`, and `HA_BED_WARMER_ENTITIES` (all empty by default — the corresponding feature reports itself unconfigured rather than guessing at hardware a deployment doesn't have). jarvis-web's kitchen-appliance endpoints (oven, fridge, freezer) are now driven by seven appliance entity-id env vars, also empty by default. (The unused `JARVIS_MEDIA_PLAYERS` env var this endpoint previously read but never consumed was deleted rather than kept as scaffolding.)
  - **Restored: satellite room detection works with zero configuration.** The scene-fallback change above made `HA_SATELLITE_ROOM_MAP` the *only* way to resolve a room from an active satellite; that briefly regressed the pre-existing behavior where an unmapped satellite's HA `friendly_name` (the `"Voice - <Room> Assist"` convention most Voice PE setups already use) still resolved a room. `_detect_room_from_active_satellite` now tries the map first, then that generic name parse, then `"unknown"` — the map is for overriding or correcting a name that doesn't fit the convention, not a prerequisite.
  - **Added — per-room light-group scene fallback** (`src/orchestrator/smart_home_controller.py`): the movie-mode / good-morning / arriving-home scenes previously fell back to turning on entity_id `"all"` — every light in the house — when the requested scene or script didn't exist. New `HA_LIGHT_GROUPS` (empty by default) maps a room to its light-group entity; a room with no configured group now gets no fallback at all rather than a house-wide one. Also fixed a pre-existing bug this uncovered: the scene-failure message referenced a variable only assigned on the success path, so a failed activation raised `UnboundLocalError` (silently swallowed into a generic error) instead of ever reaching the intended "may not be configured yet" message.
- **Added — Music Assistant and SearXNG configuration** (`src/shared/config.py`, `admin/backend/app/routes/music_config.py`, `admin/backend/main.py`, `src/orchestrator/parallel_search.py`): `MUSIC_ASSISTANT_URL` and `SEARXNG_BASE_URL` are new `AthenaConfig` fields; both empty by default. An unset Music Assistant URL now reports `{"enabled": false, "error": "Music Assistant not configured"}` instead of falling back to a hardcoded host (the admin-stored `MusicConfig.music_assistant_url` DB row still takes precedence when set). An unset SearXNG URL means the provider isn't registered and the admin status reads "not configured," with no network probe. `parallel_search.py` now reads `SEARXNG_BASE_URL` as canonical, with the older `SEARXNG_URL` accepted as a fallback that logs a one-time deprecation warning.
- **Changed — new-conversation rate limiter's trusted-proxy default** (`src/shared/config.py`): `TRUSTED_PROXY_CIDRS` now defaults to empty rather than a specific cluster's pod CIDR — every caller's peer address is trusted directly out of the box (correct with no reverse proxy in front of the gateway); a deployment behind one must set this explicitly, or every caller behind it shares one rate-limit bucket (the gateway logs `trusted_proxy_cidrs_unset` once at startup as a nudge).
- **Improved — RAG service error detail reaches the LLM** (`src/orchestrator/rag_client.py`): a 4xx/5xx RAG response's `detail`/`error` body field is now included (capped at 200 chars) in the tool-call error message, instead of being collapsed to a bare status code — needed for services like Amtrak, whose 400 response asks the caller for a missing parameter.
- **Added — unconfigured-service indicator in the Admin UI** (`admin/backend/app/services/health_poller.py`, `admin/frontend/service-control.js`): a RAG service reporting a 200 health check with `configured: false` now shows a distinct amber "Needs Setup" badge, rather than the same green "healthy" state as a fully working service.
- **Fixed — orchestrator container import cycle** (`src/orchestrator/automation_agent.py`, `src/orchestrator/semantic_cache.py`): a module-level `from orchestrator.helpers import ...` in each file ran before `main.py` imported `orchestrator.nodes`, triggering a circular partial-init `ImportError` at container startup that the unit suite's own import order never exercised. Both imports moved to their single call site. New `tests/unit/test_orchestrator_entrypoint_import.py` runs `import main` in a fresh subprocess with each service's actual Dockerfile `cwd`/`PYTHONPATH` (orchestrator, gateway, jarvis-web backend) to catch this class of defect directly.
- **Breaking for OSS deployers**:
  - The transportation, community-events, and Amtrak RAG services need explicit region configuration or they start unconfigured (503 on data routes).
  - The orchestrator's query and session routes require `X-Service-Key` by default (`ORCHESTRATOR_INGRESS_AUTH=enforce`); set `SERVICE_API_KEY` and use `warn` mode to find any caller a deployment's own integrations add before switching to `enforce`.
  - `docker-compose.yml` (which defines the orchestrator and gateway services only) now fails fast without `SERVICE_API_KEY` set. Every other deployment path that talks to the orchestrator — jarvis-web, admin-backend's SMS webhook, the gateway's Wyoming bridge — needs `SERVICE_API_KEY` set in its own environment too; see `docs/INSTALLATION.md`.
  - The MCP domain allowlist defaults to localhost-only; a deployment relying on the previous fail-open behavior for a remote MCP endpoint must add it explicitly.
  - `/v1/responses` now returns 400 for `previous_response_id` instead of silently ignoring it.
  - Every live OpenAI-compatible session id resets once, the first time a conversation is seen after this ships.
- **Known limitation (pre-existing, unrelated)**: `tests/unit/test_price_compare.py::TestPriceResult::test_to_dict_includes_all_fields` remains red; untouched by this change.
- **Operator note**: these are code fixes only. `athena-orchestrator`, `athena-gateway`, `athena-admin-backend`, `athena-jarvis-web`, and `athena-rag-transportation` need to be rebuilt and rolled by digest to take effect in `athena-prod` — that roll is hank's, under the parent house-cutover campaign.

> **Commits:** `44da391`/`daa8040` (Phase 1 — red/fix), `cbcadfc`/`15d097e` (Phase 2 — red/fix), `33b9229`/`eb8db83` (Phase 3 — red/fix), `ecfa4a8`/`31ff77a` (Phase 4 — red/fix), `8a5e65d` (Phase 4 reconciliation — binding runtime session test), `2475180`/`a47c69c` (Phase 5 — red/fix), `b0d6fb4`, `e403f29`, `fa250db`, `f7922e9`, `629b4aa`, `c2dbada`, `a63180e` (reconciliation round 1 — F36–F42, F34), `cf270eb`, `3c9b050`, `8125ad8`, `d9c20ab`, `64eaa61`, `b7dbb50`, `b028e03` (reconciliation round 2 — F43–F49; `b028e03` corrects `a63180e`'s D13 write)

#### Voice intent defects: sequence-timing false positives, shared streaming session, dining keyword gap, RAG key-name drift, short-turn continuation

Four defects found after the house cutover (`dick`'s post-cutover investigation 2), plus the short-turn continuation rule that was the actual misroute mechanism behind one of them.

- **Fixed — sequence-timing matcher and multi-intent splitter** (`src/orchestrator/sequence_executor.py`, `src/orchestrator/smart_home_controller.py`, `src/orchestrator/search_providers/intent_classifier.py`): `detect_sequence_intent` matched bare substrings (`'at '`, `'in '`, `'then'`, `'times'`, `'tonight'`, …), so a question like "what place has happy hour and outdoor seating?" was misdetected as a timed device sequence and routed around multi-intent splitting. Both `sequence_executor.detect_sequence_intent` and `SmartHomeController.detect_sequence_intent` now call one word-boundary matcher, `has_sequence_timing`.
  - Bare `at <number>` (no meridiem or `o'clock`) and a handful of other bare temporal words (`tonight`, `tomorrow`, `later`, `again`, …) only count as sequence timing when the query starts with an imperative device verb (`turn`, `set`, `dim`, `play`, …) — so "the restaurant at 5 north main street" and "what time does the store open tomorrow" are no longer sequences, while "turn off the lights at 5" and "turn on the fan at seven" still are. `at <number>` with an explicit meridiem/`o'clock` (`"at 5pm"`, `"at seven o'clock"`) always counts, with no verb required.
  - The multi-intent splitter (`IntentClassifier.detect_multi_intent`) no longer splits a descriptive "and" clause into two requests unless each part after the first is independently standalone (starts with an action verb, a wh-word, or an auxiliary+subject). "what place has happy hour and outdoor seating?" and "bars with a patio and open late" now stay one request; "turn off the lights and open the garage" and "play some jazz and set the lights to blue" still split into two. A too-short or non-standalone fragment now makes the splitter return the whole query unsplit, rather than silently dropping the fragment.
- **Fixed — per-conversation OpenAI-compatible sessions** (`src/orchestrator/helpers.py`, `src/orchestrator/main.py`, `src/orchestrator/session_manager.py`, `src/gateway/main.py`): every streaming and non-streaming call on the orchestrator's `/v1/chat/completions` used one shared Redis session id (`"openwebui-session"` / `"ha-voice-assistant"`), so every device and every conversation on the OpenAI-compatible path shared one history. (The gateway's non-streaming `/v1/chat/completions` → `/query` path is unchanged — one session per request, as today.)
  - Each conversation is now keyed by `"oai-" + HMAC-SHA256(SERVICE_API_KEY, room + user + first user message)[:32]` — an HMAC, not a bare hash, so a caller can't precompute another conversation's id from a guessable opener. Room is mixed in so a satellite-detection flap changes the key (isolation over continuity).
  - **First-turn reset, with a grace window**: when the replayed history holds exactly one user message, the orchestrator resets the session and its `athena:context:` key before use — except when a session under the same fingerprint was created within the last `NEW_CONVERSATION_RESET_GRACE_SECONDS` (default 120s), which protects Home Assistant's truncated-ASR retry (a resend of the same single-message opener) from wiping a live conversation mid-turn.
  - **Explicit ids**: a caller may pass `session_id` (top-level, or `extra_body.session_id`) matching `^explicit-[A-Za-z0-9._:-]{1,55}$` (≤ 64 chars total). Anything else — including the legacy literals, a bare `oai-…` id, or an id outside the `explicit-` namespace — is rejected with a WARNING (`openai_session_id_rejected`) and falls back to the fingerprint.
  - **Bounded**: `SESSION_MAX_COUNT` (default 5000) caps both the in-memory session dict and a Redis creation-time index; the oldest session is evicted through `SessionManager.delete_session` (one atomic Lua `EVAL` for the Redis index, so concurrent orchestrator replicas can't over-evict), which clears both stores together.
  - **Breaking change for operators**: outside `DEV_MODE`, the orchestrator now refuses to start (`SystemExit`) when `SERVICE_API_KEY` is empty or the placeholder `dev-service-key-change-in-production` — the HMAC secret is resolved first in `lifespan`, before any client is constructed. In `DEV_MODE` only, it falls back to a per-process ephemeral secret (a WARNING is logged; sessions don't survive a restart and don't agree across replicas).
- **Fixed — gateway identity forwarding and a new-conversation rate limit** (`src/gateway/main.py`, `src/gateway/conversation_limiter.py`): the gateway dropped `user`/`session_id` when forwarding to the orchestrator's streaming route, and there was no bound on new-conversation creation. `ChatCompletionRequest`/`ResponsesAPIRequest` now carry `user`, `session_id` and forward them (plus `room`) top-level to the orchestrator. A new per-source limiter guards first-turn requests (no explicit `session_id`) on both `/v1/chat/completions` and `/v1/responses`: `NEW_CONVERSATION_PER_MINUTE_PER_IP` (default 120 — raised from an initial 30 once reconciliation found a single reverse-proxy-facing IP represents the whole house), scoped per resolved client key, with a 429 over the limit. `TRUSTED_PROXY_CIDRS` (default `10.244.0.0/16`, the cluster's pod CIDR) bounds which peers' `X-Forwarded-For` header is trusted, parsed right-to-left to the nearest untrusted hop. Counters are Redis-backed (atomic Lua `EVAL`, shared across gateway replicas) when `REDIS_URL` connects at gateway startup, and degrade to an in-memory, single-replica counter — including on a request-time Redis error — otherwise.
- **Fixed — dining keyword gap and recipe precedence** (`src/orchestrator/context/detector.py`): added venue phrasings (`outdoor seating`, `patio seating`, `rooftop seating`, `beer garden`, `cocktail lounge`, `drink specials`, `grab drinks`/`grab a drink`/`get drinks`, `place for drinks`, `spot for drinks`) and single words (`bars`, `pubs`, `tavern`, `gastropub`, `bistro`, `diner`, `eatery`) to `STRONG_INTENT_INDICATORS["dining"]`. Narrowed the recipe-precedence rule: a recipe request now only beats a dining match when the recipe signal is explicit (`recipe`, `recipes`, `how to make`, `how to cook`) **and** the dining match carries none of `reservation`, `restaurant`, `near me`, `nearby`, `menu` — so "how to make a reservation at a restaurant" stays dining, and "cook dinner and grab drinks after" (no trigger phrase either way) falls through to dining via the existing priority order.
- **Fixed — RAG service API-key env-var names, and a CI drift guard** (`scripts/generate-rag-manifests.py`, `scripts/create-secrets.sh`, `manifests/athena-prod/rag-services.yaml`, `docs/CONFIGURATION.md`, `docs/INSTALLATION.md`, `docs/MODULES.md`, `.env.example`, `.env.secrets.example`, `config.env.example`, `src/rag/dining/start.sh`, `src/rag/news/start.sh`): the manifest generator, `create-secrets.sh` and the docs named env vars that 11 RAG services' own code never read.

  | Service | Before | After |
  |---|---|---|
  | Airports | *(no secret ref at all)* | `FLIGHTAWARE_API_KEY` (shared with Flights) |
  | News | `NEWSAPI_KEY` | *(none — admin key store only: `api-newsapiai` / `api-webz`)* |
  | Sports | `THESPORTSDB_API_KEY` only | `THESPORTSDB_API_KEY` + `API_FOOTBALL_KEY` + `GNEWS_API_KEY` |
  | Dining | `YELP_API_KEY` | `GOOGLE_PLACES_API_KEY` |
  | SeatGeek | `SEATGEEK_API_KEY` | `SEATGEEK_CLIENT_ID` + `SEATGEEK_CLIENT_SECRET` |
  | Tesla | `TESLA_API_KEY` | *(removed — code reads no key)* |
  | Media (Overseerr) | *(no secret ref at all)* | `OVERSEERR_API_KEY` |
  | Directions | `GOOGLE_MAPS_API_KEY` | `GOOGLE_DIRECTIONS_API_KEY` + `GOOGLE_PLACES_API_KEY` |
  | Sitescraper | *(no secret ref at all)* | `BRAVE_API_KEY` (shared with WebSearch) |
  | SerpAPI | `SERPAPI_KEY` | `SERPAPI_API_KEY` |
  | BrightData | `BRIGHTDATA_API_KEY` | `BRIGHT_DATA_API_TOKEN` |

  Every `athena-api-keys` `secretKeyRef` in the regenerated manifest is now `optional: true` (the `SERVICE_API_KEY` ref from `athena-encryption` stays required), so re-applying against an existing Secret that hasn't been rotated to the new names doesn't break a Deployment. New `scripts/check-rag-key-env.py` (reusing `check-env-example.py`'s `collect_getenv_names` AST walker) checks per-service code-vs-generator name parity, the `create-secrets.sh` union, the required/optional ref shape, and a diff of the committed manifest against fresh generator output; it runs in CI via `.github/workflows/rag-generator-drift.yml` on any change to the generator, the checker, `create-secrets.sh`, the manifest, or `src/rag/**`.
- **Changed — short-turn context continuation** (`src/orchestrator/context/detector.py`, `src/orchestrator/main.py`, `src/orchestrator/state.py`): replaced the blanket rule "a query of ≤ 8 words always continues the previous intent" — the actual misroute mechanism, since `has_context_ref` was itself substring-matched (a bare `"and"` or `"any"` anywhere in the query set it) — with `decide_context_continuation`. A short turn now continues the previous intent only when it carries explicit anaphora or ellipsis (`yes`/`no`, "do that again", "level 2", a bare pronoun, a device modifier like "brighter", a leading "and"/"what about", …) **or** has no confidently classifiable intent of its own; a topic switch with a confident fresh intent (≥ 0.85, not the same intent family as the previous turn) classifies fresh instead. "what place has happy hour and outdoor seating?" after a recipe request now classifies DINING instead of inheriting RECIPES; "turn it off", "and the kitchen?" and "make it warmer" after a CONTROL turn still continue.
  - Because the seven existing readers of `has_context_ref`/`ref_types`/`is_continuation` (previous-exchange injection into tool calls, the Phase-2 intent-demotion route, the UNKNOWN-continuation route, both synthesis-prompt continuation flags, and the control-parameter merge/inquiry/modifier logic) still read raw substring-matched flags, `classify_node` is now the single writer of `state.context_ref_info`: it stores a `context_ref_view` — the raw flags on a **continued** turn, all-false on a **declined** turn, and the tightened anaphora-only flags when no previous context was consulted — plus a `state.continuation_decision` record of `{"decision", "reason"}`. The seven readers are unchanged; they see the view rather than the raw signal.
- **Follow-up (documented gap)**: a *continued* yes/no turn in a Phase-2 intent — e.g. "yes please" answering "Would you like me to find some places?" — is still demoted to synthesis-from-history rather than re-running the RAG tool call. That's the pre-existing continuation design (unchanged by this fix, deliberately: D13 leaves continued turns alone) and needs its own design (detecting an offer in the previous assistant turn); tracked as a follow-up ticket.
- **Follow-up**: importing `orchestrator.helpers` before `orchestrator.nodes` raises `ImportError` (a helpers ↔ nodes/`_runtime` cycle), pre-existing and contained today by `main.py`'s import order; documented in `helpers.py`'s import-contract docstring. Breaking the cycle is backlogged.
- **Follow-up**: a bare "drinks" collision against non-dining queries is now pinned by a regression test; no further code change was needed.
- **Process notes**: two items surfaced during validation are about the plan record, not the code — the plan's phase-3 test rows referenced "unchanged from r0" after r0 had been overwritten by a later revision (swept in the final plan revision), and the plan's worked cache-bypass example ("what about the ravens") can't reach the semantic cache under `UNCACHEABLE_PATTERNS` and was substituted with an equivalent query in the test suite.
- **Known limitation (pre-existing, unrelated)**: `tests/unit/test_price_compare.py::TestPriceResult::test_to_dict_includes_all_fields` remains red; untouched by this change.
- **Operator notes**:
  - Re-running `scripts/create-secrets.sh` writes `""` for every unexported key and `kubectl apply` overwrites the Secret — export every key you use before re-running it, including the renamed ones above.
  - Rotating `SERVICE_API_KEY` changes every session's HMAC key, so every live conversation resets once.
  - Explicit session ids must match `explicit-<1-55 chars>` (≤ 64 chars total); anything else falls back to the fingerprint.
  - Outside `DEV_MODE`, the orchestrator now refuses to start when `SERVICE_API_KEY` is empty or the placeholder value — see the session section above.
  - A turn with no explicit anaphora no longer gets referential handling from an embedded substring (e.g. a bare "and") — see the continuation section above.
  - These are code fixes only. `athena-orchestrator` and `athena-gateway` still need to be rebuilt and rolled by digest to take effect in `athena-prod` — that roll is hank's, under the parent house-cutover campaign.

> **Commits:** `567b929`/`eb415f4` (Phase 1 — red/fix), `510c1a2`/`d32cc21` (Phase 2 — red/fix), `1134dfd`/`a4c01d6` (Phase 3 — red/fix), `3e92be2`/`cdd6fe1` (Phase 4 — red/fix)

#### Post-cutover defects: RAG URL parity, streaming think-disable parity, temporal-location filter, gateway LiveKit deps

- **Fixed**: RAG service URLs were read from three disagreeing env-var spellings across `orchestrator/urls.py`, `rag_tools.py`, and `utils/constants.py` — 10 of the 23 names the live Deployment sets were read by no module at all, so `search_events`'s SerpAPI/SeatGeek/Community sub-providers dialed localhost on every call. `urls.py` is now the single resolver: canonical `RAG_<NAME>_URL` wins; the legacy `<NAME>_RAG_URL` spelling is still accepted (logs `rag_url_legacy_env_name` at WARNING; `rag_url_env_conflict` at WARNING if both are set and differ); a blank value counts as unset. Five default ports were also corrected for local dev with the env unset: flights 8012→8013, events 8013→8014, streaming 8014→8015, news 8015→8016, stocks 8016→8012 — plus `MODE_SERVICE_URL` (8021→8022) and directions (8022→8030).
- **Fixed**: `LLMRouter`'s Ollama streaming path (`_generate_ollama_stream`) never disabled qwen3 "thinking" or forwarded the `/no_think` prefix, unlike the non-streaming path. Both now build through a shared `_build_ollama_generate_payload`, so a streaming and non-streaming request to `/api/generate` are identical except `stream`. `generate_stream` also now forwards `system_prompt` to the Ollama, OpenAI, Anthropic, and Google streaming branches (previously hard-coded to `None` or omitted on three of the four), matching what non-streaming `generate()` already sent.
- **Fixed**: the retrieve-node temporal-location filter now excludes "right now", "currently", "at the moment", "this morning/afternoon/evening", "later", and "later today" from being treated as location entities (previously only "today", "tonight", and "now" were filtered) — these phrases now fall back to `DEFAULT_LOCATION` instead of being geocoded and failing.
- **Fixed**: `src/gateway/livekit_service.py` imports `numpy` and the LiveKit SDK without either declared in `src/gateway/requirements.in`/`.txt`, so `/livekit/*` routes silently 404'd — or, once `numpy` resolved locally but the SDK didn't, silently reported `enabled:false` with no visible error. `numpy`, `livekit`, and `livekit-api` are now declared gateway dependencies; the lock was regenerated with `scripts/lock-requirements.sh` (tooling from the dependency-remediation work): `numpy==2.4.6`, `livekit==1.1.20`, `livekit-api==1.2.1`, plus transitives, with no pre-existing gateway pin moved. An import failure in either the routes module or the SDK now logs at ERROR (`livekit_routes_import_failed` / `livekit_sdk_import_failed`) once `configure_logging` has run, instead of at INFO before it. `scripts/smoke-images.sh` gained a gateway-only gate that fails the image build if it can't import both.
- **Follow-up**: `generate_stream` still has no `BackendType.AUTO` branch — non-streaming `generate()` handles AUTO, but the streaming path falls through to the MLX/unknown-backend branch. A separate routing gap, not a parity fix of the request Ollama receives; not fixed here.
- **Follow-up**: `test_flag_on_startup_logs_sdk_status_before_gated_init` doesn't independently prove the log-before-init ordering (the code implements it in `gateway/main.py`; the aggregate test suite covers the operator-visible behavior). Left for the backlog.
- **Known limitation (pre-existing, unrelated)**: `tests/unit/test_price_compare.py::TestPriceResult::test_to_dict_includes_all_fields` remains red; untouched by this change.
- **Operator note**: these are code fixes only. `athena-orchestrator` and `athena-gateway` still need to be rebuilt and rolled by digest to take effect in `athena-prod` — that roll is hank's, under the parent house-cutover campaign.

#### Operations — house cutover to athena-prod

- **Operations**: a Home-Assistant-integrated deployment previously running on Docker Compose / launchd
  was migrated onto this repo's OSS build (Kubernetes, digest-pinned images), carrying forward its
  database, encrypted API-key data, and Home Assistant conversation-entry configuration.
- **Config keys that matter for this shape of deployment** (a house/production Home-Assistant-integrated
  install): `DEFAULT_CITY`/`DEFAULT_STATE`/`DEFAULT_TIMEZONE` (required for any location-dependent RAG
  service to resolve a fallback location instead of failing on an empty query); `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`
  and `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` (must list every private host/CIDR the deployment's health poller
  or site-scraper needs to reach, or those calls are SSRF-blocked); `CONTROL_AGENT_ENABLED`/`CONTROL_AGENT_URL`
  (only meaningful if a Control Agent process is actually running somewhere reachable); `ATHENA_SEED_DEFAULTS`
  (set `false` once real configuration exists — `true` UPSERTs OSS default rows over it on every restart).

> **Commits:** `71d152d` (Phase 1), `253f9a1` (Phase 2 — merge and revert as one unit), `ae0396f` (drop the superseded str-mocked test)

#### Twilio SMS webhook signature validation

- **Fixed**: signature validation raised `RuntimeError: Stream consumed` (500) on every signed request when `TWILIO_AUTH_TOKEN` was set, and its re-parse dropped blank and repeated params. It now validates the parsed form.
- **Fixed**: the validation URL was Host-derived `http://…`, which Twilio's validator never matches behind a TLS-terminating proxy.
- **Added**: `TWILIO_WEBHOOK_BASE_URL`.
- **Changed**:
  - Token set with base unset or malformed → 503 plus an error log (per request and once at import).
  - Non-urlencoded → 403.
  - Logs carry `path`, not `url`.
- **Operator note**:
  - Enabling signature validation needs both variables.
  - The Admin UI "Auth Token" field isn't persisted.
  - Until a deployment sets both, the webhook still accepts unsigned requests; the post-merge action is tracked separately.

> **Plan:** n/a — TINY tier
> **Commits:** `4f0e265` (guard semantics), `12bd012` (fixture fixup), `459d6de` (lexical-detection docstring, exit 2 on git read failure)

#### skip-guard fix: subset + tree-presence in place of set-equality

- **Fixed**: `scripts/check-no-new-test-skips.py` (added by the frontend-escaping campaign) asserted set *equality* between skips added in `base..HEAD` and a pinned allowlist — true only while the originating campaign branch was unmerged. Once merged, every subsequent `base..HEAD` diff added no skips, and an empty added-set can never equal a non-empty pinned set, so the gate went permanently red (CI run 34845479251 on `8302271`). Now enforces two obligations independently: (a) skips added in `base..HEAD` must be a **subset** of the pinned allowlist (diff-scoped); (b) every pinned skip must still be **present in HEAD's tree** via `git ls-tree`/`git show`, checked regardless of `--base` (tree-scoped).
- **Fixed**: `.github/workflows/frontend-escaping.yml` — on `push`, the guard now diffs against `github.event.before` (falling back to `--base HEAD` for a branch's first push, whose `before` is the all-zeros SHA) instead of `origin/main`, so it verifies what the push actually introduced instead of a vacuous base-equals-HEAD comparison.
- **Fixed**: `check_tree_presence`'s `git show` failure on a path `git ls-tree` already confirmed exists at HEAD now exits 2 ("could not run"), distinct from exit 1 for a pinned skip genuinely missing from HEAD's tree.
- **Documented**: the script's docstring now states detection is lexical (substring match, no AST) — a ratchet against carelessness, not a control against a motivated bypass — and spells out how to retire a pinned skip: remove it from `SANCTIONED` in the same change that removes the skip.

> **Commits:** `0468035`..`9d12d97`..`7411f6e` (Phase 1 — guards + baseline), `03af3df`..`15a09cd` (Phase 2 — callee/param/sink table), `087a47b`..`0df3ff6`..`2a48323` (Phase 3 — 52 wrong-primitive sites + callee-sink closures), `dd1b30d` (Phase 4 — `emerging-intents.js`), `045b6eb`..`e842149`..`9b328f6` (Phase 5 — 18 definitions deleted), `76384cd`, `1e5e284`, `bd23f40` (Phase 6 — 77 unescaped-quoted sites), `89b03ba`, `d8f4021`, `088ac1e`, `d06e33a`, `8c9245a` (Phase 7 — hardening, `immutable` removal, CI)

#### admin-frontend escaping consolidation

- **Added**: `admin/frontend/escape-html.js` is now the **only** file in `admin/frontend/` defining `escapeHtml`/`escapeJsAttr` — 20 definitions (7 closure-local, 2 entity-map, 11 DOM-round-trip) consolidated to 1, enforced by `scripts/check-escape-html-uniqueness.py` at absolute zero (name-matched forms, a body-shape scan for a hand-rolled entity map under any name, getter/`defineProperty`/computed-name forms — all path-scoped-excluding the canonical file itself).
- **Added**: `Object.defineProperty` freezes `window.escapeHtml`/`window.escapeJsAttr` against runtime overwrite (a console paste, a lazily-injected script) — the one threat static analysis can't see. Declaration drift and tag-order drift remain the uniqueness script's and the load-order harness's jobs respectively; three threats, three mechanisms, documented in `admin/frontend/README.md`.
- **Fixed**: 52 `on*=` handler sites (46 direct + 6 routed through `oss-profiles.js`'s `actionButton` builder) that called `escapeHtml` — the wrong primitive for a JS-string-literal position inside a handler attribute — now call `escapeJsAttr`, which escapes for the JS-string layer *and* the HTML-attribute layer in the correct order.
- **Fixed**: 77 `on*=` handler sites (63 direct + 14 builder-routed) with no escaping call at all now call `escapeJsAttr`. Includes **6** hand-rolled `.replace(/'/g, "\'")` escapes deleted (`app.js` ×3, `escalation.js` ×3, one of which landed in Phase 3 with its call site) in favor of `escapeJsAttr` (replace, never wrap — wrapping renders `O'Brien` as `O\'Brien`), and `app.js:3309`'s special case (`escapeHtml(JSON.stringify(config))`, delimiter switched from `'` to `"`) — a bare JS object-literal argument in the frontend's only single-quoted handler attribute, where `escapeJsAttr` would have produced a `SyntaxError`.
- **Fixed**: the 2 verified callee-sink defects (`app.js`'s `revealSecret`, `escalation.js`'s `showCloneEscalationPresetModal`) where a handler argument was delivered to a callee that re-rendered it into an unescaped `.innerHTML`. Mechanized via `scripts/check-callee-sinks.py` — a fail-closed classifier over every converted handler argument, with `unresolved` treated as a violation until adjudicated in `admin/frontend/.callee-sink-adjudications.json`.
- **Fixed**: 6 raw LLM-fed interpolations in `emerging-intents.js` (`display_name`, `canonical_name`, `description`) now `escapeHtml`-wrapped. Provenance: LLM output shaped by user input via prompt injection against the intent classifier, with no CSP backstop (`script-src 'unsafe-inline'`).
- **Fixed**: `app.js`'s `infoIcon` — a 21st in-tree hand-rolled entity-map implementation whose escaping was undone by a `getAttribute` read-back before reaching `innerHTML`. Escaping moved to the sink.
- **Changed**: `admin/frontend/nginx.conf` no longer sends `immutable` in `Cache-Control` (kept `public`, `expires 1h`) — `immutable` means a browser does not revalidate even on a user-initiated reload (RFC 8246), which is exactly what made the guest-name XSS hotfix unrecoverable for an hour on warm-cache clients.
- **Added**: `.github/workflows/frontend-escaping.yml` now runs the full absolute-zero gate set (handler escaping, callee sinks, uniqueness, wiring parity, hardening) plus the D10 wide-population ratchets (`data-attribute` ≤344, `innerhtml-sink` ≤442, `bare-expression` ≤169, and `emerging-intents.js` text-node ≤19) on every PR and on push to `main`.
- **Note**: the wide out-of-scope populations — 344 unescaped data-bearing plain-attribute interpolations (44 files), 442 `.innerHTML =` assignments (52 files) and 169 bare-expression handler interpolations — are ratcheted at their measured values, not fixed. Escaping in this frontend remains the exception, not the rule; tracked as a follow-up.
- **Note**: `apps/jarvis-web/` carries the same defect class (including a 22nd definition, unescaped OSM place names, and the same Lodgify `guest_name` reaching an end-user-facing chat surface) and is **not** covered by this campaign's guards. Tracked as a follow-up.

> **Commits:** `a482635`, `ed8714f`

#### admin-frontend guest-name XSS hotfix

- **Fixed**: live reflected-XSS in `guest-context.js` and `memory-context.js` — a Lodgify-sourced guest name reached an `on*=` handler attribute unescaped. Introduced `admin/frontend/escape-html.js` (an IIFE, loaded as the **last** local `<script>` tag) so `window.escapeHtml`/`window.escapeJsAttr` resolve to its implementation regardless of the other files still declaring their own local copies — classic scripts, later tag wins.
- **Added**: `scripts/check-frontend-escape-load-order.js` — a Node `vm` harness that loads the real `index.html` tag order and asserts `escape-html.js` is the terminal definition.
- **Added**: `scripts/check-frontend-cache-busters.py` — a changed `.js` file whose `?v=` did not move never reaches a warm-cache browser; three-valued exit (`0` clean, `1` findings, `2` could-not-run). Bumped `guest-context.js` and `memory-context.js`'s busters so the fix in this release actually reaches clients that had already cached the vulnerable version.

> **Commits:** `1657900` (Phase 1), `8bd26fd` (Phase 2), `8fb9b7e` (Phase 3), `689fd11`, `657ea56` (Phase 4), `93b2643` (Phase 5), `8c33829` (Phase 5 follow-up), `6814b6f`, `d9ff720`, `c84672c`, `e11e244`, `3e359ee`, `4df47b8` (Phase 6), `39daf6f`, `449bbfe`, `7b07a31` (Phase 7), `ccccd47` (Phase 8), `21a4b45`, `b3b9a9e`, `32df611` (Phase 9), `9f0dacd` (merge into the campaign branch), `5b765f5`, `95b4423` (documentation and changelog corrections)

#### dependency remediation

**Phase 1 — repair the verification harness:**

- **Fixed**: `scripts/smoke-rag-images.sh` — the RAG image smoke test CI already runs on every PR touching `src/rag/**`/`src/shared/**` (`.github/workflows/rag-smoke.yml`) now runs `pip check` after the import smoke, so an unmet or conflicting dependency in a shipped image fails the build, not just a missing/broken import.
- **Fixed**: `.github/workflows/rag-generator-drift.yml` — the Dockerfile-generator drift check ran under GitHub's default shell (`bash -e {0}`, no `pipefail`), so `--check | tee drift-summary.txt` always reported success regardless of the generator's own exit code. Added `shell: bash` so the step's exit status is the check's, not `tee`'s.
- **Fixed**: `scripts/generate-rag-dockerfiles.py` — strict `--check` now treats a missing RAG service directory or Dockerfile as drift; previously a deleted service Dockerfile silently passed as "no drift detected."
- **Added**: `scripts/smoke-images.sh` — generalizes the RAG-only image smoke harness to all 29 Python images in the repo (the 23 RAG images plus admin-backend, chat-embed, jarvis-web, gateway, mode-service, orchestrator). Supports `--list`, `--list-excluded`, `--service <name>`, `--dry-run`.
- **Added**: `scripts/lock-requirements.sh` — a single `uv pip compile` wrapper for every image's dependency lock, so no two locks can be produced by a slightly different invocation. Discovers and compiles every `requirements.in` (including a `requirements-test.in`) in dependency order. `--check` detects a `requirements.in` that was edited without recompiling its lock; `--upgrade` recompiles every lock against current upstream versions; `--upgrade-package NAME` (repeatable, added in Phase 7) scopes an upgrade to named packages only, since a plain recompile preserves an already-satisfied pin even after its ceiling lifts.
- **Added**: `scripts/check-build-tooling.py` — asserts the pinned build-tooling triplet (`pip==26.2.1 setuptools==84.0.0 wheel==0.48.0`) precedes every dependency install in a Dockerfile stage. `make check-build-tooling` was added in Phase 6 once the triplet had a repo-wide population to check; CI wiring is tracked separately.
- **Added**: `Makefile` targets `smoke-images`, `lock`, `lock-upgrade`, `lock-check` (developer conveniences — CI and other automation should call the underlying `scripts/*.sh` directly, since `make` collapses every non-zero recipe exit code to `2`).

**Phase 2 — close the unauthenticated upload exposure at jarvis-web's `POST /api/voice/transcribe`** (`apps/jarvis-web/backend/main.py`, no auth dependency, fully attacker-controlled request body). Three changes were needed to close it:

- **Security fix (version-independent)**: the endpoint now rejects a request whose declared `Content-Length` exceeds **25 MB** (`MAX_AUDIO_UPLOAD_BYTES`, env-overridable) before the multipart parser runs, and separately bounds both the form parse and the file read with a **30 s** timeout (`AUDIO_UPLOAD_READ_TIMEOUT_SECONDS`, env-overridable) that catches a request with no, or a dishonest, `Content-Length`. This closes the DoS class at this endpoint regardless of which multipart/HTTP library version is resolved here. Also fixed in the same change: `HTTPException`s raised inside `transcribe_audio` (the new `408`/`413`, and the pre-existing `400`/`502`) were being silently rewritten to a generic `503` by the function's own catch-all `except Exception`, since `HTTPException` is itself an `Exception` subclass.
- **Security fix**: `apps/jarvis-web/backend/requirements.{in,txt}` — `python-multipart` `0.0.20` → `>=0.0.32` (locked at `0.0.32`), clearing seven advisories affecting `0.0.20`: CVE-2026-24486 (arbitrary file write, fixed 0.0.22), CVE-2026-40347 (0.0.26), CVE-2026-42561 (0.0.27), CVE-2026-53537 (0.0.30), CVE-2026-53538 (0.0.30), CVE-2026-53539 (0.0.30), CVE-2026-53540 (0.0.31). CVE-2024-53981 (CVSS 8.7) was already patched at `0.0.20` (fixed in `0.0.18`) and is not one of the seven cleared here.
- **Security fix**: `apps/jarvis-web/backend/requirements.{in,txt}` — `fastapi` `0.115.6` → `>=0.141,<0.142` (locked at `0.141.1`), moving the transitively-resolved `starlette` from `0.41.3` to `1.6.0`. The `python-multipart` bump alone left the same unauthenticated endpoint DoS-able through Starlette's own multipart parser: seven distinct advisories affect `starlette==0.41.3` (CVE-2025-54121 fixed 0.47.2, CVE-2025-62727 fixed 0.49.1, CVE-2026-48710 fixed 1.0.1, CVE-2026-48817 fixed 1.1.0, CVE-2026-48818 fixed 1.1.0, CVE-2026-54282 fixed 1.3.0, CVE-2026-54283 fixed 1.3.1) — all seven clear at `starlette==1.6.0`. `fastapi==0.115.6` pinned `starlette<0.42.0,>=0.40.0`, so re-pinning starlette alone was not possible; `0.141.1` is the same target Phase 4 uses for admin-backend.
- **Added**: `apps/jarvis-web/backend/requirements.in` — `requirements.txt` converted from a hand-pinned file to a generated, hashed, `x86_64-unknown-linux-gnu`-platform lock. Only `python-multipart` and `fastapi` (and its transitive `starlette`) moved; `httpx==0.28.1`, `pydantic==2.10.3`, `uvicorn==0.34.0`, `websockets==12.0` unchanged.
- **Fixed**: `apps/jarvis-web/Dockerfile` — pinned build tooling (`pip==26.2.1 setuptools==84.0.0 wheel==0.48.0`) added to the `AS builder` stage, above the line that installs the hashed lock.
- **Fixed**: `apps/jarvis-web/Dockerfile`'s production stage now removes its own factory-installed `pip`/`setuptools`/`wheel` before `COPY --from=builder` copies the pinned versions over — Docker's `COPY` onto an existing directory merges rather than replaces, so without this the base image's `pip==24.0`/`setuptools==79.0.1` (with their own CVEs, including CVE-2026-59890) survived on disk alongside the pinned 26.2.1/84.0.0, and `importlib.metadata`-based tooling resolved the stale, vulnerable one. Verified after the fix: exactly one dist-info per tool, `pip-audit` reports zero vulnerabilities in the finished image. `--no-build-isolation` intentionally **not** added here — this Dockerfile installs no editable package.
- **Changed**: `README.md` — the two jarvis-web dev-install commands (`:235`, `:347`) repointed from `requirements.txt` to `requirements.in`. Not a fix for a failure: installing the generated `requirements.txt` directly on Apple Silicon succeeds, by silently resolving macOS wheels instead of the x86_64 binaries the image ships — a reproducibility gap, not a break.
- **Documented, not fixed**: `apps/jarvis-web/backend/main.py:115-122` — `allow_origins=["*"]` + `allow_credentials=True` makes Starlette reflect the request `Origin` header verbatim, defeating same-origin credential protection; most of this app's routes (`/api/welcome`, `/api/climate`, `/api/sensors/*`, `/api/chat`) need no auth at all, so any origin's JavaScript can already read guest PII, HVAC state, and occupancy data regardless of credentials. Tracked separately; fixing it requires enumerating every legitimate origin that embeds this app, a separate consumer audit.
- **Ticketed, not fixed**: `apps/jarvis-web/Dockerfile:41-42` copies the builder stage's entire `site-packages` and `/usr/local/bin` into the production image, so the runtime image ships a working `pip` and its entrypoints — a post-RCE hardening gap. Tracked separately.

**Phase 3 — canonicalize the shared dependency declaration:**

- **Fixed**: deleted `src/shared/requirements.txt` — a duplicate of `src/shared/pyproject.toml`'s `dependencies` array with zero consumers anywhere in the tree outside this campaign's planning documents. `pyproject.toml` is now the sole source of shared's dependency spec.
- **Changed**: relocated the `httpx~=0.28` upgrade warning (the private `_pool` API dependency in `url_safety.py`'s `_build_pinned_transport`) from the deleted flat file to a comment directly above the pin in `pyproject.toml`, and recorded the same contract in `CONTRIBUTING.md`'s SSRF guard section.

**Phase 4 — resolve the admin-backend dependency graph, and commit the lock that was tested:**

- **Fixed**: `admin/backend/requirements.in` — four pin changes take the environment from unsatisfiable (`ollama>=0.4.9` needs `pydantic>=2.9`, the spec pinned `pydantic==2.5.0`) to satisfiable, and the admin-backend test suite from 203 errors to 0: `fastapi==0.104.1` → `>=0.141,<0.142` (resolves `0.141.1`), `pydantic==2.5.0` → `>=2.13,<3` (resolves `2.13.5`, forced transitively by `ollama`), `authlib==1.3.0` → `>=1.8,<2` (resolves `1.8.0`; `parse_id_token` gained `leeway=` in 1.8, closing 10 `TypeError` failures in `test_oidc_validation.py`), `python-multipart==0.0.6` → `>=0.0.32` (resolves `0.0.32`, closes the `TWILIO_AUTH_TOKEN`-gated admin-backend multipart exposure). `httpx~=0.28` deliberately untouched.
- **Added**: `admin/backend/requirements.in`/`requirements.txt` — `requirements.txt` renamed to `.in` and compiled to a generated, hashed, `x86_64-unknown-linux-gnu` lock. Verification installs from this committed lock, not a scratch file, so a later resolve can't land an untested FastAPI/starlette pair.
- **Added**: `admin/backend/requirements-test.in`/`requirements-test.txt` — a locked test environment (`pytest==9.1.1`, `pytest-asyncio==1.4.0`) compiled with `-c requirements.txt`, so no test dependency can resolve a different version of anything the production lock already pins.
- **Changed**: FastAPI 0.141.1 is stricter about request bodies than 0.104.1: a non-empty request body sent with no `Content-Type` header now returns `422` (a request with no body, or any `Content-Type` value, is unaffected); `Bearer` credentials are also whitespace-stripped before comparison. All internal outbound POST callers of admin-backend (`src/shared/admin_config.py`, `src/orchestrator/main.py`) already use `json=` or target endpoints with no JSON body parameter, so none is affected. An external caller using the `X-API-Key` path that posts with `data=json.dumps(...)` and no explicit header must switch to `json=` or set the header itself.
- **Disclosed**: the resolved lock carries `starlette==0.52.1`, which still has 5 distinct CVEs — CVE-2026-48710, CVE-2026-48817, CVE-2026-48818 (Windows-only, not applicable to this deployment), CVE-2026-54282, CVE-2026-54283 — with fix floors `1.0.1`–`1.3.1` that require starlette's `1.x` major, delivered in Phase 7. This is a strict reduction from `main`'s pre-remediation `starlette==0.27.0` (7 distinct CVEs: the above 5 plus CVE-2024-47874 and CVE-2025-54121, both already fixed at 0.52.1) — Phase 4 introduced no new advisory. `admin/backend/app/routes/sms_webhook.py:48,56` reconstructs `str(request.url)` for Twilio HMAC verification; `main.py` registers no `TrustedHostMiddleware`.
- **Known limitation**: `test_success_path_not_artificially_delayed` (`admin/backend/tests/test_security_hardening.py`) is a wall-clock assertion (`elapsed_ms < 1500`) that fails only when the test suite runs under amd64 emulation on non-x86 hardware (measured about 4.2–6.4 s under amd64 emulation, ~67× slower than native); it passes natively on arm64. Pre-existing; the admin-backend suite is otherwise clean across Phases 4, 6, and 7 (0 errors, 0 unexplained failures).

**Phase 5 — fix 4 order-dependent test failures (test files only, no skips, xfails, or deletions):**

- **Fixed**: `admin/backend/tests/test_security_hardening.py::TestTwilioSignatureValidation` — three tests used `asyncio.get_event_loop().run_until_complete(...)` instead of `asyncio.run(...)`, the only async calls in the suite doing so; `asyncio.run` closes the loop after each call, stranding these three once every other async test had moved to it.
- **Fixed**: `admin/backend/tests/test_phase2_consolidation.py::TestPhase2ReconcileRegressions::test_integrations_healthy_path_uses_rag_service_host` — a dotted-string `monkeypatch.setattr` target could fail depending on test collection order, because a different test file's module-eviction fixture doesn't always leave `app.routes` re-attached to a freshly re-imported `app`. Fixed by importing `app.routes.integrations` inside the test function, immediately before the `monkeypatch.setattr` call, so the binding is always current relative to whatever module tree is live at that point.

**Phase 6 — clear the non-major CVE surface, pin build tooling across the remaining images, and harden the CVE-allowlist guard:**

- **Fixed**: `admin/backend/requirements.in` — three bumps clear the two remaining auth-path advisories: `aiohttp==3.9.1` → `>=3.14.3,<4` (resolves `3.14.3`), `python-jose[cryptography]==3.3.0` → `>=3.5,<4` (resolves `3.5.0`), `python-dotenv==1.0.0` → `>=1.2.3,<2` (resolves `1.2.3`). `starsessions`, `starlette`, and `httpx` deliberately untouched — the starlette major is Phase 7's alone.
- **Fixed**: build tooling (`pip==26.2.1 setuptools==84.0.0 wheel==0.48.0`) pinned ahead of every dependency install across the remaining 28 Python images — the 5 hand-written Dockerfiles that lacked the full triplet (`admin/backend`, `apps/chat-embed`, `src/gateway`, `src/mode_service`, `src/orchestrator`) and the RAG Dockerfile generator template, regenerated across all 23 RAG service Dockerfiles. `apps/jarvis-web/Dockerfile` already carried this pin from Phase 2. `setuptools==84.0.0` ships zero `pkg_resources/` entries (a real removal, fixing CVE-2026-59890 whose floor is `83.0.0`; no downgrade below that floor is possible without reintroducing it). A direct `import pkg_resources` was checked on seven images: `athena-gateway`, `athena-mode-service`, `athena-orchestrator`, `athena-chat-embed`, `athena-admin-backend`, `athena-rag-sitescraper`, `athena-rag-weather`. It's absent in all seven, and none of them needs it. `athena-orchestrator` and `athena-rag-sitescraper` were additionally checked with unguarded imports of the optional content-fetcher packages (`trafilatura`, `extruct`, `pandas`, `playwright` in `src/shared/content_fetcher.py`) they actually ship.
- **Added**: `scripts/audit-images.sh` + `make audit-images` (`SCOPE=remediated` scopes to `athena-admin-backend` and `athena-jarvis-web`). Builds each image (`docker buildx build --platform linux/amd64`), freezes its installed package list from its own Python (`pip freeze --all --exclude-editable`), then audits that frozen list from a throwaway venv that never touches the built image, so the reported advisory surface reflects what the pinned build tooling and committed lock actually produce. `pip-audit`'s exit code is captured to a sidecar file rather than swallowed; only rc 0 (clean) or rc 1 (findings) with parseable JSON counts as a result, anything else is `TOOL_ERROR` (exit 2). Carries exactly one `--ignore-vuln`: `PYSEC-2026-1325` (`ecdsa`, transitive via `python-jose[cryptography]`), unreachable under this codebase's HS256-only JWT usage — `admin/backend/app/auth/oidc.py` calls `jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])` with `JWT_ALGORITHM = "HS256"` at both call sites — gated on `scripts/check-jwt-algorithm-guard.py` passing first; a guard failure withholds the allowlist for that run.
- **Added**: `scripts/check-jwt-algorithm-guard.py` — an AST-based check (immune to a match inside a comment or string) that resolves every python-jose import form (`import jose`, `import jose.jwt`, `from jose import jwt`/`jws`, `from jose.jwt import decode/encode`, `from jose.jws import verify/sign`, and attribute chains built on any of them) to a canonical name, then verifies `JWT_ALGORITHM == "HS256"` (the single top-level literal in `oidc.py`, with no other rebinding permitted), that every `jwt.decode`/`jws.verify` call site under `admin/backend/app` (excluding tests) and `src/shared` passes `algorithms=` naming only `HS256`/`JWT_ALGORITHM`, and that every `jwt.encode`/`jws.sign` call passes `algorithm="HS256"` or `JWT_ALGORITHM`. A non-UTF-8 file or other tool failure exits 2 rather than passing silently. Covered by `tests/unit/test_jwt_algorithm_guard.py` (16 cases). **Known limitation**: dynamic-dispatch forms — an intermediate-variable alias (`x = jwt; x.decode(...)`), a star import, `importlib.import_module`, or `setattr` on `JWT_ALGORITHM` — are not detected; the current tree is clean of all of these, but the guard cannot prove it stays that way. Tracked as a follow-up.
- **Fixed**: `scripts/audit-images.sh`, `scripts/smoke-images.sh`, `scripts/smoke-rag-images.sh`, and `scripts/lock-requirements.sh` each had at least one `set -u` empty-array expansion (e.g. any image with no shared-copy step) that aborted or silently swallowed the script's real exit status under macOS's stock bash 3.2. Fixed to the `"${arr[@]+"${arr[@]}"}"` form; verified directly under bash 3.2.57.
- **Documented (operator note)**: python-jose 3.5 rejects a `JWT_SECRET`/`SESSION_SECRET_KEY` that contains a PEM header or SSH key-type substring, mistaking it for asymmetric key material — `jwt.encode` raises `JWSError`, `jwt.decode` raises `JWKError`, and neither subclasses `JWTError`, so both escape `oidc.py`'s `except JWTError` handling: login and every authenticated request return `500`, not `401`. Generate these secrets as plain random strings (e.g. `openssl rand -base64 32`, per `docs/CONFIGURATION.md`), never as a copy-pasted key file.
- **Audit result**: `bash scripts/audit-images.sh --scope remediated` exits 1 (findings), not 0, at the end of Phase 6. `athena-jarvis-web` is clean. `athena-admin-backend` carries 5 distinct unallowlisted advisories on `starlette==0.52.1` (`PYSEC-2026-161`, `PYSEC-2026-248`, `PYSEC-2026-249`, `PYSEC-2026-2280`, `PYSEC-2026-2281`) — the same 5 CVEs disclosed above under Phase 4. Only Phase 7's starlette major clears them; no second `--ignore-vuln` was added for them.

**Phase 7 — the starlette major:**

- **Fixed**: `admin/backend/requirements.in` — `starsessions[redis]==2.1.3` (which requires `starlette>=0,<1`, and was the sole gate blocking the starlette major) → `>=2.2.1,<3`. Recompiled the lock scoped to exactly the two packages this lifts: `starlette` `0.52.1` → `1.6.0`, `starsessions` `2.1.3` → `2.2.1`. Nothing else in the lock moved. Clears all 5 residual starlette advisories from Phase 4/6: `bash scripts/audit-images.sh --scope remediated` now reports both remediated images clean besides the one allowlisted `PYSEC-2026-1325` residual.
- **Verified**: `starsessions==2.2.1`'s `SessionMiddleware.__init__` signature, `RedisStore(connection=, prefix=)`, and `InMemoryStore()` are unchanged from `2.1.3`; `request.session` behaves identically under `starlette==1.6.0`. No changes needed to `admin/backend/main.py`'s or `app/routes/local_auth.py`'s session-middleware usage. The admin-backend test suite is unchanged from Phase 6 (same one pre-existing wall-clock flake under emulation, see Phase 4; every session/middleware/cookie/OIDC/WebSocket test passes).
- **Pending, blocking for merge (not for this commit)**: manual confirmation in a browser that the session cookie set by `/api/auth/login` carries `Secure` and `SameSite=Lax`, that `/api/auth/logout` clears it, and that a second, independent browser session is unaffected by the first one's logout.
- **Fixed**: `scripts/lock-requirements.sh`'s `--upgrade-package`, `--input`, `--output`, and `--constraint` each crashed with an "unbound variable" error when their value was missing, or silently consumed the next flag as their own value. Each now fails cleanly (`FAIL: <flag> requires a value`, non-zero exit); a following token starting with `--` is treated as a missing value too. `--check` combined with `--upgrade`/`--upgrade-package`, in either order, now fails (`FAIL: --check cannot be combined with --upgrade/--upgrade-package`) instead of letting a verification run mutate pins.
- **Ticketed, not fixed**: `regenerate_session_id()` is never called on login (`admin/backend/app/routes/local_auth.py:167-169`, `main.py:817-818,888-889`) — a pre-existing session-fixation gap, unrelated to and unaffected by this phase's library bump. Tracked as a follow-up.
- **Revert note**: Phase 7 ships as three commits — `39daf6f`, `449bbfe`, `7b07a31`. To undo it as a group: revert `7b07a31`, then `449bbfe`, then `39daf6f`, in that order. Only `CHANGELOG.md` conflicts (resolvable by restoring the pre-Phase-7 text); reverting `39daf6f` before `7b07a31` instead conflicts in `scripts/lock-requirements.sh`. The result restores `admin/backend/requirements.in` to `starsessions[redis]==2.1.3`, both admin-backend locks (`requirements.txt`, `requirements-test.txt`) to `starlette==0.52.1`/`starsessions==2.1.3`, and `scripts/lock-requirements.sh` to its pre-Phase-7 form. It changes no other image's lock or Dockerfile.

**Phase 8 — lock the remaining 27 images:**

- **Added**: the remaining 27 image-mapped `requirements.txt` files (23 RAG services, `apps/chat-embed`, `src/gateway`, `src/mode_service`, `src/orchestrator`) converted to `requirements.in`, then compiled into generated, hashed, `x86_64-unknown-linux-gnu`-platform locks. Every image-mapped dependency spec in the repo is now a compiled lock (29 total, alongside `admin/backend` and `apps/jarvis-web/backend` from earlier phases). Resolved versions match a fresh install today across all 27: `fastapi==0.141.1`, `pydantic==2.13.5`, `httpx==0.28.1`, `starlette==1.6.0`, `uvicorn==0.53.0` — locking freezes current behavior, since every non-admin spec floors with `>=` rather than pinning.
- **Added**: a "deliberately unlocked" header on the three requirements files that intentionally stay unlocked, each naming the target platform and why an `x86_64-Linux` lock would be wrong for it: `./requirements.txt` (a developer's own machine), `src/control_agent/requirements.txt` (an arm64 macOS host), `src/jetson/requirements.txt` (an aarch64 Jetson edge device — `torch` ships Jetson-specific wheels from NVIDIA's own index, not PyPI's x86_64 build).
- **Added**: `.gitattributes`, marking all 29 image-mapped locks plus `admin/backend/requirements-test.txt` as `linguist-generated` (kept out of human review diffs).
- **Fixed**: `CONTRIBUTING.md`'s RAG-service checklist named `requirements.txt` (now a generated lock) as the file to hand-edit — corrected to `requirements.in` plus `make lock`. A new test-only dependency belongs in `admin/backend/requirements-test.in`, with `pytest-httpserver` documented as the one grandfathered exception still living in the production spec.
- **Disclosed**: an image built from Phase 8 alone still installs `src/shared`'s dependencies through an unhashed editable install (`pip install -e /app/shared`) that runs before the hashed lock — unconstrained, and able to resolve any version satisfying `src/shared/pyproject.toml`'s own floors independent of what the lock pins. Closed in Phase 9.

**Phase 9 — the image consumes exactly the lock:**

- **Fixed**: every shared install across all 27 shared-installing images (23 RAG services, `admin/backend`, `src/gateway`, `src/mode_service`, `src/orchestrator`) changed from `pip install --no-cache-dir -e /app/shared` to `pip install --no-cache-dir --no-deps --no-build-isolation -e /app/shared`. `--no-deps` makes the hashed lock — installed in the same stage immediately after — the single source of every installed package version. `--no-build-isolation` closes a second gap: `src/shared/pyproject.toml`'s own build-backend floor (`setuptools>=61.0`, unpinned `wheel`) would otherwise resolve fresh over the network inside an isolated build environment, bypassing the pinned triplet entirely. All 9 of `src/shared/pyproject.toml`'s dependencies are pinned with `==` in all 27 shared-installing locks. The 23 RAG Dockerfiles were regenerated from the generator template (never hand-edited); the 4 hand-written Dockerfiles (`admin/backend`, `src/gateway`, `src/mode_service`, `src/orchestrator`) were edited directly. `apps/chat-embed` and `apps/jarvis-web` install no shared module and are unchanged.
- **Known limitation**: the four hand-written Dockerfiles' `--no-deps`/`--no-build-isolation` line and the 29 `.in`-to-`.txt` lock syncs have no CI gate — enforcement today is local (`make lock-check`, `scripts/check-build-tooling.py`, `scripts/generate-rag-dockerfiles.py --check`, and `scripts/smoke-images.sh`). The lock content itself is still consumed on every image build regardless, and the 23 generated RAG Dockerfiles keep their drift gate (the generator `--check`). Tracked as a follow-up.
- **Known limitation**: the `pip`/`setuptools`/`wheel` bootstrap triplet is pinned by version across all 29 images, not by hash — a supply-chain gap one level below the hashed dependency locks. Tracked as a follow-up.
- **Documented**: the orchestrator lock's `httpx2`, `httpcore2`, and `langchain-protocol` entries are legitimate upstream dependencies, not typosquats — `httpx2`/`httpcore2` are pulled in by `langsmith` and by starlette 1.6's own `TestClient`; `langchain-protocol` is pulled in by `langchain-core` and `langgraph-sdk`.
- **Fixed**: `scripts/audit-images.sh`'s isolated pip-audit venv was created at `/audit-venv`, a path requiring write access to the container filesystem root — 26 of 29 images set a non-root `USER athena`, so the audit could not complete on any of them. Moved the venv into a `mktemp`-created directory under `/tmp`, which every base image in this repo makes world-writable.
- **Corrected**: `CONTRIBUTING.md`'s httpx version contract now states the current mechanism: httpx is pinned exactly (`httpx==0.28.1`) in every one of the 29 generated locks; for the 27 images compiled against `src/shared/pyproject.toml`, that pin is fixed at lock time by `scripts/lock-requirements.sh`, not at image-install time (the shared install now installs zero dependencies); changing it means editing the `src/shared/pyproject.toml` constraint and running `make lock`.
- **Fixed (Security)**: `POST /api/auth/local-login`, `POST /api/auth/ws-ticket`, and all 6 service-registry write endpoints returned `500` on every request once the rate limiter was active (i.e. whenever Redis was reachable at startup — production), because `fastapi-limiter==0.1.6`'s route-introspection crashes under FastAPI 0.141.1's `_IncludedRouter` wrapper. `admin/backend/app/utils/rate_limit.py` no longer calls `fastapi_limiter.depends.RateLimiter`; the replacement enforces the same per-route, per-budget limits directly against Redis without touching `request.app.routes`. A lost Lua script cache (Redis restart, failover, or `SCRIPT FLUSH`) is now recovered transparently instead of crashing; a mid-request Redis outage now fails open (the request proceeds normally, logged) rather than returning `500`.

> **Plan:** `thoughts/shared/plans/2026-05-15-deliver-auth-deferred-hardening.md` (r2)
> **Commits:** `179fd8c` (Phase 1), `77f50d6` (Phase 2), `f956be2` (Phase 3), `9b2926e` (Phase 4)

#### auth-deferred-hardening

- **Added**: `OIDC_VALIDATE_ISS` env flag (default `true`, opt-out). When set to `false`, the issuer-**mismatch** startup gate (branch (d) in `_enforce_oidc_runtime_gates`) is softened from `SystemExit` to `logger.warning`, and authlib's own `iss` check is relaxed via `claims_options={"iss": {"essential": False}}` on the OIDC callback at `main.py:815`. **Scope is deliberately narrow**: the flag relaxes issuer *mismatch* only — it does **not** let the service boot with an empty/placeholder issuer, an unreachable IdP, or a discovery doc missing the `issuer` field (those branches remain fatal `SystemExit` regardless of the flag). Startup emits `oidc_iss_validation_disabled` warning when the flag is false; flag state is exposed on `GET /api/auth/methods` as `"oidc_iss_validation"` for runtime observability. **Audience validation note**: authlib (which this codebase uses for the OIDC callback) validates the `aud` claim via `IDToken.validate_azp` against the registered `OIDC_CLIENT_ID`; `OIDC_VALIDATE_ISS=false` has no effect on audience validation.

- **Added**: `POST /api/auth/ws-ticket` — authenticated, rate-limited endpoint that mints a short-lived (45 s) single-use WS ticket. Ticket claims: `{user_id, ws_ticket: true, aud: "ws", jti: <uuid4>, exp: now+45s}`. Authentication via `Depends(get_current_user)` (not a session read). Rate-limited via `Depends(login_rate_limit_dep)`. Response shape: `{"ticket": "<jwt>", "ws_ticket_supported": true}`. The `ws_ticket_supported` marker is the capability signal the frontend uses; an old backend without this endpoint returns 404.

- **Changed**: `admin/backend/app/routes/websocket.py` — WS handler now accepts **both** a `ws_ticket` JWT (audience-scoped, single-use via Redis) and a legacy session JWT (`?token=`) during the one-release deprecation window. Ticket path: `oidc.decode_ws_ticket(token)` (validates `aud="ws"` positively via PyJWT `audience="ws"`) → requires `payload.get("ws_ticket") is True` (identity, not truthy) → single-use claim via `athena:ws_ticket:<jti>` Redis key (DEV_MODE: in-memory set). Legacy path: falls through to `decode_access_token` on `InvalidAudienceError`; logs `websocket_legacy_token_auth_deprecated`. Origin checked against `CORS_ORIGINS` at upgrade time; mismatch → close 4003. `JWT_SECRET`/`JWT_ALGORITHM` consolidated to the `oidc` module (no second `os.getenv` read in `websocket.py`). Token fragment logging removed.

- **Changed**: `admin/backend/app/utils/oidc.py` — `decode_access_token` / `get_current_user` now reject any token carrying `aud == "ws"` or `ws_ticket is True` with HTTP 401 (WS ticket is not a valid REST credential — lateral-path closure). New `decode_ws_ticket(token)` helper validates `aud="ws"` positively, keeping one source of truth for the JWT secret.

- **Changed**: `admin/frontend/admin-jarvis.js` — `connectWebSocket()` now:
  - POSTs `/api/auth/ws-ticket` (Bearer session token) and uses the returned ticket in the WS URL; fresh mint on every (re)connect — no cached URL.
  - **Mint-time fallback** (404 only): old backend with no ticket endpoint → legacy session JWT in `?token=`. Status 401/403/5xx → fail loudly, no fallback.
  - **Upgrade-time capability fallback**: WS close 4001 + `ticketMintStatus === 200` + `!legacyRetried` → one retry with legacy session JWT (mixed-rollout gap where a new pod mints the ticket but an old pod handles the WS upgrade and rejects `aud="ws"`). 4003, second 4001, or mint status ≠ 200 → fail loudly. `legacyRetried` resets per `connectWebSocket()` invocation.
  - `console.log` logs host+path only (never `?token=` query string) — xander C-2.

- **Operational note — deploy ordering**: the admin **backend** (`athena-admin-backend`, `replicas: 2`, RollingUpdate) must be fully rolled out to the ticket-aware image **before** the new admin-frontend image is promoted. Both deployments roll independently with no `sessionAffinity`; the capability fallback above covers the residual window (e.g. a backend pod restart mid-frontend-rollout). Gate: `kubectl rollout status deployment/athena-admin-backend -n athena-prod`.

- **Security review follow-ups (xander mid-build, same campaign):**
  - **(M-1) Origin policy documented**: absent `Origin` header (non-browser clients — curl, scripts, monitors) is now explicitly **allowed** on WS upgrade. Non-browser clients have no CSRF surface; the Origin check defends against browser-based cross-origin upgrades only. Only a PRESENT-but-mismatched Origin closes 4003. A startup log line (`websocket_origin_absent_allowed`) confirms the policy at runtime. Tests: `TestWsOriginPolicy`.
  - **(M-2) Dead variable removed**: `aud_mismatch` local variable in the WS handler was assigned but never read; removed. Added an explanatory comment on the legacy fallthrough condition clarifying it is entered on ANY `JWTClaimsError` / decode failure, not only `aud` mismatch, and why the fallthrough is safe (legacy `decode_access_token` independently rejects `aud="ws"` tokens).
  - **(L-1) Replay guard test**: static ordering assertion confirms `_claim_jti` replay-rejection (close 4001 + return) appears before the legacy fallthrough path in source, proving a replayed ticket can never be laundered through `decode_access_token`. Unit test also verifies `_claim_jti` returns `False` on second call. Tests: `TestWsReplayGuardL1`.
  - **(L-2) Audience list-form regression pin**: token with `aud=["ws","api"]` (JSON array) as REST Bearer → 401. python-jose may deserialize list-form `aud` back to a list; the `ws_ticket=True` discriminator check is the primary guard in that case. Pinned explicitly. Tests: `TestWsAudienceListFormL2`.

- **Follow-up (Phase 6, next release — out of scope)**: remove legacy `?token=` session-JWT acceptance from `websocket.py` and the frontend 404-fallback. Tracked as a follow-up; not built here.

> **Plan:** `thoughts/shared/plans/active/2026-06-12-deliver-rbac-ssrf-partial.md` (r4)

#### SSRF guard: shared safe_request helper wired into user/admin URL fetch chokepoints

- **Added**: `src/shared/url_safety.py` — canonical SSRF guard module. Exports `validate_url_not_private` (sync, never-raises, never-reads-env), `safe_request`/`safe_get`/`safe_post` (async, IP-pinned transport, per-hop revalidation, ~10 MB cap), `SsrfBlockedError`, `UrlSafetyResult` (frozen dataclass). DNS-rebinding mitigated via `_PinnedNetworkBackend` that dials the validated IP while httpcore's TLS layer preserves SNI hostname. POST 301/302/303 redirects downgrade to GET and strip body/credential headers on cross-origin hops; POST 307/308 redirects are refused.
- **Changed**: `src/rag/site_scraper/main.py` — `is_url_allowed` now performs literal-IP detection and domain allow/block matching (`_domain_matches`); it does NOT call `validate_url_not_private` (DNS-resolving guard lives in the async fetch path via `safe_get` to avoid blocking the event loop). `blocked_domains` and `allowed_domains` substring matching replaced with exact-host/suffix matching (`_domain_matches`) to close attacker bypass via `evil.com.attacker.net` (xander H-1).
- **Changed**: `admin/backend/app/routes/calendar_sources.py` — `fetch_ical_data` now uses `safe_get(allowed_schemes=frozenset({"https"}))` — enforces HTTPS on every redirect hop.
- **Changed**: `src/shared/content_fetcher.py` — default client changed to `follow_redirects=False`; all HTTP fetches on user-supplied URLs route through `safe_get`; Playwright paths gated behind `CONTENT_FETCHER_ALLOW_BROWSER_FETCH` (default `false`).
- **Changed**: `admin/backend/app/routes/tool_calling.py` — MCP discovery POST for Class-1 sources (request body / feature-flag DB rows) replaced with `safe_post`; Class-3 (N8N_MCP_URL env) kept exempt.
- **Changed**: `src/shared/tool_registry.py` — MCP POST for Class-1 feature-flag rows replaced with `safe_post`; Class-3 (N8N_MCP_URL env) kept exempt.
- **Changed**: `admin/backend/app/services/health_poller.py` — `_validate_service_url` promoted to `async def`; local `_PRIVATE_NETS` duplicate retired; IPv6 bare addresses bracketed before URL construction; delegates to shared `validate_url_not_private`.
- **Changed**: `admin/backend/app/routes/services.py` — all 5 `aiohttp.ClientSession.get()` calls in connector health checks set `allow_redirects=False`; missing `await` on Redis SSRF guard call fixed.
- **Changed**: `admin/backend/app/routes/rag_connectors.py` — all 6 `session.get()` calls set `allow_redirects=False`.
- **Changed**: `admin/backend/app/routes/music_config.py` — `HA_URL` hardcoded fallback `http://192.168.10.168:8123` removed; empty default with startup log warning (OSS-First rule).
- **Added**: `src/shared/config.py` — `sitescraper_allowed_private_hosts` (str, default `""`), `content_fetcher_allow_browser_fetch` (bool, default `false`).
- **Added**: `tests/unit/test_url_safety.py` — 69 tests covering all SSRF guard contracts, IP-pinning PoC, POST redirect semantics, safe_request/safe_get/safe_post wrappers.
- **Fixed**: Health poller `TestSSRFGuard` tests updated to `asyncio.run()` for async `_validate_service_url`; `test_phase4_reconcile.py` and `test_codex_r2_reconcile.py` updated similarly.
- **Fixed**: Streaming size cap in `safe_request` now iterates `aiter_bytes()` and aborts before buffering the full body (H-1 gate fix — previous `aread()`-then-check defeated the cap).
- **Fixed**: `discover_mcp_tools` reads `N8N_MCP_URL` env exactly once into a local before URL resolution and Class-1/Class-3 classification to prevent mismatch (H-3 gate fix).
- **Fixed**: `_domain_matches` in `site_scraper/main.py` strips trailing dots on both domain and pattern (M-3 gate fix — trailing dot previously bypassed exact-match).
- **Fixed**: `is_url_allowed` in `site_scraper/main.py` no longer calls blocking `socket.getaddrinfo` synchronously from async handlers; DNS-resolving guard lives only in the async `safe_get` fetch path (M-1 gate fix).
- **Fixed**: `/health` endpoint `browser_rendering` field now reflects `HAS_PLAYWRIGHT AND CONTENT_FETCHER_ALLOW_BROWSER_FETCH` gate (IAN-9).
- **Added**: TLS SNI PoC tests prove `_PinnedNetworkBackend` dials the pinned IP and the httpcore pool uses the original hostname for SNI (H-2 gate fix).
- **Added**: IPv6 ULA URL-form parametrized tests (`http://[fc00::1]/`, `http://[fd00::1]/`) confirm ULA addresses are blocked (M-4 gate fix).
- **Added**: Tool registry localhost:5678 default fail-closed test — asserts `SsrfBlockedError` is caught, `_mcp_tools` stays empty, and `mcp_tool_registry_ssrf_blocked` log fires (IAN item 10).

##### Deployer migration guide

**Behavior changes from this release — action required before upgrading:**

**(a) Implicit `localhost:5678` MCP default is now fail-closed.**
`tool_registry._load_mcp_tools` falls back to `http://localhost:5678/mcp` when
`N8N_MCP_URL` is unset and no feature-flag row is configured.  Loopback is in the
blocked CIDR, so this default now silently returns an empty MCP tool list.
To restore previous behavior: set `N8N_MCP_URL=http://localhost:5678/mcp` (Class-3
env — unguarded) **or** add `localhost` to `SITESCRAPER_ALLOWED_PRIVATE_HOSTS`
(Class-1 allowlist, kept guarded).

**(b) `http://` and private-IP iCal sources are now rejected.**
`fetch_ical_data` requires HTTPS on every redirect hop
(`allowed_schemes=frozenset({"https"})`).  iCal sources served over plain HTTP or
pointing at private-IP hosts will return HTTP **400** (not 422).
Migration: migrate sources to HTTPS; for private-network iCal servers add their
hostname/CIDR to `SITESCRAPER_ALLOWED_PRIVATE_HOSTS` AND change the URL to HTTPS.

**(c) Playwright browser fallback is now default-off.**
Content fetcher's Playwright path is disabled by default
(`CONTENT_FETCHER_ALLOW_BROWSER_FETCH` defaults to `false`).  JS-rendered pages
that previously relied on the headless browser fallback will now return plain-HTTP
content only.  To restore: set `CONTENT_FETCHER_ALLOW_BROWSER_FETCH=true`
(accepts the R3 residual — no per-hop SSRF guard inside Playwright).

**(d) `music_config` `HA_URL` hardcoded default removed.**
The `http://192.168.10.168:8123` fallback in `music_config.py` is gone.  Deployments
that relied on the default must set `HA_URL` explicitly in their env/ConfigMap.
An unset `HA_URL` now logs a startup warning and returns HTTP 503 on music requests.

**(e) SSRF IP-pinned transport requires `httpx~=0.28`.**
`src/shared/url_safety._build_pinned_transport` replaces `httpx.AsyncHTTPTransport._pool`
(a private httpx 0.28 API) to close the DNS-rebinding TOCTOU window.
`src/shared/pyproject.toml` and `admin/backend/requirements.txt` are both pinned to
`httpx~=0.28`.  If a dependency conflict forces an older httpx version at image-build
time, the helper detects the missing `_pool` attribute at runtime and falls back
gracefully: per-hop SSRF validation remains active, but the IP-pinned transport is
disabled and a `url_safety_pinned_transport_unavailable` structured-log warning fires
so the operator can see the degraded state.  Check logs on first startup after upgrade.

> **Commits:** `67075c1` (Phases 1/1b — observability), `a780bfa` (Phase 2 — harness), `d6213a8` (codex r2 + valerie reconciliation)

#### Orchestrator benchmark observability + tool-calling harness

- **Added**: `skip_semantic_cache` field on `POST /query` request body (`OrchestratorState`). When `true`, the semantic-cache lookup and write are both skipped for that request. This prevents cached responses from contaminating repeated benchmark runs. The field is also included in `QueryResponse.metadata` so callers can confirm it was honoured.
- **Added**: `metadata.model_component_used` — the actual model tag used by the tool-calling node for the turn (e.g. `"qwen3:4b"`). Populated from the component model assignment resolved at query time; falls back to `null` when the tool-call node was not reached.
- **Added**: `metadata.model_component_name` — the component-model config name resolved by the router (e.g. `"tool_calling_simple"`). Separate from `model_component_used` to distinguish the router's component decision from the actual model tag.
- **Fixed**: helper-cache invalidation in the tool-calling node — a stale cache entry could return the prior turn's component assignment after a model config change. Cache is now keyed on the component name so config changes are picked up on the next turn.
- **Added**: `scripts/bench_tool_calling.py` — benchmark harness for A/B tool-calling trials. Sends each query from `bench/query_set.yaml` N times against a live orchestrator (`--n` runs per query, default 20), records per-turn JSONL rows, and writes results to `bench/results/`. Key bindings per run: `temperature=0.1`, `skip_semantic_cache=true`. Pass `--self-test` to validate query-set, scoring logic, and fallback attribution without a live host.
- **Added**: `scripts/bench_report.py` — aggregates one or two JSONL result files into a human-readable per-component and per-cell summary (correct-tool rate, false-positive rate, p50/p90 latency). Pass one file for a single-cell summary; pass two for an A/B diff.
- **Added**: `bench/query_set.yaml` — 40-query synthetic benchmark set covering all tool-calling components (simple, complex, super-complex) plus none-tagged turns for false-positive measurement. No real user data.
- **Added**: `bench/README.md` — JSONL schema, decision gates (Gate 1: correct-tool rate +5pp; Gate 2: FP rate ≤ 15% absolute and ≤ qwen3+5pp relative; Gate 3: p90 latency ≤ incumbent × 1.10), environment contract, attribution-fallback rule, and committed-results policy.

**Status:** Benchmark executed 2026-06-12. Decision: **NO-SWAP** — gemma4 QAT challengers failed all three gates; qwen3:4b-instruct-2507-q4_K_M remains on all tool-calling components; config unchanged. See the committed benchmark decision record in `bench/results/`.

> **Plan:** `thoughts/shared/plans/2026-05-15-deliver-audit-deferred-cleanup-batch.md`

#### Audit-deferred cleanup-batch reconciliation

- **Changed**: `admin-backend DEV_MODE startup gate now allows local-host Postgres with WARNING instead of FATAL (xander:6 carve-out). Production K8s pods still fail fatally via KUBERNETES_SERVICE_HOST guard. See `admin/backend/app/utils/url_validators.py:151` and `admin/backend/main.py:381-426`.`
- **Fixed**: `Migration 058 clears legacy `oidc_redirect_uri`/`oidc_provider_url` rows in the `secrets` table that were seeded with the maintainer's domain values prior to commit `5403a8a`. Requires `ENCRYPTION_KEY` to be set. Supports `DRY_RUN_058=true` for rehearsal. No-op on fresh deployments. (bob:1 follow-up)`

> **Plans:** `thoughts/shared/plans/active-2026-05-11-deliver-rag-services-table-rename.md`, `thoughts/shared/plans/active-2026-05-11-deliver-health-poller-leader-election.md`

#### Rename `rag_services` table to `athena_service_registry`

- **Changed**: Alembic migration `057_rename_rag_services_to_athena_service_registry.py` renames the `rag_services` table to `athena_service_registry`. No data loss; downgrade restores the original name. SQLAlchemy model `RagService` updated to `__tablename__ = "athena_service_registry"`. All ORM queries, route docstrings, and YAML comments updated to reference the new table name.

#### Health-poller leader election

- **Added**: Redis SETNX-based leader election in `admin/backend/app/services/health_poller.py`. With `replicas: 2`, only one replica polls and writes health columns per cycle; the non-leader yields the iteration. A strict-abort per-cycle heartbeat task (every `HEARTBEAT_INTERVAL_SECONDS=20`) renews the lease atomically via Lua check-and-set and cancels the in-flight `_poll_all_services` task if the lease cannot be renewed, preventing any replica from writing after lease loss. New env var: `ATHENA_NAMESPACE` (default `athena-prod`, introduced in a prior phase of the SSRF-guard work via downward API injection) — the Redis lease key is namespaced using this value so concurrent deployments in different namespaces do not share a lease. Leader election otherwise uses the existing `REDIS_URL`. `HEALTH_POLL_INTERVAL_SECONDS` must remain < 40s (`LEASE_TTL_SECONDS`); startup raises `SystemExit FATAL` if this invariant is violated.

> **Plan:** `thoughts/shared/plans/2026-05-09-deliver-validator-fix-general-info.md`

#### Validator training-knowledge bypass

- **Fixed**: `src/orchestrator/nodes/validate_node` — validator no longer rejects GENERAL_INFO and conversation-context responses synthesized from LLM training knowledge when no retrieved data and no base knowledge are available. Previously, `validate.py:189`'s fact-check prompt ("ANY specific factual claims are likely hallucinations" when no Retrieved Data is present) caused false-positive Layer 4 rejections for responses that `synthesize_node` itself explicitly allowed using training knowledge (`synthesize.py:129-144` for `GENERAL_INFO`, `synthesize.py:152-166` for any intent with conversation history). First-turn current-domain queries (WEATHER, SPORTS, STOCKS, NEWS, etc.) with no RAG data continue to run the LLM fact-check because their synthesize branch (`synthesize.py:167-179`) tells the model not to invent specifics — Layer 4 protection is correctly aligned there. WEBSEARCH is carved out even with conversation context (freshness implied by intent).
- **Added**: new Prometheus metric label `validation_counter{passed="true", reason="training_knowledge_fallback"}` for observability of the bypass path. No env var or API change.

> **Plan:** `thoughts/shared/plans/2026-05-09-deliver-rag-oss-dep-cleanup.md`

#### RAG OSS dependency cleanup

- **Fixed**: `src/rag/sports/requirements.txt` — added `feedparser>=6.0.10`. Service had `import feedparser` but the package was missing, causing CrashLoopBackOff at startup.
- **Fixed**: `src/rag/community_events/main.py` — replaced `REDIS_HOST`/`REDIS_PORT`/`REDIS_DB` env-var reads with `COMMUNITY_EVENTS_REDIS_URL` (default `redis://redis:6379/1`). The kubelet auto-injects `REDIS_PORT=tcp://<svc-ip>:6379` for any K8s Service named `redis`, which broke the `int(os.getenv("REDIS_PORT"))` cast at import time. The new URL-based approach is unambiguous and immune to the injection.
- **Fixed**: `src/rag/price_compare/Dockerfile` — `providers/` subpackage is now COPYd to `/app/providers` (the WORKDIR, matching `from providers.base import ...`). Previously it was copied to `/app/rag_service/providers/` (unreachable on `sys.path`). Fix is encoded in `scripts/generate-rag-dockerfiles.py` via the new `SERVICE_EXTRA_COPIES` dict so future `--force` regeneration doesn't clobber it.
- **Fixed**: `src/rag/site_scraper/main.py` — `ContentFetcher` is now imported from `shared.content_fetcher` (not `orchestrator.search_providers.content_fetcher`, which was never in the site_scraper image). `ContentFetcher` moved to `src/shared/content_fetcher.py`; backward-compat shim at the old path re-exports all public symbols. A follow-up removes the backward-compat shim.
- **Fixed**: `src/rag/tesla/requirements.txt` — added `asyncpg>=0.29.0`. Service startup no longer crashes on import. Pool creation is now gated behind `TESLAMATE_ENABLED` (default `false`); when disabled the service starts cleanly and query endpoints return HTTP 503 with a clear remediation message. `/health` returns 200 regardless of DB state.
- **Fixed**: `src/rag/transportation/requirements.txt` — added `beautifulsoup4>=4.12.0`. Service had `from bs4 import BeautifulSoup` but the package was missing.
- **Added**: `scripts/generate-rag-dockerfiles.py` — `SERVICE_EXTRA_COPIES` dict for service-specific subpackage COPY entries; `--service <name>` flag to regenerate a single service; `--check` and `--check-advisory` flags for drift detection.
- **Added**: `scripts/service-defs.sh` — single source of truth for `RAG_SERVICES`, `CORE_SRC_SERVICES`, `ADMIN_SERVICES` arrays; sourced by both `build-and-push.sh` and `smoke-rag-images.sh`.
- **Added**: `scripts/smoke-rag-images.sh` — builds all 23 RAG images and verifies `python -c "import main"` for each. Full-sweep (not fail-fast); `--service <name>` for single-service runs.
- **Added**: `Makefile` with `smoke-rags` target (`make smoke-rags SERVICE=<name>`).
- **Added**: `.github/workflows/rag-smoke.yml` — PR-gated CI check on `src/rag/**` and `src/shared/**` changes.
- **Added**: `.github/workflows/rag-generator-drift.yml` — advisory-only CI check for generator/Dockerfile drift (always exits 0 until enforcement is turned on).
- **Added**: `CONTRIBUTING.md` — "Adding or modifying a RAG service" checklist (6 items).

> **Plan:** `thoughts/shared/plans/2026-05-08-deliver-service-auth-hardening.md`
> **Commits:** `3ed22e6` (phase 1), `eb8b305` (phase 2)

#### service-auth hardening

- **Fixed**: `verify_service_or_oidc` no longer silently falls through to OIDC when `SERVICE_API_KEY` is unset and a caller sends a non-empty `X-Service-Key` header. The helper now returns HTTP 503 with body `{"detail": "Service authentication not configured"}`. The `WWW-Authenticate` header is intentionally absent — this is a server-side misconfiguration signal, not an authentication challenge; retrying with credentials will not help. (`admin/backend/app/utils/service_auth.py`, `3ed22e6`)
- **Behavioral change for callers**: any `verify_service_or_oidc`-protected endpoint (service-registry write endpoints: POST, toggle, refresh, delete, poll-now, check) will now return 503 instead of the previous silent OIDC fallthrough when a non-empty `X-Service-Key` is sent to a deployment where `SERVICE_API_KEY` is unset. Callers that send no `X-Service-Key` header are unaffected.
- **Startup gate — behavioral tests added**: the existing production gate (`_INSECURE_DEFAULTS` loop in `admin/backend/main.py`) that already raises `SystemExit` when `SERVICE_API_KEY` is empty **or** set to the placeholder `dev-service-key-change-in-production` now has dedicated behavioral regression tests (`TestAthena21StartupGate` in `test_security_hardening.py`). The prior static-source-scan test that asserted the gate's existence by reading `main.py` source text has been demoted via comment as superseded. (`eb8b305`)

> **Plan:** `thoughts/shared/plans/2026-05-07-deliver-consolidate-service-registry.md`
> **Commits:** `058d489` → `086e4e1` (phases 1–5)

#### service-registry consolidation

- 5-phase architectural refactor consolidating 3 service-definition tables across 2 databases into a single source-of-truth `rag_services` table in the admin DB.
- New `RagService` SQLAlchemy model (`admin/backend/app/models.py`) replaces `ServiceRegistry` + `AthenaService` + `ServerConfig` as the canonical service definition. The 3 deprecated tables are renamed `*_deprecated` in migration 055 and scheduled for hard drop in migration 056 after a 7-day maintenance window.
- Background async health poller (`admin/backend/app/services/health_poller.py`) replaces inline-blocking pings on `GET /api/service-registry/services`. Health state (`health_status`, `last_health_check`, `last_error`, `last_response_time_ms`) is written back to `rag_services` by the poller; the admin UI reads the cache. Eliminates the previous up-to-44s block on the listing endpoint (22 services × 2s timeout).
- Control Agent gains `sync_registry_loop` — on startup it POSTs each entry in `PROCESS_SERVICES` to admin-backend's `POST /api/service-registry/services` using `X-Service-Key`. Host is derived from `urlparse(CONTROL_AGENT_URL).hostname` — no `localhost` fallback (which would silently poison the registry from inside the K8s pod). Missing `CONTROL_AGENT_URL` logs critical and skips the upsert; all other CA endpoints continue normally.
- 5 new env vars: `SERVICE_REGISTRY_WRITE_PER_MINUTE` (default 60), `HEALTH_POLL_INTERVAL_SECONDS` (default 30), `HEALTH_POLL_TIMEOUT_SECONDS` (default 5), `HEALTH_POLL_CONCURRENCY` (default 8), `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS` (comma-separated CIDRs/hostnames overriding the SSRF block; default empty — K8s operators with services on private subnets must set this).
- SSRF guard in `health_poller._validate_service_url` mirrors `src/control_agent/url_validator.py::_PRIVATE_NETS` — blocks RFC1918 (10/8, 172.16/12, 192.168/16), loopback, link-local, IPv6 ULA (fc00::/7), IPv6 link-local (fe80::/10), and `.cluster.local` / `kubernetes.default.svc` suffix targets. Path-injection (CRLF, `..`, NUL) also rejected. Overridden per-host via `HEALTH_POLL_ALLOWED_PRIVATE_HOSTS`.
- `last_error` values are categorically sanitized — stored as one of `connection_refused`, `timeout`, `http_5xx`, `http_4xx`, `ssrf_blocked`, `unknown`. No raw exception text is written to the DB or rendered in the admin UI.
- `GET /api/service-registry/services` now requires authentication (Bearer JWT or `X-Service-Key`). Pre-Campaign-4 the endpoint was unauthenticated.
- Dual-auth helper `verify_service_or_oidc` (`admin/backend/app/utils/service_auth.py`) accepts `X-Service-Key` OR OIDC Bearer JWT. Wired on POST, toggle, refresh, delete, and poll-now endpoints. Write endpoints are also covered by the `SERVICE_REGISTRY_WRITE_PER_MINUTE` rate-limit bucket (separate from the login rate-limit).
- **Removed**: `admin/backend/app/routes/servers.py` route module, `ServerConfig` model, and the "Servers" tab in the admin UI. The "server" concept was a pre-consolidation artifact with no remaining callers after Phase 5.
- **Behavioral change**: `health_status` column values normalized to `'healthy'`/`'unhealthy'`/`'unknown'`/`'pending'`. Previous mixed values (`'online'`/`'degraded'`/`'offline'`) from legacy code paths are no longer emitted.

> **Plan:** `thoughts/shared/plans/active-2026-05-06-deliver-auth-rate-limit-bypass.md`
> **Commits:** `f94c589` → `6e2b8db` (phases 1–4)

#### auth-hardening

- **Per-IP rate limit** on `POST /api/auth/local-login` via fastapi-limiter (Redis-backed). Default: 5 requests/minute/IP. Custom identifier uses `request.client.host` only — `X-Forwarded-For` is ignored to defeat IP-rotation bypass. Returns 429 on breach. Configurable via `LOGIN_RATE_LIMIT_PER_MINUTE`; set to 0 to disable (lockout + timing floor still apply).
- **Per-username DB lockout**: `users.failed_login_count` is incremented atomically on each wrong-password attempt. Account locks (`users.locked_until` set) once `LOGIN_LOCKOUT_THRESHOLD` cumulative failures are reached (default 10). Lockout window is `LOGIN_LOCKOUT_MINUTES` (default 30 min). Lockout is idempotent — past-threshold attempts do not extend the window. Successful login resets the counter. Manual unlock: `UPDATE users SET failed_login_count=0, locked_until=NULL WHERE username='<name>';`
- **400 ms wall-time floor** (`LOGIN_MINIMUM_DELAY_MS`, default 400) on every failure path. All four failure branches (not-found, inactive, locked, wrong-password) pay full PBKDF2-600k cost via dummy-hash AND sleep until elapsed >= floor, closing the timing side-channel.
- **Enumeration oracle closed**: all four failure branches now return an identical 401 `"Invalid username or password"`. `403 Account inactive` is no longer emitted — **behavioral change for callers that distinguished inactive-user 403 from wrong-password 401**.
- **4 new env vars**: `LOGIN_RATE_LIMIT_PER_MINUTE` (default 5), `LOGIN_LOCKOUT_THRESHOLD` (default 10), `LOGIN_LOCKOUT_MINUTES` (default 30), `LOGIN_MINIMUM_DELAY_MS` (default 400). All modelled on `AthenaConfig`; see `.env.example` and `manifests/athena-prod/config.yaml` for commented stubs.
- **Follow-up work** (out of scope for this campaign): public-route service-key gating for alert-write + tool_calling api-key endpoints; lockout-DoS mitigation (admin-unlock CLI + email notification on lockout).

> **Plan:** `thoughts/shared/plans/active-2026-05-06-deliver-security-hardening.md`
> **Commits:** `762f263` → `41f0b57` (8 commits, phases 1–4)

#### Security

- **Phase 1** (xander:6): admin-backend now exits at startup if `DEV_MODE=true` AND `DATABASE_URL` is a non-SQLite URL. `DEV_MODE` auto-creates an unauthenticated `dev-admin` user with `role=owner` on every unauthenticated request — running this against a real database is a misconfiguration that would silently provision a privileged account. The startup gate fires before `init_db()` so no partial state is produced. Error message names both env vars and the resolution. (`762f263`, `ea623d0`)
- **Phase 2** (xander:13 + codex-M2): `_INSECURE_DEFAULTS` rejection dict now covers `OIDC_CLIENT_ID`. Two placeholder values are rejected: `"demo-mode"` (previously handled by an ad-hoc `if` block, now folded into the canonical dict) and `"CONFIGURE_ME_OIDC_CLIENT_ID"` (the placeholder emitted by `scripts/create-secrets.sh:127-132`, which documented backend rejection that was never enforced). Whitespace-bypass closed: `OIDC_CLIENT_ID` is read via `get_config().oidc_client_id` (pydantic-stripped), so `" demo-mode"` no longer evades the gate. (`0117a25`)
- **Phase 2 reconcile** (xander:16 + xander:17): `DEMO_MODE=true` with `DEV_MODE=false` now raises `SystemExit` at startup — closes a separate privilege-escalation path through `auth_login`'s demo-bypass branch. Empty `OIDC_CLIENT_ID` is now rejected before `oauth.register()` is called, preventing authlib registration with a blank client ID. (`6115ecb`)
- **Phase 3** (xander:3 + MED-A + MED-E): OIDC ID-token `iss`, `aud`, and `exp` validation re-enabled. The `claims_options={"essential": False, ...}` override that disabled authlib's built-in claim validation was removed; authlib now enforces `iss`/`aud`/`exp` by default. Two new fail-closed startup gates added: (1) a runtime-issuer assertion that fires after `configure_oauth_client()` loads the DB-stored OIDC config, catching a tampered or empty issuer that would slip past the env-var gate; (2) a discovery-doc gate that fetches and validates the IdP's `.well-known/openid-configuration` at startup — if the document is unreachable or omits `issuer`, the backend exits rather than registering with a client whose `iss` validation would be silently skipped by authlib. **Operational note for deployers upgrading from a prior release:** the admin-backend now requires the IdP to be reachable at startup. An unreachable or non-conformant IdP causes `SystemExit("FATAL: OIDC discovery metadata fetch failed")`. Sequence pod startup behind an init container or readiness gate that verifies IdP connectivity. If your IdP's `iss` claim does not match `OIDC_ISSUER` exactly, align them before upgrading — tokens with a mismatched issuer will now be rejected. (`85f35f7`, `701fbe9`)
- **Phase 4** (xander:4): JWT is no longer passed as `?token=<jwt>` in the OIDC callback redirect URL or the `DEMO_MODE` redirect URL. The backend now writes the JWT to the server session and redirects to `<FRONTEND_URL>?logged_in=1`. The admin frontend detects `?logged_in=1`, clears any stale `localStorage.auth_token` (preventing cross-user contamination on shared devices), and fetches the JWT from the existing `/api/auth/session-token` endpoint. The `?token=` URL query parameter is no longer emitted; the `?logged_in=1` hint is idempotent and carries no credentials. Closes the JWT-leak-via-URL chain: 8-hour bearer tokens are no longer written to reverse-proxy access logs, browser history, or `Referer` headers on every admin login. Note: the `admin-jarvis.js` WebSocket URL (`?token=` in upgrade request) was a related but distinct exposure requiring a backend protocol change; it was deferred to a separate Plane ticket at the time of this release — closed by later auth-hardening work (see above, cross-ref Notes). (`33db179`, `41f0b57`)

#### Notes

- This is **Campaign 2 of 6** in the audit-deferred security-hardening sequence. Findings closed: xander:3, xander:4, xander:6, xander:13 (audit-named scope) plus xander:16, xander:17 (pre-existing findings pulled into Campaign 2 by user direction). The admin-jarvis.js WebSocket query-token (codex-H2) was explicitly out of scope in this campaign — closed by later auth-hardening work (see the auth-deferred-hardening entry above).
- `pytest-httpserver>=1.0.8` was added to `admin/backend/requirements.txt` (annotated `# test-only`) to support OIDC validation tests that drive authlib against a real fixture issuer. Splitting dev and production requirements is deferred to a future campaign (HIGH-E).

> **Plan:** `thoughts/shared/plans/active-2026-05-06-deliver-audit-deferred-quick-wins.md`
> **Commits:** phases 1–6

#### Added

- `admin/backend/alembic/versions/053_clear_legacy_gateway_config_ips.py` — data migration that clears legacy maintainer-IP defaults (`http://192.168.10.167:*`) from `gateway_config.orchestrator_url` and `gateway_config.ollama_fallback_url` rows; handles exact and trailing-slash variants. (audit bob:1 follow-up)
- `manifests/athena-prod/ollama-model-pull-job.yaml` — Ollama model-pull Job extracted into its own manifest file. `scripts/deploy.sh` now accepts a `--first-run` flag; the Job apply/wait is gated behind `FIRST_RUN=true` so normal re-deploys skip it. (audit otto:11/12)

#### Changed

- **control-agent**: Control Agent is now opt-in via `CONTROL_AGENT_ENABLED` (default `false`). OSS deployers no longer see Control-Agent connection errors out of the box. Existing Mac-Studio-equipped deployments must set `CONTROL_AGENT_ENABLED=true` (and `CONTROL_AGENT_URL=<host>:8099`) in their env or kubeconfig overlay. Disabled-path responses are per-endpoint: 503 for download mutations, structured logs for orchestrator keepalive, neutral typed responses for service-control queries. (audit bob:4)
- `docs/INSTALLATION.md`: `imagePullPolicy: Always` documented as dev-default; first-run vs. normal deploy flow clarified. (audit otto:11/12)

#### Fixed

- **a11y**: added `for=`/`id=` associations to ~426 admin-frontend `<label>` elements across 28 files (WCAG 1.3.1, 4.1.2). Display-only label misuse converted to `<p>`/`<span>`. Wrapping labels (~41 residual) left as-is per containment rule. (audit ruby:1)

#### Removed

- Deleted dead stub directories under `apps/`: `gateway/`, `orchestrator/`, `rag/`, `share-service/`, `shared/`, `validators/`. These were README-only placeholders with zero importers (verified by librarian agent at HEAD `03736ee`). The live `apps/jarvis-web/` (Jarvis voice/chat web UI) and `apps/chat-embed/` (CORS-relay proxy) are unchanged. (audit bob:6 / librarian:8)

#### Notes

- This is **Campaign 1 of 6** in the audit-deferred remediation sequence. Subsequent campaigns cover: security-hardening (OIDC `iss` validation, JWT URL removal, SQLite `DEV_MODE` — xander); rate-limiting (`fastapi-limiter`); network policies + RBAC (otto:10); `BaseRAGService` migration (librarian:2/4); and remaining UI scope (ruby:2–10). Changes here are self-contained; none of those campaigns depend on this one shipping first.
- Per-endpoint disabled-path route tests for Phase 6 (`debug_logs` status, `model_downloads` helper + create/retry/delete gates, `service_control` containers/ollama-health shape) are deferred — `admin/backend` lacks route-level test scaffolding at HEAD.

> **Plan:** `thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md`
> **Commits:** `615d7d0` → `14fcb73` (19 commits)

Pure refactor — no behavior change. Decomposed `src/orchestrator/main.py` from 12,409 lines into an 8,758-line core plus 12 sibling modules. Zero new failures introduced; 209 new unit tests added (31 failed / 207 passed → 31 failed / 416 passed).

#### Added

- `src/orchestrator/nodes/_runtime.py` — runtime singleton accessor (`_runtime.get_X()` / `_runtime.set_X()` / `_runtime.is_ready()` / `_runtime.missing_required()` / `_runtime.required_singletons()`). Singletons are set by lifespan, read at call time. Tests install fakes via setters directly.
- `src/orchestrator/urls.py` — 15 module-level service URL constants previously scattered in `main.py`'s constant block.
- `src/orchestrator/metrics.py` — 7 Prometheus metric declarations (`request_counter`, `request_duration`, `node_duration`, `tool_call_breakdown`, `validation_counter`, `hallucination_counter`, `validation_layer_duration`) moved verbatim from `main.py`.
- `src/orchestrator/helpers.py` — 17 stateless helper functions extracted from `main.py`. Helpers that need runtime singletons call `_runtime.get_X()` at call time (Pattern 1).
- `src/orchestrator/mode_permission.py` — 6 mode/permission helpers (`get_current_mode`, `detect_owner_mode_command`, `extract_pin_from_query`, `activate_owner_override`, `check_intent_permission`, `check_entity_permission`) plus `OWNER_MODE_PATTERNS` constant.
- `src/orchestrator/nodes/route_info.py` — `route_info_node` (33 LOC, zero runtime dependencies).
- `src/orchestrator/nodes/send_sms.py` — `send_sms_node`.
- `src/orchestrator/nodes/notification_pref.py` — `notification_pref_node`.
- `src/orchestrator/nodes/synthesize.py` — `synthesize_node`.
- `src/orchestrator/nodes/validate.py` — `validate_node`.
- `src/orchestrator/nodes/finalize.py` — `finalize_node`.
- `src/orchestrator/nodes/route_control.py` — `route_control_node`.
- `src/orchestrator/nodes/route_music.py` — `route_music_node`.
- `src/orchestrator/nodes/route_tv.py` — `route_tv_node`.
- `src/orchestrator/nodes/retrieve.py` — `retrieve_node` (largest single extraction; 538 LOC body).
- 209 new unit tests across `tests/unit/test_helpers.py`, `test_mode_permission.py`, `test_route_info.py`, `test_send_sms_node.py`, `test_notification_pref.py`, `test_synthesize.py`, `test_validate.py`, `test_finalize.py`, `test_route_control.py`, `test_route_music.py`, `test_route_tv.py`, `test_retrieve.py`, `test_health_probes.py`.

#### Changed

- `src/orchestrator/main.py` reduced from 12,409 → 8,758 lines (−3,651, ~29%). The 10 extracted node functions and 17 helpers are imported back into `main.py`'s graph builder; runtime behavior is byte-identical.
- `src/orchestrator/main.py` runtime singletons: 97 bare module-level reads migrated to `_runtime.get_X()` call-time accessors. 16 bare `Optional[X] = None` module-level declarations removed. `global` keyword removed from lifespan. Lifespan dual-write removed (Phase 1.2 scaffolding); `_runtime.set_X()` is now the sole write path.
- 6 bare-except blocks in `health_check` and `readiness_probe` replaced with `except Exception as e` + structured log with `exc_info=e`.

#### Notes

- `src/orchestrator/state.py` is now the canonical source for `OrchestratorState`, `IntentCategory`, `ModelTier`, and `ConversationContext`. Duplicate definitions that had accumulated in `main.py` were removed in commit `8034360` (Phase 1.1).
- `classify_node` (2,473 lines), `tool_call_node`, route handlers, and streaming functions remain in `main.py`. Extraction is deferred: `classify_node` to Campaign 2; `tool_call_node` to Campaign 1.3; route handlers to Campaign 1.5.
- 14 proxy-class instances across `nodes/` share a common `__getattr__`-defers-to-`_runtime.get_X()` shape. Promotion to a shared `orchestrator.nodes._proxy.runtime_proxy()` factory is deferred to Campaign 2.
- xander (security review) on Phase 3.1 surfaced 2 HIGH + 3 MEDIUM + 3 LOW pre-existing findings on the permission/PIN surface. All pre-existing; none introduced by this refactor. Tracked for a follow-up security-hardening campaign.

> **Plan:** `thoughts/shared/plans/2026-05-06-deliver-config-py-rebuild.md`
> **Commits:** `aaa989d`

#### Added

- `AthenaConfig` (`src/shared/config.py`) — canonical pydantic-settings `BaseSettings` object centralizing 11 env vars: `OLLAMA_URL`, `LLM_SERVICE_URL`, `REDIS_URL`, `DATABASE_URL`, `SERVICE_API_KEY`, `DEFAULT_TIMEZONE`, `DEFAULT_CITY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `DEMO_MODE`, `DEV_MODE`. Read via `get_config()`. `admin_url` is a computed field that delegates to Campaign 3's `get_admin_url()` and is not env-loadable via `ADMIN_URL`.
- `pydantic-settings>=2.1.0,<3.0` dependency (required by `AthenaConfig`).
- `CONTRIBUTING.md` Configuration Guidelines section updated to recommend the `AthenaConfig` extension pattern for new env vars.

#### Removed

- **`admin/frontend/router.js`**: deleted (~200 lines of dead navigation code that paralleled the live `showTab()` system; no callers besides one in `command-palette.js`, now updated). (audit follow-up: dexter:7)

#### Changed

- **LLM endpoint precedence** (`admin-backend`): when both `OLLAMA_URL` and
  `LLM_SERVICE_URL` are set, `LLM_SERVICE_URL` now wins at the database
  seeder (matches the dominant precedence used by orchestrator + gateway).
  Previously `OLLAMA_URL` won at `admin/backend/app/database.py:268` (the seed
  path) while the rest of the codebase used `LLM_SERVICE_URL`-first. Operators
  using both env vars should verify the seeded `system_settings.ollama_url` row
  after deploy. Run:
  `kubectl -n athena-prod exec -it deploy/athena-admin-backend -- psql $DATABASE_URL -c "SELECT key, value FROM system_settings WHERE key='ollama_url';"`
  See Campaign 4 plan, Phase 2a-ii.

- **`REDIS_URL` default** changed from `redis://localhost:6379` (mixed across call sites) to `redis://redis:6379/0` (in-cluster DNS shortname, consistent with `manifests/athena-prod/config.yaml`). Production deployments are unaffected — the manifest sets `REDIS_URL` explicitly. Local-dev users should add `REDIS_URL=redis://localhost:6379` to their `.env` file — see `.env.example`.

- **Qdrant**: PersistentVolumeClaim is now the default storage backend (was `emptyDir`, which silently lost all conversation memory on every pod restart). Deployers must replace `YOUR_STORAGE_CLASS` in `manifests/athena-prod/qdrant.yaml` with their cluster's StorageClass before applying. Existing `emptyDir`-based deployments will lose their current Qdrant data on the next apply — see `docs/INSTALLATION.md` for migration notes. (audit follow-up: otto:3)

---


---

## [0.3.0] - 2026-05-06 — Admin URL Consolidation

> **Plan:** `thoughts/shared/plans/2026-05-06-deliver-admin-url-consolidation.md`
> **Commits:** `105f782` → `979812f` (8 commits)

Replaces 32 independent admin-URL resolution sites across 20 files with a single canonical helper. One resolution order, one fallback chain, one startup log line per service.

### Added

- `src/shared/admin_url.py` — canonical `get_admin_url()` helper. Resolution order: `ADMIN_API_URL` → `ADMIN_BACKEND_URL` → `ADMIN_INTERNAL_URL` (deprecated alias) → `LOCAL_DEV=true` → K8s in-cluster auto-discovery (`KUBERNETES_SERVICE_HOST`) → empty string + warning log. Caches the resolved URL at module import time; cache is invalidable via `_clear_cache_for_tests()` in test code.

### Changed

- **32 admin-URL resolution sites consolidated** — all callers in `src/shared/`, `src/orchestrator/`, `src/gateway/`, `src/mode_service/`, `src/rag/` (4 services), `apps/jarvis-web/backend/`, and `src/sms/` now delegate to `get_admin_url()` instead of each performing their own `os.getenv` chain.
- **jarvis-web Dockerfile build context changed to repo root** — required so `src/shared/admin_url.py` is reachable during the image build. `apps/jarvis-web/build-and-deploy.sh` updated accordingly.
- **`docs/CONFIGURATION.md`** — `ADMIN_API_URL` promoted to Required Settings table; full resolution order documented with reference to `src/shared/admin_url.py`.
- **`.env.example`** — resolution order documented inline; `ADMIN_BACKEND_URL` and `ADMIN_INTERNAL_URL` moved to commented-out alias block with deprecation note; `LOCAL_DEV` escape-hatch entry added.
- **`docs/INSTALLATION.md`** — admin URL configuration section updated to reference the new helper and `ADMIN_API_URL` as the canonical variable.
- **`README.md`** — env-var table updated; `ADMIN_API_URL` entry now references the resolver with `LOCAL_DEV=true` note.

### Fixed

- **`src/mode_service/main.py` port typo** — fallback was `http://localhost:5000` (the mode service's own port); corrected to delegate to `get_admin_url()` which resolves to the admin backend.
- **3 hardcoded literals in `src/orchestrator/smart_home_controller.py`** (lines 2885, 2925, 3176) — plain `admin_url = "http://localhost:8080"` string literals inside `_create_stuck_sensor_alert`, `_resolve_stuck_sensor_alert`, and `_get_house_layout` that pointed to the pod's own localhost in K8s. Now call `get_admin_url()`.
- **1 hardcoded literal in `src/sms/service.py`** (line 252, `SMSService.from_admin_config()`) — same localhost-literal pattern, also broken in K8s. Now calls `get_admin_url()`.
- **`src/orchestrator/memory_manager.py` IN_CLUSTER namespace defect** — previous fallback used the fully-qualified namespace `athena-admin.svc.cluster.local` which is only valid for cross-namespace calls; the helper now uses `athena-admin-backend:8080` (same-namespace short form, consistent with the rest of the fleet).
- **`src/shared/cache.py`** — was the only site that checked `ADMIN_BACKEND_URL` before `ADMIN_API_URL`, silently ignoring `ADMIN_API_URL` if `ADMIN_BACKEND_URL` was set. Now follows the canonical order via the helper.

### Deprecated

- **`ADMIN_INTERNAL_URL`** — accepted as a backward-compatible alias at resolution priority 3, but documented as deprecated in `.env.example` and `docs/CONFIGURATION.md`. Will be removed in a future release. Deployments using this variable should migrate to `ADMIN_API_URL`.

### Removed

- **`src/shared/config_loader.py`** — dead file; no in-tree callers. Deleted in `979812f`.

---

## [0.2.0] - 2026-05-06 — Comprehensive OSS Audit Remediation

> **Audit document:** `thoughts/shared/audits/2026-05-05-audit-athena-oss-comprehensive.md`
> **Plan:** `thoughts/shared/plans/2026-05-05-audit-athena-oss-comprehensive.md`
> **Commits:** `9f4c40e` → `5830a71` (13 commits)

This release bundles all changes from the comprehensive OSS audit conducted 2026-05-05.
All changes are additive or hardening — no features were removed.

### Added

- `CHANGELOG.md` — this file, tracking changes from the OSS baseline forward
- `apps/chat-embed/` — CORS-relay proxy for embedding Athena-backed chat on external websites; documented in README and build scripts
- GitHub issue and pull request templates (`.github/`)
- `pytest.ini` — `integration` marker registered; default run (`pytest`) skips live-service tests; `pytest -m integration` selects them
- `scripts/check-env-example.py` — audits `.env.example` for drift against env vars referenced in source code

### Changed

- **Admin backend startup validation hardened** — in production (non-dev) mode the process now hard-fails at startup if `OIDC_ISSUER` is empty, missing, or matches the `CONFIGURE_ME` placeholder; if `OIDC_CLIENT_ID` is the literal string `demo-mode`; or if `SERVICE_API_KEY` is unset. Previously these conditions were silently ignored.
- **Service-to-service auth enforced end-to-end** — `SERVICE_API_KEY` is now required and wired through all service boundaries (admin backend, orchestrator, gateway, RAG services). Previously some paths accepted unauthenticated internal calls.
- **Alembic migrations parameterized** — 6 migrations that previously embedded deployment-specific values via Python f-strings now read those values from environment variables at migration time. `alembic upgrade head` is safe to run against any deployment without code edits.
- **Control Agent input hardening** — path-traversal and SSRF guards added; callback URLs are rejected unless the hostname matches `ALLOWED_CALLBACK_HOSTS` (fail-closed by default when the variable is empty).
- **`scripts/create-secrets.sh` is now idempotent** — re-running the script on a cluster where secrets already exist skips rotation rather than overwriting keys.
- **`scripts/deploy.sh` pre-flight check** — the deploy script now verifies the target namespace and required secrets (`athena-db-credentials`, `athena-encryption`, `athena-oidc`) exist before running `kubectl apply`. Missing secrets abort with an actionable error.
- **Orchestrator Kubernetes manifest** — memory limit raised from 512Mi to 2Gi; CPU limit raised from 250m to 2000m; `startupProbe` added with 420-second grace period for slow LLM initialization.
- **nginx security headers** — admin frontend nginx config now emits `Content-Security-Policy`, `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`, and `Referrer-Policy`. CSP allows existing CDN dependencies (Bootstrap, cdnjs).
- **README** — Chat Embed interface documented; build scripts updated.
- **`.env.example` curated** — stale keys removed; drift between documented and actual environment variables corrected.

### Fixed

- `docs/INSTALLATION.md` — broken cross-references repaired
- Hardcoded references to the maintainer's domain removed from admin OIDC configuration panel — all OIDC fields now derive from environment variables
- Hardcoded location defaults (`Baltimore`, MD timezone) removed from RAG services — `DEFAULT_CITY`, `DEFAULT_STATE`, and `DEFAULT_TIMEZONE` are now required from the environment or left blank
- Hardcoded HA JWT removed from `src/jetson/` — **token revocation in Home Assistant is a required manual step** (the token appears in git history at commit `794096b`; see audit doc for details)
- Alembic JSONB cast error in Phase 4 migrations corrected (codex r2, `5830a71`)
- RAG `SERVICE_API_KEY` wiring fixed — keys were read but not forwarded in some service paths
- `CONTROL_AGENT_URL` handling corrected — fallback behavior on missing var now logs a warning instead of raising

### Security

- Deployment-specific secrets and domains removed from source code across 18+ files
- Admin backend endpoints that previously accepted requests without authentication now require a valid session or service key
- nginx CSP, HSTS, `X-Frame-Options`, `X-Content-Type-Options`, and `Referrer-Policy` headers added to admin frontend
- Control Agent hardened against path traversal and SSRF via callback URL allowlist (`ALLOWED_CALLBACK_HOSTS`)
- **Action required on upgrade:** revoke the Home Assistant long-lived access token that was hardcoded in `src/jetson/` — it is present in git history at commit `794096b` even though the code reference was removed in `be251ef`

### New environment variables

The following variables were added to `.env.example` and are required or recommended for production deployments:

| Variable | Required | Description |
|---|---|---|
| `SERVICE_API_KEY` | Yes (production) | Shared secret for service-to-service auth |
| `OIDC_ISSUER` | Yes (production) | OIDC provider issuer URL; startup fails if unset |
| `OIDC_REDIRECT_URI` | Yes (with OIDC) | Callback URL registered with your OIDC provider |
| `OIDC_CLIENT_ID` | Yes (with OIDC) | Must not be the literal string `demo-mode` in production |
| `ALLOWED_CALLBACK_HOSTS` | Yes (with Control Agent) | Allowlist of hostnames for HuggingFace download-progress callbacks |
| `DEFAULT_CITY` | No | Default city for location-aware RAG queries (blank = no default) |
| `DEFAULT_STATE` | No | Default state/region for location-aware RAG queries |
| `DEFAULT_TIMEZONE` | No | Timezone for time-aware queries (e.g., `America/New_York`); defaults to `UTC` |
| `OIDC_USERINFO_URL` | No | Manual override for OIDC userinfo endpoint; auto-derived from discovery if unset |

---

## [0.1.0] - 2026-05-05

> **First public OSS baseline, anchored to commit [`7f5387b`](https://github.com/jstuart0/project-athena-oss/commit/7f5387b).**
> Pre-existing commits represent initial development history leading to this point.
> Entries below describe the state of the project at this baseline, not changes since a prior release.

### Added

- **Jarvis Web** (`apps/jarvis-web/`) — full-featured browser chat interface with streaming text, push-to-talk voice, LiveKit WebRTC streaming, smart home widgets, owner/guest mode, and music playback
- **Chat Embed** (`apps/chat-embed/`) — lightweight CORS-relay proxy so external websites can embed an Athena chatbot; fetches assistant profile from admin backend at startup; includes per-IP rate limiting and analytics source tagging
- **MLX streaming** — real token-level streaming for MLX-format models; `answer_chunk` SSE events relay tokens to the browser as they are generated
- **Analytics Mode** — optional conversation capture and review pipeline; tracks source, mode, latency, and session data; gated behind `MODULE_ANALYTICS=true`
- **Persistent chat sessions** — anonymous browser-cookie session IDs retain conversation context across page reloads
- **Safety guardrails** — jailbreak pre-screen layer in the orchestrator preprocessing stack
- **Semantic cache** — intent-aware response caching to avoid redundant LLM calls for equivalent queries
- **Privacy filter** — PII scrubbing (`src/shared/privacy_filter.py`) for queries routed to cloud LLM backends
- **Complexity-aware model routing** — regex-only complexity detector selects fast 4B vs. capable 14B/32B model tier without a routing LLM call
- **Multi-intent decomposition** — orchestrator decomposes compound queries ("turn on the lights and check the weather") into parallel sub-queries
- **23 RAG microservices** — weather, sports, dining, flights, airports, Amtrak, directions, transportation, streaming, events, SeatGeek, SerpAPI, community events, news, stocks, price comparison, Tesla Fleet API, web search, site scraper, BrightData, media, recipes, one-call weather
- **OpenAI-compatible gateway** — `/v1/chat/completions` endpoint for drop-in compatibility with Home Assistant and any OpenAI client library
- **4-layer anti-hallucination pipeline** — dedicated validation model fact-checks LLM responses against retrieved source data before delivery
- **Admin UI** — web interface for runtime model assignment, feature flags, encrypted API key management, service registry, device management, guest mode, analytics, audit log, and memory management; 62 route modules, 50+ DB migrations
- **Module system** — `MODULE_*` environment variable toggles for Home Assistant integration, guest mode, analytics, and Jarvis Web
- **Read-only viewer role** — restricted admin access tier for non-operator users
- **OSS tuning controls** — diagnostics panel and control-plane UI for observable pipeline tuning

### Changed

- README restructured to present chat interface as a first-class deployment path alongside voice hardware
- Dashboard health checks use environment variables rather than hardcoded addresses
- Orchestrator synthesis token limit raised for chat interface to prevent response cutoff on long list answers
- Chat interface routed to complex model tier for richer responses
- `qwen3` and `llama.cpp` thinking/reasoning tokens suppressed via `/no_think` and equivalent flags to reduce latency

### Fixed

- Conversation context not persisting across multi-turn streamed sessions
- Empty responses from streaming endpoint under certain model backends
- `NameError` / `UnboundLocalError` in analytics and search-log paths when modules were partially enabled
- Debug-logs endpoint returning 503 instead of degrading gracefully
- Jarvis Web streaming not wired to `/api/chat/stream` (frontend connected to non-streaming path)
- `interface_type` not forwarded to streaming endpoint initial state
- LLM hallucinated role-continuation in streaming `finalize_node`
- Base knowledge URL validation and progressive streaming correctness
- ASCII art formatting in README

### Security

- Admin backend hardened: service-to-service auth, CORS policy, cookie flags, and startup validation added
- API permission enforcement scoped by viewer vs. operator role

---

[Unreleased]: https://github.com/jstuart0/project-athena-oss/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/jstuart0/project-athena-oss/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jstuart0/project-athena-oss/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jstuart0/project-athena-oss/compare/979812f...v0.4.0
[0.3.0]: https://github.com/jstuart0/project-athena-oss/compare/5830a71...979812f
[0.2.0]: https://github.com/jstuart0/project-athena-oss/compare/7f5387b...5830a71
[0.1.0]: https://github.com/jstuart0/project-athena-oss/commit/7f5387b
