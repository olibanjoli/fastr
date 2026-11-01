# fastr

A local CLI that pulls a runner's Garmin Connect history, and optionally a Runna plan,
into SQLite and builds a training report, so that Claude can act as the athlete's coach.
Python 3.12, uv, typer, SQLite. Run everything with `uv run fastr <command>`.

## Privacy, non-negotiable

`data/`, `fastr.toml` and `plans/*` (except the README and the example) hold the athlete's
Garmin tokens, raw API payloads, goals, zones and plans. They are git-ignored and the repo
is public. Never copy anything from them into tracked files, tests, docs or commit messages:
no goals, race names, dates, activity IDs, locations, names or e-mail addresses. Test fixtures
use placeholder IDs and generic names. If you are unsure whether something is private, it is.

## The coaching loop

1. `uv run fastr sync` pulls what is new (`--recent-days 7` is enough for a weekly check-in).
2. `uv run fastr report` writes `data/reports/latest.md` and `latest.json`. Read the markdown;
   the JSON has the numbers behind it.
3. `uv run fastr decoupling` shows pace-to-heart-rate drift per steady run: were the easy
   runs actually easy.
4. Give the advice in chat. When zones or goals change, edit `[paces]` and `[[goals]]` in
   `fastr.toml`; the report scores against them.
5. `uv run fastr plan push` keeps the next three weeks of the CSV plan on the watch. Use
   `plan move FROM TO` when a session slips to another day and `plan push --replace` after
   a pace change. `plan push --dry-run` shows the exact steps first.

Conventions the report and the plan format assume:

- Easy and long runs are prescribed by heart-rate cap, quality sessions by pace. Hills make
  pace targets wrong; a heart-rate cap does not care.
- Adherence matches runs to planned sessions within the same week by distance, not by date,
  so moving a session inside a week costs nothing.
- A missed easy run is gone. A missed quality session moves within the same week. Nothing is
  ever added on top of another day to compensate.
- The first run after any injury is an easy test run, whatever was planned.
- Judge recovery on the seven-day HRV average, not on one night, and ask the athlete what
  happened before attributing a dip to training. Late nights explain more than workouts do.
- Strength, rest and cross-training rows are never pushed to Garmin.

## Code map

```
src/fastr/
  cli.py        commands (typer); stdout is reconfigured to UTF-8 for Windows consoles
  config.py     fastr.toml template and loader, goals, paces, formatting helpers
  store.py      SQLite schema, migrations and access
  garmin.py     login with saved tokens, sync and flattening of Garmin responses
  runna.py      Runna iCalendar feed parser
  plan.py       CSV plan import (plan source "claude_plan")
  push.py       session text -> steps -> Garmin workout JSON, schedule, move, unpush
  analysis.py   weekly summaries, adherence, VDOT/Riegel, decoupling, flags
  report.py     markdown + JSON report
tests/          synthetic end-to-end tests, no network
plans/          CSV plans; README.md documents the session grammar
```

## Working on the code

- `uv run pytest` runs the tests on synthetic data. Add a test when you add a parser,
  a command or a Garmin JSON shape.
- Garmin access goes through the unofficial `garminconnect` library. When an endpoint changes,
  `uv run fastr inspect <kind>` prints the raw payload so the extractor in `garmin.py` can be fixed.
- Garmin quirks already handled: lactate-threshold speed arrives in dm/s; pace-zone targets
  take the faster bound first; HR zones are lactate-threshold based with a narrow zone 2, so
  easy runs often show as zone 3.
- Shell heredocs with mixed quotes have failed on Windows; write files with the editor tool.
- Commits: one short line, no attribution trailers.
