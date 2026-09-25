"""Round-trip tests for storage.py, using a throwaway SQLite DB (pytest's
tmp_path) — never the user's real ~/.config/stress_analyzer/history.db."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import storage
import stress_core


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "history.db")


def _sample_grid(target_date, tz):
    timestamps = pd.date_range(
        datetime.combine(target_date, time(9, 0), tzinfo=tz),
        datetime.combine(target_date, time(9, 4), tzinfo=tz),
        freq="1min",
    )
    return pd.DataFrame(
        {
            "timestamp_local": timestamps,
            "stress": [10.0, 20.0, None, 40.0, 50.0],
            "event": ["Standup", "Standup", "Standup", "No Meeting", "No Meeting"],
        }
    )


def test_save_and_load_day_round_trip(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    event_summary = stress_core.summarize_by_event(grid)

    storage.init_db(db_path)
    assert not storage.has_day(db_path, target_date)

    storage.save_day(db_path, target_date, grid, event_summary)
    assert storage.has_day(db_path, target_date)

    loaded_grid = storage.load_day_grid(db_path, target_date)
    assert len(loaded_grid) == len(grid)
    assert loaded_grid["stress"].isna().sum() == 1
    assert list(loaded_grid["event"]) == list(grid["event"])

    loaded_summary = storage.load_event_summary(db_path, target_date)
    assert set(loaded_summary.index) == {"Standup", "No Meeting"}
    assert loaded_summary.loc["Standup", "avg_stress"] == 15.0  # (10+20)/2, NaN dropped
    assert loaded_summary.loc["No Meeting", "avg_stress"] == 45.0


def test_save_day_is_idempotent_on_rerun(db_path):
    """save_day() deletes-then-inserts per date, so re-running the same day
    must not duplicate rows or accumulate stale event_summary entries."""
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.init_db(db_path)

    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    loaded_grid = storage.load_day_grid(db_path, target_date)
    assert len(loaded_grid) == len(grid)  # not doubled

    loaded_summary = storage.load_event_summary(db_path, target_date)
    assert len(loaded_summary) == 2  # not doubled


def test_load_day_events_reconstructs_contiguous_spans(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    # load_day_events() only reconstructs spans for real meetings — "No
    # Meeting" gaps are collapsed to None and never emitted, since they're
    # only used to redraw the chart's colored event shading from history.
    events = storage.load_day_events(db_path, target_date)
    titles = [e["title"] for e in events]
    assert titles == ["Standup"]
    assert events[0]["start"] == pd.Timestamp(datetime.combine(target_date, time(9, 0), tzinfo=tz))
    assert events[0]["end"] == pd.Timestamp(datetime.combine(target_date, time(9, 3), tzinfo=tz))


def test_daily_summary_range_and_event_rollup(db_path):
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    for offset, day in enumerate([date(2024, 1, 15), date(2024, 1, 16)]):
        grid = _sample_grid(day, tz)
        storage.save_day(db_path, day, grid, stress_core.summarize_by_event(grid))

    summary_range = storage.load_daily_summary_range(db_path, date(2024, 1, 15), date(2024, 1, 16))
    assert len(summary_range) == 2
    assert list(summary_range["date"]) == ["2024-01-15", "2024-01-16"]

    rollup = storage.load_event_rollup(db_path, date(2024, 1, 15), date(2024, 1, 16))
    # "Standup" occurs both days with the same avg (15.0), weighted-avg must equal that.
    assert rollup.loc["Standup", "avg_stress"] == pytest.approx(15.0)
    assert rollup.loc["Standup", "occurrences"] == 2
    assert rollup.loc["Standup", "total_minutes"] == 4  # 2 valid minutes/day * 2 days


def test_load_day_grid_for_missing_date_is_empty(db_path):
    storage.init_db(db_path)
    grid = storage.load_day_grid(db_path, date(2099, 1, 1))
    assert grid.empty


def _sample_events(target_date, tz):
    return [
        {
            "title": "Standup",
            "start": datetime.combine(target_date, time(9, 0), tzinfo=tz),
            "end": datetime.combine(target_date, time(9, 3), tzinfo=tz),
            "attendees": [
                {"email": "alice@example.com", "name": "Alice Anderson"},
                {"email": "bob@example.com", "name": "Bob Brown"},
            ],
        },
        {
            # Never appears in the grid's "event" column (e.g. Garmin had no
            # valid stress for its window, so summarize_by_event() never
            # produces a row for it) — must not get attendee rows either.
            "title": "Phantom Meeting",
            "start": datetime.combine(target_date, time(9, 3), tzinfo=tz),
            "end": datetime.combine(target_date, time(9, 5), tzinfo=tz),
            "attendees": [{"email": "carol@example.com", "name": "Carol Chen"}],
        },
    ]


def test_save_day_persists_attendees_only_for_real_events(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    events = _sample_events(target_date, tz)
    storage.init_db(db_path)

    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid), events)

    rollup = storage.load_person_rollup(db_path, target_date, target_date)
    names = set(rollup.index)
    assert names == {"Alice Anderson", "Bob Brown"}  # Carol's event isn't a real meeting, excluded
    assert rollup.loc["Alice Anderson", "avg_stress"] == pytest.approx(15.0)  # same as the Standup avg


def test_save_day_attendees_idempotent_and_no_duplicate_rows(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    events = _sample_events(target_date, tz)
    storage.init_db(db_path)

    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid), events)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid), events)

    rollup = storage.load_person_rollup(db_path, target_date, target_date)
    assert rollup.loc["Alice Anderson", "meetings"] == 1  # not doubled by the re-run


def test_load_person_rollup_weights_across_multiple_days(db_path):
    """Same attendee in two meetings with different stress levels on
    different days — the rollup must be minute-weighted, not a plain
    average of the two per-event averages."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    day1 = date(2024, 1, 15)
    grid1 = _sample_grid(day1, tz)  # Standup avg = 15.0 over 2 valid minutes
    storage.save_day(db_path, day1, grid1, stress_core.summarize_by_event(grid1), _sample_events(day1, tz))

    day2 = date(2024, 1, 16)
    grid2 = pd.DataFrame(
        {
            "timestamp_local": pd.date_range(
                datetime.combine(day2, time(9, 0), tzinfo=tz),
                datetime.combine(day2, time(9, 3), tzinfo=tz),
                freq="1min",
            ),
            "stress": [90.0, 90.0, 90.0, 90.0],
            "event": ["Standup"] * 4,
        }
    )
    storage.save_day(db_path, day2, grid2, stress_core.summarize_by_event(grid2), _sample_events(day2, tz))

    rollup = storage.load_person_rollup(db_path, day1, day2)
    # weighted: (15.0*2 + 90.0*4) / (2+4) = 65.0, not the naive (15+90)/2 = 52.5
    assert rollup.loc["Alice Anderson", "avg_stress"] == pytest.approx(65.0)
    assert rollup.loc["Alice Anderson", "meetings"] == 2


def test_load_person_rollup_empty_range_returns_empty_frame(db_path):
    storage.init_db(db_path)
    rollup = storage.load_person_rollup(db_path, date(2099, 1, 1), date(2099, 1, 2))
    assert rollup.empty


# --------------------------------------------------------------------------
# delta_stress: schema migration for pre-existing (older-schema) databases
# --------------------------------------------------------------------------

_OLD_EVENT_SUMMARY_SCHEMA = """
CREATE TABLE event_summary (
    date TEXT NOT NULL,
    event TEXT NOT NULL,
    avg_stress REAL,
    peak_stress REAL,
    minutes INTEGER,
    PRIMARY KEY (date, event)
);
"""


def _build_old_schema_db(db_path):
    """Simulates a real pre-existing ~/.config/stress_analyzer/history.db
    created before delta_stress existed: event_summary has no such column,
    and it already has a row in it."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_EVENT_SUMMARY_SCHEMA)
    conn.execute(
        "INSERT INTO event_summary (date, event, avg_stress, peak_stress, minutes) VALUES (?, ?, ?, ?, ?)",
        ("2024-01-15", "Standup", 15.0, 20.0, 2),
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_old_schema_missing_delta_stress_column(db_path):
    _build_old_schema_db(db_path)

    # Must not raise — including on a second call, which is where a naive
    # (non-idempotent) ALTER TABLE would blow up with "duplicate column".
    storage.init_db(db_path)
    storage.init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(event_summary)")]
        assert "delta_stress" in columns
        row = conn.execute(
            "SELECT event, avg_stress, delta_stress FROM event_summary WHERE date = ?", ("2024-01-15",)
        ).fetchone()
    # Pre-existing row survives the migration untouched; the new column is
    # NULL for it since it predates delta_stress being computed at all.
    assert row == ("Standup", 15.0, None)


def test_save_day_works_against_migrated_old_schema_db(db_path):
    """save_day() also runs the same defensive migration (via
    _ensure_schema), so it must work even if init_db() was never called
    against this particular old-schema file first."""
    _build_old_schema_db(db_path)

    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 16)
    grid = _sample_grid(target_date, tz)
    event_summary = stress_core.summarize_by_event(grid)

    storage.save_day(db_path, target_date, grid, event_summary)  # must not raise

    loaded = storage.load_event_summary(db_path, target_date)
    assert set(loaded.index) == {"Standup", "No Meeting"}


# --------------------------------------------------------------------------
# delta_stress: persistence + rollup aggregation
# --------------------------------------------------------------------------

def test_save_day_persists_delta_stress_and_event_rollup_reports_avg_delta(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    event_summary = stress_core.summarize_by_event(grid)
    # Mirrors how stress_analyzer.py/dashboard.py merge compute_meeting_deltas()'s
    # output onto summarize_by_event()'s before calling save_day().
    deltas = pd.DataFrame({"delta_stress": [12.5]}, index=pd.Index(["Standup"], name="event"))
    event_summary = event_summary.join(deltas)

    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, event_summary)

    rollup = storage.load_event_rollup(db_path, target_date, target_date)
    assert rollup.loc["Standup", "avg_delta"] == pytest.approx(12.5)
    # avg_stress/peak_stress/minutes/sort order are unaffected by the new column.
    assert rollup.loc["Standup", "avg_stress"] == pytest.approx(15.0)
    # "No Meeting" never got a delta_stress value -> NaN, not fabricated.
    assert pd.isna(rollup.loc["No Meeting", "avg_delta"])


def test_load_person_rollup_avg_delta_weights_across_multiple_days(db_path):
    """Mirrors test_load_person_rollup_weights_across_multiple_days, but for
    avg_delta: minutes-weighted across the days that actually had a
    computable delta. day2's event_summary has no delta_stress at all (as if
    compute_meeting_deltas() returned NaN for it) — it must contribute zero
    weight, not be treated as a delta of 0."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    day1 = date(2024, 1, 15)
    grid1 = _sample_grid(day1, tz)  # Standup: 2 valid minutes
    event_summary1 = stress_core.summarize_by_event(grid1)
    event_summary1 = event_summary1.join(
        pd.DataFrame({"delta_stress": [10.0]}, index=pd.Index(["Standup"], name="event"))
    )
    storage.save_day(db_path, day1, grid1, event_summary1, _sample_events(day1, tz))

    day2 = date(2024, 1, 16)
    grid2 = pd.DataFrame(
        {
            "timestamp_local": pd.date_range(
                datetime.combine(day2, time(9, 0), tzinfo=tz),
                datetime.combine(day2, time(9, 3), tzinfo=tz),
                freq="1min",
            ),
            "stress": [90.0, 90.0, 90.0, 90.0],
            "event": ["Standup"] * 4,
        }
    )
    event_summary2 = stress_core.summarize_by_event(grid2)  # no delta_stress column this run
    storage.save_day(db_path, day2, grid2, event_summary2, _sample_events(day2, tz))

    rollup = storage.load_person_rollup(db_path, day1, day2)
    # Only day1's Standup (10.0 over 2 minutes) had a computable delta; day2's
    # 4 minutes contribute nothing to the weighted average.
    assert rollup.loc["Alice Anderson", "avg_delta"] == pytest.approx(10.0)
    # avg_stress keeps its existing (unrelated) weighting behavior.
    assert rollup.loc["Alice Anderson", "avg_stress"] == pytest.approx(65.0)
