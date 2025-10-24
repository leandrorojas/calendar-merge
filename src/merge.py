# imports
import os

# partial imports
from pathlib import Path
from pyicloud import PyiCloudService
from pyicloud.services.calendar import EventObject
from dotenv import load_dotenv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from enum import Enum
from icalendar import Calendar

# custom imports
from pyfangs.yaml import YamlHelper
from pyfangs.filesystem import FileSystem
from pyfangs.time import convert_to_utc
import pyfangs.terminal as term

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
ICS_FIELD_OOO = "TRANSP"

ENV_ICLOUD_USER = "ICLOUD_USERNAME"
ENV_ICLOUD_PASS = "ICLOUD_PASSWORD"
ENV_VAR_CALENDAR_URL = "CALENDAR_URL_{index}"

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

def validate_2fa(api: PyiCloudService) -> bool: #TODO: add try/except blocks
    status:bool = True

    if api.requires_2fa:
        security_key_names = api.security_key_names

        if security_key_names:
            print(f"Security key confirmation is required. Please plug in one of the following keys: {', '.join(security_key_names)}")

            devices = api.fido2_devices

            print("Available FIDO2 devices:")

            for idx, dev in enumerate(devices, start=1):
                print(f"{idx}: {dev}")

            choice = click.prompt("Select a FIDO2 device by number", type=click.IntRange(1, len(devices)), default=1,)
            selected_device = devices[choice - 1]

            print("Please confirm the action using the security key")

            api.confirm_security_key(selected_device)

        else:
            print("Two-factor authentication required.")
            result = api.validate_2fa_code(input("Enter the code you received of one of your approved devices: "))
            print("Code validation result: %s" % result)

            if not result:
                print("Failed to verify security code")
                status = False

        if not api.is_trusted_session:
            print("Session is not trusted. Requesting trust...")
            result = api.trust_session()
            print("Session trust result %s" % result)

            if not result:
                print("Failed to request trust. You will likely be prompted for confirmation again in the coming weeks")

    elif api.requires_2sa:
        import click
        print("Two-step authentication required. Your trusted devices are:")

        devices = api.trusted_devices
        for i, device in enumerate(devices):
            print("  %s: %s" % (i, device.get('deviceName', "SMS to %s" % device.get('phoneNumber'))))

        device = click.prompt('Which device would you like to use?', default=0)
        device = devices[device]
        if not api.send_verification_code(device):
            print("Failed to send verification code")
            status = False

        code = click.prompt('Please enter validation code')
        if not api.validate_verification_code(device, code):
            print("Failed to verify verification code")
            status = False

    return status

def get_datetime(dt:datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, tzinfo=dt.tzinfo)

def get_from_list(items:list, value:str): #TODO: verify each call and validate if returned value is None
    try:
        return_value = items.get(value)
    except:
        return_value = None

    return return_value

def build_datetime(dt, tz) -> datetime:
    return datetime(dt[1], dt[2], dt[3], dt[4], dt[5], tzinfo=tz)

def main():
    load_dotenv()
    fs = FileSystem()
    
    try:
        yaml_helper = YamlHelper(fs.join_paths(str(Path(__file__).resolve().parent.parent), YAML_FILENAME))
    except:
        pass

    try:
        icloud_service = PyiCloudService(os.getenv(ENV_ICLOUD_USER), os.getenv(ENV_ICLOUD_PASS))
    except:
        pass

    try:
        future_event_days = yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_FUTURE_EVENTS_DAYS)
    except:
        pass

    now = datetime.now()
    # filter dates can be passed in local tz
    today_bod = datetime(now.year, now.month , now.day, 0, 0, 0)
    cut_off_date = now + timedelta(days=future_event_days)
    cut_off_date = datetime(cut_off_date.year, cut_off_date.month, cut_off_date.day, 23, 59, 59)

    if (validate_2fa(icloud_service)):
        calendar_service = icloud_service.calendar

        try:
            calendar_guid = calendar_service.get_calendars()[0].get("guid")
        except:
            pass

        filter_start = datetime.today()

        try:
            filter_end = datetime.today() + timedelta(days=future_event_days)
        except:
            pass

        try:
            all_icloud_events = calendar_service.get_events(from_dt=filter_start, to_dt=filter_end)
        except:
            pass

        icloud_events:list[MergeEvent] = []

        try:
            skip_days = yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_SKIP_DAYS)
        except:
            pass

        for icloud_event in all_icloud_events:
            all_day:bool = get_from_list(icloud_event, ICLOUD_FIELD_ALL_DAY_EVENT)

            if all_day is not None:
                if (all_day == False):
                    start_datetime = get_from_list(icloud_event, ICLOUD_FIELD_START_DATE)

                    if (start_datetime is not None):
                        tzinfo = get_from_list(icloud_event, ICLOUD_FIELD_TZ)

                        if tzinfo is not None:
                            tzinfo = ZoneInfo(tzinfo)
                            start_datetime = convert_to_utc(build_datetime(start_datetime, tzinfo))
                            
                            if str(start_datetime.weekday()) not in skip_days:
                                end_datetime = get_from_list(icloud_event, ICLOUD_FIELD_END_DATE)

                                if end_datetime is not None:
                                    event_end_datetime = convert_to_utc(build_datetime(end_datetime, tzinfo))
                                    event_title = get_from_list(icloud_event, ICLOUD_FIELD_TITLE)

                                    if event_title is not None:
                                        icloud_events.append(MergeEvent(event_title, start_datetime, event_end_datetime, icloud_event, None))
                                    else:
                                        pass
                                else:
                                    pass
                        else:
                            pass
                    else:
                        pass
            else:
                pass

        source_index = 0
        end_of_calendars = False

        # loop though each configured calendar in yaml
        while not end_of_calendars:
            try:
                # read it’s config
                calendar_source = yaml_helper.get(YAML_SECTION_SOURCE_CALENDAR.format(index=source_index), YAML_SETTING_CALENDAR_SOURCE)
            # if error end of config
            except Exception as e:
                end_of_calendars = True
                continue

            if not end_of_calendars:
                calendar_section = YAML_SECTION_SOURCE_CALENDAR.format(index=source_index)
                # continue reading it’s config
                try:
                    calendar_tag = yaml_helper.get(calendar_section, YAML_SETTING_CALENDAR_TAG)
                except:
                    pass

                try:
                    calendar_title = yaml_helper.get(calendar_section, YAML_SETTING_CALENDAR_TITLE)
                except:
                    pass

                try:
                    calendar_tz = ZoneInfo(yaml_helper.get(calendar_section, YAML_SETTING_CALENDAR_TZ))
                except:
                    pass

                try:
                    # read the url from the .env
                    calendar_url = os.getenv(ENV_VAR_CALENDAR_URL.format(index=source_index))
                except:
                    pass

                # download calendar
                timestamp_filename = fs.join_paths(fs.get_temp_dir(), f"{convert_to_utc(now).strftime('%Y%m%d%H%M%S%f')}.ics")
                try:
                    fs.download(calendar_url, timestamp_filename)
                except:
                    pass

                # prepare index for next iteration
                source_index += 1

                #read calendar
                try:
                    with open(timestamp_filename, "rb") as ics_file:
                        ics_calendar = Calendar.from_ical(ics_file.read())
                except:
                    pass
                
                source_calendar_events:list[MergeEvent] = []
                utc_today_bod = convert_to_utc(today_bod)
                utc_cut_off_date = convert_to_utc(cut_off_date)

                # filter events with config
                for file_event in ics_calendar.walk(ICS_TAG_VEVENT):
                    ooo_status = get_from_list(file_event, ICS_FIELD_OOO) # verify if the event is "opaque" wich means "out of office"

                    if (ooo_status is None):
                        start_datetime = get_from_list(file_event, ICS_FIELD_DATE_START)

                        if start_datetime is not None:
                            start_datetime:datetime = start_datetime.dt
                            if (isinstance(start_datetime, datetime) == True):
                                start_datetime = convert_to_utc(datetime(start_datetime.year, start_datetime.month, start_datetime.day, start_datetime.hour, start_datetime.minute, tzinfo=start_datetime.tzinfo))

                                if ((str(start_datetime.weekday()) not in skip_days) and (start_datetime >= utc_today_bod and start_datetime <= utc_cut_off_date)):
                                    end_datetime = get_from_list(file_event, ICS_FIELD_DATE_END)

                                    if end_datetime is not None:
                                        end_datetime:datetime = end_datetime.dt
                                        end_datetime = convert_to_utc(datetime(end_datetime.year, end_datetime.month, end_datetime.day, end_datetime.hour, end_datetime.minute, tzinfo=end_datetime.tzinfo))
                                        source_calendar_events.append(MergeEvent(None, start_datetime, end_datetime, None, None))
                                    else:
                                        pass
                        else:
                            pass

                # compare events from source calendar and icloud calendar
                source_tag = f"[{calendar_tag}] {calendar_title}/{calendar_source}"

                filtered_icloud_events = list(filter(lambda e: e.title == source_tag, icloud_events))
                merge_events:list[MergeEvent] = []
                event_addtion = False

                # if at least one current event
                if len(filtered_icloud_events) > 0:
                    # for each filtered iCloud event
                    for icloud_event in filtered_icloud_events:
                        source_filtered_events = list(filter(lambda e: e.start == icloud_event.start and e.end == icloud_event.end, source_calendar_events))

                        event_action:EventAction = None
                        # if the event is not in the source calendar, mark it for deletion
                        if len(source_filtered_events) == 0:
                            event_action = EventAction.delete
                            # icloud_event.action = EventAction.delete
                        else:
                            event_action = EventAction.none
                            # icloud_event.action = EventAction.none

                        icloud_event.action = event_action
                        
                        for source_event in source_filtered_events:
                            source_event.action = event_action

                        merge_events.append(icloud_event)

                    # for each source event that is still none, mark it for addition
                    events_to_add = list(filter(lambda e: e.action == None, source_calendar_events))
                    event_addtion = len(events_to_add) > 0

                else: # if no current events, all source events are new
                    event_addtion = True
                    events_to_add = source_calendar_events

                if (event_addtion == True):
                    for source_event in events_to_add:
                        source_event.title = source_tag
                        source_event.action = EventAction.add
                        merge_events.append(source_event)

                    event_addtion = False

                if len(merge_events) > 0:
                    merge_events = filter(lambda e: e.action != EventAction.none, merge_events)
                    for merge_event in merge_events:
                        if merge_event.action == EventAction.add:
                            try:
                                calendar_service.add_event(event=EventObject(pguid=calendar_guid, title=merge_event.title, start_date=merge_event.start.astimezone(calendar_tz), end_date=merge_event.end.astimezone(calendar_tz)))
                            except:
                                pass
                        elif merge_event.action == EventAction.delete:
                            remove_event = EventObject(pguid=merge_event.full_event["pGuid"],  guid=merge_event.full_event["guid"], title=merge_event.title)
                            try:
                                calendar_service.remove_event(remove_event)
                            except:
                                pass

if __name__ == "__main__":
    main()
