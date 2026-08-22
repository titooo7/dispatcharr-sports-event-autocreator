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
3. **Restart both the web AND celery containers once** after the first
   enable and after any plugin update (`docker restart dispatcharr_web
   dispatcharr_celery`) — restarting only one is not enough:
   - `celery` needs it so the `dvr`-queue worker (see below) imports the
     plugin's background task.
   - `web` needs it too: the *"Run now"* / *"Dry run"* / *"Run one job"*
     buttons queue the task from inside the web container's own long-lived
     Django process, which only re-imports `tasks.py` on restart. Update
     the files on disk without restarting web and those buttons keep
     building messages from the stale, pre-update task definition —
     `celery inspect registered` and the celery logs can look completely
     healthy while this is happening, since the problem is entirely on the
     sending side. If a run still fails with "Received unregistered task"
     right after an update despite celery having been restarted, this is
     almost always why.
   The plugin's background task itself runs on the `dvr` queue specifically
   (not the default `celery` queue) — a `--pool=threads` worker, which does
   not have the prefork parent/child import gap that the default queue's
   `--pool=prefork` worker has on Dispatcharr ≥ 0.28.0 (see the comment
   above `run_jobs_task`'s `@shared_task` decorator in `tasks.py` for the
   full story).
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
| Max simultaneous recordings (0 = unlimited) | Caps how many recordings (any origin) may be airing at once (default `2`) — distinct events can overlap and the provider's concurrent-stream budget is finite. Duplicate feeds of one broadcast are already deduplicated by event identity, so this only gates different events. Dead rows (interrupted/failed/stopped/completed) don't occupy a slot. Extras beyond the cap are skipped, logged, and counted in the run summary (`N capped`) — retried automatically on the next run. |
| Teamarr watcher: time-shift tolerance (hours, 0 = disable) | Used by the Teamarr watcher only — each job has its own separate setting of the same name for the keyword-EPG path (see *Auto-DVR / Replays* below), not a fallback/inheritance relationship. Default `3`. |
| Teamarr watcher: max extension (hours, 0 = uncapped) | Used by the Teamarr watcher only — same relationship to the per-job setting as above. Default `2`. |
| Teamarr watcher: channel groups / title patterns / exclude patterns | Optional watcher that auto-records Teamarr event channels (see *Auto-DVR / Replays* below). Empty groups or empty patterns = off. |
| Black-screen probe budget (per run) | Total ffmpeg probes allowed per run (default `80`), split **evenly as a guaranteed reserve** across every job with *Skip black-screen streams* enabled, with any leftover pooled as first-come shared surplus. See *Black-screen filtering* below. |
| EPG link corroboration tolerance (minutes) | How close a real programme's start time must be to a Phase 2 (name-search) event's parsed time to be trusted as a match (default `90`). See *EPG assignment* below. |
| Job name for 'Run one job' | Which job the *Run one job* button runs (also used by *Adopt channels*). |
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
| Search all EPG sources | — | When on, searches every currently-active EPG source automatically and ignores the checkboxes below entirely — so a source added *after* this job was last configured is covered without you having to come back and tick it. Defaults to **off** for any job that already has its per-source checkboxes saved (keeps exactly what's configured, no surprise widening), and **on** for a job that's never had them saved. |
| Search EPG source: … | — | One checkbox per active EPG source from **M3U & EPG Manager**; tick one or several and their programmes are searched together. Ignored while *Search all EPG sources* (above) is on. Data is read from Dispatcharr's database (already fetched and parsed — no extra download) and carries the provider `tvg-id`s that match streams. Takes precedence over the XMLTV URL. In exported JSON this is the `"epg_sources"` list (the old `"epg_source"` string is still accepted on import). Newly added sources appear after **Reload job fields** + page refresh. |
| XMLTV URL | `--xmltv` | Optional. External XMLTV URL (or a file path under `/data`) for the EPG phase. Fetched once per run even if several jobs share it. |
| Search terms | `--search` | EPG phase matches whole words in title/description; name phase matches substrings in stream names. |
| Also use these terms for name-based search (Phase 2) | — | On by default. Untick to keep this job's EPG phase as-is and skip Phase 2 (stream-name search) entirely — useful when another tool (e.g. Teamarr) already covers name-search event creation better for a given sport. |
| Search programme descriptions | — | On by default. Untick to match search terms in EPG programme **titles only** — recommended for rich EPG sources whose descriptions cause false matches (films/series mentioning a sport). Exclude terms always check descriptions. |
| Exclude terms | `--exclude` | Whole-word exclusions. |
| Exclude stream-name prefixes (EPG matches) | — | One per line, e.g. `SKY:`, `PL:`. Candidate streams of an EPG match whose name starts with one of these are dropped (case-insensitive) and the next candidate is used. Name-search streams are unaffected. |
| Purge mode | `--purge-group` / `--purge-unmatched` | Full purge / unmatched-only / none. |
| Cleanup old/excluded | `--cleanup` | Delete channels matching excludes or older than *max past hours*. |
| Only unassigned streams | `--unassigned` | |
| Starting channel number | `--start-number` | 0 = none. |
| Ending channel number | — | 0 = unbounded/implied. See *Channel numbering and ranges* below. |
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
| Update matching channels in place | — | **Off by default.** Phase 1 (EPG) only. See *Channel ownership* below. |
| Update in place: time-shift tolerance (hours) | — | Only used when the above is on. Default `3`. |

### Sharing configuration (import / export)

Both directions go through the **Jobs JSON (import / export box)** field in
Settings (the plugin UI cannot trigger browser downloads or input dialogs —
the box is the closest equivalent):

- To **export**: press **Export jobs JSON**, refresh the page (without
  pressing Save first), open Settings and copy the JSON from the box. The
  same JSON is also written to
  `data/plugins/.plugin_state/sports_event_autocreator/jobs.export.json`
  (shown in the action's `Output:` line) — this directory survives a plugin
  update (see *Auto-DVR / Replays* below), unlike the plugin's own code
  directory.
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
- **Name-search channels whose stream carries a `tvg-id`** now also attempt a
  **corroborated** real-EPG link before falling back to a generated entry: the
  resolved `EPGData` must be an *active*, non-plugin-synthetic source, **and**
  it must have an actual programme within **EPG link corroboration tolerance
  (minutes)** (global setting, default `90`) of the parsed event time **and**
  that programme's title must also match. Either signal alone is not enough —
  a provider's self-reported `tvg-id` on a name-search stream is otherwise
  likely to be its generic 24/7 channel id, which would attach a totally
  unrelated full-day guide purely because the times happen to line up. When
  the link isn't corroborated, the run log states why
  (`[EPG-ASSIGN] Not linking real EPG for '…' (tvg_id '…'): time matched but
  title did not corroborate (programme title: '…')`) and the channel falls
  back to the generated single-event entry below.
- **Name-search channels** with a reliably parsed time (and no corroborated
  real-EPG link) get a single generated guide entry (event title, parsed
  start time, configurable duration) stored under a plugin-owned EPG source
  named **"Sports Event Auto-Creator"**. That source appears in M3U & EPG
  Manager as *inactive* — leave it that way; being inactive is what keeps
  Dispatcharr's EPG refresh from touching it. The same fallback applies to
  EPG-search hits whose feed isn't ingested in EPG Manager (external XMLTV
  URL only).
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
  the stream URL). Verdicts are cached per stream for the whole run, so shared
  streams are probed once.
- **Probe budget is per-job, not a single shared pool spent job-by-job.** The
  global **Black-screen probe budget (per run)** (default `80`) is split
  *evenly as a guaranteed reserve* across every job that has this toggle on —
  e.g. 80 across 8 such jobs guarantees 10 probes each — with any leftover
  from the division pooled as first-come shared surplus any job can draw
  from once its own reserve is spent. Before this, the budget was a single
  flat pool consumed in job order, so the *last* job(s) in a run could see it
  already exhausted by earlier jobs and get zero probes even with plenty of
  candidates left; each job now gets its own guaranteed share regardless of
  run order. Once a job's own reserve (plus any remaining surplus) is spent,
  its remaining candidates for that job are kept unprobed — logged once per
  job (`[BLACK-CHECK] Probe budget exhausted for this job — remaining
  candidates kept unprobed`), never once per candidate. The run summary
  reports the totals (`N black-screen probe(s) (M capped)`).
- Probing also runs during **Dry run** (it is read-only) so you can preview
  verdicts. Every decision is logged with the `[BLACK-CHECK]` tag, e.g.
  `[BLACK-CHECK] 'Team A vs Team B': stream 'ES: … 20:00' → BLACK (YAVG 16.2) — skipped`.

## Channel numbering and ranges

Every job's channel numbers are drawn from one **instance-wide** pool (a
number in use anywhere — any `Channel`, and any `ChannelOverride` — is off
limits, mirroring how Dispatcharr itself reserves numbers), so two jobs can
never hand out the same number even if their ranges were misconfigured to
overlap.

- **Ending channel number** (0 = unbounded/implied) caps a job's own range.
  Leave it at 0 and the job's range is **implied**: it runs from its own
  *Starting channel number* up to just below the next-higher **enabled**
  job's starting number — so a "Track & Field" job starting at 260 with no
  end, and a "Basketball Euroleague" job starting at 270, gives Track & Field
  an implied range of 260–269 automatically, with **zero configuration
  changes needed**. The job with the highest starting number (and no end set)
  is unbounded. Disabled jobs never consume or imply a range.
- **When a job's range runs out**, it does not silently overflow into a
  neighboring job's numbers and it does not skip creating the channel either:
  the channel is created with **no channel number** (visible in Dispatcharr,
  self-healing the moment a number frees up on a later run), a `WARNING` is
  logged, and it's counted in the run summary as `N unnumbered`. If you see
  this, either raise the job's *Ending channel number* / the next job's
  *Starting channel number*, or accept that the group has more events than
  numbered slots on a given day.
- **A skipped-over occupied number** (one job's range containing a number
  some other channel already holds) is logged at `INFO` and counted as
  `N number conflict(s) resolved` — the allocator just moves on to the next
  free number, this is informational, not a problem needing action.
- **Validate configuration** (and the run summary, once per run) warns about,
  but never blocks on: two enabled jobs with overlapping *explicit* ranges,
  a job whose *implied* range is suspiciously narrow (under 20 numbers), two
  enabled jobs sharing the same channel group, and *Full purge* with neither
  *Preserve below* nor *Preserve above* set at all. These are warnings, not
  errors — an existing working config never starts failing runs because of a
  newly-added check.

## Channel ownership

Historically, a job's cleanup/purge pass only knew about *channel numbers*
and *names* — it had no concept of *whose* channel something was. Two jobs
sharing one channel group, or a manually-added channel dropped into a job's
group, could get deleted by a purge pass that never meant to touch it.

- Every channel the plugin creates (or, from v1.3.0 on, updates in place) is
  recorded in a durable **channel registry**
  (`data/plugins/.plugin_state/sports_event_autocreator/channel_registry.json`,
  the same durable-across-updates location as the DVR tombstones) mapping
  channel id → owning job, group, number, identity and stream ids. Dispatcharr's
  `Channel` model has no field the plugin could tag this onto directly, hence
  the separate file.
- **Purge/cleanup now checks ownership first**, before any of today's
  purge_group/cleanup/purge_unmatched rules: a channel with **no** registry
  entry is always left alone (protects manual channels, and — pre-adoption —
  everything the plugin already made); a channel owned by **another
  currently-configured job** (enabled or disabled) is always left alone too,
  even if that job's own rules would otherwise have deleted it; only a
  channel owned by **this** job is evaluated by today's rules as before.
- **One-shot adoption, automatic on upgrade**: the first time a job's target
  group has zero registry entries, every existing channel in that group is
  checked against a heuristic — a channel is adopted into that job if its
  EPG-assigned `tvg-id` matches the plugin's own synthetic pattern
  (`sea-ch-<id>`), if its name matches the plugin's generated
  `HH:MM, D-Mon | Title` naming pattern, or if it shares a stream id with
  something the job prepared this run. Anything matching none of those is
  left unowned (protects real manual channels) and counted as
  `N unowned kept`. The pass logs its result loudly
  (`[OWNERSHIP] Adopted N channel(s) into job 'X'`) and runs at most once per
  group per run, coordinated across jobs that share a group.
- **A channel owned by a job whose name has since been removed from the
  config** is automatically re-adopted into whichever currently-configured
  job now touches that group (self-correcting — orphaned ownership is never
  left stranded), counted as `N adopted`.
- **Adopt channels (job's group)** action: force-runs the same heuristic
  against the job named in *Job name for 'Run one job'*, even if its group
  already has ownership tracked — use this if the automatic pass under-adopted
  something in a group.
- **Update matching channels in place** (per-job, off by default, Phase 1/EPG
  only): when on, an EPG event that already has an owned channel — matched by
  exact identity, or by the same title within *Update in place: time-shift
  tolerance (hours)* (default `3`) of the owned channel's last-known time —
  has its streams, name and EPG link updated **in the same channel**, instead
  of being deleted and recreated. The channel number is **never** changed by
  this. If the channel has an active or scheduled recording, the stream swap
  is deferred to a later run (name/EPG still update immediately) and counted
  as `N stream-swap deferred`; successful updates count as `N updated in
  place`. Split-mode channels of one event are disambiguated by exact
  stream-set match, then any shared stream id, then a persisted slot index —
  never by name alone (several split channels of one event can share an
  identical display name). Phase 2 (name-search) is unaffected by this
  setting and always uses the existing delete/recreate behavior — its
  identity is built from far less stable provider-authored stream names,
  too risky to match against safely yet.

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
- **Recording length follows the EPG**: for EPG-matched events the recording
  covers the programme's real start→end span (plus the pads). Events whose
  duration is unknown — name-search events, or XMLTV rows without a `stop`
  time — use the per-job **Auto-record: duration (hours)** if set (you know
  each sport's typical length: a rally stage ~1.5h, a boxing card ~4h),
  otherwise the global *Generated event programme duration*. Priority:
  real EPG span > per-job duration > global default. (Teamarr-watcher
  recordings already used the EPG span.)
- **Recordings extend when a game runs long**: if a later run sees a
  longer real EPG span for a recording already created (extra time, a
  rain delay, a broadcast running over), its `end_time` is extended —
  whether the recording is still pending or already capturing (Dispatcharr's
  own recording task re-reads `end_time` from the database every ~2 seconds
  and adjusts its stop point live). Capped at the per-job **Auto-record:
  max extension (hours)** past the *original* end (default `2`, `0` =
  uncapped), so a bad EPG value can't schedule a runaway-length capture.
- **A broadcaster moving an event's start time reschedules the existing
  recording** instead of creating a duplicate or missing it — as long as the
  shift is within the per-job **Auto-record: time-shift tolerance (hours)**
  (default `3`, `0` disables this and reverts to the old exact-time-match
  behavior, where a shifted event creates a second recording). If the
  original recording is already capturing by the time the shift is noticed,
  it is never interrupted — it's left running (and extended if the new EPG
  end is later), and a second recording is created for the new time as a
  backup, logged clearly since this uses two concurrency-cap slots for one
  event.
- **De-duplication** keys on the event's identity — the **raw, underlying**
  title (not the formatted display name) plus `event_start` — checked across
  *all* auto-recordings regardless of channel, so duplicate provider feeds of
  one broadcast produce a single recording, purge/recreate cycles (which
  give channels fresh ids every run) don't double-book, changing the padding
  never does either, and per-job display toggles (*country flags*, *no
  region label*) can't accidentally break dedup between two jobs or two runs
  of the same job, since they only affect the cosmetic name, never the
  identity used here.
- **User deletions stick**: if you delete an auto-created recording, the
  plugin remembers (a tombstone in
  `data/plugins/.plugin_state/sports_event_autocreator/auto_dvr_state.json`,
  a location the plugin-update process never touches, unlike the plugin's
  own code directory) and will not re-create a recording for that same
  event — even while the event is still airing.

The job-creation task itself runs on the `dvr` queue (not the default
`celery` queue) — a deliberate choice, not a config default. Dispatcharr's
default queue is served by a `--pool=prefork --autoscale` worker; on
Dispatcharr ≥ 0.28.0 that worker's arbiter process never imports plugin
modules at all (only its forked children do), so a task routed there is
permanently unable to register — every "Run now" or scheduled run fails
with "Received unregistered task". The `dvr` queue's `--pool=threads`
worker has no such split. See the comment above `run_jobs_task`'s
`@shared_task` decorator in `tasks.py` for the full story.
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
  the last actual run result, and — separately — when the plugin last
  *actually succeeded* (a skipped/overlapping run or a dry run never
  overwrites that, so it stays trustworthy even after one). A run that
  crashes outside the normal per-job error handling is reported as its own
  distinct status rather than silently leaving the previous result showing.
  The run summary also reports how many EPG programmes were scanned and
  matched, so a dead/stale EPG source (0 scanned) doesn't look the same as
  a genuinely quiet week (thousands scanned, still 0 matches) — use it
  first whenever "nothing happens".
- Deletion safety is the same as the script: `purge_group` respects
  `preserve_below`; `purge_unmatched` only deletes channels whose streams are
  also unmatched, or whose streams were re-used by a newer event — and, from
  v1.3.0 on, ownership (see *Channel ownership* above) is checked before any
  of that: a channel not owned by the current job is never touched by it.
- Files: `plugin.py` (UI glue), `engine.py` (parsing/naming logic),
  `runner.py` (job execution via ORM), `tasks.py` (Celery task + schedule),
  `jobs.default.json` (default jobs). State files live under
  `data/plugins/.plugin_state/sports_event_autocreator/`: `auto_dvr_state.json`
  (DVR tombstones), `last_run.json`, and `channel_registry.json` (channel
  ownership, v1.3.0+) — this directory survives a plugin update, unlike the
  plugin's own code directory.
