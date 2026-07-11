# Sports Event Auto-Creator — Dispatcharr plugin

Auto-creates (and cleans up) event channels for sports — Boxing, MotoGP,
F1/NASCAR/IndyCar, Rugby, Tennis, Track & Field, Futsal, Euroleague basketball
and any other sport you configure — from EPG and stream-name searches, with
per-sport "jobs" edited directly in the Dispatcharr settings UI and a
configurable schedule.

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

- No EPG sources are pre-selected — tick your own sources in each job's
  settings (one checkbox per source in your M3U & EPG Manager).
- Review each job's channel group, numbering and search terms — they reflect
  the author's setup.
- Use **Dry run** to preview what a job would create before enabling the
  schedule.

## Versions

| Version | Date | Notes |
|---------|------|-------|
| 1.0.0 | 2026-07-11 | First public release. |

## License

[MIT](LICENSE)
