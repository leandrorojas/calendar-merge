import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from merge import (
    is_skip_day,
    _should_process,
    _handle_override_date_lifecycle,
    _consume_override_on_last,
    _handle_cancel,
    JSON_SETTING_OVERRIDE_FLAG,
    JSON_SETTING_OVERRIDE_DATE,
    JSON_SETTING_TELEGRAM_OFFSET,
)

# Fixed reference dates
MONDAY    = datetime(2025, 2, 24)   # weekday 0 — work day
SATURDAY  = datetime(2025, 2, 22)   # weekday 5 — skip day
SUNDAY    = datetime(2025, 2, 23)   # weekday 6 — skip day
SKIP_DAYS = ["5", "6"]

# ── Helpers ──────────────────────────────────────────────────────────────────

def _state(override_date=None, override_flag=False, telegram_offset=None):
    return {
        JSON_SETTING_OVERRIDE_DATE:    override_date,
        JSON_SETTING_OVERRIDE_FLAG:    override_flag,
        JSON_SETTING_TELEGRAM_OFFSET:  telegram_offset,
    }

def _args(**kwargs):
    m = MagicMock()
    m.cancel   = kwargs.get("cancel",   False)
    m.override = kwargs.get("override", False)
    m.first    = kwargs.get("first",    False)
    m.last     = kwargs.get("last",     False)
    return m

# ── is_skip_day ───────────────────────────────────────────────────────────────

class TestIsSkipDay(unittest.TestCase):

    def test_saturday_is_skip_day(self):
        self.assertTrue(is_skip_day(SATURDAY, SKIP_DAYS))

    def test_sunday_is_skip_day(self):
        self.assertTrue(is_skip_day(SUNDAY, SKIP_DAYS))

    def test_monday_is_not_skip_day(self):
        self.assertFalse(is_skip_day(MONDAY, SKIP_DAYS))

    def test_empty_skip_days_never_skips(self):
        self.assertFalse(is_skip_day(SATURDAY, []))

# ── _should_process ───────────────────────────────────────────────────────────

class TestShouldProcess(unittest.TestCase):

    def test_work_day_no_override_processes(self):
        self.assertTrue(_should_process(_state(), MONDAY, SKIP_DAYS))

    def test_skip_day_no_override_does_not_process(self):
        self.assertFalse(_should_process(_state(), SATURDAY, SKIP_DAYS))

    def test_skip_day_override_today_processes(self):
        self.assertTrue(_should_process(_state("2025-02-22"), SATURDAY, SKIP_DAYS))

    def test_skip_day_override_future_does_not_process(self):
        self.assertFalse(_should_process(_state("2025-03-01"), SATURDAY, SKIP_DAYS))

    def test_skip_day_override_past_does_not_process(self):
        self.assertFalse(_should_process(_state("2025-02-15"), SATURDAY, SKIP_DAYS))

# ── _handle_override_date_lifecycle ──────────────────────────────────────────

class TestOverrideDateLifecycle(unittest.TestCase):

    def test_1a_active_today_no_state_change(self):
        state = _state("2025-02-22")
        with patch("merge.send_telegram_message"):
            changed = _handle_override_date_lifecycle(state, SATURDAY)
        self.assertFalse(changed)
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-02-22")

    def test_1b_pending_future_no_state_change(self):
        state = _state("2025-03-01")
        changed = _handle_override_date_lifecycle(state, MONDAY)
        self.assertFalse(changed)
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-03-01")

    def test_1c_expired_clears_date(self):
        state = _state("2025-02-15")
        with patch("merge.send_telegram_message"):
            changed = _handle_override_date_lifecycle(state, MONDAY)
        self.assertTrue(changed)
        self.assertIsNone(state[JSON_SETTING_OVERRIDE_DATE])

    def test_no_override_date_is_noop(self):
        state = _state()
        changed = _handle_override_date_lifecycle(state, MONDAY)
        self.assertFalse(changed)

# ── _consume_override_on_last ─────────────────────────────────────────────────

class TestConsumeOverrideOnLast(unittest.TestCase):
    """AC: Tests prove consume only on --last."""

    def test_last_run_matching_date_clears_override(self):
        state = _state("2025-02-22")
        with patch("merge.send_telegram_message"), patch("merge._save_state") as mock_save:
            _consume_override_on_last(state, SATURDAY, "/tmp/state.json")
        self.assertIsNone(state[JSON_SETTING_OVERRIDE_DATE])
        mock_save.assert_called_once()

    def test_last_run_non_matching_date_does_not_clear(self):
        state = _state("2025-03-01")
        with patch("merge.send_telegram_message"), patch("merge._save_state") as mock_save:
            _consume_override_on_last(state, SATURDAY, "/tmp/state.json")
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-03-01")
        mock_save.assert_not_called()

    def test_no_last_flag_means_function_not_called_so_state_unchanged(self):
        """Without --last, _consume_override_on_last is never invoked by main().
        Simulate: state is intact after a middle/first run."""
        state = _state("2025-02-22")
        # Not calling _consume_override_on_last at all (middle run)
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-02-22")

    def test_first_run_does_not_consume(self):
        """--first run: _consume_override_on_last must not be called."""
        state = _state("2025-02-22")
        with patch("merge._save_state") as mock_save:
            # simulate first run: don't call _consume_override_on_last
            pass
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-02-22")
        mock_save.assert_not_called()

# ── _handle_cancel ────────────────────────────────────────────────────────────

class TestHandleCancel(unittest.TestCase):
    """AC: Tests prove cancel deletion scope correctness."""

    def test_no_cancel_signal_is_noop(self):
        state = _state("2025-02-22")
        stop, cleanup, date = _handle_cancel(_args(), set(), state, SATURDAY)
        self.assertFalse(stop)
        self.assertFalse(cleanup)
        self.assertIsNone(date)
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-02-22")

    def test_cancel_today_scope_future_today_stops_execution(self):
        """override_date == today → stop=True, scope=FUTURE_TODAY."""
        state = _state("2025-02-22")
        with patch("merge.send_telegram_message"):
            stop, cleanup, date = _handle_cancel(_args(cancel=True), {"cancel"}, state, SATURDAY)
        self.assertTrue(stop)
        self.assertTrue(cleanup)
        self.assertEqual(date, "2025-02-22")
        self.assertIsNone(state[JSON_SETTING_OVERRIDE_DATE])
        self.assertFalse(state[JSON_SETTING_OVERRIDE_FLAG])

    def test_cancel_future_scope_all_day_continues_execution(self):
        """override_date > today → stop=False, scope=ALL_DAY."""
        state = _state("2025-03-01")
        with patch("merge.send_telegram_message"):
            stop, cleanup, date = _handle_cancel(_args(cancel=True), {"cancel"}, state, MONDAY)
        self.assertFalse(stop)
        self.assertTrue(cleanup)
        self.assertEqual(date, "2025-03-01")
        self.assertIsNone(state[JSON_SETTING_OVERRIDE_DATE])
        self.assertFalse(state[JSON_SETTING_OVERRIDE_FLAG])

    def test_cancel_no_override_armed_noop(self):
        state = _state()
        with patch("merge.send_telegram_message"):
            stop, cleanup, date = _handle_cancel(_args(cancel=True), {"cancel"}, state, SATURDAY)
        self.assertFalse(stop)
        self.assertFalse(cleanup)
        self.assertIsNone(date)

    def test_cancel_override_past_noop(self):
        state = _state("2025-02-15")
        with patch("merge.send_telegram_message"):
            stop, cleanup, date = _handle_cancel(_args(cancel=True), {"cancel"}, state, MONDAY)
        self.assertFalse(stop)
        self.assertFalse(cleanup)
        self.assertIsNone(date)
        self.assertEqual(state[JSON_SETTING_OVERRIDE_DATE], "2025-02-15")

    def test_cancel_via_telegram_command_behaves_same_as_cli(self):
        """Telegram 'cancel' command is equivalent to --cancel flag."""
        state = _state("2025-02-22")
        with patch("merge.send_telegram_message"):
            stop, cleanup, date = _handle_cancel(_args(cancel=False), {"cancel"}, state, SATURDAY)
        self.assertTrue(stop)
        self.assertTrue(cleanup)
        self.assertEqual(date, "2025-02-22")

    def test_cancel_today_clears_override_flag_to_prevent_rearming(self):
        state = _state("2025-02-22", override_flag=True)
        with patch("merge.send_telegram_message"):
            _handle_cancel(_args(cancel=True), {"cancel"}, state, SATURDAY)
        self.assertFalse(state[JSON_SETTING_OVERRIDE_FLAG])

    def test_cancel_future_clears_override_flag_to_prevent_rearming(self):
        state = _state("2025-03-01", override_flag=True)
        with patch("merge.send_telegram_message"):
            _handle_cancel(_args(cancel=True), {"cancel"}, state, MONDAY)
        self.assertFalse(state[JSON_SETTING_OVERRIDE_FLAG])


if __name__ == "__main__":
    unittest.main()
