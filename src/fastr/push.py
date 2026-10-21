"""Push a CSV plan's sessions into Garmin Connect as scheduled structured workouts.

Session texts (see plans/README.md) are parsed into steps, built into Garmin's
workout JSON, uploaded, scheduled on their date and recorded in the store so a
later push can replace or remove them. Paces and heart-rate caps come from the
[paces] section of fastr.toml.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

from .config import Config, parse_hms, parse_pace_range
from .store import Store

STEP_TYPES = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "repeat": 6, "other": 7}
CONDITIONS = {"lap.button": 1, "time": 2, "distance": 3, "iterations": 7}
TARGETS = {"no.target": 1, "heart.rate.zone": 4, "pace.zone": 6}
SPORT = {"sportTypeId": 1, "sportTypeKey": "running"}
KM_UNIT = {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}

REPS_RE = re.compile(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(km|m)\s*@(\w+)(?:\s+with\s+(.+?)\s+(?:jog|rest|walk))?", re.IGNORECASE)
CONT_RE = re.compile(r"(\d+)\s*min\s+continuous\s*@(\w+)", re.IGNORECASE)
LAST_RE = re.compile(r"last\s+(\d+(?:\.\d+)?)\s*km\s*@(\w+)", re.IGNORECASE)
BLOCKS_RE = re.compile(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*km\s*@(\w+)", re.IGNORECASE)
STRIDES_RE = re.compile(r"(\d+)\s*x\s*(\d+)\s*s\s*(strides|hill sprints)", re.IGNORECASE)
RACE_DIST = {"half": 21097.5, "marathon": 42195.0}


def _dur(text: str) -> float:
    """'90 s', '2 min', '2:30' -> seconds."""
    t = text.strip().lower()
    if ":" in t:
        return float(parse_hms(t) or 0)
    m = re.match(r"(\d+(?:\.\d+)?)\s*(s|sec|min|m)\b", t)
    if not m:
        return 90.0
    v = float(m.group(1))
    return v * 60 if m.group(2).startswith("m") else v


# -- step model ----------------------------------------------------------------

def step(kind: str, *, distance_m: float | None = None, time_s: float | None = None,
         pace: tuple[int, int] | None = None, hr: tuple[int, int] | None = None, desc: str = "") -> dict[str, Any]:
    return {"kind": kind, "distance_m": distance_m, "time_s": time_s, "pace": pace, "hr": hr, "desc": desc}


def repeat(n: int, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "repeat", "reps": n, "steps": steps}


class Zones:
    def __init__(self, paces: dict[str, Any]):
        self.easy_hr = (95, int(paces.get("easy_hr_max", 152)))
        self.long_hr = (95, int(paces.get("long_hr_max", 145)))
        self.pace = {k: parse_pace_range(str(paces[k])) for k in ("easy", "long", "mp", "hmp", "t", "i", "strides") if paces.get(k)}

    def zone(self, code: str) -> tuple[int, int]:
        key = code.lower()
        if key not in self.pace:
            raise ValueError(f"unknown pace zone @{code}; add it under [paces] in fastr.toml")
        return self.pace[key]


# -- session text -> steps -----------------------------------------------------

def _wu_cd(total_m: float, work_m: float) -> tuple[float, float]:
    rem = max(total_m - work_m, 0.0)
    if rem < 1500:
        return rem / 2, rem / 2
    wu = round(rem * 0.6 / 500) * 500
    wu = max(1000.0, min(wu, rem - 500))
    return wu, rem - wu


def _strides(text: str, z: Zones) -> list[dict[str, Any]]:
    m = STRIDES_RE.search(text)
    if m:
        n, secs, what = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    elif "strides" in text.lower():
        n, secs, what = 6, 20, "strides"
    elif "hill sprint" in text.lower():
        n, secs, what = 6, 10, "hill sprints"
    else:
        return []
    fast = step("interval", time_s=float(secs), desc=f"{what[:-1] if what.endswith('s') else what} fast and relaxed")
    rec = step("recovery", time_s=90.0 if "hill" in what else 60.0, desc="walk back")
    return [repeat(n, [fast, rec])]


def parse_session(text: str, km: float, z: Zones) -> tuple[str, list[dict[str, Any]]] | None:
    """Return (short label, steps) for a run session, or None for strength/rest."""
    code, _, rest = text.strip().partition(" ")
    code = code.upper()
    total = km * 1000.0
    rest_l = rest.lower()

    if code in ("E", "SHAKE"):
        main = step("interval", distance_m=total, hr=z.easy_hr, desc=f"easy, HR {z.easy_hr[1]} or below")
        extra = _strides(rest, z)
        label = "Easy" if code == "E" else "Shakeout"
        if extra:
            label += " + strides" if "stride" in rest_l else " + hills"
        return label, [main, *extra]

    if code == "L":
        steps: list[dict[str, Any]] = []
        m = LAST_RE.search(rest)
        b = BLOCKS_RE.search(rest)
        if m:
            fast_m = float(m.group(1)) * 1000
            easy_m = total - fast_m
            steps += _long_easy(easy_m, z)
            steps.append(step("interval", distance_m=fast_m, pace=z.zone(m.group(2)), desc=f"last {m.group(1)} km @{m.group(2).upper()}"))
            return f"Long + {m.group(1)} km @{m.group(2).upper()}", steps
        if b:
            n, each, zone = int(b.group(1)), float(b.group(2)) * 1000, b.group(3)
            float_m = 1000.0
            before = total - n * (each + float_m) - 1000.0  # n blocks, n floats, 1 km cooldown
            steps += _long_easy(max(before, 2000.0), z)
            inner = [step("interval", distance_m=each, pace=z.zone(zone), desc=f"{b.group(2)} km @{zone.upper()}"),
                     step("recovery", distance_m=float_m, hr=z.easy_hr, desc="easy float")]
            steps.append(repeat(n, inner))
            steps.append(step("cooldown", distance_m=1000.0, hr=z.easy_hr, desc="easy"))
            return f"Long + {n}x{b.group(2)} km @{zone.upper()}", steps
        return "Long easy", _long_easy(total, z)

    if code in ("T", "I", "MP"):
        c = CONT_RE.search(rest)
        r = REPS_RE.search(rest)
        if c:
            mins, zone = int(c.group(1)), c.group(2)
            work_m = mins * 60 / (sum(z.zone(zone)) / 2) * 1000
            wu, cd = _wu_cd(total, work_m)
            return f"{code} {mins} min", [
                step("warmup", distance_m=wu, hr=z.easy_hr, desc="easy"),
                step("interval", time_s=mins * 60.0, pace=z.zone(zone), desc=f"{mins} min @{zone.upper()}"),
                step("cooldown", distance_m=cd, hr=z.easy_hr, desc="easy"),
            ]
        if r:
            n, each, unit, zone = int(r.group(1)), float(r.group(2)), r.group(3).lower(), r.group(4)
            each_m = each * 1000 if unit == "km" else each
            rec = _dur(r.group(5)) if r.group(5) else 90.0
            wu, cd = _wu_cd(total, n * each_m)
            each_txt = f"{each:g} km" if unit == "km" else f"{int(each)} m"
            return f"{code} {n}x{each_txt}", [
                step("warmup", distance_m=wu, hr=z.easy_hr, desc="easy"),
                repeat(n, [
                    step("interval", distance_m=each_m, pace=z.zone(zone), desc=f"{each_txt} @{zone.upper()}"),
                    step("recovery", time_s=rec, desc="jog"),
                ]),
                step("cooldown", distance_m=cd, hr=z.easy_hr, desc="easy"),
            ]
        raise ValueError(f"cannot parse quality session: {text!r}")

    if code == "RACE":
        dist = 10000.0
        for key, d in RACE_DIST.items():
            if key in rest_l:
                dist = d
        m = re.search(r"(\d+(?:\.\d+)?)\s*km", rest)
        if m and "half" not in rest_l and "marathon" not in rest_l:
            dist = float(m.group(1)) * 1000
        wu = max(total - dist, 0.0)
        steps = []
        if wu > 0:
            steps.append(step("warmup", distance_m=wu / 2, hr=z.easy_hr, desc="easy"))
        steps.append(step("interval", distance_m=dist, desc="race"))
        if wu > 0:
            steps.append(step("cooldown", distance_m=wu / 2, hr=z.easy_hr, desc="easy"))
        return "Race", steps

    return None  # strength, rest, cross-training


def _long_easy(total_m: float, z: Zones) -> list[dict[str, Any]]:
    """Long runs: first 10 km at the easy cap, the rest at the long-run cap."""
    if total_m <= 12000:
        return [step("interval", distance_m=total_m, hr=z.easy_hr, desc=f"easy, HR {z.easy_hr[1]} or below")]
    return [
        step("interval", distance_m=10000.0, hr=z.easy_hr, desc=f"easy, HR {z.easy_hr[1]} or below"),
        step("interval", distance_m=total_m - 10000.0, hr=z.long_hr, desc=f"easy, HR {z.long_hr[1]} or below"),
    ]


# -- steps -> Garmin JSON --------------------------------------------------------

def _executable(s: dict[str, Any], order: int, child: int | None) -> dict[str, Any]:
    kind = s["kind"]
    if s.get("distance_m") is not None:
        end, value, unit = "distance", float(s["distance_m"]), KM_UNIT
    elif s.get("time_s") is not None:
        end, value, unit = "time", float(s["time_s"]), None
    else:
        end, value, unit = "lap.button", None, None
    out: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": STEP_TYPES[kind], "stepTypeKey": kind},
        "childStepId": child,
        "description": s.get("desc") or None,
        "endCondition": {"conditionTypeId": CONDITIONS[end], "conditionTypeKey": end},
        "endConditionValue": value,
        "preferredEndConditionUnit": unit,
        "targetType": {"workoutTargetTypeId": TARGETS["no.target"], "workoutTargetTypeKey": "no.target"},
        "targetValueOne": None,
        "targetValueTwo": None,
        "zoneNumber": None,
    }
    if s.get("pace"):
        slow, fast = s["pace"]
        out["targetType"] = {"workoutTargetTypeId": TARGETS["pace.zone"], "workoutTargetTypeKey": "pace.zone"}
        out["targetValueOne"] = 1000.0 / fast  # faster bound = higher speed
        out["targetValueTwo"] = 1000.0 / slow
    elif s.get("hr"):
        lo, hi = s["hr"]
        out["targetType"] = {"workoutTargetTypeId": TARGETS["heart.rate.zone"], "workoutTargetTypeKey": "heart.rate.zone"}
        out["targetValueOne"] = float(lo)
        out["targetValueTwo"] = float(hi)
    return out


def to_garmin_json(name: str, description: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    order = 0
    groups = 0

    def build(items: list[dict[str, Any]], child: int | None) -> list[dict[str, Any]]:
        nonlocal order, groups
        out = []
        for s in items:
            order += 1
            if s["kind"] == "repeat":
                groups += 1
                gid = groups
                node = {
                    "type": "RepeatGroupDTO",
                    "stepOrder": order,
                    "stepType": {"stepTypeId": STEP_TYPES["repeat"], "stepTypeKey": "repeat"},
                    "childStepId": gid,
                    "numberOfIterations": int(s["reps"]),
                    "smartRepeat": False,
                    "endCondition": {"conditionTypeId": CONDITIONS["iterations"], "conditionTypeKey": "iterations"},
                    "endConditionValue": float(s["reps"]),
                    "workoutSteps": build(s["steps"], gid),
                }
                out.append(node)
            else:
                out.append(_executable(s, order, child))
        return out

    body = build(steps, None)
    return {
        "workoutName": name,
        "description": description,
        "sportType": SPORT,
        "estimatedDistanceInMeters": steps_distance(steps),
        "workoutSegments": [{"segmentOrder": 1, "sportType": SPORT, "workoutSteps": body}],
    }


def steps_distance(steps: list[dict[str, Any]]) -> float:
    total = 0.0
    for s in steps:
        if s["kind"] == "repeat":
            total += s["reps"] * steps_distance(s["steps"])
        else:
            total += float(s.get("distance_m") or 0)
    return total


def describe(steps: list[dict[str, Any]]) -> str:
    parts = []
    for s in steps:
        if s["kind"] == "repeat":
            parts.append(f"{s['reps']}x({describe(s['steps'])})")
            continue
        length = f"{s['distance_m'] / 1000:.1f} km" if s.get("distance_m") else f"{int(s['time_s'])} s" if s.get("time_s") else "lap"
        if s.get("pace"):
            slow, fast = s["pace"]
            tgt = f" @ {fast // 60}:{fast % 60:02d}-{slow // 60}:{slow % 60:02d}"
        elif s.get("hr"):
            tgt = f" HR<={s['hr'][1]}"
        else:
            tgt = ""
        parts.append(f"{s['kind']} {length}{tgt}")
    return " | ".join(parts)


# -- push ----------------------------------------------------------------------

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def plan_workouts(store: Store, cfg: Config, source: str, start: date, end: date) -> list[dict[str, Any]]:
    """Build (but do not upload) the workouts for every run session in the window."""
    z = Zones(cfg.paces)
    out = []
    for p in store.planned(since=start, until=end, source=source):
        if p["workout_type"] in ("strength", "rest", "mobility", "walk"):
            continue
        km = float(p["distance_m"] or 0) / 1000.0
        parsed = parse_session(p["title"] or "", km, z)
        if parsed is None:
            continue
        label, steps = parsed
        d = date.fromisoformat(p["date"])
        import json as _json
        meta = (_json.loads(p["steps_json"] or "{}").get("meta") or {})
        wk = meta.get("plan_week")
        name = f"W{wk:02d} {DOW[d.weekday()]} {label}" if wk else f"{DOW[d.weekday()]} {label}"
        desc = f"{p['title']}\n{p['description'] or ''}".strip()
        out.append({"uid": p["uid"], "date": p["date"], "name": name, "steps": steps, "json": to_garmin_json(name, desc, steps)})
    return out


def push_workouts(g: Any, store: Store, workouts: list[dict[str, Any]], replace: bool = False, log: Callable[[str], None] = print) -> tuple[int, int]:
    created = skipped = 0
    already = {r["uid"]: r for r in store.pushed()}
    for w in workouts:
        prev = already.get(w["uid"])
        if prev and not replace:
            skipped += 1
            continue
        if prev:
            _remove(g, prev, log)
            store.forget_push(w["uid"])
        res = g.upload_workout(w["json"])
        workout_id = res.get("workoutId") if isinstance(res, dict) else None
        schedule_id = None
        if workout_id:
            sched = g.schedule_workout(workout_id, w["date"])
            schedule_id = sched.get("workoutScheduleId") if isinstance(sched, dict) else None
        store.record_push(w["uid"], w["date"], workout_id, schedule_id, w["name"])
        created += 1
        log(f"  {w['date']} {w['name']}: workout {workout_id}, schedule {schedule_id}")
    return created, skipped


def _remove(g: Any, row: Any, log: Callable[[str], None]) -> None:
    try:
        if row["schedule_id"]:
            g.unschedule_workout(row["schedule_id"])
        if row["workout_id"]:
            g.delete_workout(row["workout_id"])
    except Exception as e:  # noqa: BLE001
        log(f"  could not remove {row['name']} ({e})")


def unpush(g: Any, store: Store, start: date | None, end: date | None, log: Callable[[str], None] = print) -> int:
    n = 0
    for row in store.pushed(since=start.isoformat() if start else None, until=end.isoformat() if end else None):
        _remove(g, row, log)
        store.forget_push(row["uid"])
        n += 1
        log(f"  removed {row['date']} {row['name']}")
    return n


def move(g: Any, store: Store, from_day: date, to_day: date, log: Callable[[str], None] = print) -> int:
    """Reschedule the workouts fastr pushed for one day onto another day (a missed session done later)."""
    n = 0
    for row in store.pushed(since=from_day.isoformat(), until=from_day.isoformat()):
        if not row["workout_id"]:
            continue
        if row["schedule_id"]:
            try:
                g.unschedule_workout(row["schedule_id"])
            except Exception as e:  # noqa: BLE001
                log(f"  could not unschedule {row['name']} ({e})")
        sched = g.schedule_workout(row["workout_id"], to_day.isoformat())
        schedule_id = sched.get("workoutScheduleId") if isinstance(sched, dict) else None
        store.record_push(row["uid"], to_day.isoformat(), row["workout_id"], schedule_id, row["name"])
        n += 1
        log(f"  {row['name']}: {from_day} -> {to_day} (schedule {schedule_id})")
    return n
