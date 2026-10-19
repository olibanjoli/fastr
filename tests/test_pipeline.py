"""End-to-end test on synthetic data: store -> analysis -> report, plus Runna ICS parsing."""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from fastr import analysis as an
from fastr.config import Config, Goal, parse_hms
from fastr.garmin import flatten_activity, flatten_lap
from fastr.report import build_report, describe_steps, write_report
from fastr.store import Store

TODAY = date(2026, 10, 19)


def make_activity(aid: int, day: date, name: str, km: float, pace_s: float, hr: int, type_key: str = "running") -> dict:
    dur = km * pace_s
    return {
        "activityId": aid,
        "activityName": name,
        "startTimeLocal": f"{day.isoformat()} 07:00:00",
        "startTimeGMT": f"{day.isoformat()} 05:00:00",
        "activityType": {"typeKey": type_key},
        "distance": km * 1000,
        "duration": dur,
        "movingDuration": dur - 10,
        "elapsedDuration": dur + 30,
        "averageSpeed": 1000 / pace_s,
        "averageHR": hr,
        "maxHR": hr + 20,
        "elevationGain": 80,
        "activityTrainingLoad": km * 6,
        "hrTimeInZone_1": dur * 0.2,
        "hrTimeInZone_2": dur * 0.5,
        "hrTimeInZone_3": dur * 0.2,
        "hrTimeInZone_4": dur * 0.1,
        "hrTimeInZone_5": 0,
        "vO2MaxValue": 50.0,
        "lapCount": int(km),
    }


def seed_store(store: Store) -> None:
    random.seed(1)
    aid = 1000
    for week in range(26):
        monday = an.week_start(TODAY) - timedelta(weeks=25 - week)
        plan = [
            (0, "Easy Run", 6 + week * 0.1, 360, 140),
            (2, "Intervals", 8 + week * 0.1, 300, 165),
            (4, "Tempo Run", 9 + week * 0.1, 285, 160),
            (6, "Long Run", 14 + week * 0.4, 345, 150),
        ]
        for offset, name, km, pace, hr in plan:
            day = monday + timedelta(days=offset)
            if day > TODAY:
                continue
            if random.random() < 0.08:
                continue  # skipped session
            act = make_activity(aid, day, name, km, pace, hr)
            store.upsert_activity(flatten_activity(act))
            laps = []
            remaining = km * 1000
            idx = 0
            while remaining > 0:
                d = min(1000.0, remaining)
                lap_pace = pace * (0.9 if name == "Intervals" and idx % 2 else 1.0)
                laps.append(flatten_lap({"lapIndex": idx, "distance": d, "duration": d / 1000 * lap_pace, "averageHR": hr}, idx))
                remaining -= d
                idx += 1
            store.upsert_laps(aid, laps)
            aid += 1
        # one strength session a week
        store.upsert_activity(flatten_activity(make_activity(aid, monday + timedelta(days=1), "Strength", 0, 1, 110, "strength_training")))
        aid += 1
    for i in range(120):
        d = TODAY - timedelta(days=i)
        store.upsert_daily(
            d,
            vo2max=50 + (120 - i) * 0.02,
            rhr=48 + (i % 3),
            hrv_last_night=60 + (i % 5),
            hrv_status="BALANCED",
            readiness_score=70,
            acute_load=400,
            chronic_load=380,
            acwr=1.05,
            training_status="PRODUCTIVE",
            lt_hr=168,
            lt_speed=3.6,
            pred_5k=1320,
            pred_10k=2760,
            pred_half=6180,
            pred_marathon=13200,
        )
    store.commit()
    store.save_snapshot("personal_records", [{"typeId": 3, "value": 1290, "prStartTimeLocal": "2026-06-01T08:00:00"}])


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Runna//EN
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-t_plan_week_1_EASY_RUN_0
DTSTART:{d1}
DTEND:{d1e}
SUMMARY:🏃 6km Easy Run • 6km
DESCRIPTION:Easy Run • 6km • 35m - 40m\\n\\n6km easy run at a conversational pace (no faster than 6:20/km).\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:2250
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-t_plan_week_1_INTERVALS_0
DTSTART:{d2}
DTEND:{d2e}
SUMMARY:🏃 800m Repeats • 9.5km
DESCRIPTION:Intervals • 9.5km • 50m - 55m\\n\\n2km warm up at a conversational pace (no faster than 6:20/km)\\n\\n6 reps of:\\n• 800m at 4:00/km, 90s walking rest\\n\\n2.5km cool down at a conversational pace (or slower!)\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:3200
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-t_plan_week_1_LONG_RUN_0
DTSTART:{d3}
DTEND:{d3e}
SUMMARY:🏃 18km Long Run • 18km
DESCRIPTION:Long Run • 18km • 1h40m - 1h50m\\n\\n18km at a conversational pace\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:6300
END:VEVENT
BEGIN:VEVENT
UID:UPCOMING_PLAN_WORKOUT-t_plan_week_1_FULL_BODY_0
DTSTART:{d4}
DTEND:{d4e}
SUMMARY:🏋️ Going Heavy • 40m - 50m
DESCRIPTION:Full Body • 40m - 50m\\n\\n3 sets of:\\n• Toe Walks\\n\\n📲 View in the Runna app: https://club.runna.com/x
X-WORKOUT-ESTIMATED-DURATION:3000
END:VEVENT
END:VCALENDAR
"""


def sample_ics(base: date) -> str:
    def ds(d: date) -> str:
        return d.strftime("%Y%m%d")

    d1, d2, d3, d4 = base, base + timedelta(days=2), base + timedelta(days=6), base + timedelta(days=1)
    return SAMPLE_ICS.format(
        d1=ds(d1), d1e=ds(d1 + timedelta(days=1)), d2=ds(d2), d2e=ds(d2 + timedelta(days=1)),
        d3=ds(d3), d3e=ds(d3 + timedelta(days=1)), d4=ds(d4), d4e=ds(d4 + timedelta(days=1)),
    )


def test_text_helpers():
    assert parse_hms("1:45:00") == 6300


def test_vdot_roundtrip():
    v = an.vdot(5000, 20 * 60)
    assert 48 < v < 52
    t = an.time_for_distance_at_vdot(5000, v)
    assert abs(t - 1200) < 2
    assert an.riegel(1200, 5000, 10000) == pytest.approx(1200 * 2 ** 1.06)


def test_describe_steps():
    steps = [
        {"kind": "warmup", "depth": 0, "end": "time", "end_value": 600, "target": "no.target"},
        {"kind": "repeat", "iterations": 6, "depth": 0},
        {"kind": "interval", "depth": 1, "end": "distance", "end_value": 800, "target": "pace.zone", "target_low": 4.0, "target_high": 4.3},
        {"kind": "recovery", "depth": 1, "end": "time", "end_value": 90, "target": "no.target"},
        {"kind": "cooldown", "depth": 0, "end": "lap.button", "target": "no.target"},
    ]
    text = describe_steps(json.dumps(steps))
    assert text.startswith("warmup 10:00 | 6x(interval 0.80 km @ 3:53/km-4:10/km, recovery 1:30) | cooldown until lap")


def test_pipeline_report(tmp_path: Path):
    store = Store(tmp_path / "t.db", raw_dir=tmp_path / "raw")
    seed_store(store)
    from fastr.runna import load_ics_file

    ics = tmp_path / "runna.ics"
    ics.write_text(sample_ics(TODAY - timedelta(days=3)), encoding="utf-8")
    load_ics_file(ics, store, log=lambda m: None)

    cfg = Config(goals=[Goal(name="Test half", date=TODAY + timedelta(weeks=20), distance_km=21.0975, target_time="1:40:00")])
    md, data = build_report(store, cfg, weeks=26, plan_weeks=25, today=TODAY)
    md_path, json_path = write_report(md, data, tmp_path / "reports", today=TODAY)
    assert md_path.exists() and json_path.exists()
    assert "## Goals" in md and "Test half" in md
    assert data["counts"]["runs"] > 80
    weekly = data["weekly"]
    assert len(weekly) == 26
    assert weekly[-2]["km"] > 30
    assert data["best_efforts"]["5k"]["time_s"] < 5 * 300  # intervals produce a fast 5k window
    ga = data["goals"][0]
    assert ga["garmin_pred_s"] == 6180
    assert ga["verdict"] in ("on track", "borderline", "behind")
    assert ga["effort_vdot"] > 40
    assert data["plan"][0]["run_sessions"] >= 1
    assert any(a["planned_runs"] for a in data["adherence"])
    store.close()
