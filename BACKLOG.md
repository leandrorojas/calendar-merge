# Backlog

Known gaps, deliberately unfixed. Each entry records what is wrong, the evidence
for it, and what a fix would actually take — so the decision to defer can be
re-examined rather than rediscovered.

Nothing here is a regression. Items are grouped by whether they change what the
program *does*, how it is *operated*, or how it is *verified*.

---

## Functional

### A pathological recurrence rule can stall a run

`_expand_recurrence` calls `rule.between()`, which iterates from `DTSTART` forward with no cap.
A high-frequency rule anchored far in the past therefore costs time proportional to the gap, not
to the window: measured at **1.7s for a single `FREQ=MINUTELY` event anchored 2023-01-01** against
a one-hour window. Daily or coarser rules are negligible, and the three live feeds contain none
finer than weekly.

**Deferred.** Any cap risks dropping legitimate occurrences, and the failure mode is a slow run
rather than a wrong one — the merge still produces correct output. Worth revisiting only if a
feed ever ships sub-hourly recurrence.

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

### A transient upstream outage sends one alert per run

Runs are scheduled every 15 minutes and hold no state between them, so an upstream outage
lasting longer than one interval sends one failure alert per run. The Apple 500s on
2026-08-18 produced three alerts for a single self-healing incident.

Suppressing until N consecutive failures needs state that survives across runs -- a marker
file or a counter -- which is the first persistent state this program would own. An in-run
retry is *not* the fix: that outage ran ~45 minutes against a 15-minute schedule, so a retry
measured in seconds would have failed anyway.

**Deferred** until it recurs. One incident is not enough to justify persistent state, and the
alerts were at least accurate -- the sync really did not happen.

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
