"""Garmin Connect: login with a persistent token store, and sync into the Store.

Everything fetched is kept twice: a flattened row in SQLite for analysis and the
untouched JSON under data/raw/ so nothing is lost when Garmin changes a shape.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.exceptions import GarminConnectNotFoundError

from .store import Store

RUN_TYPES = {
    "running", "treadmill_running", "trail_running", "track_running",
    "street_running", "indoor_running", "virtual_run", "ultra_run", "obstacle_run",
}

# Be polite to Garmin: a short pause between calls avoids 429s on big backfills.
CALL_PAUSE_S = 0.25


class GarminAuthRequired(RuntimeError):
    pass


# -- login ---------------------------------------------------------------------

def login_with_tokens(token_dir: Path) -> Garmin:
    """Resume a session from saved tokens. Raises GarminAuthRequired otherwise."""
    if not token_dir.exists() or not any(token_dir.iterdir()):
        raise GarminAuthRequired("No Garmin tokens found. Run `fastr auth` first.")
    g = Garmin()
    try:
        g.login(tokenstore=str(token_dir))
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, FileNotFoundError) as e:
        raise GarminAuthRequired(f"Saved Garmin session is no longer valid ({e}). Run `fastr auth`.") from e
    return g


def login_with_credentials(
    email: str, password: str, token_dir: Path, prompt_mfa: Callable[[], str]
) -> Garmin:
    token_dir.mkdir(parents=True, exist_ok=True)
    g = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    g.login(tokenstore=str(token_dir))
    return g


# -- helpers -------------------------------------------------------------------

def _iso(d: date) -> str:
    return d.isoformat()


def _chunks(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a Garmin method, tolerating missing-data errors and pausing briefly."""
    time.sleep(CALL_PAUSE_S)
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError:
            wait = 30 * (attempt + 1)
            print(f"  rate limited, waiting {wait}s ...")
            time.sleep(wait)
        except GarminConnectNotFoundError:
            return None  # no data for that day / feature not on this account
        except GarminConnectConnectionError as e:
            msg = str(e)
            # other 4xx answers mean "nothing here" rather than a transient fault
            if "client error" in msg or "No data" in msg:
                return None
            if attempt == 2:
                raise
            time.sleep(2)
    return None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _date_of(s: str | None) -> str | None:
    if not s:
        return None
    return str(s)[:10]


# -- activities ----------------------------------------------------------------

def flatten_activity(a: dict[str, Any]) -> dict[str, Any]:
    type_key = (a.get("activityType") or {}).get("typeKey") or a.get("activityTypeKey") or ""
    start_local = a.get("startTimeLocal")
    return {
        "activity_id": int(a["activityId"]),
        "date": _date_of(start_local) or _date_of(a.get("startTimeGMT")) or "",
        "start_time_local": start_local,
        "start_time_gmt": a.get("startTimeGMT"),
        "name": a.get("activityName"),
        "type_key": type_key,
        "is_run": 1 if type_key in RUN_TYPES else 0,
        "distance_m": _num(a.get("distance")),
        "duration_s": _num(a.get("duration")),
        "moving_duration_s": _num(a.get("movingDuration")),
        "elapsed_s": _num(a.get("elapsedDuration")),
        "avg_speed": _num(a.get("averageSpeed")),
        "max_speed": _num(a.get("maxSpeed")),
        "avg_hr": _num(a.get("averageHR")),
        "max_hr": _num(a.get("maxHR")),
        "avg_cadence": _num(a.get("averageRunningCadenceInStepsPerMinute")),
        "elevation_gain": _num(a.get("elevationGain")),
        "elevation_loss": _num(a.get("elevationLoss")),
        "calories": _num(a.get("calories")),
        "aerobic_te": _num(a.get("aerobicTrainingEffect")),
        "anaerobic_te": _num(a.get("anaerobicTrainingEffect")),
        "te_label": a.get("trainingEffectLabel"),
        "training_load": _num(a.get("activityTrainingLoad")),
        "vo2max": _num(a.get("vO2MaxValue")),
        "workout_id": _int(a.get("workoutId")),
        "hr_z1": _num(a.get("hrTimeInZone_1")),
        "hr_z2": _num(a.get("hrTimeInZone_2")),
        "hr_z3": _num(a.get("hrTimeInZone_3")),
        "hr_z4": _num(a.get("hrTimeInZone_4")),
        "hr_z5": _num(a.get("hrTimeInZone_5")),
        "avg_power": _num(a.get("avgPower")),
        "norm_power": _num(a.get("normPower")),
        "avg_stride_len": _num(a.get("avgStrideLength")),
        "avg_gct": _num(a.get("avgGroundContactTime")),
        "avg_vert_osc": _num(a.get("avgVerticalOscillation")),
        "lap_count": _int(a.get("lapCount")),
        "location": a.get("locationName"),
        "description": a.get("description"),
        "raw": json.dumps(a, ensure_ascii=False, default=str),
    }


def flatten_lap(lap: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "lap_index": int(lap.get("lapIndex", index)),
        "start_time_gmt": lap.get("startTimeGMT"),
        "distance_m": _num(lap.get("distance")),
        "duration_s": _num(lap.get("duration")),
        "moving_duration_s": _num(lap.get("movingDuration")),
        "avg_speed": _num(lap.get("averageSpeed")),
        "max_speed": _num(lap.get("maxSpeed")),
        "avg_hr": _num(lap.get("averageHR")),
        "max_hr": _num(lap.get("maxHR")),
        "avg_cadence": _num(lap.get("averageRunCadence")),
        "avg_power": _num(lap.get("averagePower")),
        "elevation_gain": _num(lap.get("elevationGain")),
        "intensity_type": lap.get("intensityType"),
        "wkt_step_index": _int(lap.get("wktStepIndex")),
        "raw": json.dumps(lap, ensure_ascii=False, default=str),
    }


def sync_activities(g: Garmin, store: Store, since: date, until: date, log=print) -> int:
    """Pull every activity summary between two dates (all sports) into the store."""
    total = 0
    for a, b in _chunks(since, until, 90):
        acts = _call(g.get_activities_by_date, _iso(a), _iso(b)) or []
        for act in acts:
            row = flatten_activity(act)
            store.upsert_activity(row)
            store.save_raw("activities", row["activity_id"], act)
            total += 1
        log(f"  activities {a} .. {b}: {len(acts)}")
    store.log_sync("activities", f"{since}..{until} n={total}")
    return total


def sync_laps(g: Garmin, store: Store, limit: int | None = None, log=print) -> int:
    """Fetch laps for running activities that do not have them yet."""
    ids = store.activity_ids_missing_laps(runs_only=True)
    if limit:
        ids = ids[:limit]
    done = 0
    for i, activity_id in enumerate(ids, 1):
        payload = _call(g.get_activity_laps, activity_id)
        laps = (payload or {}).get("lapDTOs") or []
        store.upsert_laps(activity_id, (flatten_lap(l, idx) for idx, l in enumerate(laps)))
        store.save_raw("laps", activity_id, payload)
        done += 1
        if i % 25 == 0:
            log(f"  laps {i}/{len(ids)}")
    store.log_sync("laps", f"n={done}")
    return done


# -- daily / range metrics -----------------------------------------------------

def _as_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Range endpoints return either a list or a dict wrapping one; normalise."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [p for p in v if isinstance(p, dict)]
        # dict keyed by date -> list of entries
        if payload and all(isinstance(v, dict) for v in payload.values()):
            out = []
            for k, v in payload.items():
                v = dict(v)
                v.setdefault("calendarDate", k)
                out.append(v)
            return out
    return []


def _entry_date(e: dict[str, Any]) -> str | None:
    for k in ("calendarDate", "date", "summaryDate", "startDate", "day", "timestamp", "startTimestampLocal"):
        if e.get(k):
            return str(e[k])[:10]
    return None


RANGE_ENDPOINTS: dict[str, tuple[str, int, dict[str, Any]]] = {
    # kind: (method name, max window in days, extra keyword arguments)
    "max_metrics": ("get_max_metrics_range", 28, {}),
    "hrv": ("get_hrv_data_range", 28, {}),
    "training_readiness": ("get_training_readiness_range", 28, {}),
    "weekly_training_status": ("get_weekly_training_status", 90, {}),
    "fitness_age": ("get_fitness_age_daily", 28, {}),
    "endurance_score": ("get_endurance_score", 90, {"aggregation": "daily"}),
    "hill_score": ("get_hill_score", 90, {}),
    "running_tolerance": ("get_running_tolerance", 90, {"aggregation": "daily"}),
    "race_predictions": ("get_race_predictions", 360, {"_type": "daily"}),
}


def sync_ranges(g: Garmin, store: Store, since: date, until: date, log=print) -> None:
    """Pull date-range metrics in windows and store them raw; extraction is separate."""
    for kind, (method, window, kwargs) in RANGE_ENDPOINTS.items():
        fn = getattr(g, method, None)
        if fn is None:
            continue
        n = 0
        for a, b in _chunks(since, until, window):
            try:
                payload = _call(fn, _iso(a), _iso(b), **kwargs)
            except Exception as e:  # noqa: BLE001 - keep syncing other kinds
                log(f"  {kind} {a}..{b}: failed ({e})")
                continue
            if payload:
                store.save_range(kind, _iso(a), _iso(b), payload)
                n += 1
        log(f"  {kind}: {n} windows")
    # lactate threshold history has its own signature
    try:
        lt = _call(g.get_lactate_threshold, latest=False, start_date=_iso(since), end_date=_iso(until))
        if lt:
            store.save_range("lactate_threshold", _iso(since), _iso(until), lt)
    except Exception as e:  # noqa: BLE001
        log(f"  lactate_threshold history failed ({e})")
    store.log_sync("ranges", f"{since}..{until}")


def extract_daily_from_ranges(store: Store, log=print) -> int:
    """Walk the raw range payloads and fill the flattened daily table."""
    n = 0

    def put(day: str | None, **cols: Any) -> None:
        nonlocal n
        if day:
            store.upsert_daily(day, **cols)
            n += 1

    for _, _, payload in store.ranges("max_metrics"):
        for e in _as_list(payload):
            generic = e.get("generic") or {}
            put(
                _entry_date(e) or _entry_date(generic),
                vo2max=_num(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")),
            )

    for _, _, payload in store.ranges("hrv"):
        for e in _as_list(payload, "hrvSummaries"):
            put(
                _entry_date(e),
                hrv_last_night=_num(e.get("lastNightAvg")),
                hrv_weekly_avg=_num(e.get("weeklyAvg")),
                hrv_status=e.get("status"),
            )

    for _, _, payload in store.ranges("training_readiness"):
        for e in _as_list(payload):
            put(_entry_date(e), readiness_score=_int(e.get("score")), readiness_level=e.get("level"))

    for _, _, payload in store.ranges("fitness_age"):
        for e in _as_list(payload, "fitnessAgeData", "data"):
            values = e.get("values") if isinstance(e.get("values"), dict) else e
            put(_entry_date(e), rhr=_int(values.get("rhr") or values.get("restingHeartRate")))

    for _, _, payload in store.ranges("weekly_training_status"):
        # {"reportData": {"<deviceId>": [ {calendarDate, trainingStatusFeedbackPhrase, ...} ]}}
        report = payload.get("reportData") if isinstance(payload, dict) else None
        entries: list[dict[str, Any]] = []
        if isinstance(report, dict):
            for lst in report.values():
                if isinstance(lst, list):
                    entries.extend(x for x in lst if isinstance(x, dict))
        else:
            entries = _as_list(payload, "trainingStatusList", "weeklyTrainingStatus")
        for e in entries:
            acute = e.get("acuteTrainingLoadDTO") or e
            put(
                _entry_date(e),
                acute_load=_num(acute.get("dailyTrainingLoadAcute") or acute.get("dailyAcuteLoad") or acute.get("acuteLoad")),
                chronic_load=_num(acute.get("dailyTrainingLoadChronic") or acute.get("dailyChronicLoad") or acute.get("chronicLoad")),
                acwr=_num(acute.get("dailyAcuteChronicWorkloadRatio") or acute.get("acwr")),
                training_status=e.get("trainingStatusFeedbackPhrase") or e.get("trainingStatus"),
            )

    for _, _, payload in store.ranges("running_tolerance"):
        for e in _as_list(payload):
            put(
                _entry_date(e),
                run_tolerance_phrase=e.get("runningToleranceFeedBackPhrase"),
                acute_distance_m=_num(e.get("acuteDistance")),
                acute_tolerance_m=_num(e.get("acuteTolerance")),
            )

    for _, _, payload in store.ranges("endurance_score"):
        group = payload.get("groupMap") if isinstance(payload, dict) else None
        if isinstance(group, dict):
            for day, e in group.items():
                if isinstance(e, dict):
                    put(str(day)[:10], endurance_score=_int(e.get("groupAverage") or e.get("overallScore")))
        else:
            for e in _as_list(payload, "enduranceScoreDTOList"):
                put(_entry_date(e), endurance_score=_int(e.get("overallScore") or e.get("enduranceScore")))

    for _, _, payload in store.ranges("hill_score"):
        for e in _as_list(payload, "hillScoreDTOList"):
            put(_entry_date(e), hill_score=_int(e.get("overallScore") or e.get("hillScore")))

    for _, _, payload in store.ranges("lactate_threshold"):
        for e in _as_list(payload, "lactateThresholdList", "data"):
            put(
                _entry_date(e),
                lt_hr=_int(e.get("heartRate") or e.get("lactateThresholdHeartRate")),
                lt_speed=_num(e.get("speed") or e.get("lactateThresholdSpeed")),
            )

    for _, _, payload in store.ranges("race_predictions"):
        for e in _as_list(payload):
            put(
                _entry_date(e),
                pred_5k=_int(e.get("time5K")),
                pred_10k=_int(e.get("time10K")),
                pred_half=_int(e.get("timeHalfMarathon")),
                pred_marathon=_int(e.get("timeMarathon")),
            )

    store.commit()
    log(f"  daily rows touched: {n}")
    return n


def sync_recent_days(g: Garmin, store: Store, days: int, log=print) -> None:
    """Per-day endpoints (sleep, RHR, training status) for the last N days only."""
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        ds = _iso(d)
        cols: dict[str, Any] = {}
        sleep = _call(g.get_sleep_data, ds)
        if sleep:
            s = sleep.get("dailySleepDTO") or {}
            scores = s.get("sleepScores") or {}
            cols["sleep_score"] = _int((scores.get("overall") or {}).get("value"))
            cols["sleep_seconds"] = _int(s.get("sleepTimeSeconds"))
            store.save_raw("sleep", ds, sleep)
        rhr = _call(g.get_rhr_day, ds)
        if rhr:
            try:
                metrics = rhr["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"]
                cols["rhr"] = _int(metrics[0]["value"])
            except (KeyError, IndexError, TypeError):
                pass
        summary = _call(g.get_user_summary, ds)
        if summary:
            cols["steps"] = _int(summary.get("totalSteps"))
            cols["body_battery_max"] = _int(summary.get("bodyBatteryHighestValue"))
            cols["body_battery_min"] = _int(summary.get("bodyBatteryLowestValue"))
            cols.setdefault("rhr", _int(summary.get("restingHeartRate")))
            store.save_raw("summary", ds, summary)
        status = _call(g.get_training_status, ds)
        if status:
            store.save_raw("training_status", ds, status)
            mrts = (status.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
            for _, v in mrts.items():
                cols["training_status"] = v.get("trainingStatusFeedbackPhrase")
                acute = v.get("acuteTrainingLoadDTO") or {}
                cols["acute_load"] = _num(acute.get("dailyTrainingLoadAcute") or acute.get("dailyAcuteLoad"))
                cols["chronic_load"] = _num(acute.get("dailyTrainingLoadChronic") or acute.get("dailyChronicLoad"))
                cols["acwr"] = _num(acute.get("dailyAcuteChronicWorkloadRatio"))
                break
            vo2 = (status.get("mostRecentVO2Max") or {}).get("generic") or {}
            cols.setdefault("vo2max", _num(vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue")))
        store.upsert_daily(d, **cols)
    store.commit()
    store.log_sync("recent_days", f"days={days}")
    log(f"  recent days: {days}")


# -- snapshots -----------------------------------------------------------------

SNAPSHOT_CALLS: dict[str, tuple[str, tuple[Any, ...], dict[str, Any]]] = {
    "profile": ("get_user_profile", (), {}),
    "hr_zones": ("get_heart_rate_zones", (), {}),
    "personal_records": ("get_personal_record", (), {}),
    "race_predictions": ("get_race_predictions", (), {}),
    "lactate_threshold": ("get_lactate_threshold", (), {"latest": True}),
    "primary_event": ("get_primary_event", (), {}),
    "upcoming_events": ("get_upcoming_events", (), {"days_forward": 365, "limit": 20}),
    "pace_bands": ("get_pace_bands", (), {}),
    "training_plans": ("get_training_plans", (), {}),
}


def sync_snapshots(g: Garmin, store: Store, log=print) -> None:
    today = _iso(date.today())
    for kind, (method, args, kwargs) in SNAPSHOT_CALLS.items():
        fn = getattr(g, method, None)
        if fn is None:
            continue
        try:
            payload = _call(fn, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            log(f"  snapshot {kind}: failed ({e})")
            continue
        if payload is not None:
            store.save_snapshot(kind, payload)
    for kind, method in (
        ("training_status", "get_training_status"),
        ("max_metrics", "get_max_metrics"),
        ("load_balance", "get_training_four_week_load_balance"),
        ("running_economy", "get_running_economy"),
    ):
        try:
            payload = _call(getattr(g, method), today)
        except Exception as e:  # noqa: BLE001
            log(f"  snapshot {kind}: failed ({e})")
            continue
        if payload is not None:
            store.save_snapshot(kind, payload)
    _daily_from_snapshots(store)
    store.log_sync("snapshots")
    log("  snapshots done")


def _daily_from_snapshots(store: Store) -> None:
    """Fill the daily table from the 'latest' snapshots, a fallback when range parsing misses."""
    preds = store.latest_snapshot("race_predictions")
    if isinstance(preds, dict) and preds.get("calendarDate"):
        store.upsert_daily(
            str(preds["calendarDate"])[:10],
            pred_5k=_int(preds.get("time5K")),
            pred_10k=_int(preds.get("time10K")),
            pred_half=_int(preds.get("timeHalfMarathon")),
            pred_marathon=_int(preds.get("timeMarathon")),
        )
    lt = store.latest_snapshot("lactate_threshold")
    if isinstance(lt, dict):
        shr = lt.get("speed_and_heart_rate") or {}
        if shr.get("calendarDate"):
            store.upsert_daily(
                str(shr["calendarDate"])[:10],
                lt_hr=_int(shr.get("heartRate")),
                lt_speed=_num(shr.get("speed")),
            )
    mm = store.latest_snapshot("max_metrics")
    entries = mm if isinstance(mm, list) else [mm] if isinstance(mm, dict) else []
    for e in entries:
        generic = (e or {}).get("generic") or {}
        if generic.get("calendarDate"):
            store.upsert_daily(
                str(generic["calendarDate"])[:10],
                vo2max=_num(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")),
            )
    store.commit()


# -- scheduled workouts (Runna pushes the next two weeks here) ------------------

def _workout_steps(workout: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Garmin workout segments/steps into a readable step list."""
    steps: list[dict[str, Any]] = []

    def walk(items: list[dict[str, Any]], depth: int = 0) -> None:
        for s in items:
            if s.get("type") == "RepeatGroupDTO" or s.get("workoutSteps"):
                reps = s.get("numberOfIterations")
                steps.append({"kind": "repeat", "iterations": reps, "depth": depth})
                walk(s.get("workoutSteps") or [], depth + 1)
                continue
            step_type = (s.get("stepType") or {}).get("stepTypeKey")
            end = (s.get("endCondition") or {}).get("conditionTypeKey")
            target = (s.get("targetType") or {}).get("workoutTargetTypeKey")
            steps.append(
                {
                    "kind": step_type,
                    "depth": depth,
                    "end": end,
                    "end_value": s.get("endConditionValue"),
                    "target": target,
                    "target_low": s.get("targetValueOne"),
                    "target_high": s.get("targetValueTwo"),
                    "description": s.get("description"),
                }
            )

    for seg in workout.get("workoutSegments") or []:
        walk(seg.get("workoutSteps") or [])
    return steps


def sync_scheduled_workouts(g: Garmin, store: Store, days_back: int = 14, days_fwd: int = 28, log=print) -> int:
    start = date.today() - timedelta(days=days_back)
    end = date.today() + timedelta(days=days_fwd)
    items = _call(g.get_scheduled_workouts_by_period, _iso(start), _iso(end)) or []
    rows = []
    for it in items:
        workout = it.get("workout") or it
        wid = it.get("workoutScheduleId") or it.get("id") or workout.get("workoutId")
        day = it.get("calendarDate") or it.get("date") or it.get("scheduleDate") or _date_of(it.get("startDate"))
        if not day:
            continue
        detail = None
        workout_id = workout.get("workoutId") or it.get("workoutId")
        if workout_id and not workout.get("workoutSegments"):
            detail = _call(g.get_workout_by_id, workout_id)
        full = detail or workout
        est_dist = _num(full.get("estimatedDistanceInMeters"))
        est_dur = _num(full.get("estimatedDurationInSecs"))
        rows.append(
            {
                "uid": f"garmin:{wid}",
                "source": "garmin_calendar",
                "date": str(day)[:10],
                "title": full.get("workoutName") or it.get("title"),
                "workout_type": (full.get("sportType") or {}).get("sportTypeKey"),
                "distance_m": est_dist,
                "duration_s": est_dur,
                "description": full.get("description"),
                "is_completed": 1 if it.get("completed") or it.get("activityId") else 0,
                "steps_json": json.dumps(_workout_steps(full), ensure_ascii=False, default=str),
                "raw": json.dumps(it if detail is None else {"schedule": it, "workout": detail}, ensure_ascii=False, default=str),
            }
        )
    store.delete_planned("garmin_calendar")
    n = store.upsert_planned(rows)
    store.save_raw("scheduled_workouts", f"{start}_{end}", items)
    store.log_sync("scheduled_workouts", f"n={n}")
    log(f"  scheduled workouts (Garmin calendar): {n}")
    return n
