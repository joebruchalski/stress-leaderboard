"""Streamlit dashboard for the Garmin stress / calendar correlation tool.

Run with:  streamlit run dashboard.py

Reads/writes the same config (~/.config/stress_analyzer/config.json + macOS
Keychain) and SQLite history database as stress_analyzer.py, so results
saved by the automated launchd run show up here, and analyses run from the
dashboard show up if you inspect the DB from the CLI too.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import streamlit as st

import storage
import stress_core

st.set_page_config(page_title="Stress vs. Calendar", layout="wide")


def render_settings_sidebar() -> dict:
    config = stress_core.resolve_config()

    st.sidebar.header("Settings")
    missing = not (config["email"] and config["ics_path"] and config["password"])
    with st.sidebar.form("settings_form", clear_on_submit=False):
        email = st.text_input("Garmin Connect email", value=config["email"])
        ics_path = st.text_input("Path to .ics calendar file", value=config["ics_path"])
        calendar_email = st.text_input(
            "Your own email as it appears in meeting invites",
            value=config["calendar_email"],
            help="Excluded from the 'By Person' tab so you don't show up as your own stressor.",
        )
        internal_domain = st.text_input(
            "Internal email domain (e.g. yourcompany.com)",
            value=config["internal_domain"],
            help="'By Person' only shows attendees on this domain — leave blank to include everyone, including customers/vendors.",
        )
        password = st.text_input(
            "Garmin Connect password",
            type="password",
            placeholder="leave blank to keep saved password",
        )
        submitted = st.form_submit_button("Save settings")

    if submitted:
        if not email or not ics_path:
            st.sidebar.error("Email and .ics path are required.")
        elif not Path(ics_path).expanduser().is_file():
            st.sidebar.error(f"File not found: {ics_path}")
        else:
            stress_core.save_config(
                email, str(Path(ics_path).expanduser()), config["tokenstore"], calendar_email, internal_domain
            )
            if password:
                stress_core.save_keychain_password(email, password)
                st.sidebar.success("Settings saved. Password stored in macOS Keychain.")
            else:
                st.sidebar.success("Settings saved.")
            config = stress_core.resolve_config()

    if missing and not submitted:
        st.sidebar.warning("Fill in your Garmin email, password, and .ics path to run analyses.")

    return config


def render_daily_tab(config: dict, db_path: str) -> None:
    col1, col2 = st.columns([1, 3])
    with col1:
        target_date = st.date_input("Date", value=date.today(), max_value=date.today())
        has_cached = storage.has_day(db_path, target_date)
        force_refresh = st.button(
            "Re-fetch from Garmin" if has_cached else "Fetch from Garmin",
            type="primary" if not has_cached else "secondary",
        )

    ready = config["email"] and config["ics_path"] and config["password"]

    if force_refresh:
        if not ready:
            st.error("Fill in Settings in the sidebar first.")
            return
        with st.spinner(f"Logging into Garmin and correlating {target_date.isoformat()}…"):
            try:
                grid, events, recovery = stress_core.run_analysis(config, target_date)
            except (stress_core.ConfigError, ValueError) as exc:
                st.error(str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - surface any Garmin/network failure
                st.error(f"Analysis failed: {exc}")
                return
            event_summary = stress_core.summarize_by_event(grid)
            deltas = stress_core.compute_meeting_deltas(grid, events)
            event_summary = event_summary.join(deltas)
            storage.init_db(db_path)
            storage.save_day(db_path, target_date, grid, event_summary, events)
            storage.save_recovery_day(db_path, target_date, recovery["sleep"], recovery["body_battery"])
        st.success("Done.")
        has_cached = True

    if not has_cached:
        st.info(f"No stored results for {target_date.isoformat()} yet. Click **Fetch from Garmin** to run it.")
        return

    grid = storage.load_day_grid(db_path, target_date)
    events = storage.load_day_events(db_path, target_date)
    event_summary = storage.load_event_summary(db_path, target_date)

    valid = grid.dropna(subset=["stress"])
    with col1:
        if not valid.empty:
            st.metric("Workday average stress", f"{valid['stress'].mean():.1f}")
            st.metric("Workday peak stress", f"{valid['stress'].max():.0f}")

    with col2:
        if grid.empty:
            st.info("No data for this date.")
        else:
            fig = stress_core.build_daily_chart(grid, events, target_date)
            st.pyplot(fig, width="stretch")

    st.subheader("Stress by meeting")
    if event_summary.empty:
        st.info("No valid stress readings for this date.")
    else:
        st.dataframe(
            event_summary.style.format({"avg_stress": "{:.1f}", "peak_stress": "{:.0f}", "minutes": "{:.0f}"}),
            width="stretch",
        )


def render_trends_tab(db_path: str) -> None:
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From", value=date.today() - timedelta(days=13), key="trend_start")
    with col2:
        end_date = st.date_input("To", value=date.today(), key="trend_end")

    if start_date > end_date:
        st.error("Start date must be before end date.")
        return

    daily_summary = storage.load_daily_summary_range(db_path, start_date, end_date)
    if daily_summary.empty:
        st.info("No stored results in this range yet. Run some daily analyses first (Daily Detail tab, or the automated job).")
        return

    fig = stress_core.build_trend_chart(daily_summary)
    st.pyplot(fig, width="stretch")

    st.subheader("Average stress by meeting, across this range")
    rollup = storage.load_event_rollup(db_path, start_date, end_date)
    if rollup.empty:
        st.info("No meeting data in this range.")
    else:
        st.dataframe(
            rollup.style.format({"avg_stress": "{:.1f}", "peak_stress": "{:.0f}", "total_minutes": "{:.0f}", "occurrences": "{:.0f}"}),
            width="stretch",
        )


def render_people_tab(db_path: str) -> None:
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From", value=date.today() - timedelta(days=13), key="people_start")
    with col2:
        end_date = st.date_input("To", value=date.today(), key="people_end")

    if start_date > end_date:
        st.error("Start date must be before end date.")
        return

    rollup = storage.load_person_rollup(db_path, start_date, end_date)
    if rollup.empty:
        st.info(
            "No attendee data in this range yet. This needs your 'own email' set in Settings "
            "(so you can be excluded) and at least one analysis run in this range."
        )
        return

    max_meetings = int(rollup["meetings"].max())
    if max_meetings > 1:
        min_meetings = st.slider(
            "Minimum meetings together (filters out noisy one-off large invites)",
            min_value=1,
            max_value=max_meetings,
            value=min(2, max_meetings),
        )
    else:
        st.caption("Not enough history yet for a minimum-meetings filter — showing everyone.")
        min_meetings = 1
    filtered = rollup[rollup["meetings"] >= min_meetings]
    if filtered.empty:
        st.info("No one meets that threshold in this range — lower the slider.")
        return

    top_n = filtered.head(20)
    fig = stress_core.build_person_rollup_chart(top_n)
    st.pyplot(fig, width="stretch")

    st.subheader("Average stress by meeting attendee")
    st.dataframe(
        filtered.style.format({"avg_stress": "{:.1f}", "peak_stress": "{:.0f}", "total_minutes": "{:.0f}", "meetings": "{:.0f}"}),
        width="stretch",
    )
    st.caption(
        "An event's average/peak stress applies to everyone who attended it — this shows who you're "
        "in stressful meetings WITH, not who specifically causes the stress within a group call."
    )


def render_patterns_tab(db_path: str) -> None:
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From", value=date.today() - timedelta(days=13), key="patterns_start")
    with col2:
        end_date = st.date_input("To", value=date.today(), key="patterns_end")

    if start_date > end_date:
        st.error("Start date must be before end date.")
        return

    by_weekday = storage.load_stress_by_weekday(db_path, start_date, end_date)
    st.subheader("By day of week")
    if by_weekday.empty:
        st.info("No stored results in this range yet. Run some daily analyses first (Daily Detail tab, or the automated job).")
    else:
        fig = stress_core.build_weekday_chart(by_weekday)
        st.pyplot(fig, width="stretch")

    by_hour = storage.load_stress_by_hour(db_path, start_date, end_date)
    st.subheader("By time of day")
    if by_hour.empty:
        st.info("No stored results in this range yet. Run some daily analyses first (Daily Detail tab, or the automated job).")
    else:
        fig = stress_core.build_hourly_chart(by_hour)
        st.pyplot(fig, width="stretch")


def render_recovery_tab(db_path: str) -> None:
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From", value=date.today() - timedelta(days=13), key="recovery_start")
    with col2:
        end_date = st.date_input("To", value=date.today(), key="recovery_end")

    if start_date > end_date:
        st.error("Start date must be before end date.")
        return

    try:
        correlation = storage.load_recovery_correlation(db_path, start_date, end_date)
    except Exception as exc:  # noqa: BLE001 - never let a query error crash the tab
        st.error(f"Could not load recovery data: {exc}")
        return

    if correlation.empty:
        st.info(
            "No sleep/Body Battery data stored for this range yet. Run an analysis "
            "(Daily Detail tab, or the automated job) on a day with synced Garmin sleep data first."
        )
        return

    st.caption(
        "Each dot is one day: does a worse night's sleep or a lower Body Battery line up "
        "with a higher-stress workday?"
    )

    sleep_df = correlation.dropna(subset=["sleep_score", "overall_avg"])
    col_sleep, col_battery = st.columns(2)

    with col_sleep:
        if sleep_df.empty:
            st.info("No overlapping sleep score + stress data in this range.")
        else:
            fig = stress_core.build_recovery_scatter_chart(
                sleep_df, "sleep_score", "Sleep score (0-100)", "Sleep Score vs. Workday Stress"
            )
            st.pyplot(fig, width="stretch")

    battery_df = correlation.dropna(subset=["body_battery_high", "overall_avg"])
    with col_battery:
        if battery_df.empty:
            st.info("No overlapping Body Battery + stress data in this range.")
        else:
            fig = stress_core.build_recovery_scatter_chart(
                battery_df,
                "body_battery_high",
                "Body Battery, morning high (0-100)",
                "Body Battery vs. Workday Stress",
            )
            st.pyplot(fig, width="stretch")

    st.subheader("Recovery and stress by day")
    st.dataframe(
        correlation.style.format(
            {
                "sleep_score": "{:.0f}",
                "total_sleep_minutes": "{:.0f}",
                "body_battery_low": "{:.0f}",
                "body_battery_high": "{:.0f}",
                "overall_avg": "{:.1f}",
            },
            na_rep="-",
        ),
        width="stretch",
    )


def main() -> None:
    st.title("Stress vs. Calendar")
    config = render_settings_sidebar()
    db_path = stress_core.DEFAULT_DB_PATH
    storage.init_db(db_path)

    tab_daily, tab_trends, tab_people, tab_patterns, tab_recovery = st.tabs(
        ["Daily Detail", "Trends", "By Person", "Patterns", "Recovery"]
    )
    with tab_daily:
        render_daily_tab(config, db_path)
    with tab_trends:
        render_trends_tab(db_path)
    with tab_people:
        render_people_tab(db_path)
    with tab_patterns:
        render_patterns_tab(db_path)
    with tab_recovery:
        render_recovery_tab(db_path)


if __name__ == "__main__":
    main()
