#!/usr/bin/env python3
"""
Weekend Planner
Searches for upcoming games and family activities in St. Louis,
evaluates results with Claude for relevance, then emails a summary.
"""

import json
import os
import re
import subprocess
import sys
import requests
import anthropic
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ddgs import DDGS
from agentmail import AgentMail
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CENTRAL = ZoneInfo("America/Chicago")  # handles CDT/CST automatically

SENDER_INBOX = "mhofford@agentmail.to"
RECIPIENTS = ["mackenzie.hofford@gmail.com", "Anne_Longtine@yahoo.com"]
PROFILE_PATH = "family_profile.json"

# Google Calendar OAuth files — credentials.json downloaded from Google Cloud Console,
# token.json is auto-generated on first run and reused thereafter.
# If token.json becomes invalid (e.g. after a long gap), delete it and run the script
# interactively (not via Task Scheduler) so the browser OAuth flow can re-authenticate.
GCAL_CREDENTIALS_FILE = "credentials.json"
GCAL_TOKEN_FILE = "token.json"
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Calendars to pull weekend events from. Add or remove as needed.
GCAL_CALENDARS = {
    "Family":        "mackenzie.hofford@gmail.com",
    "Mac & Anne":    "anne.mac.wedding@gmail.com",
    "Luke's School": "lafayetteprep.org_robbu23p0k2c6sd3piuqbfhs88@group.calendar.google.com",
    "BenchApp":      "ksm5fif8viu1ci868nuf0prlciuq7cde@import.calendar.google.com",
}


# ---------------------------------------------------------------------------
# API Key Helpers
# ---------------------------------------------------------------------------

def _get_win_env(var_name):
    """Fetch a Windows user environment variable via PowerShell.
    Needed because Git Bash does not inherit Windows user env vars."""
    return subprocess.check_output(
        ["powershell.exe", "-Command",
         f"[System.Environment]::GetEnvironmentVariable('{var_name}', 'User')"],
        text=True
    ).strip()


def get_agentmail_key():
    # Prefer env var (e.g. set at shell level); fall back to Windows user env var
    return os.environ.get("AGENTMAIL_API_KEY") or _get_win_env("AGENTMAIL_API_KEY")


def get_anthropic_key():
    return os.environ.get("ANTHROPIC_API_KEY") or _get_win_env("ANTHROPIC_API_KEY")


def get_firecrawl_key():
    return os.environ.get("FIRECRAWL_API_KEY") or _get_win_env("FIRECRAWL_API_KEY") or ""


def get_tavily_key():
    return os.environ.get("TAVILY_API_KEY") or _get_win_env("TAVILY_API_KEY") or ""




def load_profile(path=PROFILE_PATH):
    """Load family_profile.json and compute each member's current age.
    Members under 2 years get age_weeks; others get age in years."""
    with open(path) as f:
        profile = json.load(f)
    today = datetime.now()
    for m in profile["family_members"]:
        if "birth_year" in m:
            born = datetime(m["birth_year"], m["birth_month"], 1)
            age_days = (today - born).days
            if age_days < 365 * 2:
                m["age_weeks"] = age_days // 7
                m["role"] = "Infant"
            else:
                # Subtract 1 if their birth month hasn't occurred yet this year
                years = today.year - m["birth_year"]
                if (today.month, today.day) < (m["birth_month"], 1):
                    years -= 1
                m["age"] = years
                m["role"] = "Child" if years < 18 else "Adult"
    return profile


def search(query, max_results=3):
    """Run a DuckDuckGo text search and return up to max_results results.
    Returns an empty list if the query produces no results or DDG raises an error."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception:
        return []


def tavily_search(query, max_results=5, api_key=""):
    """Search via Tavily and return results in the same dict shape as DDG (title/body/href).
    Tavily returns relevance-scored results with richer content than DDG snippets."""
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "basic"},
            timeout=20,
        )
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "body": r.get("content", "")[:600], "href": r.get("url", "")}
            for r in resp.json().get("results", [])
        ]
    except Exception:
        return []


def firecrawl_scrape(url, api_key=""):
    """Scrape a single URL via Firecrawl and return a result dict (title/body/href).
    Returns None on any error so callers can skip gracefully."""
    if not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        body = (data.get("markdown") or "").strip()
        if not body:
            return None
        return {
            "title": data.get("metadata", {}).get("title", url),
            "body":  body[:2000],
            "href":  url,
        }
    except Exception:
        return None


def firecrawl_search(query, limit=3, api_key=""):
    """Search via Firecrawl and return scraped page content from result pages.
    Falls back to an empty list on any error. Results use the same dict shape
    as DDG (title/body/href) so the rest of the pipeline is unchanged."""
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "limit": limit, "scrapeOptions": {"formats": ["markdown"]}},
            timeout=30,
        )
        resp.raise_for_status()
        results = []
        for item in resp.json().get("data", []):
            results.append({
                "title": item.get("title", ""),
                "body":  (item.get("markdown") or item.get("description", ""))[:2000],
                "href":  item.get("url", ""),
            })
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# ESPN Schedule
# ---------------------------------------------------------------------------

# Pro teams tracked via the ESPN scoreboard API.
# Each entry needs: sport slug, league slug, ESPN team ID, and home venue name.
#
# IMPORTANT — why scoreboard instead of team schedule endpoint:
# The ESPN team schedule endpoint (/teams/{id}/schedule) only returns a limited
# window of future fixtures and can have gaps. The scoreboard endpoint
# (/scoreboard?dates=YYYYMMDD) queries by date across the whole league and is
# consistently complete. We learned this when City SC's March 21 game didn't
# appear via the team schedule but showed up immediately on the scoreboard.
#
# IMPORTANT — home_venue filter:
# The venue name must match what ESPN returns for the game. City SC's stadium
# was renamed from CityPark to Energizer Park in 2026 — the old name caused
# every City SC home game to be silently dropped. Update this if the venue
# name changes again.
#
# City2 (MLS NEXT Pro) is NOT listed here — ESPN has no coverage of that league.
# City2 is fetched separately via Fox Sports (see fetch_city2_home_games).
ESPN_TEAMS = [
    {"name": "St. Louis Battlehawks","sport": "football","league": "ufl",   "id": 112651,"home_venue": "The Dome at America's Center"},
    {"name": "St. Louis Blues",     "sport": "hockey",  "league": "nhl",   "id": 19,    "home_venue": "Enterprise Center"},
    {"name": "St. Louis Cardinals", "sport": "baseball","league": "mlb",   "id": 24,    "home_venue": "Busch Stadium"},
    {"name": "St. Louis City SC",   "sport": "soccer",  "league": "usa.1", "id": 21812, "home_venue": "Energizer Park"},
]

# College teams: fetch schedule pages directly from official athletics sites.
# All three schools use the SIDEARM Sports platform, which server-renders schedule
# pages as plain HTML — no API key needed, just a GET request and text parsing.
# Update the season slug (e.g. 2025-26, 2026) each year if needed.
COLLEGE_SCHEDULES = [
    {"name": "Lindenwood Lions", "sport": "Hockey",  "url": "https://lindenwoodlions.com/sports/mhockey-ncaa/schedule/2025-26"},
    {"name": "WashU Bears",      "sport": "Baseball", "url": "https://washubears.com/sports/baseball/schedule/2026"},
    {"name": "WashU Bears",      "sport": "Soccer",   "url": "https://washubears.com/sports/msoccer/schedule/2026"},
    {"name": "SLU Billikens",    "sport": "Baseball", "url": "https://slubillikens.com/sports/baseball/schedule"},
]


def fetch_espn_home_games(weekend_saturday):
    """Query the ESPN scoreboard API for each sport/league on each day of the weekend
    and return home games for our tracked teams.
    Uses the scoreboard endpoint (not team schedule) to avoid gaps in future fixtures.
    Returns a list of dicts: {team, opponent, date_str, time_str, venue}."""
    friday = weekend_saturday - timedelta(days=1)
    sunday = weekend_saturday + timedelta(days=1)
    weekend_days = [friday, weekend_saturday, sunday]

    # Build a lookup: ESPN team id → team config, grouped by (sport, league)
    from collections import defaultdict
    league_teams = defaultdict(dict)  # (sport, league) → {team_id: team_config}
    for team in ESPN_TEAMS:
        league_teams[(team["sport"], team["league"])][team["id"]] = team

    seen = set()  # deduplicate by (team_id, event_date) in case a game spans midnight UTC
    home_games = []
    failed_sources = []
    failed_leagues = set()  # deduplicate per-league errors across days

    for (sport, league), teams_by_id in league_teams.items():
        for day in weekend_days:
            date_str = day.strftime("%Y%m%d")
            url = (
                f"https://site.api.espn.com/apis/site/v2/sports/"
                f"{sport}/{league}/scoreboard?dates={date_str}"
            )
            try:
                data = requests.get(url, timeout=10).json()
                for event in data.get("events", []):
                    competitions = event.get("competitions", [{}])
                    comp = competitions[0] if competitions else {}
                    competitors = comp.get("competitors", [])

                    home_team = next((c for c in competitors if c.get("homeAway") == "home"), None)
                    if not home_team:
                        continue

                    # ESPN returns competitor IDs as strings; cast to int to match our config
                    home_id = int(home_team.get("id", 0))
                    if home_id not in teams_by_id:
                        continue  # not one of our tracked teams

                    team = teams_by_id[home_id]

                    # Parse event time for local display
                    raw_date = event.get("date", "")
                    try:
                        event_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        event_local = event_dt.astimezone(CENTRAL)
                    except Exception:
                        continue

                    dedup_key = (home_id, event_local.date())
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    away_team = next((c for c in competitors if c.get("homeAway") == "away"), None)
                    opponent = away_team["team"]["displayName"] if away_team else "TBD"
                    venue = comp.get("venue", {}).get("fullName", "")

                    # Skip games not at the team's home venue (e.g. MLB spring training).
                    # Match is case-insensitive substring so minor venue name variations still pass.
                    home_venue = team.get("home_venue", "")
                    if home_venue and home_venue.lower() not in venue.lower():
                        continue

                    # .replace(" 0", " ") strips the leading zero from hours (e.g. "07:00 PM" → "7:00 PM")
                    time_str = event_local.strftime("%A, %B %d at %I:%M %p CT").replace(" 0", " ")

                    home_games.append({
                        "team":     team["name"],
                        "opponent": opponent,
                        "time":     time_str,
                        "venue":    venue,
                    })
            except Exception as e:
                print(f"  ESPN scoreboard fetch failed for {sport}/{league} on {date_str}: {e}")
                if league not in failed_leagues:
                    failed_leagues.add(league)
                    failed_sources.append(f"ESPN {league.upper()} scoreboard")

    return home_games, failed_sources


def fetch_college_home_games(weekend_saturday):
    """Fetch each college team's official schedule page and extract home games
    for the target weekend. Parses stripped HTML text for date strings and
    uses 'vs' (home) vs 'at'/'@' (away) notation to filter home games only."""
    friday = weekend_saturday - timedelta(days=1)
    sunday = weekend_saturday + timedelta(days=1)
    weekend_days = [friday, weekend_saturday, sunday]

    # Date strings in formats commonly used on SIDEARM Sports schedule pages
    date_searches = []
    for d in weekend_days:
        date_searches.append((d, d.strftime("%B %d")))         # "March 14"
        date_searches.append((d, f"{d.month}/{d.day}/"))        # "3/14/" — trailing slash prevents "3/1" matching "3/14"
        date_searches.append((d, f"{d.month:02d}/{d.day:02d}")) # "03/14"

    home_games = []
    failed_sources = []
    for team in COLLEGE_SCHEDULES:
        try:
            resp = requests.get(
                team["url"], timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            if resp.status_code != 200:
                print(f"  {team['name']} {team['sport']} schedule: HTTP {resp.status_code}")
                continue

            # Strip HTML tags, decode named entities (e.g. &amp; &nbsp;), collapse whitespace
            text = re.sub(r'<[^>]+>', ' ', resp.text)
            text = re.sub(r'&[a-zA-Z]+;', ' ', text)
            text = re.sub(r'\s+', ' ', text)

            # Track matched dates so multiple format variants don't produce duplicate entries
            found_dates = set()
            for day, date_str in date_searches:
                if day.date() in found_dates:
                    continue
                idx = text.find(date_str)
                if idx == -1:
                    continue
                # Grab a wide window so Claude can read opponent, time, and home/away context
                context = text[max(0, idx - 120): idx + 400].strip()
                # "vs" = home game; "at " or "@ " before opponent = away — skip away games
                if re.search(r'\bvs\.?\s', context, re.IGNORECASE):
                    found_dates.add(day.date())
                    home_games.append({
                        "team":    team["name"],
                        "sport":   team["sport"],
                        "date":    day.strftime("%A, %B %d"),
                        "context": context,
                    })
        except Exception as e:
            print(f"  {team['name']} {team['sport']} fetch failed: {e}")
            failed_sources.append(f"{team['name']} {team['sport']} schedule")

    return home_games, failed_sources


def fetch_city2_home_games(weekend_saturday):
    """Fetch St. Louis City2 (MLS NEXT Pro) home games for the target weekend.

    Why Fox Sports instead of ESPN or the official mlsnextpro.com site:
    - ESPN's API has no coverage of MLS NEXT Pro — City2 exists as a team (ID 21449)
      but has no league association and returns 0 schedule events from any endpoint.
    - mlsnextpro.com and stlcitysc.com/city2/schedule are JavaScript-rendered and
      return no useful data from a plain HTTP GET.
    - Fox Sports server-renders its team schedule pages as plain HTML, making it
      the only reliable, freely scrapable source for City2's schedule.

    Note on times: Fox Sports lists game times in UTC. This function converts them
    to Central time using the CENTRAL timezone (handles CDT/CST automatically).

    Returns a list of dicts: {team, opponent, time, venue}.
    """
    friday = weekend_saturday - timedelta(days=1)
    sunday = weekend_saturday + timedelta(days=1)
    weekend_dates = {friday.date(), weekend_saturday.date(), sunday.date()}

    url = "https://www.foxsports.com/soccer/saint-louis-city-sc-2-team-schedule"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if resp.status_code != 200:
            print(f"  City2 Fox Sports fetch: HTTP {resp.status_code}")
            return [], ["City2 schedule (Fox Sports)"]

        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        text = re.sub(r"\s+", " ", text)

        # Restrict to the UPCOMING GAMES section so we don't accidentally match rows
        # from the PAST GAMES section, which uses a similar M/D format.
        upcoming_idx = text.find("UPCOMING GAMES")
        if upcoming_idx < 0:
            print("  City2 Fox Sports: UPCOMING GAMES section not found")
            return [], []
        text = text[upcoming_idx:]

        # Fox Sports renders each upcoming game as a single line:
        # "M/D Opponent Name (H|A) record HH:MMam/pm Venue Name, City, ST"
        # We capture everything from the time to the next M/D entry as the venue,
        # which handles venues with commas like "Energizer Park, St. Louis, MO".
        # NOTE: do NOT add re.IGNORECASE here — the [AP]M in the time pattern must
        # match uppercase only to avoid ambiguity with lowercase text elsewhere.
        pattern = re.compile(
            r"(\d{1,2}/\d{1,2})\s+"        # date: M/D
            r"(.+?)\s+"                      # opponent name (non-greedy)
            r"\((H|A)\)\s+"                  # home/away flag
            r"[\d\-]+\s+"                    # record (e.g. 1-0-2)
            r"(\d{1,2}:\d{2}[AP]M)\s+"      # time in UTC (uppercase AM/PM)
            r"(.*?)\s*(?=\d{1,2}/\d{1,2}|$)"  # venue: everything until next M/D or end
        )

        home_games = []
        for m in pattern.finditer(text):
            date_str, opponent, home_away, time_utc_str, venue = m.groups()
            if home_away.upper() != "H":
                continue

            # Parse M/D into a date using the target year
            month, day = map(int, date_str.split("/"))
            year = weekend_saturday.year
            try:
                game_date = datetime(year, month, day).date()
            except ValueError:
                continue

            if game_date not in weekend_dates:
                continue

            # Convert UTC time to Central. Fox Sports displays UTC as "6:00PM" (no space).
            # Parse hour/minute, apply 12-hour AM/PM correction, then localize.
            try:
                hour, minute = divmod(int(time_utc_str[:-2].replace(":", "")), 100)
                if "PM" in time_utc_str.upper() and hour != 12:
                    hour += 12
                elif "AM" in time_utc_str.upper() and hour == 12:
                    hour = 0
                game_utc = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
                game_local = game_utc.astimezone(CENTRAL)
                time_str = game_local.strftime("%A, %B %d at %I:%M %p CT").replace(" 0", " ")
            except Exception:
                time_str = f"{game_date.strftime('%A, %B %d')} at {time_utc_str} UTC"

            home_games.append({
                "team":     "St. Louis City2",
                "opponent": opponent.strip(),
                "time":     time_str,
                "venue":    venue.strip(),
            })

        return home_games, []
    except Exception as e:
        print(f"  City2 Fox Sports fetch failed: {e}")
        return [], ["City2 schedule (Fox Sports)"]


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def get_gcal_service():
    """Authenticate with Google and return a Calendar API service object.
    On first run this opens a browser for OAuth approval and saves token.json.
    Subsequent runs use the saved token, refreshing it automatically if expired.

    If the token expires and can't be refreshed (invalid_grant error), the script
    will silently skip calendar data rather than crash — but the fix is to delete
    token.json and run the script interactively once to re-authenticate.
    """
    creds = None
    if os.path.exists(GCAL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GCAL_TOKEN_FILE, GCAL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh token revoked or expired — fall through to browser re-auth
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(GCAL_CREDENTIALS_FILE, GCAL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GCAL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_calendar_events(weekend_saturday):
    """Fetch events from all family calendars covering Friday evening through Sunday.
    Returns a flat list of events with calendar name, title, start time, and location.
    Individual calendar failures are caught so one bad calendar doesn't break the rest."""
    try:
        service = get_gcal_service()
        friday = weekend_saturday - timedelta(days=1)
        sunday = weekend_saturday + timedelta(days=1)
        # Start at 5pm Central on Friday; convert to UTC so the API gets the right window
        time_min = datetime(friday.year, friday.month, friday.day, 17, 0, 0, tzinfo=CENTRAL).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59, tzinfo=CENTRAL).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        all_events = []
        for cal_name, cal_id in GCAL_CALENDARS.items():
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    timeZone="America/Chicago",
                    singleEvents=True,   # expand recurring events into individual instances
                    orderBy="startTime",
                ).execute()
                for event in result.get("items", []):
                    # dateTime is present for timed events; date is present for all-day events
                    start = event["start"].get("dateTime", event["start"].get("date", ""))
                    all_events.append({
                        "calendar": cal_name,
                        "summary":  event.get("summary", "(No title)"),
                        "start":    start,
                        "location": event.get("location", ""),
                    })
            except Exception as e:
                print(f"  Could not fetch {cal_name} calendar: {e}")

        return all_events, []
    except Exception as e:
        print(f"  Google Calendar fetch failed: {e}")
        return [], ["Google Calendar"]


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

def fetch_weather(profile):
    """Fetch a 3-day forecast from Open-Meteo (free, no API key required).
    Lat/lon are stored in family_profile.json to avoid a geocoding round-trip."""
    try:
        lat = profile["location"]["latitude"]
        lon = profile["location"]["longitude"]

        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "temperature_unit": "fahrenheit",
                "timezone": "America/Chicago",
                "forecast_days": 3,
            },
            timeout=10
        ).json()

        # WMO weather interpretation codes (subset covering common conditions)
        wmo = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle",
            55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
            71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Rain showers",
            81: "Heavy showers", 82: "Violent showers", 95: "Thunderstorm",
        }

        daily = resp["daily"]
        days = []
        for i, date_str in enumerate(daily["time"]):
            code = daily["weathercode"][i]
            days.append({
                "date":        datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d"),
                "description": wmo.get(code, f"Code {code}"),
                "max_f":       round(daily["temperature_2m_max"][i]),
                "min_f":       round(daily["temperature_2m_min"][i]),
                "rain_chance": daily["precipitation_probability_max"][i],
            })
        return days, []
    except Exception as e:
        print(f"  Weather fetch failed: {e}")
        return [], ["Weekend weather forecast"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def gather_results(profile, firecrawl_key="", tavily_key=""):
    """Fetch sports schedules and search for the upcoming weekend.

    Sports data:
    - Pro teams (Blues, Cardinals, City SC) via ESPN scoreboard API
    - City2 (MLS NEXT Pro) via Fox Sports HTML scrape (ESPN has no MLS NEXT Pro data)
    - College teams (Lindenwood, WashU, SLU) via direct SIDEARM Sports page fetch

    Search data:
    - Special events at favorite venues: Firecrawl (full page content) if key is set, else DDG
    - Nearby neighborhood events, Friday evening, general, outdoor: DuckDuckGo

    Returns (sections, weekend_date_str, weekend_date_range, weekend_saturday_datetime, failed_sources).
    """
    today = datetime.now()
    # weekday() returns 0=Mon ... 5=Sat; `or 7` ensures we skip to next Saturday if today is Saturday
    days_until_saturday = (5 - today.weekday()) % 7 or 7
    weekend_saturday = today + timedelta(days=days_until_saturday)
    weekend_friday = weekend_saturday - timedelta(days=1)
    weekend_sunday = weekend_saturday + timedelta(days=1)
    weekend_date = weekend_saturday.strftime("%B %d")
    weekend_date_range = f"{weekend_friday.strftime('%B %d')}-{weekend_sunday.strftime('%d')}"  # e.g. "March 13-15"

    sections = []

    # --- Sports ---
    espn_games,    espn_failed    = fetch_espn_home_games(weekend_saturday)
    college_games, college_failed = fetch_college_home_games(weekend_saturday)
    city2_games,   city2_failed   = fetch_city2_home_games(weekend_saturday)
    failed_sources = espn_failed + college_failed + city2_failed

    # Quoted date strings for high-precision DuckDuckGo search matching
    sat_date  = weekend_saturday.strftime("%B %d %Y") # e.g. "March 14 2026"
    fri_date  = weekend_friday.strftime("%B %d %Y")   # e.g. "March 13 2026"
    sun_date  = weekend_sunday.strftime("%B %d %Y")   # e.g. "March 15 2026"

    sections.append({
        "title":          "Home Games This Weekend",
        "type":           "espn_plus_college",
        "espn_games":     espn_games,
        "college_games":  college_games,
        "city2_games":    city2_games,
    })

    # --- Favorite venues ---
    # Zoo and Botanical Garden are scraped directly — search queries land on wrong pages.
    # All others use Firecrawl search with DDG fallback.
    venues = [
        ("Magic House",              f'"Magic House" St. Louis special event program "{sat_date}"'),
        ("St. Louis Science Center", f'"Science Center" St. Louis special event program "{sat_date}"'),
        ("St. Louis Aquarium",       f'"St. Louis Aquarium" special event program "{sat_date}"'),
        ("Forest Park",              f'"Forest Park" St. Louis event festival "{sat_date}"'),
        ("Made for Kids",            f'"Made for Kids" St. Louis event "{sat_date}"'),
        ("City Museum",              f'"City Museum" St. Louis special event "{sat_date}"'),
    ]

    venue_items = []
    for venue, query in venues:
        if firecrawl_key:
            results = firecrawl_search(query, limit=3, api_key=firecrawl_key) or search(query, max_results=5)
        else:
            results = search(query, max_results=5)
        if results:
            venue_items.append({"label": venue, "results": results})

    # Zoo: scrape event page directly (search queries land on education pages)
    if firecrawl_key:
        zoo_result = firecrawl_scrape("https://stlzoo.org/events/", api_key=firecrawl_key)
        if zoo_result:
            venue_items.append({"label": "St. Louis Zoo", "results": [zoo_result]})

    # Botanical Garden: events calendar is JS-rendered; Tavily finds their specific event articles
    if tavily_key:
        mobot_results = tavily_search(
            f'site:missouribotanicalgarden.org events "{sat_date}"',
            max_results=2,
            api_key=tavily_key,
        )
        if mobot_results:
            venue_items.append({"label": "Missouri Botanical Garden", "results": mobot_results})

    sections.append({"title": "Special Events at Favorite Venues", "type": "labeled", "items": venue_items})

    # --- Nearby neighborhood events ---
    # Scrape explorestlouis.com and stlparent.com directly; fall back to DDG without Firecrawl.
    if firecrawl_key:
        explore_stl = firecrawl_search(
            f'explorestlouis.com/events St. Louis things to do "{sat_date}"',
            limit=2,
            api_key=firecrawl_key,
        )
        # STL Parent events page is JS-rendered; search finds their actual event articles instead
        if tavily_key:
            stlparent = tavily_search(
                f'site:stlparent.com events activities "{sat_date}"',
                max_results=2,
                api_key=tavily_key,
            )
            explore_stl = explore_stl + stlparent
    else:
        explore_stl = search(
            f'St. Louis events things to do "{sat_date}" explorestlouis OR stlcalendar OR stlparent',
            max_results=4,
        )
    def _search(query, max_results):
        if tavily_key:
            return tavily_search(query, max_results=max_results, api_key=tavily_key) or search(query, max_results)
        return search(query, max_results)

    nearby_hoods = _search(
        f'St. Louis weekend events "{sat_date}" Soulard "Lafayette Square" Cherokee "Tower Grove" "South Grand"',
        max_results=4,
    )
    # Deduplicate by base URL (strip query params) — explorestlouis returns same page with different filters
    seen_urls = set()
    neighborhood_items = []
    for item in explore_stl + nearby_hoods:
        base_url = item.get("href", "").split("?")[0]
        if base_url and base_url not in seen_urls:
            seen_urls.add(base_url)
            neighborhood_items.append(item)
    sections.append({
        "title": "Nearby Neighborhood Events",
        "type": "list",
        "items": neighborhood_items,
    })

    # --- Friday evening specifically ---
    friday_evening = _search(
        f'St. Louis things to do Friday evening "{fri_date}" family',
        max_results=3,
    )
    sections.append({"title": "Friday Evening Ideas", "type": "list", "items": friday_evening})

    # --- General family weekend activities ---
    general = _search(
        f'St. Louis family activities "{sat_date}" toddler stroller',
        max_results=4,
    )
    sections.append({"title": "Family Weekend Activities", "type": "list", "items": general})

    # --- Outdoor activities ---
    outdoor = _search(
        f'St. Louis outdoor activities families park "{sat_date}"',
        max_results=4,
    )
    sections.append({"title": "Outdoor Ideas", "type": "list", "items": outdoor})

    return sections, weekend_date, weekend_date_range, weekend_saturday, failed_sources


# ---------------------------------------------------------------------------
# LLM Evaluation & Email Generation
# ---------------------------------------------------------------------------

def evaluate_and_generate_email(sections, weather_data, calendar_events, profile, weekend_date, weekend_date_range, anthropic_key, failed_sources=None, debug=False):
    """Build a prompt from weather, calendar, and search data, send it to Claude,
    and return the HTML email body."""

    # Build weather block for the prompt
    weather_text = ""
    if weather_data:
        weather_text = "## Weekend Weather Forecast\n"
        for day in weather_data:
            weather_text += (
                f"- {day['date']}: {day['description']}, "
                f"High {day['max_f']}°F / Low {day['min_f']}°F, "
                f"{day['rain_chance']}% chance of rain\n"
            )

    # Build calendar block — format timed events as "Day HH:MM AM/PM", all-day as just "Day"
    calendar_text = ""
    if calendar_events:
        calendar_text = "## Already on the Family Calendar\nNote: [BenchApp] events are Mac's beer league hockey games.\n"
        for e in calendar_events:
            start_str = e["start"]
            try:
                dt = datetime.fromisoformat(start_str)
                # Google Calendar uses ISO datetime with "T" for timed events, plain date for all-day
                start_str = dt.strftime("%A %I:%M %p") if "T" in e["start"] else dt.strftime("%A")
            except Exception:
                pass
            loc = f" @ {e['location']}" if e["location"] else ""
            calendar_text += f"- [{e['calendar']}] {e['summary']} — {start_str}{loc}\n"

    # Build search results block, preserving section structure for Claude
    search_text = ""
    for section in sections:
        search_text += f"\n## {section['title']}\n"
        if section["type"] == "espn_plus_college":
            # Pro team home games — confirmed via ESPN scoreboard API
            if section["espn_games"]:
                search_text += "\n### Pro Home Games (confirmed via ESPN)\n"
                for g in section["espn_games"]:
                    venue_str = f" at {g['venue']}" if g["venue"] else ""
                    search_text += f"- {g['team']} vs {g['opponent']} — {g['time']}{venue_str}\n"
            else:
                search_text += "\n### Pro Home Games (confirmed via ESPN)\nNone this weekend.\n"
            # College home games — fetched directly from official athletics schedule pages
            if section["college_games"]:
                search_text += "\n### College Home Games (from official athletics sites)\n"
                for g in section["college_games"]:
                    search_text += f"\n#### {g['team']} ({g['sport']}) — {g['date']}\n"
                    search_text += f"{g['context']}\n"
            else:
                search_text += "\n### College Home Games (from official athletics sites)\nNone found this weekend.\n"
            # City2 home games — confirmed via Fox Sports HTML scrape
            if section.get("city2_games"):
                if section["city2_games"]:
                    search_text += "\n### City2 Home Games (MLS NEXT Pro, confirmed via Fox Sports)\n"
                    for g in section["city2_games"]:
                        search_text += f"- {g['team']} vs {g['opponent']} — {g['time']} at {g['venue']}\n"
        elif section["type"] == "labeled":
            # Labeled sections (venues) — allow up to 1500 chars to capture Firecrawl's richer content
            for item in section["items"]:
                search_text += f"\n### {item['label']}\n"
                for r in item["results"]:
                    search_text += f"Title: {r['title']}\n"
                    search_text += f"Body: {r['body'][:1500]}\n"
                    if r.get("href"):
                        search_text += f"URL: {r['href']}\n"
                    search_text += "\n"
        else:
            # List sections (neighborhoods, general) are a flat set of results
            for r in section["items"]:
                search_text += f"Title: {r['title']}\n"
                search_text += f"Body: {r['body'][:400]}\n"
                if r.get("href"):
                    search_text += f"URL: {r['href']}\n"
                search_text += "\n"

    # Summarize kids for the prompt (age in years, or weeks for infants under 2)
    kids_info = ", ".join(
        f"{m['name']} ({'age ' + str(m['age']) if 'age' in m else str(m.get('age_weeks', '?')) + ' weeks old'})"
        for m in profile["family_members"] if m["role"] in ("Child", "Infant")
    )

    # Add stroller/infant note dynamically if any family member is still in the infant stage.
    # Uses age_weeks (computed at load time) rather than the static role field.
    # Takes only the first infant found — edge case of multiple infants is not expected.
    infants = [m for m in profile["family_members"] if "age_weeks" in m]
    infant_note = (
        f"Family has a {infants[0]['age_weeks']}-week-old infant — activities should be stroller-friendly and accommodating for nursing/infant care"
        if infants else ""
    )

    failed_sources_text = ""
    if failed_sources:
        failed_sources_text = (
            "## Data Retrieval Errors\n"
            "The following sources could not be reached and their data is missing from this email:\n"
            + "".join(f"- {s}\n" for s in failed_sources)
        )

    prompt = f"""You are creating a weekend activity planning email for a family in {profile['location']['city']}, {profile['location']['state']}.

Today is {datetime.now().strftime('%A, %B %d, %Y')}. The upcoming weekend is {weekend_date_range} (Friday evening through Sunday).
Family kids: {kids_info}.
{('Important note: ' + infant_note) if infant_note else ''}

{weather_text}
{calendar_text}
{failed_sources_text}
Below are raw internet search results. Many may be generic or not specifically about this weekend.

{search_text}

Your task:
1. Start with a "Top 3 Picks" section — one standout recommendation per day (Friday evening, Saturday, Sunday), max 2 sentences each. Choose the single most compelling option for each day based on the data. This section goes first in both plain text and HTML.
2. After the Top 3, open with a brief acknowledgment of what's already on the family calendar so they have context — weave it into the narrative rather than just listing events.
3. Evaluate each search result for genuine relevance to THIS specific upcoming weekend.
4. All home games listed under "Pro Home Games" and "College Home Games" are sourced directly from official schedules — include all of them.
5. For favorite venues, the family already knows these places well. Focus only on what's new or special: rotating exhibits, ticketed events, seasonal programs, or anything out of the ordinary. Skip generic descriptions of the venue itself.
6. Highlight neighborhood events and family activities that are stroller-accessible.
7. Suggest what to dress for based on the weather forecast.
8. Write in a warm, friendly, practical tone addressed to the family.
9. Do NOT include a suggested itinerary or sample plan at the end — just present the options and let the family decide.
10. If there are any entries under "Data Retrieval Errors" above, include a brief note at the bottom of the email listing those sources as unavailable so the family knows the information may be incomplete.

Respond with only the HTML email — nothing before <EMAIL_HTML> and nothing after </EMAIL_HTML>:

<EMAIL_HTML>
[A complete, self-contained HTML email. Use inline styles only. Max-width 680px, font-family Arial. Use #1a73e8 as the accent color. Structure: (1) blue header banner, (2) a "Top 3 Picks" highlight box with a light blue (#e8f0fe) background showing one bold pick per day in 2 sentences, (3) Already Planned (calendar events), (4) Weather, (5) Home Games (only if confirmed), (6) Special Events at Venues, (7) Neighborhood Events, (8) Weekend Activity Ideas. Each item in sections 3–8 as a styled card with left blue border. Omit any section with no relevant content. If there are data retrieval errors, include them at the bottom as a small muted gray note (font-size 12px, color #888). Include a small footer with the generation date.]
</EMAIL_HTML>"""

    if debug:
        debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_prompt.txt")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        print(f"  [DEBUG] Prompt written to {debug_path}")
        print(f"  [DEBUG] Skipping Claude call and email send.")
        return None, None

    print("  Calling Claude to evaluate results and generate email...")
    client = anthropic.Anthropic(api_key=anthropic_key)
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        full_response = stream.get_final_message().content[0].text

    # Parse the HTML section out of Claude's response
    html_match = re.search(r'<EMAIL_HTML>(.*?)</EMAIL_HTML>', full_response, re.DOTALL)
    html = html_match.group(1).strip() if html_match else f"<html><body><pre>{full_response}</pre></body></html>"

    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    debug = "--debug" in sys.argv

    print("Loading family profile...")
    profile = load_profile()

    print("Fetching weekend weather...")
    weather_data, weather_failed = fetch_weather(profile)

    firecrawl_key = get_firecrawl_key()
    if firecrawl_key:
        print("Firecrawl key found — venue searches will use full page scraping.")
    else:
        print("No Firecrawl key found — venue searches will use DuckDuckGo.")

    tavily_key = get_tavily_key()
    if tavily_key:
        print("Tavily key found — general searches will use Tavily.")
    else:
        print("No Tavily key found — general searches will use DuckDuckGo.")

    print("Searching for weekend activities and games...")
    sections, weekend_date, weekend_date_range, weekend_saturday, sports_failed = gather_results(
        profile, firecrawl_key=firecrawl_key, tavily_key=tavily_key
    )

    print("Fetching family calendar events...")
    calendar_events, calendar_failed = fetch_calendar_events(weekend_saturday)

    all_failed = weather_failed + sports_failed + calendar_failed
    # Abort if too many data sources failed — no point spending tokens on an empty email.
    # Threshold of 5 catches widespread network outages (today's run had 14 failures)
    # while allowing for a few isolated timeouts on a normal run.
    if not debug and len(all_failed) >= 5:
        print(f"Aborting: {len(all_failed)} data sources failed — skipping Claude and email to avoid wasting tokens.")
        print(f"  Failed: {', '.join(all_failed)}")
        print("Re-run the script once the network is available.")
        return

    if debug:
        print("Evaluating results [DEBUG MODE — skipping Claude and email]...")
        evaluate_and_generate_email(
            sections, weather_data, calendar_events, profile, weekend_date, weekend_date_range,
            anthropic_key=None, failed_sources=all_failed, debug=True
        )
        return

    print("Evaluating results with Claude and generating email...")
    html = evaluate_and_generate_email(
        sections, weather_data, calendar_events, profile, weekend_date, weekend_date_range,
        get_anthropic_key(), failed_sources=all_failed
    )

    # Save a local copy of the HTML email for archiving and troubleshooting
    archive_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails")
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"weekend_{datetime.now().strftime('%Y-%m-%d')}.html")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Email saved to {archive_path}")

    print("Sending email via AgentMail...")
    client = AgentMail(api_key=get_agentmail_key())
    client.inboxes.messages.send(
        inbox_id=SENDER_INBOX,
        to=RECIPIENTS,
        subject=f"Weekend Plans — {weekend_date}",
        html=html,
    )
    print(f"Done! Email sent from {SENDER_INBOX} to {', '.join(RECIPIENTS)}")


if __name__ == "__main__":
    main()
