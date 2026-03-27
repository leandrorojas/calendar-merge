import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from merge import (
    EventAction,
    MergeEvent,
    get_datetime,
    get_from_list,
    build_datetime,
    get_tag,
    _normalize_skip_days,
    _calculate_future_date,
    _end_of_day,
    _reconcile_events,
)


# --- EventAction enum ---

class TestEventAction:
    def test_values(self):
        assert EventAction.none.value == 0
        assert EventAction.add.value == 1
        assert EventAction.delete.value == 2


# --- MergeEvent dataclass ---

class TestMergeEvent:
    def test_construction(self):
        dt = datetime(2026, 3, 27, 10, 0)
        event = MergeEvent(title="test", start=dt, end=dt, full_event=None, action=EventAction.add)
        assert event.title == "test"
        assert event.start == dt
        assert event.action == EventAction.add

    def test_action_mutable(self):
        dt = datetime(2026, 3, 27, 10, 0)
        event = MergeEvent(title="test", start=dt, end=dt, full_event=None, action=None)
        event.action = EventAction.delete
        assert event.action == EventAction.delete

    def test_none_fields(self):
        dt = datetime(2026, 3, 27, 10, 0)
        event = MergeEvent(title=None, start=dt, end=dt, full_event=None, action=None)
        assert event.title is None
        assert event.full_event is None
        assert event.action is None


# --- get_datetime ---

class TestGetDatetime:
    def test_strips_seconds_and_microseconds(self):
        dt = datetime(2026, 3, 27, 14, 30, 45, 123456)
        result = get_datetime(dt)
        assert result == datetime(2026, 3, 27, 14, 30)
        assert result.second == 0
        assert result.microsecond == 0

    def test_preserves_timezone(self):
        tz = ZoneInfo("America/New_York")
        dt = datetime(2026, 3, 27, 14, 30, 45, tzinfo=tz)
        result = get_datetime(dt)
        assert result.tzinfo == tz

    def test_naive_datetime(self):
        dt = datetime(2026, 3, 27, 14, 30, 45)
        result = get_datetime(dt)
        assert result.tzinfo is None

    def test_midnight_boundary(self):
        dt = datetime(2026, 1, 1, 0, 0, 59)
        result = get_datetime(dt)
        assert result == datetime(2026, 1, 1, 0, 0)


# --- get_from_list ---

class TestGetFromList:
    def test_dict_get(self):
        assert get_from_list({"a": 1}, "a") == 1

    def test_missing_key(self):
        assert get_from_list({"a": 1}, "b") is None

    def test_non_dict_input(self):
        assert get_from_list([1, 2], "x") is None

    def test_none_input(self):
        assert get_from_list(None, "x") is None

    def test_value_is_none(self):
        assert get_from_list({"a": None}, "a") is None


# --- build_datetime ---

class TestBuildDatetime:
    def test_basic(self):
        tz = ZoneInfo("UTC")
        result = build_datetime([0, 2026, 3, 27, 14, 30], tz)
        assert result == datetime(2026, 3, 27, 14, 30, tzinfo=tz)

    def test_non_utc_timezone(self):
        tz = ZoneInfo("America/New_York")
        result = build_datetime([0, 2026, 3, 27, 14, 30], tz)
        assert result.tzinfo == tz

    def test_tuple_input(self):
        tz = ZoneInfo("UTC")
        result = build_datetime((0, 2026, 3, 27, 14, 30), tz)
        assert result == datetime(2026, 3, 27, 14, 30, tzinfo=tz)

    def test_midnight(self):
        tz = ZoneInfo("UTC")
        result = build_datetime([0, 2026, 1, 1, 0, 0], tz)
        assert result == datetime(2026, 1, 1, 0, 0, tzinfo=tz)

    def test_short_sequence_raises(self):
        with pytest.raises(IndexError):
            build_datetime([0, 2026, 3], ZoneInfo("UTC"))


# --- get_tag ---

class TestGetTag:
    def test_basic(self):
        assert get_tag("foo") == "[foo]"

    def test_empty_string(self):
        assert get_tag("") == "[]"

    def test_nested_brackets(self):
        assert get_tag("[nested]") == "[[nested]]"


# --- _normalize_skip_days ---

class TestNormalizeSkipDays:
    def test_string_input(self):
        assert _normalize_skip_days("5, 6") == ["5", "6"]

    def test_string_extra_whitespace(self):
        assert _normalize_skip_days(" 5 , 6 , ") == ["5", "6"]

    def test_list_of_ints(self):
        assert _normalize_skip_days([5, 6]) == ["5", "6"]

    def test_list_of_strings(self):
        assert _normalize_skip_days(["5", "6"]) == ["5", "6"]

    def test_empty_string(self):
        assert _normalize_skip_days("") == []

    def test_none(self):
        assert _normalize_skip_days(None) == []

    def test_empty_list(self):
        assert _normalize_skip_days([]) == []

    def test_single_value(self):
        assert _normalize_skip_days("0") == ["0"]


# --- _calculate_future_date ---

class TestCalculateFutureDate:
    def test_no_skip_days(self):
        # Monday 2026-03-23 + 5 days = Saturday 2026-03-28
        start = datetime(2026, 3, 23)
        result = _calculate_future_date(start, 5, [])
        assert result == datetime(2026, 3, 28)

    def test_skip_weekends_from_monday(self):
        # Monday 2026-03-23, counting from next day: Tue(1), Wed(2), Thu(3), Fri(4), Sat(skip), Sun(skip), Mon(5)
        start = datetime(2026, 3, 23)
        result = _calculate_future_date(start, 5, ["5", "6"])
        assert result == datetime(2026, 3, 30)
        assert result.weekday() == 0  # Monday

    def test_skip_weekends_spanning_weekend(self):
        # Thursday 2026-03-26 + 3 non-weekend days with skip [5, 6]
        # Fri(1), Sat(skip), Sun(skip), Mon(2), Tue(3) = Tuesday 2026-03-31
        start = datetime(2026, 3, 26)
        result = _calculate_future_date(start, 3, ["5", "6"])
        assert result == datetime(2026, 3, 31)
        assert result.weekday() == 1  # Tuesday

    def test_zero_future_days(self):
        start = datetime(2026, 3, 23)
        result = _calculate_future_date(start, 0, ["5", "6"])
        assert result == start

    def test_future_days_one(self):
        # Monday + 1 = Tuesday
        start = datetime(2026, 3, 23)
        result = _calculate_future_date(start, 1, [])
        assert result == datetime(2026, 3, 24)

    def test_only_one_day_allowed(self):
        # Skip everything except Sunday (6). Start Monday 2026-03-23.
        # skip 0,1,2,3,4,5 = Mon-Sat. Only Sunday counts.
        # Next Sunday = 2026-03-29 (count 1), then 2026-04-05 (count 2)
        start = datetime(2026, 3, 23)
        result = _calculate_future_date(start, 2, ["0", "1", "2", "3", "4", "5"])
        assert result == datetime(2026, 4, 5)
        assert result.weekday() == 6  # Sunday


# --- _end_of_day ---

class TestEndOfDay:
    def test_basic(self):
        tz = ZoneInfo("UTC")
        dt = datetime(2026, 3, 27, 10, 30, tzinfo=tz)
        result = _end_of_day(dt)
        assert result == datetime(2026, 3, 27, 23, 59, 59, tzinfo=tz)

    def test_already_end_of_day(self):
        dt = datetime(2026, 3, 27, 23, 59, 59)
        result = _end_of_day(dt)
        assert result == dt

    def test_midnight_input(self):
        dt = datetime(2026, 3, 27, 0, 0, 0)
        result = _end_of_day(dt)
        assert result == datetime(2026, 3, 27, 23, 59, 59)

    def test_preserves_naive(self):
        dt = datetime(2026, 3, 27, 10, 30)
        result = _end_of_day(dt)
        assert result.tzinfo is None


# --- _reconcile_events ---

def _make_event(start_hour, end_hour, title="test", action=None, full_event=None) -> MergeEvent:
    """Helper to create MergeEvent instances for testing."""
    start = datetime(2026, 3, 27, start_hour, 0, tzinfo=ZoneInfo("UTC"))
    end = datetime(2026, 3, 27, end_hour, 0, tzinfo=ZoneInfo("UTC"))
    return MergeEvent(title=title, start=start, end=end, full_event=full_event, action=action)


class TestReconcileEvents:
    def test_no_icloud_all_source_added(self):
        source = [_make_event(9, 10), _make_event(11, 12)]
        merge_events, has_additions = _reconcile_events([], source)
        assert has_additions is True
        assert len(merge_events) == 2
        assert all(e.action == EventAction.add for e in merge_events)

    def test_matching_events_marked_none(self):
        icloud = [_make_event(9, 10, title="[WRK] Work/Google")]
        source = [_make_event(9, 10)]
        merge_events, has_additions = _reconcile_events(icloud, source)
        assert has_additions is False
        assert len(merge_events) == 1
        assert merge_events[0].action == EventAction.none

    def test_icloud_not_in_source_deleted(self):
        icloud = [_make_event(9, 10, title="[WRK] Work/Google")]
        source = []
        merge_events, has_additions = _reconcile_events(icloud, source)
        assert has_additions is False
        assert len(merge_events) == 1
        assert merge_events[0].action == EventAction.delete

    def test_source_not_in_icloud_added(self):
        icloud = [_make_event(9, 10, title="[WRK] Work/Google")]
        source = [_make_event(9, 10), _make_event(14, 15)]
        merge_events, has_additions = _reconcile_events(icloud, source)
        assert has_additions is True
        added = [e for e in merge_events if e.action == EventAction.add]
        matched = [e for e in merge_events if e.action == EventAction.none]
        assert len(added) == 1
        assert added[0].start.hour == 14
        assert len(matched) == 1

    def test_both_empty(self):
        merge_events, has_additions = _reconcile_events([], [])
        assert has_additions is False
        assert len(merge_events) == 0

    def test_duplicate_times_in_source(self):
        icloud = [_make_event(9, 10, title="[WRK] Work/Google")]
        source = [_make_event(9, 10), _make_event(9, 10)]
        merge_events, has_additions = _reconcile_events(icloud, source)
        # One matches icloud, one is new
        assert has_additions is True
        added = [e for e in merge_events if e.action == EventAction.add]
        matched = [e for e in merge_events if e.action == EventAction.none]
        assert len(added) == 1
        assert len(matched) == 1

    def test_duplicate_times_in_icloud(self):
        icloud = [_make_event(9, 10, title="[WRK] Work/Google"), _make_event(9, 10, title="[WRK] Work/Google")]
        source = [_make_event(9, 10)]
        merge_events, has_additions = _reconcile_events(icloud, source)
        # One icloud matches, other is deleted
        assert has_additions is False
        deleted = [e for e in merge_events if e.action == EventAction.delete]
        matched = [e for e in merge_events if e.action == EventAction.none]
        assert len(deleted) == 1
        assert len(matched) == 1

    def test_mixed_add_delete_none(self):
        icloud = [
            _make_event(9, 10, title="[WRK] Work/Google"),   # matches source
            _make_event(11, 12, title="[WRK] Work/Google"),   # no source match → delete
        ]
        source = [
            _make_event(9, 10),   # matches icloud
            _make_event(14, 15),  # no icloud match → add
        ]
        merge_events, has_additions = _reconcile_events(icloud, source)
        assert has_additions is True
        actions = {e.action: [] for e in merge_events}
        for e in merge_events:
            actions[e.action].append(e)
        assert len(actions[EventAction.none]) == 1
        assert len(actions[EventAction.delete]) == 1
        assert len(actions[EventAction.add]) == 1
