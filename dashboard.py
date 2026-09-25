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

import pandas as pd
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


def _consume_person_selection(event, display: "pd.DataFrame", session_key: str) -> tuple[str, str] | None:
    """Turn an st.dataframe(on_select="rerun", selection_mode="single-row")
    return value into a (attendee_email, attendee_name) pair, but ONLY the
    run a row is actually newly clicked — not every run the selection
    happens to still be active.

    Why this matters: the Leaderboard and the "Most Improved" panel below it
    are two separate selectable tables, each with its own widget key/state
    that persists across reruns until the user clicks elsewhere in THAT
    table. Without tracking whether a given table's selection changed THIS
    run, whichever table's render call happens to execute later in the
    script would unconditionally re-assert its (possibly stale) selection
    every rerun — silently overriding a fresh click the user just made in
    the other table. Comparing against the previous run's selected row
    positions (stored under f"{session_key}_sig") makes "last click wins"
    actually true regardless of which table's code runs first or second."""
    rows = list(event.selection["rows"]) if event is not None and event.selection else []
    sig = tuple(rows)
    prev_sig = st.session_state.get(f"{session_key}_sig")
    st.session_state[f"{session_key}_sig"] = sig
    if rows and sig != prev_sig:
        row = display.iloc[rows[0]]
        return row["_attendee_email"], row["Attendee"]
    return None


def render_most_improved_panel(db_path: str, start_date: date, end_date: date, min_meetings: int) -> None:
    """The positive-reinforcement flip side of the ranked "who stresses you
    out" table above: who's least stressful to work with, and — more
    interesting than a flat low average, which could just mean you haven't
    met with them much — whose trend is actually improving over time (see
    storage.load_person_trend: minutes-weighted avg_stress in the first vs.
    second half of the selected date range). Kept as a small, clearly
    separate panel so the positive framing doesn't get lost inside the
    negative-framed leaderboard."""
    st.subheader("🕊️ Most Improved / Least Stressful")
    trend_df = storage.load_person_trend(db_path, start_date, end_date)
    if trend_df.empty:
        st.caption("No attendee data in this range yet.")
        return

    filtered = trend_df[trend_df["meetings"] >= min_meetings]
    if filtered.empty:
        st.caption("No one meets the minimum-meetings threshold above.")
        return

    # People with a computable trend rank first (most-improving/most-negative
    # trend on top) — that's the more interesting signal this panel exists
    # for. People without enough history for a trend (see load_person_trend's
    # min-2-meetings-per-half requirement) still show up, ranked by plain
    # avg_stress, rather than being silently dropped.
    have_trend = filtered.dropna(subset=["trend"]).sort_values("trend")
    no_trend = filtered[filtered["trend"].isna()].sort_values("avg_stress")
    ranked = pd.concat([have_trend, no_trend]).head(10)

    display = ranked.reset_index().rename(columns={"attendee_name": "Attendee"})
    display.insert(0, "Rank", range(1, len(display) + 1))
    display = display[["Rank", "Attendee", "meetings", "avg_stress", "trend", "attendee_email"]].set_index("Rank")
    display.columns = ["Attendee", "Meetings", "Avg stress", "Trend (2nd half vs. 1st half)", "_attendee_email"]
    visible_columns = ["Attendee", "Meetings", "Avg stress", "Trend (2nd half vs. 1st half)"]

    event = st.dataframe(
        display.style.format(
            {"Meetings": "{:.0f}", "Avg stress": "{:.1f}", "Trend (2nd half vs. 1st half)": "{:+.1f}"},
            na_rep="–",
        )
        # Reversed (low = most saturated) since low avg_stress is the "good"
        # end here — the opposite intent from the main leaderboard's Blues
        # gradient, which highlights HIGH avg_stress.
        .background_gradient(subset=["Avg stress"], cmap="Greens_r", vmin=0, vmax=100)
        # first color = negative values (improving = good = blue), second =
        # positive (worsening = bad = red) — confirmed via Styler.bar's
        # actual rendered output, not assumed from the API docs alone.
        .bar(subset=["Trend (2nd half vs. 1st half)"], align=0, color=["#2a78d6", "#e34948"], vmin=-30, vmax=30),
        column_order=visible_columns,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="most_improved_table",
    )
    st.caption(
        "Trend = avg stress in the second half of the date range minus the first half — negative "
        "means getting less stressful over time. Needs at least 2 meetings in each half to compute; "
        "shown as “–” otherwise, not guessed from too little data. Click a row to see their full history."
    )

    selection = _consume_person_selection(event, display, "most_improved_table")
    if selection:
        st.session_state["selected_person_email"], st.session_state["selected_person_name"] = selection


def render_person_drilldown(db_path: str, start_date: date, end_date: date) -> None:
    """Dedicated drill-down view for one person, shown below both leaderboard
    tables once a row is clicked in either one — chosen over a permanent
    always-visible area so the default (nobody selected) state stays clean
    and doesn't compete with the two ranked tables for attention."""
    email = st.session_state.get("selected_person_email")
    name = st.session_state.get("selected_person_name")
    if not email:
        st.caption("Click a name in either table above to see their full stress history over time.")
        return

    st.divider()
    header_col, clear_col = st.columns([5, 1])
    with header_col:
        st.subheader(f"📈 {name}'s stress history")
    with clear_col:
        st.write("")
        if st.button("✕ Clear", help="Close this drill-down view"):
            st.session_state["selected_person_email"] = None
            st.session_state["selected_person_name"] = None
            st.rerun()

    history = storage.load_person_history(db_path, email, start_date, end_date)
    if history.empty:
        st.info(f"No meeting history for {name} in this date range.")
        return

    valid = history.dropna(subset=["avg_stress"])
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Meetings", f"{len(history)}")
    if not valid.empty:
        h2.metric("Avg stress", f"{valid['avg_stress'].mean():.0f}")
        h3.metric("Peak stress", f"{valid['peak_stress'].max():.0f}")
    deltas = history["delta_stress"].dropna()
    if not deltas.empty:
        h4.metric("Avg Δ (during vs. before)", f"{deltas.mean():+.1f}")

    fig = stress_core.build_person_history_chart(history, name)
    st.plotly_chart(fig, width="stretch", theme=None)
    st.caption(
        "One point per meeting occurrence with them, in chronological order — a recurring meeting "
        "on different days shows up as separate points, not flattened into a single average."
    )


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
        [
            "Rank",
            "Attendee",
            "meetings",
            "avg_stress",
            "peak_stress",
            "total_stress_exposure",
            "avg_delta",
            "attendee_email",
        ]
    ].set_index("Rank")
    display.columns = [
        "Attendee",
        "Meetings",
        "Avg stress",
        "Peak stress",
        "Total exposure",
        "Δ stress (during vs. before)",
        "_attendee_email",
    ]
    leaderboard_visible_columns = [
        "Attendee",
        "Meetings",
        "Avg stress",
        "Peak stress",
        "Total exposure",
        "Δ stress (during vs. before)",
    ]
    leaderboard_event = st.dataframe(
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
        column_order=leaderboard_visible_columns,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="leaderboard_table",
    )
    st.caption(
        "Total exposure = avg stress × minutes, summed across every meeting with them — not "
        "capped at 100 like the other columns, since it's a cumulative total, not a level. "
        "Click a row to see that person's full history below."
    )

    selection = _consume_person_selection(leaderboard_event, display, "leaderboard_table")
    if selection:
        st.session_state["selected_person_email"], st.session_state["selected_person_name"] = selection

    with st.expander("Chart view (average stress)"):
        fig = stress_core.build_person_rollup_chart(filtered.sort_values("avg_stress", ascending=False).head(20))
        st.pyplot(fig, width="stretch")

    st.caption(
        "An event's average/peak stress applies to everyone who attended it — this shows who you're "
        "in stressful meetings WITH, not who specifically causes the stress within a group call."
    )

    st.divider()
    render_most_improved_panel(db_path, start_date, end_date, min_meetings)

    render_person_drilldown(db_path, start_date, end_date)


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
