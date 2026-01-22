"""
SMS Message Splitting Logic.

Intelligently splits long messages into SMS-friendly segments while
preserving meaning and readability.
"""

import re
from typing import List


def split_for_sms(
    content: str,
    max_segment_length: int = 1500,
    add_part_numbers: bool = True
) -> List[str]:
    """
    Split content into SMS-friendly segments while preserving meaning.

    Strategy:
    1. If content fits in one message, return as-is
    2. Split by paragraphs first (preserve meaning)
    3. If paragraphs too long, split by sentences
    4. If sentences too long, split by words (last resort)
    5. Add part numbers if multiple segments

    Args:
        content: The full message content
        max_segment_length: Maximum characters per segment (default 1500)
        add_part_numbers: Whether to add "(1/N)" prefix to multi-part messages

    Returns:
        List of message segments
    """
    content = content.strip()

    # If it fits, ship it
    if len(content) <= max_segment_length:
        return [content]

    segments: List[str] = []
    current_segment = ""

    # Split by double newlines (paragraphs)
    paragraphs = re.split(r'\n\n+', content)

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Check if paragraph fits in current segment
        test_length = len(current_segment) + len(para) + 2  # +2 for \n\n

        if test_length <= max_segment_length:
            if current_segment:
                current_segment += "\n\n" + para
            else:
                current_segment = para
        else:
            # Save current segment if not empty
            if current_segment:
                segments.append(current_segment.strip())
                current_segment = ""

            # Check if paragraph itself is too long
            if len(para) > max_segment_length:
                # Split paragraph by sentences
                sentence_segments = _split_by_sentences(para, max_segment_length)
                segments.extend(sentence_segments[:-1])
                current_segment = sentence_segments[-1] if sentence_segments else ""
            else:
                current_segment = para

    # Don't forget the last segment
    if current_segment:
        segments.append(current_segment.strip())

    # Add part numbers if multiple segments
    if len(segments) > 1 and add_part_numbers:
        total = len(segments)
        segments = [f"({i+1}/{total})\n{seg}" for i, seg in enumerate(segments)]

    return segments


def _split_by_sentences(text: str, max_length: int) -> List[str]:
    """
    Split text by sentences when paragraphs are too long.

    Args:
        text: Paragraph text to split
        max_length: Maximum segment length

    Returns:
        List of sentence-based segments
    """
    # Sentence-ending patterns
    sentence_ends = re.compile(r'(?<=[.!?])\s+')
    sentences = sentence_ends.split(text)

    segments: List[str] = []
    current_segment = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        test_length = len(current_segment) + len(sentence) + 1

        if test_length <= max_length:
            if current_segment:
                current_segment += " " + sentence
            else:
                current_segment = sentence
        else:
            if current_segment:
                segments.append(current_segment.strip())

            # If single sentence is too long, split by words
            if len(sentence) > max_length:
                word_segments = _split_by_words(sentence, max_length)
                segments.extend(word_segments[:-1])
                current_segment = word_segments[-1] if word_segments else ""
            else:
                current_segment = sentence

    if current_segment:
        segments.append(current_segment.strip())

    return segments


def _split_by_words(text: str, max_length: int) -> List[str]:
    """
    Last resort: split by words when sentences are too long.

    Args:
        text: Text to split
        max_length: Maximum segment length

    Returns:
        List of word-based segments
    """
    words = text.split()
    segments: List[str] = []
    current_segment = ""

    for word in words:
        test_length = len(current_segment) + len(word) + 1

        if test_length <= max_length:
            if current_segment:
                current_segment += " " + word
            else:
                current_segment = word
        else:
            if current_segment:
                segments.append(current_segment.strip())
            current_segment = word

    if current_segment:
        segments.append(current_segment.strip())

    return segments


def estimate_segment_count(content: str, max_segment_length: int = 1500) -> int:
    """
    Estimate how many SMS segments content will require.

    Useful for cost estimation before sending.

    Args:
        content: Message content
        max_segment_length: Maximum characters per segment

    Returns:
        Estimated segment count
    """
    if not content:
        return 0

    content = content.strip()
    if len(content) <= max_segment_length:
        return 1

    # Quick estimate based on length
    return (len(content) // max_segment_length) + 1
