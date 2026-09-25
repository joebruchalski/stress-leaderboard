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
from datetime import date, datetime
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
            config["email"], config["ics_path"], config["tokenstore"], config["calendar_email"], config["internal_domain"]
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
        config["email"], config["ics_path"], config["tokenstore"], config["calendar_email"], config["internal_domain"]
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
    return parser.parse_args()


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

    try:
        grid, events = stress_core.run_analysis(config, args.date)
    except (ConfigError, ValueError) as exc:
        sys.exit(str(exc))

    print(stress_core.format_summary_text(grid, args.date))

    storage.init_db(args.db)
    event_summary = stress_core.summarize_by_event(grid)
    deltas = stress_core.compute_meeting_deltas(grid, events)
    event_summary = event_summary.join(deltas)
    storage.save_day(args.db, args.date, grid, event_summary, events)
    print(f"\nResults saved to: {args.db}")

    output_path = args.output or Path(f"stress_report_{args.date.isoformat()}.png")
    fig = stress_core.build_daily_chart(grid, events, args.date)
    fig.savefig(output_path, dpi=150)
    print(f"Chart saved to: {output_path}")


if __name__ == "__main__":
    main()
