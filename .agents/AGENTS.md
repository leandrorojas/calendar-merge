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
uv run calendar-merge --override   # arm processing override for next skip day
uv run calendar-merge --cancel     # cancel active or pending override
```

## Code Layout

- Single-file app flow in `src/merge.py`:
  - load env + yaml config
  - poll Telegram once for pending commands (override / cancel)
  - Step 0: cancel handling (may exit early)
  - Step 1: override_date lifecycle (active / pending / stale)
  - Step 2: override flag ingestion and override_date computation
  - authenticate to iCloud (2FA aware) — only when processing is enabled
  - read current iCloud events
  - iterate `source-calendar-N` + `CALENDAR_URL_N`
  - diff and apply add/delete operations
  - consume override_date on `--last`
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

### Override / Cancel Control Plane

Key functions in order of execution inside `main()`:

| Function | PRD Step | Role |
|---|---|---|
| `_poll_telegram_commands_async(state)` | — | Polls Telegram once per run; returns `set[str]` of commands found; advances `telegram_offset` |
| `_handle_cancel(args, telegram_commands, state, today)` | Step 0 | Checks cancel signal; clears override state if eligible; returns `True` to stop execution when canceling today |
| `_handle_override_date_lifecycle(state, today)` | Step 1 | Logs 1A (active), 1B (pending), or 1C (stale → clears with Telegram warning) |
| `_ingest_override_flag(args, telegram_commands, state)` | Step 2a | Sets persistent `override_flag` in state when signal received; idempotent |
| `_compute_and_persist_override_date(args, state, skip_days, today)` | Step 2b | Consumes `override_flag` → computes and stores `override_date` (today if `--first` on skip day, else next skip day) |
| `_should_process(state, today, skip_days)` | Step 3 | Returns `True` if today == override_date or today is not a skip day |
| `_consume_override_on_last(state, today, state_path)` | Step 1A consume | Clears `override_date` on `--last` when today matches |

**Telegram polling design:** `_poll_telegram_commands_async` is called once per run and its result (`set[str]`) is shared with both `_handle_cancel` and `_ingest_override_flag`. This ensures cancel and override commands in the same Telegram update batch are never lost to competing offset advances.

**State file (`state.json`):** auto-created at project root; not committed. Fields:
- `override_flag` (bool) — pending override signal awaiting computation
- `override_date` (str, `YYYY-MM-DD`) — active or future override date
- `telegram_offset` (int) — last consumed Telegram update ID; prevents reprocessing old messages

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
- `state.json` (auto-created, do not commit)
  - `override_flag`, `override_date`, `telegram_offset`

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
