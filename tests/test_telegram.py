"""Tests for the Telegram send and poll helpers.

Async functions are driven with `asyncio.run` rather than pytest-asyncio to keep
the dev dependency set small.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import merge
from tests.conftest import FakeMessage, FakeNotifier, FakeUpdate, async_cm_factory, notifier_factory


def run(coro):
    return asyncio.run(coro)


class TestGetTelegramCredentials:
    def test_returns_pair_when_configured(self, telegram_configured):
        assert merge._get_telegram_credentials() == ("test-token", "test-chat")

    def test_returns_none_without_token(self, monkeypatch, quiet_terminal):
        monkeypatch.setenv(merge.ENV_TELEGRAM_CHAT_ID, "chat")

        assert merge._get_telegram_credentials() is None
        assert any("token not configured" in line for line in quiet_terminal)

    def test_returns_none_without_chat_id(self, monkeypatch, quiet_terminal):
        monkeypatch.setenv(merge.ENV_TELEGRAM_TOKEN, "token")

        assert merge._get_telegram_credentials() is None
        assert any("chat id not configured" in line for line in quiet_terminal)


class TestSendViaNotifier:
    def test_prefers_send_message(self):
        notifier = FakeNotifier()

        run(merge._send_via_notifier(notifier, "hello", disable_notification=True))

        assert notifier.sent == [("hello", True)]

    def test_falls_back_to_bot_when_silencing(self):
        """A notifier without send_message must route through .bot to silence."""
        calls: dict[str, object] = {}

        class BotOnly:
            chat_id = "chat-1"

            class bot:
                @staticmethod
                async def send_message(chat_id, text, disable_notification):
                    calls.update(chat_id=chat_id, text=text, silent=disable_notification)

        run(merge._send_via_notifier(BotOnly(), "quiet", disable_notification=True))

        assert calls == {"chat_id": "chat-1", "text": "quiet", "silent": True}

    def test_falls_back_to_send_when_not_silencing(self):
        received = []

        class SendOnly:
            async def send(self, message):
                received.append(message)

        run(merge._send_via_notifier(SendOnly(), "loud", disable_notification=False))

        assert received == ["loud"]


class TestCloseNotifier:
    def test_awaits_async_close(self):
        notifier = FakeNotifier()

        run(merge._close_notifier(notifier))

        assert notifier.closed is True

    def test_calls_sync_close(self):
        state = {"closed": False}

        class SyncClose:
            def close(self):
                state["closed"] = True

        run(merge._close_notifier(SyncClose()))

        assert state["closed"] is True

    def test_tolerates_missing_close(self):
        class NoClose:
            pass

        run(merge._close_notifier(NoClose()))  # must not raise


class TestSendTelegramMessageAsync:
    def test_ignores_empty_message(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        run(merge.send_telegram_message_async(""))

        assert notifier.sent == []

    def test_returns_quietly_when_unconfigured(self, monkeypatch, quiet_terminal):
        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        run(merge.send_telegram_message_async("hi"))

        assert notifier.sent == []

    def test_sends_via_plain_notifier_and_closes(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        run(merge.send_telegram_message_async("hello"))

        assert notifier.sent == [("hello", False)]
        assert notifier.closed is True
        assert notifier.token == "test-token"
        assert notifier.chat_id == "test-chat"

    def test_sends_via_context_manager(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", async_cm_factory(notifier))

        run(merge.send_telegram_message_async("ctx", disable_notification=True))

        assert notifier.sent == [("ctx", True)]
        assert notifier.closed is True

    def test_closes_notifier_even_when_send_raises(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()

        async def boom(message, disable_notification=False):
            raise RuntimeError("network down")

        monkeypatch.setattr(notifier, "send_message", boom)
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        with contextlib.suppress(RuntimeError):
            run(merge.send_telegram_message_async("hello"))

        assert notifier.closed is True


class TestSendTelegramMessage:
    def test_ignores_empty_message(self, monkeypatch):
        called = []
        monkeypatch.setattr(merge, "send_telegram_message_async", lambda *a, **k: called.append(a))

        merge.send_telegram_message("")

        assert called == []

    def test_runs_coroutine_outside_event_loop(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        merge.send_telegram_message("sync path")

        assert notifier.sent == [("sync path", False)]

    def test_schedules_task_inside_running_loop(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        async def driver():
            merge.send_telegram_message("loop path")
            # Yield so the created task gets a chance to run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        run(driver())

        assert notifier.sent == [("loop path", False)]


class TestUsableReplyText:
    """Per-update filtering, extracted from the poll loop to cut its complexity."""

    def test_returns_text_of_a_fresh_reply(self):
        mark = datetime.now(UTC)
        upd = FakeUpdate(1, FakeMessage("123456", date=mark + timedelta(seconds=1)))

        assert merge._usable_reply_text(upd, mark, None) == "123456"

    def test_ignores_update_without_a_message(self):
        assert merge._usable_reply_text(FakeUpdate(1, None), datetime.now(UTC), None) is None

    def test_ignores_message_without_text(self):
        mark = datetime.now(UTC)
        upd = FakeUpdate(1, FakeMessage(None, date=mark))

        assert merge._usable_reply_text(upd, mark, None) is None

    def test_ignores_reply_sent_before_the_prompt(self):
        mark = datetime.now(UTC)
        upd = FakeUpdate(1, FakeMessage("123456", date=mark - timedelta(minutes=5)))

        assert merge._usable_reply_text(upd, mark, None) is None

    def test_reads_a_naive_timestamp_as_utc(self):
        mark = datetime.now(UTC)
        naive = (mark + timedelta(seconds=1)).replace(tzinfo=None)
        upd = FakeUpdate(1, FakeMessage("123456", date=naive))

        assert merge._usable_reply_text(upd, mark, None) == "123456"

    def test_ignores_reply_without_a_timestamp(self):
        mark = datetime.now(UTC)
        upd = FakeUpdate(1, FakeMessage("123456", date=None))

        assert merge._usable_reply_text(upd, mark, None) is None

    def test_applies_the_validator(self, quiet_terminal):
        mark = datetime.now(UTC)
        upd = FakeUpdate(1, FakeMessage("nope", date=mark + timedelta(seconds=1)))

        assert merge._usable_reply_text(upd, mark, merge._is_two_factor_code) is None
        assert any("not a 6-digit code" in line for line in quiet_terminal)

    def test_accepts_a_reply_the_validator_allows(self):
        mark = datetime.now(UTC)
        upd = FakeUpdate(1, FakeMessage("123456", date=mark + timedelta(seconds=1)))

        assert merge._usable_reply_text(upd, mark, merge._is_two_factor_code) == "123456"


class TestPollTelegramUpdates:
    def test_returns_text_of_message_after_mark(self):
        mark = datetime.now(UTC)
        msg = FakeMessage("123456", date=mark + timedelta(seconds=1))
        notifier = FakeNotifier(updates_batches=[[FakeUpdate(1, msg)]])

        result = run(merge._poll_telegram_updates(notifier, mark, timeout_seconds=5))

        assert result == "123456"

    def test_ignores_message_from_before_mark(self):
        """A reply predating the prompt must not be accepted as the answer."""
        mark = datetime.now(UTC)
        batch = [
            FakeUpdate(1, FakeMessage("stale-code", date=mark - timedelta(minutes=5))),
            FakeUpdate(2, FakeMessage("fresh-code", date=mark + timedelta(seconds=1))),
        ]
        notifier = FakeNotifier(updates_batches=[batch])

        result = run(merge._poll_telegram_updates(notifier, mark, timeout_seconds=5))

        assert result == "fresh-code"

    def test_treats_naive_message_date_as_utc(self):
        mark = datetime.now(UTC)
        naive = FakeMessage("naive", date=(mark + timedelta(seconds=1)).replace(tzinfo=None))
        notifier = FakeNotifier(updates_batches=[[FakeUpdate(1, naive)]])

        assert run(merge._poll_telegram_updates(notifier, mark, timeout_seconds=5)) == "naive"

    def test_skips_updates_without_text(self):
        mark = datetime.now(UTC)
        batch = [
            FakeUpdate(1, None),
            FakeUpdate(2, FakeMessage(None, date=mark + timedelta(seconds=1))),
            FakeUpdate(3, FakeMessage("real", date=mark + timedelta(seconds=2))),
        ]
        notifier = FakeNotifier(updates_batches=[batch])

        assert run(merge._poll_telegram_updates(notifier, mark, timeout_seconds=5)) == "real"

    def test_advances_offset_between_batches(self):
        mark = datetime.now(UTC)
        first = [FakeUpdate(7, FakeMessage(None, date=mark))]
        second = [FakeUpdate(9, FakeMessage("second", date=mark + timedelta(seconds=1)))]
        notifier = FakeNotifier(updates_batches=[first, second])

        assert run(merge._poll_telegram_updates(notifier, mark, timeout_seconds=5)) == "second"
        # Offset must be last_update_id + 1 so batches are not re-read.
        assert notifier.get_updates_calls[0]["offset"] is None
        assert notifier.get_updates_calls[1]["offset"] == 8

    def test_keeps_polling_after_an_empty_batch(self, monkeypatch):
        """An empty poll must back off and retry, not give up."""
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(merge.asyncio, "sleep", fake_sleep)
        mark = datetime.now(UTC)
        reply = FakeMessage("late-code", date=mark + timedelta(seconds=1))
        notifier = FakeNotifier(updates_batches=[[], [], [FakeUpdate(1, reply)]])

        result = run(merge._poll_telegram_updates(notifier, mark, timeout_seconds=5))

        assert result == "late-code"
        assert slept == [1, 1]

    def test_ignores_replies_the_validator_rejects(self, quiet_terminal):
        """Chatter must not consume the prompt.

        The first reply is not a code, so polling continues rather than handing
        "what code?" back to be submitted to Apple.
        """
        mark = datetime.now(UTC)
        batch = [
            FakeUpdate(1, FakeMessage("what code?", date=mark + timedelta(seconds=1))),
            FakeUpdate(2, FakeMessage("123456", date=mark + timedelta(seconds=2))),
        ]
        notifier = FakeNotifier(updates_batches=[batch])

        result = run(merge._poll_telegram_updates(notifier, mark, 5, merge._is_two_factor_code))

        assert result == "123456"
        assert any("not a 6-digit code" in line for line in quiet_terminal)

    def test_accepts_any_text_when_no_validator_given(self):
        mark = datetime.now(UTC)
        notifier = FakeNotifier(updates_batches=[[FakeUpdate(1, FakeMessage("anything", date=mark))]])

        assert run(merge._poll_telegram_updates(notifier, mark, 5)) == "anything"

    def test_times_out_and_reports(self, quiet_terminal):
        notifier = FakeNotifier(updates_batches=[])

        result = run(merge._poll_telegram_updates(notifier, datetime.now(UTC), timeout_seconds=0))

        assert result is None
        assert any("Timed out waiting for Telegram reply" in line for line in quiet_terminal)

    def test_timeout_is_bounded(self, quiet_terminal):
        """A zero timeout must not poll forever."""
        notifier = FakeNotifier(updates_batches=[])

        run(merge._poll_telegram_updates(notifier, datetime.now(UTC), timeout_seconds=0))

        # Deadline already passed, so the loop body never runs.
        assert notifier.get_updates_calls == []


class TestWaitForTelegramReply:
    def test_returns_none_when_unconfigured(self, quiet_terminal):
        assert run(merge._wait_for_telegram_reply("give me the code")) is None

    def test_sends_prompt_then_returns_reply(self, telegram_configured, monkeypatch):
        mark_reply = FakeMessage("999888", date=datetime.now(UTC) + timedelta(seconds=1))
        notifier = FakeNotifier(updates_batches=[[FakeUpdate(1, mark_reply)]])
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        result = run(merge._wait_for_telegram_reply("give me the code"))

        assert result == "999888"
        assert notifier.sent == [("give me the code", False)]
        assert notifier.closed is True

    def test_invokes_after_send_callback_after_prompt(self, telegram_configured, monkeypatch):
        order = []
        reply = FakeMessage("1", date=datetime.now(UTC) + timedelta(seconds=1))
        notifier = FakeNotifier(updates_batches=[[FakeUpdate(1, reply)]])

        async def tracking_send(message, disable_notification=False):
            order.append("sent")

        monkeypatch.setattr(notifier, "send_message", tracking_send)
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        run(merge._wait_for_telegram_reply("prompt", after_send=lambda: order.append("after_send")))

        # The push must be triggered only after the user has been prompted.
        assert order == ["sent", "after_send"]

    def test_works_through_context_manager_factory(self, telegram_configured, monkeypatch):
        reply = FakeMessage("ctx-code", date=datetime.now(UTC) + timedelta(seconds=1))
        notifier = FakeNotifier(updates_batches=[[FakeUpdate(1, reply)]])
        monkeypatch.setattr(merge.tg, "TelegramNotifier", async_cm_factory(notifier))

        assert run(merge._wait_for_telegram_reply("prompt")) == "ctx-code"
        assert notifier.closed is True

    def test_closes_notifier_when_polling_raises(self, telegram_configured, monkeypatch):
        notifier = FakeNotifier()

        async def boom(*a, **k):
            raise RuntimeError("poll failed")

        monkeypatch.setattr(notifier, "get_updates", boom)
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))

        with contextlib.suppress(RuntimeError):
            run(merge._wait_for_telegram_reply("prompt"))

        assert notifier.closed is True

    def test_uses_the_module_timeout_constant(self, telegram_configured, monkeypatch):
        seen: dict[str, object] = {}

        async def spy(notifier, mark, timeout_seconds, accept=None):
            seen["timeout"] = timeout_seconds
            return None

        notifier = FakeNotifier()
        monkeypatch.setattr(merge.tg, "TelegramNotifier", notifier_factory(notifier))
        monkeypatch.setattr(merge, "_poll_telegram_updates", spy)

        run(merge._wait_for_telegram_reply("prompt"))

        assert seen["timeout"] == merge.TELEGRAM_POLL_TIMEOUT_SECONDS


class TestPromptTelegramReplyGuardsTransport:
    def test_returns_none_when_telegram_raises(self, monkeypatch, quiet_terminal):
        """Mirrors send_telegram_message rather than letting the error escape.

        Unguarded, a flood-control response during 2FA surfaced as the generic
        "2FA validation error" instead of a Telegram problem.
        """

        async def flood_control(prompt, after_send=None, accept=None):
            raise RuntimeError("Flood control exceeded. Retry in 581 seconds")

        monkeypatch.setattr(merge, "_wait_for_telegram_reply", flood_control)

        assert merge.prompt_telegram_reply("give me the code") is None
        assert any("Unexpected Telegram error: Flood control exceeded" in line for line in quiet_terminal)

    def test_passes_the_validator_through(self, monkeypatch):
        seen = {}

        async def fake_wait(prompt, after_send=None, accept=None):
            seen["accept"] = accept
            return "123456"

        monkeypatch.setattr(merge, "_wait_for_telegram_reply", fake_wait)

        merge.prompt_telegram_reply("p", accept=merge._is_two_factor_code)

        assert seen["accept"] is merge._is_two_factor_code


class TestPromptTelegramReply:
    def test_delegates_to_wait_for_reply(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_wait(prompt, after_send=None, accept=None):
            seen["prompt"] = prompt
            seen["after_send"] = after_send
            seen["accept"] = accept
            return "delegated"

        monkeypatch.setattr(merge, "_wait_for_telegram_reply", fake_wait)

        def callback() -> None:
            return None

        assert merge.prompt_telegram_reply("ask", after_send=callback) == "delegated"
        # accept defaults to None: the transport accepts any text unless a caller
        # opts into filtering.
        assert seen == {"prompt": "ask", "after_send": callback, "accept": None}


class TestPollTimeoutConstant:
    def test_is_five_minutes(self):
        assert merge.TELEGRAM_POLL_TIMEOUT_SECONDS == 300
