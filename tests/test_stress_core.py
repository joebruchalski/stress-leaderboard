"""Tests for stress_core.py: the minute-grid correlation logic and calendar
parsing. These are the tests that would have caught the real bug found while
investigating "nothing is actually working": attach_stress() crashed with a
pandas MergeError any time it ran against a real minute grid + real Garmin
stress payload, because the two sides had matching timezone but different
datetime64 resolution ("us" vs "ms"). See test_attach_stress_dtype_mismatch_*
below.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pytest

import stress_core


# --------------------------------------------------------------------------
# build_minute_grid
# --------------------------------------------------------------------------

def test_build_minute_grid_covers_full_workday(local_tz):
    grid = stress_core.build_minute_grid(date(2024, 1, 15), local_tz)

    assert grid["timestamp_local"].iloc[0] == datetime.combine(
        date(2024, 1, 15), time(9, 0), tzinfo=local_tz
    )
    assert grid["timestamp_local"].iloc[-1] == datetime.combine(
        date(2024, 1, 15), time(17, 0), tzinfo=local_tz
    )
    # 9:00 through 17:00 inclusive, one row per minute.
    assert len(grid) == 8 * 60 + 1
    assert str(grid["timestamp_local"].dt.tz) == str(local_tz)


# --------------------------------------------------------------------------
# attach_stress
# --------------------------------------------------------------------------

def _garmin_style_stress_df(local_tz, timestamps_ms_and_values):
    """Build a stress DataFrame exactly the way fetch_stress_minutes() does
    in production (pd.to_datetime(..., unit="ms") then tz_convert), so tests
    exercise the real dtype, not a hand-picked one."""
    df = pd.DataFrame(timestamps_ms_and_values, columns=["timestamp_ms", "stress"])
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["timestamp_local"] = df["timestamp_utc"].dt.tz_convert(local_tz)
    return df[["timestamp_local", "stress"]]


def test_attach_stress_dtype_mismatch_regression(local_tz):
    """Regression test for the actual production bug: build_minute_grid()'s
    pd.date_range() produces datetime64[us, tz] timestamps, while
    fetch_stress_minutes()'s pd.to_datetime(..., unit="ms") produces
    datetime64[ms, tz] timestamps. Same tz, different resolution. Before the
    fix, pd.merge_asof() raised:
        MergeError: incompatible merge keys ... must be the same type
    on every single real run that got past the Garmin fetch — which is why
    the user's history.db had zero rows despite a working Garmin login."""
    grid = stress_core.build_minute_grid(date(2024, 1, 15), local_tz)

    nine_am_ms = int(
        datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz).timestamp() * 1000
    )
    stress_df = _garmin_style_stress_df(
        local_tz, [(nine_am_ms, 42), (nine_am_ms + 120_000, 55)]
    )

    # Confirm the two sides really do start out with different resolutions —
    # otherwise this test wouldn't actually exercise the bug that was fixed.
    assert grid["timestamp_local"].dtype != stress_df["timestamp_local"].dtype

    # This must not raise.
    result = stress_core.attach_stress(grid, stress_df)

    assert len(result) == len(grid)
    assert result["stress"].notna().any()


def test_attach_stress_nearest_within_tolerance(local_tz):
    grid = stress_core.build_minute_grid(date(2024, 1, 15), local_tz)
    reading_time = datetime.combine(date(2024, 1, 15), time(9, 1), tzinfo=local_tz)
    stress_df = pd.DataFrame({"timestamp_local": [pd.Timestamp(reading_time)], "stress": [77.0]})

    result = stress_core.attach_stress(grid, stress_df)
    row = result.loc[result["timestamp_local"] == pd.Timestamp(reading_time)]
    assert row["stress"].iloc[0] == 77.0


def test_attach_stress_outside_tolerance_is_nan(local_tz):
    grid = stress_core.build_minute_grid(date(2024, 1, 15), local_tz)
    far_time = datetime.combine(date(2024, 1, 15), time(13, 0), tzinfo=local_tz)
    stress_df = pd.DataFrame({"timestamp_local": [pd.Timestamp(far_time)], "stress": [99.0]})

    result = stress_core.attach_stress(grid, stress_df)
    nine_am_row = result.loc[
        result["timestamp_local"] == pd.Timestamp(datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz))
    ]
    assert pd.isna(nine_am_row["stress"].iloc[0])


# --------------------------------------------------------------------------
# attach_events
# --------------------------------------------------------------------------

def test_attach_events_labels_minutes_and_fills_no_meeting(local_tz):
    grid = stress_core.build_minute_grid(date(2024, 1, 15), local_tz)
    events = [
        {
            "title": "Standup",
            "start": datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz),
            "end": datetime.combine(date(2024, 1, 15), time(9, 15), tzinfo=local_tz),
        }
    ]
    result = stress_core.attach_events(grid, events)

    in_meeting = result[
        (result["timestamp_local"] >= pd.Timestamp(events[0]["start"]))
        & (result["timestamp_local"] < pd.Timestamp(events[0]["end"]))
    ]
    assert (in_meeting["event"] == "Standup").all()

    after_meeting = result[result["timestamp_local"] >= pd.Timestamp(events[0]["end"])]
    assert (after_meeting["event"] == "No Meeting").all()


def test_attach_events_overlap_first_started_wins(local_tz):
    """Overlapping events: first-started-wins, applied in start-time order."""
    grid = stress_core.build_minute_grid(date(2024, 1, 15), local_tz)
    early = {
        "title": "Early Meeting",
        "start": datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz),
        "end": datetime.combine(date(2024, 1, 15), time(9, 30), tzinfo=local_tz),
    }
    overlapping = {
        "title": "Overlapping Meeting",
        "start": datetime.combine(date(2024, 1, 15), time(9, 10), tzinfo=local_tz),
        "end": datetime.combine(date(2024, 1, 15), time(9, 40), tzinfo=local_tz),
    }
    result = stress_core.attach_events(grid, [early, overlapping])

    at_9_20 = result.loc[
        result["timestamp_local"] == pd.Timestamp(datetime.combine(date(2024, 1, 15), time(9, 20), tzinfo=local_tz))
    ]
    assert at_9_20["event"].iloc[0] == "Early Meeting"

    at_9_35 = result.loc[
        result["timestamp_local"] == pd.Timestamp(datetime.combine(date(2024, 1, 15), time(9, 35), tzinfo=local_tz))
    ]
    assert at_9_35["event"].iloc[0] == "Overlapping Meeting"


# --------------------------------------------------------------------------
# compute_meeting_deltas
# --------------------------------------------------------------------------

def _grid_with_event(local_tz, target_date, events):
    """A full 9-5 workday grid with the given events labeled onto it via the
    real attach_events(), and a uniform placeholder stress value everywhere
    (tests override specific windows afterward)."""
    grid = stress_core.build_minute_grid(target_date, local_tz)
    grid = stress_core.attach_events(grid, events)
    grid["stress"] = 10.0
    return grid


def _set_stress_window(grid, start, end, value):
    mask = (grid["timestamp_local"] >= pd.Timestamp(start)) & (grid["timestamp_local"] < pd.Timestamp(end))
    grid.loc[mask, "stress"] = value


def test_compute_meeting_deltas_positive_when_stress_rises_into_meeting(local_tz):
    target_date = date(2024, 1, 15)
    event = {
        "title": "Standup",
        "start": datetime.combine(target_date, time(10, 0), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(10, 15), tzinfo=local_tz),
    }
    grid = _grid_with_event(local_tz, target_date, [event])
    _set_stress_window(grid, datetime.combine(target_date, time(9, 45), tzinfo=local_tz), event["start"], 20.0)
    _set_stress_window(grid, event["start"], event["end"], 60.0)

    deltas = stress_core.compute_meeting_deltas(grid, [event])
    assert deltas.loc["Standup", "delta_stress"] == pytest.approx(40.0)


def test_compute_meeting_deltas_negative_when_stress_drops_into_meeting(local_tz):
    target_date = date(2024, 1, 15)
    event = {
        "title": "Focus Time Killer",
        "start": datetime.combine(target_date, time(14, 0), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(14, 15), tzinfo=local_tz),
    }
    grid = _grid_with_event(local_tz, target_date, [event])
    _set_stress_window(grid, datetime.combine(target_date, time(13, 45), tzinfo=local_tz), event["start"], 70.0)
    _set_stress_window(grid, event["start"], event["end"], 30.0)

    deltas = stress_core.compute_meeting_deltas(grid, [event])
    assert deltas.loc["Focus Time Killer", "delta_stress"] == pytest.approx(-40.0)


def test_compute_meeting_deltas_nan_when_meeting_starts_at_workday_open(local_tz):
    """A 9:00 AM meeting has no minutes at all before it in the workday
    grid — the baseline window can't be satisfied, so the delta must be NaN,
    not fabricated from a truncated/missing baseline."""
    target_date = date(2024, 1, 15)
    event = {
        "title": "Early Bird",
        "start": datetime.combine(target_date, time(9, 0), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(9, 15), tzinfo=local_tz),
    }
    grid = _grid_with_event(local_tz, target_date, [event])

    deltas = stress_core.compute_meeting_deltas(grid, [event])
    assert pd.isna(deltas.loc["Early Bird", "delta_stress"])


def test_compute_meeting_deltas_nan_when_immediately_preceded_by_another_meeting(local_tz):
    """Back-to-back meetings with no gap: the "baseline" minutes right
    before the second meeting are actually claimed by the first meeting in
    the grid, not "No Meeting" — so they don't count as genuine free time
    and the delta must be NaN."""
    target_date = date(2024, 1, 15)
    event_a = {
        "title": "Event A",
        "start": datetime.combine(target_date, time(9, 0), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(9, 30), tzinfo=local_tz),
    }
    event_b = {
        "title": "Event B",
        "start": datetime.combine(target_date, time(9, 30), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(9, 45), tzinfo=local_tz),
    }
    grid = _grid_with_event(local_tz, target_date, [event_a, event_b])

    deltas = stress_core.compute_meeting_deltas(grid, [event_a, event_b])
    assert pd.isna(deltas.loc["Event A", "delta_stress"])  # nothing before workday open
    assert pd.isna(deltas.loc["Event B", "delta_stress"])  # baseline window is all Event A


def test_compute_meeting_deltas_nan_when_baseline_window_too_short(local_tz):
    """A meeting at 9:10 AM only has 10 minutes of genuinely free time
    before it (9:00-9:10), short of the default 15-minute baseline window —
    must be NaN, not computed from a truncated baseline."""
    target_date = date(2024, 1, 15)
    event = {
        "title": "Late Standup",
        "start": datetime.combine(target_date, time(9, 10), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(9, 20), tzinfo=local_tz),
    }
    grid = _grid_with_event(local_tz, target_date, [event])

    deltas = stress_core.compute_meeting_deltas(grid, [event])
    assert pd.isna(deltas.loc["Late Standup", "delta_stress"])


def test_compute_meeting_deltas_averages_multiple_occurrences_ignoring_nan(local_tz):
    """A recurring title that happens twice in one day: one occurrence has
    no computable baseline (NaN), the other does. The title's row must be
    the valid occurrence's delta alone, not averaged down by the NaN one."""
    target_date = date(2024, 1, 15)
    occurrence_1 = {
        "title": "Standup",
        "start": datetime.combine(target_date, time(9, 0), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(9, 15), tzinfo=local_tz),
    }
    occurrence_2 = {
        "title": "Standup",
        "start": datetime.combine(target_date, time(11, 0), tzinfo=local_tz),
        "end": datetime.combine(target_date, time(11, 15), tzinfo=local_tz),
    }
    grid = _grid_with_event(local_tz, target_date, [occurrence_1, occurrence_2])
    _set_stress_window(
        grid, datetime.combine(target_date, time(10, 45), tzinfo=local_tz), occurrence_2["start"], 20.0
    )
    _set_stress_window(grid, occurrence_2["start"], occurrence_2["end"], 50.0)

    deltas = stress_core.compute_meeting_deltas(grid, [occurrence_1, occurrence_2])
    assert deltas.loc["Standup", "delta_stress"] == pytest.approx(30.0)


def test_compute_meeting_deltas_no_events_returns_empty_frame(local_tz):
    target_date = date(2024, 1, 15)
    grid = stress_core.build_minute_grid(target_date, local_tz)
    grid["event"] = "No Meeting"
    grid["stress"] = 10.0

    deltas = stress_core.compute_meeting_deltas(grid, [])
    assert deltas.empty
    assert list(deltas.columns) == ["delta_stress"]


# --------------------------------------------------------------------------
# fetch_stress_minutes: invalid-value handling
# --------------------------------------------------------------------------

class _FakeGarminApi:
    def __init__(self, values, sleep_response=None, body_battery_response=None, raise_on_sleep=False, raise_on_body_battery=False):
        self._values = values
        self._sleep_response = sleep_response
        self._body_battery_response = body_battery_response
        self._raise_on_sleep = raise_on_sleep
        self._raise_on_body_battery = raise_on_body_battery

    def get_stress_data(self, date_str):
        return {"stressValuesArray": self._values}

    def get_sleep_data(self, cdate):
        if self._raise_on_sleep:
            raise RuntimeError("simulated Garmin failure")
        return self._sleep_response

    def get_body_battery(self, startdate, enddate=None):
        if self._raise_on_body_battery:
            raise RuntimeError("simulated Garmin failure")
        return self._body_battery_response


def test_fetch_stress_minutes_converts_invalid_sentinels_to_nan(local_tz):
    base_ms = int(
        datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz).timestamp() * 1000
    )
    api = _FakeGarminApi(
        [
            [base_ms, 40],
            [base_ms + 60_000, -1],  # Garmin "not measurable" sentinel
            [base_ms + 120_000, -2],  # Garmin "no data" sentinel
            [base_ms + 180_000, 60],
        ]
    )
    df = stress_core.fetch_stress_minutes(api, date(2024, 1, 15), local_tz)

    assert list(df["stress"]) == [40, None, None, 60] or df["stress"].isna().sum() == 2
    assert df["stress"].isna().sum() == 2
    assert df["stress"].notna().sum() == 2


def test_fetch_stress_minutes_raises_when_no_data(local_tz):
    api = _FakeGarminApi([])
    with pytest.raises(ValueError):
        stress_core.fetch_stress_minutes(api, date(2024, 1, 15), local_tz)


# --------------------------------------------------------------------------
# fetch_sleep_summary / fetch_body_battery_summary: real-shaped responses,
# observed via a live call against get_sleep_data("2026-09-21") and
# get_body_battery("2026-09-21", "2026-09-21") — not guessed. Both must be
# resilient (return None, never raise) on missing/empty/erroring data, since
# this is supplementary data that must not crash the core stress analysis.
# --------------------------------------------------------------------------

REAL_SHAPED_SLEEP_RESPONSE = {
    "dailySleepDTO": {
        "calendarDate": "2026-09-21",
        "sleepTimeSeconds": 25320,
        "deepSleepSeconds": 1860,
        "lightSleepSeconds": 20340,
        "remSleepSeconds": 3120,
        "awakeSleepSeconds": 3840,
        "avgSleepStress": 21.0,
        "sleepScores": {
            "overall": {"value": 54, "qualifierKey": "POOR"},
        },
    },
    "sleepMovement": [],
}

REAL_SHAPED_BODY_BATTERY_RESPONSE = [
    {
        "date": "2026-09-21",
        "charged": 77,
        "drained": 52,
        "bodyBatteryValuesArray": [
            [1789964820000, 32],
            [1789991460000, 75],
            [1789995600000, 82],
            [1790019180000, 55],
            [1790035200000, 32],
            [1790049420000, 59],
        ],
    }
]


def test_fetch_sleep_summary_parses_real_shaped_response():
    api = _FakeGarminApi([], sleep_response=REAL_SHAPED_SLEEP_RESPONSE)
    result = stress_core.fetch_sleep_summary(api, date(2026, 9, 21))
    assert result == {"sleep_score": 54.0, "total_sleep_minutes": pytest.approx(422.0)}


def test_fetch_sleep_summary_returns_none_when_no_dto():
    api = _FakeGarminApi([], sleep_response={"dailySleepDTO": None})
    assert stress_core.fetch_sleep_summary(api, date(2026, 9, 21)) is None

    api_empty = _FakeGarminApi([], sleep_response={})
    assert stress_core.fetch_sleep_summary(api_empty, date(2026, 9, 21)) is None

    api_none = _FakeGarminApi([], sleep_response=None)
    assert stress_core.fetch_sleep_summary(api_none, date(2026, 9, 21)) is None


def test_fetch_sleep_summary_returns_none_instead_of_raising_on_error():
    api = _FakeGarminApi([], raise_on_sleep=True)
    assert stress_core.fetch_sleep_summary(api, date(2026, 9, 21)) is None


def test_fetch_body_battery_summary_parses_real_shaped_response():
    api = _FakeGarminApi([], body_battery_response=REAL_SHAPED_BODY_BATTERY_RESPONSE)
    result = stress_core.fetch_body_battery_summary(api, date(2026, 9, 21))
    assert result == {"body_battery_low": 32.0, "body_battery_high": 82.0}


def test_fetch_body_battery_summary_returns_none_when_empty():
    api = _FakeGarminApi([], body_battery_response=[])
    assert stress_core.fetch_body_battery_summary(api, date(2026, 9, 21)) is None

    api_no_values = _FakeGarminApi(
        [], body_battery_response=[{"date": "2026-09-21", "bodyBatteryValuesArray": []}]
    )
    assert stress_core.fetch_body_battery_summary(api_no_values, date(2026, 9, 21)) is None

    api_none = _FakeGarminApi([], body_battery_response=None)
    assert stress_core.fetch_body_battery_summary(api_none, date(2026, 9, 21)) is None


def test_fetch_body_battery_summary_returns_none_instead_of_raising_on_error():
    api = _FakeGarminApi([], raise_on_body_battery=True)
    assert stress_core.fetch_body_battery_summary(api, date(2026, 9, 21)) is None


# --------------------------------------------------------------------------
# Calendar parsing (.ics fixtures)
# --------------------------------------------------------------------------

def test_load_calendar_events_simple_timed_event(fixtures_dir, local_tz):
    events = stress_core.load_calendar_events(fixtures_dir / "simple.ics", date(2024, 1, 15), local_tz)
    assert [e["title"] for e in events] == ["Simple Meeting"]
    assert events[0]["start"] == datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz)
    assert events[0]["end"] == datetime.combine(date(2024, 1, 15), time(10, 0), tzinfo=local_tz)


def test_load_calendar_events_expands_recurring_event(fixtures_dir, local_tz):
    events = stress_core.load_calendar_events(fixtures_dir / "recurring.ics", date(2024, 1, 15), local_tz)
    assert [e["title"] for e in events] == ["Weekly Standup"]
    assert events[0]["start"] == datetime.combine(date(2024, 1, 15), time(11, 0), tzinfo=local_tz)

    # And the occurrence a week earlier also expands correctly.
    prior_week = stress_core.load_calendar_events(fixtures_dir / "recurring.ics", date(2024, 1, 8), local_tz)
    assert [e["title"] for e in prior_week] == ["Weekly Standup"]

    # A non-Monday has no occurrence.
    off_day = stress_core.load_calendar_events(fixtures_dir / "recurring.ics", date(2024, 1, 16), local_tz)
    assert off_day == []


def test_load_calendar_events_excludes_all_day_events(fixtures_dir, local_tz):
    """All-day events have no clock time to correlate against a minute grid
    and must be skipped, not crash or appear as a bogus all-day span."""
    events = stress_core.load_calendar_events(fixtures_dir / "allday.ics", date(2024, 1, 15), local_tz)
    assert events == []


def test_load_calendar_events_combined_fixture(fixtures_dir, local_tz):
    events = stress_core.load_calendar_events(fixtures_dir / "combined.ics", date(2024, 1, 15), local_tz)
    titles = sorted(e["title"] for e in events)
    assert titles == ["Simple Meeting", "Weekly Standup"]  # all-day excluded, others present
    # sorted by start ascending
    assert events == sorted(events, key=lambda e: e["start"])


# --------------------------------------------------------------------------
# Attendee extraction (real calendar exports are messy: CN sometimes equals
# the email, room/resource attendees show up alongside people, and the
# calendar owner appears as their own attendee)
# --------------------------------------------------------------------------

def test_load_calendar_events_extracts_attendees_excludes_self_and_rooms(fixtures_dir, local_tz):
    events = stress_core.load_calendar_events(
        fixtures_dir / "attendees.ics", date(2024, 1, 15), local_tz, self_email="me@example.com"
    )
    assert [e["title"] for e in events] == ["Team Sync"]
    attendees = events[0]["attendees"]

    # Self (me@example.com) and the CUTYPE=ROOM resource must not appear.
    emails = [a["email"] for a in attendees]
    assert "me@example.com" not in emails
    assert "room-a@example.com" not in emails

    # Alice's CN is a real name; Bob's CN is just his own email, so his
    # display name must be derived from the email local part instead.
    by_email = {a["email"]: a["name"] for a in attendees}
    assert by_email["alice@example.com"] == "Alice Anderson"
    assert by_email["bob@example.com"] == "Bob"

    # Alice appears twice in the raw .ics (duplicate ATTENDEE line) — deduped.
    assert emails.count("alice@example.com") == 1


def test_load_calendar_events_no_self_email_keeps_everyone(fixtures_dir, local_tz):
    """Without a configured self_email, nothing is excluded on that basis —
    only the CUTYPE=ROOM resource is filtered."""
    events = stress_core.load_calendar_events(fixtures_dir / "attendees.ics", date(2024, 1, 15), local_tz)
    emails = [a["email"] for a in events[0]["attendees"]]
    assert "me@example.com" in emails
    assert "room-a@example.com" not in emails


def test_extract_attendees_handles_single_non_list_attendee():
    """icalendar returns a bare vCalAddress (not a list) when a VEVENT has
    exactly one ATTENDEE property — must not crash on that shape."""
    from icalendar import Event

    event = Event()
    event.add("summary", "Solo invite")
    event.add("attendee", "mailto:alice@example.com", parameters={"CUTYPE": "INDIVIDUAL", "CN": "Alice"})

    attendees = stress_core.extract_attendees(event, self_email="")
    assert attendees == [{"email": "alice@example.com", "name": "Alice"}]


def test_extract_attendees_filters_by_internal_domain():
    """internal_domain excludes customers/vendors on other domains, so 'By
    Person' only reflects colleagues."""
    from icalendar import Event

    event = Event()
    event.add("summary", "Customer Call")
    event.add("attendee", "mailto:colleague@company.com", parameters={"CUTYPE": "INDIVIDUAL", "CN": "Colleague"})
    event.add("attendee", "mailto:customer@othercorp.com", parameters={"CUTYPE": "INDIVIDUAL", "CN": "Customer"})

    filtered = stress_core.extract_attendees(event, self_email="", internal_domain="company.com")
    assert filtered == [{"email": "colleague@company.com", "name": "Colleague"}]

    unfiltered = stress_core.extract_attendees(event, self_email="", internal_domain="")
    assert {a["email"] for a in unfiltered} == {"colleague@company.com", "customer@othercorp.com"}

    # A leading "@" in the configured domain (easy to type by habit) is tolerated.
    at_prefixed = stress_core.extract_attendees(event, self_email="", internal_domain="@company.com")
    assert at_prefixed == [{"email": "colleague@company.com", "name": "Colleague"}]


def test_guess_internal_domain_from_calendar_email():
    assert stress_core.guess_internal_domain("joe@company.com") == "company.com"
    assert stress_core.guess_internal_domain("") == ""
    assert stress_core.guess_internal_domain("not-an-email") == ""


# --------------------------------------------------------------------------
# Analytics helpers
# --------------------------------------------------------------------------

def test_summarize_by_event_averages_and_sorts(local_tz):
    grid = pd.DataFrame(
        {
            "timestamp_local": pd.date_range(
                datetime.combine(date(2024, 1, 15), time(9, 0), tzinfo=local_tz), periods=4, freq="1min"
            ),
            "stress": [10.0, 20.0, 80.0, None],
            "event": ["A", "A", "B", "B"],
        }
    )
    summary = stress_core.summarize_by_event(grid)
    assert list(summary.index) == ["B", "A"]  # B has higher avg (80 vs no valid... )
    assert summary.loc["A", "avg_stress"] == 15.0
    assert summary.loc["A", "minutes"] == 2
    assert summary.loc["B", "minutes"] == 1  # the NaN stress minute is dropped


def test_summarize_by_event_all_invalid_returns_empty(local_tz):
    grid = pd.DataFrame({"timestamp_local": [pd.Timestamp("2024-01-15 09:00")], "stress": [None], "event": ["A"]})
    summary = stress_core.summarize_by_event(grid)
    assert summary.empty


def test_build_event_color_map_overflows_past_palette():
    events = [{"title": f"Event {i}", "start": None, "end": None} for i in range(10)]
    color_map = stress_core.build_event_color_map(events)
    assert len(color_map) == 10
    for i in range(len(stress_core.EVENT_COLORS)):
        assert color_map[f"Event {i}"] == stress_core.EVENT_COLORS[i]
    assert color_map["Event 8"] == stress_core.OVERFLOW_COLOR
    assert color_map["Event 9"] == stress_core.OVERFLOW_COLOR
