"""Runna calendar feed parsing against the real event format."""

from __future__ import annotations

import json
from datetime import date

from fastr.runna import describe_steps, parse_ics, parse_step_line, plan_summary

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Runna//EN
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-abc_plan_week_1_TEMPO_0
DTSTAMP:20260105
DTSTART:20260106
DTEND:20260107
SUMMARY:🏃 Tempo 2-1-1 • 6km
DESCRIPTION:Tempo • 6km • 35m - 40m\\n\\n1km warm up at a conversational pace (no faster than 6:20/km)\\n\\n2km at 5:30/km, 120s walking rest\\n1km at 5:20/km, 90s walking rest\\n1km at 5:20/km, 90s walking rest\\n\\n1km cool down at a conversational pace (or slower!)\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-USER-TIMEZONE:Europe/Zurich
X-WORKOUT-ESTIMATED-DURATION:2400
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-abc_plan_week_1_FULL_BODY_0
DTSTART:20260107
DTEND:20260108
SUMMARY:🏋️ Going Heavy • 40m - 50m
DESCRIPTION:Full Body • 40m - 50m\\n\\n3 sets of:\\n• Toe Walks\\n• Walking Lunge\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:3000
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-abc_plan_week_1_INTERVALS_0
DTSTART:20260110
DTEND:20260111
SUMMARY:🏃 1km Repeats • 6.5km
DESCRIPTION:Intervals • 6.5km • 40m - 45m\\n\\n2km warm up at a conversational pace (no faster than 6:20/km), 90s walking rest\\n\\n3 reps of:\\n• 1km at 5:15/km (5:05-5:25/km), 90s walking rest\\n\\n1.5km cool down at a conversational pace (or slower!)\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:2700
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-abc_plan_week_14_LONG_RUN_0
DTSTART:20260409
DTEND:20260410
SUMMARY:🏃 24km Race Practice Long Run • 24km
DESCRIPTION:Long Run • 24km • 2h0m - 2h20m\\n\\n14km at a conversational pace\\n10km at 5:10/km (your target Marathon pace)\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:8100
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-abc_plan_week_25_RACE_0
DTSTART:20260628
DTEND:20260629
SUMMARY:🏃 City Marathon • 42.2km
DESCRIPTION:Race • 42.2km • 3h34m - 3h42m\\n\\nMarathon race at 5:05-5:15/km\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:13500
END:VEVENT
BEGIN:VEVENT
UID:COMPLETED_PLAN_WORKOUT-garmin-10000000001
DTSTART:20260105T145446Z
DTEND:20260105T153703Z
SUMMARY:🏃 6km Easy Run
DESCRIPTION:📊 Summary:\\nDistance: 7.16km\\nTime: 42:17\\nAvg Pace: 5:54 /km\\n\\n📋 Description:\\n6km easy run at a conversational pace (no faster than 6:20/km).\\n\\n♻️ Laps:\\n6.00 km @ 5:55 /km\\n1.16 km @ 5:49 /km\\n\\n📲 View in the Runna app: https://club.runna.com/x
END:VEVENT
BEGIN:VEVENT
UID:COMPLETED_NON_PLAN_WORKOUT-garmin-10000000002
DTSTART:20260102T124742Z
DTEND:20260102T125203Z
SUMMARY:🏃 Treadmill Running
DESCRIPTION:📊 Summary:\\nDistance: 0.73km\\nTime: 4:21\\nAvg Pace: 5:56 /km\\n\\n📲 View in the Runna app: https://club.runna.com/x
END:VEVENT
END:VCALENDAR
"""


def test_parse_feed():
    rows = parse_ics(FEED, today=date(2026, 1, 5))
    assert len(rows) == 7
    by = {r["uid"].split("-", 1)[1]: r for r in rows}

    tempo = by["abc_plan_week_1_TEMPO_0"]
    assert tempo["workout_type"] == "tempo"
    assert tempo["distance_m"] == 6000
    assert tempo["duration_s"] == 2400
    assert tempo["is_completed"] == 0
    sj = json.loads(tempo["steps_json"])
    assert sj["meta"]["plan_week"] == 1 and sj["meta"]["slot"] == "TEMPO"
    kinds = [s["kind"] for s in sj["steps"]]
    assert kinds == ["warmup", "work", "work", "work", "cooldown"]
    assert sj["steps"][1] == {"kind": "work", "distance_m": 2000.0, "pace_s": 330, "rest_s": 120}

    strength = by["abc_plan_week_1_FULL_BODY_0"]
    assert strength["workout_type"] == "strength"
    assert strength["distance_m"] is None
    assert strength["duration_s"] == 3000

    reps = by["abc_plan_week_1_INTERVALS_0"]
    assert reps["workout_type"] == "intervals"
    assert reps["distance_m"] == 6500
    steps = json.loads(reps["steps_json"])["steps"]
    assert steps[1]["kind"] == "repeat" and steps[1]["reps"] == 3
    assert steps[1]["steps"][0]["pace_low_s"] == 305 and steps[1]["steps"][0]["pace_high_s"] == 325
    assert describe_steps(steps) == "wu 2.0km (<= 6:20), rest 90s | 3x(1.0km @5:15 (5:05-5:25), rest 90s) | cd 1.5km"

    long_run = by["abc_plan_week_14_LONG_RUN_0"]
    assert long_run["workout_type"] == "long"
    assert long_run["distance_m"] == 24000
    assert json.loads(long_run["steps_json"])["steps"][1]["target_label"] == "Marathon"

    race = by["abc_plan_week_25_RACE_0"]
    assert race["workout_type"] == "race"
    assert race["distance_m"] == 42200

    done = by["garmin-10000000001"]
    assert done["is_completed"] == 1
    assert done["date"] == "2026-01-05"
    assert done["workout_type"] == "easy"
    assert done["distance_m"] == 6000  # prescribed
    meta = json.loads(done["steps_json"])["meta"]
    assert meta["actual"] == {"actual_distance_m": 7160.0, "actual_duration_s": 2537, "actual_pace_s": 354}

    other = by["garmin-10000000002"]
    assert other["workout_type"] == "unplanned"
    assert other["distance_m"] == 730.0

    summary = plan_summary(rows)
    assert summary["plan_weeks"] == 25
    assert summary["plan_start"] == "2026-01-05"
    assert summary["target_paces"] == {"Marathon": 310}
    assert summary["race"]["date"] == "2026-06-28"
    assert summary["race"]["pace_low_s"] == 305 and summary["race"]["duration_high_s"] == 3 * 3600 + 42 * 60


def test_step_lines():
    assert parse_step_line("• 400m at 4:40/km, 60s walking rest") == {"kind": "work", "distance_m": 400.0, "pace_s": 280, "rest_s": 60}
    assert parse_step_line("90s walking rest") == {"kind": "rest", "rest_s": 90}
    assert parse_step_line("25km at a conversational pace")["kind"] == "easy"
    assert parse_step_line("Marathon race at 5:05-5:15/km") == {"kind": "race", "pace_low_s": 305, "pace_high_s": 315}
    assert parse_step_line("----------") is None
