"""ATHENA-127 Phase 1 -- unit tests for src/shared/booking_window.py.

Discriminating cases per the plan (Implementer note, step 5): the New
York timezone cases are the ones that fail against pre-fix behaviour; the
UTC case is a no-op guard only.
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

import pytest
import structlog

from shared import booking_window as bw


NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _fresh_log_latches():
    reset = getattr(bw, "_reset_log_latches_for_tests", None)
    if reset:
        reset()
    yield
    if reset:
        reset()


class TestResolvePropertyTz:
    def test_valid_zone(self):
        tz, valid = bw.resolve_property_tz("America/New_York")
        assert valid is True
        assert tz == NY

    def test_empty_is_invalid(self):
        with structlog.testing.capture_logs() as logs:
            tz, valid = bw.resolve_property_tz("")
        assert valid is False
        assert tz == timezone.utc
        assert any(e["event"] == "booking_timezone_invalid" for e in logs)

    def test_unknown_zone_is_invalid(self):
        with structlog.testing.capture_logs() as logs:
            tz, valid = bw.resolve_property_tz("Not/AZone")
        assert valid is False
        assert tz == timezone.utc
        assert any(e["event"] == "booking_timezone_invalid" for e in logs)

    def test_utc_is_valid_no_op_guard(self):
        tz, valid = bw.resolve_property_tz("UTC")
        assert valid is True
        assert tz.utcoffset(datetime(2026, 7, 1)) == timedelta(0)


class TestLocalizeLocalDst:
    def test_16_00_unambiguous_both_dates(self):
        spring = bw.localize_local(date(2026, 3, 8), "16:00", NY)
        fall = bw.localize_local(date(2026, 11, 1), "16:00", NY)
        assert spring == datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)
        assert fall == datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)

    def test_spring_forward_gap_maps_to_0730z(self):
        # 02:30 doesn't exist on 2026-03-08 (US spring-forward). fold=0
        # (the default; PEP 495, no pytz) resolves to 07:30Z == 03:30 EDT.
        result = bw.localize_local(date(2026, 3, 8), "02:30", NY)
        assert result == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)

    def test_fall_back_ambiguous_takes_first_occurrence(self):
        # 01:30 occurs twice on 2026-11-01 (US fall-back). fold=0 takes the
        # first (EDT, -04:00) -> 05:30Z.
        result = bw.localize_local(date(2026, 11, 1), "01:30", NY)
        assert result == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)


class TestFeedValueToUtc:
    def test_date_only_uses_default_hhmm_in_property_zone(self):
        result = bw.feed_value_to_utc(date(2026, 7, 1), default_hhmm="16:00", tz=NY)
        assert result == datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)

    def test_floating_datetime_localised_in_property_zone(self):
        floating = datetime(2026, 7, 1, 16, 0)
        result = bw.feed_value_to_utc(floating, default_hhmm="16:00", tz=NY)
        assert result == datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)

    def test_aware_datetime_preserved_as_utc(self):
        aware = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)
        result = bw.feed_value_to_utc(aware, default_hhmm="16:00", tz=NY)
        assert result == aware


class TestDbValueToUtc:
    def test_naive_read_from_db_is_utc(self):
        naive = datetime(2026, 7, 1, 20, 0)
        assert bw.db_value_to_utc(naive) == datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)

    def test_aware_is_converted(self):
        aware = datetime(2026, 7, 1, 16, 0, tzinfo=NY)
        assert bw.db_value_to_utc(aware) == aware.astimezone(timezone.utc)


_EXPECTED_BLOCK_LABELS = {
    "airbnb": {"not available", "airbnb (not available)"},
    "vrbo": {"blocked"},
    "lodgify": {"closed period", "blocked", "closed", "closed block", "owner block"},
}
_EXPECTED_BLOCK_LABELS["generic_ical"] = set().union(*_EXPECTED_BLOCK_LABELS.values()) | {"unavailable"}
_TYPES = ("airbnb", "vrbo", "lodgify", "generic_ical")


def _classify(summary, source_type):
    # generic_ical goes through the default argument -- the mode service's
    # legacy-calendar_url call (bookings.py) passes no source_type.
    if source_type == "generic_ical":
        return bw.classify_summary(summary)
    return bw.classify_summary(summary, source_type=source_type)


_BLOCKED_CASES = [
    ("Airbnb (Not available)", "airbnb"),
    ("  not   AVAILABLE ", "airbnb"),
    ("Blocked", "vrbo"),
    ("Closed Block", "lodgify"),
    ("  CLOSED  ", "lodgify"),
    ("Owner Block", "lodgify"),
    ("Closed   Period", "lodgify"),
    ("unavailable", "generic_ical"),
    ("BLOCKED", "generic_ical"),
    ("Not Available", "generic_ical"),
]

_CONFIRMED_EVERY_TYPE = [
    "Tom Blocked", "Unavailable Smith", "Notavailable", "John Block", "Blockley",
    "Closed Cooper", "Owner", "Reserved", "J*** D**", "Closedo", "Blocked - owner",
    "Not available until May",
]
_CONFIRMED_CASES = [("Blocked", "airbnb"), ("Closed", "vrbo"), ("Unavailable", "lodgify")] + [
    (summary, t) for summary in _CONFIRMED_EVERY_TYPE for t in _TYPES
]


class TestClassifySummary:
    def test_label_tables_match_the_plan(self):
        for source_type, expected in _EXPECTED_BLOCK_LABELS.items():
            assert set(bw.BLOCK_LABELS_BY_SOURCE_TYPE[source_type]) == expected, source_type
        assert set(bw.BLOCK_LABELS_BY_SOURCE_TYPE) == set(_EXPECTED_BLOCK_LABELS)
        assert not hasattr(bw, "BLOCK_SUMMARY_MARKERS")

    @pytest.mark.parametrize("summary,source_type", _BLOCKED_CASES)
    def test_blocked(self, summary, source_type):
        assert _classify(summary, source_type) == "blocked"

    @pytest.mark.parametrize("summary,source_type", _CONFIRMED_CASES)
    def test_confirmed(self, summary, source_type):
        assert _classify(summary, source_type) == "confirmed"

    def test_confirmed_population_floor_and_named_members(self):
        assert len(_CONFIRMED_CASES) >= 16
        for named in (("Tom Blocked", "generic_ical"), ("Unavailable Smith", "generic_ical"),
                      ("Notavailable", "generic_ical"), ("Blocked", "airbnb")):
            assert named in _CONFIRMED_CASES

    @pytest.mark.parametrize("summary", ["Blocked", "Tom Blocked", "unavailable", "Reserved", "", None])
    def test_default_is_generic(self, summary):
        assert bw.classify_summary(summary) == bw.classify_summary(summary, source_type="generic_ical")

    @pytest.mark.parametrize("unknown", [None, "", "hostaway"])
    def test_unknown_type_uses_generic_set(self, unknown):
        assert bw.classify_summary("unavailable", source_type=unknown) == "blocked"
        assert bw.classify_summary("Tom Blocked", source_type=unknown) == "confirmed"


class TestDayPair:
    def test_naive_db_value_is_utc_then_localized(self):
        # 04:30 UTC is 00:30 EDT the same day, but 03:30 on the 2nd UTC is
        # 23:30 EDT on the 1st: a naive DB value must be read as UTC first.
        start = datetime(2026, 7, 2, 3, 30)
        end = datetime(2026, 7, 5, 15, 0)
        assert bw.day_pair(start, end, NY) == (date(2026, 7, 1), date(2026, 7, 5))

    def test_naive_0430_utc_in_new_york_is_the_previous_date(self):
        start = datetime(2026, 1, 10, 4, 30)
        assert bw.day_pair(start, start + timedelta(days=2), NY)[0] == date(2026, 1, 9)

    def test_aware_matches_naive_utc(self):
        naive = datetime(2026, 7, 1, 20, 0)
        aware = naive.replace(tzinfo=timezone.utc)
        assert bw.day_pair(naive, naive, NY) == bw.day_pair(aware, aware, NY)

    def test_stay_day_pair_delegates(self):
        b = bw.Booking(id=1, key="k", source="s", label="l",
                       start=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
                       end=datetime(2026, 7, 5, 15, tzinfo=timezone.utc))
        assert bw.stay_day_pair(b, NY) == bw.day_pair(b.start, b.end, NY) == (date(2026, 7, 1), date(2026, 7, 5))


class TestBufferClamp:
    def test_within_range_unchanged(self):
        assert bw.clamp_buffer_hours(2) == 2.0

    def test_clamps_high(self):
        with structlog.testing.capture_logs() as logs:
            assert bw.clamp_buffer_hours(500) == 168.0
        assert any(e["event"] == "mode_booking_buffer_clamped" for e in logs)

    def test_clamps_negative(self):
        assert bw.clamp_buffer_hours(-5) == 0.0

    def test_invalid_defaults_to_zero(self):
        assert bw.clamp_buffer_hours("garbage") == 0.0


def _booking(start, end, source="admin", key="k1", label="l", is_test=False, booking_id=1):
    return bw.Booking(id=booking_id, key=key, source=source, label=label, start=start, end=end, is_test=is_test)


class TestIsActive:
    def test_start_inclusive_end_exclusive(self):
        b = _booking(
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
        )
        zero = timedelta(0)
        assert bw.is_active(b, b.start, zero, zero) is True
        assert bw.is_active(b, b.end, zero, zero) is False
        assert bw.is_active(b, b.end - timedelta(seconds=1), zero, zero) is True

    def test_buffers_extend_window(self):
        b = _booking(
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
        )
        before = timedelta(hours=2)
        after = timedelta(hours=1)
        assert bw.is_active(b, b.start - timedelta(hours=1), before, after) is True
        assert bw.is_active(b, b.start - timedelta(hours=3), before, after) is False
        assert bw.is_active(b, b.end + timedelta(minutes=30), before, after) is True
        assert bw.is_active(b, b.end + timedelta(hours=2), before, after) is False

    def test_end_le_start_is_invalid_and_ignored(self):
        b = _booking(
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
        )
        with structlog.testing.capture_logs() as logs:
            assert bw.is_active(b, b.start, timedelta(0), timedelta(0)) is False
        assert any(e["event"] == "mode_booking_invalid_window" for e in logs)

    def test_turnover_gap(self):
        # A checks out 11:00, B checks in 16:00, after=1h, before=2h.
        a = _booking(
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
            key="a",
        )
        b = _booking(
            datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc),
            key="b",
        )
        before = timedelta(hours=2)
        after = timedelta(hours=1)
        owner_gap_start = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
        owner_gap_mid = datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc)
        guest_before_gap = datetime(2026, 7, 5, 11, 59, tzinfo=timezone.utc)
        guest_after_gap = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)

        assert bw.active_booking([a, b], owner_gap_start, before, after) is None
        assert bw.active_booking([a, b], owner_gap_mid, before, after) is None
        assert bw.active_booking([a, b], guest_before_gap, before, after) is a
        assert bw.active_booking([a, b], guest_after_gap, before, after) is b

    def test_back_to_back_no_gap(self):
        a = _booking(
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
            key="a",
        )
        b = _booking(
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc),
            key="b",
        )
        zero = timedelta(0)
        assert bw.active_booking([a, b], a.end, zero, zero) is b

    def test_overlapping_buffers_stay_guest_throughout(self):
        a = _booking(
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
            key="a",
        )
        b = _booking(
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc),
            key="b",
        )
        before = timedelta(hours=2)
        after = timedelta(hours=2)
        midpoint = datetime(2026, 7, 5, 12, 30, tzinfo=timezone.utc)
        assert bw.active_booking([a, b], midpoint, before, after) is not None


class TestStayDayPair:
    def test_local_day_pair(self):
        b = _booking(
            datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),
        )
        # 2026-07-02T00:00Z is 2026-07-01 20:00 in New York.
        assert bw.stay_day_pair(b, NY) == (date(2026, 7, 1), date(2026, 7, 5))


class TestMerge:
    def test_exact_key_collapses(self):
        a = _booking(
            datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc),
            source="admin", key="lodgify_1", label="admin label",
        )
        dup = _booking(
            datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc),
            source="admin", key="lodgify_1", label="admin label dup",
        )
        merged = bw.merge([a, dup], [], suppressed_pairs=set(), tz=NY)
        assert len(merged) == 1

    def test_day_pair_union_widens_window(self):
        admin_row = _booking(
            datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),  # 16:00 NY
            datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc),  # 11:00 NY
            source="admin", key="lodgify_1", label="admin",
        )
        ical_twin = _booking(
            datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc),  # 17:00 NY, same local day
            datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc),  # 12:00 NY, same local day, later checkout
            source="ical", key="uid-999", label="ical <uid-999>",
        )
        merged = bw.merge([admin_row], [ical_twin], suppressed_pairs=set(), tz=NY)
        assert len(merged) == 1
        result = merged[0]
        # Union: widest window (never shrinks guest time), non-ical label/source preferred.
        assert result.source == "admin"
        assert result.start == admin_row.start
        assert result.end == ical_twin.end

    def test_suppression_hits_advisory_not_primary(self):
        admin_row = _booking(
            datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc),
            source="admin", key="lodgify_1", label="admin",
        )
        suppressed_ical = _booking(
            datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc),
            source="ical", key="uid-suppressed", label="ical",
        )
        pair = bw.stay_day_pair(suppressed_ical, NY)
        merged = bw.merge([admin_row], [suppressed_ical], suppressed_pairs={pair}, tz=NY)
        assert len(merged) == 1
        assert merged[0].key == "lodgify_1"

    def test_suppression_never_drops_primary(self):
        admin_row = _booking(
            datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc),
            source="admin", key="lodgify_1", label="admin",
        )
        pair = bw.stay_day_pair(admin_row, NY)
        merged = bw.merge([admin_row], [], suppressed_pairs={pair}, tz=NY)
        assert len(merged) == 1


class TestLogOncePerValue:
    """D5: the timezone, clamp and invalid-window logs fire once per process
    per distinct value, not on every call (these run on every /mode read)."""

    @staticmethod
    def _count(logs, event):
        return sum(1 for e in logs if e["event"] == event)

    def test_invalid_timezone_logged_once_per_name(self):
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                bw.resolve_property_tz("Mars/Olympus")
            bw.resolve_property_tz("Venus/Ishtar")
            bw.resolve_property_tz("")
            bw.resolve_property_tz("")
        assert self._count(logs, "booking_timezone_invalid") == 3

    def test_buffer_clamp_logged_once_per_value(self):
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                assert bw.clamp_buffer_hours(500) == 168.0
            assert bw.clamp_buffer_hours(-1) == 0.0
        assert self._count(logs, "mode_booking_buffer_clamped") == 2

    def test_invalid_window_logged_once_per_booking(self):
        inverted = _booking(
            datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
        )
        other = _booking(inverted.start, inverted.end, key="k2")
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                assert bw.is_active(inverted, inverted.start, timedelta(0), timedelta(0)) is False
            assert bw.is_active(other, other.start, timedelta(0), timedelta(0)) is False
        assert self._count(logs, "mode_booking_invalid_window") == 2


class TestNaiveDbValueIsUtcRegardlessOfHostZone:
    def test_naive_is_utc_under_a_non_utc_host_tz(self, monkeypatch):
        if not hasattr(time, "tzset"):
            pytest.skip("time.tzset unavailable on this platform")
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        try:
            naive = datetime(2026, 7, 1, 20, 0)
            assert bw.db_value_to_utc(naive) == datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
        finally:
            monkeypatch.undo()
            time.tzset()


class TestMergeIsTest:
    def test_is_test_only_when_every_member_is_test(self):
        start = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc)
        admin_test = _booking(start, end, source="admin", key="a", is_test=True)
        ical_real = _booking(start, end, source="ical", key="i", is_test=False, booking_id=None)
        ical_test = _booking(start, end, source="ical", key="i", is_test=True, booking_id=None)

        mixed = bw.merge([admin_test], [ical_real], suppressed_pairs=set(), tz=NY)
        both = bw.merge([admin_test], [ical_test], suppressed_pairs=set(), tz=NY)
        assert [b.is_test for b in mixed] == [False]
        assert [b.is_test for b in both] == [True]


class TestBookingKey:
    def test_byte_identical_to_the_d3_formula(self):
        import hashlib

        for source, external_id in [("lodgify", "lodgify_9001"), ("ical", "1418fb94e984-zq@example.org@airbnb.com"),
                                    ("manual", "manual_ab12"), ("ical", "")]:
            expected = hashlib.sha256(f"{source}|{external_id}".encode()).hexdigest()[:16]
            assert bw.booking_key(source, external_id) == expected

    def test_both_services_use_the_shared_helper(self):
        import inspect

        import mode_service.bookings as ms_bookings

        assert "hashlib" not in inspect.getsource(ms_bookings)
        internal_src = open("admin/backend/app/routes/internal.py").read()
        assert "hashlib.sha256(f\"{source}|" not in internal_src
        assert "booking_key(" in internal_src


class TestLogLatchIsBounded:
    def test_latch_never_exceeds_its_cap(self):
        with structlog.testing.capture_logs():
            for i in range(bw._LOG_LATCH_CAP + 50 if hasattr(bw, "_LOG_LATCH_CAP") else 1100):
                bw.clamp_buffer_hours(1000 + i)
        assert len(bw._logged_once) <= getattr(bw, "_LOG_LATCH_CAP", 1024)
