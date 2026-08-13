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

**skip_days is per source, future_events_days is global.** `config.skip_days` is the default;
a `source-calendar-N` section may declare its own `skip_days` to override it, resolved by
`_resolve_source_skip_days()`. `future_events_days` is global and is counted with the *global*
skip_days, producing one date window shared by all sources — a per-source override selects
weekdays within that window, it does not extend it.

Because skip_days is per source, `_collect_icloud_events()` deliberately does **not** apply the
weekday filter; it would have to pick one global value for a shared list. The filter lives in
`_select_source_icloud_events()`, applied per source alongside the title match.

**Reading optional per-source settings:** `YamlHelper.get()` raises `YamlError` for an absent
setting, and `main()` treats a `YamlError` from a source as "no more source calendars" and breaks
the loop. So an optional setting must be read through a helper that catches `YamlError` locally
(see `_resolve_source_skip_days`), after a required setting has already proven the section exists.
Reading one naively silently drops every calendar after the first that omits it — no error, no log.

**Event exclusion is provider-dependent — do not unify it.** `TRANSP` means different things
depending on who published the feed, so `_is_excluded_event()` takes a `google_feed` flag that
`_is_google_feed()` derives once per calendar from `PRODID`.

| Feed | Rule |
|---|---|
| Google (`PRODID` contains `Google`) | **Any** explicit `TRANSP` → skip. Real meetings carry no `TRANSP` at all; an explicit value marks time the user blocked themselves (lunch, focus time, out of office). |
| Anything else | RFC reading: only `TRANSPARENT` → skip, plus `X-MICROSOFT-CDO-BUSYSTATUS: OOF`. |

Both mistakes have already shipped, so neither rule may be applied universally:

- "`TRANSP` present → skip" everywhere (≤ v0.1.5) imported **0 of 158** Outlook events, because
  Outlook stamps `TRANSP` on every event.
- "only `TRANSPARENT` → skip" everywhere (v0.1.6–v0.1.7) synced **1518** personal blocks from the
  Google work calendar — `lunch`, `no meeting time`, `Out of office - Pick Up Kids`.

Measured on the three live feeds: Google work 1630 events (not 3148), Google personal 4, Outlook
144 (not 0). Corroboration for the Google split: `ATTENDEE` appears on exactly the 1630 meetings
and `STATUS:CONFIRMED` on exactly the 1518 blocks, so two other properties agree with `TRANSP`.

Outlook `TENTATIVE` is **kept**, deliberately. It is the equivalent of a Google "maybe", and Google
feeds strip `PARTSTAT` entirely (0 occurrences), so a "maybe" there is indistinguishable from an
accepted meeting and already syncs. Keeping `TENTATIVE` makes both providers behave the same.

An unrecognised publisher gets the RFC reading, so a new provider errs towards syncing too much
rather than syncing nothing — the silent failure mode that hid the Outlook breakage for a release.

Two Outlook events (`SALIDA NIÑES`, `TERAPIA`) are personal blocks marked `BUSY`, indistinguishable
from meetings, and are knowingly synced. Excluding them would need title matching, which was
considered and rejected.

**Source feed quirks:** Outlook/Microsoft 365 feeds are plain ICS and need no dedicated code
path. They use Windows timezone identifiers (`Argentina Standard Time`), which `icalendar` maps
to IANA zones, so the parsed datetimes arrive timezone-aware.

**One event per slot per calendar.** `_parse_source_events` ends with
`_deduplicate_event_slots()`, so a calendar that lists two meetings at the same `(start, end)`
contributes a single event. This is lossless: parsed source events are
`MergeEvent(None, start, end, None, None)` with no title or raw event, and every synced event gets
the same source tag, so duplicates would render as identical blocks. Downstream only needs to know the
slot is busy.

Only exact matches collapse — overlapping and contained slots stay separate, because merging those
means choosing new bounds. Deduplication is per calendar, so two sources holding the same slot still
contribute one event each under their own tags. Note `_reconcile_events` still preserves
multiplicity if handed duplicates directly; the invariant comes from the parse step.

**Recurring events are not expanded.** `walk(ICS_TAG_VEVENT)` yields the series master, not its
occurrences, so a repeating meeting contributes at most its original `DTSTART`. Outlook anchors
series at their original start date, so long-running weekly meetings can contribute nothing to a
forward-looking window. Expanding them needs RRULE handling plus `EXDATE`/`RECURRENCE-ID`
overrides.

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
