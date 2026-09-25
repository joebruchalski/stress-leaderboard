"""Shared logic for the Garmin stress / calendar correlation tool.

Used by both the CLI (stress_analyzer.py, meant for unattended/launchd runs)
and the Streamlit dashboard (dashboard.py). Contains no terminal-blocking I/O
(no input()/getpass()) so it is safe to call from a non-interactive context.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, time
from pathlib import Path

import garminconnect
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import plotly.graph_objects as go
import recurring_ical_events
from icalendar import Calendar
from tzlocal import get_localzone

# Shared Plotly layout bits so the interactive charts (Trends, Recovery) read
# as one system with the still-matplotlib charts (Leaderboard, Daily Detail)
# elsewhere in the app: white surface, the same recessive gridline color, no
# top/right border.
GRIDLINE_COLOR = "#e1e0d9"
_PLOTLY_AXIS_COMMON = dict(
    zeroline=False,
    showline=True,
    linecolor=GRIDLINE_COLOR,
    ticks="outside",
    tickcolor=GRIDLINE_COLOR,
)


def _style_plotly_figure(fig: go.Figure, *, y_gridlines: bool = True, show_legend: bool = True) -> go.Figure:
    """Apply the shared white-surface / recessive-grid look to a Plotly figure
    in place, and return it (for chaining)."""
    fig.update_layout(
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(color="#31302a"),
        margin=dict(l=60, r=20, t=50, b=60),
        showlegend=show_legend,
        legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="left", x=0, bgcolor="rgba(0,0,0,0)"),
    )
    fig.update_xaxes(showgrid=False, **_PLOTLY_AXIS_COMMON)
    fig.update_yaxes(
        **_PLOTLY_AXIS_COMMON,
        showgrid=y_gridlines,
        gridcolor=GRIDLINE_COLOR,
        gridwidth=0.8,
    )
    return fig

WORKDAY_START = time(9, 0)
WORKDAY_END = time(17, 0)
INVALID_STRESS_VALUES = {-1, -2}
BASELINE_MINUTES = 15  # "before" window for compute_meeting_deltas()

CONFIG_DIR = Path.home() / ".config" / "stress_analyzer"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_TOKENSTORE = "~/.garminconnect"
DEFAULT_DB_PATH = str(CONFIG_DIR / "history.db")
KEYCHAIN_SERVICE = "stress-analyzer-garmin"

# Categorical palette (validated colorblind-safe adjacency), used in fixed order.
EVENT_COLORS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
OVERFLOW_COLOR = "#898781"  # muted gray for the 9th+ distinct event
STRESS_LINE_COLOR = "#2a78d6"


class ConfigError(Exception):
    """Raised when required configuration is missing and cannot be prompted for."""


# --------------------------------------------------------------------------
# Configuration: env vars > saved config file / Keychain > caller decides
# --------------------------------------------------------------------------

def load_saved_config() -> dict:
    if CONFIG_FILE.is_file():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(
    email: str, ics_path: str, tokenstore: str, calendar_email: str = "", internal_domain: str = ""
) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(
            {
                "email": email,
                "ics_path": ics_path,
                "tokenstore": tokenstore,
                "calendar_email": calendar_email,
                "internal_domain": internal_domain,
            },
            indent=2,
        )
    )


def guess_calendar_email(ics_path: str) -> str:
    """Calendar exports are often named after the account's own address
    (e.g. joe@example.com.ics) — use that as a default guess for which
    attendee entry is 'me' and should be excluded from per-person stress."""
    if not ics_path:
        return ""
    stem = Path(ics_path).stem
    return stem if "@" in stem else ""


def guess_internal_domain(calendar_email: str) -> str:
    """Default the 'internal only' attendee filter to the calendar owner's
    own email domain — the most common case for a company calendar export."""
    if calendar_email and "@" in calendar_email:
        return calendar_email.split("@", 1)[1]
    return ""


def get_keychain_password(email: str) -> str | None:
    """Look up the Garmin password from the macOS Keychain, if present."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def save_keychain_password(email: str, password: str) -> None:
    """Save/update the Garmin password in the macOS Keychain."""
    subprocess.run(
        ["security", "add-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE, "-w", password, "-U"],
        capture_output=True,
        text=True,
        check=True,
    )


def resolve_config() -> dict:
    """Resolve whatever config is available from env vars, the saved config
    file, and the Keychain. Missing fields come back as None/empty for the
    caller (CLI prompts, or the dashboard shows a settings form) to fill in."""
    saved = load_saved_config()
    email = os.environ.get("GARMIN_EMAIL") or saved.get("email") or ""
    ics_path = os.environ.get("ICS_FILE_PATH") or saved.get("ics_path") or ""
    tokenstore = os.environ.get("GARMIN_TOKENSTORE") or saved.get("tokenstore") or DEFAULT_TOKENSTORE

    password = os.environ.get("GARMIN_PASSWORD") or ""
    if not password and email:
        password = get_keychain_password(email) or ""

    calendar_email = (
        os.environ.get("CALENDAR_EMAIL") or saved.get("calendar_email") or guess_calendar_email(ics_path)
    )
    internal_domain = (
        os.environ.get("INTERNAL_DOMAIN") or saved.get("internal_domain") or guess_internal_domain(calendar_email)
    )

    return {
        "email": email,
        "password": password,
        "ics_path": ics_path,
        "tokenstore": tokenstore,
        "calendar_email": calendar_email,
        "internal_domain": internal_domain,
    }


# --------------------------------------------------------------------------
# Garmin
# --------------------------------------------------------------------------

def garmin_login(email: str, password: str, tokenstore: str) -> garminconnect.Garmin:
    """Log in to Garmin Connect. Garmin.login(tokenstore) handles reading a
    cached session from tokenstore first and falling back to a fresh
    email/password login, persisting new tokens back to tokenstore itself —
    no manual token handling needed here."""
    if not email or not password:
        raise ConfigError("Garmin email and password are required.")

    tokenstore_path = str(Path(tokenstore).expanduser())
    api = garminconnect.Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Enter Garmin MFA code: ").strip(),
    )
    try:
        api.login(tokenstore_path)
    except garminconnect.GarminConnectTooManyRequestsError as exc:
        raise ConfigError(f"Garmin is rate-limiting login attempts: {exc}") from exc
    except garminconnect.GarminConnectAuthenticationError as exc:
        raise ConfigError(f"Garmin login failed: {exc}") from exc
    return api


def fetch_stress_minutes(api: garminconnect.Garmin, target_date: date, local_tz) -> pd.DataFrame:
    """Return a DataFrame of (timestamp_local, stress) built from Garmin's
    stressValuesArray for target_date."""
    raw = api.get_stress_data(target_date.isoformat())
    values = (raw or {}).get("stressValuesArray") or []
    if not values:
        raise ValueError(f"No stress data returned by Garmin for {target_date.isoformat()}.")

    df = pd.DataFrame(values, columns=["timestamp_ms", "stress"])
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["timestamp_local"] = df["timestamp_utc"].dt.tz_convert(local_tz)
    df["stress"] = df["stress"].where(~df["stress"].isin(INVALID_STRESS_VALUES))
    return df[["timestamp_local", "stress"]]


def fetch_sleep_summary(api: garminconnect.Garmin, target_date: date) -> dict | None:
    """Return {"sleep_score", "total_sleep_minutes"} for target_date, or None
    if Garmin has no sleep record for that day (or the call fails).

    This is supplementary data, not the core stress fetch — unlike
    fetch_stress_minutes(), a missing/empty/erroring result must never raise
    and take down the rest of run_analysis().

    Real response shape (inspected via a live call, not guessed): a dict with
    top-level key "dailySleepDTO" (None/absent on a day with no synced sleep),
    which itself has "sleepTimeSeconds" (int) and a nested
    "sleepScores" -> "overall" -> "value" (int, 0-100, Garmin's sleep score).
    """
    try:
        raw = api.get_sleep_data(target_date.isoformat())
    except Exception:
        return None

    dto = (raw or {}).get("dailySleepDTO") or {}
    if not dto:
        return None

    sleep_time_seconds = dto.get("sleepTimeSeconds")
    total_sleep_minutes = (
        sleep_time_seconds / 60 if isinstance(sleep_time_seconds, (int, float)) else None
    )
    overall = (dto.get("sleepScores") or {}).get("overall") or {}
    sleep_score = overall.get("value")
    sleep_score = float(sleep_score) if isinstance(sleep_score, (int, float)) else None

    if sleep_score is None and total_sleep_minutes is None:
        return None
    return {"sleep_score": sleep_score, "total_sleep_minutes": total_sleep_minutes}


def fetch_body_battery_summary(api: garminconnect.Garmin, target_date: date) -> dict | None:
    """Return {"body_battery_low", "body_battery_high"} for target_date, or
    None if Garmin has no Body Battery record for that day (or the call
    fails). Supplementary data — same never-raise contract as
    fetch_sleep_summary().

    Real response shape (inspected via a live call, not guessed):
    get_body_battery(startdate, enddate) returns a LIST of per-day dicts
    (one per date in the range — here a single-day range still returns a
    list), each with "bodyBatteryValuesArray": a list of
    [timestamp_ms, bodyBatteryLevel] pairs. Low/high are derived as the
    min/max of those levels across the day.
    """
    try:
        raw = api.get_body_battery(target_date.isoformat(), target_date.isoformat())
    except Exception:
        return None

    if not raw or not isinstance(raw, list):
        return None
    day = raw[0] or {}
    values = day.get("bodyBatteryValuesArray") or []
    levels = [
        v[1]
        for v in values
        if isinstance(v, (list, tuple)) and len(v) >= 2 and isinstance(v[1], (int, float))
    ]
    if not levels:
        return None
    return {"body_battery_low": float(min(levels)), "body_battery_high": float(max(levels))}


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def _display_name_from_cn_or_email(cn: str | None, email: str) -> str:
    """Real calendar exports are inconsistent: CN is sometimes a proper
    display name, sometimes just the email address again. Prefer CN when
    it's actually a name; otherwise derive something readable from the
    email's local part."""
    if cn and cn.strip().lower() != email.strip().lower():
        return cn.strip()
    local_part = email.split("@")[0]
    return local_part.replace(".", " ").replace("_", " ").title()


def extract_attendees(component, self_email: str, internal_domain: str = "") -> list[dict]:
    """Extract individual (non-room/non-resource) attendees from a VEVENT,
    excluding the calendar owner and, if internal_domain is set, anyone
    outside that email domain (e.g. customers/vendors on the invite).
    Returns deduped [{"email", "name"}, ...]."""
    raw = component.get("ATTENDEE")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]

    self_email_lower = (self_email or "").strip().lower()
    domain_suffix = "@" + internal_domain.strip().lstrip("@").lower() if internal_domain else ""
    seen: set[str] = set()
    attendees = []
    for item in items:
        cutype = str(item.params.get("CUTYPE", "INDIVIDUAL")).upper()
        if cutype != "INDIVIDUAL":
            continue  # skip conference rooms / resources / groups
        email = str(item).replace("mailto:", "", 1).strip().lower()
        if not email or email == self_email_lower or email in seen:
            continue
        if domain_suffix and not email.endswith(domain_suffix):
            continue  # external attendee (customer/vendor) — not who we're tracking
        seen.add(email)
        name = _display_name_from_cn_or_email(item.params.get("CN"), email)
        attendees.append({"email": email, "name": name})
    return attendees


def load_calendar_events(
    ics_path: Path, target_date: date, local_tz, self_email: str = "", internal_domain: str = ""
) -> list[dict]:
    """Parse the .ics file and return timed events (title, start, end,
    attendees) on target_date, in local time, with recurring events
    expanded."""
    calendar = Calendar.from_ical(Path(ics_path).read_bytes())

    window_start = datetime.combine(target_date, time.min, tzinfo=local_tz)
    window_end = datetime.combine(target_date, time.max, tzinfo=local_tz)
    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)

    events = []
    for component in occurrences:
        start = component["DTSTART"].dt
        if not isinstance(start, datetime):
            continue  # all-day event, no clock time to correlate against

        dtend = component.get("DTEND")
        end = dtend.dt if dtend is not None else start

        start = start.astimezone(local_tz)
        end = end.astimezone(local_tz)
        title = str(component.get("SUMMARY", "Untitled event"))
        attendees = extract_attendees(component, self_email, internal_domain)
        events.append({"title": title, "start": start, "end": end, "attendees": attendees})

    events.sort(key=lambda e: e["start"])
    return events


# --------------------------------------------------------------------------
# Correlation
# --------------------------------------------------------------------------

def build_minute_grid(target_date: date, local_tz) -> pd.DataFrame:
    start = datetime.combine(target_date, WORKDAY_START, tzinfo=local_tz)
    end = datetime.combine(target_date, WORKDAY_END, tzinfo=local_tz)
    minutes = pd.date_range(start, end, freq="1min")
    return pd.DataFrame({"timestamp_local": minutes})


def attach_stress(grid: pd.DataFrame, stress_df: pd.DataFrame) -> pd.DataFrame:
    """Attach the nearest Garmin stress reading to each minute (Garmin samples
    roughly every 2-3 minutes, not every single minute).

    grid["timestamp_local"] comes from pd.date_range() over Python datetimes,
    which pandas resolves to datetime64[us, tz]. stress_df["timestamp_local"]
    comes from pd.to_datetime(..., unit="ms") in fetch_stress_minutes(),
    which pandas resolves to datetime64[ms, tz] — same timezone, different
    resolution. merge_asof requires the merge keys to share both tz AND
    resolution and raises MergeError otherwise, so normalize explicitly
    before merging rather than relying on both sides happening to match."""
    grid = grid.copy()
    stress_df = stress_df.copy()
    grid["timestamp_local"] = grid["timestamp_local"].dt.as_unit("us")
    stress_df["timestamp_local"] = stress_df["timestamp_local"].dt.as_unit("us")
    return pd.merge_asof(
        grid.sort_values("timestamp_local"),
        stress_df.sort_values("timestamp_local"),
        on="timestamp_local",
        direction="nearest",
        tolerance=pd.Timedelta("3min"),
    )


def attach_events(grid: pd.DataFrame, events: list[dict]) -> pd.DataFrame:
    """Label each minute with the event that claims it. Overlaps are resolved
    first-started-wins: events are applied in start-time order and never
    overwrite a minute another event already claimed."""
    grid = grid.copy()
    grid["event"] = pd.NA
    for event in events:  # already sorted by start ascending
        mask = (
            (grid["timestamp_local"] >= event["start"])
            & (grid["timestamp_local"] < event["end"])
            & grid["event"].isna()
        )
        grid.loc[mask, "event"] = event["title"]
    grid["event"] = grid["event"].fillna("No Meeting")
    return grid


def compute_meeting_deltas(
    grid: pd.DataFrame, events: list[dict], baseline_minutes: int = BASELINE_MINUTES
) -> pd.DataFrame:
    """Before-vs-during stress delta per event — a more causal signal than a
    flat during-meeting average: did stress actually rise when the meeting
    started, relative to genuinely free time right before it?

    For each event occurrence, the baseline is the `baseline_minutes`
    immediately before its start. That baseline only counts if every one of
    those minutes (a) falls inside the minute grid (e.g. not before 9:00 AM)
    and (b) is labeled "No Meeting" in the grid — i.e. real free time, not
    spillover from a back-to-back prior meeting. If the baseline window
    isn't fully available, or either the baseline or during-meeting window
    ends up with no valid stress readings, that occurrence's delta is NaN
    rather than a number fabricated from insufficient data.

        delta = during-meeting avg valid stress - baseline avg valid stress

    (same "average of valid readings" definition summarize_by_event() uses).
    The during-meeting window is additionally restricted to minutes the grid
    actually attributes to this event's title (attach_events() resolves
    overlaps first-started-wins, so a later overlapping event may not own
    every minute inside its own start/end span).

    A title with multiple occurrences in `events` (e.g. a recurring meeting
    that happens twice the same day) collapses to one row: the mean of its
    occurrences' deltas, NaN occurrences excluded (all-NaN occurrences ->
    NaN for the title, not a dropped row — so callers can left-join this
    onto summarize_by_event()'s output without losing rows).

    Pure function, no Garmin/DB dependency. Returns a DataFrame indexed by
    event title ("event") with a single delta_stress column.
    """
    if grid.empty or not events:
        return pd.DataFrame({"delta_stress": pd.Series(dtype="float64")}).rename_axis("event")

    grid = grid.sort_values("timestamp_local").reset_index(drop=True)
    grid_start = grid["timestamp_local"].iloc[0]

    occurrence_deltas: dict[str, list[float]] = {}
    for event in events:
        title = event["title"]
        start = pd.Timestamp(event["start"])
        end = pd.Timestamp(event["end"])
        baseline_start = start - pd.Timedelta(minutes=baseline_minutes)

        delta = float("nan")
        if baseline_start >= grid_start:
            baseline_mask = (grid["timestamp_local"] >= baseline_start) & (grid["timestamp_local"] < start)
            baseline_rows = grid.loc[baseline_mask]
            baseline_is_free = (
                len(baseline_rows) >= baseline_minutes and (baseline_rows["event"] == "No Meeting").all()
            )
            if baseline_is_free:
                baseline_valid = baseline_rows.dropna(subset=["stress"])
                during_mask = (
                    (grid["timestamp_local"] >= start)
                    & (grid["timestamp_local"] < end)
                    & (grid["event"] == title)
                )
                during_valid = grid.loc[during_mask].dropna(subset=["stress"])
                if not baseline_valid.empty and not during_valid.empty:
                    delta = during_valid["stress"].mean() - baseline_valid["stress"].mean()

        occurrence_deltas.setdefault(title, []).append(delta)

    rows = []
    for title, deltas in occurrence_deltas.items():
        valid = [d for d in deltas if not pd.isna(d)]
        avg_delta = (sum(valid) / len(valid)) if valid else float("nan")
        rows.append({"event": title, "delta_stress": avg_delta})

    return pd.DataFrame(rows).set_index("event")


def run_analysis(config: dict, target_date: date) -> tuple[pd.DataFrame, list[dict], dict]:
    """End-to-end: log in, fetch stress, parse calendar, correlate. Returns
    the minute-level grid (timestamp_local, stress, event), the raw event
    list (for chart shading), and a recovery dict {"sleep": ..., "body_battery": ...}
    (each value is a fetch_*_summary() dict or None — sleep/Body Battery are
    supplementary and must never fail the core stress/calendar analysis).

    Adds a third return value vs. the original (grid, events) signature —
    existing callers that only unpack two values need updating; this is a
    deliberate, visible break rather than a silent dict bolted onto one of
    the existing items."""
    local_tz = get_localzone()
    api = garmin_login(config["email"], config["password"], config["tokenstore"])
    stress_df = fetch_stress_minutes(api, target_date, local_tz)
    events = load_calendar_events(
        config["ics_path"], target_date, local_tz, config.get("calendar_email", ""), config.get("internal_domain", "")
    )

    grid = build_minute_grid(target_date, local_tz)
    grid = attach_stress(grid, stress_df)
    grid = attach_events(grid, events)

    recovery = {
        "sleep": fetch_sleep_summary(api, target_date),
        "body_battery": fetch_body_battery_summary(api, target_date),
    }
    return grid, events, recovery


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------

def summarize_by_event(grid: pd.DataFrame) -> pd.DataFrame:
    """Per-event avg/peak/minutes, sorted highest-average first. Index is the
    event title."""
    valid = grid.dropna(subset=["stress"])
    if valid.empty:
        return pd.DataFrame(columns=["avg_stress", "peak_stress", "minutes"])
    return (
        valid.groupby("event")["stress"]
        .agg(avg_stress="mean", peak_stress="max", minutes="count")
        .sort_values("avg_stress", ascending=False)
    )


def format_summary_text(grid: pd.DataFrame, target_date: date) -> str:
    valid = grid.dropna(subset=["stress"])
    lines = [f"Stress vs. Calendar — {target_date.isoformat()} (9:00 AM-5:00 PM)", "=" * 60]

    if valid.empty:
        lines.append("No valid stress readings in this window.")
        return "\n".join(lines)

    overall = valid["stress"]
    lines.append(f"Workday average stress: {overall.mean():.1f}")
    lines.append(f"Workday peak stress:    {overall.max():.0f}")
    lines.append("")

    summary = summarize_by_event(grid)
    lines.append(f"{'Event':<40} {'Avg':>6} {'Peak':>6} {'Mins':>6}")
    lines.append("-" * 60)
    for title, row in summary.iterrows():
        label = (title[:37] + "...") if len(title) > 40 else title
        lines.append(f"{label:<40} {row['avg_stress']:>6.1f} {row['peak_stress']:>6.0f} {int(row['minutes']):>6}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

def build_event_color_map(events: list[dict]) -> dict[str, str]:
    color_map: dict[str, str] = {}
    for event in events:
        if event["title"] not in color_map:
            idx = len(color_map)
            color_map[event["title"]] = EVENT_COLORS[idx] if idx < len(EVENT_COLORS) else OVERFLOW_COLOR
    return color_map


def build_daily_chart(grid: pd.DataFrame, events: list[dict], target_date: date) -> plt.Figure:
    """Build (but do not save) the stress-vs-events line chart for one day."""
    fig, ax = plt.subplots(figsize=(12, 6))
    color_map = build_event_color_map(events)

    plotted_labels = set()
    day_start, day_end = grid["timestamp_local"].iloc[0], grid["timestamp_local"].iloc[-1]
    for event in events:
        span_start = max(event["start"], day_start)
        span_end = min(event["end"], day_end)
        if span_start >= span_end:
            continue
        label = event["title"] if event["title"] not in plotted_labels else None
        ax.axvspan(span_start, span_end, color=color_map[event["title"]], alpha=0.28, label=label, linewidth=0)
        plotted_labels.add(event["title"])

    ax.plot(
        grid["timestamp_local"],
        grid["stress"],
        color=STRESS_LINE_COLOR,
        linewidth=2,
        solid_capstyle="round",
        label="Stress level",
    )

    ax.set_ylim(0, 100)
    ax.set_ylabel("Stress level (0-100)")
    ax.set_xlabel("Time")
    ax.set_title(f"Stress vs. Calendar Events — {target_date.isoformat()}")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I:%M %p", tz=grid["timestamp_local"].iloc[0].tzinfo))
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8, zorder=0)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8, frameon=False)
    fig.tight_layout()
    return fig


def build_daily_chart_interactive(grid: pd.DataFrame, events: list[dict], target_date: date) -> go.Figure:
    """Interactive (Plotly) counterpart to build_daily_chart(), used only by
    the dashboard's Daily Detail tab (st.plotly_chart). build_daily_chart()
    itself is untouched — stress_analyzer.py (the CLI) still calls it to
    save a static PNG, and that export path must keep working.

    hovermode="x unified" means the pointer only has to be near a time on
    the x-axis, not pixel-precise on the line, to see that minute's exact
    local time, stress value, and which meeting (or "No Meeting") owns it —
    directly answering "was I in a meeting when this spike happened?".
    Meeting shading is drawn with add_vrect() (the Plotly analog of
    axvspan()); since vrect shapes don't hover or appear in a legend on
    their own, per-meeting identity comes from two places: the "Meeting"
    line in the unified hover tooltip (sourced from grid["event"] via
    customdata on the stress trace) and a real always-visible legend built
    from invisible marker traces, one per meeting — per the dataviz skill's
    ">=2 series always gets a legend, not hover-only identification" rule.
    """
    color_map = build_event_color_map(events)
    day_start, day_end = grid["timestamp_local"].iloc[0], grid["timestamp_local"].iloc[-1]

    fig = go.Figure()

    plotted_labels: set[str] = set()
    for event in events:
        span_start = max(event["start"], day_start)
        span_end = min(event["end"], day_end)
        if span_start >= span_end:
            continue
        fig.add_vrect(
            x0=span_start,
            x1=span_end,
            fillcolor=color_map[event["title"]],
            opacity=0.28,
            line_width=0,
            layer="below",
        )
        if event["title"] not in plotted_labels:
            plotted_labels.add(event["title"])
            fig.add_trace(
                go.Scatter(
                    x=[None],
                    y=[None],
                    mode="markers",
                    marker=dict(size=10, symbol="square", color=color_map[event["title"]]),
                    name=event["title"],
                    hoverinfo="skip",
                    showlegend=True,
                )
            )

    fig.add_trace(
        go.Scatter(
            x=grid["timestamp_local"],
            y=grid["stress"],
            mode="lines",
            line=dict(color=STRESS_LINE_COLOR, width=2, shape="linear"),
            name="Stress level",
            customdata=grid["event"],
            hovertemplate="Stress: %{y:.0f}<br>Meeting: %{customdata}<extra></extra>",
            connectgaps=False,
        )
    )

    fig.update_layout(
        title=f"Stress vs. Calendar Events — {target_date.isoformat()}",
        hovermode="x unified",
        xaxis=dict(
            title="Time",
            type="date",  # explicit: the invisible x=[None] legend-marker
            # traces (added before the real datetime trace, so the legend
            # order matches build_daily_chart's) otherwise make Plotly's
            # axis-type autodetection fall back to linear/category and drop
            # the real timestamps entirely.
            tickformat="%I:%M %p",
            hoverformat="%I:%M %p",
            showgrid=False,
            showline=True,
            linecolor="#e1e0d9",
        ),
        yaxis=dict(
            title="Stress level (0-100)",
            range=[0, 100],
            gridcolor="#e1e0d9",
            zeroline=False,
            showline=True,
            linecolor="#e1e0d9",
        ),
        plot_bgcolor="white",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02, font=dict(size=11)),
        margin=dict(l=60, r=160, t=60, b=50),
        height=520,
    )
    return fig


def build_trend_chart(daily_summary: pd.DataFrame) -> go.Figure:
    """Interactive line chart of average/peak workday stress across stored
    history. daily_summary must have columns: date (str, ISO), overall_avg,
    overall_peak. Hovering either line shows the exact date and value;
    hovermode="x unified" ties both series together at whatever date the
    mouse is over, since they share the same date axis."""
    dates_parsed = pd.to_datetime(daily_summary["date"])

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=dates_parsed,
            y=daily_summary["overall_avg"],
            mode="lines+markers",
            name="Daily average",
            line=dict(color=STRESS_LINE_COLOR, width=2),
            marker=dict(size=6),
            hovertemplate="Daily average: %{y:.1f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates_parsed,
            y=daily_summary["overall_peak"],
            mode="lines+markers",
            name="Daily peak",
            line=dict(color=EVENT_COLORS[1], width=1.5, dash="dash"),
            marker=dict(size=5),
            hovertemplate="Daily peak: %{y:.1f}<extra></extra>",
        )
    )

    fig.update_layout(
        title="Workday Stress Over Time",
        hovermode="x unified",
    )
    # Plotly's date-axis autotick can otherwise land on sub-day steps (e.g.
    # every 12h) for short ranges, which prints the same day label twice in a
    # row. Snap the tick step to a whole number of days, sized to keep to
    # roughly 8 gridlines regardless of the selected range.
    span_days = max((dates_parsed.max() - dates_parsed.min()).days, 0)
    day_step = max(1, round(span_days / 8)) if span_days else 1
    fig.update_xaxes(title_text="Date", tickformat="%b %-d", dtick=day_step * 86_400_000)
    fig.update_yaxes(title_text="Stress level (0-100)", range=[0, 100])
    _style_plotly_figure(fig)
    return fig


def build_person_rollup_chart(rollup: pd.DataFrame) -> plt.Figure:
    """Horizontal ranked bar chart of average stress by meeting attendee.
    This ranks ONE measure across many categories (a magnitude job), so it
    uses a single sequential hue rather than the categorical event palette —
    color isn't carrying identity here, position/label already does.
    `rollup` must be indexed by display name with an avg_stress column,
    already sorted descending (see storage.load_person_rollup)."""
    fig, ax = plt.subplots(figsize=(10, max(3, 0.4 * len(rollup))))
    ordered = rollup.iloc[::-1]  # barh draws bottom-up; reverse so #1 lands on top
    ax.barh(ordered.index, ordered["avg_stress"], color=STRESS_LINE_COLOR, height=0.6)

    ax.set_xlim(0, 100)
    ax.set_xlabel("Average stress level (0-100)")
    ax.set_title("Average Stress by Meeting Attendee")
    ax.set_axisbelow(True)  # zorder=0 on grid() alone doesn't reliably sit behind bar patches
    ax.grid(axis="x", color="#e1e0d9", linewidth=0.8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    return fig


def build_recovery_scatter_chart(df: pd.DataFrame, x_col: str, x_label: str, title: str) -> go.Figure:
    """Interactive scatter, one dot per day: x = a recovery metric (sleep
    score or Body Battery), y = that day's average workday stress. Hovering a
    dot shows which day it is plus both values — otherwise there's no way to
    tell which day a given dot represents. Deliberately a single scatter with
    one axis, not a dual-axis (two y-scales) chart — dual-axis is the #1
    chart mistake for exactly this "does X predict Y" question, per the
    dataviz skill. `df` must have columns [x_col, "overall_avg", "date"],
    already dropna'd of rows missing either value (see
    load_recovery_correlation)."""
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df[x_col],
            y=df["overall_avg"],
            mode="markers",
            marker=dict(color=STRESS_LINE_COLOR, size=10, opacity=0.75, line=dict(width=0)),
            customdata=df["date"],
            hovertemplate=(
                "%{customdata}<br>"
                + x_label
                + ": %{x:.0f}<br>Stress: %{y:.1f}<extra></extra>"
            ),
            showlegend=False,
        )
    )

    fig.update_layout(title=title)
    fig.update_xaxes(title_text=x_label)
    fig.update_yaxes(title_text="Workday average stress (0-100)", range=[0, 100])
    _style_plotly_figure(fig, show_legend=False)
    return fig
