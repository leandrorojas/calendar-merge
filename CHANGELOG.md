# Changelog

All notable changes to this project will be documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
tagged in git.

## [Unreleased]

### Fixed

- Session trust is now requested even when 2FA code validation fails. Apple can
  refuse a code while still granting trust, and the v0.1.5 refactor's early return
  stopped that request from happening — so a run like the 2026-07-30 incident no
  longer established the trust that had made the following run succeed without a
  prompt. The run still reports failure, but a Telegram message explains that the
  session is trusted and the next run should not prompt.
- The 2FA prompt no longer retries when Apple's code request itself failed. A
  trusted-device bridge that does not bootstrap leaves nothing to validate
  against, so the retry introduced in v0.1.7 asked the user for three doomed
  codes instead of one.

### Added

- Telegram confirmation when an Apple 2FA code is accepted. The code is submitted
  over Telegram but success was only reported to the terminal and the log file, so
  an accepted code was indistinguishable from one that never arrived until a
  failure message or the evening notification appeared. Sent only on the
  trusted-device path, where the user is actually waiting on Telegram.

### Changed

- The 2FA code prompt is retried up to three times instead of aborting the merge
  on the first rejected code. Apple's push is requested only on the first
  attempt, since re-requesting invalidates the code the user is holding, and a
  timeout still returns immediately rather than burning attempts.

### Fixed

- Telegram replies are now validated as six-digit codes before being submitted to
  Apple. Previously the first text message after the prompt was used, so `ok` — or
  anyone else speaking in the chat — was sent as the code and failed the run.
  Non-matching replies are ignored while polling continues.
- An expired code no longer escapes the retry loop. pyicloud returns `False` for a
  wrong code but can raise for an expired one, which was relabelled as the generic
  `2FA validation error`; it is now treated as a rejection.
- `prompt_telegram_reply` swallows transport errors like `send_telegram_message`
  already did. A flood-control response during 2FA was reported as a 2FA failure
  rather than a Telegram problem.

### Fixed

- A calendar listing two meetings in the same slot no longer syncs two events.
  `_parse_source_events` now collapses source events sharing an exact
  `(start, end)`, which is lossless because parsed events carry no title or raw
  event and every synced event gets the same source tag. Duplicates created
  before this change clean themselves up on the next run. Only exact matches
  collapse: overlapping and contained slots stay separate, and deduplication is
  per calendar. Measured on live feeds: 1630 → 1628 and 144 → 143 events.

## [v0.1.7] — 2026-08-13

### Added

- `skip_days` can now be set per source calendar. A `source-calendar-N` section
  may declare its own `skip_days`, overriding `config.skip_days`; omitting the
  key inherits the global value, and an empty value syncs every day.
  `future_events_days` stays global.

### Changed

- The weekday filter moved out of `_collect_icloud_events()` into the new
  `_select_source_icloud_events()`, so it is applied per source alongside the
  title match instead of once with a single global value. Behaviour is unchanged
  for a source without an override.

### Fixed

- Out-of-office detection is provider-aware again. v0.1.6 replaced the original
  "any explicit `TRANSP` means skip" rule with the RFC reading (skip only
  `TRANSPARENT`). That fixed Outlook, which stamps `TRANSP` on every event, but
  silently broke Google: there a real meeting carries no `TRANSP` at all, and an
  explicit value marks time you blocked yourself. On a real work calendar that
  meant 1518 personal blocks — `lunch`, `no meeting time`,
  `Out of office - Pick Up Kids` — started syncing.

  The feed's publisher is now read once from `PRODID`. Google feeds skip any event
  with an explicit `TRANSP`; everything else uses the RFC reading plus
  `X-MICROSOFT-CDO-BUSYSTATUS: OOF`. An unrecognised publisher gets the RFC
  reading, so a new provider errs towards syncing too much rather than nothing.

  Outlook `TENTATIVE` events are kept, matching a Google "maybe": Google feeds
  strip `PARTSTAT` entirely, so those already sync.

  Verified against three live feeds — Google work 1630 (was 3148), Google personal
  4 (unchanged), Outlook 144 (was 0 before v0.1.6).

- `skip_days` now accepts a bare scalar and zero. `skip_days: 6` parses as an int
  and crashed with `'int' object is not iterable`, and `skip_days: 0` (Monday) was
  silently treated as "skip nothing" because 0 is falsy. Both are much easier to
  hit now that a single day is a natural per-source value.
- Tests could load the developer's real `.env`. `_load_config()` calls
  `load_dotenv()`, which injected real credentials into `os.environ` and undid
  the `clean_env` fixture — the entry-point test reached the live Telegram API
  and tripped its flood limit. An autouse fixture now neutralises `load_dotenv`
  for the whole suite.


## [v0.1.6] — 2026-08-12

### Added

- Support for Outlook / Microsoft 365 calendar sources, documented in
  `README.md`. Beyond the `TRANSP` fix below no code changes were needed:
  Outlook feeds are plain ICS, and Windows timezone names
  (`Argentina Standard Time`) are mapped to IANA zones by `icalendar`.
- Full test suite: 218 tests at 100% statement and branch coverage of
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

- `config.yaml.template` and `.env.template` refreshed: removed the `ai_tone`
  and `GEMINI_API_KEY` entries left over from the Gemini integration dropped in
  v0.1.4, fixed the malformed `source-calendar-N` block, and documented the
  logging env vars.

### Fixed

- Source events declaring `TRANSP` are no longer dropped. `_parse_source_events`
  skipped any VEVENT that set `TRANSP` at all; Outlook stamps it on every event,
  so an entire Outlook feed imported as 0 events. Per RFC 5545 only
  `TRANSPARENT` means the event does not consume busy time, and a missing value
  defaults to `OPAQUE` — which is why Google feeds were unaffected.

### Known limitations

- Recurring events are not expanded. `walk("VEVENT")` returns only the series
  master, and because Outlook anchors a series at its original start date, a
  long-running weekly meeting can contribute no events to a forward-looking
  window. Measured on a real Outlook feed, one week yielded 6 events instead of
  18.

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
