"""
Render cron entrypoint — captures opening + closing lines for CAPTURE_SPORTS.

Runs on a schedule (see render.yaml). Writes straight to Supabase, so it needs
SUPABASE_URL / SUPABASE_KEY (and ODDS_API_KEY for real-time lines) in its env.
No HTTP call, no token — it invokes the capture logic in-process.

The "close" is defined as the last snapshot taken before kickoff, so the cron
should run frequently during game windows. See render.yaml for the schedule.

Exits non-zero when a sport fails to load or Supabase is unconfigured. That is
deliberate: Render marks a non-zero cron run as FAILED and notifies, which is
the only way an exhausted Odds API balance or a bad env var becomes visible
before a whole weekend of closing lines is lost.
"""
import sys

from app import _sb, SB_KEY_IS_READONLY, run_capture, resolve_capture_slugs

if __name__ == "__main__":
    if _sb is None:
        print("[capture] FATAL: Supabase not configured - set SUPABASE_URL / SUPABASE_KEY")
        sys.exit(1)

    if SB_KEY_IS_READONLY:
        print("[capture] FATAL: SUPABASE_KEY is the anon key. RLS is on with zero "
              "policies, so every write will be denied (42501). Set the service_role "
              "key from Supabase -> Project Settings -> API Keys.")
        sys.exit(1)

    result = run_capture(resolve_capture_slugs())
    print("[capture]", result)

    errors = result.get("errors") or []
    if errors:
        print(f"[capture] FAILED with {len(errors)} error(s):")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    # A run that had real lines to record but banked nothing is a failure even with no
    # exception - exactly the shape the RLS lockdown took. Gate on bettable_games, not
    # games_seen: a slate with no lines posted yet is a legitimate zero, and failing on
    # it would just train you to ignore these alerts.
    if result.get("bettable_games") and not result.get("opens_recorded"):
        print("[capture] FAILED:", result["bettable_games"],
              "games had lines but 0 opens were recorded - nothing was written.")
        sys.exit(1)
