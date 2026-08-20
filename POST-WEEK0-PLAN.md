# Post-Week-0 Plan — sharper closing lines for $0

Written 2026-08-20, the day scheduled capture first went live. **Do none of this
until Week 0 (Sat Aug 29) has been captured successfully.** The pipeline works now;
the only job between now and then is to not break it.

---

## Why this matters

The CLV track record is the proof asset behind Cash on Saturdays. It is a public
claim that *you beat the closing number*. If the "close" being graded against is
15-30 minutes stale, that claim is measured against a number nobody could have bet.
The error runs both directions, so the record is not inflated -- it is **noisy in a
way that cannot be defended** if a sharp person interrogates it.

Paid fix rejected: The Odds API's cheapest tier is **$30/month for 20,000 credits**
and in-season need is **~1,090/month**. That is 5% utilisation for $30, with no tier
between it and free.

---

## First: what actually burned the free 500 credits

Not a runaway cron -- **the website**.

- `all_games()` loops every sport in `SPORTS`. With `USE_ODDS_API` true that is
  `OA_SPORTS` = **9 sports** (NFL, NFL preseason, NCAAF, NBA, NCAAB, MLB, NHL,
  World Cup, MLS).
- The Odds API bills 3 credits per call (3 markets x 1 region), so **one cold page
  load = 27 credits**.
- Render's free instance spins down after ~15 min idle, and the cache is in-memory,
  so nearly every visit is a cold load. `CACHE_TTL` of 1800s never gets the chance
  to help.
- **500 / 27 = ~18 page loads to exhaust an entire month.**
- The crons never ran (they did not exist until 2026-08-20), so the website was the
  *only* consumer. Nothing "changed" in August -- it had always been this expensive,
  and normal browsing finally drained it.

Roughly 78% of that spend was on sports never bet: MLS, World Cup, NHL, NBA, NCAAB, MLB.

---

## Track A -- stop the waste (do this first, it is the cheapest win)

Even on the free tier, these make the 500 credits go ~5x further.

1. **Restrict `SPORTS` when `USE_ODDS_API` is on.** Fetch NCAAF (+ NFL in season),
   not all nine. Cuts a cold load from 27 credits to 3-6.
2. **Lazy-load by sport.** The UI already has sport tabs; fetch on tab selection
   rather than fetching everything on page load.
3. **Persist the cache.** Store fetched odds in Supabase (or a file) so a spin-down
   does not wipe it. This is what makes `CACHE_TTL` meaningful on a free instance.

---

## Track B -- surgical Odds API use, still $0

500 free credits = **166 calls/month**. One call returns the *entire* slate for a
sport, so the credits are only wasted by calling too often, not by covering too many
games.

Today every cron run hits the configured source. Instead:

- Use **ActionNetwork for the broad sweep** (opens, and closes far from kickoff).
- Use **The Odds API only in the final ~15 minutes before each kickoff wave**, which
  is the only snapshot that determines CLV.
- Budget: a CFB Saturday has roughly 4-6 kickoff waves. At 3 credits each that is
  ~18 credits/Saturday, ~80/month -- comfortably inside the free 500.

Implementation sketch: add a `source` argument to `fetch_sport`, and have
`run_capture` choose per game based on minutes-to-kickoff rather than reading one
global `USE_ODDS_API` flag.

---

## Track C -- evaluate genuinely free sharp sources

Closing lines are widely published; The Odds API is a convenience, not a monopoly.
Worth testing, roughly in order of expected sharpness:

- **Pinnacle** -- the industry benchmark for a sharp close, and what most serious
  CLV tracking grades against. Highest priority to evaluate.
- **DraftKings / FanDuel public endpoints** -- these are the books actually bet, so
  their close is the most honest benchmark for *this* bettor.
- **scoresandodds.com** -- already flagged as a source for opening-line history.
- **Covers.com / SBR line history** -- good for backfill and verification.

For each: confirm it is queryable without auth, check update latency near kickoff,
and check terms of use before depending on it.

---

## Track D -- measure the problem before solving it

**Do this before Tracks A-C.** It may make them unnecessary.

The premise is that ActionNetwork's delayed close is materially wrong. That is an
assumption, not a measurement.

- For one Saturday, capture the close from ActionNetwork *and* from one alternative
  source for the same games.
- Compare: how far apart are they, in points on the spread and cents on the juice?
- **If the median difference is under ~0.5 points, the delayed close is fine** and
  none of this work is worth doing. Bank the season and spend the effort on Phase B
  (the bets table, the CLV view, the public track-record page) instead.

Cheapest possible version: the free tier resets monthly. Spend a few credits on one
Saturday purely to measure the gap.

---

## Explicitly NOT doing

- Paying $30/month for 5% utilisation of a 20,000-credit plan.
- Any change to the cron schedules, which are verified correct for Week 0
  (earliest kickoff Sat Aug 29 is 12:00 PM ET; `capture-sat` starts 11:00 AM ET).
