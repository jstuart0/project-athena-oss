"""
SMS Service Module for Project Athena.

Provides Twilio integration for sending SMS messages to guests,
including content detection, message splitting, and cost tracking.
"""

from .service import SMSService
from .content_detector import (
    detect_textable_content,
    extract_sms_content,
    DetectedContent,
    CONTENT_PATTERNS,
)
from .splitter import split_for_sms
from .text_me_that import (
    is_text_me_that_request,
    handle_text_me_that,
    handle_phone_capture,
    extract_phone_from_query,
)
from .tips import (
    TipsService,
    get_tips_service,
    get_trigger_condition,
    TRIGGER_CONDITIONS,
    DEFAULT_TIPS,
)
from .scheduler import (
    SMSScheduler,
    get_scheduler,
    start_scheduler,
    stop_scheduler,
    schedule_delayed_sms,
    calculate_trigger_time,
)

__all__ = [
    'SMSService',
    'detect_textable_content',
    'extract_sms_content',
    'DetectedContent',
    'CONTENT_PATTERNS',
    'split_for_sms',
    # Text Me That handler
    'is_text_me_that_request',
    'handle_text_me_that',
    'handle_phone_capture',
    'extract_phone_from_query',
    # Tips system
    'TipsService',
    'get_tips_service',
    'get_trigger_condition',
    'TRIGGER_CONDITIONS',
    'DEFAULT_TIPS',
    # Scheduler
    'SMSScheduler',
    'get_scheduler',
    'start_scheduler',
    'stop_scheduler',
    'schedule_delayed_sms',
    'calculate_trigger_time',
]
