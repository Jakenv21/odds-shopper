"""
Odds Shopper — Flask web app
Data source: ActionNetwork public scoreboard API (free, no key required)
Books: FanDuel, DraftKings, BetMGM, Caesars, bet365, ESPN Bet, BetRivers
"""

import os
import time
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, abort
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

BASE_URL = "https://api.actionnetwork.com/web/v2/scoreboard"

# ActionNetwork internal book IDs → display names
BOOKS = {
    15:  "FanDuel",
    30:  "DraftKings",
    49:  "BetMGM",
    68:  "Caesars",
    69:  "bet365",
    71:  "theScore",
    75:  "BetRivers",
}

# Sports to load
SPORTS = {
    "nfl":   "NFL",
    "ncaaf": "NCAAF",
    "nba":   "NBA",
    "ncaab": "NCAAB",
    "mlb":   "MLB",
    "nhl":   "NHL",
}

CACHE_TTL = 300   # 5 minutes — covers both games list and odds
_CACHE: dict = {}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.actionnetwork.com/",
    "Accept":     "application/json",
})


# ── Cache ────────────────────────────────────────────────────────────────────

def cache_get(key):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[1]) < CACHE_TTL:
        return entry[0]
    return None


def cache_set(key, data):
    _CACHE[key] = (data, time.time())


# ── ActionNetwork fetch ───────────────────────────────────────────────────────

def fetch_sport(sport_slug: str) -> list:
    """Fetch all games + embedded odds for one sport from ActionNetwork."""
    cached = cache_get(sport_slug)
    if cached is not None:
        return cached

    url = f"{BASE_URL}/{sport_slug}"
    resp = SESSION.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    games = data.get("games") or []
    result = []

    now_ts = time.time()

    for g in games:
        status = g.get("status", "")
        if status in ("final", "cancelled", "postponed"):
            continue

        # Parse start time
        start_str = g.get("start_time") or ""
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            # Skip games that started more than 4 hours ago (likely finished)
            if (now_ts - start_dt.timestamp()) > 14400:
                continue
        except Exception:
            pass

        teams = {t["id"]: t for t in (g.get("teams") or [])}
        away_id = g.get("away_team_id")
        home_id = g.get("home_team_id")
        away_team = teams.get(away_id, {}).get("full_name", "Away")
        home_team = teams.get(home_id, {}).get("full_name", "Home")

        result.append({
            "id":       g["id"],
            "sport":    SPORTS.get(sport_slug, sport_slug.upper()),
            "away":     away_team,
            "home":     home_team,
            "date":     start_str,
            "status":   status,
            "markets":  g.get("markets") or {},
            "away_id":  away_id,
            "home_id":  home_id,
        })

    cache_set(sport_slug, result)
    return result


def all_games() -> list:
    """Load all sports and merge into one sorted list."""
    merged = []
    for slug in SPORTS:
        try:
            merged.extend(fetch_sport(slug))
        except Exception as ex:
            app.logger.warning("Could not load %s: %s", slug, ex)
    merged.sort(key=lambda g: g["date"])
    return merged


# ── Odds parsing ──────────────────────────────────────────────────────────────

def parse_odds(game: dict) -> dict:
    """
    Extract moneylines, spreads, and totals per book.
    Also computes best_bets — instant answer to 'where do I get the best number?'
    """
    markets = game.get("markets") or {}
    away    = game["away"]
    home    = game["home"]

    spreads:   dict = {}
    totals:    dict = {}
    moneylines: dict = {}

    def fmt(val: int | None) -> str | None:
        if val is None:
            return None
        return f"+{val}" if val > 0 else str(val)

    def add_spread(book_name, side, value, odds):
        if book_name not in spreads:
            spreads[book_name] = {"away_pt": None, "away_px": None,
                                  "home_pt": None, "home_px": None}
        if side in ("away", "road"):
            spreads[book_name]["away_pt"] = value
            spreads[book_name]["away_px"] = fmt(odds)
        elif side == "home":
            spreads[book_name]["home_pt"] = value
            spreads[book_name]["home_px"] = fmt(odds)

    def add_total(book_name, side, value, odds):
        if book_name not in totals:
            totals[book_name] = {"total": None, "over_px": None, "under_px": None}
        totals[book_name]["total"] = value
        if side == "over":
            totals[book_name]["over_px"] = fmt(odds)
        elif side == "under":
            totals[book_name]["under_px"] = fmt(odds)

    def add_ml(book_name, side, odds):
        if book_name not in moneylines:
            moneylines[book_name] = {"away_px": None, "home_px": None,
                                     "away_odds": None, "home_odds": None}
        if side in ("away", "road"):
            moneylines[book_name]["away_px"]   = fmt(odds)
            moneylines[book_name]["away_odds"] = odds
        elif side == "home":
            moneylines[book_name]["home_px"]   = fmt(odds)
            moneylines[book_name]["home_odds"] = odds

    # Format A: markets keyed by book_id string
    if markets and isinstance(next(iter(markets.values()), None), dict):
        for book_id_str, book_data in markets.items():
            try:
                bid = int(book_id_str)
            except ValueError:
                continue
            if bid not in BOOKS:
                continue
            book_name = BOOKS[bid]
            event = book_data.get("event") or book_data

            for outcome in (event.get("spread") or []):
                add_spread(book_name, outcome.get("side",""),
                           outcome.get("value"), outcome.get("odds"))
            for outcome in (event.get("total") or []):
                add_total(book_name, outcome.get("side",""),
                          outcome.get("value"), outcome.get("odds"))
            for outcome in (event.get("moneyline") or []):
                add_ml(book_name, outcome.get("side",""), outcome.get("odds"))

    # Format B: flat list
    elif isinstance(markets, list):
        for outcome in markets:
            bid = outcome.get("book_id")
            if bid not in BOOKS:
                continue
            book_name = BOOKS[bid]
            mtype  = outcome.get("type", "")
            side   = outcome.get("side", "")
            value  = outcome.get("value")
            odds   = outcome.get("odds")
            period = outcome.get("period", "event")
            if period not in ("event", "game", "full"):
                continue
            if mtype == "spread":
                add_spread(book_name, side, value, odds)
            elif mtype == "total":
                add_total(book_name, side, value, odds)
            elif mtype == "moneyline":
                add_ml(book_name, side, odds)

    # Clean up incomplete entries
    spreads    = {b: v for b, v in spreads.items()
                  if v["away_pt"] is not None and v["home_pt"] is not None}
    totals     = {b: v for b, v in totals.items() if v["total"] is not None}
    moneylines = {b: v for b, v in moneylines.items()
                  if v["away_odds"] is not None or v["home_odds"] is not None}

    # ── Build best_bets: instant answers ─────────────────────────────────────
    best_bets = []

    # Best moneyline per team
    for team_key, side_label, odds_key in [
        (away, f"{away} ML", "away_odds"),
        (home, f"{home} ML", "home_odds"),
    ]:
        entries = [(b, v[odds_key]) for b, v in moneylines.items()
                   if v[odds_key] is not None]
        if not entries:
            continue
        # Best = highest value (most positive for dog, least negative for fave)
        entries.sort(key=lambda x: -x[1])
        best_book, best_odds = entries[0]
        others = [{"book": b, "odds": fmt(o)} for b, o in entries[1:]]
        best_bets.append({
            "type":  "ml",
            "label": side_label,
            "book":  best_book,
            "odds":  fmt(best_odds),
            "raw":   best_odds,
            "others": others,
        })

    # Best spread per team (best point first, then best juice if tied)
    for side_label, pt_key, px_key in [
        (f"{away} Spread", "away_pt", "away_px"),
        (f"{home} Spread", "home_pt", "home_px"),
    ]:
        entries = [(b, v[pt_key], v[px_key]) for b, v in spreads.items()
                   if v[pt_key] is not None]
        if not entries:
            continue
        entries.sort(key=lambda x: -x[1])  # highest point = best
        best_book, best_pt, best_px = entries[0]
        others = [{"book": b, "odds": f"{pt} ({px})"} for b, pt, px in entries[1:]]
        best_bets.append({
            "type":  "spread",
            "label": side_label,
            "book":  best_book,
            "odds":  f"{'+' if best_pt > 0 else ''}{best_pt} ({best_px})",
            "raw":   best_pt,
            "others": others,
        })

    # Best over and under
    if totals:
        total_entries = [(b, v["total"], v["over_px"], v["under_px"])
                         for b, v in totals.items() if v["total"] is not None]
        if total_entries:
            # Best over = lowest total
            over_sorted = sorted(total_entries, key=lambda x: x[1])
            bo_book, bo_total, bo_px, _ = over_sorted[0]
            others_over = [{"book": b, "odds": f"{t} ({opx})"} for b, t, opx, _ in over_sorted[1:]]
            best_bets.append({
                "type":  "over",
                "label": "Best Over",
                "book":  bo_book,
                "odds":  f"{bo_total} ({bo_px})",
                "raw":   bo_total,
                "others": others_over,
            })
            # Best under = highest total
            under_sorted = sorted(total_entries, key=lambda x: -x[1])
            bu_book, bu_total, _, bu_px = under_sorted[0]
            others_under = [{"book": b, "odds": f"{t} ({upx})"} for b, t, _, upx in under_sorted[1:]]
            best_bets.append({
                "type":  "under",
                "label": "Best Under",
                "book":  bu_book,
                "odds":  f"{bu_total} ({bu_px})",
                "raw":   bu_total,
                "others": others_under,
            })

    return {
        "away":       away,
        "home":       home,
        "moneylines": moneylines,
        "spreads":    spreads,
        "totals":     totals,
        "best_bets":  best_bets,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/games")
def get_games():
    games = all_games()
    # Return lightweight list (no markets payload to browser)
    return jsonify([{
        "id":     g["id"],
        "sport":  g["sport"],
        "away":   g["away"],
        "home":   g["home"],
        "date":   g["date"],
        "status": g["status"],
    } for g in games])


@app.route("/api/odds/<int:game_id>")
def get_odds(game_id: int):
    # Find game in cache
    for slug in SPORTS:
        for g in (cache_get(slug) or []):
            if g["id"] == game_id:
                return jsonify(parse_odds(g))

    # Not in cache — reload all and try again
    for slug in SPORTS:
        games = fetch_sport(slug)
        for g in games:
            if g["id"] == game_id:
                return jsonify(parse_odds(g))

    abort(404)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
