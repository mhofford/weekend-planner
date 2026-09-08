# Weekend Planner

Automatically searches for weekend activities in St. Louis, checks the family calendar, and emails a curated summary every week via a scheduled task (Wednesdays at noon).

## What it does

1. **Weather** — Fetches a 3-day forecast (Friday–Sunday) from Open-Meteo (no API key required)
2. **Google Calendar** — Pulls existing family events for the weekend so Claude can weave them into the email (BenchApp calendar = Mac's beer league hockey games)
3. **Pro sports schedules** — Multiple sources, each chosen based on what's actually available:
   - **Blues (NHL), Cardinals (MLB), City SC (MLS), Battlehawks (UFL)** — ESPN scoreboard API (queried by date, not team schedule, to avoid fixture gaps)
   - **City2 (MLS NEXT Pro)** — Fox Sports HTML scrape; ESPN has no MLS NEXT Pro coverage and the official mlsnextpro.com site is JS-rendered
   - Cardinals spring training games are filtered out automatically via the home venue check
4. **College sports schedules** — Fetches official athletics schedule pages directly for home game detection:
   - Lindenwood Lions (Hockey)
   - WashU Bears (Baseball, Soccer)
   - SLU Billikens (Baseball, Soccer)
5. **Venue event pages** — Firecrawl scrapes/searches for specific events and rotating exhibits at favorite venues:
   - Magic House, Science Center, Aquarium, Forest Park, Made for Kids, City Museum, The Muny, Missouri History Museum, Union Station, Grant's Farm — Firecrawl search (full page content, not snippets)
   - St. Louis Zoo — direct Firecrawl scrape of `stlzoo.org/events/`
   - Missouri Botanical Garden — Tavily search targeting `missouribotanicalgarden.org` (events calendar is JS-rendered)
6. **Neighborhood & activity search** — Tavily searches (with DuckDuckGo fallback) for:
   - Nearby neighborhood events (Soulard, Lafayette Square, Tower Grove, Cherokee, South Grand)
   - `explorestlouis.com` date-specific event pages via Firecrawl
   - `stlparent.com` event articles via Tavily
   - Friday evening ideas
   - General family-friendly and outdoor activities
7. **Email generation** — Sends all data to Claude (Opus 4.6) to evaluate relevance and write a warm, practical email with a **Top 3 Picks** highlight at the top (one standout per day). If any data sources failed to load, Claude calls them out in the email.
8. **Delivery** — Sends HTML-only email via AgentMail to all recipients
9. **Archive** — Saves a local copy of every generated email to `emails/weekend_YYYY-MM-DD.html` for review and troubleshooting

## Recipients

The sender inbox and recipient list live in `family_profile.json` (which is gitignored, so no personal addresses are committed):

```json
"email": {
  "sender_inbox": "you@agentmail.to",
  "recipients": ["someone@example.com", "another@example.com"]
}
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. API keys

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
chmod 600 .env
```

| Variable | Required? | Description | Get one at |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | Anthropic — Claude LLM | console.anthropic.com |
| `AGENTMAIL_API_KEY` | **yes** | AgentMail — email delivery | app.agentmail.to |
| `FIRECRAWL_API_KEY` | no | Firecrawl — full page scraping for venue event pages | firecrawl.dev |
| `TAVILY_API_KEY` | no | Tavily — relevance-scored search for neighborhood/activity queries | app.tavily.com |

`.env` is gitignored. The script loads it automatically (via `python-dotenv`); real environment
variables, if set, take precedence. Without the two required keys the script exits early with a
message. Without the optional keys it still runs, falling back to DuckDuckGo search.

### 3. Google Calendar credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project and enable the **Google Calendar API**
3. Create an OAuth 2.0 credential (Desktop app) and download it as `credentials.json`
4. Place `credentials.json` in this directory
5. On first run, a browser window will open for Google sign-in — approve it and `token.json` will be saved for future runs

> **Note:** The OAuth app is currently in Testing mode, which causes `token.json` to expire every 7 days. To fix permanently, publish the app via the OAuth consent screen in Google Cloud Console (requires filling in App name and contact email fields).

### 4. Family profile

Edit `family_profile.json` to update family members, interests, location, or venue preferences. Ages are calculated automatically from `birth_year` and `birth_month` — no manual updates needed. Favorite venues listed under `interests` are used to drive the venue event searches.

This file also holds the personal config that used to be hardcoded in `weekend_planner.py`:

- `email` — `sender_inbox` and the `recipients` list (see [Recipients](#recipients))
- `calendars` — a `{"Display Name": "calendar-id"}` map of the Google Calendars to pull weekend events from. `BenchApp` is Mac's beer league hockey schedule; Claude is told what it represents in the prompt.

The script exits with a clear error if `email` or `calendars` is missing.

## Running

API keys are loaded from `.env` automatically — no need to export anything first.

**Normal run** (searches, generates, and sends the email):
```bash
python3 weekend_planner.py      # or: ./run_full.sh
```

**Debug mode** (runs all searches, writes prompt to `debug_prompt.txt`, skips Claude and email — free to run):
```bash
python3 weekend_planner.py --debug      # or: ./run_debug.sh
```

## Error handling

- **Widespread network failures** — If 5 or more data sources fail (e.g. DNS outage at scheduled run time), the script aborts without calling Claude or sending an email to avoid wasting API tokens. The specific failed sources are printed to the log. Re-run manually once the network is available.
- **Partial failures** — If fewer than 5 sources fail, the email is sent as normal but Claude includes a note listing which sources were unavailable.
- **Google Calendar token expiry** — If `token.json` is invalid, the error is caught silently and the email sends without calendar data rather than crashing.

## Scheduled runs

A **systemd user timer** runs the pipeline every Wednesday at noon. Units are in `systemd/`
(they use the `%h` specifier and assume the repo lives at `~/Projects/weekend-planner`):

```bash
cp systemd/weekend-planner.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now weekend-planner.timer

systemctl --user list-timers weekend-planner.timer   # check next run
journalctl --user -u weekend-planner.service -n 50    # view last run's log
```

Why a timer and not cron: on a laptop, cron silently skips the job if the machine is
asleep or off at noon. The timer has `Persistent=true`, so a missed run fires on the
next wake/boot. Output also goes to `run.log` (via `run.sh`).

- **Manual run:** `systemctl --user start weekend-planner.service` (sends a real email).
- **Survive logout:** `sudo loginctl enable-linger $USER` so the timer runs even when
  you're not logged into a graphical session. Optional — with `Persistent=true` a run
  missed while logged out is caught up at next login anyway.
- `run.sh` auto-selects a `python3` that has the dependencies (cron/systemd run with a
  minimal `PATH` where bare `python3` is the dependency-free system Python). Override
  with `WEEKEND_PLANNER_PYTHON=/path/to/python3`.

## Files

| File | Description |
|---|---|
| `weekend_planner.py` | Main script |
| `family_profile.json` | Family info, location, interests, venue preferences, email config, and calendar IDs (gitignored) |
| `.env` | API keys (gitignored) — copy from `.env.example` |
| `.env.example` | Template listing the required and optional API keys |
| `run.sh` | Wrapper used by the systemd timer — picks a working `python3`, logs to `run.log` |
| `run_full.sh` | Interactive wrapper — runs the full pipeline |
| `run_debug.sh` | Interactive wrapper — runs in debug mode |
| `systemd/` | `weekend-planner.service` + `.timer` for the weekly scheduled run |
| `requirements.txt` | Python dependencies |
| `credentials.json` | Google OAuth credentials — download from Google Cloud Console, do not commit |
| `token.json` | Auto-generated Google auth token — do not commit |
| `debug_prompt.txt` | Generated by `--debug` mode — shows the full prompt sent to Claude |
| `run.log` | Scheduled-run log |
| `emails/` | Archived HTML emails, one per run (`weekend_YYYY-MM-DD.html`) |

## Maintenance notes

- **College schedule URLs** — some use season slugs (e.g. `/schedule/2026`, `/schedule/2025-26`); update `COLLEGE_SCHEDULES` in `weekend_planner.py` at the start of each new season. If a slug goes stale, SIDEARM redirects to the site homepage with a 200 — `fetch_college_home_games` now detects that (final URL no longer under `/schedule`) and reports the source as unavailable instead of silently returning no games.
- **ESPN team IDs** are stable and should not need updating.
- **ESPN `home_venue` names** must match what ESPN returns for the game venue. City SC's stadium was renamed from CityPark to Energizer Park in 2026 — the old name caused every home game to be silently dropped. Update `ESPN_TEAMS` if a venue is renamed again. The Battlehawks play at The Dome at America's Center.
- **BenchApp calendar** is Mac's beer league hockey schedule. It's one of the entries under `calendars` in `family_profile.json` and Claude is told what it represents in the prompt.
- **Fox Sports City2 URL** (`/soccer/saint-louis-city-sc-2-team-schedule`) — if this ever breaks, Fox Sports is the only known server-rendered source for City2's schedule. ESPN has no MLS NEXT Pro data; mlsnextpro.com and stlcitysc.com/city2/schedule are JS-rendered.
- **Google token** (`token.json`) refreshes automatically but expires every 7 days while the OAuth app is in Testing mode. If it stops working, delete `token.json` and run the script interactively (not via the scheduled task) to re-authenticate via browser.
- **Firecrawl credits** — Hobby plan allows ~500 scrapes/month. The script uses roughly 16–19 Firecrawl calls per run (12 venue searches + 2 direct scrapes + 2 neighborhood searches). At one run/week that's ~70 credits/month, well within the free tier.
- **Tavily credits** — Free tier allows 1,000 searches/month. The script uses ~10 Tavily calls per run (~40/month at one run/week).
- **Scheduled runs** — the systemd user timer runs `run.sh` every Wednesday at noon (see [Scheduled runs](#scheduled-runs)). Check `journalctl --user -u weekend-planner.service` or `run.log` after a failure.
