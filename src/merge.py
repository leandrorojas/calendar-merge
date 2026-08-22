# imports
import argparse
import asyncio
import logging
import os
import re

# partial imports
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

import click
import pyfangs.telegram as tg
import pyfangs.terminal as term
from dateutil.rrule import rrulestr
from dotenv import load_dotenv
from icalendar import Calendar
from pyfangs.filesystem import FileSystem
from pyfangs.time import convert_to_utc

# custom imports
from pyfangs.yaml import YamlError, YamlHelper
from pyicloud import PyiCloudService
from pyicloud.services.calendar import EventObject

# region CONSTS
YAML_FILENAME = "config.yaml"
YAML_SECTION_GENERAL = "config"
YAML_SETTING_SKIP_DAYS = "skip_days"
YAML_SETTING_FUTURE_EVENTS_DAYS = "future_events_days"

YAML_SECTION_SOURCE_CALENDAR = "source-calendar-{index}"
YAML_SETTING_CALENDAR_SOURCE = "source"
YAML_SETTING_CALENDAR_TAG = "tag"
YAML_SETTING_CALENDAR_TITLE = "title"
YAML_SETTING_CALENDAR_TZ = "tz"

ICLOUD_FIELD_START_DATE = "startDate"
ICLOUD_FIELD_END_DATE = "endDate"
ICLOUD_FIELD_TITLE = "title"
ICLOUD_FIELD_TZ = "tz"
ICLOUD_FIELD_ALL_DAY_EVENT = "allDay"

ICS_TAG_VEVENT = "VEVENT"
ICS_FIELD_DATE_START = "dtstart"
ICS_FIELD_DATE_END = "dtend"
ICS_FIELD_TRANSPARENCY = "TRANSP"
ICS_FIELD_PRODID = "PRODID"
ICS_FIELD_MS_BUSY_STATUS = "X-MICROSOFT-CDO-BUSYSTATUS"
ICS_FIELD_RRULE = "RRULE"
ICS_FIELD_EXDATE = "EXDATE"
ICS_FIELD_RDATE = "RDATE"
ICS_FIELD_RECURRENCE_ID = "RECURRENCE-ID"
ICS_FIELD_UID = "UID"

# TRANSP means different things in practice depending on who wrote the feed, so
# the same value cannot be interpreted the same way everywhere.
#
# Google publishes real meetings with no TRANSP at all and writes an explicit
# value only for time you blocked yourself -- lunch, focus time, out of office.
# On a shared work calendar those blocks carry TRANSP:OPAQUE, so on a Google feed
# the *presence* of TRANSP is the signal to skip, whichever value it holds.
#
# Outlook stamps TRANSP on every event: OPAQUE for genuine meetings, TRANSPARENT
# for free time. Treating presence as "skip" there drops the entire feed, so only
# TRANSPARENT counts as free, plus X-MICROSOFT-CDO-BUSYSTATUS:OOF for out of
# office. Outlook's TENTATIVE is kept: it is the equivalent of a Google "maybe",
# and Google feeds strip PARTSTAT entirely so those already sync.
#
# Anything that is not recognisably Google follows the RFC reading, so an unknown
# provider errs towards syncing too much rather than silently syncing nothing.
ICS_TRANSPARENCY_FREE = "TRANSPARENT"
ICS_MS_BUSY_OUT_OF_OFFICE = "OOF"
ICS_PRODID_GOOGLE = "GOOGLE"

ENV_ICLOUD_USER = "ICLOUD_USERNAME"
ENV_ICLOUD_PASS = "ICLOUD_PASSWORD"
ENV_VAR_CALENDAR_URL = "CALENDAR_URL_{index}"
ENV_TELEGRAM_TOKEN = "TELEGRAM_BOT_API_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_LOG_FILE = "CALENDAR_MERGE_LOG_FILE"
ENV_LOG_LEVEL = "CALENDAR_MERGE_LOG_LEVEL"

# Maximum time to wait for a Telegram reply (e.g., 2FA code). Prevents the script
# from hanging indefinitely when Telegram is flood-controlled or the user is away.
TELEGRAM_POLL_TIMEOUT_SECONDS = 300  # 5 minutes

# Apple sends a six-digit code. Replies that do not look like one are ignored
# rather than submitted, so ordinary chatter in the Telegram chat cannot burn an
# attempt. A mistyped code gets more than one try because a human is in the loop
# and the alternative is aborting the whole merge until the next scheduled run.
TWO_FACTOR_CODE_ATTEMPTS = 3

# Bounds the source-calendar loop. Not a configuration limit -- the list normally ends
# when a section is absent. This only stops a persistent fault *before* that section read
# from looping forever, which became possible once per-source failures stopped aborting
# the run: a hung schedule is worse than a failed one.
MAX_SOURCE_CALENDARS = 100

# YamlHelper.get re-reads config.yaml on every call and raises YamlError for six
# distinct situations, distinguishable only by message prefix. Two are about a section;
# the rest are about the file, and retrying those at every index cannot help.
YAML_ABSENT_SECTION_PREFIX = "Missing key:"
YAML_MISSING_SETTING_PREFIX = "Missing setting:"

# Floor for how much of each cause survives in an aggregated alert.
SOURCE_FAILURE_MIN_DETAIL = 30

# pyicloud >= 2.6.5 asks Apple for a code on its own from inside authenticate().
# Named here so the canary test fails loudly if a future release renames it.
PYICLOUD_AUTO_2FA_METHOD = "_request_2fa_code"
_TWO_FACTOR_CODE_PATTERN = re.compile(r"^\d{6}$")

# Sent only on the trusted-device path, where the user submitted the code over
# Telegram and is waiting there. The FIDO2 and 2SA paths prompt on the terminal
# instead, and validate_2fa ignores the FIDO2 result, so a confirmation there
# could claim success for a key confirmation that actually failed.
TELEGRAM_2FA_ACCEPTED_MESSAGE = "✅ Apple 2FA code accepted"

# The run still fails, but the failure alert on its own is misleading when trust
# was established anyway: it looks like nothing was achieved when in fact the next
# run will not need a code.
TELEGRAM_2FA_TRUSTED_AFTER_FAILURE_MESSAGE = (
    "⚠️ Apple 2FA code was not accepted, but the session is now trusted — the next run should not prompt"
)

# Default log file relative to project root. Overridable via CALENDAR_MERGE_LOG_FILE.
DEFAULT_LOG_FILE = "logs/calendar-merge.log"
DEFAULT_LOG_LEVEL = "INFO"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

# Causes to unwrap into an alert. Deep enough for wrapper -> library -> transport,
# short enough that the message stays readable on a phone.
ERROR_CAUSE_DEPTH = 3

# Per-part ceiling for an alert. pyicloud embeds the whole HTTP response body in
# PyiCloudAPIResponseException's message, so an Apple error page would otherwise
# arrive verbatim. Telegram rejects anything past 4096 characters and
# send_telegram_message swallows the failure, which would drop the alert entirely
# -- the worse the upstream error, the more certainly you would hear nothing.
# Bounds the whole message at roughly (ERROR_CAUSE_DEPTH + 1) * this.
ERROR_PART_MAX_CHARS = 300

# A delete that returns this has already achieved what it was asked to do.
HTTP_NOT_FOUND = 404

# Regex to strip ANSI color codes (e.g., TAG_* constants include terminal colors)
# so file logs stay clean.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

TAG_2F_AUTH = term.TerminalColors.magenta.value + "2f_auth" + term.TerminalColors.reset.value
TAG_CALENDAR_MERGE = term.TerminalColors.cyan.value + "cal-merge" + term.TerminalColors.reset.value
TAG_ICLOUD_AUTH = term.TerminalColors.orange.value + "icloud_auth" + term.TerminalColors.reset.value
TAG_ERROR = term.TerminalColors.red.value + "error" + term.TerminalColors.reset.value
# endregion


logger = logging.getLogger("calendar-merge")


def _configure_logging() -> None:
    """Set up the rotating file handler for persistent logs.

    Called once at startup. Safe to call multiple times (idempotent).
    """
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return  # already configured

    level_name = os.getenv(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    log_file = os.getenv(ENV_LOG_FILE, DEFAULT_LOG_FILE)
    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parent.parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    # Don't propagate to root to avoid duplicate console output.
    logger.propagate = False


def _strip_ansi(text: str) -> str:
    """Remove ANSI color codes so file logs stay clean."""
    return _ANSI_ESCAPE.sub("", text)


def _condense(text: str, limit: int = ERROR_PART_MAX_CHARS) -> str:
    """Flatten to one line and bound the length, for alerts that must stay sendable."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _describe_error(err: BaseException) -> str:
    """Render an exception together with the causes it was raised from.

    Every failure in this program reaches Telegram through the `__main__` handler,
    which used to send only `str(err)`. The raise sites deliberately wrap low-level
    failures in a readable RuntimeError -- `raise RuntimeError("Unable to load
    events from iCloud") from err` -- so the message that arrived described *where*
    the merge stopped and never *why*. On 2026-08-18 that cost an investigation: an
    Apple outage returning bodiless HTTP 500s was indistinguishable, from the alert
    alone, from a broken session.

    Only `__cause__` is followed, never `__context__`. `from err` is explicit and
    always meaningful; implicit context is whatever happened to be in flight and
    would add noise to an alert that has to stay readable on a phone.

    Note the cause's own text can be actively misleading and this is not the place
    to correct it -- pyicloud rewrites the reason for any 409/421/450/500 to
    "Authentication required for Account." (see CLAUDE.md). Surfacing the type and
    code is what makes that recognisable.
    """
    parts: list[str] = []
    seen: set[int] = {id(err)}
    cause: BaseException | None = err.__cause__

    while cause is not None and len(parts) < ERROR_CAUSE_DEPTH and id(cause) not in seen:
        seen.add(id(cause))
        detail = _condense(str(cause))
        parts.append(f"{type(cause).__name__}: {detail}" if detail else type(cause).__name__)
        cause = cause.__cause__

    message = _condense(str(err)) or type(err).__name__
    if not parts:
        return message
    return f"{message} ({' <- '.join(parts)})"


class SessionTrust(Enum):
    """Outcome of a session-trust request.

    `already_trusted` is kept distinct from `granted` so the caller never tells
    the user that trust was just established when nothing was requested.
    """

    already_trusted = 0
    granted = 1
    refused = 2


class EventAction(Enum):
    none = 0
    add = 1
    delete = 2


@dataclass
class SyncOutcome:
    """What one source calendar's sync actually changed.

    Every other step in the pipeline announces itself, and then the one that mutates
    the calendar used to do so silently -- so confirming a run had done anything meant
    inferring it from how long the process paused.
    """

    added: int = 0
    deleted: int = 0
    already_gone: int = 0


@dataclass
class MergeEvent:
    title: str | None
    start: datetime
    end: datetime
    full_event: EventObject | None
    action: EventAction | None


# region 2FA helpers


def _validate_2fa_fido2(api: PyiCloudService) -> bool:
    """Handle FIDO2 security key 2FA flow. Returns True on success."""
    security_key_names = api.security_key_names
    print_step(
        TAG_2F_AUTH,
        f"Security key confirmation is required. Please plug in one of the following keys: {', '.join(security_key_names)}",
        one_liner=True,
    )

    devices = api.fido2_devices
    print_step(TAG_2F_AUTH, "Available FIDO2 devices:", one_liner=True)
    for idx, dev in enumerate(devices, start=1):
        print_step(TAG_2F_AUTH, f"{idx}: {dev}", one_liner=True)

    choice = click.prompt(
        f"{get_tag(TAG_2F_AUTH)} Select a FIDO2 device by number",
        type=click.IntRange(1, len(devices)),
        default=1,
    )
    api.confirm_security_key(devices[choice - 1])
    return True


def _is_two_factor_code(text: str) -> bool:
    """Return True when a Telegram reply looks like an Apple 2FA code."""
    return bool(_TWO_FACTOR_CODE_PATTERN.match(text.strip()))


def _validate_two_factor_code(api: PyiCloudService, code: str) -> bool:
    """Submit a code to Apple, treating a raised error as a rejection.

    The code is stripped first. `_is_two_factor_code` accepts surrounding
    whitespace, and Telegram clients readily append a trailing newline, so
    submitting the raw text would have Apple reject a reply that was accepted as
    valid here.

    pyicloud returns False for a wrong code but can raise for an expired one.
    Both mean "this code did not work", and both should leave the retry loop
    intact rather than aborting the merge, so the error is reported and counted
    as a failed attempt.
    """
    try:
        result = api.validate_2fa_code(code.strip())
    except Exception as err:
        print_step(TAG_2F_AUTH, f"Code rejected by Apple: {err}", one_liner=True)
        return False

    print_step(TAG_2F_AUTH, f"Code validation result: {result}", one_liner=True)
    if not result:
        print_step(TAG_2F_AUTH, "Failed to verify security code", one_liner=True)
    return bool(result)


def _validate_2fa_trusted_device(api: PyiCloudService) -> bool:
    """Handle trusted-device 2FA code flow via Telegram. Returns True on success."""
    print_step(TAG_2F_AUTH, "Two-factor authentication required.", one_liner=True)

    # Prevent pyicloud 2.5.0's SMS fallback when the trusted-device bridge
    # WebSocket times out — we want trusted-device-only validation.
    api._can_request_sms_2fa_code = lambda: False

    def _request_2fa():
        print_step(TAG_2F_AUTH, "requesting 2FA code from Apple...", one_liner=True)
        try:
            api.request_2fa_code()
        except Exception as err:
            # Not fatal, and deliberately does not disable the retries: the bridge
            # posts step0 (which makes Apple push the code) before the wait that
            # times out, so the user often has a valid code despite this error.
            print_step(TAG_2F_AUTH, f"2FA request warning: {err}", one_liner=True)
        delivery = getattr(api, "two_factor_delivery_method", "unknown")
        print_step(TAG_2F_AUTH, f"delivery method: {delivery}", one_liner=True)
        print_step(TAG_2F_AUTH, "waiting for 2FA code via Telegram...", one_liner=True)

    for attempt in range(1, TWO_FACTOR_CODE_ATTEMPTS + 1):
        # Apple's push is triggered once. Re-requesting on a retry would issue a
        # fresh code and invalidate the one the user is already holding.
        if attempt == 1:
            prompt = "provide the Apple 2FA code"
            after_send: Callable[[], None] | None = _request_2fa
        else:
            prompt = f"that code was rejected, send the Apple 2FA code again ({attempt}/{TWO_FACTOR_CODE_ATTEMPTS})"
            after_send = None

        code = prompt_telegram_reply(prompt, after_send=after_send, accept=_is_two_factor_code)
        if not code:
            # Timed out or Telegram failed. Retrying will not help: either nobody
            # is there to answer, or the transport is broken.
            print_step(TAG_2F_AUTH, "No code received from Telegram", one_liner=True)
            return False

        if _validate_two_factor_code(api, code):
            # The user is waiting on Telegram, where nothing else reports success:
            # print_step only reaches the terminal and the log file. Without this,
            # an accepted code looks identical to one that never arrived.
            send_telegram_message(TELEGRAM_2FA_ACCEPTED_MESSAGE)
            return True

    print_step(TAG_2F_AUTH, f"Failed to verify security code after {TWO_FACTOR_CODE_ATTEMPTS} attempts", one_liner=True)
    return False


def _describe_trusted_device(device: dict) -> str:
    """Label a trusted device for the on-screen picker, with the number masked.

    Only the last four digits are shown: enough to tell two phones apart, without
    putting a full number on screen or in a screenshot.
    """
    name = device.get("deviceName")
    if name:
        return str(name)
    number = str(device.get("phoneNumber") or "")
    digits = "".join(character for character in number if character.isdigit())
    return f"SMS to ****{digits[-4:]}" if digits else "SMS to an unlisted number"


def _validate_2fa_2sa(api: PyiCloudService) -> bool:
    """Handle legacy two-step authentication flow. Returns True on success."""
    devices = api.trusted_devices

    # Only the count is logged. print_step mirrors every message into
    # logs/calendar-merge.log, and trusted-device entries carry phone numbers and
    # device names -- CodeQL flags that as clear-text logging of sensitive data.
    print_step(TAG_2F_AUTH, f"Two-step authentication required. {len(devices)} trusted device(s) found.")

    # The picker itself is terminal-only, so the user can still tell devices apart
    # without any of it reaching the log file.
    for i, device in enumerate(devices):
        term.print(f"{get_tag(TAG_2F_AUTH)}   {i}: {_describe_trusted_device(device)}", True)

    device = click.prompt(f"{get_tag(TAG_2F_AUTH)} Which device would you like to use?", default=0)
    device = devices[device]
    if not api.send_verification_code(device):
        print_step(TAG_2F_AUTH, "Failed to send verification code", one_liner=True)
        return False

    send_telegram_message("Calendar merger triggered Apple two-step authentication. Please enter the validation code.")
    code = click.prompt(f"{get_tag(TAG_2F_AUTH)} Please enter validation code")
    if not api.validate_verification_code(device, code):
        print_step(TAG_2F_AUTH, "Failed to verify verification code", one_liner=True)
        return False
    return True


def _request_session_trust(api: PyiCloudService) -> SessionTrust:
    """Request session trust if needed, reporting what actually happened.

    Three outcomes rather than a bool, because "was already trusted" must not be
    reported to the user as "trust has just been established": `requires_2fa` is
    true whenever `hsaChallengeRequired` is set, even on a trusted session, so a
    run can prompt, fail, and find the session already trusted without anything
    having changed.
    """
    if api.is_trusted_session:
        return SessionTrust.already_trusted

    print_step(TAG_2F_AUTH, "Session is not trusted. Requesting trust...", one_liner=True)
    try:
        # Logged raw, coerced only for the decision: pyicloud returning something
        # richer than a bool is worth seeing when diagnosing a flaky 2FA run.
        trust_response = api.trust_session()
    except Exception as err:
        # trust_session() only catches PyiCloudAPIResponseException and
        # PyiCloud2FARequiredException, but _authenticate_with_token() raises
        # PyiCloudFailedLoginException, which is neither. Letting that escape
        # relabels an accurate "2FA validation failed" as the generic
        # "2FA validation error" -- and this now runs on the failure path, where
        # the session is least healthy.
        print_step(TAG_2F_AUTH, f"Session trust request failed: {err}", one_liner=True)
        return SessionTrust.refused

    print_step(TAG_2F_AUTH, f"Session trust result {trust_response}", one_liner=True)
    if not trust_response:
        print_step(
            TAG_2F_AUTH,
            "Failed to request trust. You will likely be prompted for confirmation again in the coming weeks",
            one_liner=True,
        )
        return SessionTrust.refused
    return SessionTrust.granted


def validate_2fa(api: PyiCloudService) -> bool:
    if api.requires_2fa:
        if api.security_key_names:
            # _validate_2fa_fido2 always reports success; its result has never
            # been checked here and changing that is out of scope.
            _validate_2fa_fido2(api)
            validated = True
        else:
            validated = _validate_2fa_trusted_device(api)

        # Attempted even when validation failed. Apple can refuse a code while
        # still granting trust -- observed on 2026-07-30, when the trusted-device
        # bridge failed to bootstrap so no code could validate, yet trust_session()
        # succeeded and the following run needed no 2FA at all. Skipping this made
        # the run honest but cost that recovery, so every later run prompted again.
        trust = _request_session_trust(api)

        # Only a fresh grant is worth reporting: an already-trusted session did
        # not stop this run from prompting, so promising the next one will be
        # quiet would be a false reassurance.
        if not validated and trust is SessionTrust.granted:
            print_step(
                TAG_2F_AUTH,
                "Code validation failed but the session is now trusted; the next run should not prompt",
                one_liner=True,
            )
            send_telegram_message(TELEGRAM_2FA_TRUSTED_AFTER_FAILURE_MESSAGE)

        return validated

    if api.requires_2sa:
        return _validate_2fa_2sa(api)

    return True


# endregion


def get_datetime(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, tzinfo=dt.tzinfo)


def get_from_list(items, value: str):
    try:
        return_value = items.get(value)
    except (AttributeError, TypeError):
        return_value = None

    return return_value


def build_datetime(dt: tuple | list, tz: ZoneInfo) -> datetime:
    """Build datetime from sequence [0, year, month, day, hour, minute]."""
    return datetime(dt[1], dt[2], dt[3], dt[4], dt[5], tzinfo=tz)


def get_tag(tag: str) -> str:
    return f"[{tag}]"


def print_step(tag: str, message: str, one_liner: bool = True):
    term.print(f"{get_tag(tag)} {message}", one_liner)
    # Mirror to the persistent log, stripped of ANSI color codes.
    level = logging.ERROR if _strip_ansi(tag) == "error" else logging.INFO
    logger.log(level, "[%s] %s", _strip_ansi(tag), message)


# region Telegram helpers


def send_telegram_message(message: str, disable_notification: bool = False) -> None:
    """Best-effort Telegram notifier used for important user-facing events."""
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
        loop.create_task(coro)


def _get_telegram_credentials() -> tuple[str, str] | None:
    """Return (token, chat_id) or None if not configured.

    Not a coroutine: it only reads environment variables. Marking it `async`
    implied it awaited something and forced its callers to await a value that was
    always available synchronously.
    """
    token = os.getenv(ENV_TELEGRAM_TOKEN)
    chat_id = os.getenv(ENV_TELEGRAM_CHAT_ID)
    if not token:
        term.print(f"{get_tag(TAG_ERROR)} Telegram token not configured", True)
        return None
    if not chat_id:
        term.print(f"{get_tag(TAG_ERROR)} Telegram chat id not configured", True)
        return None
    return token, chat_id


async def _send_via_notifier(notifier: tg.TelegramNotifier, message: str, disable_notification: bool = False) -> None:
    """Send a message using whichever method the notifier supports."""
    if hasattr(notifier, "send_message"):
        await notifier.send_message(message=message, disable_notification=disable_notification)
    elif disable_notification:
        await notifier.bot.send_message(chat_id=notifier.chat_id, text=message, disable_notification=True)
    else:
        await notifier.send(message)


async def _close_notifier(notifier: tg.TelegramNotifier) -> None:
    """Safely close a notifier that wasn't opened via async-with."""
    close_fn = getattr(notifier, "close", None)
    if close_fn:
        maybe_coro = close_fn()
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro


async def send_telegram_message_async(message: str, disable_notification: bool = False) -> None:
    if not message:
        return

    creds = _get_telegram_credentials()
    if creds is None:
        return
    token, chat_id = creds

    notifier_factory = tg.TelegramNotifier
    if hasattr(notifier_factory, "__aenter__"):
        async with notifier_factory(token=token, chat_id=chat_id) as notifier:
            await _send_via_notifier(notifier, message, disable_notification)
    else:
        notifier = notifier_factory(token=token, chat_id=chat_id)
        try:
            await _send_via_notifier(notifier, message, disable_notification)
        finally:
            await _close_notifier(notifier)


def prompt_telegram_reply(
    prompt: str,
    after_send: Callable[[], None] | None = None,
    accept: Callable[[str], bool] | None = None,
) -> str | None:
    """Send a prompt over Telegram and wait for a reply, or None.

    Mirrors send_telegram_message in swallowing transport errors: a flood-control
    response or a network blip is reported as a Telegram problem rather than
    bubbling up to be relabelled as a 2FA failure by the caller.
    """
    try:
        return asyncio.run(_wait_for_telegram_reply(prompt, after_send, accept))
    except Exception as err:
        term.print(f"{get_tag(TAG_ERROR)} Unexpected Telegram error: {err}", True)
        return None


def _usable_reply_text(update, mark: datetime, accept: Callable[[str], bool] | None) -> str | None:
    """Return the reply text of one update, or None if it should be ignored.

    Skips updates without text, replies that predate *mark* (so an answer sent
    before the prompt cannot be mistaken for this one), and anything *accept*
    rejects. A naive timestamp is read as UTC, which is what Telegram sends.
    """
    msg = getattr(update, "message", None)
    if not (msg and msg.text):
        return None

    msg_dt = msg.date
    if msg_dt and msg_dt.tzinfo is None:
        msg_dt = msg_dt.replace(tzinfo=UTC)
    if not (msg_dt and msg_dt >= mark):
        return None

    if accept is not None and not accept(msg.text):
        term.print(f"{get_tag(TAG_2F_AUTH)} ignoring reply that is not a 6-digit code", True)
        return None

    return msg.text


async def _poll_telegram_updates(
    notifier: tg.TelegramNotifier,
    mark: datetime,
    timeout_seconds: int,
    accept: Callable[[str], bool] | None = None,
) -> str | None:
    """Poll for a new text message arriving after *mark*, with timeout.

    When *accept* is given, replies it rejects are ignored and polling continues,
    so unrelated chatter in the group does not get submitted as the answer.
    """
    offset = None
    deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
    while datetime.now(UTC) < deadline:
        updates = await notifier.get_updates(offset=offset, timeout=30, allowed_updates=["message"])
        if not updates:
            await asyncio.sleep(1)
            continue
        offset = updates[-1].update_id + 1
        for upd in updates:
            text = _usable_reply_text(upd, mark, accept)
            if text is not None:
                return text

    term.print(f"{get_tag(TAG_ERROR)} Timed out waiting for Telegram reply", True)
    return None


async def _wait_for_telegram_reply(
    prompt: str,
    after_send: Callable[[], None] | None = None,
    accept: Callable[[str], bool] | None = None,
) -> str | None:
    creds = _get_telegram_credentials()
    if creds is None:
        return None
    token, chat_id = creds

    notifier_factory = tg.TelegramNotifier
    mark = datetime.now(UTC)

    async def _send_and_poll(notifier: tg.TelegramNotifier) -> str | None:
        await _send_via_notifier(notifier, prompt)
        if after_send:
            after_send()
        return await _poll_telegram_updates(notifier, mark, TELEGRAM_POLL_TIMEOUT_SECONDS, accept)

    if hasattr(notifier_factory, "__aenter__"):
        async with notifier_factory(token=token, chat_id=chat_id) as notifier:
            return await _send_and_poll(notifier)

    notifier = notifier_factory(token=token, chat_id=chat_id)
    try:
        return await _send_and_poll(notifier)
    finally:
        await _close_notifier(notifier)


# endregion

# region Pure logic helpers


def _normalize_skip_days(skip_days) -> list[str]:
    """Coerce a configured skip_days value into a list of weekday strings.

    Accepts what YAML actually produces: "5, 6" and "0,6" (plain scalars), a
    sequence like [5, 6], a bare scalar like `skip_days: 6`, and None or an empty
    value for "skip nothing".

    A bare scalar has to be wrapped rather than tested for truthiness, because
    `skip_days: 0` means Monday and would otherwise be discarded as falsy.
    """
    if skip_days is None:
        return []
    if isinstance(skip_days, str):
        skip_days = [day.strip() for day in skip_days.split(",") if day.strip()]
    elif not isinstance(skip_days, list | tuple | set):
        skip_days = [skip_days]
    return [str(day) for day in skip_days]


def _resolve_source_skip_days(yaml_helper: YamlHelper, section: str, default_skip_days: list[str]) -> list[str]:
    """Return a source's own skip_days, falling back to the global setting.

    The YamlError is caught deliberately and must not escape: main() treats a
    YamlError from a source as "no more source calendars" and stops the loop, so
    letting a merely-absent optional setting propagate would silently drop every
    remaining calendar. Callers must already have proven the section exists by
    reading a required setting first.
    """
    try:
        return _normalize_skip_days(yaml_helper.get(section, YAML_SETTING_SKIP_DAYS))
    except YamlError:
        return default_skip_days


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


def _reconcile_events(
    filtered_icloud_events: list[MergeEvent],
    source_calendar_events: list[MergeEvent],
) -> tuple[list[MergeEvent], bool]:
    """Match source events against iCloud events by (start, end) and assign actions.

    Returns (merge_events, has_additions). Mutates event.action in place on the
    inputs.
    """
    merge_events: list[MergeEvent] = []

    if filtered_icloud_events:
        source_event_map: dict[tuple[datetime, datetime], list[MergeEvent]] = {}
        for source_event in source_calendar_events:
            source_event_map.setdefault((source_event.start, source_event.end), []).append(source_event)

        for icloud_event in filtered_icloud_events:
            event_time = (icloud_event.start, icloud_event.end)
            bucket = source_event_map.get(event_time, [])
            if bucket:
                matched = bucket.pop(0)
                matched.action = EventAction.none
                icloud_event.action = EventAction.none
            else:
                icloud_event.action = EventAction.delete
            merge_events.append(icloud_event)

        events_to_add = [event for event in source_calendar_events if event.action is None]
        has_additions = len(events_to_add) > 0
    else:
        has_additions = len(source_calendar_events) > 0
        events_to_add = source_calendar_events

    if has_additions:
        for source_event in events_to_add:
            source_event.action = EventAction.add
            merge_events.append(source_event)

    return merge_events, has_additions


def _collect_icloud_events(raw_events: list) -> list[MergeEvent]:
    """Parse existing iCloud events into MergeEvents.

    Deliberately does not filter by skip_days: those are per source now, so the
    weekday filter is applied later against the owning source's setting.
    """
    events: list[MergeEvent] = []
    for raw_event in raw_events:
        all_day: bool = get_from_list(raw_event, ICLOUD_FIELD_ALL_DAY_EVENT)
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
        event_end_datetime = convert_to_utc(build_datetime(end_datetime, tzinfo))
        events.append(MergeEvent(event_title, start_datetime, event_end_datetime, raw_event, None))

    return events


def _select_source_icloud_events(
    icloud_events: list[MergeEvent], source_tag: str, skip_days: list[str]
) -> list[MergeEvent]:
    """Pick the existing iCloud events that belong to one source.

    Events on the source's skipped days are excluded so they are not reconciled,
    which matches how the source feed itself is filtered.
    """
    return [
        event for event in icloud_events if event.title == source_tag and str(event.start.weekday()) not in skip_days
    ]


def _is_google_feed(ics_calendar: Calendar) -> bool:
    """Return True when the feed was published by Google Calendar.

    Only Google gets the "any explicit TRANSP means skip" reading; everything
    else falls back to the RFC interpretation.
    """
    prodid = get_from_list(ics_calendar, ICS_FIELD_PRODID)
    return prodid is not None and ICS_PRODID_GOOGLE in str(prodid).upper()


def _is_excluded_event(file_event, google_feed: bool) -> bool:
    """Return True when a VEVENT should not be synced.

    Excludes free time on any feed, self-blocked time on Google feeds (lunch,
    focus time, out of office), and out-of-office events on Outlook feeds.
    Tentative Outlook events are kept -- see the TRANSP notes in CONSTS.
    """
    transparency = get_from_list(file_event, ICS_FIELD_TRANSPARENCY)
    if transparency is not None:
        if str(transparency).strip().upper() == ICS_TRANSPARENCY_FREE:
            return True
        if google_feed:
            return True

    busy_status = get_from_list(file_event, ICS_FIELD_MS_BUSY_STATUS)
    if busy_status is None:
        return False
    return str(busy_status).strip().upper() == ICS_MS_BUSY_OUT_OF_OFFICE


def _deduplicate_event_slots(events: list[MergeEvent]) -> list[MergeEvent]:
    """Collapse events from one calendar that occupy the exact same slot.

    Two source events with the same (start, end) are indistinguishable by the
    time they get here: parsed source events carry no title and no raw event, and
    every synced event is titled with the same source tag, so the merged calendar
    would just show identical blocks. What matters downstream is that the slot is
    busy, not how many meetings fill it.

    Only exact matches collapse. Overlapping or contained slots stay separate,
    since merging those means choosing new bounds.

    Deduplication is per calendar. Two sources that both hold the same slot still
    produce one event each, because they carry different source tags.
    """
    seen: set[tuple[datetime, datetime]] = set()
    unique: list[MergeEvent] = []
    for event in events:
        slot = (event.start, event.end)
        if slot in seen:
            continue
        seen.add(slot)
        unique.append(event)
    return unique


def _normalise_ics_datetime(value) -> datetime | None:
    """Rebuild an ICS datetime to minute precision in UTC.

    Returns None for anything that is not a datetime -- an all-day VEVENT carries a
    `date`, which this module does not sync.
    """
    if not isinstance(value, datetime):
        return None
    return convert_to_utc(datetime(value.year, value.month, value.day, value.hour, value.minute, tzinfo=value.tzinfo))


def _collect_recurrence_overrides(ics_calendar: Calendar) -> set[tuple[str, datetime]]:
    """`(UID, occurrence start)` pairs that a modified instance already accounts for.

    A moved or edited occurrence is published as its own VEVENT carrying
    `RECURRENCE-ID`, pointing at the slot in the series it replaces. Those VEVENTs
    are parsed by the ordinary path, so expanding the master over the same slot
    would place the meeting twice -- once at its new time and once at the original.
    """
    overrides: set[tuple[str, datetime]] = set()
    for file_event in ics_calendar.walk(ICS_TAG_VEVENT):
        recurrence_id = get_from_list(file_event, ICS_FIELD_RECURRENCE_ID)
        if recurrence_id is None:
            continue
        # A VEVENT carrying both properties is a THISANDFUTURE split: a master in its
        # own right, not an override of one. Registering it would make it suppress its
        # own first occurrence, losing that meeting entirely.
        if get_from_list(file_event, ICS_FIELD_RRULE) is not None:
            continue
        moment = _normalise_ics_datetime(recurrence_id.dt)
        uid = str(get_from_list(file_event, ICS_FIELD_UID) or "")
        # Without a UID the override cannot be attributed, and keying it on the empty
        # string would let it suppress that slot in every other UID-less series.
        if moment is not None and uid:
            overrides.add((uid, moment))
    return overrides


def _cancelled_occurrences(file_event) -> set[datetime]:
    """Occurrences removed from a series with EXDATE.

    Expanding without these invents busy time for meetings that were cancelled,
    which is worse than the under-reporting it set out to fix: an absent event can
    be checked against the source calendar, a phantom one is self-consistent.
    """
    raw = get_from_list(file_event, ICS_FIELD_EXDATE)
    if raw is None:
        return set()
    cancelled: set[datetime] = set()
    for entry in raw if isinstance(raw, list) else [raw]:
        for stamp in getattr(entry, "dts", []):
            moment = _normalise_ics_datetime(stamp.dt)
            if moment is not None:
                cancelled.add(moment)
    return cancelled


def _additional_occurrences(file_event, anchor: datetime) -> list[datetime]:
    """Extra dates attached to a series with RDATE, in the anchor's own frame.

    Returned unnormalised because the caller filters against window bounds it has
    already converted into that frame; normalisation happens with the rest.
    """
    raw = get_from_list(file_event, ICS_FIELD_RDATE)
    if raw is None:
        return []
    extras: list[datetime] = []
    for entry in raw if isinstance(raw, list) else [raw]:
        for stamp in getattr(entry, "dts", []):
            moment = stamp.dt
            if isinstance(moment, datetime) and bool(moment.tzinfo) == bool(anchor.tzinfo):
                extras.append(moment)
    return extras


def _expand_recurrence(
    file_event, uid: str, overrides: set[tuple[str, datetime]], window_start: datetime, window_end: datetime
) -> list[tuple[datetime, datetime]] | None:
    """The (start, end) slots a repeating VEVENT actually places inside the window.

    `walk` yields only the series master, whose DTSTART is the *first* occurrence --
    Outlook anchors that at the date the series was created, so a long-running weekly
    meeting has a DTSTART far outside any forward-looking window and contributes
    nothing at all without this.

    Returns None when the rule cannot be read at all, which the caller distinguishes
    from an empty list: an empty list means the series genuinely places nothing in
    the window, while None falls back to treating the master as a plain event so a
    malformed rule costs its occurrences rather than the meeting itself.
    """
    rule_field = get_from_list(file_event, ICS_FIELD_RRULE)
    start_field = get_from_list(file_event, ICS_FIELD_DATE_START)
    end_field = get_from_list(file_event, ICS_FIELD_DATE_END)
    if rule_field is None or start_field is None or end_field is None:
        return None

    anchor, finish = start_field.dt, end_field.dt
    if not isinstance(anchor, datetime) or not isinstance(finish, datetime):
        return None

    try:
        # Inside the try: a VEVENT may pair an aware DTSTART with a floating DTEND, and
        # subtracting those raises. Left outside, that escaped every guard here and
        # aborted the whole run -- the opposite of what this fallback exists for.
        duration = finish - anchor
        rule = rrulestr(rule_field.to_ical().decode(), dtstart=anchor)
        # Expand in the series' own frame; comparing across offsets needs matching awareness.
        lower = window_start.astimezone(anchor.tzinfo) if anchor.tzinfo else window_start.replace(tzinfo=None)
        upper = window_end.astimezone(anchor.tzinfo) if anchor.tzinfo else window_end.replace(tzinfo=None)
        occurrences = list(rule.between(lower, upper, inc=True))
        # DTSTART is the first occurrence even when it does not match the pattern --
        # dateutil deliberately omits it there, which silently drops a real meeting.
        if lower <= anchor <= upper and anchor not in occurrences:
            occurrences.append(anchor)
        # RDATE attaches extra one-off dates to a series.
        occurrences.extend(moment for moment in _additional_occurrences(file_event, anchor) if lower <= moment <= upper)
    except Exception:
        # Deliberately broad: this parses third-party feed data, and one unreadable
        # rule among hundreds of events must not sink the whole sync. exc_info keeps
        # it diagnosable rather than merely survivable.
        logger.warning("could not expand a recurrence rule; contributing its original occurrence only", exc_info=True)
        return None

    cancelled = _cancelled_occurrences(file_event)
    slots: list[tuple[datetime, datetime]] = []
    for occurrence in occurrences:
        start = _normalise_ics_datetime(occurrence)
        end = _normalise_ics_datetime(occurrence + duration)
        if start is None or end is None or start in cancelled or (uid, start) in overrides:
            continue
        slots.append((start, end))
    return slots


def _field_datetime(file_event, field: str) -> datetime | None:
    """Return a VEVENT date-time field in UTC, or None when absent or not a datetime.

    Collapses the get-field, take-.dt, check-type, normalise sequence that both the
    single-event and recurring paths need.
    """
    value = get_from_list(file_event, field)
    if value is None:
        return None
    return _normalise_ics_datetime(value.dt)


def _parse_single_event(
    file_event, skip_days: list[str], window_start: datetime, window_end: datetime
) -> MergeEvent | None:
    """Parse a non-recurring VEVENT, or None when it does not belong in the window."""
    start = _field_datetime(file_event, ICS_FIELD_DATE_START)
    if start is None or str(start.weekday()) in skip_days:
        return None
    if not (window_start <= start <= window_end):
        return None
    end = _field_datetime(file_event, ICS_FIELD_DATE_END)
    if end is None:
        return None
    return MergeEvent(None, start, end, None, None)


def _parse_recurring_event(
    file_event,
    skip_days: list[str],
    overrides: set[tuple[str, datetime]],
    window_start: datetime,
    window_end: datetime,
) -> list[MergeEvent] | None:
    """Occurrences a series places in the window, or None when its rule cannot be read.

    None is distinct from an empty list: empty means the series genuinely contributes
    nothing, while None tells the caller to fall back to the single-event path so an
    unreadable rule costs its repeats rather than the meeting itself.
    """
    uid = str(get_from_list(file_event, ICS_FIELD_UID) or "")
    occurrences = _expand_recurrence(file_event, uid, overrides, window_start, window_end)
    if occurrences is None:
        return None
    return [
        MergeEvent(None, start, end, None, None) for start, end in occurrences if str(start.weekday()) not in skip_days
    ]


def _parse_source_events(
    ics_calendar: Calendar, skip_days: list[str], utc_today_bod: datetime, utc_cut_off_date: datetime
) -> list[MergeEvent]:
    """Filter and parse VEVENTs from an ICS calendar into MergeEvents."""
    events: list[MergeEvent] = []
    # Both are properties of the whole feed, so resolve them once before the loop.
    google_feed = _is_google_feed(ics_calendar)
    overrides = _collect_recurrence_overrides(ics_calendar)
    for file_event in ics_calendar.walk(ICS_TAG_VEVENT):
        if _is_excluded_event(file_event, google_feed):
            continue

        if get_from_list(file_event, ICS_FIELD_RRULE) is not None:
            occurrences = _parse_recurring_event(file_event, skip_days, overrides, utc_today_bod, utc_cut_off_date)
            if occurrences is not None:
                events.extend(occurrences)
                # The master is only a template; its own DTSTART is the first
                # occurrence and the expansion already covers it when in range.
                continue
            # An unreadable rule falls through, so the meeting still contributes its
            # original occurrence instead of vanishing from the merged calendar.

        parsed = _parse_single_event(file_event, skip_days, utc_today_bod, utc_cut_off_date)
        if parsed is not None:
            events.append(parsed)
    return _deduplicate_event_slots(events)


def _is_missing_event_error(err: BaseException) -> bool:
    """True when the API reports the event is not there.

    Read from `code` when pyicloud raises its own exception and from the attached
    response otherwise: a 404 only reaches `PyiCloudAPIResponseException` when
    Apple answers with JSON, and surfaces as `requests.HTTPError` when it does not.
    """
    code = getattr(err, "code", None)
    if code is None:
        code = getattr(getattr(err, "response", None), "status_code", None)
    return str(code) == str(HTTP_NOT_FOUND)


def _sync_events_to_icloud(
    calendar_service, calendar_guid: str, calendar_tz: ZoneInfo, merge_events: list[MergeEvent], source_tag: str
) -> SyncOutcome:
    """Apply add/delete actions to the iCloud calendar, reporting what changed.

    The report is emitted from a `finally`, so a run that mutates the calendar and
    then fails still says what it managed to do. Without that, the case where the
    log matters most -- a partial sync, where the calendar alone cannot tell you
    whether anything ran -- is the one case it stayed silent about.

    Owning `term.print_done()` comes with that: the caller opens an unterminated
    "synchronizing..." line, and anything printed before it is closed lands glued to
    it. Since the failure path already closes the line via `term.print_failed()`,
    both ends now belong here.
    """
    outcome = SyncOutcome()
    try:
        _apply_event_actions(calendar_service, calendar_guid, calendar_tz, merge_events, outcome)
        # Completes the caller's pending "synchronizing..." line before the summary is
        # written, or the summary is appended to that line and "done!" is orphaned.
        # The failure path is already closed by term.print_failed() at the raise site.
        term.print_done()
    finally:
        summary = f"{source_tag}: {outcome.added} added, {outcome.deleted} deleted"
        if outcome.already_gone:
            summary += f", {outcome.already_gone} already gone"
        print_step(TAG_CALENDAR_MERGE, summary)
    return outcome


def _apply_event_actions(
    calendar_service, calendar_guid: str, calendar_tz: ZoneInfo, merge_events: list[MergeEvent], outcome: SyncOutcome
) -> None:
    """Add and delete events, tallying into `outcome` as each call succeeds."""
    actionable_events = [event for event in merge_events if event.action != EventAction.none]
    for merge_event in actionable_events:
        if merge_event.action == EventAction.add:
            try:
                calendar_service.add_event(
                    event=EventObject(
                        pguid=calendar_guid,
                        title=merge_event.title,
                        start_date=merge_event.start.astimezone(calendar_tz),
                        end_date=merge_event.end.astimezone(calendar_tz),
                    )
                )
                outcome.added += 1
            except Exception as err:
                term.print_failed()
                raise RuntimeError(f"Unable to add event {merge_event.title}") from err
        elif merge_event.action == EventAction.delete:
            assert merge_event.full_event is not None
            remove_event = EventObject(
                pguid=merge_event.full_event["pGuid"], guid=merge_event.full_event["guid"], title=merge_event.title
            )
            try:
                calendar_service.remove_event(remove_event)
                outcome.deleted += 1
            except Exception as err:
                # Deleting is idempotent in intent: a 404 means the event is already
                # gone, which is what the action was asking for. Aborting there costs
                # the rest of this calendar *and* every source calendar after it,
                # because the loop in main() only catches YamlError -- so one event
                # removed from a phone would sink the whole run.
                if not _is_missing_event_error(err):
                    term.print_failed()
                    raise RuntimeError(f"Unable to delete event {merge_event.title}") from err
                # Logged rather than swallowed: if a systemic fault ever made every
                # delete return 404, the merge would otherwise report success while
                # silently doing nothing.
                outcome.already_gone += 1
                print_step(TAG_CALENDAR_MERGE, f"{merge_event.title} was already gone from iCloud")


# endregion

# region main flow helpers


def _load_config() -> tuple[YamlHelper, int, list[str], FileSystem]:
    """Load .env, parse config.yaml, return (yaml_helper, future_event_days, skip_days, fs)."""
    print_step(TAG_CALENDAR_MERGE, "reading config...", one_liner=False)
    load_dotenv()
    fs = FileSystem()
    config_path = fs.join_paths(str(Path(__file__).resolve().parent.parent), YAML_FILENAME)
    try:
        yaml_helper = YamlHelper(config_path)
    except Exception as err:
        term.print_failed()
        raise RuntimeError("Unable to open YAML configuration") from err

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
    return yaml_helper, future_event_days, skip_days, fs


def _disable_automatic_2fa_requests() -> None:
    """Stop pyicloud from asking Apple for a 2FA code on its own.

    pyicloud 2.6.5 added `_request_2fa_code`, called from inside `authenticate()`
    -- which `PyiCloudService` runs in its own constructor. It pushes to the
    trusted device and then, if the Apple ID has a trusted phone number, sends an
    SMS as well. It consults neither `_can_request_sms_2fa_code` nor anything else
    this module can set: the instance does not exist yet when it runs, so
    `_validate_2fa_trusted_device`'s guard is assigned far too late to matter.

    That works directly against the retry loop, which issues the push on the first
    attempt only because every fresh request invalidates the code the user is
    holding. Left alone, one re-authentication delivers a push, an SMS and then
    another push, and the code the user reads off their phone may already be dead.

    Suppressed so this module remains the only thing that asks Apple for a code.
    The explicit `api.request_2fa_code()` in `_validate_2fa_trusted_device` still
    runs, still pushes, and still honours the SMS guard.

    Patched on the class rather than the instance because the call happens during
    construction. Guarded by `hasattr` so an older pyicloud, or a release that
    drops the method, is left untouched instead of gaining a stub.
    """
    if hasattr(PyiCloudService, PYICLOUD_AUTO_2FA_METHOD):
        setattr(PyiCloudService, PYICLOUD_AUTO_2FA_METHOD, lambda self: None)


def _authenticate_icloud() -> PyiCloudService:
    """Connect to iCloud and handle 2FA. Returns the authenticated service."""
    print_step(TAG_ICLOUD_AUTH, "authenticating with iCloud...", one_liner=False)
    _disable_automatic_2fa_requests()
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
    return icloud_service


def _load_icloud_events(icloud_service: PyiCloudService, future_event_days: int, skip_days: list[str]) -> tuple:
    """Fetch calendar GUID, load iCloud events, compute date range.

    Returns (calendar_service, calendar_guid, icloud_events, today_bod, cut_off_date).
    """
    print_step(TAG_CALENDAR_MERGE, "loading iCloud calendar events...", one_liner=False)
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

    icloud_events = _collect_icloud_events(all_icloud_events)

    now = datetime.now().astimezone()
    today_bod = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=now.tzinfo)
    cut_off_candidate = _calculate_future_date(now, future_event_days, skip_days)
    cut_off_date = _end_of_day(cut_off_candidate)

    term.print_done()
    return calendar_service, calendar_guid, icloud_events, today_bod, cut_off_date, now


class SourceConfigOutcome(Enum):
    """What a `YamlError` from a source-calendar read actually means.

    Three cases, not two. `main()` uses the required `source` read as its end-of-list
    signal, but `YamlHelper.get` raises the same type when the section exists and omits
    a setting, *and* when the config file itself cannot be read -- and it re-reads the
    file on every call, so a file-level fault repeats at every index.
    """

    absent = 0
    """No section at this index: the list has ended."""

    malformed = 1
    """The section exists but is broken: this calendar fails, the others proceed."""

    unusable = 2
    """The config file itself: every later index fails identically, so stop."""


def _classify_source_config_error(err: YamlError) -> SourceConfigOutcome:
    """Sort a `YamlError` into end-of-list, broken calendar, or unusable file.

    pyfangs distinguishes them only by message prefix -- "Missing key: '<section>'",
    "Missing setting: '<name>'", and four file-level forms. That coupling to message
    text is deliberate and monitored: `TestYamlErrorShapes` pins the shapes against the
    real `YamlHelper`, so a change to pyfangs' wording fails the build rather than
    silently reclassifying an error.

    Defaulting the unknown case to `unusable` is the safe choice: treating a file-level
    fault as a broken section logged it once per index and reported more failures than
    the user has calendars.
    """
    message = str(err)
    if message.startswith(YAML_ABSENT_SECTION_PREFIX):
        return SourceConfigOutcome.absent
    if message.startswith(YAML_MISSING_SETTING_PREFIX):
        return SourceConfigOutcome.malformed
    return SourceConfigOutcome.unusable


def _summarise_source_failures(failures: list[tuple[str, str]], attempted: int) -> str:
    """One line naming every failed source, with as much of each cause as fits.

    The `__main__` handler condenses the alert to `ERROR_PART_MAX_CHARS`, so a summary
    built from unbounded per-source text loses every failure after the first -- exactly
    the behaviour aggregating them was meant to replace. Each cause therefore gets an
    equal share of what remains after the header and the section names, so the
    identities always survive and only the details are trimmed.
    """
    header = f"{len(failures)} of {attempted} source calendars failed"
    overhead = len(header) + 3 + sum(len(section) + 2 for section, _ in failures) + 2 * (len(failures) - 1)
    share = max(SOURCE_FAILURE_MIN_DETAIL, (ERROR_PART_MAX_CHARS - overhead) // len(failures))
    details = "; ".join(f"{section}: {_condense(cause, share)}" for section, cause in failures)
    return f"{header} - {details}"


def _process_source_calendar(
    yaml_helper: YamlHelper,
    source_index: int,
    fs: FileSystem,
    now: datetime,
    default_skip_days: list[str],
    utc_today_bod: datetime,
    utc_cut_off_date: datetime,
    icloud_events: list[MergeEvent],
    calendar_service,
    calendar_guid: str,
) -> None:
    """Download, parse, reconcile, and sync a single source calendar.

    `default_skip_days` is the global `config.skip_days`, used unless the source
    section declares its own.
    """
    section = YAML_SECTION_SOURCE_CALENDAR.format(index=source_index)
    calendar_source = yaml_helper.get(section, YAML_SETTING_CALENDAR_SOURCE)

    print_step(
        TAG_CALENDAR_MERGE, f"reading {calendar_source} [{source_index}] source calendar config...", one_liner=False
    )
    try:
        calendar_tag = yaml_helper.get(section, YAML_SETTING_CALENDAR_TAG)
        calendar_title = yaml_helper.get(section, YAML_SETTING_CALENDAR_TITLE)
        calendar_tz = ZoneInfo(yaml_helper.get(section, YAML_SETTING_CALENDAR_TZ))
    except Exception as err:
        term.print_failed()
        raise RuntimeError(f"Invalid calendar configuration at index {source_index}") from err

    # Optional per-source override; the section is known to exist by now because
    # the required settings above were read successfully.
    skip_days = _resolve_source_skip_days(yaml_helper, section, default_skip_days)

    calendar_url = os.getenv(ENV_VAR_CALENDAR_URL.format(index=source_index))
    if not calendar_url:
        term.print_failed()
        raise RuntimeError(f"Missing calendar URL for index {source_index}")
    term.print_done()

    print_step(TAG_CALENDAR_MERGE, f"downloading calendar {calendar_source} [{source_index}]...", one_liner=False)
    timestamp_filename = fs.join_paths(fs.get_temp_dir(), f"{convert_to_utc(now).strftime('%Y%m%d%H%M%S%f')}.ics")
    try:
        fs.download(calendar_url, timestamp_filename)
    except Exception as err:
        term.print_failed()
        raise RuntimeError(f"Unable to download calendar {calendar_source} [{source_index}]") from err
    term.print_done()

    print_step(TAG_CALENDAR_MERGE, f"reading source calendar from {timestamp_filename}...", one_liner=False)
    try:
        with open(timestamp_filename, "rb") as ics_file:
            ics_calendar = Calendar.from_ical(ics_file.read())
    except Exception as err:
        term.print_failed()
        raise RuntimeError(f"Unable to parse calendar {calendar_source} [{source_index}]") from err
    term.print_done()

    print_step(
        TAG_CALENDAR_MERGE,
        f"filtering events from source calendar {calendar_source} [{source_index}]...",
        one_liner=False,
    )
    # Guarded so a raise cannot leave the pending one-liner open. The loop in main()
    # now continues to the next calendar, which would print its first step glued onto
    # the unterminated line -- and every other raise site here closes its own line, so
    # leaving these three unguarded made that claim false.
    try:
        source_calendar_events = _parse_source_events(ics_calendar, skip_days, utc_today_bod, utc_cut_off_date)
    except Exception as err:
        term.print_failed()
        raise RuntimeError(f"Unable to read events from calendar {calendar_source} [{source_index}]") from err
    term.print_done()

    source_tag = f"[{calendar_tag}] {calendar_title}/{calendar_source}"
    print_step(TAG_CALENDAR_MERGE, f"filtering {source_tag} events in iCloud calendar...", one_liner=False)
    try:
        filtered_icloud_events = _select_source_icloud_events(icloud_events, source_tag, skip_days)
    except Exception as err:
        term.print_failed()
        raise RuntimeError(f"Unable to select iCloud events for {source_tag}") from err
    term.print_done()

    print_step(TAG_CALENDAR_MERGE, "reconciling events...", one_liner=False)
    try:
        merge_events, event_addition = _reconcile_events(filtered_icloud_events, source_calendar_events)
    except Exception as err:
        term.print_failed()
        raise RuntimeError(f"Unable to reconcile events for {source_tag}") from err
    if event_addition:
        for event in merge_events:
            if event.action == EventAction.add:
                event.title = source_tag
    term.print_done()

    print_step(TAG_CALENDAR_MERGE, "synchronizing events to iCloud calendar...", one_liner=False)
    # Reported per source rather than per run: a total says the merge did something,
    # this says which calendar it happened to. Emitted inside the call so it survives
    # a mid-sync failure.
    _sync_events_to_icloud(calendar_service, calendar_guid, calendar_tz, merge_events, source_tag)


# endregion


def main():
    parser = argparse.ArgumentParser(description="Calendar merge with Telegram notifications.")
    parser.add_argument("--first", action="store_true", help="Send start-of-day Telegram notification.")
    parser.add_argument("--last", action="store_true", help="Send end-of-day Telegram notification.")
    args = parser.parse_args()

    yaml_helper, future_event_days, skip_days, fs = _load_config()

    if args.first:
        send_telegram_message("☀️ calendar-merge started for today.")

    icloud_service = _authenticate_icloud()
    calendar_service, calendar_guid, icloud_events, today_bod, cut_off_date, now = _load_icloud_events(
        icloud_service, future_event_days, skip_days
    )

    utc_today_bod = convert_to_utc(today_bod)
    utc_cut_off_date = convert_to_utc(cut_off_date)

    print_step(
        TAG_CALENDAR_MERGE,
        term.TerminalColors.yellow.value + "processing source calendars" + term.TerminalColors.reset.value,
        one_liner=True,
    )
    source_index = 0
    failures: list[tuple[str, str]] = []
    while True:
        if source_index > MAX_SOURCE_CALENDARS:
            # A runaway guard, not a policy limit: YamlHelper leaks TypeError rather
            # than YamlError when the config's top level or a section's value is a
            # list, and that repeats at every index, so without this the loop would
            # never end. Strictly greater, so a configuration of exactly
            # MAX_SOURCE_CALENDARS still reaches its terminating lookup rather than
            # being reported as a failure. The wording describes what happened instead
            # of implying a configured maximum.
            failures.append(
                ("configuration", f"gave up after {source_index} indexes without reaching the end of the list")
            )
            break
        try:
            _process_source_calendar(
                yaml_helper,
                source_index,
                fs,
                now,
                skip_days,
                utc_today_bod,
                utc_cut_off_date,
                icloud_events,
                calendar_service,
                calendar_guid,
            )
        except YamlError as err:
            outcome = _classify_source_config_error(err)
            if outcome is SourceConfigOutcome.unusable:
                # The config file itself, not this section. YamlHelper re-reads it on
                # every call, so continuing would hit the identical error at every
                # index -- logging it a hundred times and reporting more failures than
                # the user has calendars.
                raise
            if outcome is SourceConfigOutcome.malformed:
                # The section exists but is broken. Treating it as the end of the list
                # would skip every calendar after it without recording anything.
                section = YAML_SECTION_SOURCE_CALENDAR.format(index=source_index)
                failures.append((section, _describe_error(err)))
                logger.exception("source calendar %d is misconfigured", source_index)
                source_index += 1
                continue
            # Not a failure: this is how the loop learns there is no section at this
            # index. It must stay ahead of the handler below, which would otherwise
            # read the end of the list as a broken calendar and never terminate.
            print_step(
                TAG_CALENDAR_MERGE,
                term.TerminalColors.yellow.value
                + "no more source calendars to process"
                + term.TerminalColors.reset.value,
                one_liner=True,
            )
            break
        except Exception as err:  # one calendar must not cost the calendars after it
            # Aborting here left healthy sources holding yesterday's picture with
            # nothing to say so: on 2026-08-20 a single already-deleted event in
            # source 0 meant sources 1 and 2 were never processed at all.
            #
            # term.print_failed() is deliberately not called -- every raise site in
            # _process_source_calendar already closed its own pending line.
            failures.append((YAML_SECTION_SOURCE_CALENDAR.format(index=source_index), _describe_error(err)))
            logger.exception("source calendar %d failed", source_index)
        source_index += 1

    if failures:
        # Raised only once every source has had its turn, so a single alert describes
        # the whole run instead of whichever calendar happened to fail first. The
        # __main__ handler turns this into the Telegram message.
        raise RuntimeError(_summarise_source_failures(failures, source_index))

    if args.last:
        send_telegram_message("🌙 calendar-merge finished for today.")


if __name__ == "__main__":
    _configure_logging()
    logger.info("calendar-merge started")

    merge_start = perf_counter()
    term.print_header_box("iCloud calendar merger")

    try:
        main()
    except Exception as err:
        described = _describe_error(err)
        term.print(f"{get_tag(TAG_ERROR)} An error occurred during the merge process: {described}")
        logger.exception("Merge process failed")
        send_telegram_message(f"Calendar merge failed: {described}")

    merge_end = perf_counter()
    total = merge_end - merge_start
    term.print_header_box("merge & sync completed", f"total time: {total:.3f} seconds")
    logger.info("calendar-merge completed in %.3f seconds", total)
