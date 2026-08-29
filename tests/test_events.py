"""Tests for iCloud event collection, ICS parsing, and syncing."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from dateutil.rrule import rrulestr
from icalendar import Calendar

import merge
from tests.conftest import (
    BA_TZ,
    PRODID_GOOGLE,
    PRODID_OUTLOOK,
    PRODID_UNKNOWN,
    FakeCalendarService,
    icloud_raw_event,
    ics_bytes,
    merge_event,
    utc,
)

# --- _collect_icloud_events ---


class TestCollectIcloudEvents:
    def test_collects_a_timed_event(self):
        events = merge._collect_icloud_events([icloud_raw_event()])

        assert len(events) == 1
        event = events[0]
        assert event.title == "[W] Work/Google"
        # 09:00 in Buenos Aires (UTC-3) is 12:00 UTC.
        assert event.start == datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
        assert event.end == datetime(2026, 8, 12, 13, 0, tzinfo=UTC)
        assert event.action is None

    def test_keeps_the_raw_event_for_deletion(self):
        raw = icloud_raw_event()

        events = merge._collect_icloud_events([raw])

        assert events[0].full_event is raw

    def test_skips_all_day_events(self):
        events = merge._collect_icloud_events([icloud_raw_event(all_day=True)])

        assert events == []

    def test_skips_when_all_day_flag_missing(self):
        raw = icloud_raw_event()
        del raw[merge.ICLOUD_FIELD_ALL_DAY_EVENT]

        assert merge._collect_icloud_events([raw]) == []

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

        assert merge._collect_icloud_events([raw]) == []

    def test_does_not_filter_by_weekday(self):
        """Collection is skip_days-agnostic now.

        skip_days is per source, so a Saturday event must survive collection and
        be filtered later against the owning source's setting.
        """
        saturday = icloud_raw_event(start=(0, 2026, 8, 15, 9, 0), end=(0, 2026, 8, 15, 10, 0))

        assert len(merge._collect_icloud_events([saturday])) == 1

    def test_handles_empty_input(self):
        assert merge._collect_icloud_events([]) == []

    def test_collects_multiple_events(self):
        raws = [
            icloud_raw_event(title="a"),
            icloud_raw_event(title="b", all_day=True),
            icloud_raw_event(title="c"),
        ]

        titles = [event.title for event in merge._collect_icloud_events(raws)]

        assert titles == ["a", "c"]


# --- _select_source_icloud_events ---


class TestSelectSourceIcloudEvents:
    def test_selects_only_matching_titles(self):
        mine = merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), title="[W] Work/Google")
        theirs = merge_event(utc(2026, 8, 12, 14), utc(2026, 8, 12, 15), title="[X] Other/Outlook")

        selected = merge._select_source_icloud_events([mine, theirs], "[W] Work/Google", [])

        assert selected == [mine]

    def test_excludes_events_on_the_sources_skip_days(self):
        # 2026-08-15 is a Saturday (weekday 5) in UTC.
        saturday = merge_event(utc(2026, 8, 15, 12), utc(2026, 8, 15, 13), title="[W] Work/Google")
        weekday = merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), title="[W] Work/Google")

        selected = merge._select_source_icloud_events([saturday, weekday], "[W] Work/Google", ["5"])

        assert selected == [weekday]

    def test_keeps_weekend_events_when_source_does_not_skip_them(self):
        saturday = merge_event(utc(2026, 8, 15, 12), utc(2026, 8, 15, 13), title="[W] Work/Google")

        selected = merge._select_source_icloud_events([saturday], "[W] Work/Google", [])

        assert selected == [saturday]

    def test_weekday_is_evaluated_in_utc(self):
        # 02:00 Saturday UTC, which is 23:00 Friday in Buenos Aires.
        late_friday = merge_event(utc(2026, 8, 15, 2), utc(2026, 8, 15, 3), title="[W] Work/Google")

        assert merge._select_source_icloud_events([late_friday], "[W] Work/Google", ["5"]) == []

    def test_empty_input(self):
        assert merge._select_source_icloud_events([], "[W] Work/Google", ["5", "6"]) == []


# --- _is_google_feed ---


def calendar_with(prodid, events=None):
    payload = events or [{"start": "20260812T120000Z", "end": "20260812T130000Z"}]
    return Calendar.from_ical(ics_bytes(payload, prodid=prodid))


class TestIsGoogleFeed:
    def test_detects_google(self):
        assert merge._is_google_feed(calendar_with(PRODID_GOOGLE)) is True

    def test_outlook_is_not_google(self):
        assert merge._is_google_feed(calendar_with(PRODID_OUTLOOK)) is False

    def test_unknown_publisher_is_not_google(self):
        assert merge._is_google_feed(calendar_with(PRODID_UNKNOWN)) is False

    def test_match_is_case_insensitive(self):
        assert merge._is_google_feed(calendar_with("-//google inc//whatever//EN")) is True

    def test_missing_prodid(self):
        raw = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        assert merge._is_google_feed(Calendar.from_ical(raw)) is False


# --- _is_excluded_event ---


def vevent(transp=None, busy_status=None):
    """Return the single VEVENT from a rendered ICS document."""
    event = {"start": "20260812T120000Z", "end": "20260812T130000Z"}
    if transp is not None:
        event["transp"] = transp
    if busy_status is not None:
        event["busy_status"] = busy_status
    return next(iter(Calendar.from_ical(ics_bytes([event])).walk("VEVENT")))


class TestIsExcludedEventRfcFeed:
    """Non-Google feeds (Outlook, unknown) follow the RFC reading."""

    def test_transparent_is_excluded(self):
        assert merge._is_excluded_event(vevent("TRANSPARENT"), google_feed=False) is True

    def test_opaque_is_kept(self):
        assert merge._is_excluded_event(vevent("OPAQUE"), google_feed=False) is False

    def test_missing_transp_is_kept(self):
        # RFC 5545 defaults TRANSP to OPAQUE.
        assert merge._is_excluded_event(vevent(None), google_feed=False) is False

    @pytest.mark.parametrize("value", ["transparent", "Transparent", "TrAnSpArEnT"])
    def test_value_comparison_is_case_insensitive(self, value):
        assert merge._is_excluded_event(vevent(value), google_feed=False) is True

    @pytest.mark.parametrize("value", ["  TRANSPARENT  ", "\tTRANSPARENT\t", "TRANSPARENT\r", " transparent "])
    def test_padded_value_is_still_excluded(self, value):
        """icalendar hands back the raw value, whitespace included."""
        assert merge._is_excluded_event(vevent(value), google_feed=False) is True

    @pytest.mark.parametrize("value", ["  OPAQUE  ", "\tOPAQUE\r"])
    def test_padded_opaque_is_still_kept(self, value):
        assert merge._is_excluded_event(vevent(value), google_feed=False) is False

    def test_unknown_transp_value_is_kept(self):
        # Anything that is not TRANSPARENT blocks time, so keep the event rather
        # than silently dropping it.
        assert merge._is_excluded_event(vevent("SOMETHING-ELSE"), google_feed=False) is False


class TestIsExcludedEventOutlookBusyStatus:
    def test_out_of_office_is_excluded(self):
        event = vevent("OPAQUE", busy_status="OOF")

        assert merge._is_excluded_event(event, google_feed=False) is True

    def test_busy_is_kept(self):
        assert merge._is_excluded_event(vevent("OPAQUE", busy_status="BUSY"), google_feed=False) is False

    def test_tentative_is_kept(self):
        """Outlook TENTATIVE is the equivalent of a Google 'maybe'.

        Google feeds strip PARTSTAT entirely, so a 'maybe' there is
        indistinguishable from an accepted meeting and already syncs. Keeping
        TENTATIVE makes the two providers behave the same way.
        """
        assert merge._is_excluded_event(vevent("OPAQUE", busy_status="TENTATIVE"), google_feed=False) is False

    def test_free_busy_status_with_transparent(self):
        assert merge._is_excluded_event(vevent("TRANSPARENT", busy_status="FREE"), google_feed=False) is True

    @pytest.mark.parametrize("value", ["oof", " OOF ", "Oof"])
    def test_out_of_office_match_is_normalised(self, value):
        assert merge._is_excluded_event(vevent("OPAQUE", busy_status=value), google_feed=False) is True


class TestIsExcludedEventGoogleFeed:
    """On a Google feed, any explicit TRANSP marks self-blocked time."""

    def test_explicit_opaque_is_excluded(self):
        """Google writes TRANSP only for lunch, focus time and out of office.

        Real meetings carry no TRANSP at all, so an explicit OPAQUE means the
        user blocked the slot themselves and it must not sync.
        """
        assert merge._is_excluded_event(vevent("OPAQUE"), google_feed=True) is True

    def test_transparent_is_excluded(self):
        assert merge._is_excluded_event(vevent("TRANSPARENT"), google_feed=True) is True

    def test_missing_transp_is_kept(self):
        # This is what an actual meeting looks like on a Google feed.
        assert merge._is_excluded_event(vevent(None), google_feed=True) is False

    def test_unknown_transp_value_is_also_excluded(self):
        assert merge._is_excluded_event(vevent("SOMETHING-ELSE"), google_feed=True) is True


# --- _deduplicate_event_slots ---


class TestDeduplicateEventSlots:
    def test_collapses_identical_slots(self):
        a = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))
        b = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))

        assert merge._deduplicate_event_slots([a, b]) == [a]

    def test_keeps_the_first_occurrence(self):
        a = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))
        b = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))

        assert merge._deduplicate_event_slots([a, b])[0] is a

    def test_preserves_order_of_distinct_slots(self):
        first = merge_event(utc(2026, 8, 13, 9), utc(2026, 8, 13, 10))
        second = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))
        third = merge_event(utc(2026, 8, 13, 11), utc(2026, 8, 13, 12))

        assert merge._deduplicate_event_slots([first, second, third]) == [first, second, third]

    def test_collapses_non_contiguous_duplicates(self):
        """Dedup must not depend on duplicates being adjacent.

        Feeds interleave events, so the second copy of a slot is usually not the
        next entry. This pins the `seen` lookup rather than a neighbour check.
        """
        first = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))
        other = merge_event(utc(2026, 8, 13, 15), utc(2026, 8, 13, 16))
        repeat = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))

        assert merge._deduplicate_event_slots([first, other, repeat]) == [first, other]

    def test_collapses_duplicates_separated_by_several_events(self):
        first = merge_event(utc(2026, 8, 13, 9), utc(2026, 8, 13, 10))
        middle = [
            merge_event(utc(2026, 8, 13, 11), utc(2026, 8, 13, 12)),
            merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14)),
            merge_event(utc(2026, 8, 13, 15), utc(2026, 8, 13, 16)),
        ]
        repeat = merge_event(utc(2026, 8, 13, 9), utc(2026, 8, 13, 10))

        result = merge._deduplicate_event_slots([first, *middle, repeat])

        assert result == [first, *middle]

    def test_collapses_three_into_one(self):
        slot = (utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))
        events = [merge_event(*slot) for _ in range(3)]

        assert len(merge._deduplicate_event_slots(events)) == 1

    def test_same_start_different_end_is_a_different_slot(self):
        """Only exact matches collapse; overlaps are left alone deliberately."""
        full = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))
        half = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 13, 30))

        assert merge._deduplicate_event_slots([full, half]) == [full, half]

    def test_contained_slot_is_kept(self):
        outer = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 15))
        inner = merge_event(utc(2026, 8, 13, 13, 30), utc(2026, 8, 13, 14))

        assert merge._deduplicate_event_slots([outer, inner]) == [outer, inner]

    def test_same_end_different_start_is_a_different_slot(self):
        long_event = merge_event(utc(2026, 8, 13, 12), utc(2026, 8, 13, 14))
        short_event = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))

        assert merge._deduplicate_event_slots([long_event, short_event]) == [long_event, short_event]

    def test_empty_input(self):
        assert merge._deduplicate_event_slots([]) == []

    def test_single_event(self):
        only = merge_event(utc(2026, 8, 13, 13), utc(2026, 8, 13, 14))

        assert merge._deduplicate_event_slots([only]) == [only]


# --- _parse_source_events ---


def parse(ics_events, skip_days=(), start=None, end=None):
    calendar = Calendar.from_ical(ics_bytes(ics_events))
    window_start = start or utc(2026, 8, 1)
    window_end = end or utc(2026, 8, 31, 23, 59)
    return merge._parse_source_events(calendar, list(skip_days), window_start, window_end)


class TestRecurrenceExpansion:
    """`walk` yields only the series master; its occurrences must be generated.

    Outlook anchors a series at the date it was created, so a long-running weekly
    meeting has a DTSTART far outside any forward-looking window and contributed
    nothing at all before this.
    """

    def test_expands_a_series_anchored_before_the_window(self):
        events = parse(
            [{"start": "20260106T120000Z", "end": "20260106T130000Z", "rrule": "FREQ=WEEKLY;BYDAY=TU"}],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert len(events) == 4  # the Tuesdays in August 2026
        assert all(event.start.weekday() == 1 for event in events)

    def test_master_alone_contributes_nothing_outside_the_window(self):
        """The regression this fixes: anchor in the past, no occurrences generated."""
        events = parse(
            [{"start": "20250820T120000Z", "end": "20250820T130000Z", "rrule": "FREQ=WEEKLY;COUNT=3"}],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert events == []

    def test_preserves_the_occurrence_duration(self):
        events = parse(
            [{"start": "20260804T090000Z", "end": "20260804T103000Z", "rrule": "FREQ=WEEKLY;BYDAY=TU"}],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 15),
        )

        assert all((event.end - event.start).total_seconds() == 90 * 60 for event in events)

    def test_exdate_cancels_an_occurrence(self):
        """Without this, expansion invents busy time for cancelled meetings."""
        events = parse(
            [
                {
                    "start": "20260804T120000Z",
                    "end": "20260804T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                    "exdate": "20260811T120000Z",
                }
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert utc(2026, 8, 11, 12) not in [event.start for event in events]
        assert utc(2026, 8, 4, 12) in [event.start for event in events]

    def test_multiple_exdates_are_all_honoured(self):
        events = parse(
            [
                {
                    "start": "20260804T120000Z",
                    "end": "20260804T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                    "exdate": ["20260811T120000Z", "20260818T120000Z"],
                }
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        starts = [event.start for event in events]
        assert utc(2026, 8, 11, 12) not in starts
        assert utc(2026, 8, 18, 12) not in starts

    def test_recurrence_id_override_is_not_placed_twice(self):
        """A moved occurrence is published separately; the master must skip its slot.

        Otherwise the meeting lands at both its new and its original time.
        """
        events = parse(
            [
                {
                    "uid": "series@test",
                    "start": "20260804T120000Z",
                    "end": "20260804T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                },
                {
                    "uid": "series@test",
                    "start": "20260811T150000Z",
                    "end": "20260811T160000Z",
                    "recurrence_id": "20260811T120000Z",
                },
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        starts = [event.start for event in events]
        assert utc(2026, 8, 11, 15) in starts, "the moved occurrence should be kept"
        assert utc(2026, 8, 11, 12) not in starts, "the original slot must not be regenerated"

    def test_an_override_for_another_series_does_not_suppress_this_one(self):
        events = parse(
            [
                {
                    "uid": "series-a@test",
                    "start": "20260804T120000Z",
                    "end": "20260804T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                },
                {
                    "uid": "series-b@test",
                    "start": "20260811T150000Z",
                    "end": "20260811T160000Z",
                    "recurrence_id": "20260811T120000Z",
                },
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert utc(2026, 8, 11, 12) in [event.start for event in events]

    def test_skip_days_apply_to_generated_occurrences(self):
        events = parse(
            [{"start": "20260801T120000Z", "end": "20260801T130000Z", "rrule": "FREQ=DAILY"}],
            skip_days=("5", "6"),
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert all(str(event.start.weekday()) not in ("5", "6") for event in events)

    def test_a_malformed_rule_still_contributes_its_original_occurrence(self, quiet_terminal):
        """An unreadable rule costs its repeats, not the meeting itself.

        Dropping the master would silently remove a real event from the merged
        calendar, which is the failure this whole feature exists to fix.
        """
        events = parse(
            [
                {"start": "20260804T120000Z", "end": "20260804T130000Z", "rrule": "FREQ=NONSENSE"},
                {"start": "20260805T120000Z", "end": "20260805T130000Z"},
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert [event.start for event in events] == [utc(2026, 8, 4, 12), utc(2026, 8, 5, 12)]

    def test_a_malformed_rule_does_not_sink_the_feed(self, quiet_terminal):
        events = parse(
            [
                {"start": "20250101T120000Z", "end": "20250101T130000Z", "rrule": "FREQ=NONSENSE"},
                {"start": "20260805T120000Z", "end": "20260805T130000Z"},
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        # The broken master is outside the window, so only the ordinary event lands.
        assert [event.start for event in events] == [utc(2026, 8, 5, 12)]

    def test_a_cancelled_first_occurrence_is_not_resurrected(self):
        """The master must not be emitted alongside its own expansion.

        DTSTART is itself an occurrence. If the master also flows through the
        ordinary path, a first occurrence removed by EXDATE reappears -- inventing
        busy time for a meeting that was cancelled.
        """
        events = parse(
            [
                {
                    "start": "20260804T120000Z",
                    "end": "20260804T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                    "exdate": "20260804T120000Z",
                }
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert utc(2026, 8, 4, 12) not in [event.start for event in events]
        assert utc(2026, 8, 11, 12) in [event.start for event in events]

    def test_an_overridden_first_occurrence_is_not_left_behind(self):
        """Same trap via RECURRENCE-ID: the moved meeting must not also sit at its old time."""
        events = parse(
            [
                {
                    "uid": "series@test",
                    "start": "20260804T120000Z",
                    "end": "20260804T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                },
                {
                    "uid": "series@test",
                    "start": "20260804T160000Z",
                    "end": "20260804T170000Z",
                    "recurrence_id": "20260804T120000Z",
                },
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        starts = [event.start for event in events]
        assert utc(2026, 8, 4, 16) in starts
        assert utc(2026, 8, 4, 12) not in starts

    def test_dtstart_is_kept_when_it_does_not_match_the_rule(self):
        """RFC 5545 makes DTSTART an occurrence; dateutil omits it when unmatched.

        A Wednesday anchor on a BYDAY=TU rule is a real meeting that synced before
        expansion existed, so losing it would be a regression, not a refinement.
        """
        events = parse(
            [{"start": "20260805T120000Z", "end": "20260805T130000Z", "rrule": "FREQ=WEEKLY;BYDAY=TU"}],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert utc(2026, 8, 5, 12) in [event.start for event in events]

    def test_a_cancelled_anchor_is_not_readded(self):
        """Re-adding DTSTART must not override EXDATE."""
        events = parse(
            [
                {
                    "start": "20260805T120000Z",
                    "end": "20260805T130000Z",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                    "exdate": "20260805T120000Z",
                }
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert utc(2026, 8, 5, 12) not in [event.start for event in events]

    def test_mixed_awareness_bounds_do_not_sink_the_run(self, quiet_terminal):
        """An aware DTSTART with a floating DTEND used to raise out of the whole merge."""
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:mixed@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260804T120000Z\r\nDTEND:20260804T130000\r\n"
            b"RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31, 23, 59))

        assert [event.start for event in events] == [utc(2026, 8, 4, 12)]

    def test_a_this_and_future_split_keeps_its_own_first_occurrence(self):
        """A VEVENT with both RECURRENCE-ID and RRULE is a master, not an override.

        Outlook writes these when "change this and all following" is used. Treating
        it as an override made it suppress its own DTSTART.
        """
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:split@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260811T140000Z\r\nDTEND:20260811T150000Z\r\n"
            b"RECURRENCE-ID;RANGE=THISANDFUTURE:20260811T140000Z\r\n"
            b"RRULE:FREQ=WEEKLY;BYDAY=TU\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31, 23, 59))

        assert utc(2026, 8, 11, 14) in [event.start for event in events]

    def test_rdate_adds_a_one_off_occurrence(self):
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:rdate@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260804T120000Z\r\nDTEND:20260804T130000Z\r\n"
            b"RRULE:FREQ=WEEKLY;COUNT=2\r\nRDATE:20260820T120000Z\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31, 23, 59))

        assert utc(2026, 8, 20, 12) in [event.start for event in events]

    def test_an_all_day_rdate_is_ignored(self):
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:rdate2@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260804T120000Z\r\nDTEND:20260804T130000Z\r\n"
            b"RRULE:FREQ=WEEKLY;COUNT=2\r\nRDATE;VALUE=DATE:20260820\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31, 23, 59))

        assert len(events) == 2

    def test_no_rdate_is_not_an_error(self):
        event = vevent()

        assert merge._additional_occurrences(event, datetime(2026, 8, 4, 12, tzinfo=UTC)) == []

    def test_expanded_occurrences_are_deduplicated(self):
        """Two series landing on the same slot collapse, as any other pair would."""
        events = parse(
            [
                {"uid": "a@test", "start": "20260804T120000Z", "end": "20260804T130000Z", "rrule": "FREQ=WEEKLY"},
                {"uid": "b@test", "start": "20260804T120000Z", "end": "20260804T130000Z", "rrule": "FREQ=WEEKLY"},
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 15),
        )

        assert len(events) == len({(event.start, event.end) for event in events})


RULE_SHAPES = [
    "FREQ=DAILY",
    "FREQ=DAILY;INTERVAL=3",
    "FREQ=WEEKLY",
    "FREQ=WEEKLY;INTERVAL=2",
    "FREQ=WEEKLY;BYDAY=TU",
    "FREQ=WEEKLY;BYDAY=MO,WE,FR",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH",
    "FREQ=MONTHLY",
    "FREQ=MONTHLY;BYMONTHDAY=15",
    "FREQ=MONTHLY;BYMONTHDAY=-1",
    "FREQ=MONTHLY;BYDAY=1TU",
    "FREQ=MONTHLY;BYDAY=-1FR",
    "FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=31",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
    "FREQ=YEARLY",
    "FREQ=YEARLY;BYMONTH=8;BYMONTHDAY=28",
    "FREQ=HOURLY;INTERVAL=6",
    "FREQ=MINUTELY;INTERVAL=90",
    "FREQ=DAILY;UNTIL=20261231T000000Z",
    "FREQ=DAILY;COUNT=5",
    "FREQ=WEEKLY;COUNT=500",
    "FREQ=DAILY;BYDAY=SA,SU",
]

# Days 29-31 are load-bearing: month and year arithmetic clamps a day the target month
# lacks, which moved every occurrence of a MONTHLY series and invented events that were
# never in the feed. The original set stopped at 29 and missed all of it.
ANCHOR_DATES = [
    (2020, 1, 1),
    (2023, 1, 1),
    (2023, 1, 31),
    (2019, 9, 30),
    (2021, 7, 31),
    (2024, 2, 29),
    (2025, 8, 28),
    (2026, 3, 15),
    (2026, 8, 29),
]

# Windows in months of differing length, so a clamped anchor has somewhere to land wrong.
WINDOWS = [
    (utc(2026, 8, 28, 9), utc(2026, 9, 10, 9)),
    (utc(2026, 10, 15, 9), utc(2026, 11, 5, 9)),
    (utc(2026, 11, 9, 9), utc(2026, 12, 19, 9)),
    (utc(2026, 2, 1, 9), utc(2026, 3, 5, 9)),
    (utc(2028, 2, 1, 9), utc(2028, 3, 5, 9)),
]


class TestAdvanceAnchor:
    """Skipping the anchor forward must not change a single occurrence.

    The optimisation only pays off because `rule.between()` iterates from DTSTART; the
    risk is that moving DTSTART moves the recurrence lattice, since BYDAY, BYMONTHDAY
    and BYSETPOS are evaluated relative to period boundaries. These assert equivalence
    against the unoptimised expansion rather than against expected dates, so a rule
    shape nobody anticipated is still covered.
    """

    @pytest.mark.parametrize("rule_text", RULE_SHAPES)
    @pytest.mark.parametrize("anchor_date", ANCHOR_DATES)
    @pytest.mark.parametrize("window", WINDOWS)
    def test_expansion_is_identical_to_the_unadvanced_rule(self, rule_text, anchor_date, window):
        year, month, day = anchor_date
        anchor = datetime(year, month, day, 9, 0, tzinfo=UTC)
        window_start, window_end = window

        naive = list(rrulestr(rule_text, dtstart=anchor).between(window_start, window_end, inc=True))
        advanced = list(
            rrulestr(rule_text, dtstart=merge._advance_anchor(rule_text, anchor, window_start)).between(
                window_start, window_end, inc=True
            )
        )

        assert advanced == naive

    @pytest.mark.parametrize("anchor_date", [(2023, 1, 31), (2019, 9, 30), (2024, 2, 29)])
    @pytest.mark.parametrize("rule_text", ["FREQ=MONTHLY", "FREQ=MONTHLY;INTERVAL=2", "FREQ=YEARLY"])
    def test_a_clampable_day_is_never_advanced(self, rule_text, anchor_date):
        """Month arithmetic clamps 31 to 30 or 28, moving the whole series.

        `FREQ=MONTHLY;INTERVAL=2` anchored 2021-07-31 went from contributing nothing to
        contributing 2026-11-30 -- inventing busy time, the failure this expansion is
        most careful to avoid.
        """
        year, month, day = anchor_date
        anchor = datetime(year, month, day, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor(rule_text, anchor, utc(2026, 11, 9, 9)) == anchor

    @pytest.mark.parametrize("interval", ["0", "-1", "not-a-number"])
    def test_a_non_positive_interval_is_refused_before_dateutil(self, interval):
        """dateutil spins forever on INTERVAL=0 instead of raising.

        An infinite loop is not catchable, so the broad handler in `_expand_recurrence`
        cannot save the run -- the whole scheduled merge hangs. It has to be refused
        before `rrulestr` is called.
        """
        assert merge._has_usable_interval(f"FREQ=DAILY;INTERVAL={interval}") is False

    @pytest.mark.parametrize("rule_text", ["FREQ=DAILY", "FREQ=DAILY;INTERVAL=1", "FREQ=WEEKLY;INTERVAL=3"])
    def test_a_usable_interval_is_accepted(self, rule_text):
        assert merge._has_usable_interval(rule_text) is True

    def test_a_zero_interval_contributes_the_master_rather_than_hanging(self, quiet_terminal):
        """The series still contributes its own occurrence; only the repeats are lost."""
        events = parse(
            [{"start": "20260901T090000Z", "end": "20260901T100000Z", "rrule": "FREQ=DAILY;INTERVAL=0"}],
            start=utc(2026, 8, 28),
            end=utc(2026, 9, 10),
        )

        assert [event.start for event in events] == [utc(2026, 9, 1, 9)]

    def test_a_clampable_day_is_still_advanced_for_fixed_units(self):
        """Only month and year arithmetic clamps; days and weeks are unaffected."""
        anchor = datetime(2023, 1, 31, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor("FREQ=DAILY", anchor, utc(2026, 8, 28, 9)) > anchor

    def test_a_trailing_semicolon_is_tolerated(self):
        """Some feeds emit `FREQ=WEEKLY;` -- the empty chunk must not become a key."""
        anchor = datetime(2023, 1, 1, 9, 0, tzinfo=UTC)

        advanced = merge._advance_anchor("FREQ=WEEKLY;", anchor, utc(2026, 8, 28, 9))

        assert advanced > anchor
        assert merge._rrule_parts("FREQ=WEEKLY;") == {"FREQ": "WEEKLY"}

    def test_a_count_limited_rule_is_never_advanced(self):
        """COUNT makes occurrences positional, so skipping ahead invents extra ones."""
        anchor = datetime(2023, 1, 1, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor("FREQ=DAILY;COUNT=5", anchor, utc(2026, 8, 28)) == anchor

    def test_an_unknown_frequency_is_never_advanced(self):
        anchor = datetime(2023, 1, 1, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor("FREQ=FORTNIGHTLY", anchor, utc(2026, 8, 28)) == anchor

    def test_a_missing_frequency_is_never_advanced(self):
        anchor = datetime(2023, 1, 1, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor("BYDAY=TU", anchor, utc(2026, 8, 28)) == anchor

    @pytest.mark.parametrize("interval", ["0", "-2", "many"])
    def test_a_nonsensical_interval_is_never_advanced(self, interval):
        """`INTERVAL=0` makes dateutil itself spin, so it must not be reasoned about."""
        anchor = datetime(2023, 1, 1, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor(f"FREQ=DAILY;INTERVAL={interval}", anchor, utc(2026, 8, 28)) == anchor

    def test_an_anchor_already_inside_the_window_is_untouched(self):
        anchor = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

        assert merge._advance_anchor("FREQ=DAILY", anchor, utc(2026, 8, 28)) == anchor

    def test_the_advanced_anchor_never_passes_the_window(self):
        """Advancing past the window start would skip occurrences inside it."""
        anchor = datetime(2023, 1, 1, 9, 0, tzinfo=UTC)
        window_start = utc(2026, 8, 28, 9)

        assert merge._advance_anchor("FREQ=WEEKLY;INTERVAL=2", anchor, window_start) <= window_start

    def test_it_actually_skips_ahead(self):
        """Otherwise the whole change is inert and the tests above prove nothing."""
        anchor = datetime(2020, 1, 1, 9, 0, tzinfo=UTC)

        advanced = merge._advance_anchor("FREQ=DAILY", anchor, utc(2026, 8, 28, 9))

        assert advanced > anchor + timedelta(days=2000)


class TestRecurrenceHelpers:
    def test_normalise_rejects_a_date(self):
        # An all-day VEVENT carries a date; this module does not sync those.
        assert merge._normalise_ics_datetime(date(2026, 8, 12)) is None

    def test_normalise_truncates_to_the_minute(self):
        moment = merge._normalise_ics_datetime(datetime(2026, 8, 12, 9, 30, 45, tzinfo=UTC))

        assert moment == utc(2026, 8, 12, 9, 30)

    def test_cancelled_occurrences_is_empty_without_exdate(self):
        assert merge._cancelled_occurrences(vevent()) == set()

    def test_expansion_needs_both_bounds(self):
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:nodtend@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260804T120000Z\r\nRRULE:FREQ=WEEKLY\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        event = next(iter(calendar.walk("VEVENT")))

        assert merge._expand_recurrence(event, "nodtend@test", set(), utc(2026, 8, 1), utc(2026, 8, 31)) is None

    def test_an_all_day_series_is_not_expanded(self):
        """All-day VEVENTs carry a date, and this module syncs only timed events."""
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:allday@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;VALUE=DATE:20260804\r\nDTEND;VALUE=DATE:20260805\r\n"
            b"RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        event = next(iter(calendar.walk("VEVENT")))

        assert merge._expand_recurrence(event, "allday@test", set(), utc(2026, 8, 1), utc(2026, 8, 31)) is None

    def test_an_all_day_exdate_is_ignored(self):
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:dateex@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260804T120000Z\r\nDTEND:20260804T130000Z\r\n"
            b"RRULE:FREQ=WEEKLY\r\nEXDATE;VALUE=DATE:20260811\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        event = next(iter(calendar.walk("VEVENT")))

        assert merge._cancelled_occurrences(event) == set()

    def test_an_all_day_recurrence_id_is_ignored(self):
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:series@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;VALUE=DATE:20260811\r\nDTEND;VALUE=DATE:20260812\r\n"
            b"RECURRENCE-ID;VALUE=DATE:20260811\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        assert merge._collect_recurrence_overrides(calendar) == set()

    def test_an_override_without_a_uid_is_not_collected(self):
        """An empty-string key would suppress that slot in every UID-less series."""
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n"
            b"BEGIN:VEVENT\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART:20260811T150000Z\r\nDTEND:20260811T160000Z\r\n"
            b"RECURRENCE-ID:20260811T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        assert merge._collect_recurrence_overrides(calendar) == set()

    def test_a_uid_less_override_does_not_suppress_another_series(self):
        events = parse(
            [
                {"uid": "", "start": "20260804T120000Z", "end": "20260804T130000Z", "rrule": "FREQ=WEEKLY;BYDAY=TU"},
                {
                    "uid": "",
                    "start": "20260811T150000Z",
                    "end": "20260811T160000Z",
                    "recurrence_id": "20260811T120000Z",
                },
            ],
            start=utc(2026, 8, 1),
            end=utc(2026, 8, 31, 23, 59),
        )

        assert utc(2026, 8, 11, 12) in [event.start for event in events]

    def test_overrides_ignore_events_without_a_recurrence_id(self):
        calendar = Calendar.from_ical(ics_bytes([{"start": "20260812T120000Z", "end": "20260812T130000Z"}]))

        assert merge._collect_recurrence_overrides(calendar) == set()


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

    def test_skips_free_events(self):
        events = parse([{"start": "20260812T120000Z", "end": "20260812T130000Z", "transp": "TRANSPARENT"}])

        assert events == []

    def test_google_feed_skips_self_blocked_time(self):
        """Regression for the vf work calendar.

        Google writes an explicit TRANSP only for lunch, focus time and out of
        office; real meetings have none. Treating OPAQUE as busy there synced
        1518 personal blocks that had always been excluded.
        """
        calendar = Calendar.from_ical(
            ics_bytes(
                [
                    {"start": "20260812T120000Z", "end": "20260812T130000Z", "summary": "Busy"},
                    {"start": "20260812T150000Z", "end": "20260812T160000Z", "summary": "lunch", "transp": "OPAQUE"},
                    {
                        "start": "20260812T170000Z",
                        "end": "20260812T180000Z",
                        "summary": "no meeting time",
                        "transp": "OPAQUE",
                    },
                ],
                prodid=PRODID_GOOGLE,
            )
        )

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert len(events) == 1
        assert events[0].start == utc(2026, 8, 12, 12, 0)

    def test_outlook_feed_keeps_busy_and_tentative_but_drops_ooo(self):
        calendar = Calendar.from_ical(
            ics_bytes(
                [
                    {"start": "20260812T120000Z", "end": "20260812T130000Z", "transp": "OPAQUE", "busy_status": "BUSY"},
                    {
                        "start": "20260813T120000Z",
                        "end": "20260813T130000Z",
                        "transp": "OPAQUE",
                        "busy_status": "TENTATIVE",
                    },
                    {"start": "20260814T120000Z", "end": "20260814T130000Z", "transp": "OPAQUE", "busy_status": "OOF"},
                    {
                        "start": "20260817T120000Z",
                        "end": "20260817T130000Z",
                        "transp": "TRANSPARENT",
                        "busy_status": "FREE",
                    },
                ],
                prodid=PRODID_OUTLOOK,
            )
        )

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert [event.start for event in events] == [utc(2026, 8, 12, 12), utc(2026, 8, 13, 12)]

    def test_keeps_busy_events_that_declare_transp(self):
        """Regression: Outlook stamps TRANSP:OPAQUE on every event.

        Treating the mere presence of TRANSP as "skip" silently dropped the
        entire Outlook feed.
        """
        events = parse([{"start": "20260812T120000Z", "end": "20260812T130000Z", "transp": "OPAQUE"}])

        assert len(events) == 1

    def test_keeps_events_without_transp(self):
        # RFC 5545 defaults a missing TRANSP to OPAQUE; Google omits it on busy
        # events, so they must still be imported.
        events = parse([{"start": "20260812T120000Z", "end": "20260812T130000Z"}])

        assert len(events) == 1

    def test_mixed_feed_keeps_only_busy_events(self):
        events = parse(
            [
                {"start": "20260812T120000Z", "end": "20260812T130000Z", "transp": "OPAQUE"},
                {"start": "20260813T120000Z", "end": "20260813T130000Z", "transp": "TRANSPARENT"},
                {"start": "20260814T120000Z", "end": "20260814T130000Z"},
            ]
        )

        assert [event.start for event in events] == [utc(2026, 8, 12, 12), utc(2026, 8, 14, 12)]

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

    def test_skip_day_is_evaluated_in_utc(self):
        """Skip days apply to the UTC weekday, not the event's local weekday.

        23:00 Friday in Buenos Aires is 02:00 Saturday UTC, so skipping Saturday
        must drop it even though it is locally a Friday event.
        """
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:tzskip@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;TZID=America/Argentina/Buenos_Aires:20260814T230000\r\n"
            b"DTEND;TZID=America/Argentina/Buenos_Aires:20260815T000000\r\n"
            b"SUMMARY:Late Friday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        kept = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))
        skipped = merge._parse_source_events(calendar, ["5"], utc(2026, 8, 1), utc(2026, 8, 31))

        assert len(kept) == 1
        assert kept[0].start == utc(2026, 8, 15, 2, 0)
        assert skipped == []

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

    def test_parses_outlook_shaped_event(self):
        """End-to-end guard for the Outlook feed shape.

        Outlook uses Windows timezone identifiers rather than IANA names and
        stamps TRANSP:OPAQUE on busy events. icalendar maps the Windows name,
        so the event must import and convert to UTC correctly.
        """
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:PUBLISH\r\n"
            b"PRODID:Microsoft Exchange Server 2010\r\n"
            b"BEGIN:VEVENT\r\nUID:outlook@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;TZID=Argentina Standard Time:20260812T090000\r\n"
            b"DTEND;TZID=Argentina Standard Time:20260812T100000\r\n"
            b"SUMMARY:Standup\r\nTRANSP:OPAQUE\r\n"
            b"X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        events = merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31))

        assert len(events) == 1
        # 09:00 Argentina (UTC-3) is 12:00 UTC.
        assert events[0].start == utc(2026, 8, 12, 12, 0)
        assert events[0].end == utc(2026, 8, 12, 13, 0)

    def test_skips_outlook_free_event(self):
        raw = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:PUBLISH\r\n"
            b"PRODID:Microsoft Exchange Server 2010\r\n"
            b"BEGIN:VEVENT\r\nUID:outlook-free@test\r\nDTSTAMP:20260812T000000Z\r\n"
            b"DTSTART;TZID=Argentina Standard Time:20260812T090000\r\n"
            b"DTEND;TZID=Argentina Standard Time:20260812T100000\r\n"
            b"SUMMARY:Focus time\r\nTRANSP:TRANSPARENT\r\n"
            b"X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(raw)

        assert merge._parse_source_events(calendar, [], utc(2026, 8, 1), utc(2026, 8, 31)) == []

    def test_drops_seconds_from_source_times(self):
        events = parse([{"start": "20260812T120045Z", "end": "20260812T130059Z"}])

        assert events[0].start == utc(2026, 8, 12, 12, 0)
        assert events[0].end == utc(2026, 8, 12, 13, 0)

    def test_two_events_in_the_same_slot_yield_one(self):
        """Reported case: two meetings 13:00-14:00 must sync as a single block."""
        events = parse(
            [
                {"start": "20260813T130000Z", "end": "20260813T140000Z", "summary": "Standup"},
                {"start": "20260813T130000Z", "end": "20260813T140000Z", "summary": "Other meeting"},
            ]
        )

        assert len(events) == 1
        assert events[0].start == utc(2026, 8, 13, 13)
        assert events[0].end == utc(2026, 8, 13, 14)

    def test_overlapping_but_not_identical_slots_both_survive(self):
        events = parse(
            [
                {"start": "20260813T130000Z", "end": "20260813T140000Z"},
                {"start": "20260813T133000Z", "end": "20260813T143000Z"},
            ]
        )

        assert len(events) == 2

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


class ServerError(Exception):
    """A non-404 API failure: fatal, unlike an already-deleted event."""

    code = 500


class TestSyncEventsToIcloud:
    def test_adds_events_marked_add(self):
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert len(service.added) == 1
        added = service.added[0]
        assert added.pguid == "cal-guid"
        assert added.title == "[W] Work/Google"

    def test_add_converts_times_to_calendar_timezone(self):
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

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

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert len(service.removed) == 1
        assert service.removed[0].guid == "g-1"
        assert service.removed[0].pguid == "p-1"

    def test_ignores_events_marked_none(self):
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.none)]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

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

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert len(service.added) == 1
        assert len(service.removed) == 1

    def test_add_failure_raises_runtime_error(self, quiet_terminal):
        service = FakeCalendarService(add_error=ConnectionError("boom"))
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        with pytest.raises(RuntimeError, match="Unable to add event"):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert "<failed>" in quiet_terminal

    def test_add_failure_preserves_cause(self):
        original = ConnectionError("boom")
        service = FakeCalendarService(add_error=original)
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add)]

        with pytest.raises(RuntimeError) as excinfo:
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

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
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

    def test_delete_treats_404_as_already_gone(self, quiet_terminal):
        """A 404 means the event is already absent, which is the goal of a delete.

        Reproduces 2026-08-20: an event removed elsewhere between the iCloud load
        and the delete aborted the entire run.
        """

        class NotFound(Exception):
            code = merge.HTTP_NOT_FOUND

        raw = icloud_raw_event()
        service = FakeCalendarService(remove_error=NotFound("Not Found"))
        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=raw,
            )
        ]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert any("was already gone" in line for line in quiet_terminal)

    def test_delete_continues_with_later_events_after_a_404(self, quiet_terminal):
        """The rest of the run must survive: main() only catches YamlError."""

        class NotFound(Exception):
            code = merge.HTTP_NOT_FOUND

        service = FakeCalendarService(remove_error=NotFound("Not Found"))
        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            ),
            merge_event(utc(2026, 8, 12, 14), utc(2026, 8, 12, 15), action=merge.EventAction.add, title="[T] later"),
        ]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert len(service.added) == 1

    def test_delete_still_raises_for_other_status_codes(self, quiet_terminal):
        """Only 404 is benign; a 500 must still stop the run."""

        class ServerError(Exception):
            code = 500

        service = FakeCalendarService(remove_error=ServerError("Server Error"))
        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            )
        ]

        with pytest.raises(RuntimeError, match="Unable to delete event"):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

    def test_delete_reads_404_from_an_attached_response(self, quiet_terminal):
        """Apple answers a 404 without JSON, so it arrives as requests.HTTPError."""

        class Response:
            status_code = merge.HTTP_NOT_FOUND

        class HttpError(Exception):
            response = Response()

        service = FakeCalendarService(remove_error=HttpError("404 Client Error"))
        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            )
        ]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert any("was already gone" in line for line in quiet_terminal)

    def test_reports_what_it_changed(self):
        """The sync used to be the only silent step, so a run's effect was unobservable."""
        service = FakeCalendarService()
        events = [
            merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add, title="[T] a"),
            merge_event(utc(2026, 8, 12, 14), utc(2026, 8, 12, 15), action=merge.EventAction.add, title="[T] b"),
            merge_event(
                utc(2026, 8, 12, 16),
                utc(2026, 8, 12, 17),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            ),
            merge_event(utc(2026, 8, 12, 18), utc(2026, 8, 12, 19), action=merge.EventAction.none),
        ]

        outcome = merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert (outcome.added, outcome.deleted, outcome.already_gone) == (2, 1, 0)

    def test_counts_match_what_the_service_saw(self):
        service = FakeCalendarService()
        events = [
            merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add, title="[T] a"),
            merge_event(
                utc(2026, 8, 12, 16),
                utc(2026, 8, 12, 17),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            ),
        ]

        outcome = merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert (outcome.added, outcome.deleted) == (len(service.added), len(service.removed))

    def test_an_already_gone_event_is_not_counted_as_deleted(self, quiet_terminal):
        """We did not delete it -- reporting otherwise would overstate the run's effect."""

        class NotFound(Exception):
            code = merge.HTTP_NOT_FOUND

        service = FakeCalendarService(remove_error=NotFound("Not Found"))
        events = [
            merge_event(
                utc(2026, 8, 12, 16),
                utc(2026, 8, 12, 17),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            )
        ]

        outcome = merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert (outcome.deleted, outcome.already_gone) == (0, 1)

    def test_the_summary_comes_after_the_step_is_closed(self, quiet_terminal):
        """Ordering is load-bearing, not cosmetic.

        The caller opens an unterminated "synchronizing..." line. Anything printed
        before `print_done()` closes it is glued onto that line, leaving "done!"
        orphaned on the next -- on every source, on every run.
        """
        service = FakeCalendarService()
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add, title="[T] a")]

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        summary = next(index for index, line in enumerate(quiet_terminal) if "1 added" in line)
        closed = next(index for index, line in enumerate(quiet_terminal) if line == "<done>")
        assert closed < summary

    def test_a_failure_closes_the_step_before_the_summary(self, quiet_terminal):
        """The failure path closes the line with print_failed instead."""
        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add, title="[T] a")]

        class AlwaysFails(FakeCalendarService):
            def add_event(self, event):
                raise ServerError("Server Error")

        service = AlwaysFails()

        with pytest.raises(RuntimeError):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        summary = next(index for index, line in enumerate(quiet_terminal) if "0 added" in line)
        closed = next(index for index, line in enumerate(quiet_terminal) if line == "<failed>")
        assert closed < summary
        assert "<done>" not in quiet_terminal

    def test_a_partial_sync_still_reports_what_it_managed(self, quiet_terminal):
        """The case the report matters most for, and the one it used to skip.

        A run that mutates the calendar and then fails leaves work the calendar
        alone cannot explain. Reporting only the failure hides it.
        """

        class PartialFailure(FakeCalendarService):
            def add_event(self, event):
                if len(self.added) >= 2:
                    raise ServerError("Server Error")
                return super().add_event(event)

        service = PartialFailure()
        events = [
            merge_event(
                utc(2026, 8, 12, 10 + index),
                utc(2026, 8, 12, 11 + index),
                action=merge.EventAction.add,
                title=f"[T] {index}",
            )
            for index in range(4)
        ]

        with pytest.raises(RuntimeError, match="Unable to add event"):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert any("[T] source: 2 added, 0 deleted" in line for line in quiet_terminal)

    def test_a_failed_add_is_not_counted(self, quiet_terminal):
        """The tally means 'changed', not 'attempted'.

        Only observable because the report now survives the abort -- counting order
        was untestable while a failure skipped the line entirely.
        """

        class AlwaysFails(FakeCalendarService):
            def add_event(self, event):
                raise ServerError("Server Error")

        events = [merge_event(utc(2026, 8, 12, 12), utc(2026, 8, 12, 13), action=merge.EventAction.add, title="[T] a")]

        service = AlwaysFails()

        with pytest.raises(RuntimeError):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert any("[T] source: 0 added, 0 deleted" in line for line in quiet_terminal)

    def test_a_failed_delete_is_not_counted(self, quiet_terminal):
        class AlwaysFails(FakeCalendarService):
            def remove_event(self, event):
                raise ServerError("Server Error")

        events = [
            merge_event(
                utc(2026, 8, 12, 12),
                utc(2026, 8, 12, 13),
                action=merge.EventAction.delete,
                full_event=icloud_raw_event(),
            )
        ]

        service = AlwaysFails()

        with pytest.raises(RuntimeError):
            merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert any("[T] source: 0 added, 0 deleted" in line for line in quiet_terminal)

    def test_an_empty_sync_reports_zeroes(self):
        outcome = merge._sync_events_to_icloud(FakeCalendarService(), "cal-guid", CAL_TZ, [], "[T] source")

        assert (outcome.added, outcome.deleted, outcome.already_gone) == (0, 0, 0)

    def test_empty_list_is_a_noop(self):
        service = FakeCalendarService()

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, [], "[T] source")

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

        merge._sync_events_to_icloud(service, "cal-guid", CAL_TZ, events, "[T] source")

        assert len(service.added) == 1
        assert service.removed == []
