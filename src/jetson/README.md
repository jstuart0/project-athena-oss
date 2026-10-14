# Jetson prototype

> **Experimental prototype — unsupported; not built, deployed or tested by Athena.**
> Don't run it. The code is kept for reference only.

## What it is

An early attempt at a voice pipeline for NVIDIA Jetson devices: a small Flask webhook that takes a voice command, optionally asks an LLM, and calls Home Assistant directly.

- `llm_webhook_service.py` - Flask webhook service
- `athena_lite.py` - the original voice pipeline
- `athena_lite_llm.py` - a variant with LLM integration
- `config/` - Home Assistant integration config
- `requirements.txt` - dependency list (incomplete and stale)

## Known breakages

- `athena_lite_llm.py` has a syntax error and doesn't import.
- The webhook service can't start as written.
- The `/conversation` route is registered after `app.run`, so it's never served.
- `ORCHESTRATOR_URL`, which an earlier version of this README told you to set, is read by nothing.
- `requirements.txt` is incomplete and out of date.

These are deliberately not fixed. Making the prototype runnable would make its security problems below live.

## Security caveats

- It talks to Home Assistant directly with a bearer token. That bypasses Athena's permission guard and the failed-sign-in latch that protects Home Assistant from repeated bad sign-ins.
- The webhook is an unauthenticated Flask service bound to all interfaces.
- It disables TLS verification (`verify=False`).
- It logs the commands it receives.
- An old Home Assistant token is in this repository's git history. If you ever used it, revoke it.

## Supported voice paths

Use one of these instead:

- The Wyoming bridge
- LiveKit with Jarvis Web
- Home Assistant Assist through the Athena gateway

See `docs/CONFIGURATION.md` for setup.
