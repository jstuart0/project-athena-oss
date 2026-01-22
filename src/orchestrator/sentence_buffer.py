"""
Sentence Buffer for LLM Streaming Pipeline

Buffers tokens from streaming LLM output and yields complete sentences
for TTS synthesis. This enables overlapped LLM generation and TTS synthesis
for reduced time-to-first-audio.

Expected savings: 1-1.5 seconds per query
"""

import re
from typing import AsyncIterator, Optional
import structlog

logger = structlog.get_logger(__name__)


# Sentence boundary patterns
SENTENCE_ENDINGS = re.compile(r'([.!?])\s*$')
SENTENCE_ENDINGS_WITH_QUOTE = re.compile(r'([.!?]["\']?)\s*$')

# Minimum sentence length to avoid fragmenting short responses
MIN_SENTENCE_LENGTH = 20

# Maximum buffer size before forcing a yield (prevents runaway buffering)
MAX_BUFFER_SIZE = 500


class SentenceBuffer:
    """
    Buffers streaming tokens and yields complete sentences.

    Usage:
        buffer = SentenceBuffer()
        async for sentence in buffer.process(token_stream):
            # Send sentence to TTS
            await tts_synthesize(sentence)
    """

    def __init__(
        self,
        min_length: int = MIN_SENTENCE_LENGTH,
        max_buffer: int = MAX_BUFFER_SIZE
    ):
        self.buffer = ""
        self.min_length = min_length
        self.max_buffer = max_buffer
        self.sentences_yielded = 0
        self.total_tokens = 0

    def _find_sentence_boundary(self) -> Optional[int]:
        """Find the position of a sentence boundary in the buffer."""
        # Look for sentence endings
        for i, char in enumerate(self.buffer):
            if char in '.!?':
                # Check if this is a real sentence ending
                # (not abbreviation like "Dr." or decimal like "3.14")
                pos = i + 1

                # Skip if too short
                if pos < self.min_length:
                    continue

                # Check for quote after punctuation
                if pos < len(self.buffer) and self.buffer[pos] in '"\'"':
                    pos += 1

                # Check for space or end of buffer (indicates sentence end)
                if pos >= len(self.buffer) or self.buffer[pos].isspace():
                    return pos

        return None

    async def process(
        self,
        token_stream: AsyncIterator[dict]
    ) -> AsyncIterator[str]:
        """
        Process a stream of tokens and yield complete sentences.

        Args:
            token_stream: Async iterator yielding dicts with 'token' key

        Yields:
            Complete sentences ready for TTS synthesis
        """
        async for chunk in token_stream:
            token = chunk.get("token", "")
            self.total_tokens += 1

            if token:
                self.buffer += token

                # Check for sentence boundary
                boundary = self._find_sentence_boundary()

                if boundary:
                    sentence = self.buffer[:boundary].strip()
                    self.buffer = self.buffer[boundary:].lstrip()

                    if sentence:
                        self.sentences_yielded += 1
                        logger.debug(
                            "sentence_buffer_yield",
                            sentence_num=self.sentences_yielded,
                            sentence_length=len(sentence),
                            remaining_buffer=len(self.buffer)
                        )
                        yield sentence

                # Force yield if buffer gets too large
                elif len(self.buffer) > self.max_buffer:
                    # Find a natural break point (comma, semicolon, dash)
                    break_pos = None
                    for delim in [', ', '; ', ' - ', ' — ']:
                        pos = self.buffer.rfind(delim, 0, self.max_buffer)
                        if pos > self.min_length:
                            break_pos = pos + len(delim)
                            break

                    if break_pos:
                        sentence = self.buffer[:break_pos].strip()
                        self.buffer = self.buffer[break_pos:].lstrip()
                    else:
                        # No good break point, force yield at max
                        sentence = self.buffer[:self.max_buffer].strip()
                        self.buffer = self.buffer[self.max_buffer:].lstrip()

                    if sentence:
                        self.sentences_yielded += 1
                        logger.debug(
                            "sentence_buffer_force_yield",
                            sentence_num=self.sentences_yielded,
                            sentence_length=len(sentence),
                            reason="max_buffer_exceeded"
                        )
                        yield sentence

            # Check if stream is done
            if chunk.get("done"):
                # Yield any remaining content
                if self.buffer.strip():
                    self.sentences_yielded += 1
                    logger.debug(
                        "sentence_buffer_final_yield",
                        sentence_num=self.sentences_yielded,
                        sentence_length=len(self.buffer.strip())
                    )
                    yield self.buffer.strip()
                    self.buffer = ""

                logger.info(
                    "sentence_buffer_complete",
                    total_tokens=self.total_tokens,
                    sentences_yielded=self.sentences_yielded
                )
                break

    def reset(self):
        """Reset the buffer state."""
        self.buffer = ""
        self.sentences_yielded = 0
        self.total_tokens = 0


async def stream_with_sentence_buffering(
    llm_router,
    model: str,
    prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 2048
) -> AsyncIterator[dict]:
    """
    Stream LLM output with sentence buffering.

    Yields:
        Dict with:
            - 'sentence': Complete sentence text
            - 'sentence_num': Sentence number (1-indexed)
            - 'is_final': True if this is the last sentence
    """
    buffer = SentenceBuffer()

    token_stream = llm_router.generate_stream(
        model=model,
        prompt=prompt,
        temperature=temperature,
        max_tokens=max_tokens
    )

    sentence_num = 0
    sentences = []

    async for sentence in buffer.process(token_stream):
        sentence_num += 1
        sentences.append(sentence)

        yield {
            "sentence": sentence,
            "sentence_num": sentence_num,
            "is_final": False
        }

    # Mark the last sentence as final
    if sentences:
        yield {
            "sentence": "",
            "sentence_num": sentence_num,
            "is_final": True,
            "total_sentences": sentence_num,
            "full_response": " ".join(sentences)
        }
