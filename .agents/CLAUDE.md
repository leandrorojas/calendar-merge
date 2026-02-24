# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

calendar-merge is a Python CLI tool that synchronizes multiple ICS calendar feeds (Google, Outlook, etc.) into a single iCloud calendar. It supports configurable day filtering, timezone conversion, iCloud 2FA, optional Telegram notifications, and AI-generated messages via Google Gemini.

## Commands

```bash
# Install dependencies
uv sync

# Run the merger (basic sync)
uv run calendar-merge

# Run with morning Telegram notification
uv run calendar-merge --first

# Run with evening Telegram notification
uv run calendar-merge --last

# Run with both
uv run calendar-merge --first --last

# Arm a processing override for the next skip day
uv run calendar-merge --override

# Cancel an active or pending override
uv run calendar-merge --cancel
```

There are no tests, linter, or CI/CD pipeline configured.

## Architecture

The entire application lives in a single file: `src/merge.py` (entry point: `main()`).

### Workflow (in `main()`)

1. **Config load** — reads `.env` (secrets) and `config.yaml` (settings) via `pyfangs.YamlHelper`
2. **Override/cancel handling** — runs every invocation before iCloud auth:
   - Loads `state.json`; polls Telegram once for pending commands (`override` / `cancel`)
   - **Step 0** `_handle_cancel`: if cancel received and eligible, clears override state and exits early (no sync)
   - **Step 1** `_handle_override_date_lifecycle`: logs active (1A), pending (1B), or clears stale (1C) `override_date`
   - **Step 2** `_ingest_override_flag` + `_compute_and_persist_override_date`: arms `override_date` from CLI/Telegram signal
   - Saves `state.json` if anything changed
3. **Processing decision** (`_should_process`) — skips iCloud entirely on skip days unless `override_date == today`
4. **iCloud auth** — authenticates via `pyicloud`, handles 2FA/2SA (2FA: FIDO2 keys or Telegram-polled codes; 2SA: terminal-entered verification code)
5. **iCloud calendar load** — fetches existing iCloud events within the configured date range, filters out all-day events and skip-days
6. **Source calendar loop** — iterates `source-calendar-0`, `source-calendar-1`, ... until no more sections exist:
   - Downloads ICS from `CALENDAR_URL_N` env var to a temp file
   - Parses with `icalendar`, filters by date range and skip-days; current implementation skips VEVENT entries where `TRANSP` is present
   - Reconciles: marks events for add (in source but not iCloud) or delete (in iCloud but not source)
   - Syncs changes to iCloud
7. **Override consume** (`_consume_override_on_last`) — on `--last`, clears `override_date` if today matches
8. **Telegram notifications** — sends AI-generated or static morning/evening messages if `--first`/`--last` flags are set

### Operational Caveats

- Target calendar selection currently uses the first iCloud calendar GUID returned by the API.

### Key Types

- `EventAction` (Enum): `none`, `add`, `delete`
- `MergeEvent` (dataclass): `title`, `start`, `end`, `full_event` (iCloud EventObject), `action`

### Configuration

- `.env` — secrets: `ICLOUD_USERNAME`, `ICLOUD_PASSWORD`, `CALENDAR_URL_N`, `TELEGRAM_BOT_API_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`
- `config.yaml` — settings: `config.skip_days` (weekday numbers, 0=Mon), `config.future_events_days` (counts non-skipped days), `config.ai_tone`, plus `source-calendar-N` blocks with `source`, `tag`, `title`, `tz`
- `state.json` — auto-created at project root; do not commit. Stores `override_flag` (bool), `override_date` (YYYY-MM-DD or null), `telegram_offset` (int or null)
- Source calendar indexes must be consecutive starting from 0 and must match between `.env` (`CALENDAR_URL_N`) and `config.yaml` (`source-calendar-N`)

### External Dependencies

- `pyicloud` (pinned 2.1.0) — iCloud API access and calendar operations
- `icalendar` — ICS file parsing
- `pyfangs` (git dep, pinned v0.7.2) — custom utility library providing `YamlHelper`, `FileSystem`, `GeminiAI`, `TelegramNotifier`, terminal colors, and `convert_to_utc`
- `google-generativeai` — Gemini AI for generating notification messages
- `dotenv` — environment variable loading

### Telegram Integration

Uses async/await with backward-compatible patterns (context manager detection, fire-and-forget via event loop). The `prompt_telegram_reply()` function polls for user replies and is used to collect 2FA codes remotely.

### Event Title Format

Merged events in iCloud use the format: `[tag] title/source` (e.g., `[WRK] Team Calendar/Google`).

## Validation

- Run basic sync path: `uv run calendar-merge`
- Run notification paths when needed: `uv run calendar-merge --first` and/or `uv run calendar-merge --last`
- Run override/cancel paths when needed: `uv run calendar-merge --override` / `--cancel`
- There are currently no automated tests configured in this repository.
