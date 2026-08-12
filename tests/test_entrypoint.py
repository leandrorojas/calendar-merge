"""Tests for the `if __name__ == "__main__"` entry-point block.

The block is executed by running the module source with `run_name="__main__"`.
`merge.term` and the `calendar-merge` logger are process-global, so the
`quiet_terminal` and `reset_logger` fixtures still intercept the fresh run.

The fresh module re-executes its own imports, so patching `pyfangs.yaml`
reaches it. That is deliberate: the config load is forced to fail so the run
never touches iCloud, regardless of whether the developer has a real
`config.yaml` and `.env` in the repo root.
"""

import logging
import runpy
from pathlib import Path

import pytest

import merge

MERGE_SOURCE = str(Path(merge.__file__).resolve())


def run_entrypoint():
    runpy.run_path(MERGE_SOURCE, run_name="__main__")


@pytest.fixture(autouse=True)
def entrypoint_env(tmp_path, monkeypatch):
    """Keep the run hermetic: log to tmp_path, no CLI flags, config load fails."""
    monkeypatch.setenv(merge.ENV_LOG_FILE, str(tmp_path / "entrypoint.log"))
    monkeypatch.setattr("sys.argv", ["calendar-merge"])

    class UnopenableYaml:
        def __init__(self, path):
            raise OSError("simulated missing config")

    # Patch the source module so the freshly executed merge picks this up.
    monkeypatch.setattr("pyfangs.yaml.YamlHelper", UnopenableYaml)
    return tmp_path


class TestEntrypoint:
    def test_reports_failure_instead_of_crashing(self, quiet_terminal):
        """A failing merge must be reported, not raised out of the process."""
        run_entrypoint()

        assert any("An error occurred during the merge process" in line for line in quiet_terminal)

    def test_surfaces_the_underlying_error_message(self, quiet_terminal):
        run_entrypoint()

        assert any("Unable to open YAML configuration" in line for line in quiet_terminal)

    def test_logs_the_traceback(self, captured_logs):
        run_entrypoint()

        failures = [record for record in captured_logs if record.levelno == logging.ERROR]
        assert any(record.exc_info for record in failures), "expected logger.exception to attach exc_info"
        assert any("Merge process failed" in record.getMessage() for record in failures)

    def test_logs_start_and_completion(self, captured_logs):
        run_entrypoint()

        messages = [record.getMessage() for record in captured_logs]
        assert any("calendar-merge started" in message for message in messages)
        assert any("completed in" in message for message in messages)

    def test_attempts_telegram_notification_on_failure(self, quiet_terminal):
        # Telegram is unconfigured (clean_env), so the notifier reports the
        # missing token instead of raising -- proving the failure path tried
        # to notify the user.
        run_entrypoint()

        assert any("token not configured" in line for line in quiet_terminal)

    def test_writes_a_log_file(self, entrypoint_env):
        run_entrypoint()

        log_file = entrypoint_env / "entrypoint.log"
        assert log_file.exists()
        assert "calendar-merge started" in log_file.read_text()

    def test_never_reaches_icloud(self, monkeypatch, quiet_terminal):
        """Config failure must short-circuit before any authentication."""
        attempted = []
        monkeypatch.setattr("pyicloud.PyiCloudService", lambda *a, **k: attempted.append(a))

        run_entrypoint()

        assert attempted == []
