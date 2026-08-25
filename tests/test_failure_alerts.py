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


def recording_sender(sent, delivered=True):
    """A send_telegram_message stub that records and reports delivery.

    The real one returns whether the message actually went out; a stub returning None
    would leave every alert unmarked and every repeat re-alerted.
    """

    def _send(message, **kwargs):
        sent.append(message)
        return delivered

    return _send


def alert_once(cause):
    """Record a failure and mark it delivered, as a successful run would."""
    runs, should_alert = merge._record_failure(cause)
    if should_alert:
        merge._mark_failure_alerted(cause, runs)
    return should_alert


CAUSE = "Unable to load events from iCloud (PyiCloudAPIResponseException: Auth required. (500))"
OTHER = "Unable to download calendar vf [0] (ConnectionError: timed out)"


@pytest.fixture
def fixed_cadence(monkeypatch):
    """Pin the reminder cadence so tests do not depend on a real config.yaml.

    Deliberately not autouse. It was, and the tests that needed the real function had to
    call `monkeypatch.undo()` -- which shares the function-scoped monkeypatch with the
    autouse isolation fixtures and therefore switched off `isolated_failure_state` and
    `clean_env` as well, pointing the state file back at the repository.
    """
    monkeypatch.setattr(merge, "_failure_alert_every", lambda: 4)


class TestRecordFailure:
    def test_the_first_failure_always_alerts(self, tmp_path):
        assert merge._record_failure(CAUSE)[1] is True

    def test_the_same_cause_repeating_stays_quiet(self, tmp_path):
        alert_once(CAUSE)

        assert merge._record_failure(CAUSE)[1] is False

    def test_a_reminder_arrives_after_the_configured_count(self, tmp_path):
        alerts = [alert_once(CAUSE) for _ in range(10)]

        # Alert on the 1st, then every 4th further failure: runs 5 and 9.
        assert [index for index, sent in enumerate(alerts, start=1) if sent] == [1, 5, 9]

    def test_a_45_minute_outage_sends_one_alert(self, tmp_path):
        """The 2026-08-18 incident: three runs, previously three alerts."""
        alerts = [alert_once(CAUSE) for _ in range(3)]

        assert alerts.count(True) == 1

    def test_a_different_cause_is_news_again(self, tmp_path):
        alert_once(CAUSE)
        alert_once(CAUSE)

        assert merge._record_failure(OTHER)[1] is True

    def test_a_zero_cadence_never_repeats(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "_failure_alert_every", lambda: 0)

        alerts = [alert_once(CAUSE) for _ in range(20)]

        assert alerts.count(True) == 1

    def test_the_run_count_is_recorded(self, tmp_path):
        for _ in range(3):
            alert_once(CAUSE)

        assert read_state(tmp_path) == {"cause": CAUSE, "runs": 3, "alerted_at": 1}

    def test_unreadable_state_fails_open(self, tmp_path):
        """A lost alert is worse than a duplicated one."""
        state_file(tmp_path).write_text("{ not json")

        assert merge._record_failure(CAUSE)[1] is True

    @pytest.mark.parametrize(
        "malformed",
        ["[]", "5", '"oops"', "null", '{"cause": "X", "runs": null}', '{"cause": "X", "runs": "many"}'],
        ids=["list", "int", "string", "null", "runs-null", "runs-not-numeric"],
    )
    def test_malformed_but_valid_json_fails_open(self, tmp_path, fixed_cadence, malformed):
        """These parse fine, so only a shape check catches them.

        Left unvalidated they raised from `.get(...)` or `int(...)` *inside* the
        failure handler, escaping it and losing the alert it was handling.
        """
        state_file(tmp_path).write_text(malformed)

        assert merge._record_failure(CAUSE)[1] is True

    def test_a_malformed_state_never_escapes_the_handler(self, tmp_path, monkeypatch, quiet_terminal):
        """The end-to-end version: cron must still get an alert, not a traceback."""
        sent: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", recording_sender(sent))
        monkeypatch.setattr(merge, "main", lambda: (_ for _ in ()).throw(RuntimeError("iCloud down")))
        state_file(tmp_path).write_text("[]")

        merge._run_and_report()

        assert sent == ["Calendar merge failed: iCloud down"]

    def test_an_undelivered_alert_is_retried_next_run(self, tmp_path, monkeypatch, quiet_terminal):
        """send_telegram_message swallows transport errors, so delivery must be checked.

        Marking a failure reported when it never arrived suppressed every repeat --
        hiding the outage for an hour, or forever with `failure_alert_every: 0`.
        """
        attempts: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", recording_sender(attempts, delivered=False))
        monkeypatch.setattr(merge, "main", lambda: (_ for _ in ()).throw(RuntimeError("iCloud down")))

        merge._run_and_report()
        merge._run_and_report()

        assert len(attempts) == 2, "an undelivered alert must be attempted again"

    def test_an_unwritable_state_file_does_not_crash_the_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "_failure_state_path", lambda: tmp_path / "nope" / "x" / "s.json")
        monkeypatch.setattr(merge.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

        assert merge._record_failure(CAUSE)[1] is True


class TestReportRecovery:
    def test_nothing_is_sent_when_nothing_failed(self, tmp_path, quiet_terminal):
        merge._report_recovery()

        assert not any("recovered" in line for line in quiet_terminal)

    def test_recovery_is_announced_after_failures(self, tmp_path, quiet_terminal, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", recording_sender(sent))
        for _ in range(3):
            alert_once(CAUSE)

        merge._report_recovery()

        assert len(sent) == 1
        assert "3 failed runs" in sent[0]

    def test_a_single_failure_is_not_pluralised(self, tmp_path, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(merge, "send_telegram_message", recording_sender(sent))
        alert_once(CAUSE)

        merge._report_recovery()

        assert "1 failed run " in sent[0]

    def test_recovery_clears_the_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: True)
        alert_once(CAUSE)

        merge._report_recovery()

        assert not state_file(tmp_path).exists()
        assert merge._record_failure(CAUSE)[1] is True

    def test_an_unclearable_state_file_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(merge, "send_telegram_message", lambda message, **k: True)
        alert_once(CAUSE)

        def boom(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(merge.Path, "unlink", boom)

        merge._report_recovery()


class TestFailureAlertEvery:
    """The only path that reads `failure_alert_every` from config.yaml."""

    def write_config(self, tmp_path, monkeypatch, body):
        config = tmp_path / "config.yaml"
        config.write_text(body)
        monkeypatch.setattr(merge, "_project_path", lambda _name: config)
        return config

    def test_reads_the_configured_value(self, tmp_path, monkeypatch):
        """Previously asserted `>= 0`, which every possible return satisfies.

        The fixture also wrote the wrong section name, so the function fell back and
        the configured value was never read at all -- a mutation replacing the body
        with the default kept the whole file green.
        """
        self.write_config(tmp_path, monkeypatch, f"{merge.YAML_SECTION_GENERAL}:\n  failure_alert_every: 7\n")

        assert merge._failure_alert_every() == 7

    def test_a_negative_value_is_clamped(self, tmp_path, monkeypatch):
        self.write_config(tmp_path, monkeypatch, f"{merge.YAML_SECTION_GENERAL}:\n  failure_alert_every: -3\n")

        assert merge._failure_alert_every() == 0

    def test_a_missing_setting_falls_back(self, tmp_path, monkeypatch):
        self.write_config(tmp_path, monkeypatch, f"{merge.YAML_SECTION_GENERAL}:\n  skip_days: 5, 6\n")

        assert merge._failure_alert_every() == merge.DEFAULT_FAILURE_ALERT_EVERY

    def test_a_non_numeric_value_falls_back(self, tmp_path, monkeypatch):
        self.write_config(tmp_path, monkeypatch, f"{merge.YAML_SECTION_GENERAL}:\n  failure_alert_every: often\n")

        assert merge._failure_alert_every() == merge.DEFAULT_FAILURE_ALERT_EVERY

    def test_falls_back_when_the_config_cannot_be_read(self, tmp_path, monkeypatch):
        """Read on the failure path, so a broken config must not mask the real error."""
        monkeypatch.setattr(merge, "_project_path", lambda _name: tmp_path / "absent.yaml")

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
        monkeypatch.setattr(merge, "send_telegram_message", recording_sender(sent))
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
