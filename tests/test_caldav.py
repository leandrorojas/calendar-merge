"""Tests for the CalDAV backend.

The adapter exists because Apple restricted password sign-in: pyicloud speaks the web
API, which accepts only a real account password, while CalDAV accepts an app-specific
one. Its whole job is to present that protocol through the five operations the rest of
the module already speaks, so most of these assert the *shape* it hands back rather than
CalDAV behaviour -- a field renamed here is a silent no-op sync everywhere else.
"""

import re
from datetime import UTC, datetime

import pytest
from icalendar import Calendar as ICalendar
from pyicloud.services.calendar import EventObject

import merge


def ics_event(summary="standup", start="20260901T130000Z", end="20260901T133000Z", all_day=False):
    """A one-VEVENT calendar, as caldav hands back from a search."""
    if all_day:
        start, end = "20260901", "20260902"
        stamp = f"DTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}"
    else:
        stamp = f"DTSTART:{start}\r\nDTEND:{end}"
    return ICalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:x@test\r\nDTSTAMP:20260901T120000Z\r\n{stamp}\r\nSUMMARY:{summary}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class FakeFound:
    """One search hit: a URL and a parsed calendar."""

    def __init__(self, url, calendar=None, raises=False):
        self.url = url
        self._calendar = calendar if calendar is not None else ics_event()
        self._raises = raises

    @property
    def icalendar_instance(self):
        if self._raises:
            raise ValueError("unparseable")
        return self._calendar


class FakeDavCalendar:
    def __init__(
        self, url, name="Familia", found=(), name_raises=False, components=("VEVENT",), components_raises=False
    ):
        self.url = url
        self._name = name
        self._found = list(found)
        self._name_raises = name_raises
        self._components = list(components)
        self._components_raises = components_raises
        self.saved: list[str] = []
        self.searched: dict | None = None

    def get_supported_components(self):
        if self._components_raises:
            raise RuntimeError("server does not publish supported-calendar-component-set")
        return self._components

    def get_display_name(self):
        if self._name_raises:
            raise RuntimeError("no displayname property")
        return self._name

    def search(self, **kwargs):
        self.searched = kwargs
        return self._found

    def save_event(self, ics):
        self.saved.append(ics)


class FakeDavClient:
    def __init__(self, calendars=(), principal_raises=False):
        self._calendars = list(calendars)
        self._principal_raises = principal_raises
        self.requested_urls: list[str] = []

    def principal(self):
        if self._principal_raises:
            raise RuntimeError("401 Unauthorized")
        return self

    def calendars(self):
        return self._calendars

    def calendar(self, **kwargs):
        # **kwargs to match caldav's own signature, which the DavClientLike protocol
        # mirrors -- a positional `url` here would not satisfy it.
        url = kwargs["url"]
        self.requested_urls.append(url)
        return next((c for c in self._calendars if c.url == url), self._calendars[0])


class TestCalDavCalendarService:
    def test_get_calendars_uses_the_url_as_the_guid(self):
        """CalDAV addresses a collection by URL; there is no Apple GUID to report.

        The caller only ever hands the value back, so the URL is the identifier.
        """
        client = FakeDavClient([FakeDavCalendar("https://x/fam/", "Familia")])
        service = merge.CalDavCalendarService(client)

        assert service.get_calendars() == [{"guid": "https://x/fam/", "title": "Familia"}]

    def test_get_events_reports_pyicloud_field_shape(self):
        client = FakeDavClient([FakeDavCalendar("https://x/fam/", found=[FakeFound("https://x/fam/1.ics")])])

        (event,) = merge.CalDavCalendarService(client).get_events()

        assert event == {
            "startDate": [0, 2026, 9, 1, 13, 0],
            "endDate": [0, 2026, 9, 1, 13, 30],
            "title": "standup",
            "tz": "UTC",
            "allDay": False,
            "guid": "https://x/fam/1.ics",
            "pGuid": "https://x/fam/",
        }

    def test_get_events_passes_the_window_through(self):
        calendar = FakeDavCalendar("https://x/fam/")
        start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC)

        merge.CalDavCalendarService(FakeDavClient([calendar])).get_events(from_dt=start, to_dt=end)

        assert calendar.searched is not None, "the calendar must have been searched"
        assert calendar.searched["start"] == start
        assert calendar.searched["end"] == end
        assert calendar.searched["expand"] is True, "recurrences must arrive already expanded"

    def test_get_events_spans_every_collection(self):
        client = FakeDavClient(
            [
                FakeDavCalendar("https://x/a/", found=[FakeFound("https://x/a/1.ics")]),
                FakeDavCalendar("https://x/b/", found=[FakeFound("https://x/b/1.ics")]),
            ]
        )

        events = merge.CalDavCalendarService(client).get_events()

        assert {e["pGuid"] for e in events} == {"https://x/a/", "https://x/b/"}

    def test_all_day_events_are_reported_as_such(self):
        """The merge handles timed events only; the caller filters on this flag."""
        found = FakeFound("https://x/fam/1.ics", ics_event(all_day=True))
        client = FakeDavClient([FakeDavCalendar("https://x/fam/", found=[found])])

        assert merge.CalDavCalendarService(client).get_events() == [{"allDay": True}]

    def test_an_unparseable_event_is_skipped_not_fatal(self):
        """One bad event must not cost the whole sync."""
        client = FakeDavClient(
            [
                FakeDavCalendar(
                    "https://x/fam/",
                    found=[FakeFound("https://x/fam/bad.ics", raises=True), FakeFound("https://x/fam/1.ics")],
                )
            ]
        )

        events = merge.CalDavCalendarService(client).get_events()

        assert [e["guid"] for e in events] == ["https://x/fam/1.ics"]

    def test_an_event_without_an_end_is_skipped(self):
        """A VEVENT may carry DURATION instead of DTEND; neither shape is syncable here."""
        no_end = ICalendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n"
            "UID:x@test\r\nDTSTAMP:20260901T120000Z\r\nDTSTART:20260901T130000Z\r\n"
            "DURATION:PT30M\r\nSUMMARY:standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        client = FakeDavClient([FakeDavCalendar("https://x/fam/", found=[FakeFound("https://x/f/1.ics", no_end)])])

        assert merge.CalDavCalendarService(client).get_events() == []

    def test_add_event_writes_to_the_collection_named_by_pguid(self):
        calendar = FakeDavCalendar("https://x/fam/")
        service = merge.CalDavCalendarService(FakeDavClient([calendar]))

        service.add_event(
            EventObject(
                pguid="https://x/fam/",
                title="standup",
                start_date=datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
                end_date=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
            )
        )

        assert calendar.saved and "SUMMARY:standup" in calendar.saved[0]

    def test_remove_event_deletes_by_url(self, monkeypatch):
        deleted = []

        class FakeEvent:
            def __init__(self, client, url):
                self.url = url

            def delete(self):
                deleted.append(self.url)

        monkeypatch.setattr(merge, "CalDavEvent", FakeEvent)
        service = merge.CalDavCalendarService(FakeDavClient([FakeDavCalendar("https://x/fam/")]))

        service.remove_event(EventObject(pguid="https://x/fam/", guid="https://x/fam/1.ics", title="standup"))

        assert deleted == ["https://x/fam/1.ics"]


class TestEventCalendarFilter:
    """Apple serves reminder lists from the same endpoint as calendars.

    Measured on the live account: six collections come back, of which two advertise
    VTODO -- a `Reminders` list and a `Familia` list sitting beside the real `Familia`
    calendar. Only a trailing warning sign in its display name keeps the reminders list
    from colliding with the calendar the merge is configured to write to.
    """

    def test_reminder_lists_are_not_offered_as_calendars(self):
        client = FakeDavClient(
            [
                FakeDavCalendar("https://x/fam/", "Familia", components=["VEVENT"]),
                FakeDavCalendar("https://x/todo/", "Familia ⚠️", components=["VTODO"]),
            ]
        )

        assert merge.CalDavCalendarService(client).get_calendars() == [{"guid": "https://x/fam/", "title": "Familia"}]

    def test_reminder_lists_are_not_searched_for_events(self):
        """Searching a task list costs a round trip and can only return nothing."""
        todo = FakeDavCalendar("https://x/todo/", "Reminders ⚠️", components=["VTODO"])
        client = FakeDavClient([FakeDavCalendar("https://x/fam/"), todo])

        merge.CalDavCalendarService(client).get_events()

        assert todo.searched is None, "a VTODO collection must never be searched"

    def test_a_collection_that_cannot_report_components_is_kept(self):
        """Fail open: Apple publishes the property, so this is for a server that does not.

        Dropping such a collection would hide every calendar it has, which is worse than
        searching one too many.
        """
        client = FakeDavClient([FakeDavCalendar("https://x/fam/", components_raises=True)])

        assert len(merge.CalDavCalendarService(client).get_calendars()) == 1


class TestCalDavCalendarName:
    def test_prefers_the_display_name(self):
        assert merge._caldav_calendar_name(FakeDavCalendar("https://x/fam/", "Familia")) == "Familia"

    def test_falls_back_when_the_property_is_missing(self):
        """Not every collection publishes a displayname; the URL always identifies it."""
        calendar = FakeDavCalendar("https://x/fam/", name_raises=True)

        assert merge._caldav_calendar_name(calendar) == "https://x/fam/"


class TestAppleDatetimeParts:
    def test_converts_to_utc_before_splitting(self):
        from zoneinfo import ZoneInfo

        moment = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))

        assert merge._apple_datetime_parts(moment) == [0, 2026, 9, 1, 14, 0]

    def test_leaves_a_naive_datetime_alone(self):
        assert merge._apple_datetime_parts(datetime(2026, 9, 1, 10, 0)) == [0, 2026, 9, 1, 10, 0]


class TestBuildIcsEvent:
    def test_renders_a_parseable_vevent_in_utc(self):
        ics = merge._build_ics_event(
            EventObject(
                pguid="https://x/fam/",
                title="standup",
                start_date=datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
                end_date=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
            )
        )

        (event,) = ICalendar.from_ical(ics).walk("VEVENT")
        assert event.decoded("DTSTART") == datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
        assert event.decoded("DTEND") == datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
        assert str(event["SUMMARY"]) == "standup"

    def test_every_event_gets_its_own_uid(self):
        args = dict(
            pguid="https://x/fam/",
            title="standup",
            start_date=datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
            end_date=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        )
        first = merge._build_ics_event(EventObject(**args))
        second = merge._build_ics_event(EventObject(**args))

        uid = lambda ics: str(ICalendar.from_ical(ics).walk("VEVENT")[0]["UID"])  # noqa: E731
        assert uid(first) != uid(second), "a shared UID would make the second event replace the first"


class TestSelectDestinationCalendar:
    """Which calendar the merge writes to must be a decision, not an accident."""

    def test_selects_by_configured_title(self):
        calendars = [
            {"guid": "https://x/work/", "title": "Work"},
            {"guid": "https://x/fam/", "title": "Familia"},
        ]

        assert merge._select_destination_calendar(calendars, "Familia") == "https://x/fam/"

    def test_names_what_is_available_when_the_destination_is_missing(self):
        """The error has to be actionable: the likely cause is a typo or a renamed calendar."""
        calendars = [{"guid": "https://x/work/", "title": "Work"}]

        with pytest.raises(RuntimeError, match=re.escape("not found. Available: Work")):
            merge._select_destination_calendar(calendars, "Familia")

    def test_unset_keeps_the_previous_first_calendar_behaviour(self):
        """Upgrading without setting it must not break an existing deployment."""
        calendars = [{"guid": "https://x/work/", "title": "Work"}, {"guid": "https://x/fam/", "title": "Familia"}]

        assert merge._select_destination_calendar(calendars, None) == "https://x/work/"

    def test_raises_when_nothing_has_a_guid(self):
        with pytest.raises(RuntimeError, match="No calendar GUID available"):
            merge._select_destination_calendar([{"title": "Work"}], None)

    def test_skips_a_matching_calendar_that_has_no_guid(self):
        calendars = [{"title": "Familia"}, {"guid": "https://x/fam2/", "title": "Familia"}]

        assert merge._select_destination_calendar(calendars, "Familia") == "https://x/fam2/"


class TestAuthenticateBackend:
    """An app-specific password selects CalDAV; without one nothing changes."""

    def test_an_app_password_selects_caldav(self, monkeypatch):
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "abcd-efgh-ijkl-mnop")
        monkeypatch.setenv(merge.ENV_ICLOUD_USER, "someone@example.com")
        seen: dict[str, str] = {}
        monkeypatch.setattr(merge, "_authenticate_caldav", lambda user, pw: seen.update(user=user, pw=pw) or "caldav")
        monkeypatch.setattr(merge, "_authenticate_icloud", lambda: pytest.fail("pyicloud must not be reached"))

        assert merge._authenticate_backend() == "caldav"
        assert seen == {"user": "someone@example.com", "pw": "abcd-efgh-ijkl-mnop"}

    def test_without_one_the_pyicloud_path_still_runs(self, monkeypatch):
        monkeypatch.delenv(merge.ENV_ICLOUD_APP_PASSWORD, raising=False)
        monkeypatch.setattr(merge, "_authenticate_caldav", lambda *a: pytest.fail("CalDAV must not be reached"))
        monkeypatch.setattr(merge, "_authenticate_icloud", lambda: "pyicloud")

        assert merge._authenticate_backend() == "pyicloud"

    def test_an_empty_app_password_does_not_select_caldav(self, monkeypatch):
        """An unset variable and a blank one mean the same thing to a shell."""
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "")
        monkeypatch.setattr(merge, "_authenticate_caldav", lambda *a: pytest.fail("CalDAV must not be reached"))
        monkeypatch.setattr(merge, "_authenticate_icloud", lambda: "pyicloud")

        assert merge._authenticate_backend() == "pyicloud"


class TestAuthenticateCalDav:
    def test_returns_a_service_exposing_the_calendar_attribute(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "DAVClient", lambda **kw: FakeDavClient([FakeDavCalendar("https://x/fam/")]))

        service = merge._authenticate_caldav("someone@example.com", "abcd-efgh")

        assert isinstance(service.calendar, merge.CalDavCalendarService)

    def test_passes_the_credentials_to_the_client(self, monkeypatch, quiet_terminal):
        seen: dict[str, str] = {}

        def client(**kwargs):
            seen.update(kwargs)
            return FakeDavClient([FakeDavCalendar("https://x/fam/")])

        monkeypatch.setattr(merge, "DAVClient", client)
        merge._authenticate_caldav("someone@example.com", "abcd-efgh")

        assert seen == {"url": merge.CALDAV_URL, "username": "someone@example.com", "password": "abcd-efgh"}

    def test_a_rejected_password_is_wrapped_with_its_cause(self, monkeypatch, quiet_terminal):
        """The alert must say the CalDAV session failed and why, not just that something did."""
        monkeypatch.setattr(merge, "DAVClient", lambda **kw: FakeDavClient(principal_raises=True))

        with pytest.raises(RuntimeError, match="Unable to start iCloud CalDAV service") as caught:
            merge._authenticate_caldav("someone@example.com", "wrong")

        assert "401 Unauthorized" in merge._describe_error(caught.value)
