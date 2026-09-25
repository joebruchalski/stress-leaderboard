#!/usr/bin/env python3
"""CLI entry point: correlate Garmin stress data with calendar events for a
workday, print a summary, save a PNG chart, and persist results to SQLite so
the Streamlit dashboard (dashboard.py) can show trends.

Designed to also run unattended (e.g. via launchd): credentials are resolved
from env vars, a saved config file (~/.config/stress_analyzer/config.json),
and the macOS Keychain, in that order — falling back to interactive terminal
prompts only when something is still missing.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import stress_core
import storage
from stress_core import ConfigError


def prompt_for_missing(config: dict) -> dict:
    """Interactively fill in whatever resolve_config() couldn't find, then
    persist the non-secret parts (and offer to save the password to Keychain)
    so future runs — including automated ones — don't need to ask again."""
    changed = False

    if not config["email"]:
        config["email"] = input("Enter your Garmin Connect email: ").strip()
        changed = True

    if not config["ics_path"]:
        config["ics_path"] = input("Enter the path to your .ics calendar export file: ").strip()
        changed = True

    ics_path = Path(config["ics_path"]).expanduser()
    if not ics_path.is_file():
        sys.exit(f"Calendar file not found: {ics_path}")
    config["ics_path"] = str(ics_path)

    if not config["password"]:
        config["password"] = getpass.getpass("Enter your Garmin Connect password: ")
        try:
            stress_core.save_keychain_password(config["email"], config["password"])
            print("Saved password to macOS Keychain for future automated runs.")
        except Exception as exc:  # noqa: BLE001 - keychain save is best-effort
            print(f"Warning: could not save password to Keychain ({exc}). You'll be asked again next time.")

    if changed:
        stress_core.save_config(
            config["email"],
            config["ics_path"],
            config["tokenstore"],
            config["calendar_email"],
            config["internal_domain"],
            config.get("ics_url", ""),
        )

    return config


def run_setup_wizard() -> None:
    """Explicit one-time setup: prompts for everything and saves it, without
    running an analysis. Run this before installing the launchd job."""
    config = stress_core.resolve_config()
    config["email"] = input(f"Garmin Connect email [{config['email'] or 'none saved'}]: ").strip() or config["email"]
    if not config["email"]:
        sys.exit("Email is required.")

    ics_input = input(f"Path to .ics calendar file [{config['ics_path'] or 'none saved'}]: ").strip()
    if ics_input:
        config["ics_path"] = ics_input
    ics_path = Path(config["ics_path"]).expanduser()
    if not ics_path.is_file():
        sys.exit(f"Calendar file not found: {ics_path}")
    config["ics_path"] = str(ics_path)
    config["calendar_email"] = stress_core.guess_calendar_email(config["ics_path"])

    ics_url_input = input(
        "Live calendar URL, e.g. a Google/Outlook/iCloud private iCal address (optional — "
        f"leave blank to keep using the local .ics file above) [{config['ics_url'] or 'none saved'}]: "
    ).strip()
    if ics_url_input:
        config["ics_url"] = ics_url_input

    cal_email_input = input(
        f"Your own email as it appears in meeting invites, to exclude from per-person "
        f"stress analysis [{config['calendar_email'] or 'none guessed'}]: "
    ).strip()
    if cal_email_input:
        config["calendar_email"] = cal_email_input

    config["internal_domain"] = stress_core.guess_internal_domain(config["calendar_email"])
    domain_input = input(
        f"Your company's email domain, to exclude external attendees like customers "
        f"[{config['internal_domain'] or 'none guessed — leave blank to include everyone'}]: "
    ).strip()
    if domain_input:
        config["internal_domain"] = domain_input

    password = getpass.getpass("Garmin Connect password (stored in macOS Keychain, not on disk): ")
    stress_core.save_keychain_password(config["email"], password)
    stress_core.save_config(
        config["email"],
        config["ics_path"],
        config["tokenstore"],
        config["calendar_email"],
        config["internal_domain"],
        config.get("ics_url", ""),
    )
    print(f"Saved settings to {stress_core.CONFIG_FILE} and password to Keychain.")
    print("You can now run this script unattended (e.g. via launchd) with no prompts.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today(),
        help="Target date, YYYY-MM-DD (default: today)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path (default: stress_report_YYYY-MM-DD.png)")
    parser.add_argument("--db", type=str, default=stress_core.DEFAULT_DB_PATH, help="SQLite history database path")
    parser.add_argument("--setup", action="store_true", help="Run the one-time setup wizard and exit")
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Analyze the last N days (today back through N-1 days ago) in one run instead of a "
            "single --date — e.g. --backfill-days 180 for the past ~6 months, or 365 for a full "
            "year. Logs in and parses the calendar ONCE for the whole run, not once per day (see "
            "stress_core.run_bulk_analysis), and paces itself between days to avoid Garmin's rate "
            "limits, so a large range can still take a while (expect minutes, not seconds). Skips "
            "dates already in the database (use --force-refresh to redo them anyway) and keeps "
            "going past per-day failures (e.g. a day with no synced Garmin data) instead of "
            "aborting the whole batch. Also useful for pre-loading history before a demo, so the "
            "dashboard doesn't need any live Garmin calls while you're showing it to someone."
        ),
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="With --backfill-days, re-fetch dates that already have stored results instead of skipping them.",
    )
    return parser.parse_args()


def run_single_day(config: dict, target_date: date, db_path: str, output_path: Path | None, save_chart: bool) -> None:
    """Analyze one day, print the summary, persist to the DB, and optionally
    save a PNG chart. Raises ConfigError/ValueError on failure — the caller
    decides whether that's fatal (single-day mode) or just skip-and-continue
    (backfill mode)."""
    grid, events, recovery = stress_core.run_analysis(config, target_date)
    print(stress_core.format_summary_text(grid, target_date))

    storage.init_db(db_path)
    event_summary = stress_core.summarize_by_event(grid)
    deltas = stress_core.compute_meeting_deltas(grid, events)
    event_summary = event_summary.join(deltas)
    storage.save_day(db_path, target_date, grid, event_summary, events)
    storage.save_recovery_day(db_path, target_date, recovery["sleep"], recovery["body_battery"])
    print(f"\nResults saved to: {db_path}")

    if save_chart:
        chart_path = output_path or Path(f"stress_report_{target_date.isoformat()}.png")
        fig = stress_core.build_daily_chart(grid, events, target_date)
        fig.savefig(chart_path, dpi=150)
        print(f"Chart saved to: {chart_path}")


def run_backfill(config: dict, days: int, db_path: str, force_refresh: bool) -> None:
    """Analyze the last `days` days, reusing ONE Garmin login and ONE parsed
    calendar across the whole batch (stress_core.run_bulk_analysis) rather
    than looping run_single_day() — critical for a large historical pull
    (weeks/months): logging in and re-parsing a multi-MB .ics file per day
    would be wasteful and risks tripping Garmin's rate limiting. Never lets
    one bad day (no synced data, a transient error) abort the rest of the
    batch. Meant to be run well ahead of a live demo (so the dashboard can
    run entirely off stored data), or to pull in months of history at once."""
    storage.init_db(db_path)

    all_dates = [date.today() - timedelta(days=offset) for offset in range(days)]
    if force_refresh:
        target_dates, skipped_dates = all_dates, []
    else:
        target_dates = [d for d in all_dates if not storage.has_day(db_path, d)]
        skipped_dates = [d for d in all_dates if d not in target_dates]

    for d in skipped_dates:
        print(f"{d.isoformat()}: already have results, skipping (use --force-refresh to redo)")

    results: list[tuple[date, str]] = [(d, "skipped (cached)") for d in skipped_dates]
    if not target_dates:
        print("\nNothing to fetch — every requested day is already cached.")
    else:
        print(
            f"\nFetching {len(target_dates)} day(s). This can take a while for a large range — "
            f"paced at ~{stress_core.BULK_FETCH_DELAY_SECONDS:.0f}s between days to be gentle on "
            f"Garmin's rate limits, on top of each day's actual fetch time."
        )

        def on_progress(index: int, total: int, target_date: date) -> None:
            print(f"[{index + 1}/{total}] {target_date.isoformat()}...")

        try:
            bulk_results = stress_core.run_bulk_analysis(config, target_dates, on_progress=on_progress)
        except (ConfigError, ValueError) as exc:
            sys.exit(f"Backfill aborted before it could start: {exc}")

        for target_date in target_dates:
            outcome = bulk_results[target_date]
            if isinstance(outcome, Exception):
                print(f"  -> {target_date.isoformat()} skipped: {outcome}")
                results.append((target_date, f"failed: {outcome}"))
                continue
            grid, events, recovery = outcome
            event_summary = stress_core.summarize_by_event(grid)
            deltas = stress_core.compute_meeting_deltas(grid, events)
            event_summary = event_summary.join(deltas)
            storage.save_day(db_path, target_date, grid, event_summary, events)
            storage.save_recovery_day(db_path, target_date, recovery["sleep"], recovery["body_battery"])
            results.append((target_date, "ok"))

    results.sort(key=lambda item: item[0], reverse=True)
    print(f"\n{'=' * 60}\nBackfill summary ({days} days)\n{'=' * 60}")
    for target_date, status in results:
        print(f"  {target_date.isoformat()}: {status}")


def main() -> None:
    args = parse_args()

    if args.setup:
        run_setup_wizard()
        return

    config = stress_core.resolve_config()
    interactive = sys.stdin.isatty()
    if interactive:
        config = prompt_for_missing(config)
    elif not (config["email"] and config["ics_path"] and config["password"]):
        sys.exit(
            "Missing configuration for a non-interactive run. Run 'python3 stress_analyzer.py --setup' first."
        )

    if args.backfill_days is not None:
        run_backfill(config, args.backfill_days, args.db, args.force_refresh)
        return

    try:
        run_single_day(config, args.date, args.db, args.output, save_chart=True)
    except (ConfigError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
