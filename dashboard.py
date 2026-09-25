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


def render_settings_popover() -> dict:
    """Settings live in a top-right dropdown, not a permanent left sidebar —
    this is a personal single-user tool used occasionally, not a multi-page
    app that needs sidebar navigation, so a full-height sidebar was just
    wasted screen width."""
    config = stress_core.resolve_config()
    missing = not (config["email"] and config["ics_path"] and config["password"])

    with st.popover(("⚠️" if missing else "⋮"), help="Settings"):
        with st.form("settings_form", clear_on_submit=False):
            email = st.text_input("Garmin Connect email", value=config["email"])
            ics_path = st.text_input("Path to .ics calendar file", value=config["ics_path"])
            calendar_email = st.text_input(
                "Your own email as it appears in meeting invites",
                value=config["calendar_email"],
                help="Excluded from the leaderboard so you don't show up as your own stressor.",
            )
            internal_domain = st.text_input(
                "Internal email domain (e.g. yourcompany.com)",
                value=config["internal_domain"],
                help="Leaderboard only shows attendees on this domain — leave blank to include everyone, including customers/vendors.",
            )
            password = st.text_input(
                "Garmin Connect password",
                type="password",
                placeholder="leave blank to keep saved password",
            )
            submitted = st.form_submit_button("Save settings")

        if submitted:
            if not email or not ics_path:
                st.error("Email and .ics path are required.")
            elif not Path(ics_path).expanduser().is_file():
                st.error(f"File not found: {ics_path}")
            else:
                stress_core.save_config(
                    email, str(Path(ics_path).expanduser()), config["tokenstore"], calendar_email, internal_domain
                )
                if password:
                    stress_core.save_keychain_password(email, password)
                    st.success("Settings saved. Password stored in macOS Keychain.")
                else:
                    st.success("Settings saved.")
                config = stress_core.resolve_config()

        if missing and not submitted:
            st.warning("Fill in your Garmin email, password, and .ics path to run analyses.")

    return config


def _shift_daily_detail_date(delta_days: int) -> None:
    """on_click callback, not a plain post-hoc `if button: mutate` check —
    Streamlit runs on_click callbacks BEFORE the script body re-executes, so
    st.session_state.daily_detail_date is already updated by the time the
    "next day" button's own `disabled=` condition is evaluated later in the
    same run. A plain post-hoc mutation (mutate only after checking whether
    the button was clicked, further down in the script) evaluates that
    `disabled=` against the PRE-click date, showing "▶" as wrongly disabled
    for one render right after clicking "◀" from today — a real bug caught
    by testing the actual button interaction, not just eyeballing the code."""
    new_date = st.session_state.daily_detail_date + timedelta(days=delta_days)
    st.session_state.daily_detail_date = min(new_date, date.today())


def render_daily_tab(config: dict, db_path: str) -> None:
    """One compact control/metrics row up top, then the chart gets the full
    width below it — it's the reason for the tab, not a box squeezed next to
    the date picker, and a wide chart makes an interactive hover layer
    actually worth using (more room per minute to aim the pointer at)."""
    if "daily_detail_date" not in st.session_state:
        st.session_state.daily_detail_date = date.today()

    nav_prev, nav_date, nav_next, fetch_col, metric_col1, metric_col2 = st.columns([0.5, 1.6, 0.5, 1.4, 1, 1])

    with nav_prev:
        st.write("")
        st.button("◀", help="Previous day", on_click=_shift_daily_detail_date, args=(-1,))
    with nav_date:
        target_date = st.date_input("Date", key="daily_detail_date", max_value=date.today())
    with nav_next:
        st.write("")
        st.button(
            "▶",
            help="Next day",
            on_click=_shift_daily_detail_date,
            args=(1,),
            disabled=st.session_state.daily_detail_date >= date.today(),
        )

    with fetch_col:
        st.write("")
        has_cached = storage.has_day(db_path, target_date)
        force_refresh = st.button(
            "Re-fetch from Garmin" if has_cached else "Fetch from Garmin",
            type="primary" if not has_cached else "secondary",
        )

    ready = config["email"] and config["ics_path"] and config["password"]

    if force_refresh:
        if not ready:
            st.error("Fill in Settings (top right) first.")
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
    with metric_col1:
        if not valid.empty:
            st.metric("Workday average stress", f"{valid['stress'].mean():.1f}")
    with metric_col2:
        if not valid.empty:
            st.metric("Workday peak stress", f"{valid['stress'].max():.0f}")

    st.divider()

    if grid.empty:
        st.info("No data for this date.")
    else:
        fig = stress_core.build_daily_chart_interactive(grid, events, target_date)
        st.plotly_chart(fig, width="stretch", theme=None)
        st.caption(
            "Hover anywhere on the chart for the exact time, stress level, and which meeting "
            "(or **No Meeting**) you were in at that moment."
        )

    st.subheader("Stress by meeting")
    if event_summary.empty:
        st.info("No valid stress readings for this date.")
    else:
        st.dataframe(
            event_summary.style.format({"avg_stress": "{:.1f}", "peak_stress": "{:.0f}", "minutes": "{:.0f}"}),
            width="stretch",
        )


def render_week_over_week_stat(db_path: str) -> None:
    """'Am I trending better or worse lately' — this week's average stress
    vs. the week before, as a single stat with a directional arrow, not just
    the all-time average the trend chart already shows below it. Placed
    before the trend chart since it's the headline answer to "how am I
    doing lately"; the chart is the supporting detail underneath."""
    week = storage.load_week_over_week(db_path)
    if week["recent_avg"] is None:
        st.caption("Not enough recent history yet for a week-over-week comparison.")
        return

    delta = week["delta"]
    st.metric(
        "This week's average stress vs. last week",
        f"{week['recent_avg']:.1f}",
        delta=f"{delta:+.1f}" if delta is not None else None,
        delta_color="inverse",  # more stress is bad (red), less is good (green) —
        # the opposite of Streamlit's default "positive=green" coloring.
        help=(
            "Average of daily overall_avg stress over the last 7 days vs. the "
            "7 days before that. No comparison shown if last week has no stored data."
        ),
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

    render_week_over_week_stat(db_path)

    daily_summary = storage.load_daily_summary_range(db_path, start_date, end_date)
    if daily_summary.empty:
        st.info("No stored results in this range yet. Run some daily analyses first (Daily Detail tab, or the automated job).")
        return

    fig = stress_core.build_trend_chart(daily_summary)
    st.plotly_chart(fig, width="stretch", theme=None)

    st.subheader("Average stress by meeting, across this range")
    rollup = storage.load_event_rollup(db_path, start_date, end_date)
    if rollup.empty:
        st.info("No meeting data in this range.")
    else:
        st.dataframe(
            rollup.style.format({"avg_stress": "{:.1f}", "peak_stress": "{:.0f}", "total_minutes": "{:.0f}", "occurrences": "{:.0f}"}),
            width="stretch",
        )

    st.subheader("Meeting size vs. stress")
    st.caption("Does attendee count line up with higher stress — are big group calls worse than small ones?")
    size_correlation = storage.load_meeting_size_correlation(db_path, start_date, end_date)
    if size_correlation.empty:
        st.info("No meetings with stored attendee data in this range.")
    else:
        fig = stress_core.build_meeting_size_scatter_chart(size_correlation)
        st.plotly_chart(fig, width="stretch", theme=None)


def render_leaderboard_tab(db_path: str) -> None:
    """The primary view: who stresses you out, ranked. Everything else in
    this dashboard (daily detail, trends, recovery) is supporting detail —
    this tab is the headline."""
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From", value=date.today() - timedelta(days=89), key="leaderboard_start")
    with col2:
        end_date = st.date_input("To", value=date.today(), key="leaderboard_end")

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

    rank_by = st.radio(
        "Rank by",
        ["Average stress", "Total stress exposure", "Stress increase when the meeting starts (Δ)"],
        horizontal=True,
        help=(
            "Average stress: their meetings' overall stress level, which can reflect a generally "
            "stressful day, not just them. Total stress exposure: cumulative stress × time spent "
            "with them — rewards someone who stresses you a little but constantly, not just one bad "
            "meeting. Δ (delta): how much stress actually rose going into their meetings vs. right "
            "before — a more causal signal, but needs enough clean before/after data."
        ),
    )
    if rank_by == "Average stress":
        ranked = filtered.sort_values("avg_stress", ascending=False)
    elif rank_by == "Total stress exposure":
        ranked = filtered.sort_values("total_stress_exposure", ascending=False)
    else:
        ranked = filtered.dropna(subset=["avg_delta"]).sort_values("avg_delta", ascending=False)

    if ranked.empty:
        st.info("No one has a computable Δ in this range yet (needs clean free time right before a meeting).")
        return

    top = ranked.iloc[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🏆 Top stressor", top.name)
    m2.metric("Avg stress in their meetings", f"{top['avg_stress']:.0f}")
    m3.metric("Meetings together", f"{int(top['meetings'])}")
    m4.metric("Total stress exposure", f"{top['total_stress_exposure']:,.0f}")

    st.subheader("Leaderboard")
    display = ranked.head(20).reset_index().rename(columns={"attendee_name": "Attendee"})
    display.insert(0, "Rank", range(1, len(display) + 1))
    display = display[
        ["Rank", "Attendee", "meetings", "avg_stress", "peak_stress", "total_stress_exposure", "avg_delta"]
    ].set_index("Rank")
    display.columns = ["Attendee", "Meetings", "Avg stress", "Peak stress", "Total exposure", "Δ stress (during vs. before)"]
    st.dataframe(
        display.style.format(
            {
                "Meetings": "{:.0f}",
                "Avg stress": "{:.1f}",
                "Peak stress": "{:.0f}",
                "Total exposure": "{:,.0f}",
                "Δ stress (during vs. before)": "{:+.1f}",
            },
            na_rep="–",
        )
        .background_gradient(subset=["Avg stress"], cmap="Blues", vmin=0, vmax=100)
        .background_gradient(subset=["Total exposure"], cmap="Blues")
        .bar(subset=["Δ stress (during vs. before)"], align=0, color=["#e34948", "#2a78d6"], vmin=-30, vmax=30),
        width="stretch",
    )
    st.caption(
        "Total exposure = avg stress × minutes, summed across every meeting with them — not "
        "capped at 100 like the other columns, since it's a cumulative total, not a level."
    )

    with st.expander("Chart view (average stress)"):
        fig = stress_core.build_person_rollup_chart(filtered.sort_values("avg_stress", ascending=False).head(20))
        st.pyplot(fig, width="stretch")

    st.caption(
        "An event's average/peak stress applies to everyone who attended it — this shows who you're "
        "in stressful meetings WITH, not who specifically causes the stress within a group call."
    )


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
            st.plotly_chart(fig, width="stretch", theme=None)

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
            st.plotly_chart(fig, width="stretch", theme=None)

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
    title_col, settings_col = st.columns([6, 1])
    with title_col:
        st.title("Who's Stressing You Out")
    with settings_col:
        config = render_settings_popover()

    db_path = stress_core.DEFAULT_DB_PATH
    storage.init_db(db_path)

    # Leaderboard first — it's the headline. Recovery is the other "why"
    # dimension. Daily Detail/Trends are granular supporting detail, pushed
    # last per the user's explicit priority: "the leaderboard... the other
    # data is more secondary."
    tab_leaderboard, tab_recovery, tab_daily, tab_trends = st.tabs(
        ["🏆 Leaderboard", "Recovery", "Daily Detail", "Trends"]
    )
    with tab_leaderboard:
        render_leaderboard_tab(db_path)
    with tab_recovery:
        render_recovery_tab(db_path)
    with tab_daily:
        render_daily_tab(config, db_path)
    with tab_trends:
        render_trends_tab(db_path)


if __name__ == "__main__":
    main()
