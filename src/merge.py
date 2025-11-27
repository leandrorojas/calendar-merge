# imports
import os
import click
import argparse


# partial imports
from pathlib import Path
import asyncio
from pyicloud import PyiCloudService
from pyicloud.services.calendar import EventObject
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
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

TAG_2F_AUTH = term.TerminalColors.magenta.value + "2f_auth" + term.TerminalColors.reset.value
TAG_CALENDAR_MERGE = term.TerminalColors.cyan.value + "cal-merge" + term.TerminalColors.reset.value
TAG_ICLOUD_AUTH = term.TerminalColors.orange.value + "icloud_auth" + term.TerminalColors.reset.value
TAG_ERROR = term.TerminalColors.red.value + "error" + term.TerminalColors.reset.value
#endregion

_TELEGRAM_NOTIFIER: tg.TelegramNotifier | None = None

class EventAction(Enum):
    none = 0
    add = 1
    delete = 2

@dataclass
class MergeEvent(object):
    title:str
    start:datetime
    end:datetime
    full_event:EventObject
    action:EventAction

def validate_2fa(api: PyiCloudService) -> bool:
    #TODO: the terminal is expecting "done" or "failed" messages. So the first print of each section must consider this
    status:bool = True
    global _TELEGRAM_NOTIFIER

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
            send_telegram_message("Calendar merger needs the Apple 2FA code. Please provide or approve the code.")
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
    global _TELEGRAM_NOTIFIER

    if not message:
        return

    token = os.getenv(ENV_TELEGRAM_TOKEN)
    chat_id = os.getenv(ENV_TELEGRAM_CHAT_ID)

    if not token:
        term.print(f"{get_tag(TAG_ERROR)} Telegram token not configured", True)
        return

    if _TELEGRAM_NOTIFIER is None:
        # Lazy init so we only create the notifier if we actually need to send something.
        # Reuse the same bot instance for the whole process.
        _TELEGRAM_NOTIFIER = tg.TelegramNotifier(token=token, chat_id=chat_id)

    notifier = _TELEGRAM_NOTIFIER
    if disable_notification:
        await notifier.bot.send_message(chat_id=notifier.chat_id, text=message, disable_notification=True)
    else:
        await notifier.send(message)

def prompt_telegram_reply(prompt: str) -> str | None:
    return asyncio.run(_wait_for_telegram_reply(prompt))

async def _wait_for_telegram_reply(prompt: str) -> str | None:
    global _TELEGRAM_NOTIFIER

    mark = datetime.now(timezone.utc)
    await send_telegram_message_async(prompt)  # initializes _TELEGRAM_NOTIFIER
    notifier = _TELEGRAM_NOTIFIER
    offset = None

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

def _normalize_skip_days(skip_days) -> list[str]:
    if isinstance(skip_days, str):
        skip_days = [day.strip() for day in skip_days.split(",") if day.strip()]
    return [str(day) for day in (skip_days or [])]

def _get_ai_tone(yaml_helper:YamlHelper) -> str | None:
    try:
        return yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_AI_TONE)
    except Exception:
        return None

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
    args = parser.parse_args()

    #region CONFIG_LOAD
    print_step(TAG_CALENDAR_MERGE, "reading config...", False)
    load_dotenv()
    fs = FileSystem()
    config_path = fs.join_paths(str(Path(__file__).resolve().parent.parent), YAML_FILENAME)
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
            "Write a cheerful good morning message, different every time. One-liner only.",
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
    response = prompt_telegram_reply("show me the code byatch")
    term.print_done()
    term.print(response)
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
    filter_end = datetime.today() + timedelta(days=future_event_days)

    try:
        all_icloud_events = calendar_service.get_events(from_dt=filter_start, to_dt=filter_end)
    except Exception as err:
        term.print_failed()
        raise RuntimeError("Unable to load events from iCloud") from err

    icloud_events = _collect_icloud_events(all_icloud_events, skip_days)

    now = datetime.now()
    today_bod = datetime(now.year, now.month , now.day, 0, 0, 0)
    cut_off_candidate = now + timedelta(days=future_event_days)
    cut_off_date = datetime(cut_off_candidate.year, cut_off_candidate.month, cut_off_candidate.day, 23, 59, 59)

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
                remove_event = EventObject(pguid=merge_event.full_event["pGuid"],  guid=merge_event.full_event["guid"], title=merge_event.title)
                try:
                    calendar_service.remove_event(remove_event)
                except Exception as err:
                    term.print_failed()
                    raise RuntimeError(f"Unable to delete event {merge_event.title}") from err
        term.print_done()

        source_index += 1

    if args.last:
        send_ai_telegram_message(
            ai_client,
            "Write a cheerful end of day wrap-up message, different every time. One-liner only.",
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
