"""Round-trip tests for storage.py, using a throwaway SQLite DB (pytest's
tmp_path) — never the user's real ~/.config/stress_analyzer/history.db."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
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
# daily_recovery: save_recovery_day / load_recovery_correlation
# --------------------------------------------------------------------------

def test_save_and_load_recovery_correlation_round_trip(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    storage.save_recovery_day(
        db_path,
        target_date,
        {"sleep_score": 54.0, "total_sleep_minutes": 422.0},
        {"body_battery_low": 32.0, "body_battery_high": 82.0},
    )

    correlation = storage.load_recovery_correlation(db_path, target_date, target_date)
    assert len(correlation) == 1
    row = correlation.iloc[0]
    assert row["date"] == target_date.isoformat()
    assert row["sleep_score"] == 54.0
    assert row["total_sleep_minutes"] == 422.0
    assert row["body_battery_low"] == 32.0
    assert row["body_battery_high"] == 82.0
    assert row["overall_avg"] == pytest.approx(30.0)  # daily_summary.overall_avg: mean of [10,20,40,50]


def test_save_recovery_day_is_idempotent_on_rerun(db_path):
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    storage.save_recovery_day(db_path, target_date, {"sleep_score": 40.0, "total_sleep_minutes": 300.0}, None)
    storage.save_recovery_day(db_path, target_date, {"sleep_score": 60.0, "total_sleep_minutes": 400.0}, None)

    correlation = storage.load_recovery_correlation(db_path, target_date, target_date)
    assert len(correlation) == 1  # not doubled
    assert correlation.iloc[0]["sleep_score"] == 60.0  # latest write wins


def test_save_recovery_day_noop_when_both_summaries_none(db_path):
    """Nothing to store (e.g. Garmin had neither sleep nor Body Battery
    synced for that day) must not create an all-NULL row."""
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    storage.save_recovery_day(db_path, target_date, None, None)

    correlation = storage.load_recovery_correlation(db_path, target_date, target_date)
    assert correlation.empty


def test_save_recovery_day_handles_partial_data(db_path):
    """Sleep synced but Body Battery didn't (or vice versa) — the row still
    saves with the missing side as NULL, not dropped entirely."""
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    storage.save_recovery_day(db_path, target_date, {"sleep_score": 70.0, "total_sleep_minutes": 410.0}, None)

    correlation = storage.load_recovery_correlation(db_path, target_date, target_date)
    assert len(correlation) == 1
    row = correlation.iloc[0]
    assert row["sleep_score"] == 70.0
    assert pd.isna(row["body_battery_low"])
    assert pd.isna(row["body_battery_high"])


def test_load_recovery_correlation_only_includes_days_with_both_recovery_and_stress(db_path):
    """Inner join semantics: a recovery row with no matching daily_summary
    (stress never computed for that date) must not appear."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    # Day with both stress and recovery.
    day1 = date(2024, 1, 15)
    grid1 = _sample_grid(day1, tz)
    storage.save_day(db_path, day1, grid1, stress_core.summarize_by_event(grid1))
    storage.save_recovery_day(db_path, day1, {"sleep_score": 50.0, "total_sleep_minutes": 400.0}, None)

    # Day with recovery data only (no stress ever computed/saved).
    day2 = date(2024, 1, 16)
    storage.save_recovery_day(db_path, day2, {"sleep_score": 80.0, "total_sleep_minutes": 450.0}, None)

    correlation = storage.load_recovery_correlation(db_path, day1, day2)
    assert list(correlation["date"]) == [day1.isoformat()]


def test_load_recovery_correlation_empty_range_returns_empty_frame(db_path):
    storage.init_db(db_path)
    correlation = storage.load_recovery_correlation(db_path, date(2099, 1, 1), date(2099, 1, 2))
    assert correlation.empty


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


# --------------------------------------------------------------------------
# load_week_over_week
# --------------------------------------------------------------------------

def _constant_stress_grid(target_date, tz, stress_value):
    """A single-minute grid whose only valid reading is stress_value, so
    daily_summary.overall_avg for that day comes out to exactly that value —
    the simplest way to pin down a day's average for week-over-week tests."""
    return pd.DataFrame(
        {
            "timestamp_local": [datetime.combine(target_date, time(9, 0), tzinfo=tz)],
            "stress": [stress_value],
            "event": ["No Meeting"],
        }
    )


def _save_constant_day(db_path, target_date, tz, stress_value):
    grid = _constant_stress_grid(target_date, tz, stress_value)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))


def test_load_week_over_week_no_data_returns_none(db_path):
    storage.init_db(db_path)
    result = storage.load_week_over_week(db_path, as_of_date=date(2024, 1, 31))
    assert result == {"recent_avg": None, "previous_avg": None, "delta": None}


def test_load_week_over_week_computes_recent_vs_previous_averages(db_path):
    """as_of=Jan 31: recent week = Jan 25-31 (all stress=80), previous week =
    Jan 18-24 (all stress=20). Averages and delta must reflect exactly that,
    not a naive all-time average."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    as_of = date(2024, 1, 31)

    for offset in range(7):  # recent week: Jan 25..31
        _save_constant_day(db_path, as_of - timedelta(days=offset), tz, 80.0)
    for offset in range(7, 14):  # previous week: Jan 18..24
        _save_constant_day(db_path, as_of - timedelta(days=offset), tz, 20.0)

    result = storage.load_week_over_week(db_path, as_of_date=as_of)
    assert result["recent_avg"] == pytest.approx(80.0)
    assert result["previous_avg"] == pytest.approx(20.0)
    assert result["delta"] == pytest.approx(60.0)


def test_load_week_over_week_defaults_as_of_date_to_today(db_path):
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    _save_constant_day(db_path, date.today(), tz, 50.0)

    result = storage.load_week_over_week(db_path)  # as_of_date omitted
    assert result["recent_avg"] == pytest.approx(50.0)


def test_load_week_over_week_missing_previous_week_gives_none_delta(db_path):
    """Only the recent week has any stored data — recent_avg is computable,
    but there's nothing to compare it against, so previous_avg/delta must be
    None rather than fabricating a comparison from zero data."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    as_of = date(2024, 1, 31)
    _save_constant_day(db_path, as_of, tz, 80.0)

    result = storage.load_week_over_week(db_path, as_of_date=as_of)
    assert result["recent_avg"] == pytest.approx(80.0)
    assert result["previous_avg"] is None
    assert result["delta"] is None


# --------------------------------------------------------------------------
# load_meeting_size_correlation
# --------------------------------------------------------------------------

def test_load_meeting_size_correlation_empty_range_returns_empty_frame(db_path):
    storage.init_db(db_path)
    result = storage.load_meeting_size_correlation(db_path, date(2099, 1, 1), date(2099, 1, 2))
    assert result.empty


def test_load_meeting_size_correlation_counts_attendees_per_occurrence(db_path):
    """Two meeting occurrences on the same day, with different attendee
    counts — attendee_count must reflect each occurrence's own headcount,
    joined against its own avg_stress, not mixed up between them."""
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    storage.init_db(db_path)

    timestamps = pd.date_range(
        datetime.combine(target_date, time(9, 0), tzinfo=tz),
        datetime.combine(target_date, time(9, 3), tzinfo=tz),
        freq="1min",
    )
    grid = pd.DataFrame(
        {
            "timestamp_local": timestamps,
            "stress": [30.0, 30.0, 70.0, 70.0],
            "event": ["Small Sync", "Small Sync", "Big Review", "Big Review"],
        }
    )
    events = [
        {
            "title": "Small Sync",
            "start": timestamps[0],
            "end": timestamps[1],
            "attendees": [{"email": "alice@example.com", "name": "Alice Anderson"}],
        },
        {
            "title": "Big Review",
            "start": timestamps[2],
            "end": timestamps[3],
            "attendees": [
                {"email": "alice@example.com", "name": "Alice Anderson"},
                {"email": "bob@example.com", "name": "Bob Brown"},
                {"email": "carol@example.com", "name": "Carol Chen"},
            ],
        },
    ]
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid), events)

    result = storage.load_meeting_size_correlation(db_path, target_date, target_date)
    assert len(result) == 2
    by_event = result.set_index("event")
    assert by_event.loc["Small Sync", "attendee_count"] == 1
    assert by_event.loc["Small Sync", "avg_stress"] == pytest.approx(30.0)
    assert by_event.loc["Big Review", "attendee_count"] == 3
    assert by_event.loc["Big Review", "avg_stress"] == pytest.approx(70.0)


def test_load_meeting_size_correlation_excludes_events_without_attendees(db_path):
    """An event_summary row with no matching event_attendees rows (e.g. a
    meeting whose .ics entry had no attendee list) must not appear — there's
    no meaningful "size" to plot for it."""
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)  # "Standup" + "No Meeting", no events/attendees passed
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid))

    result = storage.load_meeting_size_correlation(db_path, target_date, target_date)
    assert result.empty


def test_load_person_rollup_keeps_attendee_email_column(db_path):
    """attendee_email must survive as a real column, not just the (discarded)
    groupby key — callers (e.g. the dashboard's drill-down selection) need
    it as the stable identity key alongside the display-name index."""
    tz = ZoneInfo("America/New_York")
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    events = _sample_events(target_date, tz)
    storage.init_db(db_path)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid), events)

    rollup = storage.load_person_rollup(db_path, target_date, target_date)
    assert rollup.loc["Alice Anderson", "attendee_email"] == "alice@example.com"
    assert rollup.loc["Bob Brown", "attendee_email"] == "bob@example.com"


# --------------------------------------------------------------------------
# load_person_trend: first-half vs. second-half stress trend per attendee
# --------------------------------------------------------------------------

def _flat_grid(day, tz, stress_value, event_title="Sync", minutes=2):
    timestamps = pd.date_range(
        datetime.combine(day, time(9, 0), tzinfo=tz),
        periods=minutes,
        freq="1min",
    )
    return pd.DataFrame(
        {
            "timestamp_local": timestamps,
            "stress": [stress_value] * minutes,
            "event": [event_title] * minutes,
        }
    )


def _attendee_event(grid, email, name, event_title="Sync"):
    ts = grid["timestamp_local"]
    return [
        {
            "title": event_title,
            "start": ts.iloc[0],
            "end": ts.iloc[-1] + pd.Timedelta(minutes=1),
            "attendees": [{"email": email, "name": name}],
        }
    ]


def test_load_person_trend_insufficient_data_returns_nan(db_path):
    """Only 1 meeting in each half of the range — below the minimum of 2
    per half — so trend must be NaN, not a number computed from too little
    data. avg_stress/meetings must still be correct (insufficient-data only
    disables the trend column, not the whole row)."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    start, end = date(2024, 1, 1), date(2024, 1, 20)  # midpoint: Jan 11

    day1 = date(2024, 1, 2)  # first half
    grid1 = _flat_grid(day1, tz, 80.0)
    storage.save_day(
        db_path, day1, grid1, stress_core.summarize_by_event(grid1), _attendee_event(grid1, "alice@example.com", "Alice A")
    )

    day2 = date(2024, 1, 12)  # second half
    grid2 = _flat_grid(day2, tz, 20.0)
    storage.save_day(
        db_path, day2, grid2, stress_core.summarize_by_event(grid2), _attendee_event(grid2, "alice@example.com", "Alice A")
    )

    trend = storage.load_person_trend(db_path, start, end)
    row = trend.loc["Alice A"]
    assert row["first_half_meetings"] == 1
    assert row["second_half_meetings"] == 1
    assert pd.isna(row["trend"])
    # Overall aggregate is still correct even though trend is uncomputable.
    assert row["meetings"] == 2
    assert row["avg_stress"] == pytest.approx(50.0)


def test_load_person_trend_computes_improving_and_worsening_trends(db_path):
    """Multi-person, multi-date correctness: Alice's stress drops from the
    first half to the second (improving -> negative trend), Bob's rises
    (worsening -> positive trend). Each half is minutes-weighted, mirroring
    load_person_rollup's existing weighting convention."""
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    start, end = date(2024, 1, 1), date(2024, 1, 20)  # midpoint: Jan 11

    # Alice: 80 -> 80 (first half, 2 meetings) then 20 -> 20 (second half, 2 meetings).
    for day in (date(2024, 1, 2), date(2024, 1, 4)):
        grid = _flat_grid(day, tz, 80.0)
        storage.save_day(
            db_path, day, grid, stress_core.summarize_by_event(grid), _attendee_event(grid, "alice@example.com", "Alice A")
        )
    for day in (date(2024, 1, 12), date(2024, 1, 15)):
        grid = _flat_grid(day, tz, 20.0)
        storage.save_day(
            db_path, day, grid, stress_core.summarize_by_event(grid), _attendee_event(grid, "alice@example.com", "Alice A")
        )

    # Bob: 20 -> 20 (first half) then 80 -> 80 (second half) — the reverse.
    for day in (date(2024, 1, 3), date(2024, 1, 5)):
        grid = _flat_grid(day, tz, 20.0)
        storage.save_day(
            db_path, day, grid, stress_core.summarize_by_event(grid), _attendee_event(grid, "bob@example.com", "Bob B")
        )
    for day in (date(2024, 1, 13), date(2024, 1, 16)):
        grid = _flat_grid(day, tz, 80.0)
        storage.save_day(
            db_path, day, grid, stress_core.summarize_by_event(grid), _attendee_event(grid, "bob@example.com", "Bob B")
        )

    trend = storage.load_person_trend(db_path, start, end)

    alice = trend.loc["Alice A"]
    assert alice["first_half_avg"] == pytest.approx(80.0)
    assert alice["second_half_avg"] == pytest.approx(20.0)
    assert alice["trend"] == pytest.approx(-60.0)  # improving

    bob = trend.loc["Bob B"]
    assert bob["first_half_avg"] == pytest.approx(20.0)
    assert bob["second_half_avg"] == pytest.approx(80.0)
    assert bob["trend"] == pytest.approx(60.0)  # worsening


def test_load_person_trend_empty_range_returns_empty_frame(db_path):
    storage.init_db(db_path)
    trend = storage.load_person_trend(db_path, date(2099, 1, 1), date(2099, 1, 2))
    assert trend.empty


# --------------------------------------------------------------------------
# load_person_history: one attendee's meeting occurrences over time
# --------------------------------------------------------------------------

def test_load_person_history_returns_chronological_occurrences_for_one_attendee(db_path):
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    # Same person, but their display name varies in capitalization across
    # occurrences (e.g. calendar invite formatting drift) — email is the
    # stable join key, per the docstring, so both occurrences must still
    # come back.
    day1 = date(2024, 1, 5)
    grid1 = _flat_grid(day1, tz, 30.0, event_title="1:1")
    storage.save_day(
        db_path, day1, grid1, stress_core.summarize_by_event(grid1),
        _attendee_event(grid1, "alice@example.com", "alice anderson", event_title="1:1"),
    )

    day2 = date(2024, 1, 10)
    grid2 = _flat_grid(day2, tz, 70.0, event_title="Planning")
    storage.save_day(
        db_path, day2, grid2, stress_core.summarize_by_event(grid2),
        _attendee_event(grid2, "alice@example.com", "Alice Anderson", event_title="Planning"),
    )

    # An unrelated attendee in the same range must not leak into Alice's history.
    day3 = date(2024, 1, 7)
    grid3 = _flat_grid(day3, tz, 90.0, event_title="Standup")
    storage.save_day(
        db_path, day3, grid3, stress_core.summarize_by_event(grid3),
        _attendee_event(grid3, "carol@example.com", "Carol Chen", event_title="Standup"),
    )

    history = storage.load_person_history(db_path, "alice@example.com", date(2024, 1, 1), date(2024, 1, 20))
    assert list(history["date"]) == ["2024-01-05", "2024-01-10"]  # chronological
    assert list(history["event"]) == ["1:1", "Planning"]
    assert list(history["avg_stress"]) == [30.0, 70.0]


def test_load_person_history_unknown_attendee_returns_empty_frame(db_path):
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    target_date = date(2024, 1, 15)
    grid = _sample_grid(target_date, tz)
    storage.save_day(db_path, target_date, grid, stress_core.summarize_by_event(grid), _sample_events(target_date, tz))

    history = storage.load_person_history(db_path, "nobody@example.com", date(2024, 1, 1), date(2024, 1, 20))
    assert history.empty
