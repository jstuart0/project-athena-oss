# Room policy

Some Home Assistant installs run a room's lighting with a policy engine: a set
of helpers per room plus a few scripts that are the only supported way to hold
a room's lights on or off, switch its motion lighting off, or lock its
brightness. Athena can drive such a room by voice, and when it does it calls
those scripts instead of writing the helpers itself. A room that isn't run this
way keeps the plain helper writes (`input_boolean.<room>_leave_lights_on` and
so on) it always had.

This page is the contract between Athena and a Home Assistant package that
provides the scripts. Nothing in Athena names a house: Home Assistant says
which rooms are policy rooms and which spoken area is which room.

## How Athena knows a room is run by the policy engine

A slug `<room>` is a **policy room** when all of these exist in Home
Assistant's states:

- `input_boolean.<room>_motion_disable`;
- `input_text.<room>_policy`, with a non-empty string attribute `ha_area`
  holding the Home Assistant **area id** of the room;
- `script.room_policy_set`.

The map from area to room is the `ha_area` attribute. An area claimed by two
rooms is dropped from the map (and counted in the log), so neither room is
reachable by voice. Publishing the package that sets the attributes, or
removing it, switches Athena's routing and the helper protection together
within one entity-cache refresh (five minutes); no Athena restart is needed.

A spoken room resolves to its area through the area registry (the area's own
name, or the one area that has the spoken name as an alias), then to the room
through the map.

## What Athena calls

Only these, only as `POST /api/services/script/<name>?return_response`:

| Script | Fields |
|---|---|
| `room_policy_set` | `area` (the room slug, never the area id), `kind` (`hold_on`, `hold_off`, `disable`, `capture`), `brightness` (1..100, required for `capture`), `duration_minutes` (absent means no end time; otherwise 5..480), `set_by` (`voice`), `caller_role` (`owner`), `apply_now` (`false`) |
| `room_policy_clear` | `area`, `caller_role`, `clear_capture` (bool) |
| `room_policy_good_night` | `area`, `caller_role` |

`room_policy_set` answers with
`{area, policy, brightness_hold, until, until_spoken, set_by, stale}`, every key
required (`area` must be the room asked about, `brightness_hold` and `stale`
booleans, the rest strings):
`until` is an ISO-8601 timestamp with an offset, or `''` (never null, never a
bare date or a time without an offset) for no end;
`until_spoken` is a clock time (`7:30 AM`) or empty; `stale` is true when the
state read back hasn't caught up. `room_policy_clear` answers the same way.
`room_policy_good_night` answers `{area, until, until_spoken}`.

A refusal is either a non-2xx answer with a `message`, or HTTP 200 with
exactly `{ok: false, reason: "<message>"}`; nothing was written. Athena never
speaks or stores the script's words. The reason is looked up, exactly, in a
closed table (`room_policy.REFUSAL_REASONS`) and Athena says its own sentence
for it; any other reason, and a refusal carrying extra keys, gets "I couldn't
change that. That wasn't allowed." Accepted answers never contain `ok`; an
empty `{}`, or any other answer shape carrying `ok`, is not confirmed.
`until_spoken` is spoken only when it is a clock time as the scripts write it
(`7:30 AM`), else "for a while".

The reason must equal a listed string byte for byte: no Unicode folding, no
trimming, no case folding. A refusal carrying any other key, F4's included, gets
the generic sentence and is never accepted. What is spoken is Athena's closed-set
sentence or a validated time, so a turn is stored in history as spoken.

The table is pinned in `tests/unit/room_policy_refusal_reasons.txt`: one reason
per line, UTF-8, LF line endings, a final newline, in the generator's order, no
blank lines. The authoritative gate is on the HA side: the HA config repo pins
the same list in its own test (`test/room-policy-refusal-reasons.txt`, same
format, byte-identical to this file), so a generator change there fails until
both pinned lists are updated. `scripts/check-room-policy-refusals.py
<ha-config-repo>` compares this file to the generator by hand (`--write`
updates it); the unit test here then fails until the table matches.

Athena never calls any other `room_policy_*` script.

### No end time

A hold with no stated duration sends no `duration_minutes`. The script decides
what that means; the reply is "until you leave or press the switch" unless the
response carries an `until`. An explicit duration keeps the 5..480 clamp.

Rooms that aren't policy rooms keep the one-hour default for a hold with no
stated duration.

## What the guard checks of a call

`run_policy_script` does not take the caller's word for what it is doing. It
checks every field against the table above (no other key; `until` and
`caller_area` are never sent; `kind` is one of the four; a duration only for a
hold, 5..480; a level only for a capture, 1..100; `set_by` is `voice`;
`apply_now` is false), that `area` is one of its own policy rooms, and derives
the writes the call stands for from the script and `kind`. Athena also checks
the answer against the contract above (all keys present with the right types,
`area` equal to the room asked about, `until` empty or a timestamp with an
offset); otherwise nothing is confirmed aloud.

## Who may do it

The caller is allowed exactly when the direct helper writes the voice command
used to make would have been allowed in the same session. Athena adds no
owner-mode, trusted-voice or degraded-mode requirement of its own on top.
Guests are refused. The check is `PermissionEnforcingHAClient.run_policy_script`,
which authorizes the caller against the entities the replaced writes would have
touched (for a good-night, `script.good_night`).

## The deny boundary

With the policy engine's scripts present, Athena's HA write guard refuses every
other path to them:

- a call to a `script.room_policy_*` script, by service name or as a target of
  `script.turn_on|turn_off|toggle` or any other service;
- a write to a policy room's helpers (`leave_lights_on`, `leave_lights_off`,
  `leave_lights_on_minutes`, `leave_lights_off_minutes`, `brightness_override`,
  `override_brightness`, `motion_disable`, `policy`, `hold_until`,
  `hold_set_by`, `hold_brightness`, in any `input_*` domain);
- anything matching a glob in `HA_POLICY_PROTECTED_EXTRA`;
- a write whose targets can't be checked: `script.turn_on|turn_off|toggle`, an
  `input_*` service or `homeassistant.turn_on|turn_off|toggle|update_entity`
  aimed at an area, device, label, `all` or nothing; an entity-registry id; a
  templated id; a service of any domain an extra names (an `automation.*` glob
  covers `automation.turn_off` at `all`) aimed at all, an area or nothing; a
  group or scene whose members can't be read. Ids listed beside an area still
  count, and so do the `entities` and `snapshot_entities` of `scene.apply` and
  `scene.create` and the members `group.set` adds. A group or scene a call
  targets is looked into through the cached `attributes.entity_id`, nested up to
  three levels; an integration's own scene with no member list is not.

The same checks run on the steps of automations the agent creates and on
scheduled sequence steps. While a policy room or an extra exists, a step that
reaches `script`, `homeassistant`, `scene`, `automation` or an `input_*` entity
must be a plain service call with literal data: no template anywhere, no
`data_template`, no string-valued target or data, no device action, no scene
step; a step of a shape whose target can't be read (an event) is refused too. A denial is recorded like any other (`ha_write_denied`,
reasons `policy_protected` and `policy_protected_unverifiable`). Protection is wider than
routing: a room's helpers stay protected when its `ha_area` is blank or
`script.room_policy_set` is briefly missing (a script reload, a restart), once a
`room_policy_*` script has been seen by the process. Until one has, helpers are
written directly as before. A cache with no `input_boolean` at all, or never
filled, counts as cold: every helper of the policy shape is treated as protected.

`HA_POLICY_PROTECTED_EXTRA` is for the entities of the engine itself that
Athena should never write, such as its scripts, enable switches and schedule
helpers.

### When the boundary is active

All of the above is inert until a policy room or an extra exists (or the
slugs can't be known), so an install without the engine behaves as it always
did. It turns on when `protected_slugs` finds a room, or `HA_POLICY_PROTECTED_EXTRA`
is non-empty. A cache that is loaded but holds no `input_boolean` and has never
shown a `room_policy_*` script counts as inactive; a cache that was never
loaded, or is empty, counts as cold and fails closed, as does a cache that
loses its `input_boolean` entities after the engine was seen.

### Accepted limits

- Pressing the engine's kill-switch button stays allowed (it reverts a room to
  the older behaviour, the safe direction); the automation behind it is the
  thing to protect, with an extra.
- An existing script or automation that someone wrote in Home Assistant and
  that writes a protected helper can still be run: running owner-authored
  content is not policed (A-D12).
- At startup, before the first entity fetch, the cache is cold: group, scene and
  `input_*` calls are refused until that fetch completes.
- While an extra names `automation.*`, an automation with a non-ASCII alias is
  refused, since its entity id can't be worked out here.
- Protection survives a script reload or restart once the process has seen the
  engine, but a process that starts while the scripts are missing protects
  nothing until they first appear (M1 residual).

## Checking a deployment

```sh
python -m orchestrator.room_policy --selftest [--expect-extras N]
```

runs the guard's matrix against a recording fake (nothing is sent to Home
Assistant) and prints one line:

```
room_policy selftest boundary=1 router=1 extras=<n> cases=<n> failures=0
```

The exit status is 0 when no case failed and, if `--expect-extras` is given,
exactly that many globs are configured. `router=1` means the voice router is in
the build. A deploy that publishes the engine's scripts should require this
line first.

```sh
python -m orchestrator.room_probe --runs 1 --policy [--areas]
```

prints `motion rooms`, `policy rooms`, `room_policy scripts` and
`protected with area/device/label` counts (the last should be 0: an entity in
an area can be reached by an area-wide call from the assistant), and with
`--areas` one `policy <area key> -> <room>` line per mapped room.
