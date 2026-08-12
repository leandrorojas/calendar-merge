# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

```bash
uv sync                          # Install dependencies
uv sync --extra dev              # Install dependencies + dev tooling (mypy, pytest)
uv run calendar-merge            # Run calendar merge
uv run calendar-merge --first    # Morning sync + Telegram start-of-day notification
uv run calendar-merge --last     # Evening sync + Telegram end-of-day notification
```

## Quality Checks

```bash
uv run ruff check src/ tests/            # Lint
uv run ruff format --check src/ tests/   # Format check
uv run mypy src/ tests/                  # Type check
uv run pytest tests/ -v                  # Unit tests
uv run pytest --cov                      # Unit tests + coverage gate
```

CI (`.github/workflows/ci.yml`) runs these on push to `main` and on every pull request, split into
two jobs: `lint` (Ruff only, no private deps needed) and `test-and-typecheck` (needs the
`PYFANGS_DEPLOY_KEY` secret to install the private `pyfangs` dependency over SSH).

## Tests

`src/merge.py` is at **100% statement and branch coverage**, enforced by `fail_under = 100` in
`[tool.coverage.report]`. CI runs `pytest --cov`, so a coverage drop fails the build — new code
needs new tests.

Test modules by area: `test_2fa.py` (FIDO2 / trusted-device / 2SA branches), `test_telegram.py`
(send + poll, driven with `asyncio.run` rather than pytest-asyncio), `test_events.py` (iCloud
collection, ICS parsing, sync), `test_flow.py` (`_load_config`, `_authenticate_icloud`,
`_load_icloud_events`, `_process_source_calendar`, `main`), `test_logging.py`, and
`test_entrypoint.py` (the `__main__` block, run via `runpy`).

`tests/conftest.py` holds the fakes (`FakeNotifier`, `FakeCalendarService`, `FakeYamlHelper`,
`FakeFileSystem`, `fake_api`) and three autouse fixtures that keep runs hermetic: `clean_env`
unsets every env var the module reads, `quiet_terminal` captures `term.print` output for
assertions, and `reset_logger` detaches handlers so `_configure_logging`'s idempotence guard
can't leak between tests.

Two conventions worth keeping:
- Tests import via `from tests.conftest import ...` because `tests/` is a package.
- `test_entrypoint.py` patches `pyfangs.yaml.YamlHelper` to force config load to fail, so the run
  never reaches iCloud even on a machine that has a real `config.yaml` and `.env`.

## Architecture

Single-file Python application (`src/merge.py`) that merges multiple ICS calendar feeds into one iCloud calendar.

**Main flow (sequential regions in `main()`):**

1. **CONFIG_LOAD** — reads `.env` + `config.yaml` (skip_days, future_events_days)
2. **ICLOUD_AUTH** — authenticates via PyiCloudService; handles 2FA (FIDO2 security key, or trusted-device code via Telegram poll)
3. **ICLOUD_CALENDAR_LOAD** — fetches iCloud calendar GUID, computes date range (today → future non-skipped days), loads existing iCloud events
4. **Source calendar loop** — for each `source-calendar-N`: downloads ICS, parses VEVENTs, filters by date range and skip_days, reconciles against iCloud events by `(start, end)` tuple, then applies add/delete actions via iCloud API

**Key types:** `EventAction` enum (`none`/`add`/`delete`), `MergeEvent` dataclass (title, start, end, full_event, action).

**Date filtering:** `_calculate_future_date()` counts N non-skipped weekdays forward — so `future_events_days: 5` with weekends skipped may span 7+ calendar days.

**2FA flow (pyicloud 2.5.0):** `api.request_2fa_code()` triggers the trusted-device push. The SMS fallback is explicitly disabled via `api._can_request_sms_2fa_code = lambda: False` because pyicloud's trusted-device bridge can time out waiting for the WebSocket return payload (while still successfully pushing the code to the device), which would otherwise switch the delivery method to `"sms"` and reject the trusted-device code at validation.

## Dependencies

- `pyfangs` (v0.7.3) — private library (`ssh://git@github.com/leandrorojas/pyfangs`): provides YamlHelper, FileSystem, terminal colors, Telegram (TelegramNotifier), and UTC conversion. The AI (GeminiAI) and DB (Postgres) modules are available as optional extras but not used here.
- `pyicloud` (2.5.0) — iCloud API (calendar service, HSA2 2FA via trusted-device bridge)
- `icalendar` — ICS file parsing
- `click` — used for interactive 2FA prompts (note: not declared in pyproject.toml, comes via pyicloud)

## Configuration

- `config.yaml` — `skip_days` are weekday integers as strings ("0"=Mon … "6"=Sun); `source-calendar-N` sections must have consecutive indexes matching `CALENDAR_URL_N` env vars
- `.env` — credentials and calendar URLs; templates at `config.yaml.template` and `.env.template`

## After Implementation

- Update `README.md` if the change affects usage, configuration, or setup instructions
- Update `CLAUDE.md` if the change affects architecture, dependencies, or conventions

## Conventions

- All datetime handling converts to UTC internally via `pyfangs.time.convert_to_utc`
- Telegram messaging is async with sync wrappers; supports both context-manager and plain TelegramNotifier instantiation
- No `Co-Authored-By` lines in commits
