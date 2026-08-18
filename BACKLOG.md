# Backlog

Known gaps, deliberately unfixed. Each entry records what is wrong, the evidence
for it, and what a fix would actually take — so the decision to defer can be
re-examined rather than rediscovered.

Nothing here is a regression. Items are grouped by whether they change what the
program *does*, how it is *operated*, or how it is *verified*.

---

## Functional

### Recurring events are not expanded

`_parse_source_events` (`src/merge.py:820`) iterates `ics_calendar.walk(ICS_TAG_VEVENT)`,
which yields the **series master**, not its occurrences. A repeating meeting therefore
contributes at most its original `DTSTART`.

Outlook anchors a series at the date it was first created, so a weekly meeting set up
last year has a `DTSTART` far outside any forward-looking window and contributes
**nothing at all**.

**Measured impact:** roughly 6 of ~18 weekly occurrences reach iCloud on the Outlook feed.
This is the largest known functional gap in the product.

**A fix needs:** RRULE expansion, plus `EXDATE` and `RECURRENCE-ID` override handling.
Skipping the override handling would resurrect cancelled and moved occurrences, which is
worse than the current under-reporting — the current failure omits busy time, that one
would invent it.

Already described in `CLAUDE.md` under *Recurring events are not expanded*.

### The date window follows the host timezone

`src/merge.py:984` computes the window from `datetime.now().astimezone()`, so "today"
is whatever the machine running the merge thinks it is. Move the host across a timezone
and the window silently shifts by a day.

The rest of the codebase is disciplined about this — every datetime converts to UTC via
`pyfangs.time.convert_to_utc` — so this one call is the sole coupling to local time.

**Deliberately deferred.** The host has not moved and the behaviour is arguably what a
person wants: a calendar day should mean the day *where the user is*. Fixing it means
choosing an explicit reference timezone and threading it through `_calculate_future_date`
and `_end_of_day`, which is only worth doing if the merge ever runs somewhere else.

---

## Operational

### Dependabot security alerts are disabled

Dependabot **version** updates work (repaired 2026-08-18, PR #74). Dependabot
**security** alerts are off, so no CVE notification arrives for a vulnerable dependency
on a public repository.

**Action:** repository Settings → Code security → enable *Dependabot alerts*. One
toggle, requires repo admin. Cannot be done from the API with the current token.

### The ruff pin drifts on every ruff bump

The ruff version is declared in three places:

| Location | Form | Dependabot sees it |
|---|---|---|
| `pyproject.toml` / `uv.lock` | dependency | ✅ |
| `.github/workflows/ci.yml` (×3) | string literal | ❌ |
| `.pre-commit-config.yaml` | `rev:` | ❌ |

The `uv` ecosystem updates only the first, so every ruff bump leaves CI linting with the
previous version — exactly the drift the exact pin exists to prevent. It happened on the
0.15.10 → 0.16.3 bump and was corrected by hand in PR #80.

**A fix would** have the lint job derive the version from `uv.lock` rather than repeating
it, or add a check that fails when the three disagree. Until then this recurs silently
on every bump and must be caught by inspection.

---

## Verification

### CI does not check the packaging layout

The test suite imports `merge` through pytest's `pythonpath = ["src"]`, which resolves
the module from the **source tree** and never from the built artifact. 336 tests at 100%
coverage are therefore blind to whether the shipped program can start at all.

This is not hypothetical: it let a broken wheel ship from at least v0.1.8 until v0.1.9,
where `from merge import main` raised `ModuleNotFoundError` on every installed copy
(fixed in PR #82).

**A fix would** add a CI step that builds the wheel and asserts `merge.py` sits at its
root — a few seconds of build time to guard a failure mode the entire test suite cannot
observe.

### The 2FA flow has never been verified end to end against Apple

The 2FA retry loop, the six-digit filter, the Telegram acceptance message, and the
session-trust-on-failure path are all covered by unit tests against a faked pyicloud.
None has been exercised against a live Apple challenge since being written.

The fakes encode behaviour read from the pyicloud source — including the trusted-device
bridge posting step0 before the wait that times out — but a fake that agrees with a
mistaken reading proves nothing. That reading has been wrong before.

**Deliberately deferred:** verifying it means triggering a real 2FA challenge, which
cannot be done on demand without disturbing a working session.

---

## Code health

### `_reconcile_events` exceeds the cognitive-complexity limit

`src/merge.py:675` measures **16** against SonarCloud's limit of 15 (confirmed with
`complexipy`, which matches Sonar's calculation). Pre-existing and untouched by recent
work. The next three offenders sit below the limit: `_parse_source_events` 13,
`_sync_events_to_icloud` 12, `_validate_2fa_trusted_device` 10.

**Deliberately deferred.** It is the reconciliation core, it is fully covered, and its
behaviour has been pinned by mutation testing. Splitting it to satisfy a threshold by one
point risks a real regression to remove a cosmetic warning.

---

## Decided, not pending

Recorded so they are not repeatedly rediscovered as bugs:

- **Two Outlook events (`SALIDA NIÑES`, `TERAPIA`) sync although they are personal
  blocks.** They are marked `BUSY` and are indistinguishable from meetings. Excluding
  them would need title matching, which was considered and rejected.
- **`TENTATIVE` Outlook events are kept.** Google feeds strip `PARTSTAT` entirely, so a
  "maybe" there already syncs. Keeping `TENTATIVE` makes both providers behave the same.
- **Event exclusion is provider-dependent and must not be unified.** Both universal rules
  have already shipped and broken a feed. See the table in `CLAUDE.md`.
