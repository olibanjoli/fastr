# fastr

Pulls your Garmin history and your Runna plan into one local store, then builds a
report that answers: *how is training going, and am I on course for my goals?*

Everything stays on your machine under `data/` (git-ignored): Garmin session
tokens, a SQLite database, the raw JSON of every API answer, and the reports.

## How the data gets in

| Source | Route | What you get |
|---|---|---|
| Garmin Connect | unofficial `garminconnect` library with a saved session | every activity (all sports), per-lap data for runs, VO2max, lactate threshold, training status and load, HRV, readiness, resting HR, sleep, race predictions, personal records, and the next two weeks of structured workouts Runna pushed to the Garmin calendar |
| Runna | the plan's iCalendar feed (Runna app: Connected Apps & Devices -> Calendars -> Other Calendar -> Connect, copy the URL) | every planned session of the active plan, as far ahead as the plan goes, with Runna's own description text |

Runna has no public API and its web version does not show plans, so the calendar
feed is the only way to see the whole plan. Garmin only ever holds the next two
weeks of Runna workouts, but those carry exact step targets (paces, durations), so
both are stored.

## Setup

```powershell
uv sync                 # installs into .venv
uv run fastr init       # writes fastr.toml
```

Edit `fastr.toml`: set your goals (`[[goals]]` blocks), paste the Runna calendar
URL under `[runna]`, optionally your Garmin email and a `history_start` date.

```powershell
uv run fastr auth       # one-time Garmin login (asks for password and MFA code)
uv run fastr sync       # Garmin history; first run pulls 18 months and takes a while
uv run fastr runna      # Runna plan from the calendar feed
uv run fastr report     # data/reports/latest.md + latest.json
```

Later runs of `fastr sync` are incremental (from a week before the latest stored
activity). Use `--full` to re-pull everything, `--since 2026-01-01` for a custom
start, `--no-metrics` to skip the daily metrics.

## Commands

| Command | Purpose |
|---|---|
| `fastr auth` | log in to Garmin once; tokens go to `data/garmin_tokens/` |
| `fastr sync` | activities, laps, daily metrics, snapshots, scheduled workouts |
| `fastr runna [--url ...] [--file plan.ics] [--show 20]` | import the Runna plan |
| `fastr report [--weeks 26] [--plan-weeks 25] [--plan-source runna_ical\|claude_plan] [--print]` | build the markdown and JSON report |
| `fastr decoupling [--weeks 8] [--min-minutes 45]` | pace-to-heart-rate drift per steady run: was the easy run actually easy? |
| `fastr plan import plans/my-plan.csv` | load a hand-written CSV plan (see `plans/README.md`) as a plan source |
| `fastr plan push [--weeks 3] [--dry-run] [--replace]` | schedule the plan's runs in Garmin Connect as structured workouts (heart-rate caps and pace zones from `[paces]` in fastr.toml), so the watch guides each session |
| `fastr plan unpush [--from DATE] [--to DATE]` | remove workouts fastr scheduled |
| `fastr status` | what is in the store, last syncs, goals |
| `fastr inspect snapshot|range|activity|laps|planned|daily [key]` | look at raw shapes to refine extractors |
| `fastr export` | CSVs of activities, laps, daily metrics, weekly totals and plan |

## Pushing a plan to the watch

`fastr plan push` builds one Garmin workout per run session of a CSV plan: easy runs
become a distance step with a heart-rate ceiling, threshold and interval sessions get a
warm-up, a repeat group with pace targets and a cool-down, long runs get a lower
heart-rate ceiling after 10 km and pace targets for any marathon- or half-pace blocks.
Each workout is scheduled on its date, and Garmin Connect sends it to the watch at the
next sync. Run with `--dry-run` first to see the exact steps. After a race changes your
zones, edit `[paces]` and run `fastr plan push --replace`. The push covers three weeks
by default, so re-run it weekly.

## What the report contains

- **Goals**: target vs Garmin race prediction (now and four weeks ago), an
  equivalent time from your best recent effort (Daniels VDOT), goal pace against
  lactate-threshold pace, current volume against the plan's peak, and a verdict.
- **Fitness markers**: VO2max trend, LT, training status and acute:chronic load,
  resting HR and HRV, readiness, sleep, endurance and hill scores, predictions.
- **Best efforts (12 weeks)**: fastest contiguous-lap windows for 1 km to marathon.
- **Aerobic decoupling**: for every steady run of 45 minutes or more, pace-to-heart-rate
  in the first half against the second half (Friel's Pa:HR). Drift under 5 percent means
  the run was genuinely easy; over 8 percent means it was faster than easy.
- **Weekly history**: runs, km, time, long run, pace, HR, elevation, load, share
  of time in HR zones 1-2, cross-training, km by session type.
- **Plan adherence**: planned vs completed runs per week, missed sessions.
- **Plan outlook**: the next 25 weeks of Runna sessions, volume, long runs, key sessions.
- **Next 14 days** with Garmin step targets when available.
- **Flags**: volume jumps, long-run share, too little easy running, high ACWR,
  elevated resting HR, poor adherence.

## Layout

```
src/fastr/
  cli.py        commands
  config.py     fastr.toml, goals, formatting helpers
  store.py      SQLite schema and access
  garmin.py     login, sync and flattening of Garmin responses
  runna.py      calendar feed fetch and parsing
  analysis.py   weekly summaries, adherence, VDOT/Riegel, goal assessment, flags
  report.py     markdown + JSON report
tests/          synthetic end-to-end test (no network)
plans/          CSV training plans (see plans/README.md); your own plans are git-ignored
data/           local only: tokens, fastr.db, raw/, reports/, exports/
```

Private data (`data/`, `fastr.toml`, your plans) is git-ignored so the repo can be public.

## License

MIT, see `LICENSE`.

## Notes

- The Garmin library is unofficial; if Garmin changes an endpoint, `fastr inspect`
  shows the raw payloads so the extractor in `garmin.py` can be adjusted.
- Range endpoints are fetched in windows and stored raw; `extract_daily_from_ranges`
  turns them into the `daily` table and is safe to re-run.
- Runna's feed has no structured step data; distances and durations are parsed
  from the event text. Check `fastr runna --show 20` after the first import.
