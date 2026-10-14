"""invalidate_cache on a real Redis server.

The unit tier runs it against an in-memory fake with ``scan_iter`` and ``unlink``.
This runs the SCAN and UNLINK themselves, so a pattern the server reads
differently or a reply type the client mishandles shows up. Like the sign-in
budget tier it needs ``HA_AUTH_TEST_REDIS_URL`` (a database it may flush) and
fails rather than skips when that is unset or does not answer.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from orchestrator import semantic_cache  # noqa: E402

pytestmark = pytest.mark.integration

URL_VARIABLE = "HA_AUTH_TEST_REDIS_URL"
SEMANTIC_KEYS = 1300  # more than two SCAN/UNLINK batches of 500
OTHER_KEYS = ("conversation:abc", "athena:session:1", "athena_semantic_other")


@pytest.fixture(scope="module")
def redis_url() -> str:
    url = os.environ.get(URL_VARIABLE)
    if not url:
        pytest.fail(f"{URL_VARIABLE} is not set: the real-Redis tier must run against a server, not skip")
    import redis

    try:
        redis.Redis.from_url(url, socket_connect_timeout=3).ping()
    except Exception as exc:
        pytest.fail(f"{URL_VARIABLE} does not answer PING ({type(exc).__name__}): the real-Redis tier cannot pass vacuously")
    return url


async def _seed(url: str):
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, decode_responses=True)
    await client.flushdb()
    pipe = client.pipeline()
    for n in range(SEMANTIC_KEYS):
        category = "weather" if n % 2 else "dining"
        pipe.set(f"athena_semantic:{category}_default_x:{n:08x}:kv2:kb_abc:mode_owner:iface_chat", "{}")
    for key in OTHER_KEYS:
        pipe.set(key, "keep")
    await pipe.execute()
    return client


async def _flush(url: str, **kwargs):
    client = await _seed(url)
    try:
        original = semantic_cache.get_cache_client
        semantic_cache.get_cache_client = lambda: SimpleNamespace(client=client)
        try:
            removed = await semantic_cache.invalidate_cache(**kwargs)
        finally:
            semantic_cache.get_cache_client = original
        remaining = sorted([key async for key in client.scan_iter(match="*", count=500)])
        return removed, remaining
    finally:
        await client.flushdb()
        await client.aclose()


def test_a_no_argument_flush_removes_every_semantic_key_and_only_those(redis_url):
    removed, remaining = asyncio.run(_flush(redis_url))
    assert removed == SEMANTIC_KEYS
    assert remaining == sorted(OTHER_KEYS)


def test_a_pattern_flush_removes_only_matching_semantic_keys(redis_url):
    removed, remaining = asyncio.run(_flush(redis_url, pattern="weather_*"))
    assert removed == SEMANTIC_KEYS // 2
    assert sum(k.startswith("athena_semantic:dining_") for k in remaining) == SEMANTIC_KEYS - SEMANTIC_KEYS // 2
    assert not any(k.startswith("athena_semantic:weather_") for k in remaining)
    assert set(OTHER_KEYS) <= set(remaining)


def test_a_bare_wildcard_pattern_cannot_reach_other_keys(redis_url):
    removed, remaining = asyncio.run(_flush(redis_url, pattern="*"))
    assert removed == SEMANTIC_KEYS
    assert remaining == sorted(OTHER_KEYS)


def test_a_flush_on_an_unreachable_server_is_zero():
    import redis.asyncio as aioredis

    dead = aioredis.from_url("redis://127.0.0.1:9/0", socket_connect_timeout=1, decode_responses=True)

    async def run():
        original = semantic_cache.get_cache_client
        semantic_cache.get_cache_client = lambda: SimpleNamespace(client=dead)
        try:
            return await semantic_cache.invalidate_cache()
        finally:
            semantic_cache.get_cache_client = original
            await dead.aclose()

    assert asyncio.run(run()) == 0
