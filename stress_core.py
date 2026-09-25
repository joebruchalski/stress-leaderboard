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
import recurring_ical_events
from icalendar import Calendar
from tzlocal import get_localzone

WORKDAY_START = time(9, 0)
WORKDAY_END = time(17, 0)
INVALID_STRESS_VALUES = {-1, -2}

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


def run_analysis(config: dict, target_date: date) -> tuple[pd.DataFrame, list[dict]]:
    """End-to-end: log in, fetch stress, parse calendar, correlate. Returns
    the minute-level grid (timestamp_local, stress, event) and the raw event
    list (for chart shading)."""
    local_tz = get_localzone()
    api = garmin_login(config["email"], config["password"], config["tokenstore"])
    stress_df = fetch_stress_minutes(api, target_date, local_tz)
    events = load_calendar_events(
        config["ics_path"], target_date, local_tz, config.get("calendar_email", ""), config.get("internal_domain", "")
    )

    grid = build_minute_grid(target_date, local_tz)
    grid = attach_stress(grid, stress_df)
    grid = attach_events(grid, events)
    return grid, events


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


def build_trend_chart(daily_summary: pd.DataFrame) -> plt.Figure:
    """Line chart of average/peak workday stress across stored history.
    daily_summary must have columns: date (str, ISO), overall_avg, overall_peak."""
    fig, ax = plt.subplots(figsize=(12, 5))
    dates_parsed = pd.to_datetime(daily_summary["date"])

    ax.plot(dates_parsed, daily_summary["overall_avg"], color=STRESS_LINE_COLOR, linewidth=2, marker="o", markersize=4, label="Daily average")
    ax.plot(dates_parsed, daily_summary["overall_peak"], color=EVENT_COLORS[1], linewidth=1.5, linestyle="--", marker="o", markersize=3, label="Daily peak")

    ax.set_ylim(0, 100)
    ax.set_ylabel("Stress level (0-100)")
    ax.set_xlabel("Date")
    ax.set_title("Workday Stress Over Time")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %-d", tz="UTC"))
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8, zorder=0)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8, frameon=False)
    fig.tight_layout()
    return fig


def build_weekday_chart(by_weekday: pd.DataFrame) -> plt.Figure:
    """Vertical bar chart of average stress by day of week. This ranks ONE
    measure (avg_stress) across ordered categories (Monday..Sunday) rather
    than by identity, so — like build_person_rollup_chart — it uses a single
    sequential hue instead of the categorical event palette. Unlike the
    person rollup, the categories have a natural order (the calendar week),
    so bars are NOT re-sorted by magnitude. `by_weekday` must be indexed by
    weekday name in Monday..Sunday order with an avg_stress column (see
    storage.load_stress_by_weekday)."""
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(by_weekday.index, by_weekday["avg_stress"], color=STRESS_LINE_COLOR, width=0.6)

    ax.set_ylim(0, 100)
    ax.set_ylabel("Average stress level (0-100)")
    ax.set_xlabel("Day of week")
    ax.set_title("Average Stress by Day of Week")
    ax.set_axisbelow(True)  # zorder=0 on grid() alone doesn't reliably sit behind bar patches
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    return fig


def build_hourly_chart(by_hour: pd.DataFrame) -> plt.Figure:
    """Vertical bar chart of average stress by hour of day. Same magnitude-
    across-ordered-categories job as build_weekday_chart: single sequential
    hue, natural (chronological) ordering, no re-sort by magnitude.
    `by_hour` must be indexed by hour-of-day (int), sorted ascending, with
    an avg_stress column (see storage.load_stress_by_hour)."""
    fig, ax = plt.subplots(figsize=(10, 5))
    labels = [f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}" for h in by_hour.index]
    ax.bar(labels, by_hour["avg_stress"], color=STRESS_LINE_COLOR, width=0.6)

    ax.set_ylim(0, 100)
    ax.set_ylabel("Average stress level (0-100)")
    ax.set_xlabel("Hour of day")
    ax.set_title("Average Stress by Time of Day")
    ax.set_axisbelow(True)  # zorder=0 on grid() alone doesn't reliably sit behind bar patches
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
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
