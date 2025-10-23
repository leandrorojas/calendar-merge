# Calendar Merge

Merge multiple ICS calendars (Google, Outlook, iCloud, etc.) into a single, unified iCloud calendar with consistent titles and timezones.

## Highlights
- Pulls events from any calendar that can expose an ICS feed.
- Normalizes timezones and filters out weekends (or any days you choose).
- Tags each imported event so you can trace the original source.
- Works with iCloud's built-in 2FA flow.

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

### `config.yaml`
Control which days are synced and how each calendar is labeled.

- `config.skip_days`: Comma-separated numbers where `0=Monday` and `6=Sunday`. Events that start on these days are ignored (e.g., `5, 6` skips Saturday and Sunday).
- `config.future_events_days`: Number of days ahead to pull events (e.g., `5` fetches the next five days).
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

During the first execution you may be prompted for iCloud two-factor authentication. Subsequent runs reuse the trusted session when possible.

### Scheduling
To keep your calendars in sync automatically, hook the command into your scheduler of choice (e.g., `cron`, launchd, Windows Task Scheduler). Make sure the job runs under a user session that has the required iCloud authentication.

## Notes
- Keep your machine timezone aligned with `America/Argentina/Buenos_Aires` if you rely on the current template assumptions.
- The script downloads temporary `.ics` files under the system temp directory while processing.
- If you remove a calendar URL or `source-calendar-N` block, clean up the numbering so the indexes stay consecutive starting from `0`.

Happy merging!
