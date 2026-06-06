#!/usr/bin/env python3
"""
Odds Shopper — CLI version
Data: ActionNetwork public API (free, no key needed)
Books: FanDuel, DraftKings, BetMGM, Caesars, bet365, ESPN Bet, BetRivers

Usage:
  python odds_shopper.py                  # interactive
  python odds_shopper.py "Chiefs"         # one-shot search
  python odds_shopper.py --list           # list all upcoming games
"""

import sys
import time
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

BASE_URL = "https://api.actionnetwork.com/web/v2/scoreboard"

BOOKS = {
    15:  "FanDuel",
    30:  "DraftKings",
    49:  "BetMGM",
    68:  "Caesars",
    69:  "bet365",
    71:  "ESPN Bet",
    75:  "BetRivers",
}

SPORTS = {
    "nfl":   "NFL",
    "ncaaf": "NCAAF",
    "nba":   "NBA",
    "ncaab": "NCAAB",
    "mlb":   "MLB",
    "nhl":   "NHL",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.actionnetwork.com/",
})

_CACHE: dict = {}
CACHE_TTL = 300


def cache_get(key):
    e = _CACHE.get(key)
    if e and (time.time() - e[1]) < CACHE_TTL:
        return e[0]
    return None


def cache_set(key, data):
    _CACHE[key] = (data, time.time())


def fetch_sport(slug):
    cached = cache_get(slug)
    if cached is not None:
        return cached

    resp = SESSION.get(f"{BASE_URL}/{slug}", timeout=15)
    resp.raise_for_status()
    data  = resp.json()
    games = data.get("games") or []
    result = []
    now_ts = time.time()

    for g in games:
        if g.get("status") in ("final", "cancelled", "postponed"):
            continue
        start_str = g.get("start_time") or ""
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if (now_ts - dt.timestamp()) > 14400:
                continue
        except Exception:
            pass
        teams    = {t["id"]: t for t in (g.get("teams") or [])}
        away_id  = g.get("away_team_id")
        home_id  = g.get("home_team_id")
        result.append({
            "id":      g["id"],
            "sport":   SPORTS.get(slug, slug.upper()),
            "away":    teams.get(away_id, {}).get("full_name", "Away"),
            "home":    teams.get(home_id, {}).get("full_name", "Home"),
            "date":    start_str,
            "status":  g.get("status", ""),
            "markets": g.get("markets") or {},
        })

    cache_set(slug, result)
    return result


def load_all():
    all_games = []
    for slug in SPORTS:
        try:
            all_games.extend(fetch_sport(slug))
        except Exception as ex:
            print(f"  Warning: {slug} — {ex}")
    all_games.sort(key=lambda g: g["date"])
    return all_games


def parse_odds(game):
    markets = game.get("markets") or {}
    spreads: dict = {}
    totals:  dict = {}

    def fmt(v):
        if v is None:
            return None
        return f"+{v}" if v > 0 else str(v)

    def add_spread(bname, side, val, odds):
        if bname not in spreads:
            spreads[bname] = {"away_pt": None, "away_px": None,
                              "home_pt": None, "home_px": None}
        if side in ("away", "road"):
            spreads[bname]["away_pt"] = val
            spreads[bname]["away_px"] = fmt(odds)
        elif side == "home":
            spreads[bname]["home_pt"] = val
            spreads[bname]["home_px"] = fmt(odds)

    def add_total(bname, side, val, odds):
        if bname not in totals:
            totals[bname] = {"total": None, "over_px": None, "under_px": None}
        totals[bname]["total"] = val
        if side == "over":
            totals[bname]["over_px"] = fmt(odds)
        elif side == "under":
            totals[bname]["under_px"] = fmt(odds)

    if markets and isinstance(next(iter(markets.values()), None), dict):
        for bid_str, bdata in markets.items():
            try:
                bid = int(bid_str)
            except ValueError:
                continue
            if bid not in BOOKS:
                continue
            bname = BOOKS[bid]
            event = bdata.get("event") or bdata
            for o in (event.get("spread") or []):
                add_spread(bname, o.get("side",""), o.get("value"), o.get("odds"))
            for o in (event.get("total") or []):
                add_total(bname, o.get("side",""), o.get("value"), o.get("odds"))
    elif isinstance(markets, list):
        for o in markets:
            bid = o.get("book_id")
            if bid not in BOOKS:
                continue
            bname = BOOKS[bid]
            t = o.get("type","")
            if o.get("period","event") not in ("event","game","full"):
                continue
            if t == "spread":
                add_spread(bname, o.get("side",""), o.get("value"), o.get("odds"))
            elif t == "total":
                add_total(bname, o.get("side",""), o.get("value"), o.get("odds"))

    spreads = {b: v for b, v in spreads.items()
               if v["away_pt"] is not None and v["home_pt"] is not None}
    totals  = {b: v for b, v in totals.items() if v["total"] is not None}
    return spreads, totals


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_games(games, query):
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


def fmt_time(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a %b %d  %I:%M %p")
    except Exception:
        return iso


def fmt_pt(pt):
    if pt is None:
        return "—"
    return f"+{pt}" if pt > 0 else str(pt)


def display_game(game):
    away  = game["away"]
    home  = game["home"]
    sport = game["sport"]
    gtime = fmt_time(game["date"])

    spreads, totals = parse_odds(game)

    print()
    print("=" * 68)
    print(f"  {away}  @  {home}")
    print(f"  {sport}  |  {gtime}")
    print("=" * 68)

    if spreads:
        print(f"\n  SPREADS")
        away_pts  = [v["away_pt"] for v in spreads.values() if v["away_pt"] is not None]
        home_pts  = [v["home_pt"] for v in spreads.values() if v["home_pt"] is not None]
        best_away = max(away_pts) if away_pts else None
        best_home = max(home_pts) if home_pts else None

        rows = sorted(spreads.items(), key=lambda kv: -(kv[1]["away_pt"] or 0))
        aw = away[:14]
        hw = home[:14]
        print(f"  {'Book':<20}  {aw:<18}  {hw}")
        print(f"  {'-'*60}")
        for book, v in rows:
            a_str = f"{fmt_pt(v['away_pt'])} ({v['away_px'] or '—'})"
            h_str = f"{fmt_pt(v['home_pt'])} ({v['home_px'] or '—'})"
            flag  = ""
            if v["away_pt"] == best_away:
                flag = "  <-- BEST AWAY"
            elif v["home_pt"] == best_home:
                flag = "  <-- BEST HOME"
            print(f"  {book:<20}  {a_str:<18}  {h_str}{flag}")

    if totals:
        print(f"\n  TOTALS")
        pts        = [v["total"] for v in totals.values() if v["total"] is not None]
        best_over  = min(pts) if pts else None
        best_under = max(pts) if pts else None

        rows = sorted(totals.items(), key=lambda kv: kv[1]["total"] or 0)
        print(f"  {'Book':<20}  {'Total':>7}  {'Over':>8}  {'Under':>8}")
        print(f"  {'-'*52}")
        for book, v in rows:
            flag = ""
            if v["total"] == best_over:
                flag = "  <-- BEST OVER"
            elif v["total"] == best_under:
                flag = "  <-- BEST UNDER"
            print(f"  {book:<20}  {fmt_pt(v['total']):>7}  "
                  f"{v['over_px'] or '—':>8}  {v['under_px'] or '—':>8}{flag}")

    if not spreads and not totals:
        print("\n  No lines posted yet for this game.")

    print()


def main():
    args   = sys.argv[1:]
    print("\nOdds Shopper — loading from ActionNetwork (free, no key needed)...")
    games  = load_all()
    print(f"  {len(games)} upcoming games loaded.\n")

    if args:
        if args[0] == "--list":
            for g in games:
                print(f"  [{g['sport']}]  {g['away']}  @  {g['home']}  —  {fmt_time(g['date'])}")
            return
        results = find_games(games, " ".join(args))
        if not results:
            print(f"  No games found. Try --list to see all games.")
        for g in results[:3]:
            display_game(g)
        return

    print("Type a team or matchup. Commands: list | reload | quit\n")
    while True:
        try:
            raw = input("Search: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        if raw.lower() in ("quit", "q", "exit"):
            break
        if raw.lower() == "list":
            for g in games:
                print(f"  [{g['sport']}]  {g['away']}  @  {g['home']}  —  {fmt_time(g['date'])}")
            continue
        if raw.lower() in ("reload", "refresh"):
            _CACHE.clear()
            games = load_all()
            print(f"  Reloaded — {len(games)} games.\n")
            continue
        results = find_games(games, raw)
        if not results:
            print(f"  No match for '{raw}'. Type 'list' to browse.\n")
            continue
        if len(results) > 1:
            print(f"\n  Found {len(results)} games:")
            for i, g in enumerate(results[:5], 1):
                print(f"  [{i}] {g['sport']:<8} {g['away']}  @  {g['home']}")
            try:
                pick = input("  Pick number (or Enter for top result): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if pick.isdigit() and 1 <= int(pick) <= min(5, len(results)):
                display_game(results[int(pick)-1])
            else:
                display_game(results[0])
        else:
            display_game(results[0])


if __name__ == "__main__":
    main()
