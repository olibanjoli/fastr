"""Configuration: paths, athlete settings and race goals (fastr.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("FASTR_DATA", PROJECT_ROOT / "data"))
CONFIG_PATH = Path(os.environ.get("FASTR_CONFIG", PROJECT_ROOT / "fastr.toml"))

TEMPLATE = '''# fastr configuration. Dates are plain TOML dates (YYYY-MM-DD).

[athlete]
timezone = "Europe/Zurich"
units = "metric"          # metric | statute
# Earliest day to pull from Garmin. Leave out for 18 months of history.
# history_start = 2025-01-01

[garmin]
# Optional. If empty, `fastr auth` asks for it. Password is never stored;
# only the Garmin session tokens are kept in data/garmin_tokens/.
email = ""

[runna]
# Runna app -> Connected Apps & Devices -> Calendars -> "Other Calendar"
# -> Connect -> copy the URL shown at the top of the screen.
# Can also be given via the FASTR_RUNNA_ICAL environment variable.
ical_url = ""

[plan]
# Which plan source the report scores adherence against:
# runna_ical | claude_plan | garmin_calendar. Leave empty for automatic.
source = ""

# Training zones used when pushing a CSV plan to Garmin (fastr plan push).
# Paces are "M:SS" per km (slow-fast), heart rates in bpm. Update after each test race.
[paces]
easy_hr_max = 152          # easy runs up to 75 min
long_hr_max = 145          # second hour of long runs
easy = "6:30-6:00"
long = "6:45-6:15"
mp = "5:30-5:25"
hmp = "5:10-5:05"
t = "5:15-5:05"
i = "4:55-4:45"
strides = "4:20-4:00"

# One [[goals]] block per target race. target_time is "H:MM:SS".
[[goals]]
name = "Example half marathon"
date = 2027-04-18
distance_km = 21.0975
target_time = "1:45:00"
priority = "A"
'''

DEFAULT_PACES: dict[str, str | int] = {
    "easy_hr_max": 152,
    "long_hr_max": 145,
    "easy": "6:30-6:00",
    "long": "6:45-6:15",
    "mp": "5:30-5:25",
    "hmp": "5:10-5:05",
    "t": "5:15-5:05",
    "i": "4:55-4:45",
    "strides": "4:20-4:00",
}


def parse_pace_range(value: str) -> tuple[int, int]:
    """'5:15-5:05' -> (315, 305) seconds per km, slow then fast."""
    a, _, b = str(value).partition("-")
    slow = parse_hms(a.strip()) or 0
    fast = parse_hms(b.strip()) if b.strip() else slow
    return (max(slow, fast), min(slow, fast))


def parse_hms(value: str | None) -> int | None:
    """'1:45:00' or '45:30' -> seconds."""
    if not value:
        return None
    parts = [int(p) for p in str(value).strip().split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, (m, s) = 0, parts
    else:
        raise ValueError(f"Cannot parse time {value!r}; use H:MM:SS")
    return h * 3600 + m * 60 + s


def fmt_hms(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(sec_per_km: float | None) -> str:
    if not sec_per_km or sec_per_km <= 0:
        return "-"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def _to_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass
class Goal:
    name: str
    date: date
    distance_km: float
    target_time: str | None = None
    priority: str = "A"

    @property
    def target_seconds(self) -> int | None:
        return parse_hms(self.target_time)

    @property
    def target_pace(self) -> float | None:
        """Required average pace in seconds per km."""
        t = self.target_seconds
        return t / self.distance_km if t else None

    def weeks_to_go(self, today: date | None = None) -> float:
        today = today or date.today()
        return (self.date - today).days / 7


@dataclass
class Config:
    timezone: str = "Europe/Zurich"
    units: str = "metric"
    history_start: date | None = None
    garmin_email: str | None = None
    runna_ical_url: str | None = None
    plan_source: str | None = None
    paces: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PACES))
    goals: list[Goal] = field(default_factory=list)
    path: Path = CONFIG_PATH

    @property
    def effective_history_start(self) -> date:
        return self.history_start or (date.today() - timedelta(days=548))


def write_template(path: Path = CONFIG_PATH, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def load_config(path: Path = CONFIG_PATH) -> Config:
    cfg = Config(path=path)
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        athlete = raw.get("athlete", {})
        cfg.timezone = athlete.get("timezone", cfg.timezone)
        cfg.units = athlete.get("units", cfg.units)
        cfg.history_start = _to_date(athlete.get("history_start"))
        cfg.garmin_email = raw.get("garmin", {}).get("email") or None
        cfg.runna_ical_url = raw.get("runna", {}).get("ical_url") or None
        cfg.plan_source = raw.get("plan", {}).get("source") or None
        cfg.paces.update({k: v for k, v in raw.get("paces", {}).items() if v not in (None, "")})
        for g in raw.get("goals", []):
            d = _to_date(g.get("date"))
            if not d:
                continue
            cfg.goals.append(
                Goal(
                    name=g.get("name", "Race"),
                    date=d,
                    distance_km=float(g.get("distance_km", 0) or 0),
                    target_time=g.get("target_time") or None,
                    priority=g.get("priority", "A"),
                )
            )
    cfg.garmin_email = os.environ.get("GARMIN_EMAIL") or cfg.garmin_email
    cfg.runna_ical_url = os.environ.get("FASTR_RUNNA_ICAL") or cfg.runna_ical_url
    cfg.goals.sort(key=lambda g: g.date)
    return cfg
