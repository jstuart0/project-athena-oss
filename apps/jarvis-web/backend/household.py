"""jarvis-web's reader for the household settings admin-backend owns.

This image has no ``shared`` package, so the pure registry arrives as the
copied ``household_settings`` module and the fetch is injected by ``main.py``
(which owns the service key and the admin URL): ``configure(fetch=...)``.

``settings()`` never waits and never raises: it reads the cached snapshot
(``peek()``), which refreshes in the background, and resolves admin value,
then environment, then today's default. Only the startup hook awaits
``warm()``.
"""
import os
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

import household_settings as hs

Fetch = Callable[[], Awaitable[Optional[Dict[str, Any]]]]

SECTIONS = ("jarvis_web",)

_cache: Optional[hs.HouseholdSettingsCache] = None


def _unconfigured() -> hs.HouseholdSettingsCache:
    """Before ``configure`` the reader serves an empty snapshot and does no I/O,
    so a process that never configured it behaves as an env-only install."""
    cache = hs.HouseholdSettingsCache(_no_fetch)
    cache.preload(hs.Snapshot())
    return cache


async def _no_fetch() -> None:
    return None


def _reader() -> hs.HouseholdSettingsCache:
    global _cache
    if _cache is None:
        _cache = _unconfigured()
    return _cache


def configure(*, fetch: Fetch) -> hs.HouseholdSettingsCache:
    """Build the reader around ``fetch`` (the GET body as a dict, or ``None``)."""
    global _cache
    _cache = hs.HouseholdSettingsCache(fetch)
    return _cache


def settings() -> hs.JarvisWeb:
    """The resolved entities, room and thermostat range. Synchronous."""
    section = _reader().peek().sections.get("jarvis_web")
    return hs.resolve_jarvis_web(section, os.environ)


async def warm() -> None:
    """Load once at startup so the first request sees admin's values."""
    await _reader().current()


async def aclose() -> None:
    await _reader().aclose()


def install_for_test(*, sources: Optional[Mapping[str, Optional[dict]]] = None) -> hs.HouseholdSettingsCache:
    """Serve ``sources`` ({"jarvis_web": {...}}, validated through the model)
    as a fresh snapshot, with no fetch."""
    global _cache
    body = {
        name: {"value": hs.validate_update(name, value).model_dump(mode="json"), "revision": 1}
        for name, value in (sources or {}).items() if value is not None
    }
    cache = hs.HouseholdSettingsCache(_no_fetch)
    cache.preload(hs.snapshot_from_body(body))
    _cache = cache
    return cache


def reset_for_test() -> None:
    global _cache
    _cache = None
