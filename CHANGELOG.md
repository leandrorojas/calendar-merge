# Changelog

All notable changes to this project will be documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
tagged in git.

## [Unreleased]

### Added

- Repeated failure alerts are suppressed. Runs are independent processes fifteen minutes apart
  with no memory of each other, so a single upstream outage sent one identical alert per run —
  three on 2026-08-18, and up to forty-one across a full weekday schedule.

  A first failure alerts, the same cause repeating does not, and `failure_alert_every` in
  `config.yaml` sets how many further failed runs pass before the alert repeats, so a long
  outage does not go indefinitely quiet. Recovery is announced, because otherwise silence would
  mean either "working" or "still broken and no longer saying so".

  Every decision fails open: unreadable or unwritable state alerts rather than suppresses, and a
  cause that varies between runs re-alerts. The state lives in `logs/failure-state.json`,
  overridable with `CALENDAR_MERGE_STATE_FILE`.

### Fixed

- The pre-commit hook for mypy pinned 1.20.1 while the lockfile resolved 2.3.1 — a major version
  apart, so a local hook run and CI could disagree about whether the code type-checks at all, in
  either direction. `tools/ruff_version.py` becomes `tools/pinned_versions.py` and guards every
  tool in `PINNED_TOOLS`, not only ruff, reporting all drifted tools rather than the first. Found
  while reviewing a Dependabot ruff bump, where the ruff guard had just done its job for the first
  time.
- The mypy hook covered `^(src|tests)/` while CI type-checks `tools/` as well — the same
  divergence in the other direction: clean locally, red in CI.
- The ruff hook id moves from `ruff` to `ruff-check`. Upstream describes `ruff` as a legacy alias,
  and since the guard forces this `rev` forward on every bump, the release removing it would
  otherwise break pre-commit for whoever pulled next.

- A source calendar's failure no longer costs the calendars after it. `main()`'s loop caught
  only `YamlError` — its termination signal rather than error handling — so any other exception
  escaped the loop entirely. On 2026-08-20 a single already-deleted event in `source-calendar-0`
  meant sources 1 and 2 were never processed; they silently kept the previous run's picture while
  one alert named only the first calendar.

  Failures are now collected per source and raised once at the end, so every calendar gets its
  turn and a single alert describes the whole run: `2 of 3 source calendars failed - ...`, each
  with its underlying cause. The run still fails, because a partial sync is not a success, and
  `--last` withholds "finished for today" rather than contradicting the failure alert.

  Configuration errors are isolated too. A malformed section fails identically every run, but
  aborting stops the healthy calendars from syncing until somebody notices — worse for the
  calendar than syncing them and naming the broken one.

  `MAX_SOURCE_CALENDARS` bounds the loop: catching per-source failures made an unbounded loop
  possible where a persistent fault before the section read would previously have aborted, and a
  hung schedule is worse than a failed one, and the bound is strictly greater so a
  configuration sitting exactly on it is not reported as a failure.

  A `YamlError` from a source read is sorted into three cases rather than two. `YamlHelper.get`
  raises the same type for an absent section, a missing setting inside an existing one, and a
  config file it cannot read at all — and it re-reads the file on every call. Reading the
  malformed case as the end of the list skipped every later calendar silently; reading a
  file-level fault as a malformed section logged it once per index and reported more failures
  than the user has calendars.

  The alert is also budgeted rather than concatenated: condensed to `ERROR_PART_MAX_CHARS` by the
  failure handler, an unbounded summary was truncated to its first failure, which is what
  aggregating them was meant to replace. Each cause now gets an equal share, so every failed
  source is always named.

## [v0.1.16] — 2026-08-21

### Changed

- `_parse_source_events` split into `_field_datetime`, `_parse_single_event` and
  `_parse_recurring_event`. Adding recurrence expansion had taken it from a cognitive
  complexity of 13 to 28 against a limit of 15, and the single-event path still inlined the
  datetime rebuild that `_normalise_ics_datetime` was added for, so the same code existed
  twice. Behaviour is unchanged: all tests pass unmodified and the three live feeds parse
  identically before and after.

### Added

- CI derives the Ruff version from `uv.lock` rather than repeating it. It was declared in six
  places and Dependabot could see two: it updates `pyproject.toml` and the lockfile, but not the
  three literals in `ci.yml` nor the `rev` in `.pre-commit-config.yaml`. The 0.15.10 → 0.16.3
  bump therefore left CI linting with the previous release while local runs used the new one —
  the drift the exact pin exists to prevent — and nothing detected it.

  `tools/ruff_version.py` reads the locked version, and `--check` asserts the pre-commit `rev`
  agrees, since a `rev` cannot be derived at run time. Drift now fails on the Dependabot PR
  itself with a message naming the fix. Standard library only, run under `uv run --no-project`,
  so the lint job still needs no private dependency.

  Guarded twice, because the value reaches a shell and `uv.lock` is editable by a fork pull
  request: the script rejects anything not shaped like a version, and the workflow passes it
  through `env:` rather than `${{ }}`, which GitHub substitutes textually before the shell
  parses the line.

- CI asserts the wheel layout. The test suite imports `merge` through pytest's
  `pythonpath = ["src"]`, which resolves it from the source tree and never from the built
  artifact, so full coverage says nothing about whether an installed copy can start — a wheel
  shipping the module as `src/merge.py` went undetected for two releases. The check derives the
  expected module from `entry_points.txt` rather than hardcoding it, so renaming the entry point
  without moving the module is caught as readily as the reverse. It needs no private dependency,
  since building only runs the hatchling backend, and so lives in the lint job.

  The check itself lives in `tools/assert_wheel_layout.py` rather than inline in the workflow,
  so it is linted, typechecked and tested like any other code. It refuses to pass vacuously:
  an empty `console_scripts` section, entry points without that section, a wheel with no entry
  points, and an ambiguous choice between several wheels are each a failure with a message
  naming the problem. Metadata is read from `*.dist-info/entry_points.txt` rather than any file
  with that name.
- `README.md` documents both ways to start the program and when each is preferable. The console
  script depends on packaging and syncs the environment first; running the module directly
  involves neither, which makes it the more predictable choice for a scheduled run.

## [v0.1.15] — 2026-08-20

### Added

- Each source calendar now logs what its sync changed —
  `[MCP] meet/parat: 17 added, 2 deleted`, with `, N already gone` appended when a delete found
  the event already absent. Every other step in the pipeline announced itself while the one that
  mutates the calendar stayed silent, so confirming a run had done anything meant inferring it
  from how long the process paused.

  Three details matter more than they look:

  - **An already-gone event is counted apart from a deletion.** We did not delete it, and folding
    the two together would overstate a run's effect — a systemic fault making every delete return
    404 should not read like a run that genuinely removed events.
  - **The report is emitted from a `finally` inside `_sync_events_to_icloud`.** A mid-sync failure
    is exactly when it matters, because the calendar has been mutated and nothing else says by how
    much; a summary written after the call returned would be skipped by the raise. It cannot live
    in the caller either, where `outcome` is unbound once the call raises. This also makes the
    tally's counting order observable, so a failed add is provably not counted as a change.
  - **`term.print_done()` moved in with it.** The caller opens an unterminated `synchronizing...`
    line, so anything printed before that line is closed gets glued onto it, orphaning `done!`.
    The failure path already closed the line via `term.print_failed()`, so both ends of the line
    now belong to the same function.

## [v0.1.14] — 2026-08-20

### Added

- Recurring events from source feeds are expanded. `walk(VEVENT)` yields only the series
  master, whose `DTSTART` is the first occurrence — and Outlook anchors that at the date the
  series was created, so a long-running weekly meeting sat outside any forward-looking window
  and contributed nothing. Measured against the live Outlook feed on 2026-08-20: **2 events
  became 19** for the same 12-day window. The Google feeds are unchanged at 14 and 1, because
  Google pre-expands server-side.

  The expansion honours `EXDATE` and `RECURRENCE-ID`, which is what makes it safe rather than
  merely fuller — without them it would create events for cancelled meetings and place moved
  ones twice. An unreadable rule falls back to the master's own occurrence rather than
  dropping the meeting.

  `RDATE` extras are included, a `DTSTART` that does not match its own rule is kept (RFC 5545
  makes it an occurrence; dateutil omits it, which would have lost meetings that synced before
  expansion existed), and a VEVENT carrying both `RECURRENCE-ID` and `RRULE` is treated as the
  `THISANDFUTURE` split it is rather than as an override of itself.

- `python-dateutil` is declared as a direct dependency. `merge.py` imports it to expand rules
  and it arrived transitively via `icalendar` — the same accident that broke `import click`
  when pyicloud dropped it.

## [v0.1.13] — 2026-08-20

### Security

- Lockfile upgraded, clearing all 15 Dependabot advisories found when alerts were switched on:
  `cryptography` 46.0.3 → 50.0.0 (5 advisories, 4 high), `urllib3` 2.5.0 → 2.7.0 (4 high),
  `requests` 2.32.5 → 2.34.2, `idna` 3.11 → 3.19, `python-dotenv` 1.1.1 → 1.2.3 and
  `Pygments` 2.19.2 → 2.21.0. All were transitive; none was reachable from a pin in
  `pyproject.toml`. `pyicloud` stays at 2.6.5.

  Verified past the test suite, which fakes pyicloud and patches `load_dotenv`: `icalendar`
  7.3.0 produces byte-identical results to 7.2.2 on all three live feeds (same totals, same
  kept counts, same slot hashes), the `python-telegram-bot` 22.8 surface `pyfangs` calls is
  unchanged, and `load_dotenv` still reads a real file.

### Added

- `README.md` documents the scheduling constraint: two invocations must never share a minute.
  Concurrent runs act on the same stale iCloud snapshot, which is what produced the `404` on
  2026-08-20 and can also create duplicate events. Written down because the collision came
  from two cron lines that look independent — `0 8 * * 1-5` and `*/15 8-17 * * 1-5`.

## [v0.1.12] — 2026-08-20

### Fixed

- A 404 when deleting an iCloud event is now treated as the event already being gone, which
  is what the action was asking for. pyicloud's `remove_event` fetches the etag through
  `get_event_detail` first, so an event removed from another device between the iCloud load
  and the delete raised there and aborted the run — and because `main()`'s source-calendar
  loop catches only `YamlError`, that skipped every remaining source calendar too. Observed
  2026-08-20. The outcome is logged, so a systemic fault making every delete 404 stays
  visible instead of reading as success.

  Root cause of the observed instance was not another device: the crontab fired the
  `--first` job and the every-15-minutes job in the same minute, so two processes loaded
  the same snapshot and both tried the delete. The schedule is the real remedy; this change
  stops one losing process from aborting a whole run.

## [v0.1.11] — 2026-08-19

### Fixed

- Failure alerts are now bounded and single-line. `PyiCloudAPIResponseException` appends the
  entire HTTP response body to its message, so a 500 returning an Apple error page would have
  put multiple KB of markup into the Telegram message. Telegram rejects anything past 4096
  characters and `send_telegram_message` is best-effort, so the alert would not have arrived
  at all — the worse the upstream error, the more certainly nothing is heard. The 500s on
  2026-08-18 had empty bodies, which is the only reason v0.1.10 did not show this.

  `_condense` collapses whitespace and caps each part. A cause with no message now renders as
  `wrapper (KeyError)` instead of leaving a dangling colon.

## [v0.1.10] — 2026-08-19

### Fixed

- pyicloud is no longer allowed to request 2FA codes on its own. 2.6.5 added
  `_request_2fa_code`, called from inside `authenticate()` — which `PyiCloudService` runs
  in its constructor — pushing to the trusted device and then sending an SMS, honouring
  neither `_can_request_sms_2fa_code` nor anything else settable on an instance that does
  not exist yet. One re-authentication delivered a push, an SMS and then our own push, and
  because each fresh request invalidates the previous code, the code the user reads may
  already be dead. `_disable_automatic_2fa_requests()` patches the class before construction.

### Changed

- Failure alerts now carry the chained cause. The `__main__` handler sent `str(err)`, and
  because raise sites wrap low-level failures in a readable RuntimeError, every alert said
  where the merge stopped and never why. It now sends `_describe_error(err)`, which appends
  the causes up to `ERROR_CAUSE_DEPTH`.

  Prompted by an Apple outage on 2026-08-18: the calendar events endpoint returned bodiless
  HTTP 500s for ~45 minutes, and since pyicloud rewrites the reason for any 409/421/450/500
  to `"Authentication required for Account."`, the alert read as a broken session. The real
  cause was only recoverable by reading the log file on the host.

### Added

- `BACKLOG.md`, recording known gaps that are deliberately unfixed — recurring-event
  expansion, the host-timezone coupling of the date window, the ruff pin that drifts on
  every bump, CI's blindness to the packaging layout, and the 2FA flow never having been
  exercised against a live Apple challenge. Each entry carries its evidence and what a fix
  would take, so a deferral can be re-examined rather than rediscovered.

## [v0.1.9] — 2026-08-18

### Changed

- Dependencies updated after Dependabot was repaired: `icalendar` 6.3.1 → 7.2.2,
  `pyicloud` 2.5.0 → 2.6.5, `click` 8.3.0 → 8.4.2, `mypy` 1.20.1 → 2.3.1,
  `pytest` 9.0.3 → 9.1.1, `pre-commit` 4.5.1 → 4.6.2, `ruff` 0.15.10 → 0.16.3,
  plus five GitHub Actions. Verified beyond the test suite, which fakes pyicloud:
  icalendar 7 produces byte-identical results on all three live feeds, and every
  pyicloud API and documented behaviour `merge.py` depends on is unchanged.
- The `ruff` pin in `ci.yml` and `.pre-commit-config.yaml` bumped to match. Those
  are string literals Dependabot cannot see, so its `uv` update left CI linting
  with 0.15.10 while the project used 0.16.3 — the exact drift that pin exists to
  prevent.

### Fixed

- `click` is now declared as a direct dependency. `merge.py` imports it for the
  interactive 2FA prompts but relied on it arriving transitively via pyicloud,
  which dropped it in 2.6.5 — so `import click` failed as soon as that upgrade was
  attempted. The import had only ever worked by accident.
- The `calendar-merge` console script now works from an installed wheel. The
  wheel shipped the module as `src/merge.py`, because hatch's `include` filters
  paths without stripping them, and `src` is not a package -- so the entry point's
  `from merge import main` raised `ModuleNotFoundError` on startup. Adding
  `sources = ["src"]` puts `merge.py` at the wheel root. Present since at least
  v0.1.8.
- `_configure_logging` no longer skips setup when an unrelated handler is attached
  to the logger. Its guard was `if logger.handlers`, so any third party attaching a
  handler silently left the application with no file logging at all. It now checks
  for its own `RotatingFileHandler`. Surfaced by pytest 9.1, which attaches a
  capture handler directly to loggers with `propagate = False`.

## [v0.1.8] — 2026-08-14

### Added

- Telegram confirmation when an Apple 2FA code is accepted. The code is submitted
  over Telegram but success was only reported to the terminal and the log file, so
  an accepted code was indistinguishable from one that never arrived until a
  failure message or the evening notification appeared. Sent only on the
  trusted-device path, where the user is actually waiting on Telegram.
- CodeQL security scanning (`.github/workflows/codeql.yml`), free and unlimited on
  public repositories. Runs on pushes to `main`, on every pull request, and weekly
  so newly published queries reach unchanged code. It complements the existing
  reviewers by doing dataflow analysis rather than reading the diff, and found the
  clear-text logging issue below within minutes of being enabled.

### Changed

- The 2FA code prompt is retried up to three times instead of aborting the merge
  on the first rejected code. Apple's push is requested only on the first
  attempt, since re-requesting invalidates the code the user is holding, and a
  timeout still returns immediately rather than burning attempts.
- `_get_telegram_credentials` is no longer a coroutine. It only reads environment
  variables, so the `async` keyword implied an await that never happened.

### Fixed

- A calendar listing two meetings in the same slot no longer syncs two events.
  `_parse_source_events` now collapses source events sharing an exact
  `(start, end)`, which is lossless because parsed events carry no title or raw
  event and every synced event gets the same source tag. Duplicates created
  before this change clean themselves up on the next run. Only exact matches
  collapse: overlapping and contained slots stay separate, and deduplication is
  per calendar. Measured on live feeds: 1630 → 1628 and 144 → 143 events.
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
- Session trust is now requested even when 2FA code validation fails. Apple can
  refuse a code while still granting trust, and the v0.1.5 refactor's early return
  stopped that request from happening — so a run like the 2026-07-30 incident no
  longer established the trust that had made the following run succeed without a
  prompt. The run still reports failure, but a Telegram message explains that the
  session is trusted and the next run should not prompt.
- `_request_session_trust` now guards `api.trust_session()`. pyicloud catches only
  two exception types there, while `_authenticate_with_token()` raises a third —
  which, on the failure path this release added, would relabel an accurate
  "2FA validation failed" as the generic "2FA validation error".
- A session that was *already* trusted is no longer reported as one that just
  became trusted. `requires_2fa` can be true on a trusted session, so that message
  promised a quiet next run on the basis of a flag that had not prevented this one
  from prompting.
- Six `pytest.raises` blocks contained more than one call that could throw, so the
  assertion did not prove which one raised. Setup calls are hoisted out.

### Security

- Trusted-device details are no longer written to the persistent log. CodeQL
  flagged `py/clear-text-logging-sensitive-data` (high): the 2SA device picker
  formatted phone numbers and device names into a message that `print_step`
  mirrored into `logs/calendar-merge.log`. Only the device count is logged now, the
  picker is written straight to the terminal, and the number is masked to its last
  four digits.

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

See git history for details.

A v0.2.0–v0.2.2 line was tagged in Feb–Mar 2026 (override/cancel, dry-run, state
management) and then reverted in PR #42. Those tags and their GitHub releases were
deleted in Aug 2026: they never described shipped behaviour, they sorted above the
current version in any version-ordered listing, and the 0.2.x range is wanted for a
future v2. The commits they pointed at remain reachable from `main`:

| Tag | Commit | Merged |
|---|---|---|
| v0.2.0 | `958400c` | #21 override/cancel docs |
| v0.2.1 | `f941b96` | #39 override-in-range |
| v0.2.2 | `b74783d` | #41 telegram debug command |
