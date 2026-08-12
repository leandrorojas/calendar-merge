# Changelog

All notable changes to this project will be documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
tagged in git.

## [Unreleased]

### Added
- Full test suite: 199 tests at 100% statement and branch coverage of
  `src/merge.py`, up from 41 tests at 29%. New modules cover the 2FA branches
  (`test_2fa.py`), Telegram send/poll (`test_telegram.py`), event
  collection/ICS parsing/iCloud sync (`test_events.py`), the top-level flow and
  `main()` (`test_flow.py`), logging (`test_logging.py`), and the
  `__main__` entry point (`test_entrypoint.py`).
- `tests/conftest.py` with fakes for iCloud, Telegram, YAML, and the filesystem,
  plus autouse fixtures that isolate env vars, terminal output, and the logger.
- `pytest-cov` dev dependency and coverage config with `fail_under = 100`;
  CI runs `pytest --cov` so a coverage drop fails the build.

### Changed
- Test coverage gate wired into `.github/workflows/ci.yml`

## [v0.1.5] — 2026-08-12

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
- CHANGELOG, SECURITY policy, and PR/issue templates

### Changed
- Extracted `_reconcile_events()` from `main()` for testability
- Reduced cognitive complexity in `merge.py` per SonarQube
- Corrected `version` in `pyproject.toml`, which had been left at `0.1.1`
  through the v0.1.2–v0.1.4 releases

### Security
- Pinned all GitHub Actions to full commit SHAs; version tags are mutable
- Restricted `GITHUB_TOKEN` to `contents: read`
- Pinned `ruff==0.15.10` in CI to match the pre-commit hook, and installed
  tools with `--no-build` so no sdist setup script executes
- Added `--locked`/`--frozen` to uv commands so CI cannot drift from `uv.lock`
- Revoked the SSH agent after fetching `pyfangs`, so PR-authored code cannot
  reach the deploy key via `SSH_AUTH_SOCK`

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
