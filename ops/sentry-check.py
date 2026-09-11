#!/usr/bin/env python3
"""Send one deliberate test exception to Sentry and exit.

Proves the DSN, the network path and the scrubbing without importing app.py --
which would run the schema migrations and the stale-task sweep against the live
database. Run from the app directory with the app's venv:

    sudo -u podskrift .venv/bin/python ops/sentry-check.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sentry_sdk  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import observability  # noqa: E402


def main():
    load_dotenv()
    if not observability.init_sentry():
        print('SENTRY_DSN is not set (or sentry-sdk is missing); nothing sent.')
        return 1
    try:
        # The fake key proves the redactor runs on the real send path: it must
        # arrive in Sentry as [redacted].
        raise RuntimeError('Podskrift Sentry check -- sk-check-not-a-real-key-000000')
    except RuntimeError as exc:
        event_id = sentry_sdk.capture_exception(exc, tags={'check': 'ops/sentry-check.py'})
    sentry_sdk.flush(timeout=10)
    print(f'sent test event {event_id}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
