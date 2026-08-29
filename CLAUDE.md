# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

The console script (`uv run calendar-merge`) and direct execution
(`.venv/bin/python src/merge.py`) are both supported and take different paths: the former
imports `merge` as an installed top-level module, the latter runs the file as `__main__` with
its own directory on `sys.path`. A packaging fault therefore breaks the first and not the
second, which is why CI asserts the wheel layout — see `README.md` for when to prefer each.

```bash
uv sync                          # Install dependencies
uv sync --extra dev              # Install dependencies + dev tooling (mypy, pytest)
uv run calendar-merge            # Run calendar merge
uv run calendar-merge --first    # Morning sync + Telegram start-of-day notification
uv run calendar-merge --last     # Evening sync + Telegram end-of-day notification
```

## Quality Checks

```bash
uv run ruff check src/ tests/ tools/            # Lint
uv run ruff format --check src/ tests/ tools/   # Format check
uv run mypy src/ tests/ tools/                  # Type check
uv run pytest tests/ -v                         # Unit tests
uv run pytest --cov                             # Unit tests + coverage gate
```

CodeQL (`.github/workflows/codeql.yml`) runs security queries on the same triggers plus a weekly
schedule, so newly published queries reach code that has not changed. It needs no build step because
Python is interpreted, and `security-events: write` is scoped to that job alone. `security-extended`
is enabled; the `quality` suite is deliberately left off to avoid duplicating Ruff and SonarCloud.

Dependabot runs both kinds of update. **Version** updates come from `.github/dependabot.yml`
(the `uv` and `github-actions` ecosystems, with a `registries` entry so the private `pyfangs`
repo can be reached). **Security** updates and alerts are repository settings rather than file
configuration, enabled 2026-08-20 — note the API reports alerts as *disabled* by returning
`404` from `GET /repos/{owner}/{repo}/vulnerability-alerts` and `204` when enabled, which reads
like a permissions failure and is not one.

CI (`.github/workflows/ci.yml`) runs these on push to `main` and on every pull request, split into
two jobs: `lint` (Ruff only, no private deps needed) and `test-and-typecheck` (needs the
`PYFANGS_DEPLOY_KEY` secret to install the private `pyfangs` dependency over SSH).

`uv.lock` is the single source of truth for pinned tool versions. The lint job reads it through
`tools/pinned_versions.py` instead of repeating it, because Dependabot updates the lockfile and
`pyproject.toml` but cannot see a string literal in a workflow — so the 0.15.10 to 0.16.3 bump
left CI linting with the previous release while local runs used the new one, which is the exact
drift the pin was introduced to prevent. `--check` additionally asserts that **every** pinned `rev` in
`.pre-commit-config.yaml` agrees — `PINNED_TOOLS` lists them, since a pre-commit `rev` cannot be derived at run time and so
has to be verified instead.

The same hole was open for mypy and went unnoticed longer, because only ruff was guarded: the
hook pinned 1.20.1 while the lockfile resolved 2.3.1, a major version apart. A pre-commit run
and CI could therefore disagree about whether the code type-checks at all — in either
direction, a local pass that fails CI or a real type error accepted locally. Adding a tool to
`.pre-commit-config.yaml` without adding it to `PINNED_TOOLS` would leave it unguarded, so
`unguarded_repositories()` compares the config against `PINNED_TOOLS` and `UNGUARDED_REPOS`
and a test asserts nothing is missing. Asserting the table's literal contents instead would
only catch removals, which is the direction that cannot silently drift.

The config is parsed **by block**, not matched with one pattern spanning repository name to
`rev:`. Such a pattern reads whatever lies between them, so a comment naming a repository, a
reordered `hooks:` key, or `ruff-pre-commit-nightly` listed first each silently change which
version is checked. A repository is also required to declare at least one hook: one pinned at
the right version with an empty `hooks:` list runs nothing, so agreeing with the lockfile
proves nothing — the same vacuous-pass failure the wheel-layout check had. `check()` evaluates every tool before
raising, so a bump moving two does not hide one behind the other. The script imports only the standard library, so the step runs it with plain `python` rather
than through `uv run`: there is no environment to resolve and nothing to build, which keeps
the lint job free of the private dependency and its deploy key. Routing it through uv meant
either resolving dependencies it does not have or passing flags to suppress that.

The resolved version reaches a shell, and `uv.lock` is a checked-in file a fork pull request can
edit, so it is guarded twice. The script rejects anything not matching `SAFE_VERSION`, and the
workflow passes the value through `env:` rather than `${{ }}` — GitHub substitutes `${{ }}`
textually before the shell parses the line, so a crafted version string would otherwise be a
command injection. The regex that locates the pre-commit `rev` deliberately captures the whole
token rather than a safe character class: validation belongs in one place, or a malformed rev is
silently truncated to its valid prefix and reported as drift instead of as malformed.

`tools/assert_wheel_layout.py` is the CI packaging check, covered by
`tests/test_wheel_layout.py`. It is tested for the same reason it exists: its value is entirely
in failing correctly, and its worst failure mode is passing while verifying nothing — an empty
`console_scripts` section would leave its loop with nothing to do. `tools/` is linted and
typechecked alongside `src/` and `tests/`, and is on pytest's `pythonpath` so the tests
import it normally rather than editing `sys.path`. It stays outside the coverage gate,
which measures `src/` only.

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

**Failure alerts carry the chained cause.** The `__main__` handler sends
`_describe_error(err)`, not `str(err)`. Raise sites wrap low-level failures in a readable
RuntimeError -- `raise RuntimeError("Unable to load events from iCloud") from err` -- so the
bare message says where the merge stopped and never why. `_describe_error` walks `__cause__`
up to `ERROR_CAUSE_DEPTH` and renders `wrapper (Type: message <- Type: message)`.

Each part goes through `_condense`, which flattens whitespace to one line and caps the length,
bounding the whole message at roughly `(ERROR_CAUSE_DEPTH + 1) * ERROR_PART_MAX_CHARS`. That is
not cosmetic: `PyiCloudAPIResponseException` appends the entire HTTP response body to its own
message, so an Apple error page arrives verbatim. Telegram rejects anything past 4096
characters and `send_telegram_message` swallows the failure, so an unbounded alert is not a
long alert — it is **no alert at all**, exactly when the upstream error is worst.

Only `__cause__` is followed. `from err` is explicit and always meaningful; `__context__` is
whatever happened to be in flight and would fill a phone-sized alert with noise.

**pyicloud rewrites the reason for 409/421/450/500.** `session.py::_raise_error` replaces the
real reason with the literal string `"Authentication required for Account."` for any of those
codes, and `AppleAuthError.GENERAL_AUTH_ERROR` is **500** -- so a plain server error is reported
as an authentication problem. Apple's actual reason is discarded and cannot be recovered.

On 2026-08-18 the calendar *events* endpoint returned bodiless 500s for ~45 minutes (runs at
16:45, 17:00 and 17:15 failed; 17:30 was clean). `get_calendars()` succeeded seconds earlier in
the same run because it uses a different endpoint, which made the failure look specific to our
code. Two tells separate this from a real auth failure: the exception message has no
`: {body}` suffix, since `PyiCloudAPIResponseException` appends `response.text` whenever there
is one, and no 2FA prompt is issued. Do not chase a session bug on this signature without
checking both.

**Each source calendar reports what its sync changed.** `_sync_events_to_icloud` returns a
`SyncOutcome` and `_process_source_calendar` logs it as
`[MCP] meet/parat: 17 added, 2 deleted`, with `, N already gone` appended only when non-zero.

Every other step in the pipeline announced itself — downloading, filtering, reconciling — and
then the one step that actually mutates the calendar did so silently. Confirming a run had done
anything meant inferring it from how long the process paused: watching the v0.1.14 deploy came
down to reading a nine-second gap in the log and multiplying by an assumed round-trip time.

An already-gone event is counted apart from a deletion. We did not delete it, and folding the
two together would overstate what the run did — which matters because a systemic fault making
every delete return 404 should look different from a run that genuinely removed events.

The report is emitted from a `finally` inside `_sync_events_to_icloud`, not by its caller. A
mid-sync failure is exactly when it matters — the calendar has been mutated and nothing else says
by how much — and a summary printed after the call returns is skipped by the raise. It cannot
live in the caller either: `outcome` is unbound there when the call raises.

That also makes counting order load-bearing. The tally counts after each API call succeeds, so it
means "changed" rather than "attempted"; counting before would report a failed add as a change.
An earlier note here called that an equivalent mutant, which was true only while a failure
skipped the report entirely — it is now killed by two tests.

**Failure alerts fire on transitions, not on occurrences.** Runs are independent processes
fifteen minutes apart holding no state between them, so a single upstream outage sent one
identical alert per run — three on 2026-08-18, and up to forty-one across a full weekday
schedule. None after the first carried information the first had not.

`_should_report_failure` records the cause in a state file (`CALENDAR_MERGE_STATE_FILE`,
default `logs/failure-state.json`) and reports only when the failure is *news*: a first
failure, a changed cause, or the reminder cadence coming round. `failure_alert_every` in
`config.yaml` sets that cadence — with a 15-minute schedule the default of 4 reminds hourly,
and `0` disables the repeat.

`_report_recovery` closes the loop. Without it suppression makes silence ambiguous: a quiet
channel would mean either "working" or "still broken and no longer saying so".

**Every decision on that state fails open.** An unreadable, corrupt or absent file is treated
the same — alert — and an unwritable one only costs suppression rather than crashing the run. A
lost alert is worse than a duplicated one. Causes are compared as text, so a message that varies
between runs re-alerts, which is right: a fault that keeps changing is news each time.

`tests/conftest.py` redirects the state file per test through an autouse fixture. It persists by
design, so without that the suite writes into the repository's `logs/` and one test's recorded
failure suppresses another's alert.

`cli()` is the entry point for **both** the console script and `python src/merge.py`, and
`pyproject.toml` points `calendar-merge` at it rather than at `main()`. It pointed at `main()`
before, which skips `_configure_logging` and `_run_and_report` entirely — so on the path the
README schedules, a failure produced a bare traceback with no log line and no Telegram alert at
all, and this whole feature was inert.

`_run_and_report` holds the outcome path rather than the `__main__` block, because driving that
block through `runpy` re-executes the module and leaves no seam for making `main()` succeed or
fail on demand.

**The state is validated on read, not trusted.** `json.loads` returns `Any`, so `[]` or
`{"runs": null}` type-checks fine and then raises from `.get(...)` or `int(...)` *inside* the
failure handler — escaping it and losing the alert it was handling, which is the one outcome the
fail-open design forbids. `_read_failure_state` returns `None` unless the parsed value is a dict
whose counters coerce to integers.

**A failure counts as reported only once the alert is delivered.** `send_telegram_message`
swallows transport errors, so recording "alerted" before the send meant a flood-control response
on the first failure suppressed every repeat — hiding the outage for an hour, or forever with
`failure_alert_every: 0`. It now returns whether the message went out, and `alerted_at` records
the run at which one actually did. Before this feature existed a dropped message self-healed on
the next run; that property is preserved deliberately.

**The state file is written atomically**, via a temporary file and `os.replace`. Runs can
overlap — 2FA polling alone may span a whole cron interval — and a torn truncating write reads as
absent. That fails open for a failure but fails *closed* for recovery: the "recovered" message
would simply never arrive.

**One source calendar's failure does not cost the calendars after it.** `_process_all_source_calendars`
catches `YamlError` as its *termination signal* — a missing section is how it learns the list
has ended, which is why that handler must stay ahead of the one below it. Every other exception
is recorded against its source and the loop continues.

Aborting cost far more than the calendar that failed. On 2026-08-20 a single already-deleted
event in `source-calendar-0` meant sources 1 and 2 were never processed: they silently kept the
previous run's picture while one alert named only the first calendar.

Failures are raised **once, after every source has had its turn**, so a single alert describes
the whole run rather than whichever calendar happened to fail first. The run still fails — a
partial sync is not a success — and `--last` withholds "finished for today" rather than
contradicting the failure alert.

Configuration errors are isolated too, not just transient ones. A malformed section will fail
identically on every run, but aborting means the healthy calendars stop syncing until somebody
notices, which is worse for the calendar than syncing them and naming the broken one.

**A `YamlError` from a source read means one of three things, not two.**
`YamlHelper.get` re-reads `config.yaml` on every call and raises the same type when the section is
absent, when a setting inside an existing section is missing, and when the file itself cannot be
read. `_classify_source_config_error` sorts them by pyfangs' message prefix into
`SourceConfigOutcome.absent` (the list has ended), `malformed` (this calendar fails, the rest
proceed) and `unusable` (the file — stop at once).

Each mapping matters. Reading a **malformed** section as the end of the list skips every calendar
after it without recording anything, the very failure this handling exists to prevent. Reading an
**unusable** file as a malformed section logs the identical error once per index and reports more
failures than the user has calendars, because every later index re-reads the same broken file. The
unknown case defaults to `unusable`, which stops rather than retries.

That coupling to message text is deliberate and monitored: `TestYamlErrorShapes` pins all three
against the **real** `YamlHelper`, so a change to pyfangs' wording fails the build rather than
silently reclassifying. The fakes in `conftest.py` mirror the shapes for the same reason — fakes
that blurred them hid the distinction entirely, and fixing them surfaced thirteen failures at once.

**The aggregated alert is budgeted, not just concatenated.** The `__main__` handler condenses it to
`ERROR_PART_MAX_CHARS`, so a summary built from unbounded per-source text is truncated to its first
failure — which is what aggregating them was meant to replace. `_summarise_source_failures` gives
each cause an equal share of what remains after the header and the section names, so the identities
always survive and only the details are trimmed.

**`MAX_SOURCE_CALENDARS` is a runaway guard, not a policy limit.** `YamlHelper` leaks `TypeError`
rather than `YamlError` when the config's top level, or a section's value, is a list — and that
repeats at every index, so the loop would never end. The bound is strictly greater so a
configuration of exactly that many calendars still reaches its terminating lookup, and its message
says what happened rather than implying a configured maximum.

`MAX_SOURCE_CALENDARS` bounds the loop. Catching per-source failures made an unbounded loop
possible where a persistent fault *before* the section read would previously have aborted the
run, and a hung schedule is worse than a failed one. Its test raises a `BaseException` past the
handler, so a missing bound fails immediately instead of hanging.

**A 404 when deleting means the event is already gone, and is not an error.**
`_sync_events_to_icloud` treats it as success. Deleting is idempotent in intent: the action
asks for the event not to exist, and a 404 says it does not. pyicloud's `remove_event` fetches
the etag through `get_event_detail` before issuing the DELETE, so an event removed from another
device between `ICLOUD_CALENDAR_LOAD` and the delete raises there rather than at the delete.

Aborting cost far more than the event. `main()`'s source-calendar loop catches only `YamlError`,
so a `RuntimeError` escaped the loop and skipped **every remaining source calendar** — one event
deleted on a phone sank the entire run, including calendars that had nothing to do with it.

The outcome is logged rather than swallowed. If a systemic fault ever made every delete return
404, the merge would otherwise report success while doing nothing at all — so the line saying an
event "was already gone" appearing on every run is the signal to investigate.

`_is_missing_event_error` reads `code` when pyicloud raises its own exception and falls back to
the attached response's `status_code`: a 404 only becomes `PyiCloudAPIResponseException` when
Apple answers with JSON, and arrives as `requests.HTTPError` when it does not. Only 404 is
benign; every other status still stops the run.

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

**The recurrence anchor is skipped forward before expanding.** `rule.between()` iterates from
`DTSTART` and discards everything earlier, so the cost tracks the anchor's *age*, not the window —
and Outlook anchors a series at its creation date, so that gap is routinely years.

`_advance_anchor` moves the anchor by a whole number of periods. That leaves the recurrence
lattice unchanged: period boundaries stay where they were, so `BYDAY`, `BYMONTHDAY` and
`BYSETPOS` — all evaluated relative to those boundaries — still select the same dates. It never
moves past `window_start`, so the period containing the window is generated in full.

It refuses to move an anchor whenever the shift cannot be proven safe:

- **A day above 28 with `MONTHLY`/`YEARLY`.** `relativedelta` clamps a day the target
  month lacks — 31 January plus 44 months is 30 September — and dateutil derives the day
  from `DTSTART` when there is no explicit `BYMONTHDAY`, so the clamp moves every later
  occurrence permanently. `FREQ=MONTHLY;INTERVAL=2` anchored 2021-07-31 went from
  contributing nothing to contributing 2026-11-30, **inventing busy time**. Monthly and
  yearly series are cheap to expand naively, so refusing them costs nothing worth having. a **`COUNT`**-limited rule,
whose occurrences are positional and would gain phantom ones past the end of the series; an
unrecognised or missing `FREQ`; a non-positive `INTERVAL` (`INTERVAL=0` makes dateutil itself
spin, so it must not be reasoned about at all).

The estimate floors against the **longest** a period can be, so it can only undershoot and the
correction only moves forward. Flooring against an average month instead makes overshoot possible
in principle and unreachable in practice — a branch that cannot be exercised is a branch that is
never known to be right.

Correctness is asserted by equivalence rather than by expected dates: `TestAdvanceAnchor` expands
22 rule shapes across 9 anchor dates and 5 windows both ways and requires identical output.

The anchor dates matter as much as the rules. The first version stopped at day 29 and passed
132/132 while the clamping bug above was live — the matrix has to include days 30 and 31, and
windows in months of differing length, or it proves only that the shapes someone thought of
work.

Measured 2026-08-28. On the live feeds this saves **0.8ms** — they hold 12 series, none finer than
weekly, and Google pre-expands so contributes none at all. The value is in bounding the worst
case: `FREQ=MINUTELY` anchored 3.7 years back goes from 1.876s to 0.018s, same output.

**Recurring events are expanded, and the expansion is the delicate part.**
`walk(ICS_TAG_VEVENT)` yields only the series master, whose `DTSTART` is the *first* occurrence.
Outlook anchors a series at the date it was created, so a long-running weekly meeting sits far
outside any forward-looking window and used to contribute **nothing at all** — measured
2026-08-20, the Outlook feed produced 2 events for a 12-day window where it should have produced
19.

Only Outlook needs this. Google pre-expands server-side: its feeds carry **0** `RRULE` and
thousands of `RECURRENCE-ID` VEVENTs, one per occurrence. That is why the gap never showed on the
Google calendars despite their being most of the event volume.

Three rules make the expansion safe rather than merely fuller:

- **`EXDATE` cancels occurrences.** Skipping this creates events for meetings that were called
  off. That is worse than the under-reporting it fixes: an absent event can be checked against
  the source calendar, a phantom one is self-consistent and blocks time that is genuinely free.
- **`RECURRENCE-ID` overrides replace a slot.** A moved occurrence is published as its own
  VEVENT, which the ordinary path already parses, so the master must skip that slot or the
  meeting lands at both its new and its original time. Matching is on `(UID, occurrence start)`
  — time alone would let one series suppress another's slot.
- **The master must not also flow through the ordinary path.** Its `DTSTART` is itself an
  occurrence, so a first occurrence removed by `EXDATE` or replaced by an override would
  reappear. `_deduplicate_event_slots` hides this in the ordinary case, which is what makes it
  worth a test rather than a comment.

`_expand_recurrence` returns `None`, distinct from `[]`, when a rule cannot be parsed. `[]` means
the series genuinely places nothing in the window; `None` falls back to treating the master as a
plain event, so an unreadable rule costs its repeats rather than the meeting itself.

**2FA code entry over Telegram.** `_validate_2fa_trusted_device` retries the prompt up to
`TWO_FACTOR_CODE_ATTEMPTS` times, because a human types the code and one mistyped digit used to
abort the whole merge until the next scheduled run. Three rules matter:

- **Apple's push fires only on the first attempt.** `after_send` is passed on attempt 1 and `None`
  afterwards; re-requesting issues a fresh code and invalidates the one the user is holding.
- **A timeout does not retry.** `prompt_telegram_reply` returning `None` means nobody is answering
  or the transport is broken, so the loop returns immediately instead of burning attempts.
- **A raised error counts as a rejection.** pyicloud returns `False` for a wrong code but can raise
  for an expired one. `_validate_two_factor_code` catches it so the retry loop survives; letting it
  escape relabels a bad code as the generic `"2FA validation error"`.

**Session trust is requested even when the code is rejected.** Apple can refuse a code while still
granting trust. On 2026-07-30 the trusted-device bridge failed to bootstrap, so no code could
validate, yet `trust_session()` succeeded and the following run needed no 2FA at all. The v0.1.5
refactor replaced the old `status` flag with an early return, which made the alert honest but threw
that recovery away — every later run would have prompted again. `validate_2fa` now calls
`_request_session_trust` regardless of the validation outcome and still returns the validation
result, so the run fails (syncing on an unverified authentication would be worse) while the trust is
kept. When validation failed but trust was granted, `TELEGRAM_2FA_TRUSTED_AFTER_FAILURE_MESSAGE`
explains that the next run should not prompt — otherwise the user only sees
`Calendar merge failed`.

`_request_session_trust` returns a `SessionTrust` enum rather than a bool, because **"was already
trusted" must not be reported as "trust has just been granted"**. `requires_2fa` is true whenever
`hsaChallengeRequired` is set, even on a trusted session, so a run can prompt, fail, request nothing,
and still find `is_trusted_session` true — promising a quiet next run there is a false reassurance,
since that same flag did not stop *this* run from prompting. Only `SessionTrust.granted` sends the
message.

It also wraps `api.trust_session()`. pyicloud catches only `PyiCloudAPIResponseException` and
`PyiCloud2FARequiredException`, while `_authenticate_with_token()` raises
`PyiCloudFailedLoginException`, which is neither. Since this call now runs on the failure path too —
where the session is least healthy — an escaping error would relabel an accurate
`"2FA validation failed"` as the generic `"2FA validation error"` and suppress the trust message.

**A raised `request_2fa_code()` must NOT disable the retries.** It is tempting to treat it as "no
code was sent", and that is wrong twice over. pyicloud's bridge posts step0 — which makes Apple push
the code — *before* the wait that times out, and when the bridge state is left unset
`validate_2fa_code()` falls back to `_validate_trusted_device_code`, the legacy endpoint, which
validates real codes. So the user usually does hold a working code. Short-circuiting there aborts on
a single mistyped digit in exactly the bridge state this deployment hits most often.

On success `_validate_2fa_trusted_device` sends `TELEGRAM_2FA_ACCEPTED_MESSAGE`. That send lives
there rather than in `validate_2fa` after `_request_session_trust`, because `validate_2fa` **ignores
the FIDO2 result** — confirming from that point would claim success for a key confirmation that
failed. FIDO2 and 2SA also prompt on the terminal, so nobody is waiting on Telegram for them.
Failure needs no equivalent: a rejection re-prompts, and an exhausted attempt limit reaches the
`__main__` handler, which sends `Calendar merge failed: ...`.

Replies are filtered by `_is_two_factor_code` (six digits) before being submitted. Without it,
`_poll_telegram_updates` returned the first text message after the prompt, so `"ok"` — or anyone
else speaking in the chat — was sent to Apple as the code. The predicate is injected through
`prompt_telegram_reply` → `_wait_for_telegram_reply` → `_poll_telegram_updates` so the transport
stays generic; passing no predicate accepts any text.

`prompt_telegram_reply` swallows transport errors the same way `send_telegram_message` does. It did
not, and a flood-control response during 2FA surfaced as a 2FA failure rather than a Telegram one.

**2FA flow (pyicloud 2.6.5):** `api.request_2fa_code()` triggers the trusted-device push. The SMS fallback is explicitly disabled via `api._can_request_sms_2fa_code = lambda: False` because pyicloud's trusted-device bridge can time out waiting for the WebSocket return payload (while still successfully pushing the code to the device), which would otherwise switch the delivery method to `"sms"` and reject the trusted-device code at validation.

**pyicloud asks Apple for codes on its own, and must be stopped.** 2.6.5 added
`_request_2fa_code`, called from inside `authenticate()` — which `PyiCloudService` runs in its
**constructor**. It pushes to the trusted device and then, if the Apple ID has a trusted phone
number, PUTs `/verify/phone` with `"mode": "sms"`. It consults `_can_request_sms_2fa_code` for
neither.

The existing guard cannot reach it. `api._can_request_sms_2fa_code = lambda: False` is assigned
to the *instance* in `_validate_2fa_trusted_device`, long after the constructor has already
asked Apple for codes. So `_authenticate_icloud` calls `_disable_automatic_2fa_requests()`
first, patching the **class** before any instance exists.

Without it one re-authentication delivers a push, an SMS, and then our own push. Since every
fresh request invalidates the previous code, the code the user reads off their phone may
already be dead — which defeats the retry loop, whose entire reason for pushing on the first
attempt only is to avoid exactly that.

It does *not* call `_set_two_factor_delivery_state`, so it does not flip the delivery method to
`"sms"`; that separate hazard, which the instance guard exists for, still does not occur. Both
guards are needed and neither replaces the other.

`PYICLOUD_AUTO_2FA_METHOD` names the upstream method, and a canary test asserts it still exists
on the real `PyiCloudService`. A rename upstream would otherwise make the patch a silent no-op
and quietly restore the duplicate requests.

**The bridge may no longer time out.** 2.6.5 taught `hsa2_bridge.py` to accept Apple's newer
`flowid` in place of an echoed `sessionUUID`; on 2.5.0 that payload failed validation outright,
which is why the bridge so reliably failed and `validate_2fa_code()` fell back to the legacy
`_validate_trusted_device_code` endpoint. The reasoning recorded above about a raised
`request_2fa_code()` still holding a working code was written against that fallback. It has not
been re-verified against a live challenge on 2.6.5, and is not tracked as outstanding work: the
next expiry exercises it on its own. If a challenge ever delivers both a device push **and** an
SMS, the class patch in `_disable_automatic_2fa_requests` is not holding.

## Dependencies

- `pyfangs` (v0.7.3) — private library (`ssh://git@github.com/leandrorojas/pyfangs`): provides YamlHelper, FileSystem, terminal colors, Telegram (TelegramNotifier), and UTC conversion. The AI (GeminiAI) and DB (Postgres) modules are available as optional extras but not used here.
- `pyicloud` (2.6.5) — iCloud API (calendar service, HSA2 2FA via trusted-device bridge)
- `icalendar` — ICS file parsing
- `click` — used for interactive 2FA prompts. Declared directly in `pyproject.toml`: it used to arrive transitively via pyicloud, which dropped it in 2.6.5, so `import click` broke the moment that bump was attempted.

## Configuration

- `config.yaml` — `skip_days` are weekday integers as strings ("0"=Mon … "6"=Sun); `source-calendar-N` sections must have consecutive indexes matching `CALENDAR_URL_N` env vars
- `.env` — credentials and calendar URLs; templates at `config.yaml.template` and `.env.template`

## After Implementation

- Update `README.md` if the change affects usage, configuration, or setup instructions
- Update `CLAUDE.md` if the change affects architecture, dependencies, or conventions
- Update `BACKLOG.md` if the change closes a known gap, or if it uncovers one that is being
  deliberately deferred. It records the evidence behind each deferral so the decision can be
  re-examined instead of rediscovered.

## Conventions

- All datetime handling converts to UTC internally via `pyfangs.time.convert_to_utc`
- Telegram messaging is async with sync wrappers; supports both context-manager and plain TelegramNotifier instantiation
- No `Co-Authored-By` lines in commits
