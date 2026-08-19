"""Tests for logging setup, ANSI stripping, and print_step mirroring."""

import logging
from itertools import pairwise
from logging.handlers import RotatingFileHandler

import merge


def rotating_handlers():
    """Our own handlers only.

    pytest attaches a LogCaptureHandler directly to loggers with
    propagate=False, so the list is not ours alone to index or count.
    """
    return [h for h in merge.logger.handlers if isinstance(h, RotatingFileHandler)]


class TestStripAnsi:
    def test_removes_color_codes(self):
        colored = merge.term.TerminalColors.red.value + "error" + merge.term.TerminalColors.reset.value
        assert merge._strip_ansi(colored) == "error"

    def test_leaves_plain_text_untouched(self):
        assert merge._strip_ansi("plain text") == "plain text"

    def test_strips_the_real_tag_constants(self):
        # The TAG_* constants embed terminal colors; log lines must come out clean.
        assert merge._strip_ansi(merge.TAG_ERROR) == "error"
        assert merge._strip_ansi(merge.TAG_2F_AUTH) == "2f_auth"
        assert merge._strip_ansi(merge.TAG_CALENDAR_MERGE) == "cal-merge"
        assert merge._strip_ansi(merge.TAG_ICLOUD_AUTH) == "icloud_auth"

    def test_handles_empty_string(self):
        assert merge._strip_ansi("") == ""


class TestDescribeError:
    """`_describe_error` is what makes a Telegram alert diagnosable."""

    def test_returns_the_message_when_there_is_no_cause(self):
        assert merge._describe_error(ValueError("plain failure")) == "plain failure"

    def test_falls_back_to_the_type_when_the_message_is_empty(self):
        assert merge._describe_error(RuntimeError()) == "RuntimeError"

    def test_appends_the_cause_type_and_message(self):
        try:
            try:
                raise KeyError("dsInfo")
            except KeyError as inner:
                raise RuntimeError("Unable to load events from iCloud") from inner
        except RuntimeError as err:
            described = merge._describe_error(err)

        assert described == "Unable to load events from iCloud (KeyError: 'dsInfo')"

    def test_reproduces_the_2026_08_18_outage_alert(self):
        """The alert that was unreadable: an Apple 500 behind a wrapper."""

        class PyiCloudAPIResponseException(Exception):
            pass

        cause = PyiCloudAPIResponseException("Authentication required for Account. (500)")
        err = RuntimeError("Unable to load events from iCloud")
        err.__cause__ = cause

        described = merge._describe_error(err)

        assert "PyiCloudAPIResponseException" in described
        assert "(500)" in described
        assert described.startswith("Unable to load events from iCloud (")

    def test_follows_a_chain_of_causes(self):
        first = ValueError("one")
        second = TypeError("two")
        second.__cause__ = first
        top = RuntimeError("top")
        top.__cause__ = second

        assert merge._describe_error(top) == "top (TypeError: two <- ValueError: one)"

    def test_stops_at_the_depth_limit(self):
        chain = [OSError("four"), KeyError("three"), TypeError("two"), ValueError("one")]
        for outer, inner in pairwise(chain):
            outer.__cause__ = inner
        top = RuntimeError("top")
        top.__cause__ = chain[0]

        described = merge._describe_error(top)

        assert described.count("<-") == merge.ERROR_CAUSE_DEPTH - 1
        assert "one" not in described

    def test_survives_a_cycle_of_causes(self):
        # A self-referential chain must terminate rather than hang the alert.
        first = ValueError("a")
        second = TypeError("b")
        first.__cause__ = second
        second.__cause__ = first

        assert merge._describe_error(first) == "a (TypeError: b)"

    def test_ignores_implicit_context(self):
        # Only `raise ... from err` is meaningful; __context__ is noise.
        err = RuntimeError("wrapper")
        err.__context__ = ValueError("incidental")

        assert merge._describe_error(err) == "wrapper"


class TestConfigureLogging:
    def test_creates_rotating_handler_at_configured_path(self, tmp_path, monkeypatch):
        log_file = tmp_path / "nested" / "merge.log"
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(log_file))

        merge._configure_logging()

        handlers = rotating_handlers()
        assert len(handlers) == 1
        handler = handlers[0]
        assert handler.maxBytes == merge.LOG_MAX_BYTES
        assert handler.backupCount == merge.LOG_BACKUP_COUNT

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        log_file = tmp_path / "does" / "not" / "exist" / "merge.log"
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(log_file))

        merge._configure_logging()

        assert log_file.parent.is_dir()

    def test_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(tmp_path / "merge.log"))

        merge._configure_logging()
        merge._configure_logging()
        merge._configure_logging()

        assert len(rotating_handlers()) == 1

    def test_honours_log_level_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(tmp_path / "merge.log"))
        monkeypatch.setenv(merge.ENV_LOG_LEVEL, "debug")  # lowercase on purpose

        merge._configure_logging()

        assert merge.logger.level == logging.DEBUG

    def test_falls_back_to_info_on_unknown_level(self, tmp_path, monkeypatch):
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(tmp_path / "merge.log"))
        monkeypatch.setenv(merge.ENV_LOG_LEVEL, "NOT_A_LEVEL")

        merge._configure_logging()

        assert merge.logger.level == logging.INFO

    def test_defaults_to_info_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(tmp_path / "merge.log"))

        merge._configure_logging()

        assert merge.logger.level == logging.INFO

    def test_does_not_propagate_to_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(tmp_path / "merge.log"))

        merge._configure_logging()

        assert merge.logger.propagate is False

    def test_relative_path_resolves_against_project_root(self, monkeypatch, tmp_path):
        # A relative CALENDAR_MERGE_LOG_FILE is anchored to the project root,
        # not the current working directory.
        monkeypatch.setenv(merge.ENV_LOG_FILE, "logs/from-relative.log")
        monkeypatch.chdir(tmp_path)

        merge._configure_logging()

        handler = rotating_handlers()[0]
        expected_root = merge.Path(merge.__file__).resolve().parent.parent
        assert merge.Path(handler.baseFilename) == expected_root / "logs" / "from-relative.log"

    def test_writes_message_to_file(self, tmp_path, monkeypatch):
        log_file = tmp_path / "merge.log"
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(log_file))

        merge._configure_logging()
        merge.logger.info("hello from test")
        for handler in merge.logger.handlers:
            handler.flush()

        assert "hello from test" in log_file.read_text()


class TestPrintStep:
    def test_writes_to_terminal(self, quiet_terminal):
        merge.print_step("cal-merge", "doing work")

        assert quiet_terminal == ["[cal-merge] doing work"]

    def test_mirrors_to_logger_at_info(self, captured_logs):
        merge.print_step(merge.TAG_CALENDAR_MERGE, "loading")

        assert len(captured_logs) == 1
        assert captured_logs[0].levelno == logging.INFO
        assert captured_logs[0].getMessage() == "[cal-merge] loading"

    def test_error_tag_logs_at_error_level(self, captured_logs):
        merge.print_step(merge.TAG_ERROR, "it broke")

        assert captured_logs[0].levelno == logging.ERROR
        assert captured_logs[0].getMessage() == "[error] it broke"

    def test_log_line_has_no_ansi_codes(self, captured_logs):
        merge.print_step(merge.TAG_ICLOUD_AUTH, "connecting")

        assert "\x1b[" not in captured_logs[0].getMessage()
