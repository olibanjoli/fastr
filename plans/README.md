# plans

Hand-written training plans as CSV, one row per week:

```powershell
uv run fastr plan import plans/my-plan.csv      # loads it as plan source "claude_plan"
uv run fastr report --plan-source claude_plan   # or set [plan] source in fastr.toml
uv run fastr plan push --dry-run                # the Garmin workouts it would create
```

Columns: `week, start` (the Monday, YYYY-MM-DD), `phase, total_km`, then for each day
`mon_km, mon, tue_km, tue, ... sun_km, sun`, and `notes`. `<day>` is the session text and
`<day>_km` the day's total distance including warm-up and cool-down. `example-plan.csv`
shows the format. Your own plans in this folder are git-ignored; only the README and
the example are tracked.

## Session text

The first word is the session code. The rest is free text, but `fastr plan push` reads
a few patterns from it to build the watch workout:

| Code | Pushed as | Patterns read from the text |
|---|---|---|
| `E`, `SHAKE` | one distance step at the easy heart-rate cap | `6x20 s strides` or `6x10 s hill sprints`, added at the end with a walk-back recovery (60 s, 90 s for hills) |
| `L` | easy at the cap; from 12 km up, the part after 10 km uses the lower long-run cap | `last 6 km @MP` for a fast finish; `2x4 km @HMP` for blocks with 1 km easy floats and a 1 km cool-down |
| `T`, `I`, `MP` | warm-up, a repeat group with pace targets, cool-down | `3x1.5 km @T with 90 s jog`, `5x1000 m @I with 2:30 jog`, `20 min continuous @T`; recovery defaults to 90 s |
| `RACE` | warm-up, the race distance without targets, cool-down | `10 km`, `half`, `marathon`; 10 km when nothing matches |
| anything else | not pushed (strength, rest, cross-training, walk) | |

The zone after `@` is a key of `[paces]` in `fastr.toml`: `T`, `I`, `MP`, `HMP`. Paces there
are written slow-fast per km, for example `t = "5:15-5:05"`. Easy and long runs carry
heart-rate caps (`easy_hr_max`, `long_hr_max`) instead of paces, so hills do not break them.

Warm-up and cool-down are whatever is left of the day's km after the work: split about
60/40 with at least 1 km of warm-up, or half and half when less than 1.5 km remains.
A `T`, `I` or `MP` session whose text matches none of the patterns stops the push with
an error, so a typo cannot silently turn into an easy run.

Workouts are named `W03 Tue T 3x1.5 km` on the watch (plan week, weekday, session).
`fastr plan move FROM TO` reschedules a pushed day; `fastr plan unpush` removes them.
