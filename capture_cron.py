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

from app import _sb, run_capture, resolve_capture_slugs

if __name__ == "__main__":
    if _sb is None:
        print("[capture] FATAL: Supabase not configured — set SUPABASE_URL / SUPABASE_KEY")
        sys.exit(1)

    result = run_capture(resolve_capture_slugs())
    print("[capture]", result)

    errors = result.get("errors") or []
    if errors:
        print(f"[capture] FAILED with {len(errors)} error(s):")
        for e in errors:
            print("  -", e)
        sys.exit(1)
