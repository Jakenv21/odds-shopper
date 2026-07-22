"""
Render cron entrypoint — captures opening + closing lines for CAPTURE_SPORTS.

Runs on a schedule (see render.yaml). Writes straight to Supabase, so it needs
SUPABASE_URL / SUPABASE_KEY (and ODDS_API_KEY for real-time lines) in its env.
No HTTP call, no token — it invokes the capture logic in-process.

The "close" is defined as the last snapshot taken before kickoff, so the cron
should run frequently during game windows. See render.yaml for the schedule.
"""
from app import run_capture, resolve_capture_slugs

if __name__ == "__main__":
    result = run_capture(resolve_capture_slugs())
    print("[capture]", result)
