---
name: stress-analyzer-dev
description: Use for any development work on the Garmin stress vs. calendar correlation tool in this repo — stress_core.py, storage.py, stress_analyzer.py, dashboard.py, and the launchd automation. Covers bug fixes, new features, chart/dataviz changes, DB schema changes, and Garmin/calendar API work. Use proactively whenever the user asks to modify, debug, or extend this tool rather than re-deriving the project from scratch.
tools: Read, Edit, Write, Bash, Grep, Glob
model: inherit
---

You are working on a personal analytics tool that correlates Garmin Connect stress data with calendar events for a 9 AM–5 PM workday. The user runs macOS, Python 3.14 (Homebrew), and this directory is not a git repo.

## Layout

- `stress_core.py` — all shared logic, no blocking terminal I/O (no `input()`/`getpass()`). Config resolution, Garmin login, stress fetch, `.ics` parsing, minute-by-minute correlation, analytics, matplotlib chart builders. Both the CLI and the dashboard import from here — keep it that way; don't duplicate logic into either entry point.
- `storage.py` — SQLite persistence (`~/.config/stress_analyzer/history.db`). Three tables: `stress_minutes` (date, timestamp_local, stress, event — one row per minute), `event_summary` (date, event, avg_stress, peak_stress, minutes), `daily_summary` (date, overall_avg, overall_peak, computed_at). `save_day()` deletes-then-inserts per date, so re-running a day is idempotent.
- `stress_analyzer.py` — CLI entry point. Also what the launchd job runs unattended. Has `--setup` (one-time interactive config wizard) and `--date`/`--output`/`--db` flags. Falls back to a clean `sys.exit` (not a hang) when required config is missing and stdin isn't a TTY — preserve that behavior, since a hang would break the automated job silently.
- `dashboard.py` — Streamlit UI (`streamlit run dashboard.py`, localhost:8501). "Daily Detail" tab (single day, fetch-or-view-cached) and "Trends" tab (date range, rollups). Uses `st.pyplot(fig, width="stretch")` — NOT the deprecated `use_container_width` (already past its removal date as of this project).
- `com.joebruchalski.stressanalyzer.plist` — launchd job, daily 6:30 PM, staged in `~/Library/LaunchAgents/` but loading/unloading it is a persistent background action with real credential access — never `launchctl load`/`unload` it yourself without the user explicitly asking in that turn.

## Environment

Always `source .venv/bin/activate` (or invoke `.venv/bin/python3` directly) before running or testing anything — the system `python3` doesn't have the dependencies and will fail with `ModuleNotFoundError`. After changing `requirements.txt`, `pip install -r requirements.txt` inside the activated venv.

The Streamlit dashboard, if left running in the background from a prior session, does NOT hot-reload on file changes (no `watchdog` installed) — after editing `stress_core.py`, `storage.py`, or `dashboard.py`, kill and restart it: `pkill -f "streamlit run dashboard.py"` then relaunch, or just tell the user to rerun.

## Hard-won gotchas — don't re-break these

1. **Never guess at the `garminconnect` library's internals.** A prior guess (`api.garth.dump(...)`) crashed with `AttributeError` because that attribute doesn't exist in the installed version. Before writing code against `garminconnect.Garmin`, inspect the actual installed source: `python3 -c "import garminconnect, inspect; print(inspect.getsource(garminconnect.Garmin.<method>))"`. The installed version's `Garmin.login(tokenstore)` already handles cached-token-first, fresh-login-fallback, and persisting new tokens internally — don't add manual token dump/load logic on top of it. Known exceptions: `GarminConnectAuthenticationError`, `GarminConnectTooManyRequestsError`, `GarminConnectConnectionError`, `GarminConnectNotFoundError`, `GarminConnectInvalidFileFormatError`.
2. **Matplotlib's `DateFormatter` silently defaults to UTC**, even for tz-aware pandas Timestamps — it will *not* infer the data's timezone. Always pass `tz=` explicitly (see `build_daily_chart`/`build_trend_chart` in `stress_core.py`) or times will render hours off with no error or warning.
3. **Credentials are never written to disk in plaintext.** Email/`.ics` path/tokenstore path live in `~/.config/stress_analyzer/config.json`; the Garmin password lives only in the macOS Keychain (service name `stress-analyzer-garmin`, via the `security` CLI — see `save_keychain_password`/`get_keychain_password`). If you add new secrets, follow this pattern, not a config file.
4. **Config resolution order** (see `resolve_config()`): env vars (`GARMIN_EMAIL`, `GARMIN_PASSWORD`, `ICS_FILE_PATH`, `GARMIN_TOKENSTORE`) → saved config file/Keychain → caller fills gaps (CLI prompts interactively; dashboard shows a sidebar form). Don't add a code path that calls `input()`/`getpass()` inside `stress_core.py` — that would break the dashboard and the automated job.
5. **Calendar input is a local `.ics` file, not a live feed** (deliberate choice — deferred: live private-ICS-URL sync). If the user reports stale calendar data in an automated run, that's expected until live sync is added, not a bug.
6. **Chart colors**: `stress_core.EVENT_COLORS` is a validated colorblind-safe categorical palette used in fixed order (from the `dataviz` skill's reference palette) — never cycle it, never reorder it arbitrarily, and fold anything past 8 concurrent event colors into `OVERFLOW_COLOR`. If asked to change chart styling meaningfully, load the `dataviz` skill first rather than picking colors by eye.
7. **After any chart-code change, actually render it and look at the PNG** (`Read` the saved file) before calling it done — a past bug (the UTC/local time axis shift) was invisible in the code and only caught by rendering and inspecting the output.

## Testing approach used so far

- `python3 -m py_compile <file>.py` for a fast syntax check after edits.
- Synthetic-data round-trip tests for `storage.py` (build a fake minute-grid DataFrame, `save_day`, then read it back and diff).
- `streamlit.testing.v1.AppTest.from_file("dashboard.py")` to catch runtime exceptions in the dashboard without a real browser — check `at.exception` is empty.
- No real Garmin credentials are available in a dev/agent context — don't attempt a live login as a test; ask the user to run `python3 stress_analyzer.py` themselves to verify anything touching real Garmin auth.
- Clean up any test artifacts you create (temp DB rows, Keychain test entries, background `streamlit` processes) before finishing — don't leave synthetic data sitting in the user's real `~/.config/stress_analyzer/history.db`.
