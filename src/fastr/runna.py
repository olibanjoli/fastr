"""Runna: ingest the plan through Runna's iCalendar feed.

Runna has no public API. The app offers a calendar subscription URL
(Connected Apps & Devices -> Calendars -> Other Calendar) that lists every
planned workout of the active plan plus completed activities. The raw feed is
saved under data/raw/runna/ so the parser can be checked against real events.

Observed event format (October 2026):

    UID: UPCOMING_PLAN_WORKOUT-<plan>_plan_week_<n>_<SLOT>_<i>
         COMPLETED_PLAN_WORKOUT-<activity id>
         COMPLETED_NON_PLAN_WORKOUT-garmin-<activity id>
    SUMMARY: "<emoji> <name> • <distance or duration>"
    DESCRIPTION (upcoming): "<Label> • <distance> • <duration range>" then the
        steps, one per line ("2km at 5:30/km, 120s walking rest", "3 reps of:",
        "• 400m at 4:40/km, 60s walking rest", "1km cool down ...").
    DESCRIPTION (completed): "📊 Summary:" block with Distance / Time / Avg Pace.
    X-WORKOUT-ESTIMATED-DURATION: seconds (upcoming only)
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from icalendar import Calendar

from .store import Store

KM_PER_MILE = 1.609344

EMOJI_CATEGORY = {
    "🏃": "run",
    "🏋": "strength",
    "🧘": "mobility",
    "🚶": "walk",
    "🚴": "ride",
    "🏊": "swim",
}

LABEL_TYPES: list[tuple[str, str]] = [
    ("taper intervals", "intervals"),
    ("intervals", "intervals"),
    ("easy run", "easy"),
    ("recovery", "easy"),
    ("long run", "long"),
    ("tempo", "tempo"),
    ("threshold", "tempo"),
    ("progressive", "tempo"),
    ("race", "race"),
    ("legs and core", "strength"),
    ("full body", "strength"),
    ("strength", "strength"),
    ("mobility", "mobility"),
    ("yoga", "mobility"),
    ("pilates", "mobility"),
]

TITLE_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("race", ("race practice",)),  # placeholder so "race practice long run" is not a race
    ("long", ("long run",)),
    ("race", ("marathon", "race", "time trial", "parkrun")),
    ("intervals", ("repeats", "interval", "rolling", "pyramid", "on off", "drop set", "fast ", "over and under", "broken", "hill", "fartlek", "strides", "400s", "300s", "200s", "1200s", "miler")),
    ("tempo", ("tempo", "threshold", "progressive", "steady")),
    ("easy", ("easy", "recovery", "shakeout", "treadmill", "running")),
]

PACE_RE = r"(\d{1,2}):(\d{2})"
DIST_META_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(km|mi)\b", re.IGNORECASE)
DURATION_RANGE_RE = re.compile(r"(?:(\d+)h)?\s*(\d+)m\b")
STEP_RE = re.compile(r"^(?:•\s*)?(?P<n>\d+(?:[.,]\d+)?)\s*(?P<unit>km|m)\b\s*(?P<rest>.*)$", re.IGNORECASE)
REPS_RE = re.compile(r"^(?:(\d+)\s*reps? of:|repeat the following\s*(\d+)x:)", re.IGNORECASE)
REST_LINE_RE = re.compile(r"^(\d+)s\s+(?:walking|jogging|standing)?\s*rest", re.IGNORECASE)
REST_RE = re.compile(r"(\d+)s\s+(?:walking|jogging|standing)?\s*rest", re.IGNORECASE)
AT_PACE_RE = re.compile(r"\bat\s+" + PACE_RE + r"\s*/km")
RANGE_PACE_RE = re.compile(r"\(?" + PACE_RE + r"\s*-\s*" + PACE_RE + r"\s*/km\)?")
CAP_PACE_RE = re.compile(r"no faster than\s+" + PACE_RE + r"\s*/km", re.IGNORECASE)
TARGET_RE = re.compile(PACE_RE + r"\s*/km \(your target ([A-Za-z ]+?) pace\)")
PLAN_UID_RE = re.compile(r"plan_week_(\d+)_([A-Z_]+?)_(\d+)$")
SUMMARY_DIST_RE = re.compile(r"Distance:\s*(\d+(?:[.,]\d+)?)\s*(km|mi)", re.IGNORECASE)
SUMMARY_TIME_RE = re.compile(r"Time:\s*(\d+):(\d{2})(?::(\d{2}))?")
SUMMARY_PACE_RE = re.compile(r"Avg Pace:\s*" + PACE_RE + r"\s*/km")


def pace_seconds(m: str, s: str) -> int:
    return int(m) * 60 + int(s)


def _to_m(value: str, unit: str) -> float:
    v = float(value.replace(",", "."))
    unit = unit.lower()
    if unit == "km":
        return v * 1000.0
    if unit == "mi":
        return v * KM_PER_MILE * 1000.0
    return v


def strip_emoji(text: str) -> tuple[str | None, str]:
    """Return (category from leading emoji, remaining text)."""
    text = text.strip()
    for emoji, cat in EMOJI_CATEGORY.items():
        if text.startswith(emoji):
            rest = text[len(emoji):]
            rest = rest.lstrip("️‍ ").strip()
            return cat, rest
    if text and not text[0].isalnum():
        parts = text.split(" ", 1)
        return None, (parts[1] if len(parts) > 1 else "").strip()
    return None, text


def classify_label(label: str) -> str | None:
    t = label.lower().strip()
    for key, wtype in LABEL_TYPES:
        if t.startswith(key) or t == key:
            return wtype
    return None


def classify_title(title: str, category: str | None) -> str:
    t = title.lower()
    if category == "strength":
        return "strength"
    if category in ("mobility", "walk", "ride", "swim"):
        return category
    if "race practice" in t:
        return "long"
    for wtype, keys in TITLE_TYPES[1:]:
        if any(k in t for k in keys):
            return wtype
    return "run" if category == "run" else "other"


def parse_duration_range(text: str) -> tuple[float | None, float | None]:
    """'35m - 40m' or '2h20m - 2h30m' -> (low_s, high_s)."""
    found = []
    for m in DURATION_RANGE_RE.finditer(text):
        h = int(m.group(1) or 0)
        found.append(float(h * 3600 + int(m.group(2)) * 60))
    if not found:
        return None, None
    return found[0], found[-1]


def parse_step_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    m = REST_LINE_RE.match(line)
    if m:
        return {"kind": "rest", "rest_s": int(m.group(1))}
    if line.lower().startswith("marathon race") or line.lower().startswith("race at"):
        step: dict[str, Any] = {"kind": "race"}
        r = RANGE_PACE_RE.search(line)
        if r:
            step["pace_low_s"] = pace_seconds(r.group(1), r.group(2))
            step["pace_high_s"] = pace_seconds(r.group(3), r.group(4))
        return step
    m = STEP_RE.match(line)
    if not m:
        return None
    dist_m = _to_m(m.group("n"), m.group("unit"))
    rest = m.group("rest")
    low = rest.lower()
    if "warm up" in low:
        kind = "warmup"
    elif "cool down" in low:
        kind = "cooldown"
    elif "easy run" in low or "conversational" in low:
        kind = "easy"
    else:
        kind = "work"
    step = {"kind": kind, "distance_m": dist_m}
    p = AT_PACE_RE.search(rest)
    if p:
        step["pace_s"] = pace_seconds(p.group(1), p.group(2))
    r = RANGE_PACE_RE.search(rest)
    if r:
        step["pace_low_s"] = pace_seconds(r.group(1), r.group(2))
        step["pace_high_s"] = pace_seconds(r.group(3), r.group(4))
    c = CAP_PACE_RE.search(rest)
    if c:
        step["pace_cap_s"] = pace_seconds(c.group(1), c.group(2))
    rs = REST_RE.search(rest)
    if rs:
        step["rest_s"] = int(rs.group(1))
    t = TARGET_RE.search(rest)
    if t:
        step["target_label"] = t.group(3).strip()
    return step


def parse_steps(description: str) -> list[dict[str, Any]]:
    """Parse the step lines of an upcoming workout into a flat list with repeat groups."""
    lines = description.split("\n")[1:]  # first line is the "Label • dist • duration" meta
    steps: list[dict[str, Any]] = []
    group: dict[str, Any] | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("📲") or set(line) <= {"-"}:
            if group is not None and not line:
                continue  # blank lines occur inside groups before the bullets end
            continue
        rm = REPS_RE.match(line)
        if rm:
            group = {"kind": "repeat", "reps": int(rm.group(1) or rm.group(2)), "steps": []}
            steps.append(group)
            continue
        step = parse_step_line(line)
        if step is None:
            continue
        if group is not None and line.startswith("•"):
            group["steps"].append(step)
        else:
            group = None
            steps.append(step)
    return steps


def steps_distance_m(steps: list[dict[str, Any]]) -> float:
    total = 0.0
    for s in steps:
        if s.get("kind") == "repeat":
            total += s["reps"] * steps_distance_m(s["steps"])
        else:
            total += float(s.get("distance_m") or 0)
    return total


def describe_steps(steps: list[dict[str, Any]]) -> str:
    """Compact one-line rendering: 'wu 2.0km | 3x(1.0km @5:15 (5:05-5:25), rest 90s) | cd 1.5km'."""
    parts = []
    for s in steps:
        parts.append(_describe_step(s))
    return " | ".join(p for p in parts if p)


def _fmt_pace(sec: int | None) -> str:
    if not sec:
        return ""
    return f"{sec // 60}:{sec % 60:02d}"


def _describe_step(s: dict[str, Any]) -> str:
    kind = s.get("kind")
    if kind == "repeat":
        inner = ", ".join(_describe_step(x) for x in s["steps"])
        return f"{s['reps']}x({inner})"
    if kind == "rest":
        return f"rest {s['rest_s']}s"
    if kind == "race":
        return f"race @{_fmt_pace(s.get('pace_low_s'))}-{_fmt_pace(s.get('pace_high_s'))}"
    label = {"warmup": "wu", "cooldown": "cd", "easy": "easy", "work": ""}.get(kind, kind or "")
    dist = s.get("distance_m") or 0
    dist_txt = f"{dist / 1000:.1f}km" if dist >= 1000 else f"{int(dist)}m"
    pace = ""
    if s.get("pace_s"):
        pace = f" @{_fmt_pace(s['pace_s'])}"
        if s.get("pace_low_s"):
            pace += f" ({_fmt_pace(s['pace_low_s'])}-{_fmt_pace(s['pace_high_s'])})"
    elif s.get("pace_cap_s"):
        pace = f" (<= {_fmt_pace(s['pace_cap_s'])})"
    rest = f", rest {s['rest_s']}s" if s.get("rest_s") else ""
    tgt = f" [{s['target_label']} pace]" if s.get("target_label") else ""
    return f"{label} {dist_txt}{pace}{tgt}{rest}".strip()


def parse_completed_summary(description: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = SUMMARY_DIST_RE.search(description)
    if m:
        out["actual_distance_m"] = _to_m(m.group(1), m.group(2))
    m = SUMMARY_TIME_RE.search(description)
    if m:
        h, mm, ss = m.group(1), m.group(2), m.group(3)
        if ss is None:
            out["actual_duration_s"] = int(h) * 60 + int(mm)
        else:
            out["actual_duration_s"] = int(h) * 3600 + int(mm) * 60 + int(ss)
    m = SUMMARY_PACE_RE.search(description)
    if m:
        out["actual_pace_s"] = pace_seconds(m.group(1), m.group(2))
    return out


def _text(v: Any) -> str:
    return "" if v is None else str(v)


def parse_event(ev: Any) -> dict[str, Any] | None:
    uid = _text(ev.get("UID"))
    summary = _text(ev.get("SUMMARY")).strip()
    description = _text(ev.get("DESCRIPTION")).strip()
    dtstart = ev.get("DTSTART")
    start = dtstart.dt if dtstart is not None else None
    if isinstance(start, datetime):
        day = start.date()
    elif isinstance(start, date):
        day = start
    else:
        return None

    status = "upcoming"
    if uid.startswith("COMPLETED_PLAN"):
        status = "completed_plan"
    elif uid.startswith("COMPLETED"):
        status = "completed_other"

    category, core = strip_emoji(summary)
    name, _, title_meta = core.partition(" • ")
    name = name.strip()
    title_meta = title_meta.strip()

    meta: dict[str, Any] = {"status": status, "category": category, "title_meta": title_meta}
    pm = PLAN_UID_RE.search(uid)
    if pm:
        meta["plan_week"] = int(pm.group(1))
        meta["slot"] = pm.group(2)

    distance_m: float | None = None
    duration_s: float | None = None
    wtype: str | None = None
    steps: list[dict[str, Any]] = []

    if status == "upcoming":
        first = description.split("\n", 1)[0]
        tokens = [t.strip() for t in first.split("•")]
        label = tokens[0] if tokens else ""
        meta["label"] = label
        wtype = classify_label(label)
        for tok in tokens[1:]:
            dm = DIST_META_RE.fullmatch(tok.strip())
            if dm and distance_m is None:
                distance_m = _to_m(dm.group(1), dm.group(2))
            elif "m" in tok:
                lo, hi = parse_duration_range(tok)
                if lo is not None:
                    meta["duration_low_s"], meta["duration_high_s"] = lo, hi
        est = ev.get("X-WORKOUT-ESTIMATED-DURATION")
        if est is not None:
            try:
                duration_s = float(str(est))
            except ValueError:
                duration_s = None
        if duration_s is None and meta.get("duration_low_s") is not None:
            duration_s = (meta["duration_low_s"] + meta["duration_high_s"]) / 2
        if category == "run" or wtype in ("easy", "tempo", "long", "intervals", "race"):
            steps = parse_steps(description)
            if distance_m is None and steps:
                distance_m = steps_distance_m(steps) or None
        if distance_m is None:
            dm = DIST_META_RE.search(title_meta)
            if dm:
                distance_m = _to_m(dm.group(1), dm.group(2))
    else:
        actual = parse_completed_summary(description)
        meta["actual"] = actual
        dm = DIST_META_RE.search(name)
        if dm:
            distance_m = _to_m(dm.group(1), dm.group(2))  # prescribed distance in the name
        elif actual.get("actual_distance_m"):
            distance_m = actual["actual_distance_m"]
        duration_s = actual.get("actual_duration_s")
        if status == "completed_other":
            wtype = "unplanned"

    if wtype is None:
        wtype = classify_title(name, category)

    return {
        "uid": f"runna:{uid}",
        "source": "runna_ical",
        "date": day.isoformat(),
        "title": name,
        "workout_type": wtype,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "description": description,
        "is_completed": 0 if status == "upcoming" else 1,
        "steps_json": json.dumps({"meta": meta, "steps": steps}, ensure_ascii=False),
        "raw": json.dumps({k: _text(ev.get(k)) for k in ev.keys()}, ensure_ascii=False),
    }


def parse_ics(text: str, today: date | None = None) -> list[dict[str, Any]]:
    cal = Calendar.from_ical(text)
    rows = [r for r in (parse_event(ev) for ev in cal.walk("VEVENT")) if r]
    rows.sort(key=lambda r: (r["date"], r["title"]))
    return rows


def plan_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the plan itself says: race, target pace, start/end, weeks."""
    out: dict[str, Any] = {"events": len(rows)}
    upcoming = [r for r in rows if not r["is_completed"]]
    if upcoming:
        out["first_upcoming"] = upcoming[0]["date"]
        out["last_upcoming"] = upcoming[-1]["date"]
    weeks = set()
    targets: dict[str, int] = {}
    week1_dates: list[date] = []
    for r in rows:
        sj = json.loads(r["steps_json"] or "{}")
        meta = sj.get("meta") or {}
        if meta.get("plan_week"):
            weeks.add(meta["plan_week"])
            if meta["plan_week"] == 1:
                week1_dates.append(date.fromisoformat(r["date"]))
        for m in TARGET_RE.finditer(r["description"] or ""):
            targets[m.group(3).strip()] = pace_seconds(m.group(1), m.group(2))
        if r["workout_type"] == "race" and not r["is_completed"]:
            race = {
                "date": r["date"],
                "name": r["title"],
                "distance_m": r["distance_m"],
                "duration_low_s": meta.get("duration_low_s"),
                "duration_high_s": meta.get("duration_high_s"),
            }
            for s in sj.get("steps") or []:
                if s.get("kind") == "race":
                    race["pace_low_s"] = s.get("pace_low_s")
                    race["pace_high_s"] = s.get("pace_high_s")
            out["race"] = race
    if week1_dates:
        first = min(week1_dates)
        out["plan_start"] = (first - timedelta(days=first.weekday())).isoformat()
    out["plan_weeks"] = max(weeks) if weeks else None
    out["target_paces"] = targets
    return out


def normalise_url(url: str) -> str:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def fetch_ics(url: str, timeout: int = 30) -> str:
    resp = requests.get(normalise_url(url), timeout=timeout, headers={"User-Agent": "fastr/0.1"})
    resp.raise_for_status()
    return resp.text


def _store_rows(rows: list[dict[str, Any]], store: Store, info_prefix: str, log) -> int:
    store.delete_planned("runna_ical")
    n = store.upsert_planned(rows)
    summary = plan_summary(rows)
    store.save_snapshot("runna_plan", summary)
    race = summary.get("race")
    info = f"{info_prefix} n={n}"
    if summary.get("first_upcoming"):
        horizon = (date.fromisoformat(summary["last_upcoming"]) - date.today()).days / 7
        info += f" upcoming {summary['first_upcoming']}..{summary['last_upcoming']} ({horizon:.1f} weeks ahead)"
    if race:
        info += f"; race {race['name']} on {race['date']}"
    store.log_sync("runna", info)
    log(f"  Runna calendar: {info}")
    return n


def sync_runna(url: str, store: Store, raw_dir: Path | None, log=print) -> int:
    text = fetch_ics(url)
    if raw_dir:
        target = raw_dir / "runna"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"feed-{date.today().isoformat()}.ics").write_text(text, encoding="utf-8")
        (target / "feed-latest.ics").write_text(text, encoding="utf-8")
    return _store_rows(parse_ics(text), store, "feed", log)


def load_ics_file(path: Path, store: Store, log=print) -> int:
    """Import a downloaded .ics file instead of fetching (handy for testing)."""
    return _store_rows(parse_ics(path.read_text(encoding="utf-8")), store, f"file={path.name}", log)
