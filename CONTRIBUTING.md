# Contributing

This started as a personal tool, so there's no formal process — open an issue or a PR and it'll get a look. A few notes to make that go smoothly:

## Before you start

- Run the test suite (`pytest`) and the frontend checks (`cd frontend && npx tsc -b --noEmit && npm run lint`) before opening a PR. Both should be clean on `main`.
- Keep `stress_core.py`/`storage.py` as the single source of truth for logic — `dashboard.py`, `api.py`, and the CLI should all call into them rather than duplicating behavior, so the two UIs stay in sync.
- No secrets in commits, ever — credentials belong in the macOS Keychain or `~/.config/stress_analyzer/`, never in a file inside the repo.

## Good first contributions

These are real gaps, not busywork — pick either one up if you want to extend what the tool can do.

### Windows/Linux support

The only OS-specific code is Garmin password storage, which shells out to macOS's `security` CLI (`get_keychain_password` / `save_keychain_password` in `stress_core.py`, service name `stress-analyzer-garmin`). Everything else — the Garmin API calls, calendar parsing, SQLite storage, both dashboards — is already cross-platform.

To port it:
1. Swap those two functions for the [`keyring`](https://pypi.org/project/keyring/) package, which has Windows Credential Locker / Linux Secret Service / macOS Keychain backends behind one API.
2. Update `resolve_config()`'s docstring/comments and the README's Requirements section accordingly.
3. The `launchd` automation (`com.joebruchalski.stressanalyzer.plist`) is macOS-only by nature — a Windows Task Scheduler XML or a `cron` entry would be the equivalent for other platforms, as a separate follow-up.

### Oura Ring integration

Right now the only stress/recovery data source is Garmin Connect, via the calls in `stress_core.py`. Oura has its own public API exposing similar readiness/sleep/stress-adjacent metrics.

Shape of the change:
1. Add an `oura_core.py` alongside `stress_core.py`, following the same pattern — a thin wrapper around Oura's API that returns data in the same shape `stress_core`'s Garmin functions do (so the correlation-with-calendar logic doesn't need to change).
2. Extend `resolve_config()`/the Settings UI so the data source is a config choice, not a hardcoded import.
3. `storage.py`'s schema would likely need a `source` column if you want Garmin and Oura history to coexist in the same database rather than being mutually exclusive per install.

## Reporting bugs

Include your Python version, OS, and (if calendar-related) whether you're using a local `.ics` file or a live `ics_url` — the two paths have different failure modes (see `resolve_calendar_bytes()` in `stress_core.py`).
