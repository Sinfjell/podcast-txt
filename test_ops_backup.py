"""Tests for nightly DB backup ops (timer, staleness check, script syntax)."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
OPS = ROOT / 'ops'
UTC = timezone.utc


def _load_metrics_mod():
    path = OPS / 'notion-daily-metrics.py'
    spec = importlib.util.spec_from_file_location('notion_daily_metrics_backup', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_backup_timer_is_2345_utc_and_persistent():
    text = (OPS / 'podskrift-backup.timer').read_text(encoding='utf-8')
    assert 'OnCalendar=*-*-* 23:45:00 UTC' in text
    assert 'Persistent=true' in text


def test_backup_service_wires_onfailure():
    text = (OPS / 'podskrift-backup.service').read_text(encoding='utf-8')
    assert 'OnFailure=podskrift-backup-failed.service' in text
    failed = (OPS / 'podskrift-backup-failed.service').read_text(encoding='utf-8')
    assert 'ops/report-backup-failure.py' in failed
    # The failed unit itself must not recurse.
    assert not any(
        line.startswith('OnFailure=') for line in failed.splitlines()
    )


def test_backup_db_sh_syntax():
    script = OPS / 'backup-db.sh'
    result = subprocess.run(
        ['bash', '-n', str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_backup_db_sh_sets_umask_077():
    text = (OPS / 'backup-db.sh').read_text(encoding='utf-8')
    assert 'umask 077' in text
    assert 'chmod 600' in text


def test_newest_backup_mtime_and_staleness_fresh():
    mod = _load_metrics_mod()
    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        fresh = backup_dir / 'podcast-20261009-234500.db.gz'
        fresh.write_bytes(b'x')
        now = datetime.now(UTC)
        lines = mod.backup_staleness_warning_lines(
            backup_dir, max_age_hours=26, now=now,
        )
        assert lines == []
        mtime = mod.newest_backup_mtime(backup_dir)
        assert mtime is not None
        assert abs(mtime - fresh.stat().st_mtime) < 1


def test_backup_staleness_warns_when_older_than_26h():
    mod = _load_metrics_mod()
    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        stale = backup_dir / 'podcast-20261008-000000.db.gz'
        stale.write_bytes(b'x')
        old = time.time() - (27 * 3600)
        os.utime(stale, (old, old))
        now = datetime.now(UTC)
        lines = mod.backup_staleness_warning_lines(
            backup_dir, max_age_hours=26, now=now,
        )
        assert len(lines) == 1
        assert 'eldre enn 26 timer' in lines[0]
        assert 'podskrift-backup.timer' in lines[0]


def test_backup_staleness_warns_when_missing():
    mod = _load_metrics_mod()
    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        lines = mod.backup_staleness_warning_lines(backup_dir, max_age_hours=26)
        assert len(lines) == 1
        assert 'Ingen DB-backup' in lines[0]


def test_backup_staleness_boundary_exactly_26h_is_ok():
    """Age == max_age_hours must not warn (only strictly older)."""
    mod = _load_metrics_mod()
    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        path = backup_dir / 'podcast-edge.db.gz'
        path.write_bytes(b'x')
        now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
        edge = (now - timedelta(hours=26)).timestamp()
        os.utime(path, (edge, edge))
        assert mod.backup_staleness_warning_lines(
            backup_dir, max_age_hours=26, now=now,
        ) == []
        # One second over the limit warns.
        os.utime(path, (edge - 1, edge - 1))
        assert mod.backup_staleness_warning_lines(
            backup_dir, max_age_hours=26, now=now,
        )


def test_umask_077_yields_mode_600(tmp_path):
    """Documented contract: with umask 077, new files are owner-only (600)."""
    script = f'''
set -euo pipefail
umask 077
OUT="{tmp_path / 'podcast-test.db.gz'}"
printf 'x' > "$OUT"
chmod 600 "$OUT"
stat -c '%a' "$OUT"
'''
    result = subprocess.run(
        ['bash', '-c', script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == '600'
    mode = (tmp_path / 'podcast-test.db.gz').stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_report_backup_failure_exits_nonzero_without_dsn(monkeypatch):
    monkeypatch.setenv('SENTRY_DSN', '')
    # Avoid importing app-side dotenv pollution; run as a subprocess like prod.
    env = os.environ.copy()
    env['SENTRY_DSN'] = ''
    # Point dotenv at an empty dir so a developer .env cannot supply a DSN.
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, str(OPS / 'report-backup-failure.py')],
            cwd=tmp,
            env={**env, 'PYTHONPATH': str(ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 1
    assert 'SENTRY_DSN is not set' in result.stderr
