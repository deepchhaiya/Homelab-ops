#!/usr/bin/env python3
"""Delete old Gotify messages per application (Gotify itself has no retention).

Runs on CT203 from gotify-prune.timer. It goes through the Gotify REST API, never the
SQLite file, so it is safe while Gotify is running.

Dry run by default: prints what it would delete. Pass --apply to delete.

Env:
  GOTIFY_URL           default http://localhost:8800
  GOTIFY_PRUNE_TOKEN   a Gotify *client* token (Gotify UI > Clients > create
                       "prune-job"); app tokens can only send, not read or delete.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Retention in days, by exact Gotify application name. Apps not listed get DEFAULT_DAYS.
RETENTION_DAYS = {
    # high-volume feeds: only recent items are useful
    "Miniflux Curator Deals": 7,
    "Filtered Deals": 7,
    "Miniflux Randoms": 7,
    # curator summaries
    "miniflux-curator": 14,
    # routine info
    "Beszel-Monitoring": 30,
    "Hindsight Memory Notifications": 30,
    "n8n HA workflows": 30,
    "RSS Feeds": 30,
    "Hermes ssh_exec": 30,
    "Job Application": 30,
    # alerts, errors, backups, summaries: keep a quarter of history
    "Grafana · Critical": 90,
    "Grafana · Security": 90,
    "Grafana · Kubernetes": 90,
    "Grafana · Network": 90,
    "Grafana · Infrastructure": 90,
    "n8n Errors": 90,
    "n8n Backup Worflow Notifications": 90,
    "Gmail Summaries": 90,
}
DEFAULT_DAYS = 90
PAGE = 200


def api(method, path, token, base):
    req = urllib.request.Request(base + path, method=method, headers={"X-Gotify-Key": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def parse_date(s):
    # Gotify returns RFC3339 with nanoseconds; trim the fraction to microseconds.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s)


def old_message_ids(app_id, cutoff, token, base):
    """Yield ids of the app's messages older than cutoff (pages newest -> oldest)."""
    since = None
    while True:
        q = f"/application/{app_id}/message?limit={PAGE}" + (f"&since={since}" if since else "")
        page = api("GET", q, token, base)
        msgs = page.get("messages", [])
        for m in msgs:
            if parse_date(m["date"]) < cutoff:
                yield m["id"]
        if len(msgs) < PAGE:
            return
        since = page["paging"]["since"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()

    base = os.environ.get("GOTIFY_URL", "http://localhost:8800").rstrip("/")
    token = os.environ.get("GOTIFY_PRUNE_TOKEN")
    if not token:
        sys.exit("GOTIFY_PRUNE_TOKEN is not set (needs a Gotify client token)")

    now = datetime.now(timezone.utc)
    total = 0
    errors = 0
    for app in sorted(api("GET", "/application", token, base), key=lambda a: a["id"]):
        days = RETENTION_DAYS.get(app["name"], DEFAULT_DAYS)
        cutoff = now - timedelta(days=days)
        ids = list(old_message_ids(app["id"], cutoff, token, base))
        for mid in ids if args.apply else []:
            try:
                api("DELETE", f"/message/{mid}", token, base)
            except urllib.error.HTTPError as e:
                errors += 1
                print(f"  delete {mid} failed: {e.code}", file=sys.stderr)
        total += len(ids)
        verb = "deleted" if args.apply else "would delete"
        print(f"{app['name'][:34]:34} keep {days:>2}d  {verb} {len(ids)}")

    print(f"total {'deleted' if args.apply else 'to delete'}: {total}" + (f", errors: {errors}" if errors else ""))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
