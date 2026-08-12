"""Shared fixtures and fakes for the calendar-merge test suite.

The production module talks to iCloud, Telegram, the filesystem and the
terminal. Everything here exists to make those boundaries injectable so the
tests stay hermetic: no network, no real config file, no log writes outside
tmp_path.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import merge

# --- environment isolation ---

ENV_VARS = (
    merge.ENV_ICLOUD_USER,
    merge.ENV_ICLOUD_PASS,
    merge.ENV_TELEGRAM_TOKEN,
    merge.ENV_TELEGRAM_CHAT_ID,
    merge.ENV_LOG_FILE,
    merge.ENV_LOG_LEVEL,
    merge.ENV_VAR_CALENDAR_URL.format(index=0),
    merge.ENV_VAR_CALENDAR_URL.format(index=1),
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Drop every env var merge.py reads, so tests never inherit a real .env."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def quiet_terminal(monkeypatch):
    """Capture terminal output instead of printing it.

    Returns the list of rendered lines so tests can assert on user-facing
    messages without polluting pytest output.
    """
    lines: list[str] = []
    monkeypatch.setattr(merge.term, "print", lambda msg, *a, **k: lines.append(str(msg)))
    monkeypatch.setattr(merge.term, "print_done", lambda *a, **k: lines.append("<done>"))
    monkeypatch.setattr(merge.term, "print_failed", lambda *a, **k: lines.append("<failed>"))
    monkeypatch.setattr(merge.term, "print_header_box", lambda *a, **k: None)
    return lines


@pytest.fixture(autouse=True)
def reset_logger():
    """Detach handlers around each test.

    `_configure_logging` is idempotent by checking `logger.handlers`, so a
    handler leaked by one test would silently disable it in the next.
    """
    original = list(merge.logger.handlers)
    merge.logger.handlers.clear()
    yield merge.logger
    for handler in merge.logger.handlers:
        handler.close()
    merge.logger.handlers.clear()
    merge.logger.handlers.extend(original)


@pytest.fixture
def captured_logs():
    """Attach an in-memory handler and return the emitted LogRecords."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    merge.logger.addHandler(handler)
    merge.logger.setLevel(logging.DEBUG)
    yield records
    merge.logger.removeHandler(handler)


# --- Telegram fakes ---


class FakeMessage:
    def __init__(self, text, date=None):
        self.text = text
        self.date = date


class FakeUpdate:
    def __init__(self, update_id, message=None):
        self.update_id = update_id
        self.message = message


class FakeNotifier:
    """Stands in for pyfangs TelegramNotifier.

    `updates_batches` is consumed one batch per `get_updates` call; once
    exhausted every further call returns an empty list.
    """

    def __init__(self, token=None, chat_id=None, updates_batches=None):
        self.token = token
        self.chat_id = chat_id
        self.sent: list[tuple[str, bool]] = []
        self.closed = False
        self._batches = list(updates_batches or [])
        self.get_updates_calls: list[dict] = []

    async def send_message(self, message, disable_notification=False):
        self.sent.append((message, disable_notification))

    async def get_updates(self, offset=None, timeout=None, allowed_updates=None):
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        if self._batches:
            return self._batches.pop(0)
        return []

    async def close(self):
        self.closed = True


def notifier_factory(notifier):
    """Build a non-context-manager factory returning `notifier`.

    `merge` branches on `hasattr(factory, "__aenter__")`, so a plain function
    exercises the manual open/close path.
    """

    def _factory(token=None, chat_id=None):
        notifier.token = token
        notifier.chat_id = chat_id
        return notifier

    return _factory


def async_cm_factory(notifier):
    """Build an async-context-manager factory returning `notifier`."""

    class _Factory:
        def __init__(self, token=None, chat_id=None):
            notifier.token = token
            notifier.chat_id = chat_id
            self.notifier = notifier

        async def __aenter__(self):
            return self.notifier

        async def __aexit__(self, *exc):
            self.notifier.closed = True
            return False

    return _Factory


@pytest.fixture
def telegram_configured(monkeypatch):
    """Populate the Telegram env vars."""
    monkeypatch.setenv(merge.ENV_TELEGRAM_TOKEN, "test-token")
    monkeypatch.setenv(merge.ENV_TELEGRAM_CHAT_ID, "test-chat")


# --- iCloud fakes ---


class FakeCalendarService:
    def __init__(self, calendars=None, events=None, add_error=None, remove_error=None):
        self._calendars = calendars if calendars is not None else [{"guid": "cal-guid"}]
        self._events = events if events is not None else []
        self.added: list = []
        self.removed: list = []
        self._add_error = add_error
        self._remove_error = remove_error

    def get_calendars(self):
        return self._calendars

    def get_events(self, from_dt=None, to_dt=None):
        return self._events

    def add_event(self, event):
        if self._add_error:
            raise self._add_error
        self.added.append(event)

    def remove_event(self, event):
        if self._remove_error:
            raise self._remove_error
        self.removed.append(event)


def fake_api(
    requires_2fa=False,
    requires_2sa=False,
    security_key_names=None,
    is_trusted_session=True,
    trust_result=True,
    validate_result=True,
):
    """Build a stand-in for PyiCloudService covering the 2FA branches."""
    return SimpleNamespace(
        requires_2fa=requires_2fa,
        requires_2sa=requires_2sa,
        security_key_names=security_key_names or [],
        fido2_devices=["key-1"],
        is_trusted_session=is_trusted_session,
        trust_session=lambda: trust_result,
        validate_2fa_code=lambda code: validate_result,
        request_2fa_code=lambda: None,
        two_factor_delivery_method="trusteddevice",
        confirm_security_key=lambda device: None,
        trusted_devices=[{"deviceName": "iPhone"}],
        send_verification_code=lambda device: True,
        validate_verification_code=lambda device, code: True,
    )


# --- event helpers ---

BA_TZ = "America/Argentina/Buenos_Aires"


def icloud_raw_event(
    start=(0, 2026, 8, 12, 9, 0),
    end=(0, 2026, 8, 12, 10, 0),
    title="[W] Work/Google",
    tz=BA_TZ,
    all_day=False,
    guid="event-guid",
    pguid="cal-guid",
):
    """Build a raw iCloud event dict in the shape merge.py expects."""
    return {
        merge.ICLOUD_FIELD_START_DATE: list(start),
        merge.ICLOUD_FIELD_END_DATE: list(end),
        merge.ICLOUD_FIELD_TITLE: title,
        merge.ICLOUD_FIELD_TZ: tz,
        merge.ICLOUD_FIELD_ALL_DAY_EVENT: all_day,
        "guid": guid,
        "pGuid": pguid,
    }


def merge_event(start, end, title="[W] Work/Google", action=None, full_event=None):
    return merge.MergeEvent(title=title, start=start, end=end, full_event=full_event, action=action)


def utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def ics_bytes(events):
    """Render a minimal but valid ICS document.

    Each entry is a dict with `start`/`end` as `YYYYMMDDTHHMMSSZ` strings and an
    optional `transp` value, which merge.py treats as an out-of-office marker.
    """
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//test//EN"]
    for index, event in enumerate(events):
        lines += [
            "BEGIN:VEVENT",
            f"UID:event-{index}@test",
            "DTSTAMP:20260812T000000Z",
            f"DTSTART:{event['start']}",
            f"DTEND:{event['end']}",
            f"SUMMARY:{event.get('summary', f'Event {index}')}",
        ]
        if event.get("transp"):
            lines.append(f"TRANSP:{event['transp']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode()


class FakeYamlHelper:
    """YamlHelper stand-in.

    `values` maps (section, key) to a value. A missing section raises YamlError,
    which is how `main()` detects the end of the source-calendar list.
    """

    def __init__(self, values):
        self.values = values

    def get(self, section, key):
        if (section, key) not in self.values:
            from pyfangs.yaml import YamlError

            raise YamlError(f"missing {section}.{key}")
        return self.values[(section, key)]


class FakeFileSystem:
    def __init__(self, tmp_path, ics_payload=b"", download_error=None):
        self.tmp_path = tmp_path
        self.ics_payload = ics_payload
        self.download_error = download_error
        self.downloads: list[tuple[str, str]] = []

    def join_paths(self, *parts):
        from pathlib import Path

        return str(Path(*parts))

    def get_temp_dir(self):
        return str(self.tmp_path)

    def download(self, url, destination):
        if self.download_error:
            raise self.download_error
        self.downloads.append((url, destination))
        from pathlib import Path

        Path(destination).write_bytes(self.ics_payload)


__all__ = [
    "BA_TZ",
    "FakeCalendarService",
    "FakeFileSystem",
    "FakeMessage",
    "FakeNotifier",
    "FakeUpdate",
    "FakeYamlHelper",
    "ZoneInfo",
    "async_cm_factory",
    "fake_api",
    "icloud_raw_event",
    "ics_bytes",
    "merge_event",
    "notifier_factory",
    "timedelta",
    "utc",
]
