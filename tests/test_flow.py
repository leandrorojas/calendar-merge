"""Tests for the top-level flow helpers and main()."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pyfangs.yaml import YamlError
from pyfangs.yaml import YamlHelper as RealYamlHelper

import merge
from tests.conftest import (
    BA_TZ,
    FakeCalendarService,
    FakeFileSystem,
    FakeYamlHelper,
    fake_api,
    icloud_raw_event,
    ics_bytes,
    utc,
)

GENERAL = merge.YAML_SECTION_GENERAL
SOURCE_0 = merge.YAML_SECTION_SOURCE_CALENDAR.format(index=0)
SOURCE_1 = merge.YAML_SECTION_SOURCE_CALENDAR.format(index=1)


def config_values(future_days=5, skip_days="5, 6", extra=None):
    values = {
        (GENERAL, merge.YAML_SETTING_FUTURE_EVENTS_DAYS): future_days,
        (GENERAL, merge.YAML_SETTING_SKIP_DAYS): skip_days,
    }
    values.update(extra or {})
    return values


def source_values(index=0, source="Work", tag="W", title="Google", tz=BA_TZ):
    section = merge.YAML_SECTION_SOURCE_CALENDAR.format(index=index)
    return {
        (section, merge.YAML_SETTING_CALENDAR_SOURCE): source,
        (section, merge.YAML_SETTING_CALENDAR_TAG): tag,
        (section, merge.YAML_SETTING_CALENDAR_TITLE): title,
        (section, merge.YAML_SETTING_CALENDAR_TZ): tz,
    }


# --- _load_config ---


class TestLoadConfig:
    def test_returns_parsed_configuration(self, monkeypatch, tmp_path):
        monkeypatch.setattr(merge, "load_dotenv", lambda: None)
        monkeypatch.setattr(merge, "FileSystem", lambda: FakeFileSystem(tmp_path))
        monkeypatch.setattr(merge, "YamlHelper", lambda path: FakeYamlHelper(config_values()))

        yaml_helper, future_days, skip_days, fs = merge._load_config()

        assert future_days == 5
        assert skip_days == ["5", "6"]
        assert isinstance(yaml_helper, FakeYamlHelper)
        assert isinstance(fs, FakeFileSystem)

    def test_coerces_future_days_to_int(self, monkeypatch, tmp_path):
        monkeypatch.setattr(merge, "load_dotenv", lambda: None)
        monkeypatch.setattr(merge, "FileSystem", lambda: FakeFileSystem(tmp_path))
        monkeypatch.setattr(merge, "YamlHelper", lambda path: FakeYamlHelper(config_values(future_days="7")))

        _, future_days, _, _ = merge._load_config()

        assert future_days == 7

    def test_raises_when_yaml_cannot_be_opened(self, monkeypatch, tmp_path, quiet_terminal):
        monkeypatch.setattr(merge, "load_dotenv", lambda: None)
        monkeypatch.setattr(merge, "FileSystem", lambda: FakeFileSystem(tmp_path))

        def boom(path):
            raise OSError("no such file")

        monkeypatch.setattr(merge, "YamlHelper", boom)

        with pytest.raises(RuntimeError, match="Unable to open YAML configuration"):
            merge._load_config()

    def test_raises_on_invalid_future_days(self, monkeypatch, tmp_path, quiet_terminal):
        monkeypatch.setattr(merge, "load_dotenv", lambda: None)
        monkeypatch.setattr(merge, "FileSystem", lambda: FakeFileSystem(tmp_path))
        monkeypatch.setattr(merge, "YamlHelper", lambda path: FakeYamlHelper(config_values(future_days="not-a-number")))

        with pytest.raises(RuntimeError, match="Invalid future event days"):
            merge._load_config()

    def test_raises_when_skip_days_missing(self, monkeypatch, tmp_path, quiet_terminal):
        monkeypatch.setattr(merge, "load_dotenv", lambda: None)
        monkeypatch.setattr(merge, "FileSystem", lambda: FakeFileSystem(tmp_path))
        values = {(GENERAL, merge.YAML_SETTING_FUTURE_EVENTS_DAYS): 5}
        monkeypatch.setattr(merge, "YamlHelper", lambda path: FakeYamlHelper(values))

        with pytest.raises(RuntimeError, match="Unable to load skip days"):
            merge._load_config()


# --- _authenticate_icloud ---


class TestAuthenticateIcloud:
    def test_returns_service_on_success(self, monkeypatch):
        api = fake_api()
        monkeypatch.setattr(merge, "PyiCloudService", lambda user, password: api)
        monkeypatch.setattr(merge, "validate_2fa", lambda service: True)

        assert merge._authenticate_icloud() is api

    def test_passes_credentials_from_env(self, monkeypatch):
        seen: dict[str, object] = {}
        monkeypatch.setenv(merge.ENV_ICLOUD_USER, "user@example.com")
        monkeypatch.setenv(merge.ENV_ICLOUD_PASS, "secret")

        def capture(user, password):
            seen.update(user=user, password=password)
            return fake_api()

        monkeypatch.setattr(merge, "PyiCloudService", capture)
        monkeypatch.setattr(merge, "validate_2fa", lambda service: True)

        merge._authenticate_icloud()

        assert seen == {"user": "user@example.com", "password": "secret"}

    def test_raises_when_service_construction_fails(self, monkeypatch, quiet_terminal):
        def boom(user, password):
            raise ConnectionError("offline")

        monkeypatch.setattr(merge, "PyiCloudService", boom)

        with pytest.raises(RuntimeError, match="Unable to start iCloud service"):
            merge._authenticate_icloud()

    def test_raises_when_2fa_returns_false(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "PyiCloudService", lambda user, password: fake_api())
        monkeypatch.setattr(merge, "validate_2fa", lambda service: False)

        with pytest.raises(RuntimeError, match="2FA validation failed"):
            merge._authenticate_icloud()

    def test_wraps_unexpected_2fa_error(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "PyiCloudService", lambda user, password: fake_api())

        def boom(service):
            raise ValueError("weird")

        monkeypatch.setattr(merge, "validate_2fa", boom)

        with pytest.raises(RuntimeError, match="2FA validation error"):
            merge._authenticate_icloud()

    def test_does_not_rewrap_runtime_error(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "PyiCloudService", lambda user, password: fake_api())

        def boom(service):
            raise RuntimeError("original message")

        monkeypatch.setattr(merge, "validate_2fa", boom)

        with pytest.raises(RuntimeError, match="original message"):
            merge._authenticate_icloud()


# --- _load_icloud_events ---


def icloud_service(calendar_service):
    return SimpleNamespace(calendar=calendar_service)


class TestLoadIcloudEvents:
    def test_returns_calendar_guid_and_events(self):
        service = FakeCalendarService(calendars=[{"guid": "abc-123"}], events=[icloud_raw_event()])

        calendar_service, guid, events, _today_bod, _cut_off, _now = merge._load_icloud_events(
            icloud_service(service), future_event_days=5, skip_days=[]
        )

        assert calendar_service is service
        assert guid == "abc-123"
        assert len(events) == 1

    def test_picks_first_calendar_with_a_guid(self):
        service = FakeCalendarService(calendars=[{}, {"guid": None}, {"guid": "real-guid"}])

        _, guid, _, _, _, _ = merge._load_icloud_events(icloud_service(service), 5, [])

        assert guid == "real-guid"

    def test_raises_when_no_guid_available(self, quiet_terminal):
        service = icloud_service(FakeCalendarService(calendars=[{}, {"guid": None}]))

        with pytest.raises(RuntimeError, match="No calendar GUID available"):
            merge._load_icloud_events(service, 5, [])

    def test_raises_when_calendars_cannot_be_fetched(self, quiet_terminal):
        class Broken(FakeCalendarService):
            def get_calendars(self):
                raise ConnectionError("offline")

        service = icloud_service(Broken())

        with pytest.raises(RuntimeError, match="Unable to fetch calendars"):
            merge._load_icloud_events(service, 5, [])

    def test_raises_when_events_cannot_be_loaded(self, quiet_terminal):
        class Broken(FakeCalendarService):
            def get_events(self, from_dt=None, to_dt=None):
                raise ConnectionError("offline")

        service = icloud_service(Broken())

        with pytest.raises(RuntimeError, match="Unable to load events from iCloud"):
            merge._load_icloud_events(service, 5, [])

    def test_today_bod_is_midnight_and_tz_aware(self):
        service = FakeCalendarService()

        _, _, _, today_bod, _cut_off, now = merge._load_icloud_events(icloud_service(service), 5, [])

        assert (today_bod.hour, today_bod.minute, today_bod.second) == (0, 0, 0)
        assert today_bod.tzinfo is not None
        assert now.tzinfo is not None

    def test_cut_off_is_end_of_day_after_today(self):
        service = FakeCalendarService()

        _, _, _, today_bod, cut_off, _ = merge._load_icloud_events(icloud_service(service), 5, [])

        assert (cut_off.hour, cut_off.minute, cut_off.second) == (23, 59, 59)
        assert cut_off > today_bod

    def test_collects_events_regardless_of_skip_days(self):
        """Loading no longer applies the weekday filter.

        skip_days is per source, so the global value must not prune the shared
        iCloud event list -- a source that does not skip Saturday still needs to
        see its Saturday events in order to reconcile them.
        """
        saturday = icloud_raw_event(start=(0, 2026, 8, 15, 9, 0), end=(0, 2026, 8, 15, 10, 0))
        service = FakeCalendarService(events=[saturday])

        _, _, events, _, _, _ = merge._load_icloud_events(icloud_service(service), 5, ["5"])

        assert len(events) == 1

    def test_global_skip_days_still_shape_the_window(self):
        # future_events_days stays global and counts only non-skipped days, so
        # skipping the weekend stretches a 5-day window past 5 calendar days.
        service = FakeCalendarService()

        _, _, _, today_bod, cut_off_skipping, _ = merge._load_icloud_events(icloud_service(service), 5, ["5", "6"])
        _, _, _, _, cut_off_plain, _ = merge._load_icloud_events(icloud_service(service), 5, [])

        assert cut_off_skipping > cut_off_plain
        assert (cut_off_plain - today_bod).days == 5


# --- _resolve_source_skip_days ---


class TestResolveSourceSkipDays:
    def test_uses_the_sources_own_setting(self):
        helper = FakeYamlHelper({(SOURCE_0, merge.YAML_SETTING_SKIP_DAYS): "0, 6"})

        assert merge._resolve_source_skip_days(helper, SOURCE_0, ["5", "6"]) == ["0", "6"]

    def test_falls_back_to_the_global_default(self):
        helper = FakeYamlHelper({})

        assert merge._resolve_source_skip_days(helper, SOURCE_0, ["5", "6"]) == ["5", "6"]

    def test_normalizes_a_list_value(self):
        helper = FakeYamlHelper({(SOURCE_0, merge.YAML_SETTING_SKIP_DAYS): [5, 6]})

        assert merge._resolve_source_skip_days(helper, SOURCE_0, []) == ["5", "6"]

    def test_empty_override_means_skip_nothing(self):
        """An explicit empty value must override, not fall through to the global."""
        helper = FakeYamlHelper({(SOURCE_0, merge.YAML_SETTING_SKIP_DAYS): ""})

        assert merge._resolve_source_skip_days(helper, SOURCE_0, ["5", "6"]) == []

    def test_does_not_leak_yaml_error(self):
        """A missing optional setting must not raise.

        main() treats a YamlError from a source as "no more source calendars", so
        letting this escape would silently drop every calendar after the first one
        that omits skip_days.
        """
        helper = FakeYamlHelper({})

        merge._resolve_source_skip_days(helper, SOURCE_0, [])  # must not raise


# --- _process_source_calendar ---

# 2026-08-14 is a Friday, 2026-08-15 a Saturday (weekday 5).
FRIDAY_AND_SATURDAY = (
    {"start": "20260814T120000Z", "end": "20260814T130000Z"},
    {"start": "20260815T120000Z", "end": "20260815T130000Z"},
)


def process(
    monkeypatch,
    tmp_path,
    *,
    yaml_values=None,
    ics_events=None,
    icloud_events=None,
    calendar_service=None,
    url="https://example.com/cal.ics",
    download_error=None,
    ics_payload=None,
    index=0,
    default_skip_days=(),
):
    values = config_values()
    values.update(source_values(index=index))
    if yaml_values is not None:
        values = yaml_values
    payload = ics_payload if ics_payload is not None else ics_bytes(ics_events or [])
    fs = FakeFileSystem(tmp_path, ics_payload=payload, download_error=download_error)
    service = calendar_service or FakeCalendarService()
    if url is not None:
        monkeypatch.setenv(merge.ENV_VAR_CALENDAR_URL.format(index=index), url)

    merge._process_source_calendar(
        FakeYamlHelper(values),
        index,
        fs,
        datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
        list(default_skip_days),
        utc(2026, 8, 1),
        utc(2026, 8, 31, 23, 59),
        icloud_events if icloud_events is not None else [],
        service,
        "cal-guid",
    )
    return fs, service


class TestSyncSummaryLine:
    """The line that makes a run's effect readable without inferring it from timing."""

    def test_reports_the_tally_against_its_source(self, monkeypatch, tmp_path, quiet_terminal):
        process(
            monkeypatch,
            tmp_path,
            ics_events=[
                {"start": "20260812T120000Z", "end": "20260812T130000Z"},
                {"start": "20260812T140000Z", "end": "20260812T150000Z"},
            ],
        )

        assert any("[W] Google/Work: 2 added, 0 deleted" in line for line in quiet_terminal)

    def test_omits_already_gone_when_there_is_none(self, monkeypatch, tmp_path, quiet_terminal):
        process(monkeypatch, tmp_path, ics_events=[{"start": "20260812T120000Z", "end": "20260812T130000Z"}])

        summary = [line for line in quiet_terminal if "added," in line]

        assert summary, "expected a summary line"
        assert "already gone" not in summary[0]

    def test_mentions_already_gone_when_it_happens(self, monkeypatch, tmp_path, quiet_terminal):
        class NotFound(Exception):
            code = merge.HTTP_NOT_FOUND

        # An iCloud event with no matching source event is reconciled to a delete.
        process(
            monkeypatch,
            tmp_path,
            ics_events=[],
            icloud_events=[
                merge.MergeEvent(
                    "[W] Google/Work",
                    utc(2026, 8, 12, 12),
                    utc(2026, 8, 12, 13),
                    icloud_raw_event(),
                    None,
                )
            ],
            calendar_service=FakeCalendarService(remove_error=NotFound("Not Found")),
        )

        assert any("0 added, 0 deleted, 1 already gone" in line for line in quiet_terminal)


class TestProcessSourceCalendar:
    def test_adds_new_source_event_with_composed_title(self, monkeypatch, tmp_path):
        _, service = process(
            monkeypatch,
            tmp_path,
            ics_events=[{"start": "20260812T120000Z", "end": "20260812T130000Z"}],
        )

        assert len(service.added) == 1
        assert service.added[0].title == "[W] Google/Work"

    def test_downloads_from_the_configured_url(self, monkeypatch, tmp_path):
        fs, _ = process(monkeypatch, tmp_path, url="https://feed.example/x.ics")

        assert len(fs.downloads) == 1
        assert fs.downloads[0][0] == "https://feed.example/x.ics"

    def test_download_target_is_in_temp_dir(self, monkeypatch, tmp_path):
        fs, _ = process(monkeypatch, tmp_path)

        assert fs.downloads[0][1].startswith(str(tmp_path))
        assert fs.downloads[0][1].endswith(".ics")

    def test_deletes_icloud_event_missing_from_source(self, monkeypatch, tmp_path):
        # An iCloud event tagged for this source with no counterpart in the feed
        # must be removed.
        stale = merge.MergeEvent(
            title="[W] Google/Work",
            start=utc(2026, 8, 12, 12),
            end=utc(2026, 8, 12, 13),
            full_event=icloud_raw_event(guid="stale-guid", pguid="p"),
            action=None,
        )

        _, service = process(monkeypatch, tmp_path, ics_events=[], icloud_events=[stale])

        assert len(service.removed) == 1
        assert service.removed[0].guid == "stale-guid"

    def test_leaves_matching_event_untouched(self, monkeypatch, tmp_path):
        existing = merge.MergeEvent(
            title="[W] Google/Work",
            start=utc(2026, 8, 12, 12),
            end=utc(2026, 8, 12, 13),
            full_event=icloud_raw_event(),
            action=None,
        )

        _, service = process(
            monkeypatch,
            tmp_path,
            ics_events=[{"start": "20260812T120000Z", "end": "20260812T130000Z"}],
            icloud_events=[existing],
        )

        assert service.added == []
        assert service.removed == []

    def test_titles_only_the_added_events(self, monkeypatch, tmp_path):
        """When a run both keeps and adds events, only the new one gets titled.

        The existing event is already titled; the retitle loop must skip it
        rather than rewrite every event in the reconciled list.
        """
        existing = merge.MergeEvent(
            title="[W] Google/Work",
            start=utc(2026, 8, 12, 12),
            end=utc(2026, 8, 12, 13),
            full_event=icloud_raw_event(),
            action=None,
        )

        _, service = process(
            monkeypatch,
            tmp_path,
            ics_events=[
                {"start": "20260812T120000Z", "end": "20260812T130000Z"},  # matches `existing`
                {"start": "20260813T140000Z", "end": "20260813T150000Z"},  # new
            ],
            icloud_events=[existing],
        )

        assert len(service.added) == 1
        assert service.added[0].title == "[W] Google/Work"
        assert service.removed == []

    def test_ignores_icloud_events_from_other_sources(self, monkeypatch, tmp_path):
        other = merge.MergeEvent(
            title="[X] Other/Outlook",
            start=utc(2026, 8, 12, 12),
            end=utc(2026, 8, 12, 13),
            full_event=icloud_raw_event(),
            action=None,
        )

        _, service = process(monkeypatch, tmp_path, ics_events=[], icloud_events=[other])

        # The foreign-tagged event must not be deleted by this source's run.
        assert service.removed == []

    def test_per_source_skip_days_filters_the_feed(self, monkeypatch, tmp_path):
        """A source that skips Saturday must not import its Saturday events."""
        values = config_values()
        values.update(source_values())
        values[(SOURCE_0, merge.YAML_SETTING_SKIP_DAYS)] = "5"

        _, service = process(
            monkeypatch,
            tmp_path,
            yaml_values=values,
            ics_events=FRIDAY_AND_SATURDAY,
            default_skip_days=[],  # global skips nothing
        )

        assert len(service.added) == 1
        assert service.added[0].start_date.day == 14

    def test_falls_back_to_global_skip_days(self, monkeypatch, tmp_path):
        values = config_values()
        values.update(source_values())  # source declares no override

        _, service = process(
            monkeypatch,
            tmp_path,
            yaml_values=values,
            ics_events=FRIDAY_AND_SATURDAY,
            default_skip_days=["5"],  # global skips Saturday
        )

        assert len(service.added) == 1
        assert service.added[0].start_date.day == 14

    def test_source_can_opt_out_of_the_global_skip(self, monkeypatch, tmp_path):
        values = config_values()
        values.update(source_values())
        values[(SOURCE_0, merge.YAML_SETTING_SKIP_DAYS)] = ""  # this source syncs every day

        _, service = process(
            monkeypatch,
            tmp_path,
            yaml_values=values,
            ics_events=[{"start": "20260815T120000Z", "end": "20260815T130000Z"}],  # Saturday
            default_skip_days=["5", "6"],  # global skips the weekend
        )

        assert len(service.added) == 1

    def test_per_source_skip_days_also_filters_the_icloud_side(self, monkeypatch, tmp_path):
        """An iCloud event on a skipped day must be left alone, not deleted.

        It is excluded from reconciliation, matching how the feed is filtered, so
        it is neither matched nor removed.
        """
        values = config_values()
        values.update(source_values())
        values[(SOURCE_0, merge.YAML_SETTING_SKIP_DAYS)] = "5"

        saturday_event = merge.MergeEvent(
            title="[W] Google/Work",
            start=utc(2026, 8, 15, 12),
            end=utc(2026, 8, 15, 13),
            full_event=icloud_raw_event(guid="sat-guid", pguid="p"),
            action=None,
        )

        _, service = process(
            monkeypatch,
            tmp_path,
            yaml_values=values,
            ics_events=[],
            icloud_events=[saturday_event],
            default_skip_days=[],
        )

        assert service.removed == []

    def test_icloud_event_is_deleted_when_source_does_not_skip_that_day(self, monkeypatch, tmp_path):
        # Same setup as above but without the override, so Saturday is in scope
        # and the orphaned event is reconciled away.
        values = config_values()
        values.update(source_values())

        saturday_event = merge.MergeEvent(
            title="[W] Google/Work",
            start=utc(2026, 8, 15, 12),
            end=utc(2026, 8, 15, 13),
            full_event=icloud_raw_event(guid="sat-guid", pguid="p"),
            action=None,
        )

        _, service = process(
            monkeypatch,
            tmp_path,
            yaml_values=values,
            ics_events=[],
            icloud_events=[saturday_event],
            default_skip_days=[],
        )

        assert len(service.removed) == 1
        assert service.removed[0].guid == "sat-guid"

    def test_duplicate_slot_creates_one_icloud_event(self, monkeypatch, tmp_path):
        """End to end: two feed events in one slot must add a single event."""
        _, service = process(
            monkeypatch,
            tmp_path,
            ics_events=[
                {"start": "20260813T130000Z", "end": "20260813T140000Z", "summary": "Standup"},
                {"start": "20260813T130000Z", "end": "20260813T140000Z", "summary": "Other meeting"},
            ],
        )

        assert len(service.added) == 1

    def test_existing_duplicate_pair_in_icloud_is_cleaned_up(self, monkeypatch, tmp_path):
        """Duplicates synced before this fix are removed on the next run.

        The feed now yields one event for the slot, so the second iCloud copy no
        longer matches anything and is reconciled away. No manual cleanup needed.
        """
        pair = [
            merge.MergeEvent(
                title="[W] Google/Work",
                start=utc(2026, 8, 13, 13),
                end=utc(2026, 8, 13, 14),
                full_event=icloud_raw_event(guid=f"dupe-{index}", pguid="p"),
                action=None,
            )
            for index in range(2)
        ]

        _, service = process(
            monkeypatch,
            tmp_path,
            ics_events=[
                {"start": "20260813T130000Z", "end": "20260813T140000Z"},
                {"start": "20260813T130000Z", "end": "20260813T140000Z"},
            ],
            icloud_events=pair,
        )

        assert service.added == []
        assert len(service.removed) == 1

    def test_raises_on_missing_url(self, monkeypatch, tmp_path, quiet_terminal):
        with pytest.raises(RuntimeError, match="Missing calendar URL for index 0"):
            process(monkeypatch, tmp_path, url=None)

    def test_raises_on_invalid_timezone(self, monkeypatch, tmp_path, quiet_terminal):
        values = config_values()
        values.update(source_values(tz="Not/AZone"))

        with pytest.raises(RuntimeError, match="Invalid calendar configuration at index 0"):
            process(monkeypatch, tmp_path, yaml_values=values)

    def test_raises_on_download_failure(self, monkeypatch, tmp_path, quiet_terminal):
        download_error = ConnectionError("timeout")

        with pytest.raises(RuntimeError, match="Unable to download calendar"):
            process(monkeypatch, tmp_path, download_error=download_error)

    def test_raises_on_unparseable_ics(self, monkeypatch, tmp_path, quiet_terminal):
        with pytest.raises(RuntimeError, match="Unable to parse calendar"):
            process(monkeypatch, tmp_path, ics_payload=b"this is not an ics file")

    def test_propagates_yaml_error_for_missing_section(self, monkeypatch, tmp_path):
        # main() relies on YamlError to detect the end of the calendar list, so
        # it must not be swallowed here.
        values = config_values()

        with pytest.raises(YamlError):
            process(monkeypatch, tmp_path, yaml_values=values, index=3)


# --- main ---


class LoopRunaway(BaseException):
    """Raised by the spy when main()'s loop runs away.

    Derived from BaseException so main()'s `except Exception` cannot swallow it: a
    missing loop bound then fails the test immediately instead of hanging it, which
    would otherwise hang CI rather than reporting anything.
    """


class FlowSpy:
    """Records main()'s calls into the helpers it orchestrates."""

    def __init__(self, source_count=1, fail_on=None, never_ends=False, runaway_after=None):
        self.source_count = source_count
        # Indexes that raise a non-YamlError, i.e. a calendar that fails rather than
        # one that does not exist.
        self.fail_on = dict(fail_on or {})
        # When set, no index ever raises YamlError, so only the loop bound stops it.
        self.never_ends = never_ends
        # Escape hatch for that case: raises past main()'s handler after N attempts.
        self.runaway_after = runaway_after
        self.attempts = 0
        self.processed: list[int] = []
        self.messages: list[str] = []

    def install(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            merge,
            "_load_config",
            lambda: (FakeYamlHelper(config_values()), 5, ["5", "6"], FakeFileSystem(tmp_path)),
        )
        monkeypatch.setattr(merge, "_authenticate_icloud", lambda: fake_api())
        monkeypatch.setattr(
            merge,
            "_load_icloud_events",
            lambda service, days, skip: (
                FakeCalendarService(),
                "cal-guid",
                [],
                utc(2026, 8, 12),
                utc(2026, 8, 20, 23, 59),
                datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            ),
        )
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: self.messages.append(msg))

        def fake_process(yaml_helper, index, *args, **kwargs):
            self.attempts += 1
            if self.runaway_after is not None and self.attempts > self.runaway_after:
                raise LoopRunaway(f"loop reached {self.attempts} attempts with no bound")
            if index in self.fail_on:
                raise self.fail_on[index]
            if not self.never_ends and index >= self.source_count:
                raise YamlError(f"Missing key: {merge.YAML_SECTION_SOURCE_CALENDAR.format(index=index)!r}")
            self.processed.append(index)

        monkeypatch.setattr(merge, "_process_source_calendar", fake_process)
        return self


class TestYamlErrorShapes:
    """Pins the pyfangs messages `main()` uses to tell the two cases apart.

    `_source_section_is_absent` reads the message text, because `YamlHelper.get` raises
    the same type for an absent section and a missing setting inside an existing one.
    Asserted against the real helper, not a fake: if pyfangs changes its wording, this
    fails loudly instead of silently restoring the skip-every-later-calendar bug.
    """

    def build(self, tmp_path, body):
        config = tmp_path / "config.yaml"
        config.write_text(body)
        return RealYamlHelper(config)

    def test_an_absent_section_is_recognised(self, tmp_path):
        helper = self.build(tmp_path, "source-calendar-0:\n  source: vf\n")

        with pytest.raises(YamlError) as excinfo:
            helper.get("source-calendar-9", "source")

        assert merge._source_section_is_absent(excinfo.value)

    def test_a_missing_setting_is_not_an_absent_section(self, tmp_path):
        helper = self.build(tmp_path, "source-calendar-0:\n  tag: T\n")

        with pytest.raises(YamlError) as excinfo:
            helper.get("source-calendar-0", "source")

        assert not merge._source_section_is_absent(excinfo.value)


class TestSourceCalendarIsolation:
    """One calendar's failure must not cost the calendars after it.

    Reproduces 2026-08-20: an already-deleted event in source 0 aborted the run, so
    sources 1 and 2 were never processed and silently kept yesterday's picture.
    """

    def test_a_failing_source_does_not_stop_the_others(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy(source_count=3, fail_on={0: RuntimeError("Unable to download calendar vf [0]")})
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="source calendars failed"):
            merge.main()

        assert spy.processed == [1, 2]

    def test_the_run_still_fails(self, monkeypatch, tmp_path, quiet_terminal):
        """A partial sync is not a success; the existing alert must still fire."""
        FlowSpy(source_count=3, fail_on={1: RuntimeError("boom")}).install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="1 of 3 source calendars failed"):
            merge.main()

    def test_one_alert_summarises_every_failure(self, monkeypatch, tmp_path, quiet_terminal):
        """Alert once with a summary, rather than once per failed source."""
        FlowSpy(
            source_count=3,
            fail_on={0: RuntimeError("first broke"), 2: RuntimeError("third broke")},
        ).install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError) as excinfo:
            merge.main()

        message = str(excinfo.value)
        assert "2 of 3 source calendars failed" in message
        assert "source-calendar-0" in message
        assert "source-calendar-2" in message

    def test_the_summary_carries_each_underlying_cause(self, monkeypatch, tmp_path, quiet_terminal):
        cause = ConnectionError("feed timed out")
        wrapper = RuntimeError("Unable to download calendar vf [0]")
        wrapper.__cause__ = cause
        FlowSpy(source_count=2, fail_on={0: wrapper}).install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="ConnectionError: feed timed out"):
            merge.main()

    def test_a_clean_run_raises_nothing(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy(source_count=3).install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert spy.processed == [0, 1, 2]

    def test_the_end_of_day_message_is_withheld_after_a_failure(self, monkeypatch, tmp_path, quiet_terminal):
        """Announcing "finished for today" alongside a failure alert contradicts it."""
        spy = FlowSpy(source_count=2, fail_on={0: RuntimeError("boom")})
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--last"])

        with pytest.raises(RuntimeError):
            merge.main()

        assert not any("finished for today" in message for message in spy.messages)

    def test_the_end_of_day_message_still_sends_on_a_clean_run(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy(source_count=2)
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--last"])

        merge.main()

        assert any("finished for today" in message for message in spy.messages)

    def test_a_malformed_section_does_not_end_the_list(self, monkeypatch, tmp_path, quiet_terminal):
        """A section that exists but omits `source` raises the same YamlError type.

        Reading it as the end of the list would skip every later calendar without
        recording anything -- the failure this whole handler exists to prevent.
        """
        malformed = YamlError("Missing setting: 'source'")
        spy = FlowSpy(source_count=3, fail_on={1: malformed})
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="1 of 3 source calendars failed"):
            merge.main()

        assert spy.processed == [0, 2], "the calendar after the malformed one must still sync"

    def test_a_malformed_section_is_named_in_the_summary(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy(source_count=2, fail_on={0: YamlError("Missing setting: 'tz'")})
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="source-calendar-0"):
            merge.main()

    def test_exactly_the_maximum_number_of_calendars_is_not_a_failure(self, monkeypatch, tmp_path, quiet_terminal):
        """The bound must not reject a configuration that sits exactly on it."""
        monkeypatch.setattr(merge, "MAX_SOURCE_CALENDARS", 3)
        spy = FlowSpy(source_count=3)
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert spy.processed == [0, 1, 2]

    def test_the_loop_is_bounded(self, monkeypatch, tmp_path, quiet_terminal):
        """A fault before the section read would otherwise never terminate.

        Catching per-source failures made that possible, and a hung schedule is worse
        than a failed one.
        """
        monkeypatch.setattr(merge, "MAX_SOURCE_CALENDARS", 3)
        spy = FlowSpy(
            never_ends=True,
            fail_on=dict.fromkeys(range(50), RuntimeError("always broken")),
            runaway_after=20,
        )
        spy.install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="more than 3 source calendars configured"):
            merge.main()

        assert spy.processed == []


class TestMain:
    def test_processes_every_source_calendar(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy(source_count=3).install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert spy.processed == [0, 1, 2]

    def test_sources_omitting_skip_days_do_not_truncate_the_loop(self, monkeypatch, tmp_path, quiet_terminal):
        """Regression guard for the optional-setting trap.

        main() breaks out of the source loop on YamlError. YamlHelper raises
        YamlError for an absent setting, so reading the optional per-source
        skip_days without catching it would stop the loop at the first source that
        omits it -- silently skipping every calendar after it, with no error.
        """
        processed = []
        values = config_values()
        for index in (0, 1, 2):
            values.update(source_values(index=index))
        # Only the middle source overrides skip_days; 0 and 2 omit it.
        values[(SOURCE_1, merge.YAML_SETTING_SKIP_DAYS)] = "0"

        helper = FakeYamlHelper(values)
        monkeypatch.setattr(merge, "_load_config", lambda: (helper, 5, ["5", "6"], FakeFileSystem(tmp_path)))
        monkeypatch.setattr(merge, "_authenticate_icloud", lambda: fake_api())
        monkeypatch.setattr(
            merge,
            "_load_icloud_events",
            lambda service, days, skip: (
                FakeCalendarService(),
                "cal-guid",
                [],
                utc(2026, 8, 12),
                utc(2026, 8, 20, 23, 59),
                datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            ),
        )
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: None)

        real_resolve = merge._resolve_source_skip_days

        def tracking_process(yaml_helper, index, fs, now, default_skip_days, *args, **kwargs):
            section = merge.YAML_SECTION_SOURCE_CALENDAR.format(index=index)
            # Raises YamlError for a genuinely absent section, ending the loop.
            yaml_helper.get(section, merge.YAML_SETTING_CALENDAR_SOURCE)
            processed.append((index, real_resolve(yaml_helper, section, default_skip_days)))

        monkeypatch.setattr(merge, "_process_source_calendar", tracking_process)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert [index for index, _ in processed] == [0, 1, 2]
        assert dict(processed) == {0: ["5", "6"], 1: ["0"], 2: ["5", "6"]}

    def test_stops_cleanly_when_no_calendars_configured(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy(source_count=0).install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert spy.processed == []
        assert any("no more source calendars" in line for line in quiet_terminal)

    def test_sends_no_notifications_without_flags(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy().install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert spy.messages == []

    def test_first_flag_sends_start_of_day_message(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy().install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--first"])

        merge.main()

        assert len(spy.messages) == 1
        assert "started" in spy.messages[0]

    def test_last_flag_sends_end_of_day_message(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy().install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--last"])

        merge.main()

        assert len(spy.messages) == 1
        assert "finished" in spy.messages[0]

    def test_both_flags_send_both_messages(self, monkeypatch, tmp_path, quiet_terminal):
        spy = FlowSpy().install(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--first", "--last"])

        merge.main()

        assert len(spy.messages) == 2
        assert "started" in spy.messages[0]
        assert "finished" in spy.messages[1]

    def test_start_message_is_sent_before_authentication(self, monkeypatch, tmp_path, quiet_terminal):
        """The morning ping should arrive even if iCloud auth later fails."""
        order = []
        FlowSpy().install(monkeypatch, tmp_path)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: order.append("message"))

        def failing_auth():
            order.append("auth")
            raise RuntimeError("2FA validation failed")

        monkeypatch.setattr(merge, "_authenticate_icloud", failing_auth)
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--first"])

        with pytest.raises(RuntimeError):
            merge.main()

        assert order == ["message", "auth"]

    def test_config_failure_propagates(self, monkeypatch, tmp_path, quiet_terminal):
        def boom():
            raise RuntimeError("Unable to open YAML configuration")

        monkeypatch.setattr(merge, "_load_config", boom)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        with pytest.raises(RuntimeError, match="Unable to open YAML configuration"):
            merge.main()

    def test_passes_utc_window_to_source_processing(self, monkeypatch, tmp_path, quiet_terminal):
        captured: dict[str, datetime] = {}
        skip_seen: list[list[str]] = []
        FlowSpy(source_count=1).install(monkeypatch, tmp_path)

        def capture(yaml_helper, index, fs, now, skip_days, today_bod, cut_off, *args):
            if index > 0:
                raise YamlError("Missing key: 'source-calendar-1'")
            captured.update(today_bod=today_bod, cut_off=cut_off)
            skip_seen.append(skip_days)

        monkeypatch.setattr(merge, "_process_source_calendar", capture)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])

        merge.main()

        assert captured["today_bod"].tzinfo == UTC
        assert captured["cut_off"].tzinfo == UTC
        assert skip_seen == [["5", "6"]]


class TestModuleConstants:
    def test_zoneinfo_is_used_for_calendar_timezone(self):
        # Guards the config contract: `tz` values must be IANA zone names.
        assert ZoneInfo(BA_TZ).key == BA_TZ
