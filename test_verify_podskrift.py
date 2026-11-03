"""Prod guards and helpers for verify-podskrift.

These tests exercise the REAL Flask loader (``import app`` after env is set),
not only the verify helpers. They must never touch ``data/podcast.db``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent
SKILL = REPO / '.claude' / 'skills' / 'verify-podskrift'
PKG = SKILL / 'verify_podskrift'
BIN = REPO / 'bin' / 'verify-podskrift'

sys.path.insert(0, str(SKILL))

from verify_podskrift.env import (  # noqa: E402
    GuardError,
    assert_sqlite_inside_verify_dir,
    assert_stripe_keys_safe,
    build_verify_environ,
)
from verify_podskrift.paths import PORT_MAX, PORT_MIN, default_port  # noqa: E402


def test_port_range_is_reserved_and_unused_by_siblings():
    assert PORT_MIN == 5502
    assert PORT_MAX == 5599
    # Sibling verify skills: 8890–8988, 3310–3399, 54339 — must not overlap.
    assert not (8890 <= PORT_MIN <= 8988)
    assert not (3310 <= PORT_MIN <= 3399)
    assert PORT_MIN != 54339
    port = default_port()
    assert PORT_MIN <= port <= PORT_MAX


def test_sqlite_guard_refuses_repo_data_db(tmp_path):
    bad = f'sqlite:///{REPO / "data" / "podcast.db"}'
    with pytest.raises(GuardError, match='outside verify dir'):
        assert_sqlite_inside_verify_dir(bad, verify_root=tmp_path / 'verify-home')


def test_sqlite_guard_accepts_path_inside_verify_home(tmp_path):
    root = tmp_path / 'verify-home'
    db = root / 'worktrees' / 'abc' / 'podcast.db'
    db.parent.mkdir(parents=True)
    db.write_text('')
    path = assert_sqlite_inside_verify_dir(
        f'sqlite:///{db}', verify_root=root)
    assert path == db.resolve()


def test_stripe_guard_refuses_live_keys():
    with pytest.raises(GuardError, match='live'):
        assert_stripe_keys_safe({'STRIPE_SECRET_KEY': 'sk_live_fake'})
    with pytest.raises(GuardError, match='live'):
        assert_stripe_keys_safe({'STRIPE_SECRET_KEY': 'rk_live_fake'})


def test_stripe_guard_allows_test_or_empty():
    assert assert_stripe_keys_safe({'STRIPE_SECRET_KEY': ''})['STRIPE_SECRET_KEY'] == ''
    assert assert_stripe_keys_safe(
        {'STRIPE_SECRET_KEY': 'sk_test_123', 'STRIPE_WEBHOOK_SECRET': 'whsec_x'}
    )['STRIPE_SECRET_KEY'].startswith('sk_test_')


def test_build_verify_environ_blanks_vendor_keys(tmp_path, monkeypatch):
    vh = (tmp_path / 'vh').resolve()
    monkeypatch.setenv('PODSKRIFT_VERIFY_HOME', str(vh))
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-should-be-wiped')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_should_be_wiped')
    monkeypatch.setenv('SENTRY_DSN', 'https://should-be-wiped')
    monkeypatch.setenv('TOGETHER_API_KEY', 'tg-wipe')
    monkeypatch.setenv('SONIOX_API_KEY', 'sx-wipe')
    monkeypatch.setenv('GROQ_API_KEY', 'gq-wipe')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-real-looking')
    env = build_verify_environ(port=5502, stt_mode='stub')
    assert env['MAILGUN_API_KEY'] == ''
    assert env['POSTHOG_KEY'] == ''
    assert env['SENTRY_DSN'] == ''
    assert env['TOGETHER_API_KEY'] == ''
    assert env['SONIOX_API_KEY'] == ''
    assert env['GROQ_API_KEY'] == ''
    assert env['OPENAI_API_KEY'].startswith('sk-verify-stub')
    assert env['TZ'] == 'Europe/Oslo'
    assert env['STT_SHADOW_RATE'] == '0'
    assert env['SCRIBE_SHADOW_PCT'] == '0'
    assert_sqlite_inside_verify_dir(env['DATABASE_URL'], verify_root=vh)


def test_full_stt_refused_in_ci(tmp_path, monkeypatch):
    vh = (tmp_path / 'vh').resolve()
    monkeypatch.setenv('PODSKRIFT_VERIFY_HOME', str(vh))
    monkeypatch.setenv('CI', 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-from-process')
    with pytest.raises(GuardError, match='CI'):
        build_verify_environ(port=5502, stt_mode='full',
                             real_openai_key='sk-from-process')


def test_empty_string_keys_win_over_dotenv_in_real_flask_loader(tmp_path):
    """Subprocess: write a .env with real-looking keys, set empty env, import app.

    Asserts the Flask process sees empty Mailgun/PostHog/Sentry/vendor keys —
    not merely that our helper blanked a dict.
    """
    work = tmp_path / 'loader'
    work.mkdir()
    db = work / 'podcast.db'
    (work / '.env').write_text(textwrap.dedent('''\
        MAILGUN_API_KEY=key-from-dotenv-file
        POSTHOG_KEY=phc_from_dotenv_file
        SENTRY_DSN=https://dsn-from-dotenv@example.com/1
        TOGETHER_API_KEY=together-from-dotenv
        SONIOX_API_KEY=soniox-from-dotenv
        GROQ_API_KEY=groq-from-dotenv
        OPENAI_API_KEY=sk-from-dotenv-file
        ELEVENLABS_API_KEY=el-from-dotenv
    '''), encoding='utf-8')

    script = textwrap.dedent(f'''\
        import os, json, sys
        os.chdir({str(work)!r})
        for k in ("MAILGUN_API_KEY","POSTHOG_KEY","SENTRY_DSN",
                  "TOGETHER_API_KEY","SONIOX_API_KEY","GROQ_API_KEY",
                  "ELEVENLABS_API_KEY"):
            os.environ[k] = ""
        os.environ["OPENAI_API_KEY"] = "sk-verify-stub-not-a-real-key"
        os.environ["PODSKRIFT_ENV"] = "dev"
        os.environ["DATABASE_URL"] = "sqlite:///{db}"
        os.environ["SECRET_KEY"] = "verify-loader-test"
        os.environ["EMAIL_ENABLED"] = "0"
        os.environ["MCP_ENABLED"] = "0"
        os.environ["STT_SHADOW_RATE"] = "0"
        os.environ["SCRIBE_SHADOW_PCT"] = "0"
        sys.path.insert(0, {str(REPO)!r})
        import app as A
        print(json.dumps({{
            "mailgun": os.environ.get("MAILGUN_API_KEY"),
            "posthog": os.environ.get("POSTHOG_KEY"),
            "sentry": os.environ.get("SENTRY_DSN"),
            "together": os.environ.get("TOGETHER_API_KEY"),
            "soniox": os.environ.get("SONIOX_API_KEY"),
            "groq": os.environ.get("GROQ_API_KEY"),
            "eleven": os.environ.get("ELEVENLABS_API_KEY"),
            "openai_env": os.environ.get("OPENAI_API_KEY"),
            "openai_global": A.GLOBAL_OPENAI_KEY,
            "db": A.app.config["SQLALCHEMY_DATABASE_URI"],
        }}))
    ''')
    proc = subprocess.run(
        [sys.executable, '-c', script],
        cwd=str(work),
        capture_output=True,
        text=True,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith('{')]
    assert lines, proc.stdout[-2000:]
    data = json.loads(lines[-1])
    assert data['mailgun'] == ''
    assert data['posthog'] == ''
    assert data['sentry'] == ''
    assert data['together'] == ''
    assert data['soniox'] == ''
    assert data['groq'] == ''
    assert data['eleven'] == ''
    assert data['openai_env'].startswith('sk-verify-stub')
    assert data['openai_global'].startswith('sk-verify-stub')
    assert str(db) in data['db']


def test_cli_help_and_dry_run_up(tmp_path):
    help_p = subprocess.run(
        [sys.executable, str(BIN), '--help'],
        cwd=str(REPO), capture_output=True, text=True, timeout=30)
    assert help_p.returncode == 0
    assert 'doctor' in help_p.stdout
    assert 'transcribe' in help_p.stdout

    dry = subprocess.run(
        [sys.executable, str(BIN), 'up', '--dry-run', '--port', '5502'],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
        env={**os.environ, 'PODSKRIFT_VERIFY_HOME': str(tmp_path / 'vh')},
    )
    assert dry.returncode == 0, dry.stderr
    payload = json.loads(dry.stdout)
    assert payload['ok'] is True
    assert payload['dry_run'] is True
    assert payload['would_start']['tz'] == 'Europe/Oslo'


def test_cli_source_has_no_pkill_ssh_or_hetzner():
    text = (PKG / 'cli.py').read_text(encoding='utf-8').lower()
    assert 'pkill' not in text
    assert 'killall' not in text
    assert 'hetzner' not in text
    # start_new_session is fine; bare ssh must not appear as a tool.
    assert 'ssh ' not in text
    assert 'scp ' not in text
