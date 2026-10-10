"""Whether an answer ends in speech, and how it is rendered for that.

One seam for every Athena path. Callers keep carrying ``interface_type`` and ask
this module what it means; the spoken-interface set, the normalizer, the rule
order for OpenAI-compatible callers and the trusted-proxy walk live here.

No I/O and no config reads: callers pass parsed networks in.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional, Sequence, Tuple

from shared.client_throttle import (
    IPNetwork,
    in_networks,
    parse_ip,
    resolve_rate_client,
)
from shared.tts_normalizer import normalize_for_tts

logger = logging.getLogger(__name__)

# Longest text a sink hands to the normalizer; matches jarvis-web's TTS_MAX_CHARS default.
SPEECH_SINK_MAX_CHARS = 5000

_SPOKEN_INTERFACE_TYPES = frozenset({"voice"})


class OutputChannel(str, Enum):
    SPEECH = "speech"
    TEXT = "text"


def channel_for_interface_type(interface_type: Optional[str]) -> OutputChannel:
    """``voice`` is speech; ``text``, ``chat`` and anything unknown are text."""
    return OutputChannel.SPEECH if interface_type in _SPOKEN_INTERFACE_TYPES else OutputChannel.TEXT


def interface_type_for_channel(channel: OutputChannel) -> Literal["voice", "text"]:
    return "voice" if channel is OutputChannel.SPEECH else "text"


def render_for_channel(text: Optional[str], channel: OutputChannel) -> Optional[str]:
    """Text for a TEXT channel is returned as is; for SPEECH it is normalized.

    SPEECH input is capped at SPEECH_SINK_MAX_CHARS first, so no caller can hand
    the normalizer an unbounded string. Never raises: on error the input comes
    back and the log names the exception class only, never the text.
    """
    if channel is not OutputChannel.SPEECH or not text:
        return text
    try:
        return normalize_for_tts(text[:SPEECH_SINK_MAX_CHARS])
    except Exception as exc:
        logger.error("tts_normalization_failed", extra={"error": type(exc).__name__})
        return text


def render_answer(text: Optional[str], interface_type: Optional[str]) -> Optional[str]:
    return render_for_channel(text, channel_for_interface_type(interface_type))


def renders_spoken_answer(handler):
    """Render a route's ``QueryResponse.answer`` by the request's ``interface_type``.

    Rendering happens after the handler returns, so anything the handler stored
    (session, cache) stays raw. Responses without an ``answer`` attribute
    (``Response``, ``StreamingResponse``) pass through untouched, and exceptions
    propagate unchanged. FastAPI passes the request by keyword; a direct call may
    pass it first.
    """

    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        response = await handler(*args, **kwargs)
        request = kwargs["request"] if "request" in kwargs else args[0]
        if hasattr(response, "answer"):
            response.answer = render_answer(response.answer, request.interface_type)
        return response

    return wrapper


def without_networks_overlapping(
    speech_networks: Sequence[IPNetwork], trusted_proxies: Sequence[IPNetwork]
) -> Tuple[Tuple[IPNetwork, ...], int]:
    """Drop speech networks that overlap a trusted proxy network; return (kept, dropped count).

    A proxy is never a speech client, and with no forwarded address a trusted
    peer would otherwise resolve to itself. Logs the count only, never an address.
    """
    kept = tuple(
        net for net in speech_networks
        if not any(net.version == proxy.version and net.overlaps(proxy) for proxy in trusted_proxies)
    )
    dropped = len(speech_networks) - len(kept)
    if dropped:
        logger.error("openai_speech_networks_overlap_trusted_proxies", extra={"count": dropped})
    return kept, dropped


@dataclass(frozen=True)
class ChannelDecision:
    channel: OutputChannel
    rule: Literal["voice_path", "client_network", "default"]


def classify_openai_caller(
    *,
    voice_path: bool,
    peer: object,
    forwarded_for: Optional[str],
    speech_networks: Sequence[IPNetwork],
    trusted_proxies: Sequence[IPNetwork],
) -> ChannelDecision:
    """Rules in order: a ``/v1/voice`` route, then the client network, else TEXT.

    The client is the address the gateway limiter attributes the request to
    (``resolve_rate_client``, nearest hop outside the trusted proxies), so an
    untrusted peer is itself and a forged left-hand forwarded hop never counts.
    An unparseable or missing peer, or garbage forwarded hops, give TEXT.
    ``forwarded_for`` is ``read_forwarded_for(headers)`` at the caller.
    """
    if voice_path:
        return ChannelDecision(OutputChannel.SPEECH, "voice_path")
    if speech_networks:
        client = resolve_rate_client(peer, forwarded_for, None, trusted_proxies, trust_cf=False)
        if in_networks(parse_ip(client.ip), speech_networks):
            return ChannelDecision(OutputChannel.SPEECH, "client_network")
    return ChannelDecision(OutputChannel.TEXT, "default")
