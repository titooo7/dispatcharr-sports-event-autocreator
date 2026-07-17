# Sports Event Auto-Creator (Dispatcharr plugin)

Automatically creates (and cleans up) sports event channels in Dispatcharr,
replacing the standalone bash/python scripts (`boxeo.sh`, `motorbike.sh`,
`rugby.sh`, ...). Each former script becomes a **job**: its own search terms,
exclusions, target channel group, numbering and filters. Jobs run on a
schedule you choose, or on demand from the plugin page.

Unlike the CLI script, the plugin talks to Dispatcharr **internally** (Django
ORM) — no URL, username or password needed.

## Installation

1. Copy the whole `sports_event_autocreator/` folder to the server into the
   Dispatcharr data volume: `<data>/plugins/sports_event_autocreator/`
   (inside the container that is `/data/plugins/sports_event_autocreator/`).
   Alternatively zip the folder and use *Import Plugin* on the Plugins page.
2. Open **Plugins** in the Dispatcharr UI, hit the refresh icon, and **enable**
   the plugin (accept the trust warning).
3. **Restart the celery container once** after the first enable and after any
   plugin update (`docker restart dispatcharr_celery`) so the workers import
   the plugin's background task. The plugin then refreshes the workers'
   task-dispatch tables automatically at boot and before every queued run,
   and gracefully recycles prefork pool children forked before the plugin
   import (both work around Dispatcharr importing plugins at `worker_ready`,
   which is after
   celery builds its dispatch table AND after the pool forks its first
   children — otherwise runs are dropped with "Received unregistered task"
   or fail with `NotRegistered` inside a pool child, even though
   `inspect registered` lists the task).
4. On the plugin card, open **Settings**: every job has its own group of
   fields (search terms, group, numbering, filters). Set **Run every N
   minutes** (or a cron expression), press **Save**, then **Apply schedule**.
5. Optional: press **Validate configuration**, then **Dry run** and check the
   logs before letting it run for real.

## Global settings

| Setting | Meaning |
| --- | --- |
| Run every N minutes | Scheduled run frequency. `0` disables scheduling. |
| Cron expression | Optional 5-part cron (system timezone). Overrides the interval. E.g. `*/20 8-23 * * *` = every 20 min between 08:00–23:59. |
| Display timezone | Timezone for the `HH:MM, DD-MMM` prefixes in channel names (default `Europe/Madrid`). |
| Recording pre-roll padding (minutes) | Start each auto-recording this many minutes before the event start (default `5`). |
| Recording post-roll padding (minutes) | Keep recording this many minutes past the event's scheduled end (default `30`). |
| Replay retention (days) | Delete auto-created recordings older than this (files + rows); `0` disables age-based deletion (default `14`). Failed/zero-byte auto-recordings are always cleaned after 1 day. Manual recordings are never touched. |
| Max simultaneous recordings (0 = unlimited) | Caps how many recordings (any origin) may be airing at once (default `2`) — distinct events can overlap and the provider's concurrent-stream budget is finite. Duplicate feeds of one broadcast are already deduplicated by event identity, so this only gates different events. Dead rows (interrupted/failed/stopped/completed) don't occupy a slot. Extras beyond the cap are skipped and logged, not queued. |
| Teamarr watcher: channel groups / title patterns / exclude patterns | Optional watcher that auto-records Teamarr event channels (see *Auto-DVR / Replays* below). Empty groups or empty patterns = off. |
| Job name for 'Run one job' | Which job the *Run one job* button runs. |
| Job names | Comma-separated list of jobs; drives which per-job field groups exist. |

Changing the interval/cron requires pressing **Apply schedule** (or it will be
picked up at the *next* tick of the old schedule — the task self-heals).

## Jobs

Each job (one per sport, like the old shell scripts) is edited entirely in
the UI under its own `⚙ Job: <name>` heading. Multi-value inputs (search
terms, excludes, pin-to-top) take **one entry per line**. Field ↔ old CLI
flag mapping:

| Field | Old flag | Notes |
| --- | --- | --- |
| Enabled | — | Untick to skip the job without losing its settings. |
| Channel group | `--group` | Required. Created automatically if missing. |
| Search EPG source: … | — | One checkbox per active EPG source from **M3U & EPG Manager**; tick one or several and their programmes are searched together. Data is read from Dispatcharr's database (already fetched and parsed — no extra download) and carries the provider `tvg-id`s that match streams. Takes precedence over the XMLTV URL. In exported JSON this is the `"epg_sources"` list (the old `"epg_source"` string is still accepted on import). Newly added sources appear after **Reload job fields** + page refresh. |
| XMLTV URL | `--xmltv` | Optional. External XMLTV URL (or a file path under `/data`) for the EPG phase. Fetched once per run even if several jobs share it. |
| Search terms | `--search` | EPG phase matches whole words in title/description; name phase matches substrings in stream names. |
| Search programme descriptions | — | On by default. Untick to match search terms in EPG programme **titles only** — recommended for rich EPG sources whose descriptions cause false matches (films/series mentioning a sport). Exclude terms always check descriptions. |
| Exclude terms | `--exclude` | Whole-word exclusions. |
| Exclude stream-name prefixes (EPG matches) | — | One per line, e.g. `SKY:`, `PL:`. Candidate streams of an EPG match whose name starts with one of these are dropped (case-insensitive) and the next candidate is used. Name-search streams are unaffected. |
| Purge mode | `--purge-group` / `--purge-unmatched` | Full purge / unmatched-only / none. |
| Cleanup old/excluded | `--cleanup` | Delete channels matching excludes or older than *max past hours*. |
| Only unassigned streams | `--unassigned` | |
| Starting channel number | `--start-number` | 0 = none. |
| Preserve below | `--preserve-below` | 0 = off. With full purge: protects curated channels numbered below N. |
| Preserve above | — | 0 = off. With full purge: protects channels numbered above N (e.g. 24/7 channels appended at the end of the group). Can be combined with *Preserve below*. |
| Only today + upcoming / window | `--upcoming --days` | |
| Max past/future hours | `--max-past-hours` / `--max-future-hours` | 0 = off. |
| Country flag emojis | `--country-flags` | |
| Hide 🌍 region labels | `--no-region-label` | |
| Pin to top | `--pin-top` | |
| One channel per stream / max | `--split-streams --max-split` | |
| Require embedded date/time | `--require-time` | Needs an actual date (day+month or a weekday); a bare time like `8:10pm` is skipped, since it would be re-read as "today" forever. | |
| Skip black-screen streams (EPG matches) | — | Probes each candidate stream of an EPG match with ffmpeg; black screens and failed probes are skipped, failing over to the next candidate. Name-search matches are never probed. See *Black-screen filtering* below. |
| Auto-record: title patterns | — | Opt-in auto-DVR. Record an event channel only when its title matches at least one term (whole-word, same syntax as *Search terms*). Empty = record nothing. One per line. See *Auto-DVR / Replays* below. |
| Auto-record: exclude patterns | — | Titles matching any of these are never recorded, even if they match a record pattern. One per line. |

### Sharing configuration (import / export)

Both directions go through the **Jobs JSON (import / export box)** field in
Settings (the plugin UI cannot trigger browser downloads or input dialogs —
the box is the closest equivalent):

- To **export**: press **Export jobs JSON**, refresh the page (without
  pressing Save first), open Settings and copy the JSON from the box. The
  same JSON is also written to
  `data/plugins/sports_event_autocreator/jobs.export.json` (shown in the
  action's `Output:` line).
- To **import**: paste the JSON array into the box, press **Save**, press
  **Import jobs JSON** (confirm), then refresh the page **without pressing
  Save again** (the still-open form holds the old values and would overwrite
  the import). Global settings (schedule, timezone) are kept; all job
  settings are replaced, and the box is cleared.
- An exported file also works as a drop-in replacement for
  `jobs.default.json` (the defaults for fresh installs).

### Adding / removing jobs

1. Edit **Job names** (e.g. append `, tenis`) and press **Save**.
2. Press **Reload job fields**, then refresh the browser page.
3. The new job's fields appear (empty defaults) — fill them in and **Save**.

Removing a name hides its fields but keeps the saved values, so re-adding
the same name restores them.

The plugin ships pre-configured with eight ready-made jobs (Track & Field,
Boxing, Basketball Euroleague, Futsal, Motorbikes, Auto Racing, Rugby and
Tennis — from `jobs.default.json`). They are meant as working examples to
adjust, not use blindly: they ship with no EPG sources selected (tick your
own sources in each job's settings), and the channel numbering, groups and
search terms reflect the author's setup — review them on a fresh install
before enabling the schedule.

## Actions

- **Run now** — queues an immediate run of all enabled jobs (also re-applies
  the schedule). Results arrive as a notification and in the logs.
- **Run one job** — same, but only the job named in *Job name*.
- **Dry run** — full preview run, nothing is created or deleted; per-channel
  decisions are written to the logs (`docker logs <container> | grep plugins`).
- **Validate configuration** — checks all job settings and reports problems
  instantly.
- **Reload job fields** — rebuilds the per-job field groups after editing
  *Job names* (refresh the page afterwards).
- **Apply schedule** — writes the interval/cron into the Celery beat schedule.

## EPG assignment

With **Assign EPG to created channels** enabled (default), every created
channel gets guide data:

- **EPG-search channels** are linked to the matched source's real `EPGData`
  row — full guide, kept fresh by that source's normal refreshes. The run log
  also states which source each event was found in
  (`[EPG] '…' @ 20:00 08-Jul — found in: EPG Spain, EPG UK-USA`).
- **Name-search channels** with a reliably parsed time get a single generated
  guide entry (event title, parsed start time, configurable duration) stored
  under a plugin-owned EPG source named **"Sports Event Auto-Creator"**. That
  source appears in M3U & EPG Manager as *inactive* — leave it that way; being
  inactive is what keeps Dispatcharr's EPG refresh from touching it. The same
  fallback applies to EPG-search hits whose feed isn't ingested in EPG Manager
  (external XMLTV URL only).
- Channels whose time couldn't be reliably parsed keep `epg_data` unset, so
  Dispatcharr's standard placeholder dummy EPG applies.
- Guide entries of deleted event channels are cleaned up automatically at the
  end of each run. EPG assignment failures never fail a run — they're logged
  as `[EPG-ASSIGN]` errors.

With **Use stream logo for created channels** enabled (default), each created
channel also takes the logo of its first stream that has one (same mechanism
as Dispatcharr's M3U auto channel sync).

## How EPG matches become channels

A matched programme only becomes a channel if streams can be attached to it.
Streams are found via the programme's EPG channel id (`tvg-id`): a stream
matches if its own `tvg-id` equals it, if a channel whose `tvg_id` field
equals it holds the stream, or if a channel with that **EPG assigned in the
UI** holds the stream. When nothing attaches, the run log states why per
event: `no streams are linked to its EPG id(s)` (no stream/channel carries
that id) or `all N candidate streams dropped` (e.g. they belong to curated
channels and the job uses *Only unassigned streams* — untick it if you want
event channels built from your curated channels' streams).

## Black-screen filtering (EPG matches)

EPG-matched events sometimes attach a stream whose picture is a permanent
black screen. With the per-job **Skip black-screen streams (EPG matches)**
toggle on, each candidate stream of an event is probed with ffmpeg before the
channel is built, and provable black screens are skipped so the next candidate
stream is used instead (in bundle mode confirmed-good streams simply lead the
failover order; in split mode black streams are dropped from the per-event
channels).

Details and caveats:

- **All EPG matches are probed, whether or not the event has started.**
  EPG-matched streams are regular 24/7 channels (attached via `tvg-id`), so a
  black screen means the stream is broken even before the event begins. This
  is different from name-search (Phase 2) event-slot streams, which ARE
  legitimately black until their event starts — those are never probed.
- **Detection.** ffmpeg samples a few seconds of the stream (global
  **Black-screen probe seconds per stream**, default 5, clamped 3–30) with the
  `signalstats` filter and averages the per-frame luma (YAVG). A mean YAVG
  below 20 (YUV TV range: 16 = pure black, real content > 40) is treated as
  black. ffmpeg is bundled in the Dispatcharr image.
- **A probed stream must prove it plays.** Both provable black screens and
  failed probes (HTTP error, timeout, no decodable video) are rejected — the
  log states the cause (`→ probe failed, skipped — ffmpeg exit 1: <error>`).
  Only streams the run *could not* probe at all (ffmpeg unavailable, probe
  budget exhausted) are kept, and never ahead of a confirmed-good stream.
  If every candidate fails, the event is skipped and no channel is created.
- **Each probe briefly opens one provider connection** (a short ffmpeg pull of
  the stream URL). Verdicts are cached per stream for the whole run and the
  number of probes per run is capped, so shared streams are probed once.
- Probing also runs during **Dry run** (it is read-only) so you can preview
  verdicts. Every decision is logged with the `[BLACK-CHECK]` tag, e.g.
  `[BLACK-CHECK] 'Team A vs Team B': stream 'ES: … 20:00' → BLACK (YAVG 16.2) — skipped`.

## Auto-DVR / Replays

Selected event channels can be **auto-recorded** by Dispatcharr's built-in DVR,
so finished events become a replay library. Recording is **strictly opt-in and
selective** — with dozens of event channels created per day, nothing is recorded
unless you ask for it:

- Set **Auto-record: title patterns** on a job to record only matching events
  (e.g. a boxing job that records only `Canelo`, `Usyk`). The patterns use the
  same whole-word syntax as *Search terms*; **Auto-record: exclude patterns**
  vetoes a match. An empty pattern list records nothing.
- The filter is evaluated for **every** matched event channel of the job —
  newly created ones *and* channels that already exist (so adding a pattern for
  an existing channel starts recording it on the next run).
- Each recording is padded by the global **pre-roll**/**post-roll** minutes and
  tagged as plugin-owned (`custom_properties.auto_dvr = true`). The plugin only
  creates the `Recording` row; Dispatcharr's own signal schedules the ffmpeg
  job. A start time already in the past but before the end still records the
  remainder.
- **De-duplication** keys on the **event's identity** (normalized title +
  `event_start`), checked across *all* auto-recordings regardless of channel —
  so duplicate provider feeds of one broadcast produce a single recording,
  purge/recreate cycles (which give channels fresh ids every run) don't
  double-book, and changing the padding never does either.
- **User deletions stick**: if you delete an auto-created recording, the
  plugin remembers (a tombstone in `auto_dvr_state.json` next to the plugin
  code) and will not re-create a recording for that same event — even while
  the event is still airing.
- Name-search events whose timezone couldn't be reliably inferred are **not**
  recorded (a guessed start time would schedule the recording wrong).

**Teamarr watcher** (optional): set **Teamarr watcher: channel groups** to the
group(s) holding Teamarr event channels (they carry a `teamarr-event-` tvg-id
prefix) plus **Teamarr watcher: title patterns** (and optional excludes). Each
run reads those channels' EPG programme times and records matching, not-yet-past
programmes — same opt-in rule, padding, dedup and tagging as jobs
(`source: "teamarr-watch"`). Patterns are matched against **both** the
programme title and the channel's own name (Teamarr names channels
`HH:MM - Team A - Team B`) — the live match itself is often titled
generically (e.g. "Brasileirao - Soccer", no team names). Which EPG row is
the live broadcast is decided **by time**: the row whose span covers the
`HH:MM` embedded in the channel name (display timezone) is the match; the
pre-game "coming up" and post-game "recap" filler rows around it are never
recorded. If a channel name carries no parseable time, the watcher falls
back to skipping rows by Teamarr's Spanish filler markers ("A continuación"
prefix / "Resumen").

**Retention** keeps the disk in check: recordings tagged `auto_dvr` older than
**Replay retention (days)** are deleted (media files first, then the DB row,
pruning now-empty folders), and failed/zero-byte auto-recordings are cleaned up
after 1 day. Manual (untagged) recordings are **never** touched. Set the
retention to `0` to disable age-based deletion.

A **purge guard** protects in-flight recordings: the normal channel cleanup
will not delete an event channel that has a recording currently in progress or
scheduled/ending in the future — it defers that deletion to a later run.

## Behavior notes

- Disabling the plugin switches the schedule off; deleting it removes the
  schedule; plain reloads (refresh icon / plugin updates) leave the schedule
  untouched. Scheduled runs also no-op if the plugin is disabled. After
  re-enabling, press **Apply schedule** (or **Run now**) to switch the
  schedule back on.
- **Show status** reports the beat schedule state, when it last dispatched,
  and the last actual run result — use it first whenever "nothing happens".
- Deletion safety is the same as the script: `purge_group` respects
  `preserve_below`; `purge_unmatched` only deletes channels whose streams are
  also unmatched, or whose streams were re-used by a newer event.
- Files: `plugin.py` (UI glue), `engine.py` (parsing/naming logic),
  `runner.py` (job execution via ORM), `tasks.py` (Celery task + schedule),
  `jobs.default.json` (default jobs).
