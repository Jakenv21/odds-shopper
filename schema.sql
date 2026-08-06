-- ============================================================================
-- odds-shopper — Supabase schema (reference copy)
--
-- STATUS: already applied to project pasedeakyktwhsumltkz on 2026-08-06 as
-- migration `add_commence_time_and_bets_log_with_source`. This file is the
-- record of what the live database looks like; you do not need to re-run it.
-- It is idempotent, so re-running is harmless.
--
-- NOTE: the live timestamp column is `snapped_at` (NOT `captured_at`, which an
-- earlier draft of this file wrongly assumed). app.py writes snapped_at
-- explicitly on close rows so the overwrite tracks the latest number.
-- ============================================================================

-- 1. Line history -----------------------------------------------------------
create table if not exists line_snapshots (
    game_id       text        not null,
    book          text        not null,
    market        text        not null,                 -- 'h2h' | 'spread' | 'total'
    side          text        not null,                 -- away|home|draw|over|under
    point         numeric,
    price         integer     not null,
    phase         text        not null default 'open',  -- 'open' | 'close'
    commence_time timestamptz,                          -- kickoff, for CLV joins
    snapped_at    timestamptz not null default now()
);

alter table line_snapshots add column if not exists commence_time timestamptz;

-- open + close must coexist per book/side, so phase is part of the key.
create unique index if not exists line_snapshots_uq
  on line_snapshots (game_id, book, market, side, phase);
create index if not exists line_snapshots_game_idx on line_snapshots (game_id);

-- 2. The bet log ------------------------------------------------------------
-- `source` records WHICH method produced the bet. Roughly 4-8 bets a week come
-- off the RLM/splits screen and ~17-21 come from other reads; without this tag
-- there is no way to tell which of those is the edge and which is the leak.
-- It cannot be reconstructed after the season, so it is captured at placement.
create table if not exists bets (
    id         bigint generated always as identity primary key,
    game_id    text,                          -- links to line_snapshots.game_id
    placed_at  timestamptz not null default now(),
    book       text,
    market     text        not null,          -- 'h2h' | 'spread' | 'total'
    side       text        not null,          -- away|home|draw|over|under
    point      numeric,                       -- the line you took
    price      integer     not null,          -- the American odds you took
    stake      numeric,
    source     text        not null default 'other',
    note       text
);

-- A typo'd tag ('RLM' vs 'rlm') silently splits a group and destroys the exact
-- comparison this column exists to make, so the taxonomy is enforced.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'bets_source_chk') then
    alter table bets add constraint bets_source_chk
      check (source in ('rlm','own-read','situational','coin-toss','injury','other'));
  end if;
end $$;

create index if not exists bets_game_idx   on bets (game_id);
create index if not exists bets_source_idx on bets (source);
create index if not exists bets_placed_idx on bets (placed_at);

-- ============================================================================
-- SECURITY — NOT YET APPLIED. Read DEPLOY.md "Lock down the database" first.
-- Both tables currently have RLS disabled, which means anyone holding the anon
-- key can read or delete every row. The anon key is in this repo's git history.
--
-- Run this ONLY AFTER SUPABASE_KEY is switched to the service_role key in both
-- .env and every Render service, or the app's own writes will start failing:
--
--   alter table public.line_snapshots enable row level security;
--   alter table public.bets           enable row level security;
--
-- No policies are needed: service_role bypasses RLS, and with RLS on and zero
-- policies the leaked anon key can no longer touch either table.
-- ============================================================================

-- ============================================================================
-- COST NOTE — The Odds API bills markets x regions = 3 credits per call.
-- See the credit budget table at the top of render.yaml. The free 500/month
-- tier does NOT cover the in-season NCAAF schedule.
-- ============================================================================
