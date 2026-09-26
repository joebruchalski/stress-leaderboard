"""FastAPI backend exposing the same functionality as dashboard.py as a JSON
API for the React frontend.

Reads/writes the same config (~/.config/stress_analyzer/config.json + macOS
Keychain) and SQLite history database as stress_analyzer.py/dashboard.py, via
storage.py and stress_core.py only — no logic is duplicated here, this module
is purely request/response plumbing plus one background-thread job runner for
the long-running backfill.

Run with:  uvicorn api:app --port 8000  (with .venv activated)
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import plotly.io as pio
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from icalendar import Calendar
from pydantic import BaseModel

import storage
import stress_core

app = FastAPI(title="Stress vs. Calendar API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = stress_core.DEFAULT_DB_PATH
storage.init_db(DB_PATH)


# --------------------------------------------------------------------------
# JSON-safety helpers
# --------------------------------------------------------------------------

def df_records(df: Optional[pd.DataFrame]) -> list[dict]:
    """Convert a DataFrame to a JSON-safe list of dicts.

    Two things a bare df.to_dict(orient="records") would get wrong for this
    API's callers:
      1. Python's default json encoder emits a bare `NaN` token for float
         NaN, which is not valid JSON and breaks JSON.parse in the browser —
         every load_* frame here can have NaN in columns like avg_delta,
         trend, delta_stress, sleep/body-battery fields, etc. Converting NaN
         to Python None (which serializes to JSON `null`) fixes that.
      2. Several load_* frames are indexed by something meaningful
         (attendee_name, event, ...) with the identity column dropped from
         the regular columns — reset_index() restores it as a plain field so
         the frontend gets it at all. Frames with a plain default RangeIndex
         (index.name is None) are left alone so we don't introduce a
         meaningless "index" column.
    """
    if df is None or df.empty:
        return []
    if df.index.name is not None:
        df = df.reset_index()
    safe = df.astype(object).where(pd.notna(df), None)
    return safe.to_dict(orient="records")


def fig_json(fig: Any) -> Optional[dict]:
    """Serialize a plotly go.Figure to a parsed {data, layout} dict (not a
    double-encoded string) — plotly's own encoder already turns NaN/numpy/
    Timestamp values into valid JSON, no extra handling needed here."""
    if fig is None:
        return None
    return json.loads(pio.to_json(fig))


def parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid date for {field!r}: {value!r}")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _config_response(config: dict) -> dict:
    return {
        "email": config["email"],
        "ics_path": config["ics_path"],
        "ics_url": config.get("ics_url", ""),
        "calendar_email": config["calendar_email"],
        "internal_domain": config["internal_domain"],
        "tokenstore": config["tokenstore"],
        "has_password": bool(config["password"]),
        "ready": bool(config["email"] and config["ics_path"] and config["password"]),
    }


class ConfigUpdate(BaseModel):
    email: str = ""
    ics_path: str = ""
    ics_url: str = ""
    calendar_email: str = ""
    internal_domain: str = ""
    password: Optional[str] = None


@app.get("/api/config")
def get_config() -> dict:
    return _config_response(stress_core.resolve_config())


@app.post("/api/config")
def post_config(body: ConfigUpdate) -> dict:
    if not body.email or not body.ics_path:
        raise HTTPException(status_code=400, detail="Email and .ics path are required.")
    if not Path(body.ics_path).expanduser().is_file():
        raise HTTPException(status_code=400, detail=f"File not found: {body.ics_path}")

    current = stress_core.resolve_config()
    stress_core.save_config(
        body.email,
        str(Path(body.ics_path).expanduser()),
        current["tokenstore"],
        body.calendar_email,
        body.internal_domain,
        body.ics_url.strip(),
    )
    if body.password:
        stress_core.save_keychain_password(body.email, body.password)

    return _config_response(stress_core.resolve_config())


class TestCalendarRequest(BaseModel):
    ics_url: str


@app.post("/api/config/test-calendar")
def post_test_calendar(body: TestCalendarRequest) -> dict:
    """Fetch a candidate live-calendar URL and confirm it actually parses as
    an .ics feed, WITHOUT saving it or touching stress_core's fetch cache —
    lets the Settings UI validate a URL before the user commits to it, so a
    typo doesn't silently break the nightly launchd run (which would only
    surface as a stale/failed automated fetch, hours later, with no one
    watching)."""
    url = body.ics_url.strip()
    if not url:
        return {"ok": False, "message": "No URL provided."}

    try:
        response = requests.get(url, timeout=stress_core.ICS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        return {"ok": False, "message": f"Fetch failed: {exc}"}

    try:
        calendar = Calendar.from_ical(response.content)
    except ValueError as exc:
        return {"ok": False, "message": f"Fetched, but this doesn't look like a valid .ics calendar: {exc}"}

    event_count = sum(1 for component in calendar.walk() if component.name == "VEVENT")
    return {
        "ok": True,
        "message": f"Looks good — fetched {len(response.content):,} bytes, found {event_count} calendar entries.",
        "event_count": event_count,
    }


# --------------------------------------------------------------------------
# Leaderboard / Most Improved / person drill-down
# --------------------------------------------------------------------------

@app.get("/api/leaderboard")
def get_leaderboard(start: str = Query(...), end: str = Query(...)) -> dict:
    start_date = parse_date(start, "start")
    end_date = parse_date(end, "end")
    rollup = storage.load_person_rollup(DB_PATH, start_date, end_date)
    if rollup.empty:
        return {"max_meetings": None, "rows": []}
    return {"max_meetings": int(rollup["meetings"].max()), "rows": df_records(rollup)}


@app.get("/api/leaderboard/chart")
def get_leaderboard_chart(
    start: str = Query(...), end: str = Query(...), min_meetings: int = Query(1)
) -> dict:
    """Backs dashboard.py's 'Chart view (average stress)' expander on the
    Leaderboard tab (stress_core.build_person_rollup_chart) — not part of
    the original API contract, added after the frontend flagged the gap.
    Mirrors dashboard.py exactly: filter by min_meetings, sort by avg_stress
    descending, take the top 20, THEN build the chart."""
    start_date = parse_date(start, "start")
    end_date = parse_date(end, "end")
    rollup = storage.load_person_rollup(DB_PATH, start_date, end_date)
    filtered = rollup[rollup["meetings"] >= min_meetings] if not rollup.empty else rollup
    if filtered.empty:
        return {"chart": None}
    top = filtered.sort_values("avg_stress", ascending=False).head(20)
    return {"chart": fig_json(stress_core.build_person_rollup_chart(top))}


@app.get("/api/most-improved")
def get_most_improved(start: str = Query(...), end: str = Query(...)) -> dict:
    start_date = parse_date(start, "start")
    end_date = parse_date(end, "end")
    trend_df = storage.load_person_trend(DB_PATH, start_date, end_date)
    return {"rows": df_records(trend_df)}


@app.get("/api/person-history")
def get_person_history(
    email: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    name: str = Query(""),
) -> dict:
    start_date = parse_date(start, "start")
    end_date = parse_date(end, "end")
    history = storage.load_person_history(DB_PATH, email, start_date, end_date)
    chart = None
    if not history.empty:
        chart = fig_json(stress_core.build_person_history_chart(history, name))
    return {"rows": df_records(history), "chart": chart}


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

@app.get("/api/recovery")
def get_recovery(start: str = Query(...), end: str = Query(...)) -> dict:
    start_date = parse_date(start, "start")
    end_date = parse_date(end, "end")
    correlation = storage.load_recovery_correlation(DB_PATH, start_date, end_date)

    sleep_chart = None
    battery_chart = None
    if not correlation.empty:
        sleep_df = correlation.dropna(subset=["sleep_score", "overall_avg"])
        if not sleep_df.empty:
            sleep_chart = fig_json(
                stress_core.build_recovery_scatter_chart(
                    sleep_df, "sleep_score", "Sleep score (0-100)", "Sleep Score vs. Workday Stress"
                )
            )
        battery_df = correlation.dropna(subset=["body_battery_high", "overall_avg"])
        if not battery_df.empty:
            battery_chart = fig_json(
                stress_core.build_recovery_scatter_chart(
                    battery_df,
                    "body_battery_high",
                    "Body Battery, morning high (0-100)",
                    "Body Battery vs. Workday Stress",
                )
            )

    return {"rows": df_records(correlation), "sleep_chart": sleep_chart, "battery_chart": battery_chart}


# --------------------------------------------------------------------------
# Daily Detail
# --------------------------------------------------------------------------

@app.get("/api/daily")
def get_daily(date_str: str = Query(..., alias="date")) -> dict:
    target_date = parse_date(date_str, "date")
    if not storage.has_day(DB_PATH, target_date):
        return {"has_cached": False, "metrics": None, "event_summary": [], "chart": None}

    grid = storage.load_day_grid(DB_PATH, target_date)
    events = storage.load_day_events(DB_PATH, target_date)
    event_summary = storage.load_event_summary(DB_PATH, target_date)

    valid = grid.dropna(subset=["stress"])
    metrics = None
    if not valid.empty:
        metrics = {"avg": float(valid["stress"].mean()), "peak": float(valid["stress"].max())}

    chart = None
    if not grid.empty:
        chart = fig_json(stress_core.build_daily_chart_interactive(grid, events, target_date))

    return {
        "has_cached": True,
        "metrics": metrics,
        "event_summary": df_records(event_summary),
        "chart": chart,
    }


class DailyFetchRequest(BaseModel):
    date: str


@app.post("/api/daily/fetch")
def post_daily_fetch(body: DailyFetchRequest) -> dict:
    target_date = parse_date(body.date, "date")
    config = stress_core.resolve_config()

    try:
        grid, events, recovery = stress_core.run_analysis(config, target_date)
    except (stress_core.ConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any Garmin/network failure
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}")

    event_summary = stress_core.summarize_by_event(grid)
    deltas = stress_core.compute_meeting_deltas(grid, events)
    event_summary = event_summary.join(deltas)

    storage.init_db(DB_PATH)
    storage.save_day(DB_PATH, target_date, grid, event_summary, events)
    storage.save_recovery_day(DB_PATH, target_date, recovery["sleep"], recovery["body_battery"])

    return get_daily(date_str=body.date)


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------

@app.get("/api/trends")
def get_trends(start: str = Query(...), end: str = Query(...)) -> dict:
    start_date = parse_date(start, "start")
    end_date = parse_date(end, "end")

    week = storage.load_week_over_week(DB_PATH)

    daily_summary = storage.load_daily_summary_range(DB_PATH, start_date, end_date)
    daily_summary_chart = None
    if not daily_summary.empty:
        daily_summary_chart = fig_json(stress_core.build_trend_chart(daily_summary))

    event_rollup = storage.load_event_rollup(DB_PATH, start_date, end_date)

    size_correlation = storage.load_meeting_size_correlation(DB_PATH, start_date, end_date)
    meeting_size_chart = None
    if not size_correlation.empty:
        meeting_size_chart = fig_json(stress_core.build_meeting_size_scatter_chart(size_correlation))

    return {
        "week_over_week": week,
        "daily_summary_chart": daily_summary_chart,
        "event_rollup": df_records(event_rollup),
        "meeting_size_chart": meeting_size_chart,
    }


# --------------------------------------------------------------------------
# Backfill (runs stress_core.run_bulk_analysis in a background thread — it's
# fully synchronous/blocking, including time.sleep pacing between days, and
# can run for many minutes on a large pull. Running it via FastAPI
# BackgroundTasks would block the whole async event loop for that entire
# duration, freezing every other endpoint including status polling.)
# --------------------------------------------------------------------------

_JOBS_LOCK = threading.Lock()
BACKFILL_JOBS: dict[str, dict] = {}


def _run_backfill_job(job_id: str, config: dict, target_dates: list[date]) -> None:
    job = BACKFILL_JOBS[job_id]

    def on_progress(index: int, total: int, target_date: date) -> None:
        with _JOBS_LOCK:
            job["index"] = index
            job["total"] = total
            job["current_date"] = target_date.isoformat()

    try:
        bulk_results = stress_core.run_bulk_analysis(config, target_dates, on_progress=on_progress)
    except (stress_core.ConfigError, ValueError) as exc:
        with _JOBS_LOCK:
            job["state"] = "error"
            job["error"] = str(exc)
        return
    except Exception as exc:  # noqa: BLE001 - surface any other Garmin/network failure
        with _JOBS_LOCK:
            job["state"] = "error"
            job["error"] = str(exc)
        return

    storage.init_db(DB_PATH)
    ok_count = 0
    failed: list[dict] = []
    for target_date in target_dates:
        outcome = bulk_results[target_date]
        if isinstance(outcome, Exception):
            failed.append({"date": target_date.isoformat(), "reason": str(outcome)})
            continue
        grid, events, recovery = outcome
        event_summary = stress_core.summarize_by_event(grid)
        deltas = stress_core.compute_meeting_deltas(grid, events)
        event_summary = event_summary.join(deltas)
        storage.save_day(DB_PATH, target_date, grid, event_summary, events)
        storage.save_recovery_day(DB_PATH, target_date, recovery["sleep"], recovery["body_battery"])
        ok_count += 1

    with _JOBS_LOCK:
        job["state"] = "done"
        job["index"] = job["total"]
        job["current_date"] = None
        job["ok_count"] = ok_count
        job["failed"] = failed


class BackfillRequest(BaseModel):
    days_back: int
    force_refresh: bool = False


@app.post("/api/backfill")
def post_backfill(body: BackfillRequest) -> dict:
    config = stress_core.resolve_config()
    ready = bool(config["email"] and config["ics_path"] and config["password"])
    if not ready:
        raise HTTPException(status_code=400, detail="Fill in Settings first.")

    all_dates = [date.today() - timedelta(days=n) for n in range(int(body.days_back))]
    target_dates = all_dates if body.force_refresh else [d for d in all_dates if not storage.has_day(DB_PATH, d)]
    already_cached = len(all_dates) - len(target_dates)

    job_id = str(uuid.uuid4())
    job = {
        "state": "running",
        "index": 0,
        "total": len(target_dates),
        "current_date": None,
        "ok_count": 0,
        "failed": [],
        "error": None,
    }
    BACKFILL_JOBS[job_id] = job

    if not target_dates:
        job["state"] = "done"
        return {"job_id": job_id, "total": 0, "already_cached": already_cached}

    thread = threading.Thread(target=_run_backfill_job, args=(job_id, config, target_dates), daemon=True)
    thread.start()

    return {"job_id": job_id, "total": len(target_dates), "already_cached": already_cached}


@app.get("/api/backfill/status")
def get_backfill_status(job_id: str = Query(...)) -> dict:
    job = BACKFILL_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    with _JOBS_LOCK:
        return dict(job)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
