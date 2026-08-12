"""Tests for logging setup, ANSI stripping, and print_step mirroring."""

import logging
from logging.handlers import RotatingFileHandler

import merge


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


class TestConfigureLogging:
    def test_creates_rotating_handler_at_configured_path(self, tmp_path, monkeypatch):
        log_file = tmp_path / "nested" / "merge.log"
        monkeypatch.setenv(merge.ENV_LOG_FILE, str(log_file))

        merge._configure_logging()

        assert len(merge.logger.handlers) == 1
        handler = merge.logger.handlers[0]
        assert isinstance(handler, RotatingFileHandler)
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

        assert len(merge.logger.handlers) == 1

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

        handler = merge.logger.handlers[0]
        assert isinstance(handler, RotatingFileHandler)
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
