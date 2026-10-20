"""Build the analysis report (markdown for reading, JSON for machines)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import analysis as an
from .config import Config, Goal, fmt_hms, fmt_pace
from .runna import describe_steps as describe_runna_steps
from .store import Store

PR_TYPE_NAMES = {
    1: "1 km", 2: "1 mile", 3: "5 km", 4: "10 km", 5: "Half marathon", 6: "Marathon",
    7: "Longest run", 8: "Longest ride", 9: "Most ascent (ride)", 10: "Max avg power 20 min",
    11: "Most steps (day)", 12: "Most steps (week)", 13: "Most steps (month)",
    14: "Longest goal streak", 15: "Fastest 40 km ride", 16: "Longest swim",
}


def _pace_from_speed(v: Any) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return fmt_pace(1000.0 / v) if v > 0 else "?"


def describe_steps(steps_json: str | None) -> str:
    """Compact one-line description of a Garmin structured workout."""
    if not steps_json:
        return ""
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError:
        return ""
    parts: list[str] = []
    i = 0
    while i < len(steps):
        s = steps[i]
        if s.get("kind") == "repeat":
            depth = s.get("depth", 0)
            inner = []
            j = i + 1
            while j < len(steps) and steps[j].get("depth", 0) > depth:
                inner.append(_describe_step(steps[j]))
                j += 1
            parts.append(f"{s.get('iterations') or '?'}x({', '.join(p for p in inner if p)})")
            i = j
            continue
        d = _describe_step(s)
        if d:
            parts.append(d)
        i += 1
    return " | ".join(parts)


def _describe_step(s: Mapping[str, Any]) -> str:
    kind = (s.get("kind") or "step").replace("_", " ")
    end = s.get("end")
    val = s.get("end_value")
    if end == "time" and val:
        length = fmt_hms(float(val))
    elif end == "distance" and val:
        length = f"{float(val) / 1000:.2f} km"
    elif end == "lap.button":
        length = "until lap"
    else:
        length = ""
    target = s.get("target")
    lo, hi = s.get("target_low"), s.get("target_high")
    if target == "pace.zone" and lo and hi:
        tgt = f"@ {_pace_from_speed(hi)}-{_pace_from_speed(lo)}"
    elif target == "heart.rate.zone" and lo and hi:
        tgt = f"@ HR {int(float(lo))}-{int(float(hi))}"
    elif target == "heart.rate.zone" and (s.get('target_low') is not None):
        tgt = f"@ HR zone {s.get('target_low')}"
    elif target and target != "no.target":
        tgt = f"@ {target} {lo or ''}-{hi or ''}".strip()
    else:
        tgt = ""
    return " ".join(x for x in (kind, length, tgt) if x)


def _runna_detail(p: Mapping[str, Any]) -> str:
    try:
        sj = json.loads(p["steps_json"] or "{}")
    except json.JSONDecodeError:
        return p["description"] or ""
    steps = sj.get("steps") or []
    if steps:
        return describe_runna_steps(steps)
    meta = sj.get("meta") or {}
    actual = meta.get("actual") or {}
    if actual.get("actual_distance_m"):
        return f"actual {actual['actual_distance_m'] / 1000:.2f} km in {fmt_hms(actual.get('actual_duration_s'))}"
    return (p["description"] or "").split("\n")[0]


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def _hm(seconds: float | None) -> str:
    if not seconds:
        return "-"
    h, m = divmod(int(seconds // 60), 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


def _pick_plan_source(store: Store) -> str | None:
    if store.planned(source="runna_ical"):
        return "runna_ical"
    if store.planned(source="garmin_calendar"):
        return "garmin_calendar"
    return None


def build_report(
    store: Store,
    cfg: Config,
    weeks: int = 26,
    plan_weeks: int = 25,
    today: date | None = None,
    plan_source: str | None = None,
) -> tuple[str, dict[str, Any]]:
    today = today or date.today()
    hist_start = an.week_start(today) - timedelta(weeks=weeks - 1)
    activities = store.activities(since=hist_start)
    all_runs = store.activities(runs_only=True)
    weekly = an.weekly_summaries(activities, weeks, today)
    daily = store.daily(since=today - timedelta(weeks=20))
    markers = an.fitness_markers(daily, today)
    best = an.best_efforts_from_laps(activities, store.laps, since=today - timedelta(weeks=12))

    loads = an.load_ratio(activities, today)
    plan_source = plan_source or cfg.plan_source or _pick_plan_source(store)
    planned_all = store.planned(source=plan_source) if plan_source else []
    plan = an.plan_weeks(planned_all, plan_weeks, today)
    adher = an.adherence(planned_all, activities, weeks=8, today=today)
    runna_plan = store.latest_snapshot("runna_plan") or {}
    if not isinstance(runna_plan, dict):
        runna_plan = {}
    race = runna_plan.get("race")
    if plan_source and plan_source != "runna_ical":
        meta = store.latest_snapshot(f"{plan_source}_meta")
        if isinstance(meta, dict) and meta.get("plan_start"):
            runna_plan = {**runna_plan, "plan_start": meta["plan_start"], "plan_weeks": meta.get("plan_weeks")}
    goal_list = list(cfg.goals)
    if not goal_list and race and race.get("date"):
        tgt = None
        paces = runna_plan.get("target_paces") or {}
        if paces and race.get("distance_m"):
            tgt = fmt_hms(list(paces.values())[0] * race["distance_m"] / 1000.0)
        goal_list.append(
            Goal(
                name=race.get("name") or "Race",
                date=date.fromisoformat(race["date"]),
                distance_km=(race.get("distance_m") or 0) / 1000.0,
                target_time=tgt,
            )
        )
    goals = [an.assess_goal(g, markers, best, weekly, plan, today) for g in goal_list]
    red_flags = an.flags(weekly, markers, adher)
    plan_start = date.fromisoformat(runna_plan["plan_start"]) if runna_plan.get("plan_start") else None

    upcoming = store.planned(since=today, until=today + timedelta(days=14))
    prs = store.latest_snapshot("personal_records") or []
    profile = store.latest_snapshot("profile") or {}

    # ---------------------------------------------------------------- markdown
    md: list[str] = []
    md.append(f"# fastr training report, {today.isoformat()}")
    md.append("")
    counts = store.counts()
    first_run = all_runs[0]["date"] if all_runs else "-"
    last_run = all_runs[-1]["date"] if all_runs else "-"
    md.append(
        f"Data: {counts['runs']} runs ({first_run} .. {last_run}), {counts['activities']} activities total, "
        f"{counts['daily']} days of metrics, {counts['planned_workouts']} planned workouts "
        f"(plan source: {plan_source or 'none'})."
    )
    last = store.last_sync()
    if last:
        md.append(f"Last sync: {last['ts']} ({last['kind']} {last['info'] or ''}).")
    md.append("")

    # goals
    md.append("## Goals")
    md.append("")
    if not goals:
        md.append("No goals configured. Add `[[goals]]` blocks to fastr.toml.")
    for ga in goals:
        g = ga.goal
        md.append(f"### {g.name}, {g.date.isoformat()} ({g.distance_km:g} km, {ga.weeks_to_go:.1f} weeks to go): **{ga.verdict}**")
        md.append("")
        rows = [
            ["Target", fmt_hms(ga.target_time_s), fmt_pace(ga.target_pace)],
            ["Garmin race prediction (now)", fmt_hms(ga.garmin_pred_s), fmt_pace(ga.garmin_pred_s / g.distance_km if ga.garmin_pred_s else None)],
            ["Garmin race prediction (4 wks ago)", fmt_hms(ga.garmin_pred_4w_ago_s), ""],
            ["Equivalent from best recent effort", fmt_hms(ga.effort_pred_s), fmt_pace(ga.effort_pred_s / g.distance_km if ga.effort_pred_s else None)],
            ["Current VDOT (best effort) / required", f"{ga.effort_vdot:.1f}" if ga.effort_vdot else "-", f"{ga.required_vdot:.1f}" if ga.required_vdot else "-"],
            ["Lactate threshold pace", fmt_pace(ga.lt_pace), f"goal pace {ga.pace_vs_lt_pct:+.1f}% vs LT" if ga.pace_vs_lt_pct is not None else ""],
            ["4-week avg volume / plan peak", f"{ga.current_4w_avg_km:.0f} km" if ga.current_4w_avg_km is not None else "-", f"{ga.plan_peak_km:.0f} km (long run {ga.plan_peak_long_run_km:.0f} km)" if ga.plan_peak_km else "-"],
        ]
        md.append(_table(["Metric", "Value", "Pace / note"], rows))
        if ga.effort_source:
            md.append("")
            md.append(f"Best recent effort used: {ga.effort_source}.")
        for n in ga.notes:
            md.append(f"- {n}")
        md.append("")

    # what the Runna plan says about itself
    if runna_plan:
        md.append("## Runna plan")
        md.append("")
        if race:
            window = ""
            if race.get("duration_low_s") and race.get("duration_high_s"):
                window = f", Runna race window {fmt_hms(race['duration_low_s'])} to {fmt_hms(race['duration_high_s'])}"
            paces = ""
            if race.get("pace_low_s") and race.get("pace_high_s"):
                paces = f" at {fmt_pace(race['pace_low_s'])} to {fmt_pace(race['pace_high_s'])}"
            md.append(f"- Race: {race.get('name')} on {race.get('date')} ({(race.get('distance_m') or 0) / 1000:.1f} km){paces}{window}.")
        for label, sec in (runna_plan.get("target_paces") or {}).items():
            md.append(f"- Target {label} pace in the plan: {fmt_pace(sec)}.")
        if plan_start:
            wk = (an.week_start(today) - plan_start).days // 7 + 1
            md.append(f"- Plan started {plan_start.isoformat()}; this is plan week {wk} of {runna_plan.get('plan_weeks') or '?'}.")
        if runna_plan.get("last_upcoming"):
            md.append(f"- Feed covers upcoming sessions {runna_plan.get('first_upcoming')} to {runna_plan.get('last_upcoming')}.")
        md.append("")

    # fitness markers
    md.append("## Fitness markers")
    md.append("")

    def mv(key: str, fmt=lambda v: v) -> str:
        v = markers.get(key)
        if not v:
            return "-"
        return f"{fmt(v[1])} ({v[0]})"

    rows = [
        ["VO2max (running)", mv("vo2max", lambda v: f"{float(v):.1f}"), f"4w ago: {mv('vo2max_4w_ago', lambda v: f'{float(v):.1f}')}, 12w ago: {mv('vo2max_12w_ago', lambda v: f'{float(v):.1f}')}"],
        ["Lactate threshold", mv("lt_hr", lambda v: f"{int(v)} bpm"), fmt_pace(markers.get("lt_pace"))],
        ["Training status", mv("training_status"), f"ACWR {mv('acwr', lambda v: f'{float(v):.2f}')}"],
        ["Acute / chronic load", mv("acute_load", lambda v: f"{float(v):.0f}"), mv("chronic_load", lambda v: f"{float(v):.0f}")],
        ["Load 7d / 28d weekly avg (computed)", f"{loads['load_7d']:.0f}" if loads.get("load_7d") is not None else "-",
         f"{loads['load_28d_weekly_avg']:.0f} (ratio {loads['computed_acwr']:.2f})" if loads.get("computed_acwr") else "-"],
        ["Running tolerance", mv("run_tolerance_phrase"),
         f"{markers['acute_distance_m'][1] / 1000:.0f} km in 7d vs tolerance {markers['acute_tolerance_m'][1] / 1000:.0f} km"
         if markers.get("acute_distance_m") and markers.get("acute_tolerance_m") else "-"],
        ["Resting HR 7d / 28d", f"{markers['rhr_7d']:.0f}" if markers.get("rhr_7d") else "-", f"{markers['rhr_28d']:.0f}" if markers.get("rhr_28d") else "-"],
        ["HRV status / 7d avg", mv("hrv_status"), f"{markers['hrv_7d']:.0f} ms" if markers.get("hrv_7d") else "-"],
        ["Training readiness (latest / 7d)", mv("readiness_score"), f"{markers['readiness_7d']:.0f}" if markers.get("readiness_7d") else "-"],
        ["Sleep 7d avg", f"{markers['sleep_7d_h']:.1f} h" if markers.get("sleep_7d_h") else "-", ""],
        ["Endurance / hill score", mv("endurance_score"), mv("hill_score")],
        ["Garmin predictions 5k / 10k", mv("pred_5k", fmt_hms), mv("pred_10k", fmt_hms)],
        ["Garmin predictions half / marathon", mv("pred_half", fmt_hms), mv("pred_marathon", fmt_hms)],
    ]
    zones = store.latest_snapshot("hr_zones") or []
    if isinstance(zones, list) and zones:
        z = zones[0]
        try:
            rows.append([
                "HR zones (Garmin)",
                f"Z2 {z['zone2Floor']}-{z['zone3Floor'] - 1}, Z3 {z['zone3Floor']}-{z['zone4Floor'] - 1}, Z4 {z['zone4Floor']}-{z['zone5Floor'] - 1}, Z5 {z['zone5Floor']}+",
                f"method {z.get('trainingMethod')}, LT {z.get('lactateThresholdHeartRateUsed')}, max {z.get('maxHeartRateUsed')}",
            ])
        except (KeyError, TypeError):
            pass
    md.append(_table(["Marker", "Latest", "Context"], rows))
    md.append("")

    md.append("## Best efforts in the last 12 weeks (from laps, approximate)")
    md.append("")
    if best:
        rows = [
            [k, fmt_hms(b["time_s"]), fmt_pace(b["time_s"] / (b["distance_m"] / 1000)), f"{b['vdot']:.1f}", f"{b['date']} {b['name']}"]
            for k, b in best.items()
        ]
        md.append(_table(["Distance", "Time", "Pace", "VDOT", "Activity"], rows))
    else:
        md.append("No lap data yet (run `fastr sync`).")
    md.append("")

    # aerobic decoupling
    decoup = an.decoupling_report(activities, store.laps, since=today - timedelta(weeks=8))
    md.append("## Aerobic decoupling (steady runs of 45 min or more, last 8 weeks)")
    md.append("")
    if decoup:
        rows = [
            [d.date, d.name[:40], f"{d.km:.1f}", _hm(d.duration_s),
             f"{fmt_pace(d.pace1)} @ {d.hr1:.0f}", f"{fmt_pace(d.pace2)} @ {d.hr2:.0f}", f"{d.pct:+.1f}%", d.verdict]
            for d in decoup
        ]
        md.append(_table(["Date", "Run", "km", "Time", "First half", "Second half", "Drift", "Verdict"], rows))
        vals = sorted(d.pct for d in decoup)
        median = vals[len(vals) // 2]
        md.append("")
        md.append(
            f"Median drift {median:+.1f}% over {len(decoup)} runs. Under 5% the pace was genuinely easy; "
            "over 8% the heart rate climbed away from the pace, so the run was faster than easy whatever it felt like."
        )
    else:
        md.append("No steady runs of 45 minutes or more with lap heart-rate data in the window.")
    md.append("")

    # weekly history
    md.append(f"## Weekly history (last {weeks} weeks, Monday-start)")
    md.append("")
    rows = []
    for w in weekly:
        types = ", ".join(f"{k} {v:.0f}" for k, v in sorted(w.km_by_type.items(), key=lambda kv: -kv[1]))
        rows.append([
            w.week_start.isoformat(), w.runs, f"{w.km:.1f}", _hm(w.duration_s), f"{w.long_run_km:.1f}",
            fmt_pace(w.avg_pace), f"{w.avg_hr:.0f}" if w.avg_hr else "-", f"{w.elev_gain_m:.0f}",
            f"{w.load:.0f}" if w.load else "-", f"{w.easy_share * 100:.0f}%" if w.easy_share is not None else "-",
            f"{w.hard_share * 100:.0f}%" if w.hard_share is not None else "-",
            w.other_sessions, types,
        ])
    md.append(_table(["Week", "Runs", "km", "Time", "Long", "Pace", "HR", "Elev", "Load", "Z1-2", "Z4-5", "Other", "km by type"], rows))
    md.append("")
    r7, r7a, r7b = an.rolling_7day_max(all_runs)
    longest = max(all_runs, key=lambda a: float(a["distance_m"] or 0)) if all_runs else None
    if longest:
        md.append(
            f"All-time in store: highest rolling 7-day volume {r7:.1f} km ({r7a} to {r7b}); "
            f"longest run {float(longest['distance_m']) / 1000:.2f} km on {longest['date']} ({longest['name']})."
        )
        md.append("")

    # adherence
    md.append("## Plan adherence (last 8 weeks)")
    md.append("")
    if adher:
        rows = [
            [a.week_start.isoformat(), f"{a.completed_runs}/{a.planned_runs}", f"{a.completion_pct:.0f}%" if a.completion_pct is not None else "-",
             f"{a.planned_km:.0f}", f"{a.actual_km:.0f}", a.extra_runs, "; ".join(a.missed) or "-"]
            for a in adher
        ]
        md.append(_table(["Week", "Done/planned", "%", "Planned km", "Actual km", "Extra runs", "Missed"], rows))
    else:
        md.append("No planned workouts in the history window (sync the Runna calendar).")
    md.append("")

    # plan outlook
    md.append(f"## Plan outlook (next {plan_weeks} weeks, source: {plan_source or 'none'})")
    md.append("")
    if plan_source:
        rows = []
        for w in plan:
            if w.sessions == 0:
                continue
            km = f"{w.km:.0f}" + (f" (+{w.unknown_distance} w/o distance)" if w.unknown_distance else "")
            wk = f"{(w.week_start - plan_start).days // 7 + 1}" if plan_start else "-"
            rows.append([w.week_start.isoformat(), wk, w.run_sessions, km, _hm(w.duration_s), f"{w.long_run_km:.0f}", w.strength_sessions, "; ".join(w.key_sessions) or "-"])
        md.append(_table(["Week", "Plan wk", "Runs", "km", "Time", "Long", "Strength", "Key sessions"], rows))
        horizon = max((date.fromisoformat(p["date"]) for p in planned_all), default=None)
        if horizon:
            md.append("")
            md.append(f"Plan visible until {horizon.isoformat()} ({(horizon - today).days / 7:.1f} weeks ahead).")
    else:
        md.append("No plan loaded. Run `fastr runna` with your calendar URL.")
    md.append("")

    # next 14 days
    md.append("## Next 14 days")
    md.append("")
    if upcoming:
        rows = []
        for p in upcoming:
            detail = describe_steps(p["steps_json"]) if p["source"] == "garmin_calendar" else _runna_detail(p)
            detail = " ".join(detail.split())
            if len(detail) > 200:
                detail = detail[:197] + "..."
            title = ("[done] " if p["is_completed"] else "") + (p["title"] or "")
            rows.append([p["date"], p["source"].replace("_", " "), title, p["workout_type"],
                         f"{(p['distance_m'] or 0) / 1000:.1f}" if p["distance_m"] else "-", detail])
        md.append(_table(["Date", "Source", "Title", "Type", "km", "Detail"], rows))
    else:
        md.append("Nothing scheduled in the store for the next two weeks.")
    md.append("")

    # flags
    md.append("## Flags")
    md.append("")
    md.extend(f"- {f}" for f in red_flags) if red_flags else md.append("- none")
    md.append("")

    # PRs
    md.append("## Garmin personal records")
    md.append("")
    pr_rows = []
    for pr in prs if isinstance(prs, list) else []:
        tid = pr.get("typeId")
        val = pr.get("value")
        if tid not in (1, 2, 3, 4, 5, 6, 7):
            continue  # running records only
        if tid in (1, 2, 3, 4, 5, 6):
            shown = fmt_hms(val)
        else:
            shown = f"{float(val) / 1000:.2f} km" if val else "-"
        when = pr.get("prStartTimeGmtFormatted") or pr.get("activityStartDateTimeLocalFormatted") or pr.get("prStartTimeLocal") or ""
        pr_rows.append([PR_TYPE_NAMES.get(tid, f"type {tid}"), shown, str(when)[:10]])
    md.append(_table(["Record", "Value", "Date"], pr_rows) if pr_rows else "No PR snapshot yet.")
    md.append("")

    md.append("## Notes")
    md.append("")
    md.append("- Best efforts are fastest contiguous-lap windows, so they understate true race ability when no race or time trial happened.")
    md.append("- Garmin race predictions come from VO2max and training history; treat them as optimistic by a few percent for the marathon.")
    md.append("- Plan distances come from the Runna calendar text; sessions without an explicit distance are counted but add 0 km.")
    md.append("")

    data = {
        "generated": today.isoformat(),
        "profile": {k: profile.get(k) for k in ("displayName", "fullName") if isinstance(profile, dict)},
        "counts": counts,
        "goals": [g.to_dict() for g in goals],
        "markers": {k: (list(v) if isinstance(v, tuple) else v) for k, v in markers.items()},
        "best_efforts": best,
        "weekly": [w.to_dict() for w in weekly],
        "adherence": [a.to_dict() for a in adher],
        "plan": [w.to_dict() for w in plan],
        "plan_source": plan_source,
        "upcoming": [dict(p) for p in upcoming],
        "flags": red_flags,
        "loads": loads,
        "runna_plan": runna_plan,
        "decoupling": [d.to_dict() for d in decoup],
    }
    for p in data["upcoming"]:
        p.pop("raw", None)
    return "\n".join(md), data


def write_report(md: str, data: dict[str, Any], out_dir: Path, today: date | None = None) -> tuple[Path, Path]:
    today = today or date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"report-{today.isoformat()}.md"
    json_path = out_dir / f"report-{today.isoformat()}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (out_dir / "latest.md").write_text(md, encoding="utf-8")
    (out_dir / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return md_path, json_path
