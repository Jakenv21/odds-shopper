#!/usr/bin/env python3
"""
Odds Shopper PRO — personal CLI powered by The Odds API (real-time)
Free tier: 500 credits/month  |  Get key: https://the-odds-api.com

With 30-min caching and personal use:
  Football season (NFL + NCAAF): ~4 credits/day = ~120/month
  All 6 sports active:           ~12 credits/day = ~360/month
  Way under the 500 free limit.

Usage:
  python odds_shopper_pro.py              # interactive
  python odds_shopper_pro.py "Dodgers"   # one-shot search
  python odds_shopper_pro.py --list      # list all games
  python odds_shopper_pro.py --credits   # check credits remaining

Set ODDS_API_KEY in a .env file or as an environment variable.
"""

import os
import sys
import time
import requests
from datetime import datetime
from difflib import SequenceMatcher

# ── Load .env if present ──────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://api.the-odds-api.com/v4/sports"

BOOKS = {
    "fanduel":        "FanDuel",
    "draftkings":     "DraftKings",
    "betmgm":         "BetMGM",
    "williamhill_us": "Caesars",
    "bet365":         "bet365",
    "betrivers":      "BetRivers",
    "hardrockbet":    "Hard Rock",
    "espnbet":        "ESPN Bet",
    "pointsbetus":    "PointsBet",
    "unibet_us":      "Unibet",
}
BOOK_KEYS = ",".join(BOOKS.keys())

SPORTS = {
    "americanfootball_nfl":   "NFL",
    "americanfootball_ncaaf": "NCAAF",
    "basketball_nba":         "NBA",
    "basketball_ncaab":       "NCAAB",
    "baseball_mlb":           "MLB",
    "icehockey_nhl":          "NHL",
}

CACHE_TTL = 1800   # 30 minutes — keeps monthly credit use well within free 500

_CACHE: dict = {}
_credits_remaining: int | None = None
_credits_used_session: int = 0

SESSION = requests.Session()


# ── API key ───────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        print()
        print("  No ODDS_API_KEY found.")
        print("  Get a free key (500 credits/month, no credit card) at:")
        print("  https://the-odds-api.com")
        print()
        key = input("  Paste your key: ").strip()
        if not key:
            print("  No key entered. Exiting.")
            sys.exit(1)
    return key


# ── Cache ─────────────────────────────────────────────────────────────────────

def cache_get(key):
    e = _CACHE.get(key)
    if e and (time.time() - e[1]) < CACHE_TTL:
        return e[0]
    return None


def cache_set(key, data):
    _CACHE[key] = (data, time.time())


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_sport(api_key: str, sport_slug: str) -> list:
    global _credits_remaining, _credits_used_session

    cached = cache_get(sport_slug)
    if cached is not None:
        return cached

    params = {
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    "h2h,spreads,totals",
        "oddsFormat": "american",
        "bookmakers": BOOK_KEYS,
    }

    resp = SESSION.get(f"{BASE_URL}/{sport_slug}/odds", params=params, timeout=15)

    if resp.status_code == 401:
        print("\n  Invalid API key. Check your key at https://the-odds-api.com")
        sys.exit(1)
    if resp.status_code == 422:
        # Sport not currently active/in-season — return empty
        cache_set(sport_slug, [])
        return []

    resp.raise_for_status()

    # Track credits from response headers
    remaining = resp.headers.get("x-requests-remaining")
    if remaining is not None:
        _credits_remaining = int(remaining)
    _credits_used_session += 1

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
            "sport":      SPORTS.get(sport_slug, sport_slug.upper()),
            "sport_slug": sport_slug,
            "home":       game["home_team"],
            "away":       game["away_team"],
            "date":       commence,
            "bookmakers": game.get("bookmakers", []),
        })

    cache_set(sport_slug, result)
    return result


def load_all(api_key: str) -> list:
    all_games = []
    for slug in SPORTS:
        try:
            all_games.extend(fetch_sport(api_key, slug))
        except Exception as ex:
            print(f"  Warning: {SPORTS.get(slug, slug)} — {ex}")
    all_games.sort(key=lambda g: g["date"])
    return all_games


# ── Odds parsing ──────────────────────────────────────────────────────────────

def fmt(v) -> str:
    """Format American odds integer as string."""
    if v is None:
        return "—"
    return f"+{v}" if v > 0 else str(v)


def fmt_pt(pt) -> str:
    """Format spread/total point value."""
    if pt is None:
        return "—"
    return f"+{pt}" if pt > 0 else str(pt)


def parse_odds(game: dict) -> tuple:
    """Returns (moneylines, spreads, totals) dicts."""
    home = game["home"]
    away = game["away"]

    moneylines: dict = {}
    spreads:    dict = {}
    totals:     dict = {}

    for bm in game.get("bookmakers", []):
        bkey  = bm["key"]
        bname = BOOKS.get(bkey, bm.get("title", bkey))

        for market in bm.get("markets", []):
            mkey     = market["key"]
            outcomes = market.get("outcomes", [])

            if mkey == "h2h":
                for o in outcomes:
                    side  = "home" if o["name"] == home else "away"
                    price = o["price"]
                    if bname not in moneylines:
                        moneylines[bname] = {"away_odds": None, "away_px": None,
                                             "home_odds": None, "home_px": None}
                    moneylines[bname][f"{side}_odds"] = price
                    moneylines[bname][f"{side}_px"]   = fmt(price)

            elif mkey == "spreads":
                for o in outcomes:
                    side  = "home" if o["name"] == home else "away"
                    point = o.get("point")
                    price = o["price"]
                    if bname not in spreads:
                        spreads[bname] = {"away_pt": None, "away_px": None,
                                          "home_pt": None, "home_px": None}
                    spreads[bname][f"{side}_pt"] = point
                    spreads[bname][f"{side}_px"] = fmt(price)

            elif mkey == "totals":
                for o in outcomes:
                    side  = o["name"].lower()   # "over" or "under"
                    point = o.get("point")
                    price = o["price"]
                    if bname not in totals:
                        totals[bname] = {"total": None, "over_px": None, "under_px": None}
                    totals[bname]["total"]        = point
                    totals[bname][f"{side}_px"]   = fmt(price)

    spreads    = {b: v for b, v in spreads.items()
                  if v["away_pt"] is not None and v["home_pt"] is not None}
    totals     = {b: v for b, v in totals.items() if v["total"] is not None}
    moneylines = {b: v for b, v in moneylines.items()
                  if v["away_odds"] is not None or v["home_odds"] is not None}

    return moneylines, spreads, totals


# ── Search ────────────────────────────────────────────────────────────────────

def similarity(a, b) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_games(games, query) -> list:
    q = query.lower()
    scored = []
    for g in games:
        targets = [g["home"], g["away"], f"{g['away']} {g['home']}",
                   *g["home"].split(), *g["away"].split()]
        score = max((similarity(q, t) for t in targets), default=0)
        if any(q in t.lower() for t in targets):
            score = max(score, 0.88)
        if score >= 0.55:
            scored.append((score, g))
    scored.sort(key=lambda x: -x[0])
    seen, result = set(), []
    for _, g in scored:
        if g["id"] not in seen:
            seen.add(g["id"])
            result.append(g)
    return result


# ── Display ───────────────────────────────────────────────────────────────────

def fmt_time(iso) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a %b %d  %I:%M %p")
    except Exception:
        return iso


def display_game(game):
    away  = game["away"]
    home  = game["home"]
    sport = game["sport"]
    gtime = fmt_time(game["date"])

    moneylines, spreads, totals = parse_odds(game)

    print()
    print("=" * 70)
    print(f"  {away}  @  {home}")
    print(f"  {sport}  |  {gtime}")
    print("=" * 70)

    # ── Moneylines ────────────────────────────────────────────────────────────
    if moneylines:
        away_ml_entries = [(b, v["away_odds"]) for b, v in moneylines.items() if v["away_odds"] is not None]
        home_ml_entries = [(b, v["home_odds"]) for b, v in moneylines.items() if v["home_odds"] is not None]
        best_away_ml = max(away_ml_entries, key=lambda x: x[1])[1] if away_ml_entries else None
        best_home_ml = max(home_ml_entries, key=lambda x: x[1])[1] if home_ml_entries else None

        print(f"\n  MONEYLINES")
        aw = away[:16]
        hw = home[:16]
        print(f"  {'Book':<20}  {aw:<20}  {hw}")
        print(f"  {'-'*64}")
        for book, v in sorted(moneylines.items()):
            a_str = v["away_px"] or "—"
            h_str = v["home_px"] or "—"
            flags = []
            if v.get("away_odds") is not None and v["away_odds"] == best_away_ml:
                flags.append(f"BEST {away.split()[-1].upper()}")
            if v.get("home_odds") is not None and v["home_odds"] == best_home_ml:
                flags.append(f"BEST {home.split()[-1].upper()}")
            flag_str = f"  <-- {' | '.join(flags)}" if flags else ""
            print(f"  {book:<20}  {a_str:<20}  {h_str}{flag_str}")

    # ── Spreads ───────────────────────────────────────────────────────────────
    if spreads:
        away_pts = [v["away_pt"] for v in spreads.values() if v["away_pt"] is not None]
        home_pts = [v["home_pt"] for v in spreads.values() if v["home_pt"] is not None]
        best_away_sp = max(away_pts) if away_pts else None
        best_home_sp = max(home_pts) if home_pts else None

        print(f"\n  SPREADS")
        aw = away[:14]
        hw = home[:14]
        print(f"  {'Book':<20}  {aw:<20}  {hw}")
        print(f"  {'-'*64}")
        for book, v in sorted(spreads.items(), key=lambda kv: -(kv[1]["away_pt"] or 0)):
            a_str = f"{fmt_pt(v['away_pt'])} ({v['away_px'] or '—'})"
            h_str = f"{fmt_pt(v['home_pt'])} ({v['home_px'] or '—'})"
            flags = []
            if v["away_pt"] == best_away_sp:
                flags.append("BEST AWAY")
            if v["home_pt"] == best_home_sp:
                flags.append("BEST HOME")
            flag_str = f"  <-- {' | '.join(flags)}" if flags else ""
            print(f"  {book:<20}  {a_str:<20}  {h_str}{flag_str}")

    # ── Totals ────────────────────────────────────────────────────────────────
    if totals:
        pts = [v["total"] for v in totals.values() if v["total"] is not None]
        best_over  = min(pts) if pts else None
        best_under = max(pts) if pts else None

        print(f"\n  TOTALS")
        print(f"  {'Book':<20}  {'Total':>7}  {'Over':>8}  {'Under':>8}")
        print(f"  {'-'*52}")
        for book, v in sorted(totals.items(), key=lambda kv: kv[1]["total"] or 0):
            flags = []
            if v["total"] == best_over:
                flags.append("BEST OVER")
            if v["total"] == best_under:
                flags.append("BEST UNDER")
            flag_str = f"  <-- {' | '.join(flags)}" if flags else ""
            print(f"  {book:<20}  {fmt_pt(v['total']):>7}  "
                  f"{v['over_px'] or '—':>8}  {v['under_px'] or '—':>8}{flag_str}")

    # ── Best bets summary ─────────────────────────────────────────────────────
    if moneylines or spreads or totals:
        print(f"\n  ── QUICK ANSWER ─────────────────────────────────────────────")

        for team, side in [(away, "away"), (home, "home")]:
            entries = [(b, v[f"{side}_odds"]) for b, v in moneylines.items()
                       if v.get(f"{side}_odds") is not None]
            if entries:
                best_b, best_o = max(entries, key=lambda x: x[1])
                rest = [f"{b} {fmt(o)}" for b, o in sorted(entries, key=lambda x: -x[1]) if b != best_b]
                rest_str = f"   (others: {' · '.join(rest[:3])})" if rest else ""
                print(f"  {team} ML       →  {best_b}  {fmt(best_o)}{rest_str}")

        for team, side in [(away, "away"), (home, "home")]:
            entries = [(b, v[f"{side}_pt"], v[f"{side}_px"]) for b, v in spreads.items()
                       if v.get(f"{side}_pt") is not None]
            if entries:
                best_b, best_pt, best_px = max(entries, key=lambda x: x[1])
                print(f"  {team} Spread   →  {best_b}  {fmt_pt(best_pt)} ({best_px})")

        if totals:
            te = [(b, v["total"], v["over_px"], v["under_px"]) for b, v in totals.items()]
            ov = min(te, key=lambda x: x[1])
            un = max(te, key=lambda x: x[1])
            print(f"  Best Over      →  {ov[0]}  {ov[1]} ({ov[2]})")
            print(f"  Best Under     →  {un[0]}  {un[1]} ({un[3]})")

    if not moneylines and not spreads and not totals:
        print("\n  No lines posted yet for this game.")

    if _credits_remaining is not None:
        print(f"\n  [API credits remaining this month: {_credits_remaining}]")

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args_in = sys.argv[1:]
    api_key = get_api_key()

    # Credits-only check — make a lightweight call to any in-season sport
    if args_in and args_in[0] == "--credits":
        print("\nChecking credits...")
        try:
            fetch_sport(api_key, "baseball_mlb")
        except Exception:
            pass
        if _credits_remaining is not None:
            print(f"  API credits remaining this month: {_credits_remaining} / 500")
        else:
            print("  Could not retrieve credit info.")
        return

    print("\nOdds Shopper PRO — loading real-time odds...")
    games = load_all(api_key)
    used_str = f"  ({_credits_used_session} API credit{'s' if _credits_used_session != 1 else ''} used this session"
    remain_str = f", {_credits_remaining} remaining this month)" if _credits_remaining is not None else ")"
    print(f"  {len(games)} upcoming games loaded.{used_str}{remain_str}\n")

    # One-shot list
    if args_in and args_in[0] == "--list":
        for g in games:
            print(f"  [{g['sport']:<6}]  {g['away']}  @  {g['home']}  —  {fmt_time(g['date'])}")
        return

    # One-shot team search
    if args_in:
        results = find_games(games, " ".join(args_in))
        if not results:
            print("  No games found. Try --list to see everything.")
        for g in results[:3]:
            display_game(g)
        return

    # Interactive mode
    print("Type a team name or matchup.  Commands:  list | reload | credits | quit\n")
    while True:
        try:
            raw = input("Search: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        cmd = raw.lower()
        if cmd in ("quit", "q", "exit"):
            break
        if cmd == "list":
            for g in games:
                print(f"  [{g['sport']:<6}]  {g['away']}  @  {g['home']}  —  {fmt_time(g['date'])}")
            continue
        if cmd == "credits":
            if _credits_remaining is not None:
                print(f"  API credits remaining this month: {_credits_remaining} / 500")
            else:
                print("  No API calls made yet this session.")
            continue
        if cmd in ("reload", "refresh"):
            _CACHE.clear()
            games = load_all(api_key)
            print(f"  Reloaded — {len(games)} games.  Credits remaining: {_credits_remaining}\n")
            continue

        results = find_games(games, raw)
        if not results:
            print(f"  No match for '{raw}'. Type 'list' to browse.\n")
            continue
        if len(results) > 1:
            print(f"\n  Found {len(results)} games:")
            for i, g in enumerate(results[:5], 1):
                print(f"  [{i}] {g['sport']:<7}  {g['away']}  @  {g['home']}")
            try:
                pick = input("  Pick number (or Enter for top result): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            idx = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= min(5, len(results)) else 0
            display_game(results[idx])
        else:
            display_game(results[0])


if __name__ == "__main__":
    main()
