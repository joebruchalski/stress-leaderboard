# Stress Leaderboard

Correlates your [Garmin Connect](https://connect.garmin.com/) stress/Body Battery data with your calendar to show what's *actually* driving your stress during the workday — which meetings, and which people, correlate with your stress spiking.

Everything runs locally: your Garmin password lives only in the macOS Keychain, and computed history is stored in a local SQLite database. Nothing is sent anywhere except to Garmin's own API and your calendar provider, to fetch your own data.

> **This is source code, not a hosted app.** Nothing runs on GitHub — to use it, you clone it and run it on your own Mac with your own Garmin account and calendar. See [Quick Start](#quick-start) below.

## Quick Start

```bash
git clone https://github.com/joebruchalski/stress-leaderboard.git
cd stress-leaderboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run dashboard.py
```

The first run will prompt you for your Garmin Connect email/password (password goes straight to the macOS Keychain) and a calendar `.ics` file or private iCal URL — see [Configure](#2-configure) for details. Prefer the newer React UI instead of Streamlit? See [Setup](#setup) below.

## Features

- **Leaderboard** — ranks the people you meet with by average stress impact, so you can see who (and what recurring meetings) correlate with your stress spiking
- **Most Improved** panel and per-person drill-down/history
- **Trends** tab — week-over-week stress, meeting size vs. stress scatter
- **Recovery** tab — how quickly your stress comes back down after a spike
- **Daily Detail** view with an interactive chart and prev/next-day navigation
- **Bulk historical backfill** for pulling in months of past data at once
- Two interchangeable UIs: the original [Streamlit](https://streamlit.io/) dashboard, and a newer React + Vite single-page app backed by a FastAPI JSON API
- Optional unattended daily runs via macOS `launchd`, so the database stays fresh even if you never open a dashboard

## How it works

| File | Role |
|---|---|
| `stress_core.py` | Shared logic: pulls stress/Body Battery from Garmin Connect, parses calendar events (`.ics` file or a live private iCal URL) for the workday, correlates the two |
| `storage.py` | SQLite persistence for computed daily history |
| `stress_analyzer.py` | CLI entry point, meant for scheduled/unattended runs |
| `dashboard.py` | Streamlit app (original UI) |
| `api.py` | FastAPI backend exposing the same functionality as a JSON API |
| `frontend/` | React + TypeScript + Vite single-page app (new UI), talks to `api.py` |

The CLI, dashboard, and API all share the same config, Keychain entry, and SQLite database — use whichever UI you like, or both.

## Requirements

- **macOS** — Garmin password storage uses the Keychain via the `security` CLI; porting to another OS would mean swapping out that piece of `stress_core.py`
- Python 3.11+ (developed on 3.14)
- Node.js 18+ and npm, if you want the React frontend
- A Garmin Connect account with stress/Body Battery data
- A calendar you can either export as `.ics` or access via a private "secret"/webcal iCal URL (Google Calendar, Outlook, and iCloud all support this)

## Setup

### 1. Install Python dependencies

```bash
git clone https://github.com/joebruchalski/stress-leaderboard.git
cd stress-leaderboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

The first time you run the CLI or a dashboard, you'll be prompted (or shown a Settings form) for:

- your Garmin Connect email and password — the password is saved to the macOS Keychain, never written to disk
- either a path to an exported `.ics` file, or your calendar's private iCal URL for live sync
- optionally, your own email (so you're excluded from your own leaderboard) and an internal email domain (so only coworkers count, not customers/vendors)

Non-secret config is saved to `~/.config/stress_analyzer/config.json`. Everything can also be set via environment variables instead, which is useful for the unattended/`launchd` case: `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `ICS_FILE_PATH`, `ICS_URL`, `GARMIN_TOKENSTORE`, `CALENDAR_EMAIL`, `INTERNAL_DOMAIN`.

### 3. Run it

**Option A — Streamlit dashboard (single process):**

```bash
streamlit run dashboard.py
```

**Option B — React frontend + API:**

```bash
# terminal 1
uvicorn api:app --port 8000 --reload

# terminal 2
cd frontend
npm install
npm run dev
```

Then open the URL Vite prints (defaults to http://localhost:5173).

**CLI (one-off run, also what the automated job calls):**

```bash
python3 stress_analyzer.py
```

### 4. (Optional) Automate daily runs on macOS

`com.joebruchalski.stressanalyzer.plist` is a `launchd` job template that runs `stress_analyzer.py` once a day so `history.db` stays current without opening a dashboard. Copy it, edit the hardcoded paths for your own username and checkout location, then:

```bash
cp com.joebruchalski.stressanalyzer.plist ~/Library/LaunchAgents/com.<you>.stressanalyzer.plist
launchctl load ~/Library/LaunchAgents/com.<you>.stressanalyzer.plist
```

## Tests

```bash
pytest
```

## Privacy

Your Garmin password never touches disk — it's stored only in the macOS Keychain (`security` CLI, service name `stress-analyzer-garmin`). Calendar data and computed correlations live only in a local SQLite database at `~/.config/stress_analyzer/history.db`. Nothing here phones home to any third party.

## License

MIT — see [LICENSE](LICENSE).
