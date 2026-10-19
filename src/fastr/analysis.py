"""Training analysis: weekly history, plan outlook, adherence, fitness, goal readiness.

Pure functions over store rows so they can be unit tested without Garmin.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from .config import Goal

Row = Mapping[str, Any]

STANDARD_DISTANCES_M = {
    "1k": 1000.0,
    "5k": 5000.0,
    "10k": 10000.0,
    "half": 21097.5,
    "marathon": 42195.0,
}

RUN_TYPE_KEYS = [
    ("race", ("race", "time trial", "parkrun")),
    ("long", ("long run", "long")),
    ("intervals", ("interval", "speed", "track", "reps", "fartlek", "hill", "vo2")),
    ("tempo", ("tempo", "threshold", "progression", "steady", "marathon pace", "race pace")),
    ("easy", ("easy", "recovery", "shakeout")),
]


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def classify_run_name(name: str | None) -> str | None:
    if not name:
        return None
    t = name.lower()
    for label, keys in RUN_TYPE_KEYS:
        if any(k in t for k in keys):
            return label
    return None


def pace_s_per_km(distance_m: float | None, duration_s: float | None) -> float | None:
    if not distance_m or not duration_s or distance_m <= 0:
        return None
    return duration_s / (distance_m / 1000.0)


# -- weekly history ------------------------------------------------------------

@dataclass
class WeekSummary:
    week_start: date
    runs: int = 0
    km: float = 0.0
    duration_s: float = 0.0
    long_run_km: float = 0.0
    long_run_name: str | None = None
    elev_gain_m: float = 0.0
    avg_pace: float | None = None
    avg_hr: float | None = None
    load: float = 0.0
    zone_s: list[float] = field(default_factory=lambda: [0.0] * 5)
    other_sessions: int = 0
    other_duration_s: float = 0.0
    km_by_type: dict[str, float] = field(default_factory=dict)
    run_names: list[str] = field(default_factory=list)

    @property
    def easy_share(self) -> float | None:
        total = sum(self.zone_s)
        if total <= 0:
            return None
        return (self.zone_s[0] + self.zone_s[1]) / total

    @property
    def hard_share(self) -> float | None:
        total = sum(self.zone_s)
        if total <= 0:
            return None
        return (self.zone_s[3] + self.zone_s[4]) / total

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["week_start"] = self.week_start.isoformat()
        d["easy_share"] = self.easy_share
        d["hard_share"] = self.hard_share
        return d


def weekly_summaries(activities: Sequence[Row], weeks: int, today: date | None = None) -> list[WeekSummary]:
    today = today or date.today()
    this_week = week_start(today)
    first_week = this_week - timedelta(weeks=weeks - 1)
    by_week: dict[date, WeekSummary] = {
        first_week + timedelta(weeks=i): WeekSummary(first_week + timedelta(weeks=i)) for i in range(weeks)
    }
    pace_num: dict[date, float] = defaultdict(float)
    hr_num: dict[date, float] = defaultdict(float)
    hr_den: dict[date, float] = defaultdict(float)

    for a in activities:
        d = date.fromisoformat(a["date"])
        ws = week_start(d)
        if ws not in by_week:
            continue
        w = by_week[ws]
        dur = float(a["duration_s"] or 0)
        if not a["is_run"]:
            w.other_sessions += 1
            w.other_duration_s += dur
            w.load += float(a["training_load"] or 0)
            continue
        km = float(a["distance_m"] or 0) / 1000.0
        w.runs += 1
        w.km += km
        w.duration_s += dur
        w.elev_gain_m += float(a["elevation_gain"] or 0)
        w.load += float(a["training_load"] or 0)
        w.run_names.append(a["name"] or "")
        if km > w.long_run_km:
            w.long_run_km = km
            w.long_run_name = a["name"]
        pace_num[ws] += dur
        if a["avg_hr"]:
            hr_num[ws] += float(a["avg_hr"]) * dur
            hr_den[ws] += dur
        for i in range(5):
            w.zone_s[i] += float(a[f"hr_z{i + 1}"] or 0)
        label = classify_run_name(a["name"]) or "unlabelled"
        w.km_by_type[label] = w.km_by_type.get(label, 0.0) + km

    for ws, w in by_week.items():
        if w.km > 0:
            w.avg_pace = pace_num[ws] / w.km
        if hr_den[ws] > 0:
            w.avg_hr = hr_num[ws] / hr_den[ws]
        # if no run was labelled long, treat the longest run of the week as the long run
        if "long" not in w.km_by_type and w.long_run_km > 0:
            lbl = classify_run_name(w.long_run_name) or "unlabelled"
            w.km_by_type[lbl] = w.km_by_type.get(lbl, 0.0) - w.long_run_km
            if w.km_by_type[lbl] <= 0.01:
                del w.km_by_type[lbl]
            w.km_by_type["long"] = w.long_run_km
    return [by_week[k] for k in sorted(by_week)]


def rolling_7day_max(activities: Sequence[Row]) -> tuple[float, date | None, date | None]:
    """Highest running distance in any 7 consecutive days (not calendar weeks)."""
    by_day: dict[date, float] = defaultdict(float)
    for a in activities:
        if a["is_run"]:
            by_day[date.fromisoformat(a["date"])] += float(a["distance_m"] or 0) / 1000.0
    if not by_day:
        return 0.0, None, None
    days = sorted(by_day)
    best, best_start = 0.0, days[0]
    for start in days:
        end = start + timedelta(days=6)
        total = sum(km for d, km in by_day.items() if start <= d <= end)
        if total > best:
            best, best_start = total, start
    return best, best_start, best_start + timedelta(days=6)


def rolling_average(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1): i + 1]
        out.append(sum(chunk) / len(chunk) if len(chunk) == window else None)
    return out


# -- plan outlook --------------------------------------------------------------

@dataclass
class PlanWeek:
    week_start: date
    sessions: int = 0
    run_sessions: int = 0
    km: float = 0.0
    duration_s: float = 0.0
    long_run_km: float = 0.0
    strength_sessions: int = 0
    key_sessions: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    unknown_distance: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["week_start"] = self.week_start.isoformat()
        return d


RUN_PLAN_TYPES = {"easy", "tempo", "intervals", "long", "race", "run"}


def plan_weeks(planned: Sequence[Row], weeks: int, today: date | None = None, start: date | None = None) -> list[PlanWeek]:
    today = today or date.today()
    first = week_start(start or today)
    by_week: dict[date, PlanWeek] = {
        first + timedelta(weeks=i): PlanWeek(first + timedelta(weeks=i)) for i in range(weeks)
    }
    for p in planned:
        d = date.fromisoformat(p["date"])
        ws = week_start(d)
        if ws not in by_week:
            continue
        w = by_week[ws]
        wtype = p["workout_type"] or "other"
        if wtype == "rest":
            continue
        w.sessions += 1
        w.titles.append(p["title"] or "")
        if wtype in ("strength", "walk", "mobility"):
            w.strength_sessions += 1
            continue
        if wtype in RUN_PLAN_TYPES or (p["distance_m"] or 0) > 0:
            w.run_sessions += 1
            km = float(p["distance_m"] or 0) / 1000.0
            if km <= 0:
                w.unknown_distance += 1
            w.km += km
            w.duration_s += float(p["duration_s"] or 0)
            if km > w.long_run_km:
                w.long_run_km = km
            if wtype in ("tempo", "intervals", "race", "long"):
                w.key_sessions.append(f"{wtype}: {p['title']}")
    return [by_week[k] for k in sorted(by_week)]


# -- adherence -----------------------------------------------------------------

@dataclass
class WeekAdherence:
    week_start: date
    planned_runs: int
    completed_runs: int
    planned_km: float
    actual_km: float
    missed: list[str]
    extra_runs: int

    @property
    def completion_pct(self) -> float | None:
        return (self.completed_runs / self.planned_runs * 100) if self.planned_runs else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["week_start"] = self.week_start.isoformat()
        d["completion_pct"] = self.completion_pct
        return d


def adherence(planned: Sequence[Row], activities: Sequence[Row], weeks: int, today: date | None = None) -> list[WeekAdherence]:
    """Match planned runs to actual runs inside the same week (Runna lets you reorder days)."""
    today = today or date.today()
    this_week = week_start(today)
    first = this_week - timedelta(weeks=weeks - 1)
    plan_by_week: dict[date, list[Row]] = defaultdict(list)
    runs_by_week: dict[date, list[Row]] = defaultdict(list)
    for p in planned:
        d = date.fromisoformat(p["date"])
        if d > today:
            continue
        if (p["workout_type"] or "run") in RUN_PLAN_TYPES:
            plan_by_week[week_start(d)].append(p)
    for a in activities:
        if a["is_run"]:
            runs_by_week[week_start(date.fromisoformat(a["date"]))].append(a)

    out: list[WeekAdherence] = []
    for i in range(weeks):
        ws = first + timedelta(weeks=i)
        plans = sorted(plan_by_week.get(ws, []), key=lambda p: -(p["distance_m"] or 0))
        runs = list(runs_by_week.get(ws, []))
        if not plans:
            continue  # the plan had not started (or the feed has no past events)
        used: set[int] = set()
        missed: list[str] = []
        completed = 0
        for p in plans:
            pd = float(p["distance_m"] or 0)
            best_idx, best_diff = None, None
            for idx, r in enumerate(runs):
                if idx in used:
                    continue
                rd = float(r["distance_m"] or 0)
                diff = abs(rd - pd) if pd > 0 else 0.0
                tol = max(1500.0, 0.35 * pd) if pd > 0 else float("inf")
                same_day = r["date"] == p["date"]
                if p["is_completed"] and same_day and (best_diff is None or diff < best_diff):
                    best_idx, best_diff = idx, diff  # Runna already confirmed this one
                elif diff <= tol and (best_diff is None or diff < best_diff):
                    best_idx, best_diff = idx, diff
            if p["is_completed"]:
                completed += 1
                if best_idx is not None:
                    used.add(best_idx)
            elif best_idx is None:
                missed.append(f"{p['date']} {p['title']}")
            else:
                used.add(best_idx)
                completed += 1
        out.append(
            WeekAdherence(
                week_start=ws,
                planned_runs=len(plans),
                completed_runs=completed,
                planned_km=sum(float(p["distance_m"] or 0) for p in plans) / 1000.0,
                actual_km=sum(float(r["distance_m"] or 0) for r in runs) / 1000.0,
                missed=missed,
                extra_runs=len(runs) - len(used),
            )
        )
    return out


# -- performance models --------------------------------------------------------

def riegel(t1_s: float, d1_m: float, d2_m: float, k: float = 1.06) -> float:
    return t1_s * (d2_m / d1_m) ** k


def vdot(distance_m: float, time_s: float) -> float:
    """Daniels & Gilbert VDOT for a race performance."""
    t_min = time_s / 60.0
    v = distance_m / t_min  # metres per minute
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)
    return vo2 / pct


def time_for_distance_at_vdot(distance_m: float, target_vdot: float) -> float:
    """Invert vdot() by bisection on time."""
    lo, hi = 60.0, 12 * 3600.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if vdot(distance_m, mid) > target_vdot:
            lo = mid  # too fast a time -> vdot too high -> need slower (more time)
        else:
            hi = mid
    return (lo + hi) / 2


def best_efforts_from_laps(
    activities: Sequence[Row], laps_for: Any, since: date, distances: Mapping[str, float] = STANDARD_DISTANCES_M
) -> dict[str, dict[str, Any]]:
    """Fastest contiguous-lap window covering each distance (approximate best efforts).

    Works with auto-laps (1 km) and workout laps; overshoot is scaled by pace.
    """
    best: dict[str, dict[str, Any]] = {}
    for a in activities:
        if not a["is_run"] or date.fromisoformat(a["date"]) < since:
            continue
        laps = [l for l in laps_for(a["activity_id"]) if (l["distance_m"] or 0) > 0 and (l["duration_s"] or 0) > 0]
        if not laps:
            # fall back to whole activity
            laps = [a] if (a["distance_m"] or 0) > 0 else []
        n = len(laps)
        for label, target in distances.items():
            if float(a["distance_m"] or 0) < target * 0.98:
                continue
            for i in range(n):
                dist = 0.0
                dur = 0.0
                for j in range(i, n):
                    dist += float(laps[j]["distance_m"])
                    dur += float(laps[j]["duration_s"])
                    if dist >= target:
                        # scale down the last lap's overshoot at that lap's pace
                        over = dist - target
                        lap_pace = float(laps[j]["duration_s"]) / float(laps[j]["distance_m"])
                        est = dur - over * lap_pace
                        if label not in best or est < best[label]["time_s"]:
                            best[label] = {
                                "time_s": est,
                                "distance_m": target,
                                "activity_id": a["activity_id"],
                                "date": a["date"],
                                "name": a["name"],
                                "vdot": vdot(target, est),
                            }
                        break
    return best


# -- aerobic decoupling --------------------------------------------------------

@dataclass
class Decoupling:
    """Pace-to-heart-rate drift between the first and second half of a steady run."""

    activity_id: int
    date: str
    name: str
    km: float
    duration_s: float
    pace1: float
    hr1: float
    pace2: float
    hr2: float
    pct: float

    @property
    def verdict(self) -> str:
        if self.pct <= 5:
            return "easy"
        if self.pct <= 8:
            return "borderline"
        return "too fast"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict
        return d


def decoupling_from_laps(laps: Sequence[Row]) -> tuple[float, float, float, float, float] | None:
    """Return (pace1, hr1, pace2, hr2, pct) or None when the laps cannot support it.

    Halves are split by distance. pct = (EF1 - EF2) / EF1 * 100 where EF is speed
    divided by heart rate (Friel's Pa:HR decoupling). Positive means the heart rate
    drifted up relative to pace; under 5 percent is the usual mark of a truly easy run.
    """
    valid = [l for l in laps if (l["distance_m"] or 0) > 0 and (l["duration_s"] or 0) > 0 and (l["avg_hr"] or 0) > 0]
    if len(valid) < 4:
        return None
    total = sum(float(l["distance_m"]) for l in valid)
    first: list[Row] = []
    second: list[Row] = []
    acc = 0.0
    for l in valid:
        (first if acc < total / 2 else second).append(l)
        acc += float(l["distance_m"])
    if not first or not second:
        return None

    def agg(ls: Sequence[Row]) -> tuple[float, float, float]:
        d = sum(float(l["distance_m"]) for l in ls)
        t = sum(float(l["duration_s"]) for l in ls)
        hr = sum(float(l["avg_hr"]) * float(l["duration_s"]) for l in ls) / t
        return t / (d / 1000.0), hr, d / t

    p1, h1, v1 = agg(first)
    p2, h2, v2 = agg(second)
    ef1, ef2 = v1 / h1, v2 / h2
    return p1, h1, p2, h2, (ef1 - ef2) / ef1 * 100.0


def lap_pace_cv(laps: Sequence[Row]) -> float | None:
    """Coefficient of variation of lap pace; high values mean a structured session."""
    paces = [
        float(l["duration_s"]) / (float(l["distance_m"]) / 1000.0)
        for l in laps
        if (l["distance_m"] or 0) >= 300 and (l["duration_s"] or 0) > 0
    ]
    if len(paces) < 3:
        return None
    mean = sum(paces) / len(paces)
    var = sum((p - mean) ** 2 for p in paces) / len(paces)
    return (var ** 0.5) / mean if mean else None


def decoupling_report(
    activities: Sequence[Row],
    laps_for: Any,
    since: date,
    min_minutes: int = 45,
    max_cv: float = 0.12,
) -> list[Decoupling]:
    """Decoupling for steady runs (easy, long, unlabelled) of at least ``min_minutes``."""
    out: list[Decoupling] = []
    for a in activities:
        if not a["is_run"] or date.fromisoformat(a["date"]) < since:
            continue
        if (a["duration_s"] or 0) < min_minutes * 60:
            continue
        label = classify_run_name(a["name"])
        if label in ("intervals", "tempo", "race"):
            continue
        laps = laps_for(a["activity_id"])
        cv = lap_pace_cv(laps)
        if cv is not None and cv > max_cv:
            continue  # structured session hiding behind a generic name
        res = decoupling_from_laps(laps)
        if res is None:
            continue
        p1, h1, p2, h2, pct = res
        out.append(
            Decoupling(
                activity_id=int(a["activity_id"]),
                date=a["date"],
                name=a["name"] or "",
                km=float(a["distance_m"] or 0) / 1000.0,
                duration_s=float(a["duration_s"] or 0),
                pace1=p1,
                hr1=h1,
                pace2=p2,
                hr2=h2,
                pct=pct,
            )
        )
    return out


# -- fitness markers -----------------------------------------------------------

def _latest(rows: Sequence[Row], col: str, before: date | None = None) -> tuple[str, Any] | None:
    for r in reversed(rows):
        if r[col] is None:
            continue
        if before and date.fromisoformat(r["date"]) > before:
            continue
        return r["date"], r[col]
    return None


def _avg(rows: Sequence[Row], col: str, since: date, until: date) -> float | None:
    vals = [float(r[col]) for r in rows if r[col] is not None and since <= date.fromisoformat(r["date"]) <= until]
    return sum(vals) / len(vals) if vals else None


def fitness_markers(daily: Sequence[Row], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    out: dict[str, Any] = {}
    for col in ("vo2max", "lt_hr", "lt_speed", "training_status", "acwr", "acute_load", "chronic_load",
                "hrv_status", "hrv_weekly_avg", "readiness_score", "readiness_level",
                "endurance_score", "hill_score", "pred_5k", "pred_10k", "pred_half", "pred_marathon"):
        out[col] = _latest(daily, col)
    for col in ("vo2max", "pred_5k", "pred_10k", "pred_half", "pred_marathon"):
        out[f"{col}_4w_ago"] = _latest(daily, col, before=today - timedelta(weeks=4))
        out[f"{col}_12w_ago"] = _latest(daily, col, before=today - timedelta(weeks=12))
    out["rhr_7d"] = _avg(daily, "rhr", today - timedelta(days=6), today)
    out["rhr_28d"] = _avg(daily, "rhr", today - timedelta(days=27), today)
    out["hrv_7d"] = _avg(daily, "hrv_last_night", today - timedelta(days=6), today)
    out["sleep_7d_h"] = (_avg(daily, "sleep_seconds", today - timedelta(days=6), today) or 0) / 3600 or None
    out["readiness_7d"] = _avg(daily, "readiness_score", today - timedelta(days=6), today)
    lt_speed = out["lt_speed"][1] if out["lt_speed"] else None
    out["lt_pace"] = lt_pace_from_speed(lt_speed)
    out["run_tolerance_phrase"] = _latest(daily, "run_tolerance_phrase")
    out["acute_distance_m"] = _latest(daily, "acute_distance_m")
    out["acute_tolerance_m"] = _latest(daily, "acute_tolerance_m")
    return out


def lt_pace_from_speed(speed: Any) -> float | None:
    """Garmin reports lactate-threshold speed in decimetres per second (0.325 -> 3.25 m/s)."""
    if not speed:
        return None
    v = float(speed)
    if v < 1.5:
        v *= 10.0
    return 1000.0 / v if v > 0 else None


def load_ratio(activities: Sequence[Row], today: date | None = None) -> dict[str, float | None]:
    """Acute (7-day) vs chronic (28-day weekly average) load from Garmin's per-activity load."""
    today = today or date.today()
    acute = chronic = 0.0
    for a in activities:
        d = date.fromisoformat(a["date"])
        age = (today - d).days
        load = float(a["training_load"] or 0)
        if 0 <= age < 7:
            acute += load
        if 0 <= age < 28:
            chronic += load
    chronic_weekly = chronic / 4.0
    return {
        "load_7d": acute,
        "load_28d_weekly_avg": chronic_weekly,
        "computed_acwr": (acute / chronic_weekly) if chronic_weekly > 0 else None,
    }


# -- goal assessment -----------------------------------------------------------

def predicted_time_from_garmin(markers: Mapping[str, Any], distance_m: float) -> float | None:
    preds = {
        5000.0: markers.get("pred_5k"),
        10000.0: markers.get("pred_10k"),
        21097.5: markers.get("pred_half"),
        42195.0: markers.get("pred_marathon"),
    }
    preds = {d: float(v[1]) for d, v in preds.items() if v and v[1]}
    if not preds:
        return None
    if distance_m in preds:
        return preds[distance_m]
    nearest = min(preds, key=lambda d: abs(math.log(d / distance_m)))
    return riegel(preds[nearest], nearest, distance_m)


@dataclass
class GoalAssessment:
    goal: Goal
    weeks_to_go: float
    target_time_s: int | None
    target_pace: float | None
    garmin_pred_s: float | None
    garmin_pred_4w_ago_s: float | None
    effort_pred_s: float | None
    effort_source: str | None
    effort_vdot: float | None
    required_vdot: float | None
    gap_pct: float | None
    lt_pace: float | None
    pace_vs_lt_pct: float | None
    current_4w_avg_km: float | None
    plan_peak_km: float | None
    plan_peak_long_run_km: float | None
    verdict: str
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["goal"] = {
            "name": self.goal.name,
            "date": self.goal.date.isoformat(),
            "distance_km": self.goal.distance_km,
            "target_time": self.goal.target_time,
            "priority": self.goal.priority,
        }
        return d


def assess_goal(
    goal: Goal,
    markers: Mapping[str, Any],
    best: Mapping[str, Mapping[str, Any]],
    weekly: Sequence[WeekSummary],
    plan: Sequence[PlanWeek],
    today: date | None = None,
) -> GoalAssessment:
    today = today or date.today()
    distance_m = goal.distance_km * 1000.0
    notes: list[str] = []
    target = goal.target_seconds
    garmin_pred = predicted_time_from_garmin(markers, distance_m)
    old_markers = {k.replace("_4w_ago", ""): v for k, v in markers.items() if k.endswith("_4w_ago")}
    garmin_pred_old = predicted_time_from_garmin(old_markers, distance_m)

    effort_pred = effort_src = effort_vdot = None
    # short efforts (1 km) extrapolate badly to long races; prefer 5 km and up
    candidates = [b for b in best.values() if b["distance_m"] >= 5000] or list(best.values())
    if candidates:
        top = max(candidates, key=lambda b: b["vdot"])
        effort_vdot = top["vdot"]
        effort_pred = time_for_distance_at_vdot(distance_m, effort_vdot)
        effort_src = f"{top['name']} on {top['date']} ({top['distance_m'] / 1000:.1f} km in {top['time_s'] / 60:.1f} min)"

    required_vdot = vdot(distance_m, target) if target else None
    ref_pred = garmin_pred if garmin_pred else effort_pred
    gap = ((ref_pred - target) / target * 100) if (ref_pred and target) else None

    lt_pace = markers.get("lt_pace")
    pace_vs_lt = None
    if lt_pace and goal.target_pace:
        pace_vs_lt = (goal.target_pace - lt_pace) / lt_pace * 100

    recent = [w.km for w in weekly[-5:-1]] if len(weekly) >= 5 else [w.km for w in weekly[:-1]]
    cur_avg = sum(recent) / len(recent) if recent else None
    future = [w for w in plan if w.week_start <= goal.date]
    peak_km = max((w.km for w in future), default=None)
    peak_long = max((w.long_run_km for w in future), default=None)

    weeks_to_go = goal.weeks_to_go(today)
    if weeks_to_go < 0:
        verdict = "past"
    elif target is None or gap is None:
        verdict = "no target or no prediction"
    elif gap <= 1.0:
        verdict = "on track"
    elif gap <= 5.0:
        verdict = "borderline"
    else:
        verdict = "behind"

    if gap is not None:
        notes.append(f"Prediction is {gap:+.1f}% versus target (negative = faster than needed).")
    if garmin_pred and garmin_pred_old:
        delta = garmin_pred - garmin_pred_old
        notes.append(f"Garmin prediction moved {delta / 60:+.1f} min over the last 4 weeks.")
    if pace_vs_lt is not None:
        notes.append(f"Goal pace is {pace_vs_lt:+.1f}% relative to current lactate-threshold pace.")
    if cur_avg is not None and peak_km:
        notes.append(f"Current 4-week average {cur_avg:.0f} km; plan peaks at {peak_km:.0f} km/week.")

    return GoalAssessment(
        goal=goal,
        weeks_to_go=weeks_to_go,
        target_time_s=target,
        target_pace=goal.target_pace,
        garmin_pred_s=garmin_pred,
        garmin_pred_4w_ago_s=garmin_pred_old,
        effort_pred_s=effort_pred,
        effort_source=effort_src,
        effort_vdot=effort_vdot,
        required_vdot=required_vdot,
        gap_pct=gap,
        lt_pace=lt_pace,
        pace_vs_lt_pct=pace_vs_lt,
        current_4w_avg_km=cur_avg,
        plan_peak_km=peak_km,
        plan_peak_long_run_km=peak_long,
        verdict=verdict,
        notes=notes,
    )


# -- red flags -----------------------------------------------------------------

def flags(weekly: Sequence[WeekSummary], markers: Mapping[str, Any], adher: Sequence[WeekAdherence]) -> list[str]:
    out: list[str] = []
    done = [w for w in weekly[:-1] if w.km > 0]
    # compare against the highest of the previous three weeks so a planned down
    # week followed by a normal week is not reported as a jump
    for i in range(1, len(done)):
        cur = done[i]
        ref = max(w.km for w in done[max(0, i - 3):i])
        if ref >= 20 and cur.km > ref * 1.2:
            out.append(f"Week of {cur.week_start}: volume jumped {((cur.km / ref) - 1) * 100:.0f}% above the recent high ({ref:.0f} -> {cur.km:.0f} km).")
    for w in done[-8:]:
        if w.km >= 25 and w.long_run_km > 0.5 * w.km:
            out.append(f"Week of {w.week_start}: long run is {w.long_run_km / w.km * 100:.0f}% of the week's volume.")
        hard = w.hard_share
        if hard is not None and hard > 0.3 and w.runs >= 3:
            out.append(f"Week of {w.week_start}: {hard * 100:.0f}% of running time in HR zones 4-5.")
    acwr = markers.get("acwr")
    if acwr and acwr[1] and float(acwr[1]) > 1.4:
        out.append(f"Acute:chronic load ratio is {float(acwr[1]):.2f} (>1.4) as of {acwr[0]}.")
    tol = markers.get("run_tolerance_phrase")
    if tol and tol[1] and "ABOVE" in str(tol[1]).upper():
        ad, at = markers.get("acute_distance_m"), markers.get("acute_tolerance_m")
        extra = f" ({ad[1] / 1000:.0f} km in 7 days vs tolerance {at[1] / 1000:.0f} km)" if ad and at and ad[1] and at[1] else ""
        out.append(f"Garmin running tolerance: {tol[1]} as of {tol[0]}{extra}.")
    rd = markers.get("readiness_7d")
    if rd is not None and rd < 35:
        out.append(f"Training readiness averaged {rd:.0f} over the last 7 days.")
    hs = markers.get("hrv_status")
    if hs and hs[1] and str(hs[1]).upper() in ("LOW", "UNBALANCED", "POOR"):
        out.append(f"HRV status is {hs[1]} as of {hs[0]}.")
    rhr7, rhr28 = markers.get("rhr_7d"), markers.get("rhr_28d")
    if rhr7 and rhr28 and rhr7 > rhr28 + 4:
        out.append(f"Resting HR 7-day average ({rhr7:.0f}) is {rhr7 - rhr28:.0f} bpm above the 28-day average.")
    for a in adher[-4:]:
        pct = a.completion_pct
        if pct is not None and pct < 70:
            out.append(f"Week of {a.week_start}: only {a.completed_runs}/{a.planned_runs} planned runs completed.")
    return out
