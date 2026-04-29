# Weekend Planner

Automatically searches for weekend activities in St. Louis, checks the family calendar, and emails a curated summary every week via Windows Task Scheduler (Wednesdays at noon).

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
   - SLU Billikens (Baseball)
5. **Venue event pages** — Firecrawl scrapes/searches for specific events and rotating exhibits at favorite venues:
   - Magic House, Science Center, Aquarium, Forest Park, Made for Kids, City Museum — Firecrawl search (full page content, not snippets)
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

- `mackenzie.hofford@gmail.com`
- `Anne_Longtine@yahoo.com`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. API keys

Add these as Windows user environment variables (Win+S → "Edit environment variables" → User variables → New):

| Variable | Description |
|---|---|
| `AGENTMAIL_API_KEY` | AgentMail — email delivery |
| `ANTHROPIC_API_KEY` | Anthropic — Claude LLM |
| `FIRECRAWL_API_KEY` | Firecrawl — full page scraping for venue event pages |
| `TAVILY_API_KEY` | Tavily — relevance-scored search for neighborhood/activity queries |

### 3. Google Calendar credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project and enable the **Google Calendar API**
3. Create an OAuth 2.0 credential (Desktop app) and download it as `credentials.json`
4. Place `credentials.json` in this directory
5. On first run, a browser window will open for Google sign-in — approve it and `token.json` will be saved for future runs

> **Note:** The OAuth app is currently in Testing mode, which causes `token.json` to expire every 7 days. To fix permanently, publish the app via the OAuth consent screen in Google Cloud Console (requires filling in App name and contact email fields).

### 4. Family profile

Edit `family_profile.json` to update family members, interests, location, or venue preferences. Ages are calculated automatically from `birth_year` and `birth_month` — no manual updates needed. Favorite venues listed under `interests` are used to drive the venue event searches.

## Running

The script reads API keys from Windows user environment variables via PowerShell internally — no need to set them manually before running.

**Normal run** (searches, generates, and sends the email):
```bash
"C:\Program Files\Python312\python.exe" weekend_planner.py
```

**Debug mode** (runs all searches, writes prompt to `debug_prompt.txt`, skips Claude and email — free to run):
```bash
"C:\Program Files\Python312\python.exe" weekend_planner.py --debug
```

> **Important:** Use Python 3.12 (`C:\Program Files\Python312\python.exe`), not the default `python` command. Python 3.14 is also installed but is missing the Google API libraries required by this script.

## Error handling

- **Widespread network failures** — If 5 or more data sources fail (e.g. DNS outage at scheduled run time), the script aborts without calling Claude or sending an email to avoid wasting API tokens. The specific failed sources are printed to the log. Re-run manually once the network is available.
- **Partial failures** — If fewer than 5 sources fail, the email is sent as normal but Claude includes a note listing which sources were unavailable.
- **Google Calendar token expiry** — If `token.json` is invalid, the error is caught silently and the email sends without calendar data rather than crashing.

## Scheduled runs

A Windows Task Scheduler task named **WeekendPlanner** runs `run.bat` every Wednesday at noon. Output is appended to `run.log`. The task is configured to run even when on battery power.

To check or modify the schedule: Win+S → "Task Scheduler" → Task Scheduler Library → WeekendPlanner.

## Files

| File | Description |
|---|---|
| `weekend_planner.py` | Main script |
| `family_profile.json` | Family info, location, interests, and venue preferences |
| `run.bat` | Wrapper script used by Windows Task Scheduler (logs to `run.log`) |
| `run_full.bat` | Interactive wrapper — runs the full pipeline and keeps the window open |
| `run_debug.bat` | Interactive wrapper — runs in debug mode and keeps the window open |
| `requirements.txt` | Python dependencies |
| `credentials.json` | Google OAuth credentials — download from Google Cloud Console, do not commit |
| `token.json` | Auto-generated Google auth token — do not commit |
| `debug_prompt.txt` | Generated by `--debug` mode — shows the full prompt sent to Claude |
| `run.log` | Task Scheduler run log |
| `emails/` | Archived HTML emails, one per run (`weekend_YYYY-MM-DD.html`) |

## Maintenance notes

- **College schedule URLs** use season slugs (e.g. `/schedule/2026`, `/schedule/2025-26`). Update `COLLEGE_SCHEDULES` in `weekend_planner.py` at the start of each new season.
- **ESPN team IDs** are stable and should not need updating.
- **ESPN `home_venue` names** must match what ESPN returns for the game venue. City SC's stadium was renamed from CityPark to Energizer Park in 2026 — the old name caused every home game to be silently dropped. Update `ESPN_TEAMS` if a venue is renamed again. The Battlehawks play at The Dome at America's Center.
- **BenchApp calendar** (`ksm5fif8viu1ci868nuf0prlciuq7cde@import.calendar.google.com`) is Mac's beer league hockey schedule. It's included in `GCAL_CALENDARS` and Claude is told what it represents in the prompt.
- **Fox Sports City2 URL** (`/soccer/saint-louis-city-sc-2-team-schedule`) — if this ever breaks, Fox Sports is the only known server-rendered source for City2's schedule. ESPN has no MLS NEXT Pro data; mlsnextpro.com and stlcitysc.com/city2/schedule are JS-rendered.
- **Google token** (`token.json`) refreshes automatically but expires every 7 days while the OAuth app is in Testing mode. If it stops working, delete `token.json` and run the script interactively (not via Task Scheduler) to re-authenticate via browser.
- **Firecrawl credits** — Hobby plan allows ~500 scrapes/month. The script uses roughly 12–15 Firecrawl calls per run (8 venue searches + 2 direct scrapes + 2 neighborhood searches). At one run/week that's ~60 credits/month, well within the free tier.
- **Tavily credits** — Free tier allows 1,000 searches/month. The script uses ~10 Tavily calls per run (~40/month at one run/week).
- **Task Scheduler** runs `run.bat` every Wednesday at noon. If the task fails, check `run.log` for errors. The task must use the full Python 3.12 path — `run.bat` is already configured for this.
