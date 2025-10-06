# imports
import os

# partial imports
from pathlib import Path
from pyicloud import PyiCloudService
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone, date
from dataclasses import dataclass
from enum import Enum
from icalendar import Calendar

# custom imports
from pyfangs.yaml import YamlHelper
from pyfangs.filesystem import FileSystem
from pyfangs.time import convert_to_utc

YAML_SECTION_GENERAL = "config"
YAML_SETTING_SKIP_DAYS = "skip_days"
YAML_SETTING_FUTURE_EVENTS_DAYS = "future_events_days"

YAML_SECTION_SOURCE_CALENDAR = "source-calendar-{index}"
YAML_SETTING_CALENDAR_SOURCE = "source"
YAML_SETTING_CALENDAR_TAG = "tag"
YAML_SETTING_CALENDAR_TITLE = "title"

ICLOUD_FIELD_START_DATE = "startDate"
ICLOUD_FIELD_END_DATE = "endDate"
ICLOUD_FIELD_TITLE = "title"

ICS_FIELD_DATE_START = "dtstart"
ICS_FIELD_DATE_END = "dtend"

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
    action:EventAction

def validate_2fa(api: PyiCloudService) -> bool:
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
            code = input("Enter the code you received of one of your approved devices: ")
            result = api.validate_2fa_code(code)
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

def main():

    load_dotenv()
    yaml_helper = YamlHelper(Path(__file__).resolve().parent.parent / "config.yaml")
    icloud_service = PyiCloudService(os.getenv("ICLOUD_USERNAME"), os.getenv("ICLOUD_PASSWORD"))
    now = datetime.now()

    today_bod = convert_to_utc(datetime(now.year, now.month , now.day, 0, 0, 0))
    cut_off_date = now + timedelta(days=yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_FUTURE_EVENTS_DAYS))
    cut_off_date = convert_to_utc(datetime(cut_off_date.year, cut_off_date.month, cut_off_date.day, 23, 59, 59))

    if (validate_2fa(icloud_service)):
        calendar_service = icloud_service.calendar

        calendar_events = calendar_service.get_events(from_dt=datetime.today(), to_dt=datetime.today() + timedelta(days=yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_FUTURE_EVENTS_DAYS)))
        merge_events:list[MergeEvent] = []
        skip_days = yaml_helper.get(YAML_SECTION_GENERAL, YAML_SETTING_SKIP_DAYS)

        for event in calendar_events:
            start_datetime = event.get(ICLOUD_FIELD_START_DATE)
            event_start_datetime = convert_to_utc(datetime(start_datetime[1], start_datetime[2], start_datetime[3], start_datetime[4], start_datetime[5]))
            if (str(event_start_datetime.weekday()) not in skip_days and (event_start_datetime >= today_bod and event_start_datetime <= cut_off_date)):
                merge_events.append(MergeEvent(event.get(ICLOUD_FIELD_TITLE), event.get(ICLOUD_FIELD_START_DATE), event.get(ICLOUD_FIELD_END_DATE), EventAction.none))

        source_index = 0
        end_of_calendars = False
        fs = FileSystem()

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
                # continue reading it’s config
                calendar_tag = yaml_helper.get(YAML_SECTION_SOURCE_CALENDAR.format(index=source_index), YAML_SETTING_CALENDAR_TAG)
                calendar_title = yaml_helper.get(YAML_SECTION_SOURCE_CALENDAR.format(index=source_index), YAML_SETTING_CALENDAR_TITLE)
                # read the url from the .env
                calendar_url = os.getenv(ENV_VAR_CALENDAR_URL.format(index=source_index))

                # download calendar
                timestamp_filename = fs.join_paths(fs.get_temp_dir(), f"{convert_to_utc(now).strftime('%Y%m%d%H%M%S%f')}.ics")
                fs.download(calendar_url, timestamp_filename)

                # prepare index for next iteration
                source_index += 1

                #read calendar
                with open(timestamp_filename, "rb") as ics_file:
                    ics_calendar = Calendar.from_ical(ics_file.read())
                
                source_calendar_events:list[MergeEvent] = []

                # filter events with config
                for event_item in ics_calendar.walk("VEVENT"):
                    start_datetime:datetime = event_item.get(ICS_FIELD_DATE_START).dt
                    if(isinstance(start_datetime, date) == True):
                        start_hour = 0
                        start_minute = 0
                    else:
                        start_hour = start_datetime.hour
                        start_minute = start_datetime.minute

                    start_datetime = convert_to_utc(datetime(start_datetime.year, start_datetime.month, start_datetime.day, start_hour, start_minute))

                    if ((str(start_datetime.weekday()) not in skip_days) and (start_datetime >= today_bod and start_datetime <= cut_off_date)):
                        source_calendar_events.append(MergeEvent(None, start_datetime, event_item.get(ICS_FIELD_DATE_END).dt, EventAction.none))

                # compare events from source calendar and icloud calendar
                print(f"processed: {calendar_title} - {calendar_tag} / {calendar_source}")

if __name__ == "__main__":
    main()