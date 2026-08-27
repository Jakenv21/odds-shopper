# Odds Shopper — Deploy Checklist

Goal: closing-line capture running before CFB Week 0 (Sat **Aug 29, 2026**).

**Already done for you (2026-08-06):**
- ✅ Supabase migration applied — `commence_time` on `line_snapshots`, plus the
  `bets` table with the `source` tag. Nothing to run in the SQL editor.
- ✅ NFL preseason wired in (`nflpre`) and verified against live odds.
- ✅ First-ever `close` rows written and verified.

**What only you can do: Steps 1–4 below (~20 min).** They all need your Render,
Odds API, and GitHub logins.

---

## Step 1 — Choose your data plan (decide first, it changes what you paste)

The Odds API bills **3 credits per call** (3 markets × 1 region). Per the budget
table in `render.yaml`:

| | credits |
|---|---|
| NCAAF opens + closes | ~1,090 / month in-season |
| NFL preseason (August only) | ~1,150 total |

**The free tier is 500/month. It will not cover this.** Pick one:

- **A — Paid Odds API plan (recommended).** Real-time closes, which is the whole
  point of CLV grading. Set `ODDS_API_KEY` on every service.
- **B — Free fallback.** Leave `ODDS_API_KEY` **unset on the cron services**.
  They fall back to ActionNetwork: no credit cost, *more* books (adds Caesars
  and bet365), but the close is 15–30 min delayed — meaningfully worse for
  grading whether you beat the number.

You can run A for closes and B for opens if you want to split the difference.

---

## Step 2 — Create the Render services

1. Render dashboard → **New** → **Blueprint** → connect `Jakenv21/odds-shopper`.
2. Render reads `render.yaml` and creates **1 web service + 5 crons**:
   `ncaaf-sunday` (opens), `capture-sat` + `capture-late` (closes),
   `nflpre-open`, `nflpre-close`.
   NFL regular-season and all-sports crons are commented out in the yaml on
   purpose — uncomment them in September if you want NFL.
3. ✅ Check: dashboard shows all 6 services, web build = **Live**.

---

## Step 3 — Paste environment variables

`sync: false` means Render never gets these from git. Paste them by hand on
**every service** (the web one plus all 5 crons):

| Var | Where to get it | Which services |
|-----|-----------------|----------------|
| `SUPABASE_URL` | Supabase → Project Settings → API | all |
| `SUPABASE_KEY` | Supabase → Project Settings → API → **service_role** | all |
| `ODDS_API_KEY` | the-odds-api.com account | all (omit on crons for plan B) |
| `CAPTURE_TOKEN` | make up any random string | web only |

> Use the **service_role** key, not anon. The app no longer has a hardcoded
> fallback key, so if `SUPABASE_KEY` is missing the service starts fine but
> silently stops writing — check `/api/info`, which reports Supabase status.

---

## Step 4 — Smoke test (don't wait for Saturday)

1. Hit `https://<your-web-url>/api/capture?token=<CAPTURE_TOKEN>`.
2. ✅ Expect JSON like
   `{"games_seen": N, "opens_recorded": N, "closes_updated": N, "errors": []}`.
   A non-empty `errors` array means a sport failed to load — usually an
   exhausted credit balance or a bad key.
3. ✅ Then in Supabase: `select phase, count(*) from line_snapshots group by phase;`
   — both `open` and `close` should have rows.

Cron failures are now **loud**: `capture_cron.py` exits non-zero on any error,
so Render marks the run FAILED and notifies you instead of dying quietly for
weeks. Turn on Render's failure notifications for these services.

---

## Step 5 — Lock down the database ✅ DONE 2026-08-18

The RLS hole Supabase emailed about every week (Aug 4 / 11 / 18) is **closed**,
applied as migration `lock_down_clv_tables_rls_and_bet_clv_view`:

- `line_snapshots` + `bets` — RLS enabled, zero policies, anon/authenticated
  grants revoked.
- `bet_clv` — was a postgres-owned **SECURITY DEFINER** view, so it would have
  kept serving the same rows straight past the new RLS. Now
  `security_invoker = true`, with anon access revoked. (The advisory email only
  mentioned one table; the real exposure was two tables *and* this view.)

Verified: the leaked anon key now gets `42501 permission denied` (HTTP 401) on
all three objects. All three ERROR-level advisories cleared.

### ⚠️ Still on you — the key swap (before Sat Aug 29, Week 0)

`SUPABASE_KEY` in `odds-shopper/.env` is currently the **anon** key, and that
key is public in this repo's git history. Now that RLS is on, the anon key can
no longer write — so **capture and the CFB Desk will fail until you swap it**:

1. Supabase → Project Settings → API Keys → copy the **service_role** (secret) key.
2. Set it as `SUPABASE_KEY` in `odds-shopper/.env` — cfb-agent reads this file too.
3. Set it as `SUPABASE_KEY` on **all 6 Render services**.
4. Make the GitHub repo **private** (Settings → General → Danger Zone).
5. Smoke test: `/api/capture?token=<CAPTURE_TOKEN>` → expect `opens_recorded > 0`,
   then confirm rows land: `select phase, count(*) from line_snapshots group by phase;`

Failures are loud: `capture_cron.py` exits non-zero, so Render marks the run
FAILED rather than dying quietly.

Rollback (only if you need the old behaviour back):

```sql
grant all on public.line_snapshots, public.bets, public.bet_clv to anon, authenticated;
alter table public.line_snapshots disable row level security;
alter table public.bets           disable row level security;
```

---

## What's captured vs. still to build

- ✅ Opening lines (first-seen, preserved — never overwritten)
- ✅ Closing lines (rolling, pre-kickoff only, `snapped_at` tracks the latest)
- ✅ `bets` table with the `source` tag, ready to log against
- ⏳ **Phase B**: bet-entry UI/endpoint writing to `bets`, the CLV view joining
  bets → close, and a public track-record page. Build once a few weeks of real
  closes are banked.
