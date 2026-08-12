"""Tests for iCloud event collection, ICS parsing, and syncing."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

import merge
from tests.conftest import BA_TZ, FakeCalendarService, icloud_raw_event, ics_bytes, merge_event, utc

# --- _collect_icloud_events ---


class TestCollectIcloudEvents:
    def test_collects_a_timed_event(self):
        events = merge._collect_icloud_events([icloud_raw_event()], skip_days=[])

        assert len(events) == 1
        event = events[0]
        assert event.title == "[W] Work/Google"
        # 09:00 in Buenos Aires (UTC-3) is 12:00 UTC.
        assert event.start == datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
        assert event.end == datetime(2026, 8, 12, 13, 0, tzinfo=UTC)
        assert event.action is None

    def test_keeps_the_raw_event_for_deletion(self):
        raw = icloud_raw_event()

        events = merge._collect_icloud_events([raw], skip_days=[])

        assert events[0].full_event is raw

    def test_skips_all_day_events(self):
        events = merge._collect_icloud_events([icloud_raw_event(all_day=True)], skip_days=[])

        assert events == []

    def test_skips_when_all_day_flag_missing(self):
        raw = icloud_raw_event()
        del raw[merge.ICLOUD_FIELD_ALL_DAY_EVENT]

        assert merge._collect_icloud_events([raw], skip_days=[]) == []

    @pytest.mark.parametrize(
        "missing",
        [
            merge.ICLOUD_FIELD_START_DATE,
            merge.ICLOUD_FIELD_END_DATE,
            merge.ICLOUD_FIELD_TITLE,
            merge.ICLOUD_FIELD_TZ,
        ],
    )
    def test_skips_events_missing_required_fields(self, missing):
        raw = icloud_raw_event()
        raw[missing] = None

        assert merge._collect_icloud_events([raw], skip_days=[]) == []

    def test_skips_events_on_skip_days(self):
        # 2026-08-15 is a Saturday (weekday 5) in UTC.
        raw = icloud_raw_event(start=(0, 2026, 8, 15, 9, 0), end=(0, 2026, 8, 15, 10, 0))

        assert merge._collect_icloud_events([raw], skip_days=["5"]) == []

    def test_keeps_events_not_on_skip_days(self):
        raw = icloud_raw_event(start=(0, 2026, 8, 12, 9, 0), end=(0, 2026, 8, 12, 10, 0))

        assert len(merge._collect_icloud_events([raw], skip_days=["5", "6"])) == 1

    def test_skip_day_is_evaluated_in_utc(self):
        """A late-evening local event can land on the next UTC day.

        23:00 on Friday in Buenos Aires is 02:00 Saturday UTC, so skipping
        Saturday must drop it.
        """
        raw = icloud_raw_event(start=(0, 2026, 8, 14, 23, 0), end=(0, 2026, 8, 15, 0, 0))

        assert merge._collect_icloud_events([raw], skip_days=["5"]) == []

    def test_handles_empty_input(self):
        assert merge._collect_icloud_events([], skip_days=["5", "6"]) == []

    def test_collects_multiple_events(self):
        raws = [
            icloud_raw_event(title="a"),
            icloud_raw_event(title="b", all_day=True),
            icloud_raw_event(title="c"),
        ]

        titles = [event.title for event in merge._collect_icloud_events(raws, skip_days=[])]

        assert titles == ["a", "c"]


# --- _parse_source_events ---


def parse(ics_events, skip_days=(), start=None, end=None):
    calendar = Calendar.from_ical(ics_bytes(ics_events))
    window_start = start or utc(2026, 8, 1)
    window_end = end or utc(2026, 8, 31, 23, 59)
    return merge._parse_source_events(calendar, list(skip_days), window_start, window_end)


class TestParseSourceEvents:
    def test_parses_event_inside_window(self):
        events = parse([{"start": "20260812T120000Z", "end": "20260812T130000Z"}])

        assert len(events) == 1
        assert events[0].start == utc(2026, 8, 12, 12, 0)
        assert events[0].end == utc(2026, 8, 12, 13, 0)

    def test_source_events_have_no_title_or_raw_event(self):
        # Titles are assigned later, from the source-calendar config.
        events = parse([{"start": "20260812T120000Z", "end": "20260812T130000Z"}])

        assert events[0].title is None
        assert events[0].full_event is None
        assert events[0].action is None

    def test_skips_out_of_office_events(self):
        events = parse([{"start": "20260812T120000Z", "end": "20260812T130000Z", "transp": "TRANSPARENT"}])

        assert events == []

    def test_skips_events_before_window(self):
        events = parse(
            [{"start": "20260701T120000Z", "end": "20260701T130000Z"}],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31),
        )

        assert events == []

    def test_skips_events_after_window(self):
        events = parse(
            [{"start": "20260915T120000Z", "end": "20260915T130000Z"}],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31),
        )

        assert events == []

    def test_window_boundaries_are_inclusive(self):
        start = utc(2026, 8, 12, 12, 0)
        events = parse(
            [{"start": "20260812T120000Z", "end": "20260812T130000Z"}],
            start=start,
            end=start,
        )

        assert len(events) == 1

    def test_skips_events_on_skip_days(self):
        # 2026-08-15 is a Saturday.
        events = parse([{"start": "20260815T120000Z", "end": "20260815T130000Z"}], skip_days=["5"])

        assert events == []

    def test_skips_all_day_events(self):
        """DTSTART with a bare date parses to a `date`, not a `datetime`."""
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:allday@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;VALUE=DATE:20260812\r\nDTEND;VALUE=DATE:20260813\r\n"
            b"SUMMARY:All day\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert events == []

    def test_skips_event_without_start_date(self):
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:nostart@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTEND:20260812T130000Z\r\nSUMMARY:No start\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert events == []

    def test_skips_event_without_end_date(self):
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:noend@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260812T120000Z\r\nSUMMARY:No end\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert events == []

    def test_converts_non_utc_event_to_utc(self):
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:tz@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;TZID=America/Argentina/Buenos_Aires:20260812T090000\r\n"
            b"DTEND;TZID=America/Argentina/Buenos_Aires:20260812T100000\r\n"
            b"SUMMARY:TZ event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert len(events) == 1
        assert events[0].start == utc(2026, 8, 12, 12, 0)

    def test_drops_seconds_from_source_times(self):
        events = parse([{"start": "20260812T120045Z", "end": "20260812T130059Z"}])

        assert events[0].start == utc(2026, 8, 12, 12, 0)
        assert events[0].end == utc(2026, 8, 12, 13, 0)

    def test_parses_multiple_events(self):
        events = parse(
            [
                {"start": "20260812T120000Z", "end": "20260812T130000Z"},
                {"start": "20260813T120000Z", "end": "20260813T130000Z"},
            ]
        )

        assert len(events) == 2

    def test_empty_calendar_yields_nothing(self):
        assert parse([]) == []


# --- _sync_events_to_icloud ---

CAL_TZ = ZoneInfo(BA_TZ)


class TestSyncEventsToIcloud:
    def test_adds_events_marked_add(self):
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert len(service.added) == 1
        added = service.added[0]
        assert added.pguid == "cal-guid"
        assert added.title == "[W] Work/Google"

    def test_add_converts_times_to_calendar_timezone(self):
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        # 12:00 UTC is 09:00 in Buenos Aires.
        assert service.added[0].start_date.hour == 9

    def test_deletes_events_marked_delete(self):
        raw = icloud_raw_event(guid="g-1", pguid="p-1")
        service = FakeCalendarService()
        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=raw,
            )
        ]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert len(service.removed) == 1
        assert service.removed[0].guid == "g-1"
        assert service.removed[0].pguid == "p-1"

    def test_ignores_events_marked_none(self):
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.none)]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert service.added == []
        assert service.removed == []

    def test_handles_mixed_actions(self):
        raw = icloud_raw_event(guid="g-2", pguid="p-2")
        service = FakeCalendarService()
        events = [
            merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add),
            merge_event(utc(2026, 8, 13, 12), utc(2026, 8, 13, 13), action=merge.EventAction.none),
            merge_event(
                utc(2026, 8, 14, 12),
                utc(2026, 8, 14, 13),
                action=merge.EventAction.delete,
                full_event=raw,
            ),
        ]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert len(service.added) == 1
        assert len(service.removed) == 1

    def test_add_failure_raises_runtime_error(self, quiet_terminal):
        service = FakeCalendarService(add_error=ConnectionError("boom"))
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        with pytest.raises(RuntimeError, match="Unable to add event"):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert "<failed>" in quiet_terminal

    def test_add_failure_preserves_cause(self):
        original = ConnectionError("boom")
        service = FakeCalendarService(add_error=original)
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        with pytest.raises(RuntimeError) as excinfo:
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert excinfo.value.__cause__ is original

    def test_delete_failure_raises_runtime_error(self, quiet_terminal):
        raw = icloud_raw_event()
        service = FakeCalendarService(remove_error=ConnectionError("nope"))
        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=raw,
            )
        ]

        with pytest.raises(RuntimeError, match="Unable to delete event"):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

    def test_empty_list_is_a_noop(self):
        service = FakeCalendarService()

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, [])

        assert service.added == []
        assert service.removed == []

    def test_ignores_unreconciled_event_with_no_action(self):
        """An action of None survives the `!= none` filter, so it must fall through.

        Reaching add_event/remove_event with an unreconciled event would either
        create a titleless entry or crash on the missing raw event.
        """
        service = FakeCalendarService()
        events = [
            merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=None),
            merge_event(utc(2026, 8, 13, 12), utc(2026, 8, 13, 13), action=merge.EventAction.add),
        ]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events)

        assert len(service.added) == 1
        assert service.removed == []
