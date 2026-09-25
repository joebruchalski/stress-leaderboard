"""SQLite persistence for daily stress/calendar analysis results, so the
dashboard can show trends without re-fetching Garmin every time."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS stress_minutes (
    date TEXT NOT NULL,
    timestamp_local TEXT NOT NULL,
    stress REAL,
    event TEXT NOT NULL,
    PRIMARY KEY (date, timestamp_local)
);

CREATE TABLE IF NOT EXISTS event_summary (
    date TEXT NOT NULL,
    event TEXT NOT NULL,
    avg_stress REAL,
    peak_stress REAL,
    minutes INTEGER,
    PRIMARY KEY (date, event)
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    overall_avg REAL,
    overall_peak REAL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_attendees (
    date TEXT NOT NULL,
    event TEXT NOT NULL,
    attendee_email TEXT NOT NULL,
    attendee_name TEXT NOT NULL,
    PRIMARY KEY (date, event, attendee_email)
);

CREATE TABLE IF NOT EXISTS daily_recovery (
    date TEXT PRIMARY KEY,
    sleep_score REAL,
    total_sleep_minutes REAL,
    body_battery_low REAL,
    body_battery_high REAL,
    computed_at TEXT NOT NULL
);
"""


@contextmanager
def _connect(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def has_day(db_path: str, target_date: date) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_summary WHERE date = ? LIMIT 1", (target_date.isoformat(),)
        ).fetchone()
        return row is not None


def save_day(
    db_path: str,
    target_date: date,
    grid: pd.DataFrame,
    event_summary: pd.DataFrame,
    events: list[dict] | None = None,
) -> None:
    """Persist one day's minute-level grid, event summary, and (if provided)
    attendee lists, replacing any prior results for that date (idempotent
    re-run). `events` is the raw list from stress_core.load_calendar_events
    (each with a "title" and optional "attendees" list) — only events that
    actually appear in event_summary (i.e. had valid stress data) get their
    attendees stored."""
    date_str = target_date.isoformat()
    valid = grid.dropna(subset=["stress"])
    overall_avg = float(valid["stress"].mean()) if not valid.empty else None
    overall_peak = float(valid["stress"].max()) if not valid.empty else None

    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM stress_minutes WHERE date = ?", (date_str,))
        conn.execute("DELETE FROM event_summary WHERE date = ?", (date_str,))
        conn.execute("DELETE FROM event_attendees WHERE date = ?", (date_str,))
        conn.execute(
            "INSERT OR REPLACE INTO daily_summary (date, overall_avg, overall_peak, computed_at) VALUES (?, ?, ?, ?)",
            (date_str, overall_avg, overall_peak, datetime.now().isoformat(timespec="seconds")),
        )

        minute_rows = [
            (date_str, ts.isoformat(), None if pd.isna(stress) else float(stress), event)
            for ts, stress, event in zip(grid["timestamp_local"], grid["stress"], grid["event"])
        ]
        conn.executemany(
            "INSERT INTO stress_minutes (date, timestamp_local, stress, event) VALUES (?, ?, ?, ?)",
            minute_rows,
        )

        summary_rows = [
            (date_str, title, float(row["avg_stress"]), float(row["peak_stress"]), int(row["minutes"]))
            for title, row in event_summary.iterrows()
        ]
        conn.executemany(
            "INSERT INTO event_summary (date, event, avg_stress, peak_stress, minutes) VALUES (?, ?, ?, ?, ?)",
            summary_rows,
        )

        if events:
            attendee_rows = [
                (date_str, event["title"], attendee["email"], attendee["name"])
                for event in events
                if event["title"] in event_summary.index
                for attendee in event.get("attendees", [])
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO event_attendees (date, event, attendee_email, attendee_name) VALUES (?, ?, ?, ?)",
                attendee_rows,
            )


def save_recovery_day(
    db_path: str,
    target_date: date,
    sleep_summary: dict | None,
    body_battery_summary: dict | None,
) -> None:
    """Persist one day's sleep/Body Battery summary (each may independently
    be None — supplementary data, not every day has both synced). No-op if
    both are None, so a day with neither never gets a row of all-NULL
    metrics sitting in the table."""
    if sleep_summary is None and body_battery_summary is None:
        return

    sleep_summary = sleep_summary or {}
    body_battery_summary = body_battery_summary or {}
    date_str = target_date.isoformat()

    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_recovery
                (date, sleep_score, total_sleep_minutes, body_battery_low, body_battery_high, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                date_str,
                sleep_summary.get("sleep_score"),
                sleep_summary.get("total_sleep_minutes"),
                body_battery_summary.get("body_battery_low"),
                body_battery_summary.get("body_battery_high"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def load_recovery_correlation(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Join daily_recovery with daily_summary by date, for the "does bad
    sleep predict worse meeting-stress days" question. Inner join: a day only
    contributes to the correlation if it has both a recovery record and a
    computed stress average."""
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT r.date, r.sleep_score, r.total_sleep_minutes,
                   r.body_battery_low, r.body_battery_high, s.overall_avg
            FROM daily_recovery r
            JOIN daily_summary s ON r.date = s.date
            WHERE r.date BETWEEN ? AND ?
            ORDER BY r.date
            """,
            conn,
            params=(start_date.isoformat(), end_date.isoformat()),
        )


def load_day_grid(db_path: str, target_date: date) -> pd.DataFrame:
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT timestamp_local, stress, event FROM stress_minutes WHERE date = ? ORDER BY timestamp_local",
            conn,
            params=(target_date.isoformat(),),
        )
    df["timestamp_local"] = pd.to_datetime(df["timestamp_local"])
    return df


def load_day_events(db_path: str, target_date: date) -> list[dict]:
    """Reconstruct contiguous event windows from the stored minute-level
    labels (used to re-draw the chart's background shading from history)."""
    grid = load_day_grid(db_path, target_date)
    events = []
    current_title = None
    current_start = None
    prev_ts = None

    for ts, title in zip(grid["timestamp_local"], grid["event"]):
        if title == "No Meeting":
            title = None
        if title != current_title:
            if current_title is not None:
                events.append({"title": current_title, "start": current_start, "end": prev_ts + pd.Timedelta(minutes=1)})
            current_title, current_start = title, ts
        prev_ts = ts

    if current_title is not None:
        events.append({"title": current_title, "start": current_start, "end": prev_ts + pd.Timedelta(minutes=1)})
    return events


def load_event_summary(db_path: str, target_date: date) -> pd.DataFrame:
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT event, avg_stress, peak_stress, minutes FROM event_summary WHERE date = ? ORDER BY avg_stress DESC",
            conn,
            params=(target_date.isoformat(),),
        )
    return df.set_index("event")


def load_daily_summary_range(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT date, overall_avg, overall_peak FROM daily_summary "
            "WHERE date BETWEEN ? AND ? ORDER BY date",
            conn,
            params=(start_date.isoformat(), end_date.isoformat()),
        )


def load_event_rollup(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Average/peak stress per event title across a date range, weighted by
    minutes so recurring meetings aggregate correctly across days."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT event, avg_stress, peak_stress, minutes FROM event_summary "
            "WHERE date BETWEEN ? AND ?",
            conn,
            params=(start_date.isoformat(), end_date.isoformat()),
        )
    if df.empty:
        return df

    df["weighted_avg"] = df["avg_stress"] * df["minutes"]
    grouped = df.groupby("event").agg(
        total_minutes=("minutes", "sum"),
        weighted_avg=("weighted_avg", "sum"),
        peak_stress=("peak_stress", "max"),
        occurrences=("event", "count"),
    )
    grouped["avg_stress"] = grouped["weighted_avg"] / grouped["total_minutes"]
    return grouped.drop(columns="weighted_avg").sort_values("avg_stress", ascending=False)


def load_person_rollup(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Average/peak stress per meeting attendee across a date range, weighted
    by minutes. Note: an event's full avg/peak/minutes apply to every
    attendee of that event (per-event granularity, not per-minute-per-
    attendee — we don't know who specifically was stressful within a
    multi-person call, only which calls someone was in)."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT ea.attendee_email, ea.attendee_name, es.avg_stress, es.peak_stress, es.minutes
            FROM event_attendees ea
            JOIN event_summary es ON ea.date = es.date AND ea.event = es.event
            WHERE ea.date BETWEEN ? AND ?
            """,
            conn,
            params=(start_date.isoformat(), end_date.isoformat()),
        )
    if df.empty:
        return df

    df["weighted_avg"] = df["avg_stress"] * df["minutes"]
    grouped = df.groupby("attendee_email").agg(
        attendee_name=("attendee_name", "first"),
        total_minutes=("minutes", "sum"),
        weighted_avg=("weighted_avg", "sum"),
        peak_stress=("peak_stress", "max"),
        meetings=("attendee_email", "count"),
    )
    grouped["avg_stress"] = grouped["weighted_avg"] / grouped["total_minutes"]
    grouped = grouped.drop(columns="weighted_avg").sort_values("avg_stress", ascending=False)
    return grouped.set_index("attendee_name")
