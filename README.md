# Calendar Merge

Merge multiple ICS calendars (Google, Outlook, iCloud, etc.) into a single, unified iCloud calendar with consistent titles and timezones.

## Highlights

- Pulls events from any calendar that can expose an ICS feed.
- Normalizes timezones and filters out weekends (or any days you choose).
- Tags each imported event so you can trace the original source.
- Works with iCloud's built-in 2FA flow (trusted-device push via Telegram).
- Optional Telegram alerts for start-of-day and end-of-day notifications.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) package manager (or another tool able to install from `pyproject.toml`)
- iCloud account with calendar access and application-specific password or 2FA
- ICS URLs for every source calendar you want to merge

## Installation & Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/leandrorojas/calendar-merge.git
   cd calendar-merge
   # optional: work off the latest tag
   git checkout "$(git describe --tags "$(git rev-list --tags --max-count=1)")"
   ```

2. **Create your environment files**
   ```bash
   cp .env.template .env
   cp config.yaml.template config.yaml
   ```

3. **Install dependencies**
   ```bash
   uv sync
   ```

## Configuration

### `.env`

Add one entry per calendar feed.

- `ICLOUD_USERNAME` and `ICLOUD_PASSWORD`: iCloud credentials the script will use to connect.
- `ICLOUD_APP_PASSWORD`: optional, and setting it **changes which backend is used**. With it the merge talks to iCloud over CalDAV; without it it uses pyicloud's web API and its 2FA flow. Generate one at [appleid.apple.com](https://appleid.apple.com) under Sign-In and Security. See [Choosing a backend](#choosing-a-backend).
- `CALENDAR_URL_N`: ICS feed URLs where `N` starts at `0` and increments (`CALENDAR_URL_0`, `CALENDAR_URL_1`, ...). Each URL must have a matching `source-calendar-N` section in `config.yaml`.
- `TELEGRAM_BOT_API_TOKEN`: Bot token used for notifications and 2FA code entry (optional, required if you want Telegram alerts or 2FA handling).
- `TELEGRAM_CHAT_ID`: Destination chat/channel id. Required whenever `TELEGRAM_BOT_API_TOKEN` is set. To obtain it, send any message to your bot, then call `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and look for `"chat":{"id": ...}` in the response.

### `config.yaml`

Control which days are synced and how each calendar is labeled.

- `config.skip_days`: Comma-separated numbers where `0=Monday` and `6=Sunday`. Events that start on these days are ignored (e.g., `5, 6` skips Saturday and Sunday). This is the **default** for every source; individual calendars can override it — see `source-calendar-N.skip_days` below.
- `config.destination_calendar`: Display name of the iCloud calendar the merge writes into, matched exactly. Omit it to keep the older behaviour of writing to whichever calendar the provider returns first — fine with a single calendar, arbitrary with several, and not necessarily the same one under each backend.
- `config.future_events_days`: Number of non-skipped days ahead to include. `_calculate_future_date()` walks forward from the current date, counting only days not in `skip_days`, so the actual calendar span depends on which day the script runs. For example, with `skip_days: 5, 6` and `future_events_days: 5`: starting on a Monday the window is 5 calendar days (Mon→Fri), but starting on a Wednesday it spans 7 calendar days (Wed→next Tue, skipping Sat and Sun).
- `source-calendar-N`: Duplicate this block per calendar and keep `N` in sync with the `.env` file.
  - `source`: Short name for the upstream calendar (e.g., `Google`, `Outlook`).
  - `tag`: Label enclosed in brackets in the generated iCloud event title.
  - `title`: Your friendly name for the merged calendar events.
  - `tz`: Timezone identifier (e.g., `America/New_York`). Events are converted to this timezone when added.
  - `skip_days`: *Optional.* Overrides `config.skip_days` for this calendar only, using the same format. Use an empty value (`skip_days: ""`) to sync every day. Omit the key entirely to inherit the global setting.

Any of these `skip_days` forms work, in either the global or the per-source position:

| Value | Means |
|---|---|
| `skip_days: 5, 6` | Saturday and Sunday |
| `skip_days: 0,6` | Monday and Sunday (spaces optional) |
| `skip_days: [5, 6]` | same as `5, 6` |
| `skip_days: 6` | Sunday only — a single day needs no quotes or brackets |
| `skip_days: 0` | Monday only |
| `skip_days: ""` | skip nothing (sync every day) |
| `skip_days: []` | skip nothing |
| `skip_days:` | skip nothing |

Note that `future_events_days` is **global only** — it cannot be set per source. It is counted using the global `config.skip_days`, and the resulting date window is shared by every calendar. A per-source `skip_days` therefore controls *which weekdays inside that window* are synced, not how far ahead the window reaches.

> [!WARNING]
> Widening a calendar's `skip_days` orphans events already synced on the newly-skipped days. They stop being reconciled, so they are neither updated nor deleted and remain in iCloud. Narrowing it is safe. Delete those events by hand if you add a skip day later.

Example:
```yaml
config:
  skip_days: 5, 6
  future_events_days: 7

source-calendar-0:
  source: "Work"
  tag: "WRK"
  title: "Team Calendar"
  tz: "America/New_York"

source-calendar-1:
  source: "Personal"
  tag: "PRS"
  title: "Family Calendar"
  tz: "America/Argentina/Buenos_Aires"
  skip_days: ""          # personal calendar syncs every day
```

### Adding an Outlook / Microsoft 365 calendar

Outlook feeds need no special handling — they are plain ICS, so they use the same
`source-calendar-N` + `CALENDAR_URL_N` pair as any other source.

To get the URL: in Outlook on the web, open **Settings → Calendar → Shared calendars**,
publish the calendar you want, and copy the **ICS** link (not the HTML one). It looks like
`https://outlook.office365.com/owa/calendar/<id>@<domain>/<token>/calendar.ics`.

> [!WARNING]
> That link is an unauthenticated secret — anyone who has it can read the calendar.
> Keep it in `.env` (which is gitignored) and never commit it.

```yaml
source-calendar-2:
  source: "Outlook"
  tag: "OUT"
  title: "Work Calendar"
  tz: "America/Argentina/Buenos_Aires"
```

```bash
CALENDAR_URL_2="https://outlook.office365.com/owa/calendar/.../calendar.ics"
```

### Duplicate time slots

If a calendar lists two meetings at exactly the same start and end time, only one event is synced —
the merged calendar shows a single block for that slot. Every synced event carries the same tag, so
duplicates would have been indistinguishable anyway.

Only exact matches are collapsed. Overlapping meetings (say 13:00–14:00 and 13:30–14:30) remain two
events. Two *different* calendars holding the same slot also stay separate, since they carry
different tags.

### Which events are skipped

Not every event in a feed is synced. What counts as "not a real meeting" depends on who published
the calendar, because `TRANSP` is used inconsistently:

| Feed | Skipped |
|---|---|
| **Google** | Any event with an explicit `TRANSP`. Google omits it on real meetings and writes it only for time you blocked yourself — lunch, focus time, out of office. |
| **Outlook / other** | Only `TRANSPARENT` (free) events, plus anything marked out of office (`X-MICROSOFT-CDO-BUSYSTATUS: OOF`). |

The publisher is detected from the feed's `PRODID`; an unrecognised one gets the standard reading,
which errs towards syncing too much rather than nothing.

Outlook's **tentative** events *are* synced. They are the equivalent of a Google "maybe", and Google
feeds do not expose RSVP status at all, so those already sync — this keeps both providers consistent.

A caveat: a personal block created as an ordinary busy event in Outlook (say a recurring "therapy"
slot) is indistinguishable from a meeting and will sync. Mark it *free* or *out of office* in Outlook
if you want it excluded.

Two things worth knowing about Outlook feeds:

- **Timezones.** Outlook uses Windows timezone names (`Argentina Standard Time`) rather than
  IANA ones. These are mapped automatically, so no extra configuration is needed. The `tz`
  value above is only used when writing events *into* iCloud.
- **Recurring events are expanded.** A repeating meeting contributes every occurrence that falls
  in the window, honouring `EXDATE` cancellations, `RECURRENCE-ID` overrides for moved instances,
  and `RDATE` extras. This matters most for Outlook, which anchors a series at its original start
  date — before expansion, a long-running weekly meeting contributed nothing at all. Google feeds
  are unaffected either way, since Google pre-expands each occurrence server-side.

## Choosing a backend

The merge can reach iCloud two ways, selected by whether `ICLOUD_APP_PASSWORD` is set.

| | pyicloud (default) | CalDAV |
|---|---|---|
| Selected by | no `ICLOUD_APP_PASSWORD` | `ICLOUD_APP_PASSWORD` set |
| Credential | account password | app-specific password |
| 2FA | required, handled over Telegram | none — the app-specific password *is* the second factor |

**Use CalDAV if sign-in fails with the account password.** Apple restricts password
sign-in on some accounts, and when it does, pyicloud cannot authenticate at all: its web
API accepts nothing else, and no configuration works around it. An app-specific password
does authenticate, which is what the CalDAV backend exists for.

Both paths remain available, and switching back is removing one line from `.env`. The
2FA machinery is untouched and still runs on the pyicloud path.

Two behaviours differ under CalDAV and are worth knowing:

- Collections are addressed by URL rather than Apple GUID. This is internal, but it is
  what `destination_calendar` resolves to.
- Apple serves reminder lists from the same endpoint as calendars, so collections that
  cannot hold events are filtered out. Without that a reminders list sharing a
  calendar's name could be selected as the destination.

## Usage

Run the merger after updating your `.env` and `config.yaml`:

```bash
uv run calendar-merge
```

### Two ways to start it

Both work, and they differ in ways worth knowing before choosing one for a scheduler.

```bash
uv run calendar-merge          # the installed console script
.venv/bin/python src/merge.py  # the module, executed directly
```

The **console script** is the packaged entry point. It is a generated shim whose body is
`from merge import main`, so it depends on the wheel placing `merge.py` at its root — which is
why CI checks that layout. `uv run` also syncs the environment before starting, so a run picks
up dependency changes on its own; add `--no-sync` to suppress that.

Running the **module directly** skips all of it. Python puts the script's own directory on
`sys.path`, so no install, entry point, or packaging is involved. Nothing is resolved and
nothing is downloaded, which makes it the more predictable choice for cron: a scheduled run
cannot pause to install anything, and cannot be affected by a half-applied dependency change.

Use whichever suits you — the console script for ad-hoc runs, the direct form when a scheduled
run should do exactly the same thing every time. If you schedule the console script instead,
prefer `uv run --no-sync calendar-merge` so the schedule stays deterministic.

Add the optional flags when you want Telegram updates:

```bash
# Morning sync + Telegram start-of-day message

uv run calendar-merge --first

# Evening sync + Telegram end-of-day message

uv run calendar-merge --last
```

During the first execution you will be prompted for iCloud two-factor authentication: a 6-digit code is pushed to your trusted Apple devices, and the script sends a Telegram message asking you to reply with the code. Subsequent runs reuse the trusted session when possible.

A few details of that exchange:

- **Only a 6-digit reply is accepted.** Anything else is ignored and the script keeps waiting, so
  ordinary conversation in the chat won't be mistaken for your code.
- **You get three attempts.** A rejected code is re-prompted rather than failing the whole run.
  Apple's code is only pushed once, on the first attempt — the retries reuse the same code, so
  don't wait for a new one.
- **You get a confirmation.** When the code is accepted you receive a ✅ reply on Telegram, so a
  working code is never mistaken for one that got lost.
- **A rejected code still banks the session trust.** If the code fails but Apple trusts the session
  anyway, the run reports failure but tells you the next run shouldn't prompt — so you don't have to
  guess whether anything was achieved.
- **You have 5 minutes.** After that the run gives up, and it won't retry because nobody is
  answering. The next scheduled run will ask again.

### Scheduling

To keep your calendars in sync automatically, hook the command into your scheduler of choice (e.g., `cron`, launchd, Windows Task Scheduler). Make sure the job runs under a user session that has the required iCloud authentication.

#### Telegram notifications

- Add `--first` to send a "day is starting" Telegram notification.
- Add `--last` to send an "end of day" Telegram notification.
- Both flags can be combined when you run the script twice per day (morning/evening).
- To validate your Telegram setup, send a test message to the bot and confirm the script can deliver notifications to the configured `TELEGRAM_CHAT_ID`.

## Scheduling

`--first` and `--last` perform a full sync in addition to sending their Telegram message, so
they replace a plain run rather than supplementing it.

**Never schedule two invocations in the same minute.** Each run loads a snapshot of the iCloud
calendar and then acts on it, so two concurrent processes work from the same stale picture:
both may try to delete the same event — one wins, the other gets a `404` — and both may decide
the same slot is missing and add it, leaving a duplicate until a later run reconciles it away.

The trap is an hourly job overlapping a recurring one. `0 8 * * 1-5` and `*/15 8-17 * * 1-5`
look independent but collide at 08:00. Give the flagged run its own slot:

```cron
0  8       * * 1-5   calendar-merge --first
15,30,45 8 * * 1-5   calendar-merge
*/15 9-17  * * 1-5   calendar-merge
0  18      * * 1-5   calendar-merge --last
```

Same coverage, no shared minute. A 404 from a lost race no longer aborts the run as of
v0.1.12, but duplicate creation is not similarly guarded — the schedule is the real fix.

## Notes

- Keep your machine timezone aligned with `America/Argentina/Buenos_Aires` if you rely on the current template assumptions.
- **Repeated failure alerts are suppressed.** A first failure notifies, the same failure
  repeating does not, and recovery is announced. `failure_alert_every` in `config.yaml` controls
  how many further failed runs pass before the alert repeats — with a 15-minute schedule the
  default of `4` reminds hourly, and `0` alerts only on the first failure and on recovery.
- The script downloads temporary `.ics` files under the system temp directory while processing.
- If you remove a calendar URL or `source-calendar-N` block, clean up the numbering so the indexes stay consecutive starting from `0`.
- Known gaps that are deliberately unfixed are recorded in [BACKLOG.md](BACKLOG.md), each with
  its evidence and what a fix would take.

Happy merging!
