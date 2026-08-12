# Changelog

All notable changes to this project will be documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
tagged in git.

## [Unreleased]

### Added
- Ruff linter and formatter with pre-commit hooks
- Pytest unit tests covering pure logic (41 tests)
- Mypy type checking with lenient baseline
- GitHub Actions CI (lint + test + typecheck)
- Dependabot for weekly dependency and GitHub Actions updates
- `.editorconfig` for cross-editor consistency
- Telegram reply poll timeout (default 5 min) to prevent indefinite hang
- Structured file logging via `logging.handlers.RotatingFileHandler`
  (default: `logs/calendar-merge.log`, 10MB × 5 files). Configurable via
  `CALENDAR_MERGE_LOG_FILE` and `CALENDAR_MERGE_LOG_LEVEL` env vars.

### Changed
- Extracted `_reconcile_events()` from `main()` for testability

## [v0.1.4] — 2026-04-14

### Fixed
- iCloud 2FA trusted-device push stopped working after Apple's server-side
  changes in late March 2026.

### Changed
- Upgraded pyicloud 2.1.0 → 2.5.0 (HSA2 bridge flow via PR #210).
- Removed `google-generativeai` and the Gemini AI-generated Telegram
  messages. `--first`/`--last` now send plain Telegram notifications.
- Upgraded pyfangs v0.7.2 → v0.7.3 (AI/DB dependencies became optional extras).
- Disabled pyicloud's SMS fallback in `validate_2fa()` — the bridge would time
  out on its WebSocket return payload (while still pushing the code to the
  device), causing the delivery method to silently switch to SMS and reject
  the trusted-device code at validation.

## [v0.1.3] and earlier

See git history for details. Tags v0.2.0 – v0.2.2 were marked as pre-release
on GitHub because that work (override/cancel, dry-run, state management) was
reverted in PR #42.
