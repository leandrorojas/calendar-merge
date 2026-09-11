# Backlog

Known gaps, deliberately unfixed. Each entry records what is wrong, the evidence
for it, and what a fix would actually take — so the decision to defer can be
re-examined rather than rediscovered.

Nothing here is a regression. Items are grouped by whether they change what the
program *does*, how it is *operated*, or how it is *verified*.

---

## Functional

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

### Hidden-detail Google events are outside the exclusion rule

Both Google feeds are published at addresses that hide event details. Google reduces anything
not explicitly marked Public to `SUMMARY:Busy` with `TRANSP`, `CLASS` and `STATUS` removed.
Measured 2026-09-10, with nothing off-diagonal: `vf` 2063 stripped / 1499 detailed, `gmail`
10 / 1. The Outlook feed is full-detail by contrast -- `CLASS:PUBLIC` on all 170.

`_is_excluded_event` skips a Google event when `TRANSP` is present at all, so on the stripped
58% it has nothing to read and excludes nothing. A self-blocked "Focus ooo" therefore synced:
a biweekly Thursday 11:00-13:00 ART series, 53 occurrences through 2028-09-07, reported by the
user on 2026-09-10 and resolved the same day by marking that event Public.

**No code fix exists for the stripped events.** All 2063 are structurally identical -- one
`ATTENDEE` line, `X-NUM-GUESTS=0`, the same six or seven properties. Any rule that drops the
block drops every real meeting with it, and the title cannot help because the title is `Busy`.
Both properties `CLAUDE.md` cites as corroborating the Google split point the wrong way here:
`ATTENDEE` is present and `STATUS:CONFIRMED` is absent, which is the signature of a meeting.

**The rule reads whether details were published, not intent.** The two only agree because the
self-blocks are overwhelmingly the Public ones; a self-block that is not Public is exactly
where the rule fails. This has a second consequence worth stating: the 1499 detailed events on
`vf` are *all* excluded today, which is right for 1497 of them but would silently drop any real
meeting the user marks Public.

**Do not "fix" this by switching to the secret iCal address.** An earlier revision of this entry
recommended it. It would publish `TRANSP:OPAQUE` on real meetings and the Google branch would
then skip the *entire* feed -- the ≤ v0.1.5 failure that imported 0 of 158 Outlook events,
replayed on the largest source. Making a full-detail Google feed work needs a code change:
the Google branch would have to be retired in favour of a signal that survives full detail,
and nothing in the current data says what that signal would be.

**Deferred.** The source-side fix is per-event and costs nothing -- mark the block Public, and
the existing rule excludes it. That is the whole remedy until a feed's visibility changes.

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
