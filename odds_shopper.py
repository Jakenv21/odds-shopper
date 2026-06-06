#!/usr/bin/env python3
"""
Odds Shopper v2 — powered by odds-api.io
Free tier: 100 req/hour, no monthly cap, email signup only.
Books: DraftKings, FanDuel, BetMGM, Caesars, bet365, Fanatics, BetRivers, Hard Rock

Usage:
  python odds_shopper.py                  # interactive mode
  python odds_shopper.py "Alabama"        # one-shot search
  python odds_shopper.py --list           # list all live/upcoming games
  python odds_shopper.py --list-all       # list including non-US leagues
  python odds_shopper.py --debug "Chiefs" # show raw API response
"""

import os
import sys
import json
import time
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from difflib import SequenceMatcher
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.getenv("ODDS_API_KEY", "").strip()
BASE_URL = "https://api.odds-api.io/v3"

# Local disk cache — avoids redundant calls within the window
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
EVENTS_CACHE_MINUTES = 10
ODDS_CACHE_MINUTES   = 5

# US books to show (exact names from the /bookmakers endpoint)
US_BOOKS = [
    "DraftKings",
    "FanDuel",
    "BetMGM",
    "Caesars",
    "bet365",
    "Fanatics",
    "BetRivers",
    "Hard Rock",
]

# Sport slugs to query
SPORT_SLUGS = [
    "american-football",
    "basketball",
    "baseball",
    "ice-hockey",
]

# Keywords that identify US major league events (checked against league name + slug)
US_LEAGUE_KEYWORDS = [
    "nfl", "ncaaf", "ncaa", "college",
    "nba", "mlb", "nhl",
    "national football", "national basketball",
    "major league", "national hockey",
]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    return CACHE_DIR / (hashlib.md5(key.encode()).hexdigest() + ".json")


def cache_get(key: str, max_age_minutes: int):
    p = _cache_path(key)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > max_age_minutes * 60:
        return None
    return json.loads(p.read_text())


def cache_set(key: str, data):
    _cache_path(key).write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "OddsShopper/2.0"})


def api_get(path: str, params: dict, cache_minutes: int = 0, debug: bool = False):
    params["apiKey"] = API_KEY
    cache_key = path + json.dumps(params, sort_keys=True)

    if cache_minutes:
        cached = cache_get(cache_key, cache_minutes)
        if cached is not None:
            return cached

    url = BASE_URL + path
    resp = SESSION.get(url, params=params, timeout=15)

    if debug:
        print(f"\n[DEBUG] GET {resp.url}")
        print(f"[DEBUG] Status: {resp.status_code}")
        try:
            print(json.dumps(resp.json(), indent=2)[:3000])
        except Exception:
            print(resp.text[:3000])

    resp.raise_for_status()
    data = resp.json()

    if cache_minutes:
        cache_set(cache_key, data)

    return data


# ---------------------------------------------------------------------------
# Odds conversion: decimal → American
# ---------------------------------------------------------------------------

def to_american(decimal_str) -> str:
    if decimal_str is None:
        return "  —  "
    try:
        d = float(decimal_str)
        if d <= 1.0:
            return "  —  "
        if d >= 2.0:
            val = int(round((d - 1) * 100))
            return f"+{val}"
        else:
            val = int(round(-100 / (d - 1)))
            return str(val)
    except (ValueError, ZeroDivisionError):
        return "  —  "


def fmt_spread(point) -> str:
    if point is None:
        return "  —  "
    try:
        p = float(point)
        return f"+{p}" if p > 0 else str(p)
    except (ValueError, TypeError):
        return str(point)


# ---------------------------------------------------------------------------
# Load events
# ---------------------------------------------------------------------------

def load_events(debug: bool = False) -> list:
    all_events = []
    now = datetime.now(timezone.utc)
    week_out = now + timedelta(days=8)

    for slug in SPORT_SLUGS:
        params = {
            "sport":  slug,
            "status": "pending,live",
            "from":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":     week_out.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":  200,
        }
        try:
            events = api_get("/events", params, cache_minutes=EVENTS_CACHE_MINUTES, debug=debug)
            for e in events:
                e["_sport_slug"] = slug
            all_events.extend(events)
        except requests.HTTPError as ex:
            print(f"  Warning: could not load {slug} ({ex})")
        except Exception as ex:
            print(f"  Warning: {slug} error — {ex}")

    return all_events


def is_us_major(event: dict) -> bool:
    league = event.get("league") or {}
    name   = (league.get("name") or "").lower()
    slug   = (league.get("slug") or "").lower()
    return any(kw in name or kw in slug for kw in US_LEAGUE_KEYWORDS)


def sport_label(event: dict) -> str:
    league = event.get("league") or {}
    return (league.get("name") or event.get("_sport_slug") or "").upper()


# ---------------------------------------------------------------------------
# Game search
# ---------------------------------------------------------------------------

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_games(events: list, query: str) -> list:
    q = query.lower().strip()
    scored = []
    for e in events:
        home = e.get("home", "")
        away = e.get("away", "")
        candidates = [
            home, away,
            f"{away} {home}",
            f"{home} {away}",
            *home.split(),
            *away.split(),
        ]
        score = max((similarity(q, c) for c in candidates), default=0)
        if any(q in c.lower() for c in candidates):
            score = max(score, 0.88)
        if score >= 0.55:
            scored.append((score, e))

    scored.sort(key=lambda x: -x[0])
    seen, result = set(), []
    for _, e in scored:
        eid = e.get("id")
        if eid not in seen:
            seen.add(eid)
            result.append(e)
    return result


# ---------------------------------------------------------------------------
# Fetch and parse odds
# ---------------------------------------------------------------------------

def fetch_odds(event_id, debug: bool = False) -> dict:
    params = {
        "eventId":    event_id,
        "bookmakers": ",".join(US_BOOKS),
    }
    return api_get("/odds", params, cache_minutes=ODDS_CACHE_MINUTES, debug=debug)


def parse_odds(data: dict) -> tuple:
    """Returns (spreads, totals) where each is a list of dicts per book."""
    spreads = {}   # book -> {home_pt, home_px, away_pt, away_px}
    totals  = {}   # book -> {total, over_px, under_px}

    bookmakers = data.get("bookmakers") or {}

    for book, markets in bookmakers.items():
        if not isinstance(markets, list):
            continue
        for mkt in markets:
            mkt_name  = (mkt.get("name") or "").lower()
            odds_list = mkt.get("odds") or []

            # --- Spreads: look for hdp field ---
            if any(kw in mkt_name for kw in ["handicap", "spread", "asian"]):
                for odds in odds_list:
                    hdp = odds.get("hdp")
                    if hdp is None:
                        continue
                    home_px = odds.get("home")
                    away_px = odds.get("away")
                    if home_px or away_px:
                        spreads[book] = {
                            "home_pt": float(hdp),
                            "home_px": to_american(home_px),
                            "away_pt": -float(hdp),
                            "away_px": to_american(away_px),
                        }
                        break  # take first entry per book

            # --- Totals: look for over/under fields ---
            elif any(kw in mkt_name for kw in ["over", "under", "total"]):
                for odds in odds_list:
                    over_px  = odds.get("over")
                    under_px = odds.get("under")
                    hdp      = odds.get("hdp")
                    if (over_px or under_px) and hdp is not None:
                        totals[book] = {
                            "total":    float(hdp),
                            "over_px":  to_american(over_px),
                            "under_px": to_american(under_px),
                        }
                        break

    # Fallback: if market names didn't match, scan all markets for hdp + over/under
    if not spreads and not totals:
        for book, markets in bookmakers.items():
            if not isinstance(markets, list):
                continue
            for mkt in markets:
                for odds in (mkt.get("odds") or []):
                    hdp = odds.get("hdp")
                    if hdp is None:
                        continue
                    over_px  = odds.get("over")
                    under_px = odds.get("under")
                    home_px  = odds.get("home")
                    away_px  = odds.get("away")
                    if (over_px or under_px) and book not in totals:
                        totals[book] = {
                            "total": float(hdp),
                            "over_px":  to_american(over_px),
                            "under_px": to_american(under_px),
                        }
                    elif (home_px or away_px) and book not in spreads:
                        spreads[book] = {
                            "home_pt": float(hdp),
                            "home_px": to_american(home_px),
                            "away_pt": -float(hdp),
                            "away_px": to_american(away_px),
                        }

    return spreads, totals


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def fmt_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a %b %d  %I:%M %p")
    except Exception:
        return iso_str


def display_game(event: dict, debug: bool = False):
    home  = event.get("home", "?")
    away  = event.get("away", "?")
    label = sport_label(event)
    gtime = fmt_time(event.get("date", ""))
    eid   = event.get("id")

    print()
    print("=" * 70)
    print(f"  {away}  @  {home}")
    print(f"  {label}  |  {gtime}")
    print("=" * 70)

    try:
        raw = fetch_odds(eid, debug=debug)
    except Exception as ex:
        print(f"\n  Could not load odds: {ex}\n")
        return

    spreads, totals = parse_odds(raw)

    # ---------- SPREADS ----------
    if spreads:
        print(f"\n  POINT SPREADS")
        aw = away[:16]
        hw = home[:16]
        print(f"  {'Book':<22}  {aw:<20}  {hw}")
        print(f"  {'-'*64}")

        rows = sorted(spreads.items(), key=lambda kv: kv[1]["away_pt"], reverse=True)
        away_pts = [v["away_pt"] for v in spreads.values()]
        home_pts = [v["home_pt"] for v in spreads.values()]
        best_away = max(away_pts) if away_pts else None
        best_home = max(home_pts) if home_pts else None

        for book, v in rows:
            a_str = f"{fmt_spread(v['away_pt'])} ({v['away_px']})"
            h_str = f"{fmt_spread(v['home_pt'])} ({v['home_px']})"
            flag  = ""
            if v["away_pt"] == best_away:
                flag = "  <-- BEST AWAY"
            elif v["home_pt"] == best_home:
                flag = "  <-- BEST HOME"
            print(f"  {book:<22}  {a_str:<20}  {h_str}{flag}")

    # ---------- TOTALS ----------
    if totals:
        print(f"\n  TOTALS  (Over / Under)")
        print(f"  {'Book':<22}  {'Total':>7}  {'Over':>8}  {'Under':>8}")
        print(f"  {'-'*54}")

        rows = sorted(totals.items(), key=lambda kv: kv[1]["total"])
        total_pts = [v["total"] for v in totals.values()]
        best_over  = min(total_pts) if total_pts else None
        best_under = max(total_pts) if total_pts else None

        for book, v in rows:
            flag = ""
            if v["total"] == best_over:
                flag = "  <-- BEST OVER"
            elif v["total"] == best_under:
                flag = "  <-- BEST UNDER"
            print(f"  {book:<22}  {fmt_spread(v['total']):>7}  "
                  f"{v['over_px']:>8}  {v['under_px']:>8}{flag}")

    if not spreads and not totals:
        print("\n  No spread or total odds posted yet.")
        if not debug:
            print("  Run with --debug to see raw API response.\n")

    print()


def list_games(events: list, us_only: bool = True):
    filtered = [e for e in events if not us_only or is_us_major(e)]
    if not filtered:
        print("  No games found." + (" Try --list-all to include non-US leagues." if us_only else ""))
        return
    by_league: dict = {}
    for e in filtered:
        label = sport_label(e)
        by_league.setdefault(label, []).append(e)
    for label, games in sorted(by_league.items()):
        print(f"\n  [{label}]")
        for g in games:
            t = fmt_time(g.get("date", ""))
            print(f"    {g['away']}  @  {g['home']}   —   {t}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not API_KEY:
        print("\nERROR: ODDS_API_KEY not set.")
        print("  1. Sign up free (email only) at: https://odds-api.io")
        print("  2. Copy .env.example to .env in this folder")
        print("  3. Paste your key:  ODDS_API_KEY=your_key_here\n")
        sys.exit(1)

    args = sys.argv[1:]
    debug = "--debug" in args
    args  = [a for a in args if a != "--debug"]

    print("\nOdds Shopper — loading games...")
    events = load_events(debug=debug)
    us_events = [e for e in events if is_us_major(e)]
    print(f"  {len(us_events)} US major league games loaded ({len(events)} total).\n")

    # One-shot modes
    if args:
        cmd = args[0].lower()

        if cmd == "--list-all":
            list_games(events, us_only=False)
            return

        if cmd == "--list":
            list_games(us_events, us_only=True)
            return

        query   = " ".join(args)
        results = find_games(us_events, query)
        if not results:
            print(f"  No games found for '{query}'. Try --list to see all games.")
        for g in results[:3]:
            display_game(g, debug=debug)
        return

    # Interactive mode
    print("Type a team name or matchup — I'll find the best line.")
    print("Commands:  list  |  list-all  |  reload  |  quit\n")

    while True:
        try:
            raw = input("Search: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        cmd = raw.lower()

        if cmd in ("quit", "exit", "q"):
            break

        if cmd == "list-all":
            list_games(events, us_only=False)
            continue

        if cmd in ("list", "ls"):
            list_games(us_events, us_only=True)
            continue

        if cmd in ("reload", "refresh"):
            # Clear event caches and reload
            for f in CACHE_DIR.glob("*.json"):
                f.unlink()
            print("Cache cleared — reloading...")
            events    = load_events(debug=debug)
            us_events = [e for e in events if is_us_major(e)]
            print(f"  {len(us_events)} US games loaded.\n")
            continue

        results = find_games(us_events, raw)
        if not results:
            print(f"  No match for '{raw}'. Type 'list' to browse all games.\n")
            continue

        if len(results) == 1:
            display_game(results[0], debug=debug)
        else:
            print(f"\n  Found {len(results)} games:\n")
            for i, g in enumerate(results[:5], 1):
                t = fmt_time(g.get("date", ""))
                print(f"  [{i}] {sport_label(g):<12}  {g['away']}  @  {g['home']}   {t}")
            print()
            try:
                pick = input("  Pick a number (or Enter for all): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if pick.isdigit() and 1 <= int(pick) <= len(results[:5]):
                display_game(results[int(pick) - 1], debug=debug)
            else:
                for g in results[:3]:
                    display_game(g, debug=debug)


if __name__ == "__main__":
    main()
