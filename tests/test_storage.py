"""Round-trip tests for storage.py, using a throwaway SQLite DB (pytest's
tmp_path) — never the user's real ~/.config/stress_analyzer/history.db."""

from __future__ import annotations

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
