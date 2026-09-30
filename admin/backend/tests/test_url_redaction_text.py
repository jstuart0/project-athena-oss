"""redact_urls_in_text: strip URL userinfo from free text (error strings,
status details) before it reaches a log line or an API response."""
import pytest

from app.utils.url_validators import redact_urls_in_text


@pytest.mark.parametrize("raw, expected", [
    ('error "http://u:p@h:6333/x"', 'error "http://h:6333/x"'),
    ("(see http://u:p@h:6333)", "(see http://h:6333)"),
    ("two: http://a:b@one:1/x and https://c:d@two:2", "two: http://one:1/x and https://two:2"),
], ids=["quoted", "parenthesised", "two_urls"])
def test_redacts_userinfo(raw, expected):
    assert redact_urls_in_text(raw) == expected


def test_redacts_password_with_slash_hash_query():
    assert redact_urls_in_text("http://u:p/ss#w?d@h:6333") == "http://h:6333"


def test_text_without_userinfo_unchanged():
    text = "connection refused to http://h:6333/collections (attempt 2)"
    assert redact_urls_in_text(text) == text
