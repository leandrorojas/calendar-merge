# imports
import os
import re
import json
import click
import argparse


# partial imports
from pathlib import Path
import asyncio
from pyicloud import PyiCloudService
from pyicloud.services.calendar import EventObject
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from enum import Enum
from icalendar import Calendar
from time import perf_counter
import google.generativeai as genai

# custom imports
from pyfangs.yaml import YamlHelper
from pyfangs.filesystem import FileSystem
from pyfangs.time import convert_to_utc
from pyfangs.ai import GeminiAI
import pyfangs.terminal as term
import pyfangs.telegram as tg

#region CONSTS
YAML_FILENAME = "config.yaml"
YAML_SECTION_GENERAL = "config"
YAML_SETTING_SKIP_DAYS = "skip_days"
YAML_SETTING_FUTURE_EVENTS_DAYS = "future_events_days"

YAML_SECTION_SOURCE_CALENDAR = "source-calendar-{index}"
YAML_SETTING_CALENDAR_SOURCE = "source"
YAML_SETTING_CALENDAR_TAG = "tag"
YAML_SETTING_CALENDAR_TITLE = "title"
YAML_SETTING_CALENDAR_TZ = "tz"
YAML_SETTING_AI_TONE = "ai_tone"

ICLOUD_FIELD_START_DATE = "startDate"
ICLOUD_FIELD_END_DATE = "endDate"
ICLOUD_FIELD_TITLE = "title"
ICLOUD_FIELD_TZ = "tz"
ICLOUD_FIELD_ALL_DAY_EVENT = "allDay"

ICS_TAG_VEVENT = "VEVENT"
ICS_FIELD_DATE_START = "dtstart"
ICS_FIELD_DATE_END = "dtend"
ICS_FIELD_OOO = "TRANSP"

ENV_ICLOUD_USER = "ICLOUD_USERNAME"
ENV_ICLOUD_PASS = "ICLOUD_PASSWORD"
ENV_VAR_CALENDAR_URL = "CALENDAR_URL_{index}"
ENV_TELEGRAM_TOKEN = "TELEGRAM_BOT_API_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_GEMINI_API_KEY = "GEMINI_API_KEY"

YAML_FILENAME_STATE = "state.json"
YAML_SECTION_STATE = "state"
YAML_SETTING_OVERRIDE_FLAG = "override_flag"
YAML_SETTING_OVERRIDE_DATE = "override_date"
YAML_SETTING_TELEGRAM_OFFSET = "telegram_offset"
TELEGRAM_COMMAND_OVERRIDE = "override"
TELEGRAM_COMMAND_CANCEL = "cancel"

TAG_2F_AUTH = term.TerminalColors.magenta.value + "2f_auth" + term.TerminalColors.reset.value
TAG_CALENDAR_MERGE = term.TerminalColors.cyan.value + "cal-merge" + term.TerminalColors.reset.value
TAG_ICLOUD_AUTH = term.TerminalColors.orange.value + "icloud_auth" + term.TerminalColors.reset.value
TAG_OVERRIDE = term.TerminalColors.yellow.value + "override" + term.TerminalColors.reset.value
TAG_CANCEL = term.TerminalColors.orange.value + "cancel" + term.TerminalColors.reset.value
TAG_ERROR = term.TerminalColors.red.value + "error" + term.TerminalColors.reset.value
#endregion

class EventAction(Enum):
    none = 0
    add = 1
    delete = 2

class RemoveMode(Enum):
    FUTURE_TODAY = "future_today"
    ALL_DAY = "all_day"

@dataclass
class MergeEvent(object):
    title:str
    start:datetime
    end:datetime
    full_event:EventObject
    action:EventAction

def validate_2fa(api: PyiCloudService) -> bool:
    status:bool = True

    if api.requires_2fa:
        security_key_names = api.security_key_names

        if security_key_names:
            print_step(TAG_2F_AUTH, f"Security key confirmation is required. Please plug in one of the following keys: {', '.join(security_key_names)}", True)

            devices = api.fido2_devices

            print_step(TAG_2F_AUTH, "Available FIDO2 devices:", True)

            for idx, dev in enumerate(devices, start=1):
                print_step(TAG_2F_AUTH, f"{idx}: {dev}", True)

            choice = click.prompt(f"{get_tag(TAG_2F_AUTH)} Select a FIDO2 device by number", type=click.IntRange(1, len(devices)), default=1,)
            selected_device = devices[choice - 1]

            print_step(TAG_2F_AUTH, "Please confirm the action using the security key", True)

            api.confirm_security_key(selected_device)

        else:
            print_step(TAG_2F_AUTH, "Two-factor authentication required.", True)
            print_step(TAG_2F_AUTH, "requesting Apple 2FA code via Telegram...", True)
            result = api.validate_2fa_code(prompt_telegram_reply("provide the Apple 2FA code"))
            print_step(TAG_2F_AUTH, f"Code validation result: {result}", True)

            if not result:
                print_step(TAG_2F_AUTH, "Failed to verify security code", True)
                status = False

        if not api.is_trusted_session:
            print_step(TAG_2F_AUTH, "Session is not trusted. Requesting trust...", True)
            result = api.trust_session()
            print_step(TAG_2F_AUTH, f"Session trust result {result}", True)

            if not result:
                print_step(TAG_2F_AUTH, "Failed to request trust. You will likely be prompted for confirmation again in the coming weeks", True)

    elif api.requires_2sa:
        print_step(TAG_2F_AUTH, "Two-step authentication required. Your trusted devices are:", True)

        devices = api.trusted_devices
        for i, device in enumerate(devices):
            print_step(TAG_2F_AUTH, f"  {i}: {device.get('deviceName', 'SMS to %s' % device.get('phoneNumber'))}", True)

        device = click.prompt(f"{get_tag(TAG_2F_AUTH)} Which device would you like to use?", default=0)
        device = devices[device]
        if not api.send_verification_code(device):
            print_step(TAG_2F_AUTH, "Failed to send verification code", True)
            status = False

        send_telegram_message("Calendar merger triggered Apple two-step authentication. Please enter the validation code.")
        code = click.prompt(f"{get_tag(TAG_2F_AUTH)} Please enter validation code")
        if not api.validate_verification_code(device, code):
            print_step(TAG_2F_AUTH, "Failed to verify verification code", True)
            status = False

    return status

def get_datetime(dt:datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, tzinfo=dt.tzinfo)

def get_from_list(items:list, value:str):
    try:
        return_value = items.get(value)
    except:
        return_value = None

    return return_value

def build_datetime(dt: tuple | list, tz: ZoneInfo) -> datetime:
    """Build datetime from sequence [0, year, month, day, hour, minute]."""
    return datetime(dt[1], dt[2], dt[3], dt[4], dt[5], tzinfo=tz)

def get_tag(tag:str) -> str:
    return f"[{tag}]"

def print_step(tag:str, message:str, one_liner:bool=True):
    term.print(f"{get_tag(tag)} {message}", one_liner)

def create_ai_service(api_key:str | None) -> GeminiAI | None:
    if not api_key:
        term.print(f"{get_tag(TAG_ERROR)} Gemini API key not configured", True)
        return None

    try:
        return GeminiAI(api_key)
    except Exception as err:
        term.print_failed()
        term.print(f"{get_tag(TAG_ERROR)} Unable to initialize Gemini AI client: {err}", True)
        return None

def send_ai_telegram_message(ai_client:GeminiAI | None, prompt:str, fallback_message:str, tone:str | None = None, prefix:str = "", disable_notification:bool=False) -> None:
    generated_message = None
    full_prompt = f"{prefix}{prompt}" if prefix else prompt

    if ai_client is not None:
        try:
            response = ai_client.generate_text(full_prompt, tone=tone)
            if response.strip():
                generated_message = f"{prefix}{response.strip()}"
        except Exception as err:
            term.print_failed()
            term.print(f"{get_tag(TAG_ERROR)} Gemini AI generation failed: {err}", True)

    if generated_message is None:
        fallback = fallback_message.strip()
        message = f"{prefix}{fallback}".strip()
        message += f" ({get_tag(TAG_ERROR)} ai message unavailable)"
    else:
        message = generated_message

    send_telegram_message(message, disable_notification=disable_notification)

def send_telegram_message(message:str, disable_notification:bool=False) -> None:
    """
    Best-effort Telegram notifier used for important user-facing events.
    """
    if not message:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = send_telegram_message_async(message, disable_notification)
    if loop is None:
        try:
            asyncio.run(coro)
        except Exception as err:  # pragma: no cover - safety net
            term.print(f"{get_tag(TAG_ERROR)} Unexpected Telegram error: {err}", True)
    else:
        # Fire-and-forget when already inside an event loop to avoid nesting asyncio.run.
        loop.create_task(coro)

async def send_telegram_message_async(message: str, disable_notification: bool = False) -> None:
    if not message:
        return

    token = os.getenv(ENV_TELEGRAM_TOKEN)
    chat_id = os.getenv(ENV_TELEGRAM_CHAT_ID)

    if not token:
        term.print(f"{get_tag(TAG_ERROR)} Telegram token not configured", True)
        return

    notifier_factory = tg.TelegramNotifier
    supports_ctx = hasattr(notifier_factory, "__aenter__")

    async def _send_with(notifier: tg.TelegramNotifier) -> None:
        # Prefer helper method when available (pyfangs newer versions), fallback for backwards compatibility.
        if hasattr(notifier, "send_message"):
            await notifier.send_message(message=message, disable_notification=disable_notification)
        elif disable_notification:
            await notifier.bot.send_message(chat_id=notifier.chat_id, text=message, disable_notification=True)
        else:
            await notifier.send(message)

    if supports_ctx:
        async with notifier_factory(token=token, chat_id=chat_id) as notifier:
            await _send_with(notifier)
    else:
        notifier = notifier_factory(token=token, chat_id=chat_id)
        try:
            await _send_with(notifier)
        finally:
            close_fn = getattr(notifier, "close", None)
            if close_fn:
                maybe_coro = close_fn()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro

def prompt_telegram_reply(prompt: str) -> str | None:
    return asyncio.run(_wait_for_telegram_reply(prompt))

async def _wait_for_telegram_reply(prompt: str) -> str | None:
    token = os.getenv(ENV_TELEGRAM_TOKEN)
    chat_id = os.getenv(ENV_TELEGRAM_CHAT_ID)
    if not token:
        term.print(f"{get_tag(TAG_ERROR)} Telegram token not configured", True)
        return None

    notifier_factory = tg.TelegramNotifier
    supports_ctx = hasattr(notifier_factory, "__aenter__")
    mark = datetime.now(timezone.utc)

    async def _send_prompt(notifier: tg.TelegramNotifier) -> None:
        if hasattr(notifier, "send_message"):
            await notifier.send_message(message=prompt, disable_notification=False)
        else:
            await notifier.send(prompt)

    async def _poll_for_reply(notifier: tg.TelegramNotifier) -> str | None:
        offset = None
        await _send_prompt(notifier)

        while True:
            updates = await notifier.get_updates(offset=offset, timeout=30, allowed_updates=["message"])
            if updates:
                offset = updates[-1].update_id + 1
                for upd in updates:
                    msg = getattr(upd, "message", None)
                    if msg and msg.text:
                        msg_dt = msg.date
                        if msg_dt and msg_dt.tzinfo is None:
                            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                        if msg_dt and msg_dt >= mark:
                            return msg.text
            else:
                await asyncio.sleep(1)  # small pause before next poll

    if supports_ctx:
        async with notifier_factory(token=token, chat_id=chat_id) as notifier:
            return await _poll_for_reply(notifier)

    notifier = notifier_factory(token=token, chat_id=chat_id)
    try:
        return await _poll_for_reply(notifier)
    finally:
        close_fn = getattr(notifier, "close", None)
        if close_fn:
            maybe_coro = close_fn()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro

def _normalize_skip_days(skip_days) -> list[str]:
    if isinstance(skip_days, str):
        skip_days = [day.strip() for day in skip_days.split(",") if day.strip()]
    return [str(day) for day in (skip_days or [])]

def _calculate_future_date(start_date: datetime, future_days: int, skip_days: list[str]) -> datetime:
    current_date = start_date
    counted_days = 0

    while counted_days < future_days:
        current_date += timedelta(days=1)
        if str(current_date.weekday()) in skip_days:
            continue
        counted_days += 1

    return current_date

def _end_of_day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=dt.tzinfo)

def _get_ai_tone(yaml_helper:YamlHelper) -> str | None:
    try:
        return yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_AI_TONE)
    except Exception:
        return None

def _load_state(state_path: str) -> dict:
    """Load persisted override state or return defaults."""
    defaults: dict = {YAML_SETTING_OVERRIDE_FLAG: False, YAML_SETTING_OVERRIDE_DATE: None, YAML_SETTING_TELEGRAM_OFFSET: None}
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
        section = data.get(YAML_SECTION_STATE) or {}
        return {
            YAML_SETTING_OVERRIDE_FLAG: bool(section.get(YAML_SETTING_OVERRIDE_FLAG, False)),
            YAML_SETTING_OVERRIDE_DATE: section.get(YAML_SETTING_OVERRIDE_DATE),
            YAML_SETTING_TELEGRAM_OFFSET: section.get(YAML_SETTING_TELEGRAM_OFFSET),
        }
    except FileNotFoundError:
        return defaults
    except Exception:
        return defaults

def _save_state(state_path: str, state: dict) -> None:
    """Persist override state to JSON file."""
    with open(state_path, "w") as f:
        json.dump({YAML_SECTION_STATE: state}, f, indent=2)

def is_skip_day(date_val: datetime, skip_days: list[str]) -> bool:
    """Return True if the given datetime falls on a configured skip day."""
    return str(date_val.weekday()) in skip_days

def _next_skip_day(from_date: datetime, skip_days: list[str]) -> datetime | None:
    """Return the next date after from_date that is a skip day, or None if none configured."""
    if not skip_days:
        return None
    candidate = from_date + timedelta(days=1)
    for _ in range(7):
        if is_skip_day(candidate, skip_days):
            return candidate
        candidate += timedelta(days=1)
    return None

async def _poll_telegram_commands_async(state: dict) -> set[str]:
    """
    Non-blocking poll for pending Telegram messages. Returns the set of known command
    strings found (e.g. {'override'}, {'cancel'}). Updates offset in state.
    Polling once per run ensures cancel and override share the same update stream without
    either missing messages consumed by the other.
    """
    token = os.getenv(ENV_TELEGRAM_TOKEN)
    if not token:
        return set()

    chat_id = os.getenv(ENV_TELEGRAM_CHAT_ID)
    offset = state.get(YAML_SETTING_TELEGRAM_OFFSET)
    known_commands = {TELEGRAM_COMMAND_OVERRIDE, TELEGRAM_COMMAND_CANCEL}
    found: set[str] = set()

    notifier_factory = tg.TelegramNotifier
    supports_ctx = hasattr(notifier_factory, "__aenter__")

    async def _poll(notifier: tg.TelegramNotifier) -> None:
        nonlocal offset
        updates = await notifier.get_updates(offset=offset, timeout=0, allowed_updates=["message"])
        if updates:
            offset = updates[-1].update_id + 1
            for upd in updates:
                msg = getattr(upd, "message", None)
                if msg and msg.text:
                    text = msg.text.strip().lower()
                    if text in known_commands:
                        found.add(text)

    if supports_ctx:
        async with notifier_factory(token=token, chat_id=chat_id) as notifier:
            await _poll(notifier)
    else:
        notifier = notifier_factory(token=token, chat_id=chat_id)
        try:
            await _poll(notifier)
        finally:
            close_fn = getattr(notifier, "close", None)
            if close_fn:
                maybe_coro = close_fn()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro

    state[YAML_SETTING_TELEGRAM_OFFSET] = offset
    return found

def _handle_override_date_lifecycle(state: dict, today: datetime) -> bool:
    """
    Step 1: Evaluate override_date against today and handle all three lifecycle cases.
    - 1A: today == override_date => log processing enabled for the day (consumed on --last)
    - 1B: today <  override_date => log override pending, today follows normal schedule
    - 1C: today >  override_date => clear as expired and send a warning
    Returns True if state was changed.
    """
    override_date_str = state.get(YAML_SETTING_OVERRIDE_DATE)
    if not override_date_str:
        return False
    try:
        override_date_val = datetime.strptime(str(override_date_str), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        state[YAML_SETTING_OVERRIDE_DATE] = None
        return True

    today_date = today.date()

    if today_date == override_date_val:
        # 1A: active today — override_date stays set until --last
        print_step(TAG_OVERRIDE, f"override active for today ({override_date_str}), processing enabled all day.", True)
        return False

    if today_date < override_date_val:
        # 1B: pending for a future skip day — today follows normal schedule
        print_step(TAG_OVERRIDE, f"override pending for {override_date_str}, today follows normal schedule.", True)
        return False

    # 1C: stale — clear with warning
    print_step(TAG_OVERRIDE, f"override_date {override_date_str} expired without being consumed, clearing.", True)
    send_telegram_message(f"⚠️ Override date {override_date_str} expired without being consumed. Clearing.")
    state[YAML_SETTING_OVERRIDE_DATE] = None
    return True

def _handle_cancel(args, telegram_commands: set[str], state: dict, today: datetime) -> tuple[bool, bool]:
    """
    Step 0: Check for cancel signal and apply pre-execution cancel behavior if eligible.

    Eligibility: cancel acts only when override_date exists AND:
    - override_date == today (cancels today's active override; stops execution), OR
    - override_date >  today (disarms a future override; execution continues normally)

    If eligible:
    - Clears override_date and override_flag (prevents immediate re-arming)
    - Returns (True,  True)  when override_date == today  (caller must stop; cleanup needed)
    - Returns (False, False) when override_date >  today  (caller continues normally)

    If not eligible (no override_date, or override_date already past): no-op → (False, False).
    """
    cli_cancel = getattr(args, "cancel", False)
    tg_cancel = TELEGRAM_COMMAND_CANCEL in telegram_commands

    if not (cli_cancel or tg_cancel):
        return False, False

    override_date_str = state.get(YAML_SETTING_OVERRIDE_DATE)

    # Not eligible: no override armed
    if not override_date_str:
        print_step(TAG_CANCEL, "cancel received but no override is armed, no-op.", True)
        send_telegram_message("⚠️ Cancel received but no override is currently armed. No action taken.")
        return False, False

    try:
        override_date_val = datetime.strptime(str(override_date_str), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        state[YAML_SETTING_OVERRIDE_DATE] = None
        return False, False

    today_date = today.date()

    # Not eligible: override_date already in the past
    if override_date_val < today_date:
        print_step(TAG_CANCEL, f"cancel received but override_date {override_date_str} is already past, no-op.", True)
        send_telegram_message(f"⚠️ Cancel received but override date {override_date_str} is already past. No action taken.")
        return False, False

    # Eligible — clear override state (also clears flag to prevent immediate re-arming)
    state[YAML_SETTING_OVERRIDE_DATE] = None
    state[YAML_SETTING_OVERRIDE_FLAG] = False

    if override_date_val == today_date:
        print_step(TAG_CANCEL, f"cancel applied: override for today ({override_date_str}) disarmed, stopping execution.", True)
        send_telegram_message(f"🚫 Override for today ({override_date_str}) canceled. Processing stopped for this run.")
        return True, True  # caller must exit; future today events must be removed

    # override_date > today — disarm silently and continue
    print_step(TAG_CANCEL, f"cancel applied: override for {override_date_str} disarmed.", True)
    send_telegram_message(f"🚫 Override for {override_date_str} canceled.")
    return False, False

def _ingest_override_flag(args, telegram_commands: set[str], state: dict) -> bool:
    """
    Step 2 ingestion: read override signal from CLI or the pre-polled Telegram commands
    and set the persistent override_flag. Idempotent: repeated signals are a no-op when
    the flag or override_date is already set. Mutates state in place. Returns True if
    state changed.
    """
    override_date_str = state.get(YAML_SETTING_OVERRIDE_DATE)
    override_flag = state.get(YAML_SETTING_OVERRIDE_FLAG, False)

    cli_override = getattr(args, "override", False)
    tg_override = TELEGRAM_COMMAND_OVERRIDE in telegram_commands
    signal_received = cli_override or tg_override

    if not signal_received:
        return False

    # Idempotency: override_date already armed — ignore
    if override_date_str:
        print_step(TAG_OVERRIDE, f"override signal received but override_date already armed ({override_date_str}), ignoring.", True)
        send_telegram_message(f"⚠️ Override already armed for {override_date_str}. No action taken.")
        return False

    # Idempotency: flag already pending — ignore
    if override_flag:
        print_step(TAG_OVERRIDE, "override signal received but override_flag already pending.", True)
        return False

    state[YAML_SETTING_OVERRIDE_FLAG] = True
    print_step(TAG_OVERRIDE, "override flag set.", True)
    return True

def _compute_and_persist_override_date(args, state: dict, skip_days: list[str], today: datetime) -> bool:
    """
    Step 2 computation: when override_flag is set and no override_date exists, compute and
    persist override_date then clear the flag.
    - If --first AND today is a skip day  => override_date = today
    - Otherwise                           => override_date = next_skip_day
    Returns True if state changed.
    """
    if not state.get(YAML_SETTING_OVERRIDE_FLAG, False):
        return False

    # Guard: flag set but date already present (shouldn't normally occur — clear flag and exit)
    if state.get(YAML_SETTING_OVERRIDE_DATE):
        state[YAML_SETTING_OVERRIDE_FLAG] = False
        return True

    is_first = getattr(args, "first", False)
    if is_first and is_skip_day(today, skip_days):
        new_date = today.strftime("%Y-%m-%d")
        state[YAML_SETTING_OVERRIDE_DATE] = new_date
        state[YAML_SETTING_OVERRIDE_FLAG] = False
        print_step(TAG_OVERRIDE, f"override armed for today ({new_date}).", True)
        send_telegram_message(f"✅ Override armed: processing enabled for today ({new_date}).")
        return True

    next_skip = _next_skip_day(today, skip_days)
    if next_skip is None:
        print_step(TAG_OVERRIDE, "no skip days configured, override has no effect.", True)
        state[YAML_SETTING_OVERRIDE_FLAG] = False
        return True

    new_date = next_skip.strftime("%Y-%m-%d")
    state[YAML_SETTING_OVERRIDE_DATE] = new_date
    state[YAML_SETTING_OVERRIDE_FLAG] = False
    print_step(TAG_OVERRIDE, f"override armed for next skip day ({new_date}).", True)
    send_telegram_message(f"✅ Override armed: processing will be enabled on {new_date}.")
    return True

def _should_process(state: dict, today: datetime, skip_days: list[str]) -> bool:
    """
    Step 1A/1B + Step 3: Determine whether data processing should run this invocation.
    Returns True if processing is enabled.
    """
    override_date_str = state.get(YAML_SETTING_OVERRIDE_DATE)
    if override_date_str:
        try:
            override_date_val = datetime.strptime(str(override_date_str), "%Y-%m-%d").date()
            if today.date() == override_date_val:
                return True  # Step 1A: override active for today
        except (ValueError, TypeError):
            pass
    return not is_skip_day(today, skip_days)  # Step 3

def _consume_override_on_last(state: dict, today: datetime, state_path: str) -> None:
    """Step 1A consume rule: on --last, clear override_date if today matches."""
    override_date_str = state.get(YAML_SETTING_OVERRIDE_DATE)
    if not override_date_str:
        return
    try:
        override_date_val = datetime.strptime(str(override_date_str), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return
    if today.date() == override_date_val:
        state[YAML_SETTING_OVERRIDE_DATE] = None
        _save_state(state_path, state)
        print_step(TAG_OVERRIDE, f"override consumed for {override_date_str} (last run of day).", True)
        send_telegram_message(f"✅ Override for {override_date_str} consumed. Normal schedule resumes tomorrow.")

_MANAGED_TITLE_RE = re.compile(r"^\[.+\] .+/.+$")

def _is_managed_event(title: str) -> bool:
    """Return True if the title matches the calendar-merge format: [TAG] title/source."""
    return bool(_MANAGED_TITLE_RE.match(title or ""))

def remove_synced_events(
    target_date: datetime,
    mode: RemoveMode,
    icloud_events: list[MergeEvent],
    calendar_service,
    now: datetime,
) -> int:
    """
    Delete calendar-merge-managed events for target_date according to mode.
    - FUTURE_TODAY: managed events on target_date whose start >= now
    - ALL_DAY:      all managed events on target_date
    Returns the count of deleted events. Only touches events whose title matches
    the [TAG] title/source format — no collateral deletions.
    """
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    removed = 0

    for event in icloud_events:
        if not _is_managed_event(event.title):
            continue
        if event.start.date() != target_date.date():
            continue
        if mode == RemoveMode.FUTURE_TODAY and event.start < now_utc:
            continue
        remove_obj = EventObject(
            pguid=event.full_event["pGuid"],
            guid=event.full_event["guid"],
            title=event.title,
        )
        try:
            calendar_service.remove_event(remove_obj)
            removed += 1
        except Exception as err:
            print_step(TAG_CANCEL, f"failed to remove event '{event.title}': {err}", True)

    return removed

def _collect_icloud_events(raw_events:list, skip_days:list[str]) -> list[MergeEvent]:
    events:list[MergeEvent] = []
    for raw_event in raw_events:
        all_day:bool = get_from_list(raw_event, ICLOUD_FIELD_ALL_DAY_EVENT)
        if all_day is None or all_day:
            continue

        start_datetime = get_from_list(raw_event, ICLOUD_FIELD_START_DATE)
        tzinfo = get_from_list(raw_event, ICLOUD_FIELD_TZ)
        end_datetime = get_from_list(raw_event, ICLOUD_FIELD_END_DATE)
        event_title = get_from_list(raw_event, ICLOUD_FIELD_TITLE)

        if None in (start_datetime, tzinfo, end_datetime, event_title):
            continue

        tzinfo = ZoneInfo(tzinfo)
        start_datetime = convert_to_utc(build_datetime(start_datetime, tzinfo))
        if str(start_datetime.weekday()) in skip_days:
            continue

        event_end_datetime = convert_to_utc(build_datetime(end_datetime, tzinfo))
        events.append(MergeEvent(event_title, start_datetime, event_end_datetime, raw_event, None))

    return events

def main():
    
    parser = argparse.ArgumentParser(description="Calendar merge with Telegram notifications.")
    parser.add_argument("--first", action="store_true", help="Send start-of-day Telegram notification.")
    parser.add_argument("--last", action="store_true", help="Send end-of-day Telegram notification.")
    parser.add_argument("--override", action="store_true", help="Arm a processing override for the next skip day.")
    parser.add_argument("--cancel", action="store_true", help="Cancel an active or pending processing override.")
    args = parser.parse_args()

    #region CONFIG_LOAD
    print_step(TAG_CALENDAR_MERGE, "reading config...", False)
    load_dotenv()
    fs = FileSystem()
    project_root = str(Path(__file__).resolve().parent.parent)
    config_path = fs.join_paths(project_root, YAML_FILENAME)
    try:
        yaml_helper = YamlHelper(config_path)
    except Exception as err:
        term.print_failed()
        raise RuntimeError("Unable to open YAML configuration") from err

    ai_tone = _get_ai_tone(yaml_helper)
    ai_client = create_ai_service(os.getenv(ENV_GEMINI_API_KEY)) if (args.first or args.last) else None

    if args.first:
        send_ai_telegram_message(
            ai_client,
            "Write a good morning message, different every time. One-liner only.",
            "calendar-merge started for today.",
            tone=ai_tone,
            prefix="☀️ ",
        )

    try:
        future_event_days = int(yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_FUTURE_EVENTS_DAYS))
    except Exception as err:
        term.print_failed()
        raise RuntimeError("Invalid future event days configuration") from err

    try:
        skip_days = yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_SKIP_DAYS)
    except Exception as err:
        term.print_failed()
        raise RuntimeError("Unable to load skip days configuration") from err
    skip_days = _normalize_skip_days(skip_days)
    term.print_done()
    #endregion

    #region OVERRIDE_HANDLING
    today = datetime.now()
    state_path = fs.join_paths(project_root, YAML_FILENAME_STATE)
    state = _load_state(state_path)

    # Poll Telegram once per run so cancel (Step 0) and override (Step 2) share the same
    # update stream without either consuming messages the other needs.
    old_offset = state.get(YAML_SETTING_TELEGRAM_OFFSET)
    try:
        telegram_commands = asyncio.run(_poll_telegram_commands_async(state))
    except Exception as err:
        print_step(TAG_CANCEL, f"Telegram poll failed: {err}", True)
        telegram_commands = set()
    offset_changed = state.get(YAML_SETTING_TELEGRAM_OFFSET) != old_offset

    # Step 0: cancel handling runs before everything else
    should_cancel, cancel_needs_cleanup = _handle_cancel(args, telegram_commands, state, today)
    if should_cancel:
        _save_state(state_path, state)
        if cancel_needs_cleanup:
            print_step(TAG_CANCEL, "removing future events for today...", False)
            try:
                icloud_service = PyiCloudService(os.getenv(ENV_ICLOUD_USER), os.getenv(ENV_ICLOUD_PASS))
                if not validate_2fa(icloud_service):
                    raise RuntimeError("2FA validation failed")
                calendar_service = icloud_service.calendar
                today_start = datetime(today.year, today.month, today.day, 0, 0, 0)
                all_today_events = calendar_service.get_events(from_dt=today_start, to_dt=_end_of_day(today))
                icloud_events_today = _collect_icloud_events(all_today_events, [])
                removed = remove_synced_events(today, RemoveMode.FUTURE_TODAY, icloud_events_today, calendar_service, datetime.now(timezone.utc))
                term.print_done()
                print_step(TAG_CANCEL, f"removed {removed} future event(s) for today.", True)
                send_telegram_message(f"🗑️ Removed {removed} future event(s) for today after cancel.")
            except Exception as err:
                term.print_failed()
                print_step(TAG_CANCEL, f"failed to remove events: {err}", True)
                send_telegram_message(f"⚠️ Cancel cleanup failed: {err}")
        return

    lifecycle_changed = _handle_override_date_lifecycle(state, today)
    ingest_changed = _ingest_override_flag(args, telegram_commands, state)
    compute_changed = _compute_and_persist_override_date(args, state, skip_days, today)
    if offset_changed or lifecycle_changed or ingest_changed or compute_changed:
        _save_state(state_path, state)
    #endregion

    should_process = _should_process(state, today, skip_days)
    if not should_process:
        print_step(TAG_CALENDAR_MERGE, "skip day — processing disabled, skipping iCloud sync.", True)
    else:
        #region ICLOUD_AUTH
        print_step(TAG_ICLOUD_AUTH, "authenticating with iCloud...", False)
        try:
            icloud_service = PyiCloudService(os.getenv(ENV_ICLOUD_USER), os.getenv(ENV_ICLOUD_PASS))
        except Exception as err:
            term.print_failed()
            raise RuntimeError("Unable to start iCloud service") from err

        try:
            if not validate_2fa(icloud_service):
                term.print_failed()
                raise RuntimeError("2FA validation failed")
        except RuntimeError:
            term.print_failed()
            raise
        except Exception as err:
            term.print_failed()
            raise RuntimeError("2FA validation error") from err
        term.print_done()
        #endregion

        #region ICLOUD_CALENDAR_LOAD
        print_step(TAG_CALENDAR_MERGE, "loading iCloud calendar events...", False)
        calendar_service = icloud_service.calendar
        try:
            calendars = calendar_service.get_calendars()
            calendar_guid = next((c.get("guid") for c in calendars if c.get("guid")), None)
        except Exception as err:
            term.print_failed()
            raise RuntimeError("Unable to fetch calendars") from err

        if not calendar_guid:
            term.print_failed()
            raise RuntimeError("No calendar GUID available")

        filter_start = datetime.today()
        filter_end = _calculate_future_date(filter_start, future_event_days, skip_days)

        try:
            all_icloud_events = calendar_service.get_events(from_dt=filter_start, to_dt=filter_end)
        except Exception as err:
            term.print_failed()
            raise RuntimeError("Unable to load events from iCloud") from err

        icloud_events = _collect_icloud_events(all_icloud_events, skip_days)

        now = datetime.now()
        today_bod = datetime(now.year, now.month, now.day, 0, 0, 0)
        cut_off_date = _end_of_day(_calculate_future_date(now, future_event_days, skip_days))

        source_index = 0
        term.print_done()
        #endregion

        print_step(TAG_CALENDAR_MERGE, term.TerminalColors.yellow.value + "processing source calendars" + term.TerminalColors.reset.value, True)
        while True:
            section = YAML_SECTION_SOURCE_CALENDAR.format(index=source_index)
            try:
                calendar_source = yaml_helper.get(section, YAML_SETTING_CALENDAR_SOURCE)
            except Exception:
                print_step(TAG_CALENDAR_MERGE, term.TerminalColors.yellow.value + "no more source calendars to process" + term.TerminalColors.reset.value, True)
                break

            print_step(TAG_CALENDAR_MERGE, f"reading {calendar_source} [{source_index}] source calendar config...", False)
            try:
                calendar_tag = yaml_helper.get(section, YAML_SETTING_CALENDAR_TAG)
                calendar_title = yaml_helper.get(section, YAML_SETTING_CALENDAR_TITLE)
                calendar_tz = ZoneInfo(yaml_helper.get(section, YAML_SETTING_CALENDAR_TZ))
            except Exception as err:
                term.print_failed()
                raise RuntimeError(f"Invalid calendar configuration at index {source_index}") from err

            calendar_url = os.getenv(ENV_VAR_CALENDAR_URL.format(index=source_index))
            if not calendar_url:
                term.print_failed()
                raise RuntimeError(f"Missing calendar URL for index {source_index}")
            term.print_done()

            print_step(TAG_CALENDAR_MERGE, f"downloading calendar {calendar_source} [{source_index}]...", False)
            timestamp_filename = fs.join_paths(fs.get_temp_dir(), f"{convert_to_utc(now).strftime('%Y%m%d%H%M%S%f')}.ics")
            try:
                fs.download(calendar_url, timestamp_filename)
            except Exception as err:
                term.print_failed()
                raise RuntimeError(f"Unable to download calendar {calendar_source} [{source_index}]") from err
            term.print_done()

            print_step(TAG_CALENDAR_MERGE, f"reading source calendar from {timestamp_filename}...", False)
            try:
                with open(timestamp_filename, "rb") as ics_file:
                    ics_calendar = Calendar.from_ical(ics_file.read())
            except Exception as err:
                term.print_failed()
                raise RuntimeError(f"Unable to parse calendar {calendar_source} [{source_index}]") from err

            source_calendar_events:list[MergeEvent] = []
            utc_today_bod = convert_to_utc(today_bod)
            utc_cut_off_date = convert_to_utc(cut_off_date)
            term.print_done()

            print_step(TAG_CALENDAR_MERGE, f"filtering events from source calendar {calendar_source} [{source_index}]...", False)
            for file_event in ics_calendar.walk(ICS_TAG_VEVENT):
                ooo_status = get_from_list(file_event, ICS_FIELD_OOO)
                if ooo_status is not None:
                    continue

                start_datetime = get_from_list(file_event, ICS_FIELD_DATE_START)
                if start_datetime is None:
                    continue

                start_datetime = start_datetime.dt
                if not isinstance(start_datetime, datetime):
                    continue

                start_datetime = convert_to_utc(datetime(start_datetime.year, start_datetime.month, start_datetime.day, start_datetime.hour, start_datetime.minute, tzinfo=start_datetime.tzinfo))

                if str(start_datetime.weekday()) in skip_days:
                    continue

                if not (utc_today_bod <= start_datetime <= utc_cut_off_date):
                    continue

                end_datetime = get_from_list(file_event, ICS_FIELD_DATE_END)
                if end_datetime is None:
                    continue

                end_datetime = end_datetime.dt
                end_datetime = convert_to_utc(datetime(end_datetime.year, end_datetime.month, end_datetime.day, end_datetime.hour, end_datetime.minute, tzinfo=end_datetime.tzinfo))
                source_calendar_events.append(MergeEvent(None, start_datetime, end_datetime, None, None))
            term.print_done()

            source_tag = f"[{calendar_tag}] {calendar_title}/{calendar_source}"
            print_step(TAG_CALENDAR_MERGE, f"filtering {source_tag} events in iCloud calendar...", False)
            filtered_icloud_events = [event for event in icloud_events if event.title == source_tag]
            merge_events:list[MergeEvent] = []
            event_addition = False
            term.print_done()

            print_step(TAG_CALENDAR_MERGE, f"identifying events to remove...", False)
            if filtered_icloud_events:
                source_event_map:dict[tuple[datetime, datetime], list[MergeEvent]] = {}
                for source_event in source_calendar_events:
                    source_event_map.setdefault((source_event.start, source_event.end), []).append(source_event)

                for icloud_event in filtered_icloud_events:
                    event_time = (icloud_event.start, icloud_event.end)
                    matching_events = source_event_map.get(event_time, [])
                    event_action = EventAction.none if matching_events else EventAction.delete
                    icloud_event.action = event_action
                    for source_event in matching_events:
                        source_event.action = event_action
                    merge_events.append(icloud_event)

                events_to_add = [event for event in source_calendar_events if event.action is None]
                event_addition = len(events_to_add) > 0
            else:
                event_addition = True
                events_to_add = source_calendar_events
            term.print_done()

            if event_addition:
                print_step(TAG_CALENDAR_MERGE, f"identifying events to add...", False)
                for source_event in events_to_add:
                    source_event.title = source_tag
                    source_event.action = EventAction.add
                    merge_events.append(source_event)
                term.print_done()

            print_step(TAG_CALENDAR_MERGE, f"synchronizing events to iCloud calendar...", False)
            actionable_events = [event for event in merge_events if event.action != EventAction.none]
            for merge_event in actionable_events:
                if merge_event.action == EventAction.add:
                    try:
                        calendar_service.add_event(event=EventObject(pguid=calendar_guid, title=merge_event.title, start_date=merge_event.start.astimezone(calendar_tz), end_date=merge_event.end.astimezone(calendar_tz)))
                    except Exception as err:
                        term.print_failed()
                        raise RuntimeError(f"Unable to add event {merge_event.title}") from err
                elif merge_event.action == EventAction.delete:
                    remove_event = EventObject(pguid=merge_event.full_event["pGuid"], guid=merge_event.full_event["guid"], title=merge_event.title)
                    try:
                        calendar_service.remove_event(remove_event)
                    except Exception as err:
                        term.print_failed()
                        raise RuntimeError(f"Unable to delete event {merge_event.title}") from err
            term.print_done()

            source_index += 1

    if args.last:
        _consume_override_on_last(state, today, state_path)

    if args.last:
        send_ai_telegram_message(
            ai_client,
            "Write a end of day wrap-up message, different every time. One-liner only.",
            "calendar-merge finished for today.",
            tone=ai_tone,
            prefix="🌙 ",
        )

if __name__ == "__main__":
    merge_start = perf_counter()
    term.print_header_box("iCloud calendar merger")

    try:
        main()
    except Exception as err:
        term.print(f"{get_tag(TAG_ERROR)} An error occurred during the merge process: {err}")
        send_telegram_message(f"Calendar merge failed: {err}")

    merge_end = perf_counter()
    term.print_header_box("merge & sync completed", f"total time: {merge_end - merge_start:.3f} seconds")
