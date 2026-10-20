# Recorded answers of the live room-policy scripts

Each `*.json` file is one call to `script.room_policy_set` or `script.room_policy_clear` and the
answer Home Assistant gave: `request` (script and fields), `accepted`, and `response`
(`service_response` for an accepted call, `{ok: false, reason}` for a refusal).
`tests/unit/test_room_policy.py` feeds each `response` to `room_policy.apply` and expects accepted
files to be confirmed and refused files to be `script_refused`. They exist because the
hand-written test responses once disagreed with the scripts (`brightness_hold` is `null` or a
percentage, not a boolean).

Source: the Home Assistant config repo (node-red-flows), `test/fixtures/room-policy/athena/`,
recorded at commit `8301343` from the contract run of `scripts/test-room-policy.sh`. These are the
sanitized copies (room `den`, no entity ids or hosts); the raw ones in the parent directory name
the real test room and are not vendored.

Refresh, after a change to `scripts/lib/p3b-scripts.js` there:

1. In the HA config repo, run `scripts/test-room-policy.sh` with `ROOM_LIVE_TEST=1` and
   `ROOM_POLICY_FIXTURE_DIR=<some empty dir>` (the contract test `test/room-policy-contract.test.js`
   does this against its simulator and lists the fixture names it expects).
2. Copy `<some empty dir>/athena/*.json` over this directory.
3. Run `pytest tests/unit/test_room_policy.py -k live`.
