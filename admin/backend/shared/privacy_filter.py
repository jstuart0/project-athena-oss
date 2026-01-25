"""
Privacy Filter for Cloud LLM Requests

Filters sensitive information before sending queries to cloud providers.
This ensures user privacy is protected even when using external cloud LLM services.

Open Source Compatible - All patterns are standard regex without vendor dependencies.
"""

import re
from typing import Dict, List, Optional, Tuple
import structlog

logger = structlog.get_logger(__name__)


# Patterns for sensitive data detection
SENSITIVE_PATTERNS = {
    'ssn': r'\b\d{3}-\d{2}-\d{4}\b',
    'credit_card': r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',
    'phone': r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b',
    'email': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    'ip_address': r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b',
    'api_key': r'\b(sk-|api[_-]?key|token)[A-Za-z0-9_-]{20,}\b',
    'password_in_text': r'(?i)password[:\s]+[^\s]+',
    'bank_account': r'\b\d{8,17}\b(?=.*(?:account|routing|bank))',
}

# Words that indicate potentially sensitive context
SENSITIVE_CONTEXT_WORDS = [
    'password', 'secret', 'credential', 'private', 'confidential',
    'ssn', 'social security', 'bank', 'account number',
    'credit card', 'routing number', 'pin', 'passcode',
]


class PrivacyFilter:
    """
    Filter sensitive information from queries before cloud transmission.

    This class provides:
    - Pattern-based detection and redaction of sensitive data
    - Context word detection in strict mode
    - Decision support for blocking high-risk queries
    """

    def __init__(self, enabled: bool = True, strict_mode: bool = False):
        """
        Initialize the privacy filter.

        Args:
            enabled: Whether filtering is active
            strict_mode: Whether to also check for sensitive context words
        """
        self.enabled = enabled
        self.strict_mode = strict_mode
        self._compiled_patterns = {
            name: re.compile(pattern, re.IGNORECASE)
            for name, pattern in SENSITIVE_PATTERNS.items()
        }

    def filter_query(self, query: str) -> Tuple[str, List[str]]:
        """
        Filter sensitive data from a query.

        Args:
            query: The original query text

        Returns:
            Tuple of (filtered_query, list of detected sensitive data types)
        """
        if not self.enabled:
            return query, []

        filtered = query
        detected_types = []

        # Check for pattern matches
        for data_type, pattern in self._compiled_patterns.items():
            if pattern.search(filtered):
                filtered = pattern.sub(f'[REDACTED_{data_type.upper()}]', filtered)
                detected_types.append(data_type)

        # In strict mode, also check for sensitive context words
        if self.strict_mode:
            query_lower = query.lower()
            for word in SENSITIVE_CONTEXT_WORDS:
                if word in query_lower:
                    detected_types.append(f'context:{word}')
                    logger.warning(
                        "privacy_filter_context_warning",
                        word=word,
                        query_preview=query[:50] + "..." if len(query) > 50 else query
                    )

        if detected_types:
            logger.info(
                "privacy_filter_applied",
                detected_types=detected_types,
                original_length=len(query),
                filtered_length=len(filtered)
            )

        return filtered, detected_types

    def should_block_cloud(self, query: str) -> Tuple[bool, str]:
        """
        Determine if a query should be blocked from cloud transmission.

        High-risk patterns (API keys, passwords, credentials) will cause
        queries to be blocked rather than just redacted.

        Args:
            query: The query to check

        Returns:
            Tuple of (should_block, reason)
        """
        if not self.enabled:
            return False, ""

        _, detected_types = self.filter_query(query)

        # Block if API keys or credentials detected
        high_risk_types = {'api_key', 'password_in_text', 'context:password',
                          'context:secret', 'context:credential'}
        blocked_types = set(detected_types) & high_risk_types

        if blocked_types:
            reason = f"Query contains high-risk sensitive data: {', '.join(blocked_types)}"
            logger.warning(
                "privacy_filter_blocked_query",
                blocked_types=list(blocked_types),
                reason=reason
            )
            return True, reason

        return False, ""

    def get_risk_level(self, query: str) -> str:
        """
        Assess the privacy risk level of a query.

        Returns:
            'low', 'medium', or 'high' risk level
        """
        _, detected_types = self.filter_query(query)

        if not detected_types:
            return 'low'

        high_risk = {'api_key', 'password_in_text', 'ssn', 'credit_card', 'bank_account'}
        if any(t in high_risk or t.startswith('context:') for t in detected_types):
            return 'high'

        return 'medium'


# Global instance management
_privacy_filter: Optional[PrivacyFilter] = None


def get_privacy_filter(enabled: bool = True, strict_mode: bool = False) -> PrivacyFilter:
    """
    Get or create the global privacy filter instance.

    Args:
        enabled: Whether filtering is active
        strict_mode: Whether to also check for sensitive context words

    Returns:
        PrivacyFilter instance
    """
    global _privacy_filter
    if _privacy_filter is None:
        _privacy_filter = PrivacyFilter(enabled=enabled, strict_mode=strict_mode)
    return _privacy_filter


def configure_privacy_filter(enabled: bool = True, strict_mode: bool = False) -> None:
    """
    Configure the global privacy filter.

    This is typically called at startup based on feature flag values.
    """
    global _privacy_filter
    _privacy_filter = PrivacyFilter(enabled=enabled, strict_mode=strict_mode)
    logger.info(
        "privacy_filter_configured",
        enabled=enabled,
        strict_mode=strict_mode
    )


def filter_for_cloud(query: str) -> str:
    """
    Convenience function to filter a query for cloud transmission.

    Args:
        query: The original query

    Returns:
        Filtered query with sensitive data redacted
    """
    filtered, _ = get_privacy_filter().filter_query(query)
    return filtered


def should_block_for_cloud(query: str) -> Tuple[bool, str]:
    """
    Convenience function to check if a query should be blocked from cloud.

    Args:
        query: The query to check

    Returns:
        Tuple of (should_block, reason)
    """
    return get_privacy_filter().should_block_cloud(query)
