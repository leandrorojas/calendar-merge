"""Tests for the terminal, time, YAML, filesystem and Telegram helpers."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import pytest
from telegram.error import InvalidToken

import merge

TERMINAL_HELPERS = ("term_print", "print_done", "print_failed", "print_header_box")
# Read at import time, before the autouse `quiet_terminal` fixture replaces them per test.
REAL_TERMINAL = {name: getattr(merge, name) for name in TERMINAL_HELPERS}


class TestTerminalOutput:
    @pytest.fixture(autouse=True)
    def real_terminal(self, monkeypatch):
        for name, helper in REAL_TERMINAL.items():
            monkeypatch.setattr(merge, name, helper)

    def test_print_prefixes_a_timestamp_and_ends_the_line(self, capsys):
        merge.term_print("hello")

        out = capsys.readouterr().out
        assert out.endswith("]: hello\n")
        stamp = out[1 : out.index("]")]
        datetime.strptime(stamp, merge.TERMINAL_TIMESTAMP_FORMAT)

    def test_print_can_leave_the_line_open(self, capsys):
        merge.term_print("working...", finish_line=False)

        assert capsys.readouterr().out.endswith("]: working...")

    def test_done_and_failed_are_coloured(self, capsys):
        merge.print_done()
        merge.print_failed()

        assert capsys.readouterr().out == (
            f"{merge.TerminalColors.green.value}done!{merge.TerminalColors.reset.value}\n"
            f"{merge.TerminalColors.red.value}failed!{merge.TerminalColors.reset.value}\n"
        )

    def test_header_box_wraps_the_longest_line(self, capsys):
        merge.print_header_box("title", "a longer subtitle", padding=1)

        assert capsys.readouterr().out.splitlines() == [
            "╔" + "═" * 19 + "╗",
            "║" + "title".center(19) + "║",
            "║" + "a longer subtitle".center(19) + "║",
            "╚" + "═" * 19 + "╝",
        ]

    def test_header_box_without_a_subtitle(self, capsys):
        merge.print_header_box("title")

        assert len(capsys.readouterr().out.splitlines()) == 3


class TestConvertToUtc:
    def test_a_naive_datetime_is_read_as_utc(self):
        assert merge.convert_to_utc(datetime(2026, 1, 1, 9, 0)) == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    def test_an_aware_datetime_is_converted(self):
        local = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=-3)))

        converted = merge.convert_to_utc(local)

        assert converted == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert converted.utcoffset() == timedelta(0)


class TestYamlHelper:
    def write(self, tmp_path, body):
        path = tmp_path / "config.yaml"
        path.write_text(body)
        return merge.YamlHelper(path)

    def test_reads_a_setting(self, tmp_path):
        assert self.write(tmp_path, "config:\n  skip_days: '5, 6'\n").get("config", "skip_days") == "5, 6"

    def test_a_malformed_file_reports_where(self, tmp_path):
        helper = self.write(tmp_path, "config: [unclosed\n")

        with pytest.raises(merge.YamlError, match=r"YAML parse error \(line \d+, col \d+\)"):
            helper.get("config", "skip_days")

    def test_a_parse_error_without_a_position_still_reports(self, tmp_path, monkeypatch):
        import yaml

        helper = self.write(tmp_path, "config: {}\n")
        monkeypatch.setattr(yaml, "load", lambda *a, **k: (_ for _ in ()).throw(yaml.YAMLError("odd")))

        with pytest.raises(merge.YamlError, match=r"^YAML parse error: odd$"):
            helper.get("config", "skip_days")

    def test_an_unreadable_file_is_reported(self, tmp_path, monkeypatch):
        helper = self.write(tmp_path, "config: {}\n")
        monkeypatch.setattr(Path, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))

        with pytest.raises(merge.YamlError, match="Failed to read YAML: denied") as excinfo:
            helper.get("config", "skip_days")

        assert isinstance(excinfo.value.__cause__, PermissionError)

    def test_an_empty_file_is_reported(self, tmp_path):
        with pytest.raises(merge.YamlError, match="Empty or invalid YAML"):
            self.write(tmp_path, "").get("config", "skip_days")


class TestFileSystem:
    def test_download_hands_the_url_and_destination_to_urllib(self, monkeypatch):
        calls = []
        monkeypatch.setattr(merge.urllib.request, "urlretrieve", lambda url, dest: calls.append((url, dest)))

        merge.FileSystem().download("https://example.test/a.ics", "/tmp/a.ics")

        assert calls == [("https://example.test/a.ics", "/tmp/a.ics")]

    def test_a_failed_download_names_the_file_and_keeps_the_cause(self, monkeypatch):
        def boom(url, dest):
            raise OSError("no route")

        monkeypatch.setattr(merge.urllib.request, "urlretrieve", boom)

        with pytest.raises(merge.FileSystemError, match=r"Error downloading /tmp/a\.ics: no route") as excinfo:
            merge.FileSystem().download("https://example.test/a.ics", "/tmp/a.ics")

        assert isinstance(excinfo.value.__cause__, OSError)

    def test_temp_dir_and_join(self):
        fs = merge.FileSystem()

        assert Path(fs.get_temp_dir()).is_dir()
        assert fs.join_paths("a", "b.ics") == "a/b.ics"


class FakeBot:
    """Records the lifecycle calls. `close` is the Bot API method and must never be reached."""

    instances: ClassVar[list["FakeBot"]] = []
    initialize_error: ClassVar[BaseException | None] = None

    def __init__(self, token):
        self.token = token
        self.events: list[str] = []
        self.sent: list[dict] = []
        FakeBot.instances.append(self)

    async def initialize(self):
        self.events.append("initialize")
        if self.initialize_error:
            raise self.initialize_error

    async def shutdown(self):
        self.events.append("shutdown")

    async def close(self):
        raise AssertionError("Bot.close() asks Telegram to close the bot; use shutdown()")

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, *exc):
        await self.shutdown()

    async def send_message(self, **kwargs):
        self.events.append("send_message")
        self.sent.append(kwargs)
        return "sent"

    async def get_updates(self, **kwargs):
        self.events.append("get_updates")
        self.sent.append(kwargs)
        return ["update"]


@pytest.fixture
def fake_bot(monkeypatch):
    FakeBot.instances = []
    FakeBot.initialize_error = None
    monkeypatch.setattr(merge, "Bot", FakeBot)
    return FakeBot


class TestTelegramNotifier:
    def test_a_one_off_send_shuts_its_bot_down(self, fake_bot):
        notifier = merge.TelegramNotifier(token="t", chat_id="c")

        assert asyncio.run(notifier.send("hi")) == "sent"

        (bot,) = fake_bot.instances
        assert bot.token == "t"
        assert bot.sent == [{"chat_id": "c", "text": "hi", "parse_mode": "Markdown"}]
        assert bot.events == ["initialize", "send_message", "shutdown"]

    def test_the_bot_is_shut_down_when_the_send_fails(self, fake_bot, monkeypatch):
        async def boom(self, **kwargs):
            raise RuntimeError("flood control")

        monkeypatch.setattr(fake_bot, "send_message", boom)
        notifier = merge.TelegramNotifier(token="t", chat_id="c")

        with pytest.raises(RuntimeError, match="flood control"):
            asyncio.run(notifier.send("hi"))

        assert fake_bot.instances[0].events[-1] == "shutdown"

    def test_the_context_manager_shares_one_bot_and_shuts_it_down_once(self, fake_bot):
        async def scenario():
            async with merge.TelegramNotifier(token="t", chat_id="c") as notifier:
                await notifier.send("one")
                await notifier.get_updates(offset=5, timeout=30, allowed_updates=["message"])
                return notifier

        notifier = asyncio.run(scenario())

        (bot,) = fake_bot.instances
        assert bot.events == ["initialize", "send_message", "get_updates", "shutdown"]
        assert bot.sent[1] == {"offset": 5, "limit": 100, "timeout": 30, "allowed_updates": ["message"]}
        assert notifier.bot is None

    def test_close_without_a_bot_does_nothing(self, fake_bot):
        asyncio.run(merge.TelegramNotifier(token="t", chat_id="c").close())

        assert fake_bot.instances == []

    def test_get_updates_outside_the_context_manager(self, fake_bot):
        notifier = merge.TelegramNotifier(token="t", chat_id="c")

        assert asyncio.run(notifier.get_updates()) == ["update"]
        assert fake_bot.instances[0].events == ["initialize", "get_updates", "shutdown"]

    def test_a_rejected_token_is_not_echoed_and_the_bot_is_shut_down(self, fake_bot):
        secret = "123456:secret-token"
        network_error = ConnectionError("Unauthorized")
        rejected = InvalidToken(f"The token `{secret}` was rejected by the server.")
        rejected.__cause__ = network_error
        fake_bot.initialize_error = rejected

        for run in (
            lambda: asyncio.run(merge.TelegramNotifier(token=secret, chat_id="c").send("hi")),
            lambda: asyncio.run(merge.TelegramNotifier(token=secret, chat_id="c").__aenter__()),
        ):
            with pytest.raises(InvalidToken) as excinfo:
                run()

            assert secret not in str(excinfo.value)
            assert excinfo.value.__cause__ is network_error

        assert [bot.events for bot in fake_bot.instances] == [["initialize", "shutdown"]] * 2

    def test_any_other_initialisation_failure_shuts_the_bot_down_and_propagates(self, fake_bot):
        fake_bot.initialize_error = TimeoutError("getMe timed out")

        with pytest.raises(TimeoutError, match="getMe timed out"):
            asyncio.run(merge.TelegramNotifier(token="t", chat_id="c").__aenter__())

        assert fake_bot.instances[0].events == ["initialize", "shutdown"]
