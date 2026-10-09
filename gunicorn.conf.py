"""Gunicorn defaults for Podskrift production.

Used as: gunicorn -c gunicorn.conf.py --bind 127.0.0.1:5002 app:app
(see ops/podskrift.service.example). CLI flags still override these values.
"""

workers = 2
threads = 4
timeout = 300
# Finish in-flight HTTP requests on SIGTERM before the worker exits.
graceful_timeout = 120


def on_starting(server):
    """Stamp this server generation's start time in the arbiter, pre-fork.

    Workers inherit it; app.resume_interrupted_tasks treats only jobs whose
    heartbeat predates it as orphaned. Without it (bare CLI, scripts), boot
    resume is skipped and only the threshold-based stale sweep runs.
    """
    import os
    import time
    os.environ['PODSKRIFT_SERVER_STARTED_AT'] = repr(time.time())


def post_worker_init(worker):
    """After gunicorn installs its signals, chain our shutdown flag."""
    try:
        import app as A
        A.install_shutdown_handlers()
    except Exception:  # noqa: BLE001 — never prevent the worker from serving
        pass
