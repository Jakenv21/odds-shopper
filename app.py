"""
Odds Shopper — Flask web app

Data sources (auto-selected):
  • The Odds API  — real-time, set ODDS_API_KEY in .env or Render env vars
  • ActionNetwork — free fallback (~15-30 min delay), no key needed

Books (The Odds API): FanDuel, DraftKings, BetMGM, BetRivers, Hard Rock, ESPN Bet
Books (ActionNetwork): FanDuel, DraftKings, BetMGM, Caesars, bet365,
                       theScore, BetRivers
"""

import os
import time
import requests
from collections import Counter
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, abort
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
USE_ODDS_API = bool(ODDS_API_KEY)

# ActionNetwork
AN_BASE  = "https://api.actionnetwork.com/web/v2/scoreboard"
AN_BOOKS = {
    15: "FanDuel",
    30: "DraftKings",
    49: "BetMGM",
    68: "Caesars",
    69: "bet365",
    71: "theScore",
    75: "BetRivers",
}
AN_SPORTS = {
    "nfl":   "NFL",
    "ncaaf": "NCAAF",
    "nba":   "NBA",
    "ncaab": "NCAAB",
    "mlb":   "MLB",
    "nhl":   "NHL",
}

# The Odds API
OA_BASE  = "https://api.the-odds-api.com/v4/sports"
OA_BOOKS = {
    "fanduel":     "FanDuel",
    "draftkings":  "DraftKings",
    "betmgm":      "BetMGM",
    "betrivers":   "BetRivers",
    "hardrockbet": "Hard Rock",
    "espnbet":     "ESPN Bet",
}
OA_BOOK_KEYS = ",".join(OA_BOOKS.keys())
OA_SPORTS = {
    "americanfootball_nfl":   "NFL",
    "americanfootball_ncaaf": "NCAAF",
    "basketball_nba":         "NBA",
    "basketball_ncaab":       "NCAAB",
    "baseball_mlb":           "MLB",
    "icehockey_nhl":          "NHL",
}

# Active source config
SPORTS    = OA_SPORTS if USE_ODDS_API else AN_SPORTS
CACHE_TTL = 1800 if USE_ODDS_API else 300   # 30 min real-time / 5 min ActionNetwork

_CACHE: dict = {}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.actionnetwork.com/",
    "Accept":     "application/json",
})


# ── Cache ─────────────────────────────────────────────────────────────────────

def cache_get(key):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[1]) < CACHE_TTL:
        return entry[0]
    return None


def cache_set(key, data):
    _CACHE[key] = (data, time.time())


# ── ActionNetwork fetch ───────────────────────────────────────────────────────

def fetch_sport_an(sport_slug: str) -> list:
    cached = cache_get(sport_slug)
    if cached is not None:
        return cached

    resp = SESSION.get(f"{AN_BASE}/{sport_slug}", timeout=15)
    resp.raise_for_status()
    data  = resp.json()
    games = data.get("games") or []
    result = []
    now_ts = time.time()

    for g in games:
        status = g.get("status", "")
        if status in ("final", "cancelled", "postponed"):
            continue
        start_str = g.get("start_time") or ""
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if (now_ts - dt.timestamp()) > 14400:
                continue
        except Exception:
            pass
        teams   = {t["id"]: t for t in (g.get("teams") or [])}
        away_id = g.get("away_team_id")
        home_id = g.get("home_team_id")
        result.append({
            "id":         str(g["id"]),
            "sport":      AN_SPORTS.get(sport_slug, sport_slug.upper()),
            "away":       teams.get(away_id, {}).get("full_name", "Away"),
            "home":       teams.get(home_id, {}).get("full_name", "Home"),
            "date":       start_str,
            "status":     status,
            "markets":    g.get("markets") or {},
            "bookmakers": None,
        })

    cache_set(sport_slug, result)
    return result


# ── The Odds API fetch ────────────────────────────────────────────────────────

def fetch_sport_oa(sport_slug: str) -> list:
    cached = cache_get(sport_slug)
    if cached is not None:
        return cached

    params = {
        "apiKey":     ODDS_API_KEY,
        "regions":    "us",
        "markets":    "h2h,spreads,totals",
        "oddsFormat": "american",
        "bookmakers": OA_BOOK_KEYS,
    }
    resp = SESSION.get(f"{OA_BASE}/{sport_slug}/odds", params=params, timeout=15)
    resp.raise_for_status()

    data   = resp.json()
    now_ts = time.time()
    result = []

    for game in data:
        commence = game.get("commence_time", "")
        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if (now_ts - dt.timestamp()) > 14400:
                continue
        except Exception:
            pass
        result.append({
            "id":         game["id"],
            "sport":      OA_SPORTS.get(sport_slug, sport_slug.upper()),
            "away":       game["away_team"],
            "home":       game["home_team"],
            "date":       commence,
            "status":     "",
            "markets":    None,
            "bookmakers": game.get("bookmakers", []),
        })

    cache_set(sport_slug, result)
    return result


def fetch_sport(sport_slug: str) -> list:
    return fetch_sport_oa(sport_slug) if USE_ODDS_API else fetch_sport_an(sport_slug)


def all_games() -> list:
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
    markets    = game.get("markets") or {}
    bookmakers = game.get("bookmakers") or []
    away       = game["away"]
    home       = game["home"]

    spreads:    dict = {}
    totals:     dict = {}
    moneylines: dict = {}

    def fmt(val):
        if val is None:
            return None
        return f"+{val}" if val > 0 else str(val)

    def add_spread(bname, side, value, odds):
        if bname not in spreads:
            spreads[bname] = {"away_pt": None, "away_px": None, "away_raw": None,
                              "home_pt": None, "home_px": None, "home_raw": None}
        if side in ("away", "road"):
            spreads[bname]["away_pt"] = value
            spreads[bname]["away_px"] = fmt(odds)
            spreads[bname]["away_raw"] = odds
        elif side == "home":
            spreads[bname]["home_pt"] = value
            spreads[bname]["home_px"] = fmt(odds)
            spreads[bname]["home_raw"] = odds

    def add_total(bname, side, value, odds):
        if bname not in totals:
            totals[bname] = {"total": None, "over_px": None, "under_px": None}
        totals[bname]["total"] = value
        if side == "over":
            totals[bname]["over_px"] = fmt(odds)
        elif side == "under":
            totals[bname]["under_px"] = fmt(odds)

    def add_ml(bname, side, odds):
        if bname not in moneylines:
            moneylines[bname] = {"away_px": None, "home_px": None,
                                 "away_odds": None, "home_odds": None}
        if side in ("away", "road"):
            moneylines[bname]["away_px"]   = fmt(odds)
            moneylines[bname]["away_odds"] = odds
        elif side == "home":
            moneylines[bname]["home_px"]   = fmt(odds)
            moneylines[bname]["home_odds"] = odds

    # ── Format C: The Odds API ────────────────────────────────────────────────
    if bookmakers:
        for bm in bookmakers:
            bkey  = bm.get("key", "")
            bname = OA_BOOKS.get(bkey, bm.get("title", bkey))
            for market in bm.get("markets", []):
                mkey = market["key"]
                for o in market.get("outcomes", []):
                    name  = o["name"]
                    price = o["price"]
                    point = o.get("point")
                    if mkey == "h2h":
                        side = "home" if name == home else "away"
                        add_ml(bname, side, price)
                    elif mkey == "spreads":
                        side = "home" if name == home else "away"
                        add_spread(bname, side, point, price)
                    elif mkey == "totals":
                        add_total(bname, name.lower(), point, price)

    # ── Format A: ActionNetwork (book_id dict) ────────────────────────────────
    elif markets and isinstance(next(iter(markets.values()), None), dict):
        for book_id_str, book_data in markets.items():
            try:
                bid = int(book_id_str)
            except ValueError:
                continue
            if bid not in AN_BOOKS:
                continue
            bname = AN_BOOKS[bid]
            event = book_data.get("event") or book_data
            for o in (event.get("spread") or []):
                add_spread(bname, o.get("side", ""), o.get("value"), o.get("odds"))
            for o in (event.get("total") or []):
                add_total(bname, o.get("side", ""), o.get("value"), o.get("odds"))
            for o in (event.get("moneyline") or []):
                add_ml(bname, o.get("side", ""), o.get("odds"))

    # ── Format B: ActionNetwork (flat list) ───────────────────────────────────
    elif isinstance(markets, list):
        for o in markets:
            bid = o.get("book_id")
            if bid not in AN_BOOKS:
                continue
            bname  = AN_BOOKS[bid]
            mtype  = o.get("type", "")
            side   = o.get("side", "")
            value  = o.get("value")
            odds   = o.get("odds")
            period = o.get("period", "event")
            if period not in ("event", "game", "full"):
                continue
            if mtype == "spread":
                add_spread(bname, side, value, odds)
            elif mtype == "total":
                add_total(bname, side, value, odds)
            elif mtype == "moneyline":
                add_ml(bname, side, odds)

    # ── Clean up incomplete entries ───────────────────────────────────────────
    spreads    = {b: v for b, v in spreads.items()
                  if v["away_pt"] is not None and v["home_pt"] is not None}
    totals     = {b: v for b, v in totals.items() if v["total"] is not None}
    moneylines = {b: v for b, v in moneylines.items()
                  if v["away_odds"] is not None or v["home_odds"] is not None}

    # ── Build best_bets ───────────────────────────────────────────────────────
    best_bets = []

    for side_label, odds_key in [
        (f"{away} ML", "away_odds"),
        (f"{home} ML", "home_odds"),
    ]:
        entries = [(b, v[odds_key]) for b, v in moneylines.items() if v[odds_key] is not None]
        if not entries:
            continue
        entries.sort(key=lambda x: -x[1])
        best_book, best_odds = entries[0]
        others = [{"book": b, "odds": fmt(o)} for b, o in entries[1:]]
        best_bets.append({
            "type": "ml", "label": side_label,
            "book": best_book, "odds": fmt(best_odds),
            "raw": best_odds, "others": others,
        })

    for side_label, pt_key, px_key, raw_key in [
        (f"{away} Spread", "away_pt", "away_px", "away_raw"),
        (f"{home} Spread", "home_pt", "home_px", "home_raw"),
    ]:
        entries = [(b, v[pt_key], v[px_key], v.get(raw_key))
                   for b, v in spreads.items() if v[pt_key] is not None]
        if not entries:
            continue
        # Use consensus line to avoid alternate lines skewing the result,
        # then pick best price at that line.
        consensus_pt = Counter(e[1] for e in entries).most_common(1)[0][0]
        consensus = [(b, pt, px, raw) for b, pt, px, raw in entries if pt == consensus_pt]
        consensus.sort(key=lambda x: -(x[3] if x[3] is not None else -9999))
        best_book, best_pt, best_px, _ = consensus[0]
        others = [{"book": b, "odds": f"{'+' if pt > 0 else ''}{pt} ({px})"}
                  for b, pt, px, _ in consensus[1:]]
        best_bets.append({
            "type": "spread", "label": side_label,
            "book": best_book,
            "odds": f"{'+' if best_pt > 0 else ''}{best_pt} ({best_px})",
            "raw": best_pt, "others": others,
        })

    if totals:
        total_entries = [(b, v["total"], v["over_px"], v["under_px"])
                         for b, v in totals.items() if v["total"] is not None]
        if total_entries:
            over_sorted = sorted(total_entries, key=lambda x: x[1])
            bo_book, bo_total, bo_px, _ = over_sorted[0]
            others_over = [{"book": b, "odds": f"{t} ({opx})"} for b, t, opx, _ in over_sorted[1:]]
            best_bets.append({
                "type": "over", "label": "Best Over",
                "book": bo_book, "odds": f"{bo_total} ({bo_px})",
                "raw": bo_total, "others": others_over,
            })
            under_sorted = sorted(total_entries, key=lambda x: -x[1])
            bu_book, bu_total, _, bu_px = under_sorted[0]
            others_under = [{"book": b, "odds": f"{t} ({upx})"} for b, t, _, upx in under_sorted[1:]]
            best_bets.append({
                "type": "under", "label": "Best Under",
                "book": bu_book, "odds": f"{bu_total} ({bu_px})",
                "raw": bu_total, "others": others_under,
            })

    return {
        "away": away, "home": home,
        "moneylines": moneylines,
        "spreads": spreads,
        "totals": totals,
        "best_bets": best_bets,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info")
def get_info():
    return jsonify({
        "source":    "The Odds API (real-time)" if USE_ODDS_API else "ActionNetwork (~15-30 min delay)",
        "realtime":  USE_ODDS_API,
        "cache_ttl": CACHE_TTL,
    })


@app.route("/api/games")
def get_games():
    games = all_games()
    return jsonify([{
        "id":     g["id"],
        "sport":  g["sport"],
        "away":   g["away"],
        "home":   g["home"],
        "date":   g["date"],
        "status": g["status"],
    } for g in games])


@app.route("/api/odds/<game_id>")
def get_odds(game_id: str):
    # Check cache first
    for slug in SPORTS:
        for g in (cache_get(slug) or []):
            if str(g["id"]) == game_id:
                return jsonify(parse_odds(g))

    # Miss — reload and retry
    for slug in SPORTS:
        games = fetch_sport(slug)
        for g in games:
            if str(g["id"]) == game_id:
                return jsonify(parse_odds(g))

    abort(404)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
