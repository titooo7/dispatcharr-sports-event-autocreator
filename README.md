# Sports Event Auto-Creator — Dispatcharr plugin

Auto-creates (and cleans up) event channels for sports — Boxing, MotoGP,
F1/NASCAR/IndyCar, Rugby, Tennis, Track & Field, Futsal, Euroleague basketball
and any other sport you configure — from EPG and stream-name searches, with
per-sport "jobs" edited directly in the Dispatcharr settings UI, a
configurable schedule, and an opt-in auto-DVR (Replays) feature.

Full plugin documentation (features, settings, jobs JSON format):
[`sports_event_autocreator/README.md`](sports_event_autocreator/README.md)

## Install

### Option A — add this plugin repository to Dispatcharr

Add this manifest URL as a plugin repository in Dispatcharr:

```
https://raw.githubusercontent.com/titooo7/dispatcharr-sports-event-autocreator/main/manifest.json
```

Then install **Sports Event Auto-Creator** from the plugin list. Updates are
detected automatically when a new version is published here.

### Option B — manual zip install

1. Download the latest zip from [`releases/`](releases/).
2. In Dispatcharr, go to **Plugins → Import Plugin** and upload the zip
   (or extract it into your `/data/plugins/` folder and restart).
3. Enable the plugin and open its settings.

## First steps after installing

The plugin ships with eight ready-made example jobs (from
`jobs.default.json`). They are working examples to adjust, not use blindly:

- Every job now defaults to searching **all** active EPG sources unless
  you've already ticked individual sources for it (see "EPG search", below)
  — review this per job if you'd rather scope a job to specific sources.
- Review each job's channel group, numbering and search terms — they reflect
  the author's setup.
- Use **Dry run** to preview what a job would create before enabling the
  schedule.

## Feature areas

### Event channel creation

Two search phases per job: an **EPG phase** (matches XMLTV/EPG-source
programme titles and, optionally, descriptions) and a **name phase**
(matches Dispatcharr stream names directly, inferring the event's timezone
from region tokens in the name/group/account). Matching uses one shared,
accent- and whitespace-folded dialect across both phases and the exclude
filter — "Fútbol" and "Futbol" match each other, and scraped XMLTV titles
with stray NBSP/zero-width characters compare cleanly. Details:
[`sports_event_autocreator/README.md`](sports_event_autocreator/README.md#matching).

### EPG search coverage

A per-job **"Search all EPG sources"** toggle searches every active source
in M3U & EPG Manager, so a job is never silently blind to a source you add
later. Existing jobs with individually-ticked sources keep exactly that
behavior on upgrade; only untouched jobs turn this on by default.

### Opt-in auto-DVR with reliability hardening

Selected event channels can be auto-recorded by Dispatcharr's own DVR.
Recording is **opt-in and per job** — set title patterns to record only what
you care about. This release's core focus is making the record/extend/
reschedule lifecycle solid:

- Recordings are identified by a **stable event identity** (the raw,
  un-formatted title + exact start time) instead of the cosmetic display
  name, so toggling country flags or region labels can no longer silently
  break dedup between two jobs or two runs.
- A pending recording whose real end time turns out later (a fuller EPG
  programme span discovered on a later run) is **extended in place**, not
  re-created — every such save is a plain, non-`update_fields` save, which
  matters: a partial-field save on an existing `Recording` can revoke its
  schedule in memory without persisting that change, silently orphaning it.
- An event that shifts time (postponement, provider re-listing) within a
  configurable tolerance window is **rescheduled** in place if still pending,
  or has its end time extended and a second recording created if it's
  already capturing.
- A per-run **concurrency cap** counts overlapping live/pending recordings
  in Python (not a database query), sidestepping any JSONField NULL-handling
  ambiguity entirely.
- One dedup **index snapshot per run**, not one table scan per candidate
  event, updated in place as the run makes decisions so two matches for the
  same event within one run still dedup correctly.
- User deletions are respected via **tombstones**, pruned once per run and
  hard-capped so the state file can't grow unbounded.
- All plugin state (tombstones, last-run status) now lives under
  `/data/plugins/.plugin_state/` instead of inside the plugin package, so it
  survives a plugin re-import/update instead of being wiped with it (with a
  one-shot migration from the old location).

Details:
[`sports_event_autocreator/README.md`](sports_event_autocreator/README.md#auto-dvr--replays).

### Retention

Auto-created recordings (files + rows) are pruned once older than a
configurable retention window; failed/zero-byte auto-recordings are always
cleaned up after a day. Manual (non-plugin) recordings are never touched.

### Teamarr watcher

An optional watcher auto-records Teamarr-generated event channels whose EPG
programme title matches your patterns, using the same event-identity and
reconciliation logic as the main jobs (so the two paths converge on the same
recording for the same real-world event instead of double-booking it).

### Black-screen filtering

EPG-phase candidate streams can be probed with `ffmpeg` and skipped if
they're a permanent black screen or fail to play at all — the next candidate
is tried automatically.

### The purge guard

A channel with an active or pending recording is never deleted mid-capture,
even when a job's purge rules would otherwise remove it; deletion is
deferred to a later run. File-deletion paths are hardened to a
normalized-path check under the real recordings root (rejecting `..`-escapes
and symlinked directories).

## Run-safety

Every task run gets a written status even if something in the orchestration
itself throws (a `crashed` status distinct from a normal per-job
misconfiguration `error`), and the last genuinely successful run's timestamp
is tracked and preserved across skips, dry runs and crashes — surfaced in the
plugin's "Show status" action.

## Versions

| Version | Date | Notes |
|---------|------|-------|
| 1.2.0 | 2026-08-22 | Recording reliability hardening: stable event identity independent of display formatting, in-place extend/reschedule (no `update_fields` traps), Python-side concurrency cap, per-run dedup index, durable plugin state directory, unified accent/whitespace-folded matching, per-job "search all EPG sources", crash-safe task with `last_success_at` tracking. |
| 1.1.10 | 2026-07-23 | Per-job toggle to disable Phase 2 name-based search. |
| 1.1.9 | 2026-07-21 | Fix `run_jobs` still unregistered on beat-scheduled runs. |
| 1.1.8 | 2026-07-20 | Remove the custom pidbox command entirely, not just its noise. |
| 1.1.7 | 2026-07-20 | Stop the residual `sea_refresh_strategies` log noise. |
| 1.1.6 | 2026-07-20 | Fix `run_jobs` unregistered on Dispatcharr ≥ 0.28.0's default queue. |
| 1.1.5 | 2026-07-19 | Timezone inference prioritizes the stream name over its group. |
| 1.1.4 | 2026-07-18 | Per-job auto-record duration for events without an EPG length. |
| 1.1.3 | 2026-07-18 | EPG-matched recordings use the real programme duration. |
| 1.1.2 | 2026-07-18 | Event-identity dedup, deletion tombstones, time-based Teamarr row selection. |
| 1.1.0 – 1.1.1 | 2026-07-17 | Opt-in auto-DVR (Replays) feature added, then recording fixes. |
| 1.0.0 | 2026-07-11 | First public release. |

## License

[MIT](LICENSE)
