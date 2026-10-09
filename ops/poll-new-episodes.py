#!/usr/bin/env python3
"""Poll followed feeds for new episodes and send digest emails.

Intended for the podskrift-new-episodes.timer systemd unit. Imports app so the
same migrations / SSRF helpers run as the web process. Safe when EMAIL_ENABLED
is off: baselines still advance so enabling mail later does not flood inboxes.

Usage (from the app directory, as the app user):

    .venv/bin/python ops/poll-new-episodes.py
"""

from __future__ import annotations

import os
import sys

# Same guard as the test suite / other ops scripts: never touch a surprise DB.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)


def main() -> int:
    import app as A

    stats = A.run_new_episode_alerts_poll()
    se = stats.get('summary_email') or {}
    print(
        'new-episode poll: '
        f"feeds={stats.get('feeds_considered', 0)} "
        f"fetched={stats.get('feeds_fetched', 0)} "
        f"baselines={stats.get('baselines_set', 0)} "
        f"emails={stats.get('emails_sent', 0)} "
        f"episodes={stats.get('episodes_announced', 0)} "
        f"skipped={stats.get('skipped_disabled', 0)} "
        f"summary_enq={stats.get('summary_jobs_enqueued', 0)} "
        f"summary_done={se.get('jobs_done', 0)} "
        f"summary_mails={se.get('emails_sent', 0)}",
        flush=True,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
