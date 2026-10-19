"""fastr command line interface."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import typer

# Runna titles carry emoji; the Windows console defaults to cp1252.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import CONFIG_PATH, DATA_DIR, fmt_hms, fmt_pace, load_config, write_template
from .store import Store

app = typer.Typer(
    help="fastr: pull Garmin history and the Runna plan, then analyse training against goals.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console()

TOKEN_DIR = DATA_DIR / "garmin_tokens"
DB_PATH = DATA_DIR / "fastr.db"
RAW_DIR = DATA_DIR / "raw"
REPORT_DIR = DATA_DIR / "reports"
EXPORT_DIR = DATA_DIR / "exports"


def log(msg: str) -> None:
    console.print(msg)


def open_store() -> Store:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Store(DB_PATH, raw_dir=RAW_DIR)


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@app.command()
def version() -> None:
    """Print the fastr version."""
    console.print(f"fastr {__version__}")


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite an existing fastr.toml")) -> None:
    """Create fastr.toml with goals, Runna calendar URL and Garmin settings."""
    path = write_template(CONFIG_PATH, overwrite=force)
    console.print(f"Config: {path}")
    console.print("Edit it, then run: fastr auth -> fastr sync -> fastr runna -> fastr report")


@app.command()
def auth(email: str = typer.Option(None, "--email", help="Garmin Connect email")) -> None:
    """Log in to Garmin Connect once; the session tokens are kept in data/garmin_tokens."""
    from .garmin import login_with_credentials

    cfg = load_config()
    email = email or cfg.garmin_email or typer.prompt("Garmin email")
    password = typer.prompt("Garmin password", hide_input=True)

    def ask_mfa() -> str:
        return typer.prompt("Garmin MFA code")

    try:
        g = login_with_credentials(email, password, TOKEN_DIR, ask_mfa)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Login failed:[/red] {e}")
        raise typer.Exit(1)
    name = g.get_full_name() if hasattr(g, "get_full_name") else email
    console.print(f"[green]Logged in as {name}.[/green] Tokens saved to {TOKEN_DIR}")


@app.command()
def sync(
    since: str = typer.Option(None, "--since", help="Start date YYYY-MM-DD (default: a week before the latest stored activity)"),
    full: bool = typer.Option(False, "--full", help="Re-pull everything from history_start"),
    laps: bool = typer.Option(True, "--laps/--no-laps", help="Fetch laps for runs missing them"),
    metrics: bool = typer.Option(True, "--metrics/--no-metrics", help="Fetch VO2max, HRV, readiness, load, LT, predictions"),
    recent_days: int = typer.Option(14, "--recent-days", help="Per-day detail (sleep, RHR, status) for the last N days"),
    laps_limit: int = typer.Option(0, "--laps-limit", help="Cap the number of activities to fetch laps for (0 = all)"),
) -> None:
    """Pull activities, laps, daily metrics, snapshots and scheduled workouts from Garmin."""
    from . import garmin as gm

    cfg = load_config()
    store = open_store()
    try:
        g = gm.login_with_tokens(TOKEN_DIR)
    except gm.GarminAuthRequired as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    today = date.today()
    start = _parse_date(since)
    if start is None:
        latest = store.latest_activity_date()
        start = cfg.effective_history_start if (full or latest is None) else latest - timedelta(days=7)
    console.print(f"[bold]Garmin sync[/bold] {start} .. {today}")

    console.print("Activities")
    n = gm.sync_activities(g, store, start, today, log=log)
    console.print(f"  stored {n} activity summaries")

    if laps:
        console.print("Laps")
        gm.sync_laps(g, store, limit=laps_limit or None, log=log)

    if metrics:
        console.print("Metrics (date ranges)")
        gm.sync_ranges(g, store, start, today, log=log)
        gm.extract_daily_from_ranges(store, log=log)
        console.print("Metrics (recent days)")
        gm.sync_recent_days(g, store, recent_days, log=log)

    console.print("Snapshots")
    gm.sync_snapshots(g, store, log=log)
    console.print("Scheduled workouts")
    gm.sync_scheduled_workouts(g, store, log=log)

    counts = store.counts()
    console.print(f"[green]Done.[/green] runs={counts['runs']} activities={counts['activities']} laps={counts['laps']} daily={counts['daily']} planned={counts['planned_workouts']}")
    store.close()


@app.command()
def runna(
    url: str = typer.Option(None, "--url", help="Runna calendar URL (default from fastr.toml / FASTR_RUNNA_ICAL)"),
    file: Path = typer.Option(None, "--file", help="Import a downloaded .ics instead of fetching"),
    show: int = typer.Option(0, "--show", help="After import, print the first N events"),
) -> None:
    """Pull the Runna plan through its calendar feed (Connected Apps -> Calendars -> Other Calendar)."""
    from .runna import load_ics_file, sync_runna

    cfg = load_config()
    store = open_store()
    if file:
        load_ics_file(file, store, log=log)
    else:
        url = url or cfg.runna_ical_url
        if not url:
            console.print("[red]No Runna calendar URL.[/red] Put it in fastr.toml under the \\[runna] section as ical_url, or pass --url.")
            raise typer.Exit(1)
        try:
            sync_runna(url, store, RAW_DIR, log=log)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Fetching the Runna calendar failed:[/red] {e}")
            raise typer.Exit(1)
    if show:
        _print_planned(store.planned(source="runna_ical")[:show])
    store.close()


def _print_planned(rows) -> None:
    from .report import _runna_detail, describe_steps

    t = Table(title="Planned workouts")
    for col in ("date", "type", "km", "min", "title", "detail"):
        t.add_column(col)
    for p in rows:
        detail = describe_steps(p["steps_json"]) if p["source"] == "garmin_calendar" else _runna_detail(p)
        detail = " ".join(detail.split())
        t.add_row(
            p["date"], p["workout_type"] or "",
            f"{(p['distance_m'] or 0) / 1000:.1f}" if p["distance_m"] else "",
            f"{(p['duration_s'] or 0) / 60:.0f}" if p["duration_s"] else "",
            ("[done] " if p["is_completed"] else "") + (p["title"] or ""), detail[:140],
        )
    console.print(t)


plan_app = typer.Typer(help="Hand-written plans (CSV, one row per week) as an alternative plan source.")
app.add_typer(plan_app, name="plan")


@plan_app.command("import")
def plan_import(
    file: Path = typer.Argument(..., help="Plan CSV, see plans/README.md and plans/example-plan.csv"),
    source: str = typer.Option("claude_plan", "--source", help="Source name stored with the sessions"),
) -> None:
    """Load a CSV plan into the store so reports can track adherence against it."""
    from .plan import import_plan

    store = open_store()
    import_plan(file, store, source=source, log=log)
    store.close()


@plan_app.command("show")
def plan_show(
    source: str = typer.Option("claude_plan", "--source"),
    since: str = typer.Option(None, "--since", help="From date YYYY-MM-DD (default today)"),
    limit: int = typer.Option(30, "--limit"),
) -> None:
    """Print upcoming sessions of a plan source."""
    store = open_store()
    rows = store.planned(since=since or date.today().isoformat(), source=source)
    _print_planned(rows[:limit])
    store.close()


@app.command()
def report(
    weeks: int = typer.Option(26, "--weeks", help="Weeks of history to summarise"),
    plan_weeks: int = typer.Option(25, "--plan-weeks", help="Weeks of plan to look ahead"),
    out: Path = typer.Option(None, "--out", help="Output directory (default data/reports)"),
    print_md: bool = typer.Option(False, "--print", help="Print the markdown to the terminal"),
    plan_source: str = typer.Option(None, "--plan-source", help="runna_ical | claude_plan | garmin_calendar (default: runna_ical if present)"),
) -> None:
    """Build the analysis report (markdown + JSON) from the local store."""
    from .report import build_report, write_report

    cfg = load_config()
    store = open_store()
    md, data = build_report(store, cfg, weeks=weeks, plan_weeks=plan_weeks, plan_source=plan_source)
    md_path, json_path = write_report(md, data, out or REPORT_DIR)
    store.close()
    if print_md:
        console.print(md, markup=False)
    console.print(f"[green]Report written:[/green] {md_path}")
    console.print(f"[green]JSON written:[/green]   {json_path}")
    for ga in data["goals"]:
        g = ga["goal"]
        console.print(
            f"  {g['name']} ({g['date']}): target {g['target_time'] or '-'}, "
            f"Garmin predicts {fmt_hms(ga['garmin_pred_s'])}, best-effort equivalent {fmt_hms(ga['effort_pred_s'])} -> [bold]{ga['verdict']}[/bold]"
        )


@app.command()
def decoupling(
    weeks: int = typer.Option(8, "--weeks", help="How many weeks back to look"),
    min_minutes: int = typer.Option(45, "--min-minutes", help="Shortest run to include"),
    max_cv: float = typer.Option(0.12, "--max-cv", help="Skip runs whose lap paces vary more than this (structured sessions)"),
) -> None:
    """Pace-to-heart-rate drift per steady run: was the easy run actually easy?"""
    from .analysis import decoupling_report

    store = open_store()
    rows = decoupling_report(
        store.activities(since=date.today() - timedelta(weeks=weeks)),
        store.laps,
        since=date.today() - timedelta(weeks=weeks),
        min_minutes=min_minutes,
        max_cv=max_cv,
    )
    store.close()
    if not rows:
        console.print("No steady runs with lap heart-rate data in that window. Run `fastr sync` first.")
        return
    t = Table(title=f"Aerobic decoupling, last {weeks} weeks")
    for col, just in (("date", "left"), ("run", "left"), ("km", "right"), ("time", "right"),
                      ("first half", "right"), ("second half", "right"), ("drift", "right"), ("verdict", "left")):
        t.add_column(col, justify=just)
    for d in rows:
        colour = {"easy": "green", "borderline": "yellow", "too fast": "red"}[d.verdict]
        t.add_row(
            d.date, d.name[:40], f"{d.km:.1f}", fmt_hms(d.duration_s),
            f"{fmt_pace(d.pace1)} @ {d.hr1:.0f}", f"{fmt_pace(d.pace2)} @ {d.hr2:.0f}",
            f"{d.pct:+.1f}%", f"[{colour}]{d.verdict}[/{colour}]",
        )
    console.print(t)
    vals = sorted(d.pct for d in rows)
    console.print(f"Median drift {vals[len(vals) // 2]:+.1f}% over {len(rows)} runs. Under 5% = easy, 5 to 8% = borderline, over 8% = faster than easy.")


@app.command()
def status() -> None:
    """Show what is in the local store and the configured goals."""
    cfg = load_config()
    store = open_store()
    counts = store.counts()
    t = Table(title="fastr store")
    t.add_column("table")
    t.add_column("rows", justify="right")
    for k, v in counts.items():
        t.add_row(k, str(v))
    console.print(t)
    first = store.activities(runs_only=True)[:1]
    last = store.latest_activity_date()
    if first:
        console.print(f"Runs from {first[0]['date']} to {last}")
    for kind in ("activities", "laps", "ranges", "recent_days", "snapshots", "scheduled_workouts", "runna"):
        row = store.last_sync(kind)
        console.print(f"  last {kind:<18} {row['ts'] if row else '-'}  {row['info'] if row else ''}")
    console.print(f"Config: {cfg.path} (exists: {cfg.path.exists()})")
    console.print(f"Garmin tokens: {TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir())}")
    console.print(f"Runna URL set: {bool(cfg.runna_ical_url)}")
    for g in cfg.goals:
        console.print(f"Goal: {g.name} on {g.date} ({g.distance_km:g} km) target {g.target_time or '-'} = {fmt_pace(g.target_pace)}, {g.weeks_to_go():.1f} weeks away")
    store.close()


@app.command()
def inspect(
    kind: str = typer.Argument(..., help="snapshot | range | activity | laps | planned | daily"),
    key: str = typer.Argument(None, help="snapshot/range kind, activity id, or date"),
    depth: int = typer.Option(2, "--depth", help="How deep to print nested keys"),
) -> None:
    """Print raw data shapes so the extractors can be refined against real responses."""
    store = open_store()
    if kind == "snapshot":
        if not key:
            console.print("kinds: " + ", ".join(store.snapshot_kinds()))
        else:
            _print_shape(store.latest_snapshot(key), depth)
    elif kind == "range":
        if not key:
            console.print("kinds: " + ", ".join(store.range_kinds()))
        else:
            ranges = store.ranges(key)
            console.print(f"{len(ranges)} windows")
            if ranges:
                _print_shape(ranges[-1][2], depth)
    elif kind == "activity":
        rows = store.activities()
        if key:
            rows = [r for r in rows if str(r["activity_id"]) == key]
        else:
            rows = rows[-1:]
        for r in rows:
            _print_shape(json.loads(r["raw"]), depth)
    elif kind == "laps":
        rows = store.laps(int(key)) if key else []
        for l in rows:
            console.print(f"{l['lap_index']:>3} {(l['distance_m'] or 0) / 1000:5.2f} km {fmt_hms(l['duration_s'])} {fmt_pace((l['duration_s'] or 0) / ((l['distance_m'] or 1) / 1000))} HR {l['avg_hr']} {l['intensity_type'] or ''}")
    elif kind == "planned":
        rows = store.planned(since=key) if key else store.planned()
        _print_planned(rows[:40])
    elif kind == "daily":
        rows = store.daily(since=key) if key else store.daily()[-14:]
        for r in rows:
            console.print({k: r[k] for k in r.keys() if r[k] is not None and k != "updated_at"})
    else:
        console.print("unknown kind")
    store.close()


def _print_shape(obj, depth: int, indent: int = 0) -> None:
    pad = "  " * indent
    if depth < 0:
        console.print(f"{pad}...", markup=False)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                console.print(f"{pad}{k}: {type(v).__name__}[{len(v)}]", markup=False)
                _print_shape(v, depth - 1, indent + 1)
            else:
                s = str(v)
                console.print(f"{pad}{k}: {s[:80]}", markup=False)
    elif isinstance(obj, list):
        if obj:
            console.print(f"{pad}[0] of {len(obj)}:", markup=False)
            _print_shape(obj[0], depth - 1, indent + 1)
    else:
        console.print(f"{pad}{str(obj)[:200]}", markup=False)


@app.command()
def export() -> None:
    """Write CSV files (activities, laps, weekly, plan) to data/exports."""
    from .analysis import weekly_summaries

    store = open_store()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    acts = store.activities()
    _write_csv(EXPORT_DIR / "activities.csv", [dict(a) for a in acts], drop={"raw"})
    laps = []
    for a in acts:
        if a["is_run"]:
            laps.extend(dict(l) for l in store.laps(a["activity_id"]))
    _write_csv(EXPORT_DIR / "laps.csv", laps, drop={"raw"})
    _write_csv(EXPORT_DIR / "daily.csv", [dict(d) for d in store.daily()])
    _write_csv(EXPORT_DIR / "planned.csv", [dict(p) for p in store.planned()], drop={"raw", "steps_json"})
    weekly = weekly_summaries(acts, weeks=104)
    _write_csv(EXPORT_DIR / "weekly.csv", [w.to_dict() for w in weekly], drop={"zone_s", "km_by_type", "run_names"})
    console.print(f"[green]Exports written to {EXPORT_DIR}[/green]")
    store.close()


def _write_csv(path: Path, rows: list[dict], drop: set[str] = frozenset()) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = [c for c in rows[0].keys() if c not in drop]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})


if __name__ == "__main__":
    app()
