"""SQLite persistence for daily stress/calendar analysis results, so the
dashboard can show trends without re-fetching Garmin every time."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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
    delta_stress REAL,
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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables, then defensively migrate any pre-existing
    ones created before delta_stress existed (e.g. the user's real
    ~/.config/stress_analyzer/history.db). SQLite has no
    'ADD COLUMN IF NOT EXISTS', so the standard idiom is try/except on the
    'duplicate column' OperationalError a second ALTER TABLE raises."""
    conn.executescript(SCHEMA)
    try:
        conn.execute("ALTER TABLE event_summary ADD COLUMN delta_stress REAL")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        _ensure_schema(conn)


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
        _ensure_schema(conn)
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

        summary_rows = []
        for title, row in event_summary.iterrows():
            # delta_stress may be entirely absent (callers that haven't
            # merged compute_meeting_deltas() in yet) or NaN for a given
            # event (insufficient baseline) — either way, store NULL rather
            # than fabricating a value.
            delta_value = row.get("delta_stress")
            delta_stress = None if delta_value is None or pd.isna(delta_value) else float(delta_value)
            summary_rows.append(
                (date_str, title, float(row["avg_stress"]), float(row["peak_stress"]), int(row["minutes"]), delta_stress)
            )
        conn.executemany(
            "INSERT INTO event_summary (date, event, avg_stress, peak_stress, minutes, delta_stress) VALUES (?, ?, ?, ?, ?, ?)",
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


def _add_weighted_delta_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add weighted_delta/delta_minutes helper columns for a minutes-weighted
    avg_delta, the same weighting approach already used for avg_stress —
    except delta_stress can be NULL per-row (insufficient baseline for that
    occurrence), so those rows contribute zero weight rather than being
    treated as a zero delta."""
    df = df.copy()
    # SQLite returns an all-NULL column as Python None objects, which pandas
    # reads back as dtype=object rather than float64 — coerce explicitly so
    # the arithmetic/assignment below always operates on floats.
    df["delta_stress"] = pd.to_numeric(df["delta_stress"], errors="coerce")
    has_delta = df["delta_stress"].notna()
    df["weighted_delta"] = 0.0
    df.loc[has_delta, "weighted_delta"] = df.loc[has_delta, "delta_stress"] * df.loc[has_delta, "minutes"]
    df["delta_minutes"] = 0
    df.loc[has_delta, "delta_minutes"] = df.loc[has_delta, "minutes"]
    return df


def _finalize_avg_delta(grouped: pd.DataFrame) -> pd.DataFrame:
    """Turn summed weighted_delta/delta_minutes into avg_delta, NaN where no
    row in the group had a computable delta (rather than dividing 0/0)."""
    safe_delta_minutes = grouped["delta_minutes"].where(grouped["delta_minutes"] > 0)
    grouped["avg_delta"] = grouped["weighted_delta"] / safe_delta_minutes
    return grouped.drop(columns=["weighted_delta", "delta_minutes"])


def load_event_rollup(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Average/peak stress per event title across a date range, weighted by
    minutes so recurring meetings aggregate correctly across days. Also
    includes avg_delta: the same minutes-weighted approach applied to
    before-vs-during meeting stress delta (see stress_core.compute_meeting_deltas)."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT event, avg_stress, peak_stress, minutes, delta_stress FROM event_summary "
            "WHERE date BETWEEN ? AND ?",
            conn,
            params=(start_date.isoformat(), end_date.isoformat()),
        )
    if df.empty:
        return df

    df["weighted_avg"] = df["avg_stress"] * df["minutes"]
    df = _add_weighted_delta_columns(df)
    grouped = df.groupby("event").agg(
        total_minutes=("minutes", "sum"),
        weighted_avg=("weighted_avg", "sum"),
        peak_stress=("peak_stress", "max"),
        occurrences=("event", "count"),
        weighted_delta=("weighted_delta", "sum"),
        delta_minutes=("delta_minutes", "sum"),
    )
    grouped["avg_stress"] = grouped["weighted_avg"] / grouped["total_minutes"]
    grouped = _finalize_avg_delta(grouped)
    return grouped.drop(columns="weighted_avg").sort_values("avg_stress", ascending=False)


def load_person_rollup(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Average/peak stress per meeting attendee across a date range, weighted
    by minutes. Note: an event's full avg/peak/minutes apply to every
    attendee of that event (per-event granularity, not per-minute-per-
    attendee — we don't know who specifically was stressful within a
    multi-person call, only which calls someone was in). Also includes
    avg_delta: the same minutes-weighted approach applied to before-vs-during
    meeting stress delta (see stress_core.compute_meeting_deltas) — a more
    causal "who stresses me out" signal than avg_stress alone, since it
    isolates stress that rose when the meeting started rather than stress
    that was already elevated beforehand. And total_stress_exposure: the sum
    of avg_stress*minutes across all their meetings (not divided down to an
    average) — rewards both intensity AND how much time you spend with them,
    so someone who stresses you a little but constantly can outrank a rare
    high-intensity meeting.

    Indexed by attendee_name (the canonical display name, per first()) for
    backward compatibility with existing callers, but attendee_email is kept
    as a regular column too — it's the stable identity key (names can vary
    in capitalization/formatting across occurrences), needed by callers that
    let the user drill into one specific person's history."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT ea.attendee_email, ea.attendee_name, es.avg_stress, es.peak_stress, es.minutes, es.delta_stress
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
    df = _add_weighted_delta_columns(df)
    grouped = df.groupby("attendee_email").agg(
        attendee_name=("attendee_name", "first"),
        total_minutes=("minutes", "sum"),
        weighted_avg=("weighted_avg", "sum"),
        peak_stress=("peak_stress", "max"),
        meetings=("attendee_email", "count"),
        weighted_delta=("weighted_delta", "sum"),
        delta_minutes=("delta_minutes", "sum"),
    )
    grouped["avg_stress"] = grouped["weighted_avg"] / grouped["total_minutes"]
    grouped["total_stress_exposure"] = grouped["weighted_avg"]
    grouped = _finalize_avg_delta(grouped)
    grouped = grouped.drop(columns="weighted_avg").sort_values("avg_stress", ascending=False)
    # reset_index() first so attendee_email (currently the index, from the
    # groupby above) survives as a plain column instead of being discarded
    # by set_index("attendee_name") below.
    grouped = grouped.reset_index()
    return grouped.set_index("attendee_name")


def load_person_trend(db_path: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Per-attendee stress trend across a date range: split each person's
    meetings into the first vs. second half of the *queried* date range (by
    the range's midpoint date, not each person's own meeting dates — so
    everyone is compared over the same window), compute a minutes-weighted
    avg_stress in each half, and

        trend = second_half_avg - first_half_avg

    Negative trend = improving (getting less stressful over time); positive
    = getting worse. This is a more interesting "who's easy to work with"
    signal than a flat low avg_stress, which could just mean you haven't met
    with them much yet.

    Requires at least 2 meetings in EACH half to compute a trend at all —
    otherwise trend is NaN, not a number fabricated from insufficient data
    (same convention as stress_core.compute_meeting_deltas's baseline/
    during-meeting minimums). Rows with an uncomputable trend are still
    returned (with overall avg_stress/meetings intact) so callers can show
    "not enough history yet" rather than silently dropping the person.

    Indexed by attendee_name, with attendee_email kept as a column — same
    shape convention as load_person_rollup."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT ea.attendee_email, ea.attendee_name, es.date, es.avg_stress, es.peak_stress, es.minutes
            FROM event_attendees ea
            JOIN event_summary es ON ea.date = es.date AND ea.event = es.event
            WHERE ea.date BETWEEN ? AND ?
            """,
            conn,
            params=(start_date.isoformat(), end_date.isoformat()),
        )
    if df.empty:
        return df

    # ISO date strings sort the same as chronological order, so a plain
    # string comparison against the midpoint is enough — no need to parse.
    total_days = (end_date - start_date).days
    midpoint = (start_date + timedelta(days=total_days // 2)).isoformat()
    df["half"] = "first"
    df.loc[df["date"] >= midpoint, "half"] = "second"

    df["weighted_avg"] = df["avg_stress"] * df["minutes"]

    overall = df.groupby("attendee_email").agg(
        attendee_name=("attendee_name", "first"),
        total_minutes=("minutes", "sum"),
        weighted_avg=("weighted_avg", "sum"),
        peak_stress=("peak_stress", "max"),
        meetings=("attendee_email", "count"),
    )
    overall["avg_stress"] = overall["weighted_avg"] / overall["total_minutes"]
    overall = overall.drop(columns="weighted_avg")

    # unstack (rather than a two-way xs lookup) so a person with ALL their
    # meetings in only one half doesn't raise a KeyError — the missing half's
    # columns just come back NaN/0, which the min-2-meetings check below
    # already handles correctly.
    per_half = df.groupby(["attendee_email", "half"]).agg(
        weighted_avg=("weighted_avg", "sum"),
        total_minutes=("minutes", "sum"),
        meetings=("attendee_email", "count"),
    )
    per_half["avg_stress"] = per_half["weighted_avg"] / per_half["total_minutes"]
    half_metrics = per_half[["avg_stress", "meetings"]].unstack("half")
    half_metrics.columns = [f"{metric}_{half}" for metric, half in half_metrics.columns]
    for col in ("avg_stress_first", "avg_stress_second", "meetings_first", "meetings_second"):
        if col not in half_metrics.columns:
            half_metrics[col] = float("nan")

    result = overall.join(half_metrics, how="left")
    result["first_half_meetings"] = result["meetings_first"].fillna(0).astype(int)
    result["second_half_meetings"] = result["meetings_second"].fillna(0).astype(int)
    result = result.rename(columns={"avg_stress_first": "first_half_avg", "avg_stress_second": "second_half_avg"})
    result = result.drop(columns=["meetings_first", "meetings_second"])

    enough_data = (result["first_half_meetings"] >= 2) & (result["second_half_meetings"] >= 2)
    result["trend"] = float("nan")
    result.loc[enough_data, "trend"] = (
        result.loc[enough_data, "second_half_avg"] - result.loc[enough_data, "first_half_avg"]
    )

    result = result.sort_values("avg_stress", ascending=True)
    result = result.reset_index()
    return result.set_index("attendee_name")


def load_person_history(db_path: str, attendee_email: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Every stored meeting occurrence with one specific attendee, ordered
    chronologically — the Leaderboard drill-down's data source. Joined on
    attendee_email (the stable identity key; display names can vary in
    capitalization/formatting across occurrences, unlike load_person_rollup's
    aggregate view, this doesn't need to pick a single canonical name).

    One row per (date, event) occurrence — a recurring meeting on different
    days produces separate rows, so a chart built from this shows real
    occurrence-level movement over time, not one flattened number."""
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT es.date, es.event, es.avg_stress, es.peak_stress, es.minutes, es.delta_stress
            FROM event_attendees ea
            JOIN event_summary es ON ea.date = es.date AND ea.event = es.event
            WHERE ea.attendee_email = ? AND ea.date BETWEEN ? AND ?
            ORDER BY es.date
            """,
            conn,
            params=(attendee_email, start_date.isoformat(), end_date.isoformat()),
        )
