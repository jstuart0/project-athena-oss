"""Audit rows and logs for SMS settings/preferences keep only a phone
number's last four digits."""
from __future__ import annotations


def test_phone_fields_are_reduced_to_last_four():
    from app.routes.sms_webhook import sms_session_id  # noqa: F401  (module import sanity)
    from app.routes.sms import redact_phone_fields

    values = {
        "from_number": "+15555550100",
        "preferred_phone": "+1 (555) 555-0199",
        "phone_number": None,
        "enabled": True,
        "quiet_hours_start": "22:00",
    }
    assert redact_phone_fields(values) == {
        "from_number_last4": "0100",
        "preferred_phone_last4": "0199",
        "phone_number_last4": None,
        "enabled": True,
        "quiet_hours_start": "22:00",
    }


def test_redaction_leaves_the_input_untouched():
    from app.routes.sms import redact_phone_fields

    values = {"from_number": "+15555550100"}
    redact_phone_fields(values)
    assert values == {"from_number": "+15555550100"}
