"""SQLite store for activities, laps, daily metrics, planned workouts and raw dumps."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id        INTEGER PRIMARY KEY,
    date               TEXT NOT NULL,
    start_time_local   TEXT,
    start_time_gmt     TEXT,
    name               TEXT,
    type_key           TEXT,
    is_run             INTEGER NOT NULL DEFAULT 0,
    distance_m         REAL,
    duration_s         REAL,
    moving_duration_s  REAL,
    elapsed_s          REAL,
    avg_speed          REAL,
    max_speed          REAL,
    avg_hr             REAL,
    max_hr             REAL,
    avg_cadence        REAL,
    elevation_gain     REAL,
    elevation_loss     REAL,
    calories           REAL,
    aerobic_te         REAL,
    anaerobic_te       REAL,
    te_label           TEXT,
    training_load      REAL,
    vo2max             REAL,
    workout_id         INTEGER,
    hr_z1              REAL,
    hr_z2              REAL,
    hr_z3              REAL,
    hr_z4              REAL,
    hr_z5              REAL,
    avg_power          REAL,
    norm_power         REAL,
    avg_stride_len     REAL,
    avg_gct            REAL,
    avg_vert_osc       REAL,
    lap_count          INTEGER,
    location           TEXT,
    description        TEXT,
    has_laps           INTEGER NOT NULL DEFAULT 0,
    raw                TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS laps (
    activity_id        INTEGER NOT NULL,
    lap_index          INTEGER NOT NULL,
    start_time_gmt     TEXT,
    distance_m         REAL,
    duration_s         REAL,
    moving_duration_s  REAL,
    avg_speed          REAL,
    max_speed          REAL,
    avg_hr             REAL,
    max_hr             REAL,
    avg_cadence        REAL,
    avg_power          REAL,
    elevation_gain     REAL,
    intensity_type     TEXT,
    wkt_step_index     INTEGER,
    raw                TEXT NOT NULL,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS daily (
    date                     TEXT PRIMARY KEY,
    vo2max                   REAL,
    rhr                      INTEGER,
    hrv_last_night           REAL,
    hrv_weekly_avg           REAL,
    hrv_status               TEXT,
    readiness_score          INTEGER,
    readiness_level          TEXT,
    sleep_score              INTEGER,
    sleep_seconds            INTEGER,
    body_battery_max         INTEGER,
    body_battery_min         INTEGER,
    acute_load               REAL,
    chronic_load             REAL,
    acwr                     REAL,
    training_status          TEXT,
    training_status_feedback TEXT,
    lt_hr                    INTEGER,
    lt_speed                 REAL,
    endurance_score          INTEGER,
    hill_score               INTEGER,
    steps                    INTEGER,
    pred_5k                  INTEGER,
    pred_10k                 INTEGER,
    pred_half                INTEGER,
    pred_marathon            INTEGER,
    updated_at               TEXT
);

CREATE TABLE IF NOT EXISTS planned_workouts (
    uid                TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    date               TEXT NOT NULL,
    title              TEXT,
    workout_type       TEXT,
    distance_m         REAL,
    duration_s         REAL,
    description        TEXT,
    is_completed       INTEGER NOT NULL DEFAULT 0,
    steps_json         TEXT,
    raw                TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planned_date ON planned_workouts(date);

CREATE TABLE IF NOT EXISTS snapshots (
    kind       TEXT NOT NULL,
    taken_at   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (kind, taken_at)
);

CREATE TABLE IF NOT EXISTS raw_ranges (
    kind       TEXT NOT NULL,
    start      TEXT NOT NULL,
    end        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (kind, start, end)
);

CREATE TABLE IF NOT EXISTS sync_log (
    ts    TEXT NOT NULL,
    kind  TEXT NOT NULL,
    info  TEXT
);
"""

DAILY_COLUMNS = {
    "vo2max", "rhr", "hrv_last_night", "hrv_weekly_avg", "hrv_status",
    "readiness_score", "readiness_level", "sleep_score", "sleep_seconds",
    "body_battery_max", "body_battery_min", "acute_load", "chronic_load", "acwr",
    "training_status", "training_status_feedback", "lt_hr", "lt_speed",
    "endurance_score", "hill_score", "steps", "pred_5k", "pred_10k", "pred_half",
    "pred_marathon", "run_tolerance_phrase", "acute_distance_m", "acute_tolerance_m",
}

# Columns added after the first release; created on open when missing.
DAILY_MIGRATIONS = {
    "run_tolerance_phrase": "TEXT",
    "acute_distance_m": "REAL",
    "acute_tolerance_m": "REAL",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: Path, raw_dir: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.raw_dir = raw_dir
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(daily)")}
        for col, ctype in DAILY_MIGRATIONS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE daily ADD COLUMN {col} {ctype}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- activities -----------------------------------------------------------
    def upsert_activity(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["updated_at"] = _now()
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in row if c not in ("activity_id", "has_laps")
        )
        self.conn.execute(
            f"INSERT INTO activities ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {updates}",
            list(row.values()),
        )
        self.conn.commit()

    def upsert_laps(self, activity_id: int, laps: Iterable[dict[str, Any]]) -> int:
        n = 0
        for lap in laps:
            lap = dict(lap)
            lap["activity_id"] = activity_id
            cols = ", ".join(lap)
            marks = ", ".join("?" for _ in lap)
            self.conn.execute(
                f"INSERT OR REPLACE INTO laps ({cols}) VALUES ({marks})",
                list(lap.values()),
            )
            n += 1
        self.conn.execute(
            "UPDATE activities SET has_laps=1 WHERE activity_id=?", (activity_id,)
        )
        self.conn.commit()
        return n

    def activities(
        self,
        since: date | str | None = None,
        until: date | str | None = None,
        runs_only: bool = False,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM activities WHERE 1=1"
        args: list[Any] = []
        if since:
            sql += " AND date >= ?"
            args.append(str(since))
        if until:
            sql += " AND date <= ?"
            args.append(str(until))
        if runs_only:
            sql += " AND is_run = 1"
        sql += " ORDER BY start_time_local"
        return list(self.conn.execute(sql, args))

    def activity_ids_missing_laps(self, runs_only: bool = True) -> list[int]:
        sql = "SELECT activity_id FROM activities WHERE has_laps = 0"
        if runs_only:
            sql += " AND is_run = 1"
        return [r[0] for r in self.conn.execute(sql + " ORDER BY date DESC")]

    def laps(self, activity_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM laps WHERE activity_id=? ORDER BY lap_index",
                (activity_id,),
            )
        )

    def latest_activity_date(self) -> date | None:
        row = self.conn.execute("SELECT MAX(date) FROM activities").fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    # -- daily ----------------------------------------------------------------
    def upsert_daily(self, day: date | str, **cols: Any) -> None:
        cols = {k: v for k, v in cols.items() if k in DAILY_COLUMNS and v is not None}
        if not cols:
            return
        cols["updated_at"] = _now()
        names = ["date", *cols]
        marks = ", ".join("?" for _ in names)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO daily ({', '.join(names)}) VALUES ({marks}) "
            f"ON CONFLICT(date) DO UPDATE SET {updates}",
            [str(day), *cols.values()],
        )

    def daily(
        self, since: date | str | None = None, until: date | str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM daily WHERE 1=1"
        args: list[Any] = []
        if since:
            sql += " AND date >= ?"
            args.append(str(since))
        if until:
            sql += " AND date <= ?"
            args.append(str(until))
        return list(self.conn.execute(sql + " ORDER BY date", args))

    # -- planned workouts -----------------------------------------------------
    def upsert_planned(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        for row in rows:
            row = dict(row)
            row["updated_at"] = _now()
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "uid")
            self.conn.execute(
                f"INSERT INTO planned_workouts ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(uid) DO UPDATE SET {updates}",
                list(row.values()),
            )
            n += 1
        self.conn.commit()
        return n

    def delete_planned(self, source: str) -> int:
        cur = self.conn.execute("DELETE FROM planned_workouts WHERE source=?", (source,))
        self.conn.commit()
        return cur.rowcount

    def planned(
        self,
        since: date | str | None = None,
        until: date | str | None = None,
        source: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM planned_workouts WHERE 1=1"
        args: list[Any] = []
        if since:
            sql += " AND date >= ?"
            args.append(str(since))
        if until:
            sql += " AND date <= ?"
            args.append(str(until))
        if source:
            sql += " AND source = ?"
            args.append(source)
        return list(self.conn.execute(sql + " ORDER BY date, title", args))

    # -- snapshots / raw ------------------------------------------------------
    def save_snapshot(self, kind: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots (kind, taken_at, payload) VALUES (?, ?, ?)",
            (kind, _now(), _json(payload)),
        )
        self.conn.commit()
        self.save_raw(f"snapshots/{kind}", date.today().isoformat(), payload)

    def latest_snapshot(self, kind: str) -> Any | None:
        row = self.conn.execute(
            "SELECT payload FROM snapshots WHERE kind=? ORDER BY taken_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot_kinds(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT kind FROM snapshots ORDER BY kind")]

    def save_range(self, kind: str, start: str, end: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_ranges (kind, start, end, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, start, end, _json(payload), _now()),
        )
        self.conn.commit()
        self.save_raw(f"ranges/{kind}", f"{start}_{end}", payload)

    def ranges(self, kind: str) -> list[tuple[str, str, Any]]:
        return [
            (r["start"], r["end"], json.loads(r["payload"]))
            for r in self.conn.execute(
                "SELECT start, end, payload FROM raw_ranges WHERE kind=? ORDER BY start",
                (kind,),
            )
        ]

    def range_kinds(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT kind FROM raw_ranges ORDER BY kind")]

    def save_raw(self, kind: str, key: str, payload: Any) -> None:
        if not self.raw_dir:
            return
        target = self.raw_dir / kind
        target.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(key))
        (target / f"{safe}.json").write_text(_json(payload), encoding="utf-8")

    def log_sync(self, kind: str, info: str = "") -> None:
        self.conn.execute(
            "INSERT INTO sync_log (ts, kind, info) VALUES (?, ?, ?)", (_now(), kind, info)
        )
        self.conn.commit()

    def last_sync(self, kind: str | None = None) -> sqlite3.Row | None:
        if kind:
            return self.conn.execute(
                "SELECT * FROM sync_log WHERE kind=? ORDER BY ts DESC LIMIT 1", (kind,)
            ).fetchone()
        return self.conn.execute("SELECT * FROM sync_log ORDER BY ts DESC LIMIT 1").fetchone()

    def counts(self) -> dict[str, int]:
        out = {}
        for table in (
            "activities", "laps", "daily", "planned_workouts", "snapshots", "raw_ranges"
        ):
            out[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out["runs"] = self.conn.execute(
            "SELECT COUNT(*) FROM activities WHERE is_run=1"
        ).fetchone()[0]
        return out

    def commit(self) -> None:
        self.conn.commit()
