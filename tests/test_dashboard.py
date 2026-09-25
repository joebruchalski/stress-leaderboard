"""Streamlit AppTest smoke tests for dashboard.py.

These never touch the user's real ~/.config/stress_analyzer/config.json or
history.db: credentials are supplied via env vars (which resolve_config()
prefers over the saved config file / Keychain, so no Keychain subprocess call
happens), and stress_core.DEFAULT_DB_PATH is monkeypatched to a tmp_path
database before each AppTest run so dashboard.py (which reads it via
`stress_core.DEFAULT_DB_PATH`) picks up the isolated DB.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from streamlit.testing.v1 import AppTest

import storage
import stress_core

DASHBOARD_PATH = str(Path(__file__).parent.parent / "dashboard.py")


def _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir):
    """Common isolation: fake-but-plausible credentials via env vars (so the
    sidebar doesn't show the "missing settings" warning and resolve_config()
    never calls out to the real macOS Keychain), and a temp DB path."""
    monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("ICS_FILE_PATH", str(fixtures_dir / "simple.ics"))
    monkeypatch.setenv("GARMIN_TOKENSTORE", str(tmp_path / "tokenstore"))
    db_path = str(tmp_path / "history.db")
    monkeypatch.setattr(stress_core, "DEFAULT_DB_PATH", db_path)
    return db_path


def test_dashboard_empty_state_renders_without_exceptions(monkeypatch, tmp_path, fixtures_dir):
    _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    assert [t.label for t in at.tabs] == ["Daily Detail", "Trends", "By Person", "Patterns"]

    # Empty DB: all four tabs should show an informational empty state, not
    # crash or show stale/wrong data.
    daily_info = " ".join(i.value for i in at.tabs[0].info)
    assert "No stored results" in daily_info or "Fetch from Garmin" in daily_info

    trends_info = " ".join(i.value for i in at.tabs[1].info)
    assert "No stored results in this range" in trends_info

    people_info = " ".join(i.value for i in at.tabs[2].info)
    assert "No attendee data" in people_info

    patterns_info = " ".join(i.value for i in at.tabs[3].info)
    assert "No stored results in this range" in patterns_info


def test_dashboard_populated_state_renders_without_exceptions(monkeypatch, tmp_path, fixtures_dir):
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)

    tz = ZoneInfo("America/New_York")
    today = date.today()
    timestamps = pd.date_range(
        datetime.combine(today, time(9, 0), tzinfo=tz),
        datetime.combine(today, time(9, 4), tzinfo=tz),
        freq="1min",
    )
    grid = pd.DataFrame(
        {
            "timestamp_local": timestamps,
            "stress": [10.0, 20.0, 30.0, 40.0, 50.0],
            "event": ["Standup"] * 3 + ["No Meeting"] * 2,
        }
    )
    events = [
        {
            "title": "Standup",
            "start": timestamps[0],
            "end": timestamps[2],
            "attendees": [
                {"email": "alice@example.com", "name": "Alice Anderson"},
                {"email": "bob@example.com", "name": "Bob Brown"},
            ],
        }
    ]
    storage.init_db(db_path)
    storage.save_day(db_path, today, grid, stress_core.summarize_by_event(grid), events)

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception

    # Daily Detail tab defaults to today, which now has cached data: expect
    # metrics + a chart + a populated table, not the "no data" empty state.
    daily_tab = at.tabs[0]
    assert len(daily_tab.info) == 0
    metric_labels = [m.label for m in daily_tab.metric]
    assert "Workday average stress" in metric_labels
    assert "Workday peak stress" in metric_labels
    assert len(daily_tab.dataframe) == 1  # the "Stress by meeting" table

    # Trends tab default range (today-13..today) includes today.
    trends_tab = at.tabs[1]
    assert len(trends_tab.info) == 0
    assert len(trends_tab.dataframe) == 1  # the event rollup table

    # By Person tab: two attendees, each with exactly 1 meeting here — below
    # the meetings-filter slider's min!=max requirement, so the slider must
    # be hidden (not crash) and everyone shown unfiltered.
    people_tab = at.tabs[2]
    assert len(people_tab.slider) == 0
    assert len(people_tab.dataframe) == 1
    rendered = str(people_tab.dataframe[0].value)
    assert "Alice Anderson" in rendered and "Bob Brown" in rendered

    # Patterns tab default range (today-13..today) includes today's data, so
    # both charts should render without falling back to the empty state.
    patterns_tab = at.tabs[3]
    assert len(patterns_tab.info) == 0


def test_dashboard_people_tab_slider_filters_by_meeting_count(monkeypatch, tmp_path, fixtures_dir):
    """Once someone has 2+ meetings in range, the meetings-filter slider
    must appear (unlike the single-meeting case, where it's hidden to avoid
    Streamlit's min_value==max_value crash), and it must actually filter out
    lower-frequency attendees at its default value."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    for offset in (1, 0):  # yesterday, today — both within the default 14-day range
        day = date.today() - timedelta(days=offset)
        timestamps = pd.date_range(
            datetime.combine(day, time(9, 0), tzinfo=tz),
            datetime.combine(day, time(9, 2), tzinfo=tz),
            freq="1min",
        )
        grid = pd.DataFrame({"timestamp_local": timestamps, "stress": [10.0, 20.0, 30.0], "event": ["Standup"] * 3})
        # Alice is in both days' Standup; Zoe only joins today's — 2 vs 1 meeting.
        attendees = [{"email": "alice@example.com", "name": "Alice Anderson"}]
        if offset == 0:
            attendees.append({"email": "zoe@example.com", "name": "Zoe Zimmer"})
        events = [{"title": "Standup", "start": timestamps[0], "end": timestamps[2], "attendees": attendees}]
        storage.save_day(db_path, day, grid, stress_core.summarize_by_event(grid), events)

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    people_tab = at.tabs[2]
    assert len(people_tab.slider) == 1
    slider = people_tab.slider[0]
    assert slider.min == 1
    assert slider.max == 2
    assert slider.value == 2  # defaults to min(2, max_meetings)

    # At the default threshold (2), Zoe's single meeting doesn't qualify.
    rendered_default = str(people_tab.dataframe[0].value)
    assert "Alice Anderson" in rendered_default
    assert "Zoe Zimmer" not in rendered_default

    # Lowering it to 1 brings Zoe back in.
    slider.set_value(1)
    at.run(timeout=60)
    assert not at.exception
    rendered_all = str(at.tabs[2].dataframe[0].value)
    assert "Alice Anderson" in rendered_all
    assert "Zoe Zimmer" in rendered_all
