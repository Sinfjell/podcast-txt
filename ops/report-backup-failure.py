#!/usr/bin/env python3
"""Report a failed podskrift-backup.service run to Sentry.

Called by systemd OnFailure= via ops/podskrift-backup-failed.service. Loads
SENTRY_DSN from the app's .env the same way the app and ops/sentry-check.py
do (load_dotenv + observability.init_sentry). No new secrets in the repo.

    sudo -u podskrift .venv/bin/python ops/report-backup-failure.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sentry_sdk  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import observability  # noqa: E402


def main() -> int:
    load_dotenv()
    if not observability.init_sentry():
        print(
            'SENTRY_DSN is not set (or sentry-sdk is missing); '
            'backup failure was not reported.',
            file=sys.stderr,
        )
        return 1
    event_id = sentry_sdk.capture_message(
        'Podskrift DB backup failed (podskrift-backup.service)',
        level='error',
        fingerprint=['podskrift-backup-failed'],
    )
    sentry_sdk.flush(timeout=10)
    print(f'sent backup-failure event {event_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
