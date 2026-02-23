# AGENTS.md

Reference notes for future coding agents working in this repository.

## Project Snapshot

- Name: `calendar-merge`
- Type: Python CLI
- Goal: merge multiple ICS feeds into iCloud with day filtering, timezone normalization, and optional Telegram/Gemini notifications.
- Main implementation: `src/merge.py`
- Entrypoint command: `uv run calendar-merge`

## Environment + Setup

- Python: `>=3.12`
- Package manager: `uv`
- Install deps: `uv sync`
- Required local files:
  - `.env` (from `.env.template`)
  - `config.yaml` (from `config.yaml.template`)

## Common Commands

```bash
uv sync
uv run calendar-merge
uv run calendar-merge --first
uv run calendar-merge --last
```

## Code Layout

- Single-file app flow in `src/merge.py`:
  - load env + yaml config
  - authenticate to iCloud (2FA aware)
  - read current iCloud events
  - iterate `source-calendar-N` + `CALENDAR_URL_N`
  - diff and apply add/delete operations
  - optionally send Telegram/Gemini notifications

### Key Types

- `EventAction` (Enum): `none`, `add`, `delete` — drives the sync loop
- `MergeEvent` (dataclass): `title`, `start`, `end`, `full_event` (iCloud EventObject), `action`

### Event Title Format

Merged events use the exact format `[tag] title/source` (e.g., `[WRK] Team Calendar/Google`). The reconciliation logic matches on this string — changing the format breaks add/delete detection.

### External Dependencies

- `pyicloud` (pinned 2.1.0) — iCloud API and calendar operations
- `icalendar` — ICS file parsing
- `pyfangs` (custom git dep, pinned v0.7.2) — provides `YamlHelper`, `FileSystem`, `GeminiAI`, `TelegramNotifier`, `convert_to_utc`, and terminal color helpers. Not on PyPI.
- `google-generativeai` — Gemini AI for notification messages
- `dotenv` — environment variable loading

### Telegram Async Patterns

Telegram functions use backward-compatible async wrappers: context manager detection (`__aenter__`), fire-and-forget via event loop, and a polling-based `prompt_telegram_reply()` for collecting 2FA codes remotely. Preserve these patterns when modifying notification code.

## Config Contracts

- `.env`
  - `ICLOUD_USERNAME`, `ICLOUD_PASSWORD`
  - `CALENDAR_URL_0..N`
  - optional: `TELEGRAM_BOT_API_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`
- `config.yaml`
  - `config.skip_days` (`0=Mon` ... `6=Sun`)
  - `config.future_events_days`
  - `config.ai_tone`
  - `source-calendar-0..N` with `source`, `tag`, `title`, `tz`

Indexes must stay consecutive and aligned across `.env` and `config.yaml`.

## Editing Guidance

- No tests, linter, or CI exist — verify changes manually via `uv run calendar-merge`.
- Keep changes minimal and localized.
- Preserve CLI behavior and config key names.
- Avoid introducing new dependencies unless necessary.
- If you change sync logic, verify add/delete behavior and date filtering carefully.

## Validation Checklist

- Run basic command path when possible: `uv run calendar-merge`
- If notification paths changed, verify `--first` and/or `--last`
- Update `README.md` when user-facing behavior or configuration changes
