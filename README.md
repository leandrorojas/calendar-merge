# Calendar Merge

Merge multiple ICS calendars (Google, Outlook, iCloud, etc.) into a single, unified iCloud calendar with consistent titles and timezones.

## Highlights
- Pulls events from any calendar that can expose an ICS feed.
- Normalizes timezones and filters out weekends (or any days you choose).
- Tags each imported event so you can trace the original source.
- Works with iCloud's built-in 2FA flow.
- Optional Telegram alerts (including morning/night summaries via Gemini AI) keep you informed even when you're away from the terminal.
- Override/cancel control plane: arm processing on a skip day on demand via Telegram or CLI, and cancel it before it runs.

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
- `CALENDAR_URL_N`: ICS feed URLs where `N` starts at `0` and increments (`CALENDAR_URL_0`, `CALENDAR_URL_1`, ...). Each URL must have a matching `source-calendar-N` section in `config.yaml`.
- `TELEGRAM_BOT_API_TOKEN`: Bot token used for notifications (optional, required if you want Telegram alerts).
- `TELEGRAM_CHAT_ID`: Destination chat/channel id (optional). If it’s missing, run `python scripts/telegram_sandbox.py` to infer it automatically.
- `GEMINI_API_KEY`: API key for the Gemini AI helper. Required only when using the Telegram morning/night summaries described below.

### `config.yaml`
Control which days are synced and how each calendar is labeled.

- `config.skip_days`: Comma-separated numbers where `0=Monday` and `6=Sunday`. Events that start on these days are ignored (e.g., `5, 6` skips Saturday and Sunday).
- `config.future_events_days`: Number of days ahead to pull events (e.g., `5` fetches the next five days).
- `config.ai_tone`: Optional text describing how Gemini AI should shape its responses (e.g., `"friendly"`, `"concise/professional"`). Use an empty string to leave Gemini’s default tone.
- `source-calendar-N`: Duplicate this block per calendar and keep `N` in sync with the `.env` file.
  - `source`: Short name for the upstream calendar (e.g., `Google`, `Outlook`).
  - `tag`: Label enclosed in brackets in the generated iCloud event title.
  - `title`: Your friendly name for the merged calendar events.
  - `tz`: Timezone identifier (e.g., `America/New_York`). Events are converted to this timezone when added.

Example:
```yaml
config:
  skip_days: 5, 6
  future_events_days: 7
  ai_tone: "friendly and concise"

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
```

## Usage

Run the merger after updating your `.env` and `config.yaml`:

```bash
uv run calendar-merge
```

Add the optional flags when you want Telegram updates:

```bash
# Morning sync + AI/Telegram “good morning”
uv run calendar-merge --first

# Evening sync + AI/Telegram wrap-up
uv run calendar-merge --last

# Arm a processing override for the next skip day
uv run calendar-merge --override

# Cancel an active or pending override
uv run calendar-merge --cancel
```

During the first execution you may be prompted for iCloud two-factor authentication. Subsequent runs reuse the trusted session when possible.

### Scheduling
To keep your calendars in sync automatically, hook the command into your scheduler of choice (e.g., `cron`, launchd, Windows Task Scheduler). Make sure the job runs under a user session that has the required iCloud authentication.

#### Telegram / AI summaries
- Add `--first` to send a “day is starting” Telegram notification (optionally AI-generated via Gemini).
- Add `--last` to send an “end of day” notification.
- Both flags can be combined when you run the script twice per day (morning/evening). The notifications gracefully fall back to static messages if Gemini or Telegram are not configured.
- To validate your Telegram configuration and automatically capture the chat id, run:
  ```bash
  uv run python scripts/telegram_sandbox.py
  ```

### Override / cancel control plane

By default the script skips processing on any day listed in `config.skip_days`. The override/cancel control plane lets you change this on demand without touching the config.

| Concept | Description |
|---|---|
| **skip day** | A day in `config.skip_days` where processing is normally disabled. |
| **override** | Arms processing for one skip day (the whole day, all cron runs). |
| **override_date** | The date (`YYYY-MM-DD`) stored in `state.json` while an override is active. |
| **cancel** | Disarms an active or pending override before it is consumed. |

#### Arming an override

Send `override` (exact, case-insensitive) to your Telegram bot **or** pass `--override` on the CLI. The next cron run picks it up and arms the override:

- If it is the **first run of the day** (`--first`) **and today is a skip day** → override is armed for **today**.
- Otherwise → override is armed for the **next upcoming skip day**.

The override stays active for the entire day and is automatically cleared after the last run (`--last`).

#### Canceling an override

Send `cancel` to your Telegram bot **or** pass `--cancel` on the CLI. Cancel is eligible when an `override_date` exists and is today or in the future:

- **override_date == today** → override is disarmed and the current run exits without processing.
- **override_date > today** → override is disarmed and the run continues on its normal (skip-day) schedule.

If no override is armed, cancel is a safe no-op.

#### State file

Override state is persisted in `state.json` at the project root (auto-created, not committed to version control). It stores `override_flag`, `override_date`, and `telegram_offset` (bookmark into the Telegram update stream for idempotent command detection).

## Notes
- Keep your machine timezone aligned with `America/Argentina/Buenos_Aires` if you rely on the current template assumptions.
- The script downloads temporary `.ics` files under the system temp directory while processing.
- If you remove a calendar URL or `source-calendar-N` block, clean up the numbering so the indexes stay consecutive starting from `0`.

Happy merging!
