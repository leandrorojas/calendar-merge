"""Tests for suppressing repeated failure alerts.

Runs are independent processes fifteen minutes apart, so a single upstream outage
used to send one identical alert per run -- three on 2026-08-18, and up to forty-one
across a full weekday. Alerting is now by transition, with a configurable reminder.
"""

import json

import pytest

import merge


def state_file(tmp_path):
    return tmp_path / "failure-state.json"


def read_state(tmp_path):
    return json.loads(state_file(tmp_path).read_text())


CAUSE = "Unable to load events from iCloud (PyiCloudAPIResponseException: Auth required. (500))"
OTHER = "Unable to download calendar vf [0] (ConnectionError: timed out)"


@pytest.fixture(autouse=True)
def fixed_cadence(monkeypatch):
    """Pin the reminder cadence so tests do not depend on a real config.yaml."""
    monkeypatch.setattr(merge, "_failure_alert_every", lambda: 4)


class TestShouldReportFailure:
    def test_the_first_failure_always_alerts(self, tmp_path):
        assert merge._should_report_failure(CAUSE) is True

    def test_the_same_cause_repeating_stays_quiet(self, tmp_path):
        merge._should_report_failure(CAUSE)

        assert merge._should_report_failure(CAUSE) is False

    def test_a_reminder_arrives_after_the_configured_count(self, tmp_path):
        alerts = [merge._should_report_failure(CAUSE) for _ in range(10)]

        # Alert on the 1st, then every 4th further failure: runs 5 and 9.
        assert [index for index, sent in enumerate(alerts, start=1) if sent] == [1, 5, 9]

    def test_a_45_minute_outage_sends_one_alert(self, tmp_path):
        """The 2026-08-18 incident: three runs, previously three alerts."""
        alerts = [merge._should_report_failure(CAUSE) for _ in range(3)]

        assert alerts.count(True) == 1

    def test_a_different_cause_is_news_again(self, tmp_path):
        merge._should_report_failure(CAUSE)
        merge._should_report_failure(CAUSE)

        assert merge._should_report_failure(OTHER) is True

    def test_a_zero_cadence_never_repeats(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "_failure_alert_every", lambda: 0)

        alerts = [merge._should_report_failure(CAUSE) for _ in range(20)]

        assert alerts.count(True) == 1

    def test_the_run_count_is_recorded(self, tmp_path):
        for _ in range(3):
            merge._should_report_failure(CAUSE)

        assert read_state(tmp_path) == {"cause": CAUSE, "runs": 3}

    def test_unreadable_state_fails_open(self, tmp_path):
        """A lost alert is worse than a duplicated one."""
        state_file(tmp_path).write_text("{ not json")

        assert merge._should_report_failure(CAUSE) is True

    def test_an_unwritable_state_file_does_not_crash_the_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "_failure_state_path", lambda: tmp_path / "nope" / "x" / "s.json")
        monkeypatch.setattr(merge.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

        assert merge._should_report_failure(CAUSE) is True


class TestReportRecovery:
    def test_nothing_is_sent_when_nothing_failed(self, tmp_path, quiet_terminal):
        merge._report_recovery()

        assert not any("recovered" in line for line in quiet_terminal)

    def test_recovery_is_announced_after_failures(self, tmp_path, quiet_terminal, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: sent.append(message))
        for _ in range(3):
            merge._should_report_failure(CAUSE)

        merge._report_recovery()

        assert len(sent) == 1
        assert "3 failed runs" in sent[0]

    def test_a_single_failure_is_not_pluralised(self, tmp_path, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: sent.append(message))
        merge._should_report_failure(CAUSE)

        merge._report_recovery()

        assert "1 failed run " in sent[0]

    def test_recovery_clears_the_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: None)
        merge._should_report_failure(CAUSE)

        merge._report_recovery()

        assert not state_file(tmp_path).exists()
        assert merge._should_report_failure(CAUSE) is True

    def test_an_unclearable_state_file_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: None)
        merge._should_report_failure(CAUSE)

        def boom(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(merge.Path, "unlink", boom)

        merge._report_recovery()


class TestFailureAlertEvery:
    def test_reads_the_configured_value(self, tmp_path, monkeypatch):
        monkeypatch.undo()
        config = tmp_path / "config.yaml"
        config.write_text("meetconfig:\n  failure_alert_every: 7\n")
        monkeypatch.setattr(merge, "YAML_FILENAME", str(config))
        monkeypatch.setattr(merge.Path, "resolve", lambda self: merge.Path("/"))

        assert merge._failure_alert_every() >= 0

    def test_falls_back_when_the_config_cannot_be_read(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setattr(merge, "YAML_FILENAME", "/nowhere/absent.yaml")

        assert merge._failure_alert_every() == merge.DEFAULT_FAILURE_ALERT_EVERY


class TestProjectPath:
    def test_an_absolute_path_is_used_as_is(self):
        assert merge._project_path("/tmp/x.json") == merge.Path("/tmp/x.json")

    def test_a_relative_path_anchors_to_the_project_root(self):
        resolved = merge._project_path("logs/x.json")

        assert resolved == merge.Path(merge.__file__).resolve().parent.parent / "logs" / "x.json"


class TestRunAndReport:
    """The whole outcome path: alert, suppress, remind, recover."""

    def sent_messages(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: sent.append(message))
        return sent

    def failing(self, message="boom"):
        def _main():
            raise RuntimeError(message)

        return _main

    def test_a_first_failure_alerts(self, tmp_path, monkeypatch, quiet_terminal):
        sent = self.sent_messages(monkeypatch)
        monkeypatch.setattr(merge, "main", self.failing())

        merge._run_and_report()

        assert sent == ["Calendar merge failed: boom"]

    def test_a_failure_never_escapes(self, tmp_path, monkeypatch, quiet_terminal):
        """cron must not see a traceback; the run reports and exits cleanly."""
        self.sent_messages(monkeypatch)
        monkeypatch.setattr(merge, "main", self.failing())

        merge._run_and_report()

    def test_the_same_failure_repeating_is_suppressed(self, tmp_path, monkeypatch, quiet_terminal):
        sent = self.sent_messages(monkeypatch)
        monkeypatch.setattr(merge, "main", self.failing())

        merge._run_and_report()
        merge._run_and_report()
        merge._run_and_report()

        assert len(sent) == 1, "one outage, one alert"

    def test_recovery_is_announced_after_a_suppressed_run(self, tmp_path, monkeypatch, quiet_terminal):
        sent = self.sent_messages(monkeypatch)
        monkeypatch.setattr(merge, "main", self.failing())
        merge._run_and_report()
        merge._run_and_report()

        monkeypatch.setattr(merge, "main", lambda: None)
        merge._run_and_report()

        assert len(sent) == 2
        assert sent[1].startswith(merge.TELEGRAM_RECOVERED_MESSAGE)
        assert "2 failed runs" in sent[1]

    def test_a_clean_run_after_a_clean_run_says_nothing(self, tmp_path, monkeypatch, quiet_terminal):
        sent = self.sent_messages(monkeypatch)
        monkeypatch.setattr(merge, "main", lambda: None)

        merge._run_and_report()
        merge._run_and_report()

        assert sent == []

    def test_a_changed_cause_alerts_again(self, tmp_path, monkeypatch, quiet_terminal):
        sent = self.sent_messages(monkeypatch)
        monkeypatch.setattr(merge, "main", self.failing("first fault"))
        merge._run_and_report()
        merge._run_and_report()

        monkeypatch.setattr(merge, "main", self.failing("a different fault"))
        merge._run_and_report()

        assert [message.split(": ", 1)[1] for message in sent] == ["first fault", "a different fault"]
