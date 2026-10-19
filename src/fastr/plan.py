"""Hand-written training plans (CSV, one row per week) as a planned-workout source.

CSV columns: week, start (Monday), phase, total_km, then <day>_km and <day> text for
mon/tue/thu/fri/sat/sun, and notes. Each day text starts with a code:
E easy, T threshold, I intervals, MP marathon pace, L long run, RACE, SHAKE, or a
non-running note such as Strength / Rest (km 0).
"""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .store import Store

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

CODE_TYPES = {
    "E": "easy",
    "T": "tempo",
    "MP": "tempo",
    "I": "intervals",
    "L": "long",
    "RACE": "race",
    "SHAKE": "easy",
}


def _wtype(text: str, km: float) -> str:
    code = text.split(" ", 1)[0].strip().upper()
    if code in CODE_TYPES:
        return CODE_TYPES[code]
    if km > 0:
        return "run"
    low = text.lower()
    if "strength" in low:
        return "strength"
    return "rest"


def read_plan(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def plan_rows(weeks: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for w in weeks:
        start = date.fromisoformat(w["start"])
        week_no = int(w["week"])
        for day, offset in DAYS.items():
            text = (w.get(day) or "").strip()
            if not text:
                continue
            km = float(w.get(f"{day}_km") or 0)
            wtype = _wtype(text, km)
            if wtype == "rest":
                continue
            d = start + timedelta(days=offset)
            rows.append(
                {
                    "uid": f"{source}:{week_no}:{day}",
                    "source": source,
                    "date": d.isoformat(),
                    "title": text,
                    "workout_type": wtype,
                    "distance_m": km * 1000 if km > 0 else None,
                    "duration_s": None,
                    "description": w.get("notes") or "",
                    "is_completed": 0,
                    "steps_json": json.dumps(
                        {"meta": {"plan_week": week_no, "phase": w.get("phase"), "total_km": float(w.get("total_km") or 0)}, "steps": []}
                    ),
                    "raw": json.dumps(w, ensure_ascii=False),
                }
            )
    return rows


def import_plan(path: Path, store: Store, source: str = "claude_plan", log=print) -> int:
    weeks = read_plan(path)
    rows = plan_rows(weeks, source)
    store.delete_planned(source)
    n = store.upsert_planned(rows)
    if weeks:
        first = date.fromisoformat(weeks[0]["start"])
        store.save_snapshot(
            f"{source}_meta",
            {
                "file": str(path),
                "plan_start": first.isoformat(),
                "plan_weeks": len(weeks),
                "total_km": sum(float(w.get("total_km") or 0) for w in weeks),
            },
        )
    store.log_sync(source, f"file={path.name} weeks={len(weeks)} sessions={n}")
    log(f"  plan {source}: {len(weeks)} weeks, {n} sessions from {path.name}")
    return n
