"""
Odds Shopper — Flask web app
Proxies odds-api.io with server-side caching. API key never exposed to browser.
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, abort
from dotenv import load_dotenv

load_dotenv()

app     = Flask(__name__)
API_KEY  = os.getenv("ODDS_API_KEY", "").strip()
BASE_URL = "https://api.odds-api.io/v3"

US_BOOKS = [
    "DraftKings", "FanDuel", "BetMGM", "Caesars",
    "bet365", "Fanatics", "BetRivers", "Hard Rock",
]

SPORT_SLUGS = [
    "american-football",
    "basketball",
    "baseball",
    "ice-hockey",
]

US_LEAGUE_KEYWORDS = [
    "nfl", "ncaaf", "ncaa", "college",
    "nba", "mlb", "nhl",
    "national football", "national basketball",
    "major league", "national hockey",
]

# Simple in-memory cache {key: (data, timestamp)}
_CACHE: dict = {}
EVENTS_TTL = 600   # 10 min
ODDS_TTL   = 300   #  5 min

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "OddsShopper/2.0"})


# ── Cache helpers ────────────────────────────────────────────────────────────

def cache_get(key: str, ttl: int):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[1]) < ttl:
        return entry[0]
    return None


def cache_set(key: str, data):
    _CACHE[key] = (data, time.time())


# ── API call ─────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict):
    params["apiKey"] = API_KEY
    resp = SESSION.get(BASE_URL + path, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_us_major(event: dict) -> bool:
    league = event.get("league") or {}
    name   = (league.get("name") or "").lower()
    slug   = (league.get("slug") or "").lower()
    return any(kw in name or kw in slug for kw in US_LEAGUE_KEYWORDS)


def to_american(decimal_str) -> int | None:
    if decimal_str is None:
        return None
    try:
        d = float(decimal_str)
        if d <= 1.0:
            return None
        return int(round((d - 1) * 100)) if d >= 2.0 else int(round(-100 / (d - 1)))
    except (ValueError, ZeroDivisionError):
        return None


def fmt_american(val: int | None) -> str | None:
    if val is None:
        return None
    return f"+{val}" if val > 0 else str(val)


def parse_odds(raw: dict) -> tuple[dict, dict]:
    """Return (spreads, totals) dicts keyed by book name."""
    spreads: dict = {}
    totals:  dict = {}
    bookmakers = raw.get("bookmakers") or {}

    for book, markets in bookmakers.items():
        if not isinstance(markets, list):
            continue
        for mkt in markets:
            name_lo   = (mkt.get("name") or "").lower()
            odds_list = mkt.get("odds") or []

            is_spread = any(kw in name_lo for kw in ["handicap", "spread", "asian"])
            is_total  = any(kw in name_lo for kw in ["over", "under", "total"])

            for odds in odds_list:
                hdp = odds.get("hdp")
                if hdp is None:
                    continue

                if is_spread and book not in spreads:
                    hp = to_american(odds.get("home"))
                    ap = to_american(odds.get("away"))
                    if hp is not None or ap is not None:
                        spreads[book] = {
                            "home_pt": float(hdp),
                            "away_pt": -float(hdp),
                            "home_px": fmt_american(hp),
                            "away_px": fmt_american(ap),
                        }

                elif is_total and book not in totals:
                    op = to_american(odds.get("over"))
                    up = to_american(odds.get("under"))
                    if op is not None or up is not None:
                        totals[book] = {
                            "total":    float(hdp),
                            "over_px":  fmt_american(op),
                            "under_px": fmt_american(up),
                        }

    # Fallback: if market names didn't match, scan everything
    if not spreads and not totals:
        for book, markets in bookmakers.items():
            if not isinstance(markets, list):
                continue
            for mkt in markets:
                for odds in (mkt.get("odds") or []):
                    hdp = odds.get("hdp")
                    if hdp is None:
                        continue
                    op = to_american(odds.get("over"))
                    up = to_american(odds.get("under"))
                    hp = to_american(odds.get("home"))
                    ap = to_american(odds.get("away"))
                    if (op or up) and book not in totals:
                        totals[book] = {
                            "total":    float(hdp),
                            "over_px":  fmt_american(op),
                            "under_px": fmt_american(up),
                        }
                    elif (hp or ap) and book not in spreads:
                        spreads[book] = {
                            "home_pt": float(hdp),
                            "away_pt": -float(hdp),
                            "home_px": fmt_american(hp),
                            "away_px": fmt_american(ap),
                        }

    return spreads, totals


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/games")
def get_games():
    cached = cache_get("games", EVENTS_TTL)
    if cached:
        return jsonify(cached)

    now      = datetime.now(timezone.utc)
    week_out = now + timedelta(days=8)
    games    = []

    for slug in SPORT_SLUGS:
        try:
            events = api_get("/events", {
                "sport":  slug,
                "status": "pending,live",
                "from":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":     week_out.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit":  200,
            })
            for e in events:
                if not is_us_major(e):
                    continue
                league = e.get("league") or {}
                games.append({
                    "id":     e["id"],
                    "home":   e.get("home", ""),
                    "away":   e.get("away", ""),
                    "league": league.get("name", slug),
                    "date":   e.get("date", ""),
                    "status": e.get("status", "pending"),
                })
        except Exception as ex:
            app.logger.warning("Could not load %s: %s", slug, ex)

    games.sort(key=lambda g: g["date"])
    cache_set("games", games)
    return jsonify(games)


@app.route("/api/odds/<int:event_id>")
def get_odds(event_id: int):
    cache_key = f"odds_{event_id}"
    cached = cache_get(cache_key, ODDS_TTL)
    if cached:
        return jsonify(cached)

    try:
        raw = api_get("/odds", {
            "eventId":    event_id,
            "bookmakers": ",".join(US_BOOKS),
        })
    except requests.HTTPError as ex:
        abort(ex.response.status_code)

    spreads, totals = parse_odds(raw)
    result = {
        "home":    raw.get("home", ""),
        "away":    raw.get("away", ""),
        "spreads": spreads,
        "totals":  totals,
    }
    cache_set(cache_key, result)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
