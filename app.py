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
from flask import Flask, render_template, jsonify, abort, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

try:
    from supabase import create_client as _sb_create
    _sb = _sb_create(os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")) \
        if (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY")) else None
except Exception:
    _sb = None

# ── Config ────────────────────────────────────────────────────────────────────

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
USE_ODDS_API = bool(ODDS_API_KEY)

# Closing-line capture: shared secret so only the cron can trigger /api/capture,
# and which sports to snapshot (comma-separated friendly names). Default NCAAF-only
# to keep The Odds API credit usage bounded — see schema.sql "COST NOTE".
CAPTURE_TOKEN  = os.getenv("CAPTURE_TOKEN", "").strip()
CAPTURE_SPORTS = os.getenv("CAPTURE_SPORTS", "ncaaf").strip()
FRIENDLY_TO_OA = {
    "nfl":   "americanfootball_nfl",
    "ncaaf": "americanfootball_ncaaf",
    "nba":   "basketball_nba",
    "ncaab": "basketball_ncaab",
    "mlb":   "baseball_mlb",
    "nhl":   "icehockey_nhl",
}

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
    "espnbet":     "theScore",
}
OA_BOOK_KEYS = ",".join(OA_BOOKS.keys())
OA_SPORTS = {
    "americanfootball_nfl":   "NFL",
    "americanfootball_ncaaf": "NCAAF",
    "basketball_nba":         "NBA",
    "basketball_ncaab":       "NCAAB",
    "baseball_mlb":           "MLB",
    "icehockey_nhl":          "NHL",
    "soccer_fifa_world_cup":  "World Cup",
    "soccer_usa_mls":         "MLS",
}
OA_TO_AN_SLUG = {
    "americanfootball_nfl":   "nfl",
    "americanfootball_ncaaf": "ncaaf",
    "basketball_nba":         "nba",
    "basketball_ncaab":       "ncaab",
    "baseball_mlb":           "mlb",
    "icehockey_nhl":          "nhl",
}
AN_SUPP_BOOKS = {68: "Caesars", 69: "bet365"}

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

def fetch_sport_an(sport_slug: str, force: bool = False) -> list:
    if not force:
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


# ── ActionNetwork supplemental (Caesars + bet365 only) ───────────────────────

def fetch_supplemental_an(oa_slug: str) -> dict:
    """Return {(away_lower, home_lower): markets_dict} for Caesars + bet365 only."""
    an_slug = OA_TO_AN_SLUG.get(oa_slug)
    if not an_slug:
        return {}
    cache_key = f"supp_{an_slug}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        resp = SESSION.get(f"{AN_BASE}/{an_slug}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result = {}
        for g in (data.get("games") or []):
            teams = {t["id"]: t for t in (g.get("teams") or [])}
            away = teams.get(g.get("away_team_id"), {}).get("full_name", "").lower().strip()
            home = teams.get(g.get("home_team_id"), {}).get("full_name", "").lower().strip()
            if not away or not home:
                continue
            raw = g.get("markets") or {}
            filtered = {k: v for k, v in raw.items()
                        if k.isdigit() and int(k) in AN_SUPP_BOOKS}
            if filtered:
                result[(away, home)] = filtered
        cache_set(cache_key, result)
        return result
    except Exception as ex:
        app.logger.warning("Supplemental AN fetch failed for %s: %s", oa_slug, ex)
        return {}


# ── The Odds API fetch ────────────────────────────────────────────────────────

def fetch_sport_oa(sport_slug: str, force: bool = False) -> list:
    if not force:
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

    # Supplement with Caesars + bet365 from ActionNetwork
    supp = fetch_supplemental_an(sport_slug)
    for game in result:
        key = (game["away"].lower().strip(), game["home"].lower().strip())
        if key in supp:
            game["markets"] = supp[key]

    cache_set(sport_slug, result)
    return result


def fetch_sport(sport_slug: str, force: bool = False) -> list:
    return fetch_sport_oa(sport_slug, force) if USE_ODDS_API else fetch_sport_an(sport_slug, force)


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
            totals[bname] = {"total": None, "over_px": None, "under_px": None,
                             "over_raw": None, "under_raw": None}
        totals[bname]["total"] = value
        if side == "over":
            totals[bname]["over_px"]  = fmt(odds)
            totals[bname]["over_raw"] = odds
        elif side == "under":
            totals[bname]["under_px"]  = fmt(odds)
            totals[bname]["under_raw"] = odds

    def add_ml(bname, side, odds):
        if bname not in moneylines:
            moneylines[bname] = {"away_px": None, "home_px": None, "draw_px": None,
                                 "away_odds": None, "home_odds": None, "draw_odds": None}
        if side in ("away", "road"):
            moneylines[bname]["away_px"]   = fmt(odds)
            moneylines[bname]["away_odds"] = odds
        elif side == "home":
            moneylines[bname]["home_px"]   = fmt(odds)
            moneylines[bname]["home_odds"] = odds
        elif side == "draw":
            moneylines[bname]["draw_px"]   = fmt(odds)
            moneylines[bname]["draw_odds"] = odds

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
                        if name == "Draw":
                            side = "draw"
                        elif name == home:
                            side = "home"
                        else:
                            side = "away"
                        add_ml(bname, side, price)
                    elif mkey == "spreads":
                        side = "home" if name == home else "away"
                        add_spread(bname, side, point, price)
                    elif mkey == "totals":
                        add_total(bname, name.lower(), point, price)

    # ── Format A: ActionNetwork (book_id dict) — also runs as OA supplement ──
    if markets and isinstance(next(iter(markets.values()), None), dict):
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
        ("Draw", "draw_odds"),
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
            def px_val(px):
                try:    return int(str(px or "").replace("+", ""))
                except: return -9999
            # Best over: lowest total, then best price (least juice) as tiebreaker
            over_sorted = sorted(total_entries, key=lambda x: (x[1], -px_val(x[2])))
            bo_book, bo_total, bo_px, _ = over_sorted[0]
            others_over = [{"book": b, "odds": f"{t} ({opx})"} for b, t, opx, _ in over_sorted[1:]]
            best_bets.append({
                "type": "over", "label": "Best Over",
                "book": bo_book, "odds": f"{bo_total} ({bo_px})",
                "raw": bo_total, "others": others_over,
            })
            # Best under: highest total, then best price (least juice) as tiebreaker
            under_sorted = sorted(total_entries, key=lambda x: (-x[1], -px_val(x[3])))
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


# ── Supabase line history ─────────────────────────────────────────────────────

def _snapshot_rows(game_id: str, parsed: dict, phase: str,
                   commence_time=None, captured_at=None) -> list:
    """Flatten a parsed game into one line_snapshots row per book/market/side."""
    base = {"game_id": game_id, "phase": phase}
    rows = []
    for book, ml in parsed["moneylines"].items():
        for side, key in [("away", "away_odds"), ("home", "home_odds"), ("draw", "draw_odds")]:
            if ml.get(key) is not None:
                rows.append({**base, "book": book, "market": "h2h",
                             "side": side, "point": None, "price": ml[key]})
    for book, sp in parsed["spreads"].items():
        for side, pt_k, raw_k in [("away", "away_pt", "away_raw"), ("home", "home_pt", "home_raw")]:
            if sp.get(raw_k) is not None:
                rows.append({**base, "book": book, "market": "spread",
                             "side": side, "point": sp[pt_k], "price": sp[raw_k]})
    for book, tot in parsed["totals"].items():
        if tot.get("over_raw") is not None:
            rows.append({**base, "book": book, "market": "total",
                         "side": "over", "point": tot["total"], "price": tot["over_raw"]})
        if tot.get("under_raw") is not None:
            rows.append({**base, "book": book, "market": "total",
                         "side": "under", "point": tot["total"], "price": tot["under_raw"]})
    return rows


def snapshot_opening(game_id: str, parsed: dict, commence_time=None):
    """Store first-seen lines as the OPEN. ignore_duplicates keeps the first write."""
    if not _sb:
        return
    rows = _snapshot_rows(game_id, parsed, "open", commence_time)
    if not rows:
        return
    try:
        _sb.table("line_snapshots").upsert(
            rows, on_conflict="game_id,book,market,side,phase", ignore_duplicates=True
        ).execute()
    except Exception as ex:
        app.logger.warning("Supabase opening snapshot failed: %s", ex)


def snapshot_closing(game_id: str, parsed: dict, commence_time=None):
    """Overwrite the CLOSE row each run. The last write before kickoff = the true close,
    so this MUST only be called while the game has not yet started."""
    if not _sb:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = _snapshot_rows(game_id, parsed, "close", commence_time, captured_at=now_iso)
    if not rows:
        return
    try:
        _sb.table("line_snapshots").upsert(
            rows, on_conflict="game_id,book,market,side,phase"  # no ignore_duplicates -> overwrite
        ).execute()
    except Exception as ex:
        app.logger.warning("Supabase closing snapshot failed: %s", ex)


def get_opening_lines(game_id: str) -> dict:
    """Return {book: {market: {side: {point, price}}}} for opening-line display."""
    if not _sb:
        return {}
    try:
        resp = _sb.table("line_snapshots").select("*") \
            .eq("game_id", game_id).eq("phase", "open").execute()
        opening: dict = {}
        for row in (resp.data or []):
            b, m, s = row["book"], row["market"], row["side"]
            opening.setdefault(b, {}).setdefault(m, {})[s] = {
                "point": row["point"], "price": row["price"]
            }
        return opening
    except Exception as ex:
        app.logger.warning("Supabase fetch failed: %s", ex)
        return {}


def resolve_capture_slugs(param: str = "") -> list:
    """Map friendly sport names (ncaaf,nfl,...) to the active source's slugs."""
    names = [s.strip().lower() for s in (param or CAPTURE_SPORTS).split(",") if s.strip()]
    slugs = []
    for n in names:
        slug = FRIENDLY_TO_OA.get(n) if USE_ODDS_API else (n if n in AN_SPORTS else None)
        if slug and slug in SPORTS and slug not in slugs:
            slugs.append(slug)
    return slugs or list(SPORTS.keys())


def run_capture(slugs: list) -> dict:
    """Snapshot opens (once) and closes (rolling, pre-kickoff only) for the given sports.
    Force-refreshes odds so the close reflects the latest pre-kickoff number, not cache."""
    now_ts = time.time()
    opened = closed = games_seen = 0
    for slug in slugs:
        try:
            games = fetch_sport(slug, force=True)
        except Exception as ex:
            app.logger.warning("capture: could not load %s: %s", slug, ex)
            continue
        for g in games:
            games_seen += 1
            start_ts = None
            try:
                start_ts = datetime.fromisoformat(
                    (g.get("date") or "").replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
            parsed = parse_odds(g)
            if not parsed["best_bets"]:
                continue
            snapshot_opening(g["id"], parsed, g.get("date"))
            opened += 1
            # Only update the close while the game is still in the future.
            if start_ts is None or start_ts > now_ts:
                snapshot_closing(g["id"], parsed, g.get("date"))
                closed += 1
    return {"sports": slugs, "games_seen": games_seen,
            "opens_recorded": opened, "closes_updated": closed}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info")
def get_info():
    sb_status = "not configured"
    if _sb:
        try:
            r = _sb.table("line_snapshots").select("game_id", count="exact").limit(1).execute()
            sb_status = f"ok ({r.count} rows)"
        except Exception as ex:
            sb_status = f"error: {ex}"
    return jsonify({
        "source":    "The Odds API (real-time)" if USE_ODDS_API else "ActionNetwork (~15-30 min delay)",
        "realtime":  USE_ODDS_API,
        "cache_ttl": CACHE_TTL,
        "supabase":  sb_status,
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
    game = None
    for slug in SPORTS:
        for g in (cache_get(slug) or []):
            if str(g["id"]) == game_id:
                game = g
                break
        if game:
            break

    if not game:
        for slug in SPORTS:
            for g in fetch_sport(slug):
                if str(g["id"]) == game_id:
                    game = g
                    break
            if game:
                break

    if not game:
        abort(404)

    result = parse_odds(game)
    try:
        dt = datetime.fromisoformat((game.get("date") or "").replace("Z", "+00:00"))
        game_started = dt.timestamp() <= time.time()
    except Exception:
        game_started = False

    if not game_started:
        snapshot_opening(game_id, result, game.get("date"))
        result["opening"] = get_opening_lines(game_id)
    else:
        result["opening"] = {}
    return jsonify(result)


@app.route("/api/capture")
def capture():
    """Cron-triggered closing-line capture. Protected by CAPTURE_TOKEN."""
    if not CAPTURE_TOKEN or request.args.get("token", "") != CAPTURE_TOKEN:
        abort(403)
    if not _sb:
        return jsonify({"error": "Supabase not configured (set SUPABASE_URL / SUPABASE_KEY)"}), 503
    slugs = resolve_capture_slugs(request.args.get("sports", ""))
    return jsonify(run_capture(slugs))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
