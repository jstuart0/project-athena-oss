"""Whether an answer ends in speech, and how it is rendered for that.

One seam for every Athena path. Callers keep carrying ``interface_type`` and ask
this module what it means; the spoken-interface set, the normalizer, the rule
order for OpenAI-compatible callers and the trusted-proxy walk live here.

No I/O and no config reads: callers pass parsed networks in.
"""
from __future__ import annotations

import functools
import logging
import re as _re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping, Optional, Sequence, Tuple

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


# --- length cap -------------------------------------------------------------------

# Within this many tokens of the cap, a backend that reports no stop reason is
# assumed to have been cut off.
CAP_HIT_TOKEN_MARGIN = 5

_LENGTH_STOPS = frozenset({"length", "max_tokens", "max_output_tokens"})
_NATURAL_STOPS = frozenset({
    "stop", "end_turn", "stop_sequence", "tool_calls", "tool_use", "function_call", "eos", "complete", "completed",
})


def speech_max_tokens(channel: OutputChannel, requested: Optional[int], cap: int) -> Optional[int]:
    """The token limit for one answer-producing call: for speech, the smaller
    of what the site asked for and the voice cap (the cap when it asked for
    nothing); for text, exactly what the site asked for."""
    if channel is OutputChannel.SPEECH:
        return min(requested, cap) if requested else cap
    return requested


def normalize_stop_reason(raw: Any) -> Optional[str]:
    """`length` when a backend says it hit its token limit, `stop` when it
    finished on its own, None when it said nothing usable.

    Covers Ollama `done_reason`, OpenAI/MLX `finish_reason`, Anthropic
    `stop_reason` and Google `finish_reason` (`MAX_TOKENS`).
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in _LENGTH_STOPS:
        return "length"
    if value in _NATURAL_STOPS:
        return "stop"
    return None


def answer_hit_cap(result: Optional[Mapping[str, Any]], cap: Optional[int]) -> bool:
    """Whether a backend result (or stream-final chunk) was cut off by the cap.

    A reported stop reason decides. With none reported, a token count within
    CAP_HIT_TOKEN_MARGIN of the cap counts as a cut-off. Never raises.
    """
    try:
        result = result or {}
        for key in ("stop_reason", "done_reason", "finish_reason"):
            reason = normalize_stop_reason(result.get(key))
            if reason is not None:
                return reason == "length"
        if not cap:
            return False
        for key in ("eval_count", "output_tokens", "tokens"):
            tokens = result.get(key)
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens > 0:
                return tokens >= cap - CAP_HIT_TOKEN_MARGIN
        return False
    except Exception:
        return False


_SENTENCE_TERMINATORS = ".!?\u2026"
_CLOSERS = "\"')]}\u201d\u2019\u00bb"
_ABBREVIATIONS = frozenset({"e.g.", "i.e.", "dr.", "mr.", "mrs.", "ms.", "st.", "vs.", "etc."})
_LIST_ORDINAL_RE = _re.compile(r"(?:^|\n)[ \t]*\d+\.$")


def _is_sentence_end(text: str, i: int) -> bool:
    """Whether the terminator at text[i] ends a sentence: followed by the end,
    whitespace, a closing quote/bracket or another terminator; and not a
    list ordinal ("1."), an abbreviation ("Dr.") or a decimal point."""
    ch = text[i]
    nxt = text[i + 1] if i + 1 < len(text) else ""
    if nxt and not (nxt.isspace() or nxt in _CLOSERS or nxt in _SENTENCE_TERMINATORS):
        return False
    if ch != ".":
        return True
    start = max(text.rfind(" ", 0, i), text.rfind("\n", 0, i)) + 1
    if text[start:i + 1].lower() in _ABBREVIATIONS:
        return False
    return not _LIST_ORDINAL_RE.search(text[: i + 1])


def trim_to_complete_sentence(text: Optional[str]) -> str:
    """Cut a cut-off answer after its last complete sentence.

    A terminator that is a decimal point, an abbreviation (e.g. i.e. Dr. Mr.
    Mrs. Ms. St. vs. etc.) or a list ordinal at line start doesn't count; a
    closing quote or bracket after a terminator stays; an ellipsis counts. With
    no terminator at all (or empty input) the text comes back unchanged.
    """
    if not text:
        return text or ""
    for i in range(len(text) - 1, -1, -1):
        if text[i] in _SENTENCE_TERMINATORS and _is_sentence_end(text, i):
            end = i + 1
            while end < len(text) and text[end] in _CLOSERS:
                end += 1
            return text[:end]
    return text


def render_for_channel(text: Optional[str], channel: OutputChannel) -> Optional[str]:
    """Text for a TEXT channel is returned as is; for SPEECH it is normalized.

    SPEECH input is capped at SPEECH_SINK_MAX_CHARS first, so no caller can hand
    the normalizer an unbounded string. Never raises: on error the capped input
    comes back (never more than the cap) and the log names the exception class only, never the text.
    """
    if channel is not OutputChannel.SPEECH or not text:
        return text
    capped = text[:SPEECH_SINK_MAX_CHARS]
    try:
        return normalize_for_tts(capped)
    except Exception as exc:
        logger.error("tts_normalization_failed", extra={"error": type(exc).__name__})
        return capped


def render_sink_text(text: Optional[str], *, sink: str) -> Optional[str]:
    """What an Athena TTS sink hands to its engine: SPEECH rendering, capped.

    The sink knows its output is spoken, whatever channel the answer was
    produced for; rendering is idempotent, so text already rendered upstream
    passes through unchanged. Logs lengths only, never the text.
    """
    if not text:
        return text
    spoken = render_for_channel(text, OutputChannel.SPEECH)
    logger.info(
        "speech_sink_rendered",
        extra={
            "sink": sink,
            "text_length": len(text),
            "spoken_length": len(spoken),
            "truncated": len(text) > SPEECH_SINK_MAX_CHARS,
        },
    )
    return spoken


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
    speech_networks: Sequence[IPNetwork],
    trusted_proxies: Sequence[IPNetwork],
    *,
    log_error: bool = True,
) -> Tuple[Tuple[IPNetwork, ...], int]:
    """Drop speech networks that overlap a trusted proxy network; return (kept, dropped count).

    A proxy is never a speech client, and with no forwarded address a trusted
    peer would otherwise resolve to itself. Logs the count only, never an
    address; a caller that logs through its own configured logger passes
    ``log_error=False`` and reports the returned count.
    """
    kept = tuple(
        net for net in speech_networks
        if not any(net.version == proxy.version and net.overlaps(proxy) for proxy in trusted_proxies)
    )
    dropped = len(speech_networks) - len(kept)
    if dropped and log_error:
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
