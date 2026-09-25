"""Streamlit AppTest smoke tests for dashboard.py.

These never touch the user's real ~/.config/stress_analyzer/config.json or
history.db: credentials are supplied via env vars (which resolve_config()
prefers over the saved config file / Keychain, so no Keychain subprocess call
happens), and stress_core.DEFAULT_DB_PATH is monkeypatched to a tmp_path
database before each AppTest run so dashboard.py (which reads it via
`stress_core.DEFAULT_DB_PATH`) picks up the isolated DB.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
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
    assert [t.label for t in at.tabs] == ["🏆 Leaderboard", "Recovery", "Daily Detail", "Trends"]

    # Empty DB: all four tabs should show an informational empty state, not
    # crash or show stale/wrong data.
    leaderboard_info = " ".join(i.value for i in at.tabs[0].info)
    assert "No attendee data" in leaderboard_info

    recovery_info = " ".join(i.value for i in at.tabs[1].info)
    assert "No sleep/Body Battery data" in recovery_info

    daily_info = " ".join(i.value for i in at.tabs[2].info)
    assert "No stored results" in daily_info or "Fetch from Garmin" in daily_info
    assert len(at.tabs[2].get("plotly_chart")) == 0  # no chart before any data exists

    trends_info = " ".join(i.value for i in at.tabs[3].info)
    assert "No stored results in this range" in trends_info


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

    # Leaderboard tab: two attendees, each with exactly 1 meeting here — below
    # the meetings-filter slider's min!=max requirement, so the slider must
    # be hidden (not crash) and everyone shown unfiltered. Hero metric names
    # the top stressor, and the ranked table lists both attendees.
    leaderboard_tab = at.tabs[0]
    assert len(leaderboard_tab.slider) == 0
    metric_labels = [m.label for m in leaderboard_tab.metric]
    assert "🏆 Top stressor" in metric_labels
    # Two selectable tables now: the main leaderboard, and the "Most
    # Improved / Least Stressful" panel below it.
    assert len(leaderboard_tab.dataframe) == 2
    rendered = str(leaderboard_tab.dataframe[0].value)
    assert "Alice Anderson" in rendered and "Bob Brown" in rendered
    # Nobody clicked yet: the drill-down section stays in its clean default
    # state, not showing a chart for anyone.
    assert len(leaderboard_tab.get("plotly_chart")) == 0

    # Daily Detail tab defaults to today, which now has cached data: expect
    # metrics + a chart + a populated table, not the "no data" empty state.
    daily_tab = at.tabs[2]
    assert len(daily_tab.info) == 0
    daily_metric_labels = [m.label for m in daily_tab.metric]
    assert "Workday average stress" in daily_metric_labels
    assert "Workday peak stress" in daily_metric_labels
    assert len(daily_tab.dataframe) == 1  # the "Stress by meeting" table

    # The daily chart is now interactive (Plotly, not matplotlib/st.pyplot).
    # AppTest has no dedicated `.plotly_chart` accessor like it does for
    # `.image` (st.pyplot) — st.plotly_chart() shows up as a generic
    # UnknownElement of type "plotly_chart", fetched via `.get(...)`.
    daily_plotly_charts = daily_tab.get("plotly_chart")
    assert len(daily_plotly_charts) == 1
    spec = json.loads(daily_plotly_charts[0].proto.spec)
    trace_names = {trace.get("name") for trace in spec["data"]}
    assert "Stress level" in trace_names  # the stress line itself
    assert "Standup" in trace_names  # legend entry for the one meeting that day
    assert spec["layout"].get("hovermode") == "x unified"

    # Trends tab default range (today-13..today) includes today.
    trends_tab = at.tabs[3]
    assert len(trends_tab.info) == 0
    # avg/peak trend line chart + the meeting-size-vs-stress scatter (today's
    # Standup has 2 stored attendees, so it has a computable attendee_count).
    assert len(trends_tab.get("plotly_chart")) == 2
    assert len(trends_tab.dataframe) == 1  # the event rollup table


def test_dashboard_recovery_tab_renders_scatter_charts_when_populated(monkeypatch, tmp_path, fixtures_dir):
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    for offset, sleep_score, bb_high in [(1, 40.0, 60.0), (0, 85.0, 95.0)]:
        day = date.today() - timedelta(days=offset)
        timestamps = pd.date_range(
            datetime.combine(day, time(9, 0), tzinfo=tz),
            datetime.combine(day, time(9, 2), tzinfo=tz),
            freq="1min",
        )
        grid = pd.DataFrame({"timestamp_local": timestamps, "stress": [50.0, 60.0, 70.0], "event": ["Standup"] * 3})
        storage.save_day(db_path, day, grid, stress_core.summarize_by_event(grid))
        storage.save_recovery_day(
            db_path,
            day,
            {"sleep_score": sleep_score, "total_sleep_minutes": 400.0},
            {"body_battery_low": 20.0, "body_battery_high": bb_high},
        )

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    recovery_tab = at.tabs[1]
    assert len(recovery_tab.info) == 0  # populated, not the empty state
    # st.plotly_chart() has no dedicated AppTest accessor (unlike st.pyplot's
    # `.image`) — it shows up as an UnknownElement with .type == "plotly_chart",
    # which Block.get() filters for.
    assert len(recovery_tab.get("plotly_chart")) == 2  # sleep-score scatter + Body Battery scatter
    assert len(recovery_tab.dataframe) == 1  # the day-by-day table


def test_dashboard_recovery_tab_partial_data_shows_info_not_exception(monkeypatch, tmp_path, fixtures_dir):
    """A day with only sleep data (no Body Battery) must still render the
    sleep chart and an info message for the missing Body Battery chart,
    never an uncaught exception."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    today = date.today()
    timestamps = pd.date_range(
        datetime.combine(today, time(9, 0), tzinfo=tz),
        datetime.combine(today, time(9, 2), tzinfo=tz),
        freq="1min",
    )
    grid = pd.DataFrame({"timestamp_local": timestamps, "stress": [50.0, 60.0, 70.0], "event": ["Standup"] * 3})
    storage.save_day(db_path, today, grid, stress_core.summarize_by_event(grid))
    storage.save_recovery_day(db_path, today, {"sleep_score": 65.0, "total_sleep_minutes": 400.0}, None)

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    recovery_tab = at.tabs[1]
    assert len(recovery_tab.get("plotly_chart")) == 1  # only the sleep-score chart
    battery_info = " ".join(i.value for i in recovery_tab.info)
    assert "No overlapping Body Battery" in battery_info


def test_dashboard_trends_tab_week_over_week_empty_state(monkeypatch, tmp_path, fixtures_dir):
    """No stored days at all: the week-over-week stat must show its
    not-enough-history caption instead of a metric, and the tab must still
    stop cleanly at the "no stored results" empty state (the meeting-size
    section, like the trend chart and event rollup, only renders once
    daily_summary has data for the selected range) — no crash either way."""
    _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    trends_tab = at.tabs[3]
    assert len(trends_tab.metric) == 0  # no week-over-week comparison with zero stored data
    caption_text = " ".join(c.value for c in trends_tab.caption)
    assert "Not enough recent history" in caption_text
    assert len(trends_tab.get("plotly_chart")) == 0
    trends_info = " ".join(i.value for i in trends_tab.info)
    assert "No stored results in this range yet" in trends_info


def test_dashboard_trends_tab_week_over_week_and_meeting_size_populated(monkeypatch, tmp_path, fixtures_dir):
    """14 days of data straddling the recent/previous week boundary, with
    attendees on each day's meeting — the week-over-week metric should show
    a real delta, and the meeting-size scatter should render alongside the
    existing trend chart (two plotly charts total in the tab)."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    today = date.today()
    for offset in range(14):  # today..today-13, covers both 7-day windows
        day = today - timedelta(days=offset)
        stress_value = 80.0 if offset < 7 else 20.0  # recent week higher than previous week
        timestamps = pd.date_range(
            datetime.combine(day, time(9, 0), tzinfo=tz),
            datetime.combine(day, time(9, 1), tzinfo=tz),
            freq="1min",
        )
        grid = pd.DataFrame(
            {
                "timestamp_local": timestamps,
                "stress": [stress_value, stress_value],
                "event": ["Standup"] * 2,
            }
        )
        events = [
            {
                "title": "Standup",
                "start": timestamps[0],
                "end": timestamps[1],
                "attendees": [
                    {"email": "alice@example.com", "name": "Alice Anderson"},
                    {"email": "bob@example.com", "name": "Bob Brown"},
                ],
            }
        ]
        storage.save_day(db_path, day, grid, stress_core.summarize_by_event(grid), events)

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    trends_tab = at.tabs[3]

    metric_labels = [m.label for m in trends_tab.metric]
    assert "This week's average stress vs. last week" in metric_labels
    week_metric = trends_tab.metric[metric_labels.index("This week's average stress vs. last week")]
    assert week_metric.value == "80.0"
    assert week_metric.delta == "+60.0"

    # Trend line chart + the new meeting-size scatter, both Plotly.
    assert len(trends_tab.get("plotly_chart")) == 2
    size_chart = trends_tab.get("plotly_chart")[1]
    spec = json.loads(size_chart.proto.spec)
    assert spec["layout"]["xaxis"]["title"]["text"] == "Attendee count"
    # Plotly's JSON spec packs numeric arrays as base64 (dtype + bdata) rather
    # than a plain list — decode to check the point count/values rather than
    # assuming list equality.
    x_field = spec["data"][0]["x"]
    x_values = np.frombuffer(base64.b64decode(x_field["bdata"]), dtype=x_field["dtype"])
    assert list(x_values) == [2] * 14  # 2 attendees, once per stored day


def test_dashboard_leaderboard_slider_filters_by_meeting_count(monkeypatch, tmp_path, fixtures_dir):
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
    leaderboard_tab = at.tabs[0]
    assert len(leaderboard_tab.slider) == 1
    slider = leaderboard_tab.slider[0]
    assert slider.min == 1
    assert slider.max == 2
    assert slider.value == 2  # defaults to min(2, max_meetings)

    # At the default threshold (2), Zoe's single meeting doesn't qualify.
    rendered_default = str(leaderboard_tab.dataframe[0].value)
    assert "Alice Anderson" in rendered_default
    assert "Zoe Zimmer" not in rendered_default

    # Lowering it to 1 brings Zoe back in.
    slider.set_value(1)
    at.run(timeout=60)
    assert not at.exception
    rendered_all = str(at.tabs[0].dataframe[0].value)
    assert "Alice Anderson" in rendered_all
    assert "Zoe Zimmer" in rendered_all


def _save_flat_day(db_path, day, tz, stress_value, email, name, event_title="Sync"):
    """Minimal single-event/single-attendee day, reused by the "Most
    Improved" panel and drill-down tests below — every minute in the event
    gets the same stress value, so avg/first-half/second-half math is easy
    to predict exactly."""
    timestamps = pd.date_range(
        datetime.combine(day, time(9, 0), tzinfo=tz),
        periods=2,
        freq="1min",
    )
    grid = pd.DataFrame(
        {"timestamp_local": timestamps, "stress": [stress_value, stress_value], "event": [event_title] * 2}
    )
    events = [
        {
            "title": event_title,
            "start": timestamps[0],
            "end": timestamps[-1] + timedelta(minutes=1),
            "attendees": [{"email": email, "name": name}],
        }
    ]
    storage.save_day(db_path, day, grid, stress_core.summarize_by_event(grid), events)


def test_dashboard_most_improved_panel_ranks_improving_trend_above_worsening(monkeypatch, tmp_path, fixtures_dir):
    """Alice's stress drops from the first half of the (default 90-day)
    leaderboard range to the second (improving); Bob's rises (worsening).
    The "Most Improved / Least Stressful" panel must rank Alice above Bob,
    and must be a second, separately selectable table alongside the main
    leaderboard — not folded into it."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)

    # Default leaderboard range is [today-89, today]; midpoint is ~today-45.
    # offsets > 45 land in the first half, offsets <= 45 in the second.
    # Alice and Bob use distinct dates: save_day() replaces a WHOLE day's
    # results per date (it models one person's single calendar, not
    # multiple independent people's calendars sharing a date), so reusing
    # the same date for both would have Bob's save silently wipe Alice's
    # already-saved attendee rows for that day.
    for offset in (80, 75):
        _save_flat_day(db_path, date.today() - timedelta(days=offset), tz, 90.0, "alice@example.com", "Alice Anderson")
    for offset in (10, 5):
        _save_flat_day(db_path, date.today() - timedelta(days=offset), tz, 10.0, "alice@example.com", "Alice Anderson")

    for offset in (82, 77):
        _save_flat_day(db_path, date.today() - timedelta(days=offset), tz, 10.0, "bob@example.com", "Bob Brown")
    for offset in (12, 7):
        _save_flat_day(db_path, date.today() - timedelta(days=offset), tz, 90.0, "bob@example.com", "Bob Brown")

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    leaderboard_tab = at.tabs[0]
    subheader_labels = [s.value for s in leaderboard_tab.subheader]
    assert any("Most Improved" in label for label in subheader_labels)

    assert len(leaderboard_tab.dataframe) == 2
    mini_table = leaderboard_tab.dataframe[1].value  # the "Most Improved" panel
    attendees_in_order = list(mini_table["Attendee"])
    # Alice (trend -80, improving) must rank above Bob (trend +80, worsening).
    assert attendees_in_order.index("Alice Anderson") < attendees_in_order.index("Bob Brown")


def test_dashboard_person_drilldown_default_state_is_clean(monkeypatch, tmp_path, fixtures_dir):
    """Before anyone clicks a row, the drill-down section must stay in its
    clean default state — a caption inviting a click, no chart."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    _save_flat_day(db_path, date.today() - timedelta(days=1), tz, 50.0, "alice@example.com", "Alice Anderson")

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)

    assert not at.exception
    leaderboard_tab = at.tabs[0]
    assert len(leaderboard_tab.get("plotly_chart")) == 0
    captions = " ".join(c.value for c in leaderboard_tab.caption)
    assert "Click a name in either table above" in captions


def test_dashboard_leaderboard_row_selection_shows_person_drilldown(monkeypatch, tmp_path, fixtures_dir):
    """Clicking (selecting) a row in the main leaderboard table must reveal
    that person's dedicated history chart below — the actual selection
    interaction, not just that the underlying data/query works. Follows the
    same click-a-widget -> rerun -> assert pattern as
    test_dashboard_leaderboard_slider_filters_by_meeting_count, but for
    st.dataframe's on_select="rerun" API instead of a slider."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    for offset in (3, 1):
        _save_flat_day(db_path, date.today() - timedelta(days=offset), tz, 45.0, "alice@example.com", "Alice Anderson")

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)
    assert not at.exception

    # Streamlit's dataframe-selection API (verified against the installed
    # streamlit version, not assumed) surfaces selection state through
    # session_state under the widget's key: st.session_state[key] =
    # {"selection": {"rows": [...]}}. Row 0 of the leaderboard table is
    # Alice, its only attendee.
    at.session_state["leaderboard_table"] = {"selection": {"rows": [0]}}
    at.run(timeout=60)

    assert not at.exception
    leaderboard_tab = at.tabs[0]
    subheader_labels = [s.value for s in leaderboard_tab.subheader]
    assert any("Alice Anderson" in label and "stress history" in label for label in subheader_labels)
    assert len(leaderboard_tab.get("plotly_chart")) == 1

    metric_labels = [m.label for m in leaderboard_tab.metric]
    assert "Meetings" in metric_labels
    assert "Avg stress" in metric_labels

    # Clearing the selection returns to the clean default state.
    clear_buttons = [b for b in leaderboard_tab.button if "Clear" in b.label]
    assert len(clear_buttons) == 1
    clear_buttons[0].click()
    at.run(timeout=60)

    assert not at.exception
    leaderboard_tab = at.tabs[0]
    assert len(leaderboard_tab.get("plotly_chart")) == 0
    captions = " ".join(c.value for c in leaderboard_tab.caption)
    assert "Click a name in either table above" in captions


def test_dashboard_most_improved_panel_row_selection_shows_person_drilldown(monkeypatch, tmp_path, fixtures_dir):
    """The drill-down must also be reachable from the "Most Improved" panel,
    not just the main leaderboard table — a separate selectable table with
    its own widget key."""
    db_path = _isolate_dashboard_env(monkeypatch, tmp_path, fixtures_dir)
    tz = ZoneInfo("America/New_York")
    storage.init_db(db_path)
    for offset in (3, 1):
        _save_flat_day(db_path, date.today() - timedelta(days=offset), tz, 45.0, "alice@example.com", "Alice Anderson")

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=60)
    assert not at.exception

    at.session_state["most_improved_table"] = {"selection": {"rows": [0]}}
    at.run(timeout=60)

    assert not at.exception
    leaderboard_tab = at.tabs[0]
    subheader_labels = [s.value for s in leaderboard_tab.subheader]
    assert any("Alice Anderson" in label and "stress history" in label for label in subheader_labels)
    assert len(leaderboard_tab.get("plotly_chart")) == 1
