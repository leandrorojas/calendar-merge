"""The preflight: each service is probed before the merge relies on it."""

import io
import urllib.error
from email.message import Message
from typing import ClassVar

import pytest
from caldav.lib.error import AuthorizationError
from telegram.error import InvalidToken, NetworkError

import merge
from merge import ServiceCheck, ServiceState
from tests.conftest import FakeCalendarService, FakeFileSystem, FakeYamlHelper, fake_api, utc
from tests.test_flow import config_values, source_values

# Captured at import, before the autouse fixture replaces it for every test.
REAL_RUN_PREFLIGHT = merge._run_preflight

SECRET_URL = "https://outlook.office365.com/owa/calendar/SECRET-TOKEN/calendar.ics"


class FakeFeed:
    def __init__(self, error=None):
        self.error = error
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, size=-1):
        self.reads += 1
        return b"B"


def feed_opener(error=None, seen: list | None = None):
    def opener(url, timeout):
        if seen is not None:
            seen.append((url, timeout))
        if error:
            raise error
        return FakeFeed()

    return opener


def http_error(code):
    return urllib.error.HTTPError(SECRET_URL, code, "reason", Message(), io.BytesIO())


class TestFeedCheck:
    def test_a_feed_that_answers_is_up(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(merge.urllib.request, "urlopen", feed_opener(seen=seen))

        check = merge._check_feed(2, SECRET_URL)

        assert check == ServiceCheck("source-calendar-2", ServiceState.up, "", 2)
        assert seen == [(SECRET_URL, merge.PREFLIGHT_TIMEOUT_SECONDS)]

    @pytest.mark.parametrize("code", [500, 502, 503, 408, 429, 301])
    def test_server_errors_and_retryable_statuses_are_down(self, monkeypatch, code):
        monkeypatch.setattr(merge.urllib.request, "urlopen", feed_opener(http_error(code)))

        check = merge._check_feed(0, SECRET_URL)

        assert (check.state, check.detail) == (ServiceState.down, f"HTTP {code}")

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
    def test_a_refusal_is_rejected(self, monkeypatch, code):
        monkeypatch.setattr(merge.urllib.request, "urlopen", feed_opener(http_error(code)))

        check = merge._check_feed(0, SECRET_URL)

        assert (check.state, check.detail) == (ServiceState.rejected, f"HTTP {code}")

    def test_an_unreachable_host_is_down_with_the_transport_reason(self, monkeypatch):
        error = urllib.error.URLError(ConnectionResetError("[Errno 104] Connection reset by peer"))
        monkeypatch.setattr(merge.urllib.request, "urlopen", feed_opener(error))

        check = merge._check_feed(2, SECRET_URL)

        assert check.state is ServiceState.down
        assert "Connection reset by peer" in check.detail

    def test_a_timeout_is_down(self, monkeypatch):
        monkeypatch.setattr(merge.urllib.request, "urlopen", feed_opener(TimeoutError("timed out")))

        check = merge._check_feed(1, SECRET_URL)

        assert (check.state, check.detail) == (ServiceState.down, "TimeoutError")

    def test_the_feed_url_never_reaches_the_report(self, monkeypatch):
        """The URLs are secrets, and a malformed one is repeated in its own exception."""
        monkeypatch.setattr(
            merge.urllib.request, "urlopen", feed_opener(ValueError(f"unknown url type: {SECRET_URL!r}"))
        )

        check = merge._check_feed(0, SECRET_URL)

        assert "SECRET-TOKEN" not in repr(check)


class FakeDav:
    error: ClassVar[BaseException | None] = None
    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeDav.instances.append(self)

    def principal(self):
        if FakeDav.error:
            raise FakeDav.error
        return object()


@pytest.fixture
def fake_dav(monkeypatch):
    FakeDav.error = None
    FakeDav.instances = []
    monkeypatch.setattr(merge, "DAVClient", FakeDav)
    return FakeDav


class TestICloudCheck:
    def test_without_an_app_password_it_is_skipped_not_probed(self, fake_dav):
        check = merge._check_icloud()

        assert check.state is ServiceState.skipped
        assert fake_dav.instances == []

    def test_a_missing_username_names_the_variable(self, monkeypatch, fake_dav):
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "pw")

        check = merge._check_icloud()

        assert (check.state, check.detail) == (ServiceState.rejected, f"{merge.ENV_ICLOUD_USER} is not set")

    def test_a_working_login_is_up_and_the_probe_is_bounded(self, monkeypatch, fake_dav):
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "pw")
        monkeypatch.setenv(merge.ENV_ICLOUD_USER, "me@example.test")

        check = merge._check_icloud()

        assert check.state is ServiceState.up
        assert fake_dav.instances[0].kwargs["timeout"] == merge.PREFLIGHT_TIMEOUT_SECONDS

    def test_refused_credentials_are_rejected(self, monkeypatch, fake_dav):
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "pw")
        monkeypatch.setenv(merge.ENV_ICLOUD_USER, "me@example.test")
        fake_dav.error = AuthorizationError()

        check = merge._check_icloud()

        assert (check.state, check.detail) == (ServiceState.rejected, "credentials refused")

    def test_any_other_failure_is_down_and_keeps_the_cause(self, monkeypatch, fake_dav):
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "pw")
        monkeypatch.setenv(merge.ENV_ICLOUD_USER, "me@example.test")
        fake_dav.error = ConnectionError("no route to host")

        check = merge._check_icloud()

        assert check.state is ServiceState.down
        assert "no route to host" in check.detail

    def test_the_password_never_reaches_the_report(self, monkeypatch, fake_dav):
        monkeypatch.setenv(merge.ENV_ICLOUD_APP_PASSWORD, "hunter2-app-password")
        monkeypatch.setenv(merge.ENV_ICLOUD_USER, "me@example.test")
        fake_dav.error = AuthorizationError()

        assert "hunter2" not in repr(merge._check_icloud())


class FakeProbeNotifier:
    error: ClassVar[BaseException | None] = None
    hang: ClassVar[bool] = False

    def __init__(self, token, chat_id):
        self.token = token

    async def __aenter__(self):
        if FakeProbeNotifier.hang:
            import asyncio

            await asyncio.sleep(60)
        if FakeProbeNotifier.error:
            raise FakeProbeNotifier.error
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_notifier(monkeypatch):
    FakeProbeNotifier.error = None
    FakeProbeNotifier.hang = False
    monkeypatch.setattr(merge, "TelegramNotifier", FakeProbeNotifier)
    monkeypatch.setenv(merge.ENV_TELEGRAM_TOKEN, "123:SECRET-BOT-TOKEN")
    monkeypatch.setenv(merge.ENV_TELEGRAM_CHAT_ID, "42")
    return FakeProbeNotifier


class TestTelegramCheck:
    def test_unconfigured_is_skipped(self, monkeypatch):
        assert merge._check_telegram().state is ServiceState.skipped

    def test_a_token_without_a_chat_id_is_skipped(self, monkeypatch):
        monkeypatch.setenv(merge.ENV_TELEGRAM_TOKEN, "123:abc")

        assert merge._check_telegram().state is ServiceState.skipped

    def test_an_accepted_token_is_up(self, fake_notifier):
        assert merge._check_telegram().state is ServiceState.up

    def test_a_rejected_token_is_rejected(self, fake_notifier):
        fake_notifier.error = InvalidToken("The bot token was rejected by the server.")

        check = merge._check_telegram()

        assert (check.state, check.detail) == (ServiceState.rejected, "token rejected")

    def test_a_network_failure_is_down_and_names_only_the_type(self, fake_notifier):
        fake_notifier.error = NetworkError("https://api.telegram.org/bot123:SECRET-BOT-TOKEN/getMe failed")

        check = merge._check_telegram()

        assert (check.state, check.detail) == (ServiceState.down, "NetworkError")
        assert "SECRET-BOT-TOKEN" not in repr(check)

    def test_a_hang_is_cut_off(self, monkeypatch, fake_notifier):
        fake_notifier.hang = True
        monkeypatch.setattr(merge, "PREFLIGHT_TIMEOUT_SECONDS", 0.05)

        check = merge._check_telegram()

        assert (check.state, check.detail) == (ServiceState.down, "TimeoutError")


class TestRunPreflight:
    def test_it_checks_icloud_telegram_then_each_feed_in_order(self, monkeypatch):
        monkeypatch.setattr(merge, "_check_icloud", lambda: ServiceCheck("iCloud", ServiceState.up))
        monkeypatch.setattr(merge, "_check_telegram", lambda: ServiceCheck("Telegram", ServiceState.up))
        monkeypatch.setattr(
            merge, "_check_feed", lambda index, url: ServiceCheck(f"feed-{index}", ServiceState.up, url, index)
        )
        monkeypatch.setenv("CALENDAR_URL_0", "u0")
        monkeypatch.setenv("CALENDAR_URL_1", "u1")

        checks = REAL_RUN_PREFLIGHT()

        assert [c.name for c in checks] == ["iCloud", "Telegram", "feed-0", "feed-1"]
        assert [c.detail for c in checks[2:]] == ["u0", "u1"]

    def test_without_feeds_only_the_two_services_are_checked(self, monkeypatch):
        monkeypatch.setattr(merge, "_check_icloud", lambda: ServiceCheck("iCloud", ServiceState.up))
        monkeypatch.setattr(merge, "_check_telegram", lambda: ServiceCheck("Telegram", ServiceState.up))

        assert [c.name for c in REAL_RUN_PREFLIGHT()] == ["iCloud", "Telegram"]

    def test_the_checks_run_side_by_side(self, monkeypatch):
        """A dead service must cost the run one timeout, not one per service."""
        import threading

        barrier = threading.Barrier(2, timeout=5)

        def meet():
            barrier.wait()
            return ServiceCheck("x", ServiceState.up)

        monkeypatch.setattr(merge, "_check_icloud", meet)
        monkeypatch.setattr(merge, "_check_telegram", meet)

        assert len(REAL_RUN_PREFLIGHT()) == 2

    def test_the_loop_is_bounded(self, monkeypatch):
        monkeypatch.setattr(merge, "_check_icloud", lambda: ServiceCheck("iCloud", ServiceState.up))
        monkeypatch.setattr(merge, "_check_telegram", lambda: ServiceCheck("Telegram", ServiceState.up))
        monkeypatch.setattr(merge, "_check_feed", lambda index, url: ServiceCheck(f"f{index}", ServiceState.up))
        monkeypatch.setattr(
            merge.os, "getenv", lambda name, default=None: "u" if name.startswith("CALENDAR_URL") else None
        )

        assert len(REAL_RUN_PREFLIGHT()) == 2 + merge.MAX_SOURCE_CALENDARS + 1


class TestReporting:
    def test_every_service_gets_a_line_and_failures_are_logged_as_warnings(self, quiet_terminal, captured_logs):
        merge._report_preflight(
            [
                ServiceCheck("iCloud", ServiceState.up),
                ServiceCheck("source-calendar-2", ServiceState.down, "TimeoutError", 2),
            ]
        )

        assert any("iCloud: up" in line for line in quiet_terminal)
        assert any("source-calendar-2: down (TimeoutError)" in line for line in quiet_terminal)
        levels = {r.getMessage(): r.levelname for r in captured_logs}
        assert levels["preflight iCloud: up"] == "INFO"
        assert levels["preflight source-calendar-2: down (TimeoutError)"] == "WARNING"

    def test_skipped_counts_as_usable(self):
        assert ServiceCheck("Telegram", ServiceState.skipped).usable

    def test_down_and_rejected_do_not(self):
        assert not ServiceCheck("a", ServiceState.down).usable
        assert not ServiceCheck("a", ServiceState.rejected).usable


class TestRunCheck:
    def test_all_usable_exits_zero(self, monkeypatch):
        monkeypatch.setattr(
            merge,
            "_run_preflight",
            lambda: [ServiceCheck("iCloud", ServiceState.up), ServiceCheck("T", ServiceState.skipped)],
        )

        assert merge._run_check() == 0

    def test_any_unusable_service_exits_one(self, monkeypatch):
        monkeypatch.setattr(
            merge,
            "_run_preflight",
            lambda: [ServiceCheck("iCloud", ServiceState.up), ServiceCheck("f", ServiceState.down)],
        )

        assert merge._run_check() == 1

    def test_the_flag_exits_with_that_status_and_merges_nothing(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["calendar-merge", "--check"])
        monkeypatch.setattr(merge, "_run_check", lambda: 1)
        monkeypatch.setattr(merge, "main", lambda: pytest.fail("a check must not merge"))
        monkeypatch.setattr(merge, "_configure_logging", lambda: None)

        with pytest.raises(SystemExit) as excinfo:
            merge.cli()

        assert excinfo.value.code == 1


class FlowHarness:
    """`main()` with every boundary faked, recording what it reached."""

    def __init__(self, tmp_path, checks):
        self.tmp_path = tmp_path
        self.checks = checks
        self.authenticated = False
        self.processed: list[tuple[int, dict]] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(merge, "_run_preflight", lambda: self.checks)
        monkeypatch.setattr(
            merge,
            "_load_config",
            lambda: (FakeYamlHelper(config_values()), 5, ["5", "6"], FakeFileSystem(self.tmp_path), None),
        )

        def authenticate():
            self.authenticated = True
            return fake_api()

        monkeypatch.setattr(merge, "_authenticate_backend", authenticate)
        monkeypatch.setattr(
            merge,
            "_load_icloud_events",
            lambda service, days, skip, destination=None: (
                FakeCalendarService(),
                "guid",
                [],
                utc(2026, 8, 12),
                utc(2026, 8, 20, 23, 59),
                utc(2026, 8, 12, 9),
            ),
        )
        monkeypatch.setattr(merge, "send_telegram_message", lambda *a, **k: True)

        def process(yaml_helper, index, *args, unavailable_feeds=None, **kwargs):
            if index >= 2:
                raise merge.YamlError(f"Missing key: {merge.YAML_SECTION_SOURCE_CALENDAR.format(index=index)!r}")
            self.processed.append((index, dict(unavailable_feeds or {})))

        monkeypatch.setattr(merge, "_process_source_calendar", process)
        monkeypatch.setattr("sys.argv", ["calendar-merge"])
        return self


class TestMainUsesThePreflight:
    def test_icloud_down_stops_the_run_before_any_sign_in(self, monkeypatch, tmp_path):
        harness = FlowHarness(tmp_path, [ServiceCheck("iCloud", ServiceState.down, "ConnectError")]).install(
            monkeypatch
        )

        with pytest.raises(RuntimeError, match=r"iCloud is unavailable \(down\): ConnectError"):
            merge.main()

        assert not harness.authenticated
        assert harness.processed == []

    def test_icloud_rejected_stops_the_run_too(self, monkeypatch, tmp_path):
        FlowHarness(tmp_path, [ServiceCheck("iCloud", ServiceState.rejected, "credentials refused")]).install(
            monkeypatch
        )

        with pytest.raises(RuntimeError, match="credentials refused"):
            merge.main()

    def test_telegram_down_does_not_stop_the_run(self, monkeypatch, tmp_path):
        harness = FlowHarness(
            tmp_path,
            [ServiceCheck("iCloud", ServiceState.up), ServiceCheck("Telegram", ServiceState.down, "TimedOut")],
        ).install(monkeypatch)

        merge.main()

        assert harness.authenticated
        assert [index for index, _ in harness.processed] == [0, 1]

    def test_a_dead_feed_is_handed_to_its_source_and_the_others_still_run(self, monkeypatch, tmp_path):
        harness = FlowHarness(
            tmp_path,
            [
                ServiceCheck("iCloud", ServiceState.up),
                ServiceCheck("source-calendar-1", ServiceState.down, "TimeoutError", 1),
            ],
        ).install(monkeypatch)

        merge.main()

        assert harness.processed[0][1] == {1: "down: TimeoutError"}

    def test_a_clean_preflight_marks_no_feed_unavailable(self, monkeypatch, tmp_path):
        harness = FlowHarness(tmp_path, [ServiceCheck("iCloud", ServiceState.up)]).install(monkeypatch)

        merge.main()

        assert all(unavailable == {} for _, unavailable in harness.processed)


class TestUnavailableFeedSource:
    def run_source(self, monkeypatch, tmp_path, unavailable):
        fs = FakeFileSystem(tmp_path, ics_payload=b"BEGIN:VCALENDAR\nEND:VCALENDAR\n")
        monkeypatch.setenv("CALENDAR_URL_0", "https://feed.test/a.ics")
        merge._process_source_calendar(
            FakeYamlHelper(config_values(extra=source_values(0))),
            0,
            fs,
            utc(2026, 8, 12, 9),
            ["5", "6"],
            utc(2026, 8, 12),
            utc(2026, 8, 20, 23, 59),
            [],
            FakeCalendarService(),
            "guid",
            unavailable_feeds=unavailable,
        )
        return fs

    def test_an_unavailable_feed_fails_that_source_without_downloading(self, monkeypatch, tmp_path):
        fs = FakeFileSystem(tmp_path)
        monkeypatch.setenv("CALENDAR_URL_0", "https://feed.test/a.ics")

        with pytest.raises(RuntimeError, match=r"is unavailable: down: TimeoutError"):
            merge._process_source_calendar(
                FakeYamlHelper(config_values(extra=source_values(0))),
                0,
                fs,
                utc(2026, 8, 12, 9),
                ["5", "6"],
                utc(2026, 8, 12),
                utc(2026, 8, 20, 23, 59),
                [],
                FakeCalendarService(),
                "guid",
                unavailable_feeds={0: "down: TimeoutError"},
            )

        assert fs.downloads == []

    def test_another_sources_failure_does_not_block_this_one(self, monkeypatch, tmp_path):
        fs = self.run_source(monkeypatch, tmp_path, {1: "down: TimeoutError"})

        assert len(fs.downloads) == 1


class TestUnavailableFeedsMapping:
    def test_only_unusable_feeds_are_listed(self):
        checks = [
            ServiceCheck("iCloud", ServiceState.down, "x"),
            ServiceCheck("source-calendar-0", ServiceState.up, "", 0),
            ServiceCheck("source-calendar-1", ServiceState.rejected, "HTTP 404", 1),
        ]

        assert merge._unavailable_feeds(checks) == {1: "rejected: HTTP 404"}
