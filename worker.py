"""
Refresh worker + daily scheduler for ReadMyNewsletter.

`refresh_user`      builds a fresh digest for one account and stores it.
`refresh_all_due`   refreshes every auto-refresh account whose latest digest is
                    older than the interval (default ~20h → effectively daily).
`start_scheduler`   runs `refresh_all_due` in a background daemon thread.

Two ways to drive the daily refresh:
  * In-process: on a single persistent instance the app starts the scheduler
    thread automatically (see app.py / RUN_SCHEDULER).
  * External cron: run `python worker.py` once per day (cron, systemd timer,
    Railway/Render cron, GitHub Actions, Vercel Cron). This is the right choice
    when you run multiple web workers.
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import newsletter_digest as nd
import store

REFRESH_MIN_AGE_HOURS = int(os.environ.get("REFRESH_MIN_AGE_HOURS", "20"))
SCHEDULER_TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "3600"))


def build_digest_for(conn):
    """Run the engine for one (revealed) connection dict. Returns
    (html, day_range, item_count, total_time). Raises on fatal errors."""
    mail = nd.connect(
        conn["imap_host"], conn["imap_port"], conn["imap_user"], conn["imap_password"]
    )
    try:
        items = nd.fetch_newsletters(
            mail, conn["days"], conn["imap_folder"],
            only_unread=not conn["include_read"],
        )
    finally:
        try:
            mail.logout()
        except Exception:  # noqa: BLE001
            pass

    if conn["use_ai"] and conn.get("api_key"):
        items = nd.enrich_with_ai(
            items, conn["api_key"], conn["model"] or nd.MODEL
        )
    else:
        items = nd.group_only(items)

    items.sort(
        key=lambda it: it["date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    day_range = f"last {conn['days']} days"
    html = nd.build_html(items, day_range)
    total_time = sum(it["read_time"] for it in items)
    return html, day_range, len(items), total_time


def refresh_user(user_id):
    """Fetch + summarise + store a digest for one user. Returns (ok, message)."""
    conn = store.get_connection(user_id, reveal=True)
    if not conn:
        return False, "No inbox connected yet."
    try:
        html, day_range, count, total_time = build_digest_for(conn)
    except Exception as exc:  # noqa: BLE001
        return False, f"Refresh failed: {exc}"
    if count == 0:
        return False, "No newsletters found in the selected window."
    store.add_digest(user_id, html, day_range, count, total_time)
    return True, f"Digest ready — {count} newsletters."


def _is_due(user_id, min_age_hours):
    last = store.latest_digest_time(user_id)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=min_age_hours)


def refresh_all_due(min_age_hours=REFRESH_MIN_AGE_HOURS):
    refreshed, skipped, failed = 0, 0, 0
    for user_id in store.connections_for_auto_refresh():
        if not _is_due(user_id, min_age_hours):
            skipped += 1
            continue
        ok, msg = refresh_user(user_id)
        if ok:
            refreshed += 1
            print(f"[worker] user {user_id}: {msg}")
        else:
            failed += 1
            print(f"[worker] user {user_id}: {msg}")
    print(f"[worker] done — refreshed={refreshed} skipped={skipped} failed={failed}")
    return refreshed, skipped, failed


def start_scheduler():
    """Start the background daily-refresh loop (idempotent per process)."""
    if getattr(start_scheduler, "_started", False):
        return
    start_scheduler._started = True

    def loop():
        # Small initial delay so app startup isn't blocked.
        time.sleep(15)
        while True:
            try:
                refresh_all_due()
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] scheduler error: {exc}")
            time.sleep(SCHEDULER_TICK_SECONDS)

    threading.Thread(target=loop, name="rmn-scheduler", daemon=True).start()
    print(f"[worker] scheduler started (tick {SCHEDULER_TICK_SECONDS}s, "
          f"min age {REFRESH_MIN_AGE_HOURS}h)")


if __name__ == "__main__":
    # One-shot mode for external cron.
    force = "--force" in sys.argv
    refresh_all_due(min_age_hours=0 if force else REFRESH_MIN_AGE_HOURS)
