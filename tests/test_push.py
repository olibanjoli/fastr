"""Session text -> steps -> Garmin workout JSON, without any network."""

from __future__ import annotations

from fastr.config import DEFAULT_PACES
from fastr.push import Zones, describe, parse_session, steps_distance, to_garmin_json

Z = Zones(dict(DEFAULT_PACES))


def test_easy_with_strides():
    label, steps = parse_session("E easy + 6x20 s strides", 8, Z)
    assert label == "Easy + strides"
    assert steps[0]["distance_m"] == 8000 and steps[0]["hr"] == (95, 152)
    assert steps[1]["kind"] == "repeat" and steps[1]["reps"] == 6
    assert steps[1]["steps"][0]["time_s"] == 20


def test_threshold_reps_split_warmup_and_cooldown():
    label, steps = parse_session("T 3x1.5 km @T with 90 s jog", 7, Z)
    assert label == "T 3x1.5 km"
    assert steps[0]["kind"] == "warmup" and steps[-1]["kind"] == "cooldown"
    assert steps[0]["distance_m"] + steps[-1]["distance_m"] == 2500
    rep = steps[1]
    assert rep["reps"] == 3 and rep["steps"][0]["distance_m"] == 1500 and rep["steps"][0]["pace"] == (315, 305)
    assert rep["steps"][1]["time_s"] == 90
    assert steps_distance(steps) == 7000


def test_interval_metres_and_minutes_recovery():
    _, steps = parse_session("I 5x1000 m @I with 2:30 jog", 10, Z)
    rep = steps[1]
    assert rep["steps"][0]["distance_m"] == 1000 and rep["steps"][0]["pace"] == (295, 285)
    assert rep["steps"][1]["time_s"] == 150
    _, steps2 = parse_session("T 2x2 km @T with 2 min jog", 6, Z)
    assert steps2[1]["steps"][1]["time_s"] == 120


def test_long_run_variants():
    label, steps = parse_session("L long easy", 20, Z)
    assert label == "Long easy" and len(steps) == 2 and steps[1]["hr"] == (95, 145)
    label, steps = parse_session("L long with last 6 km @MP", 22, Z)
    assert steps[-1]["distance_m"] == 6000 and steps[-1]["pace"] == (330, 325)
    assert steps_distance(steps) == 22000
    label, steps = parse_session("L long with 2x4 km @HMP", 22, Z)
    assert label == "Long + 2x4 km @HMP" and steps_distance(steps) == 22000


def test_race_and_strength():
    label, steps = parse_session("RACE 10 km or parkrun + warm up and cool down", 12, Z)
    assert label == "Race" and steps[1]["distance_m"] == 10000 and steps[1]["pace"] is None
    assert parse_session("Strength", 0, Z) is None


def test_garmin_json_shape():
    _, steps = parse_session("T 3x1.5 km @T with 90 s jog", 7, Z)
    j = to_garmin_json("W02 Tue T 3x1.5 km", "desc", steps)
    assert j["sportType"]["sportTypeKey"] == "running"
    top = j["workoutSegments"][0]["workoutSteps"]
    assert [s["type"] for s in top] == ["ExecutableStepDTO", "RepeatGroupDTO", "ExecutableStepDTO"]
    assert top[0]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone" and top[0]["targetValueTwo"] == 152
    grp = top[1]
    assert grp["numberOfIterations"] == 3 and grp["childStepId"] == 1
    work = grp["workoutSteps"][0]
    assert work["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert work["targetValueOne"] > work["targetValueTwo"]  # faster bound first, as Garmin does
    assert abs(1000 / work["targetValueOne"] - 305) < 0.01 and abs(1000 / work["targetValueTwo"] - 315) < 0.01
    orders = [top[0]["stepOrder"], grp["stepOrder"], grp["workoutSteps"][0]["stepOrder"], grp["workoutSteps"][1]["stepOrder"], top[2]["stepOrder"]]
    assert orders == [1, 2, 3, 4, 5]
    assert "3x(interval 1.5 km @ 5:05-5:15 | recovery 90 s)" in describe(steps)


class FakeGarmin:
    def __init__(self):
        self.calls: list[tuple] = []

    def unschedule_workout(self, schedule_id):
        self.calls.append(("unschedule", schedule_id))

    def schedule_workout(self, workout_id, day):
        self.calls.append(("schedule", workout_id, day))
        return {"workoutScheduleId": 900 + len(self.calls)}


def test_move_reschedules_pushed_workout(tmp_path):
    from datetime import date

    from fastr.push import move
    from fastr.store import Store

    store = Store(tmp_path / "t.db", raw_dir=tmp_path / "raw")
    store.record_push("p:1:tue", "2026-10-20", 111, 501, "W01 Tue Easy")
    store.record_push("p:1:thu", "2026-10-22", 112, 502, "W01 Thu Easy")
    g = FakeGarmin()
    n = move(g, store, date(2026, 10, 20), date(2026, 10, 21), log=lambda s: None)
    assert n == 1
    assert g.calls == [("unschedule", 501), ("schedule", 111, "2026-10-21")]
    rows = {r["uid"]: dict(r) for r in store.pushed()}
    assert rows["p:1:tue"]["date"] == "2026-10-21" and rows["p:1:tue"]["schedule_id"] == 902
    assert rows["p:1:thu"]["date"] == "2026-10-22"
    assert move(g, store, date(2026, 10, 20), date(2026, 10, 21), log=lambda s: None) == 0
    store.close()
