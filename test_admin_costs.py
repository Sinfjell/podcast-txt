"""Tests for admin OpenAI / fixed-cost estimates (read-only; no billing changes)."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

_TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-costs-'), 'test.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ['SENTRY_DSN'] = ''
os.environ['POSTHOG_KEY'] = ''
os.environ['POSTHOG_HOST'] = ''
os.environ['PODSKRIFT_DISABLE_WATCHDOG'] = '1'
os.environ['PODSKRIFT_DISABLE_SHUTDOWN_HANDLERS'] = '1'
# Fixed costs off unless a test opts in.
os.environ.pop('COST_FIXED_MONTHLY_USD', None)
os.environ.pop('COST_FIXED_MONTHLY_BREAKDOWN', None)

import app as A  # noqa: E402
import admin_costs as AC  # noqa: E402


def _make_user(email, **extra):
    from models import db, User
    with A.app.app_context():
        u = User(email=email.lower(), trial_seconds_limit=A.NEW_USER_TRIAL_SECONDS,
                 **extra)
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _login(uid):
    """Match test_app._login: forge a Flask-Login session on the test client."""
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    return client


def test_audio_prices_config_matches_spec():
    assert AC.OPENAI_AUDIO_USD_PER_MIN['whisper-1'] == 0.006
    assert AC.OPENAI_AUDIO_USD_PER_MIN['gpt-4o-transcribe'] == 0.006
    assert AC.OPENAI_AUDIO_USD_PER_MIN['gpt-4o-mini-transcribe'] == 0.003
    assert AC.DEFAULT_TRANSCRIPTION_MODEL == 'whisper-1'
    # 100 minutes of whisper-1 → $0.60
    assert AC.estimate_transcription_cost_usd(100 * 60) == pytest.approx(0.60)
    assert AC.estimate_transcription_cost_usd(
        100 * 60, 'gpt-4o-mini-transcribe') == pytest.approx(0.30)


def test_task_key_source_classification():
    assert AC.task_key_source(None, None) == 'user'
    assert AC.task_key_source(None, 0) == 'user'
    assert AC.task_key_source(0, 0) == 'trial'  # settled platform, no spend
    assert AC.task_key_source(600, 0) == 'trial'
    assert AC.task_key_source(None, 300) == 'paid'
    assert AC.task_key_source(0, 300) == 'paid'
    assert AC.task_key_source(300, 300) == 'mixed'


def test_parse_fixed_monthly_breakdown(monkeypatch):
    assert AC.parse_fixed_monthly_breakdown('') == []
    assert AC.parse_fixed_monthly_breakdown('hetzner:5.59,mailgun:1') == [
        ('hetzner', 5.59), ('mailgun', 1.0),
    ]
    assert AC.parse_fixed_monthly_breakdown('bad,also:bad,ok:2') == [('ok', 2.0)]
    monkeypatch.setenv('COST_FIXED_MONTHLY_BREAKDOWN', 'hetzner:10,mailgun:2')
    monkeypatch.delenv('COST_FIXED_MONTHLY_USD', raising=False)
    total, parts = AC.fixed_monthly_costs_from_env()
    assert total == pytest.approx(12.0)
    assert parts == [('hetzner', 10.0), ('mailgun', 2.0)]
    monkeypatch.delenv('COST_FIXED_MONTHLY_BREAKDOWN', raising=False)
    monkeypatch.setenv('COST_FIXED_MONTHLY_USD', '7.5')
    total, parts = AC.fixed_monthly_costs_from_env()
    assert total == pytest.approx(7.5)


def test_summary_cost_prefers_stored_then_tokens_then_fallback():
    assert AC.estimate_summary_cost_usd(
        summary_status='ready',
        summary_cost_usd_est=0.0123,
        summary_model='gpt-4o-mini',
        summary_prompt_tokens=1000,
        summary_completion_tokens=100,
    ) == pytest.approx(0.0123)

    token_est = AC.estimate_summary_cost_usd(
        summary_status='ready',
        summary_cost_usd_est=None,
        summary_model='gpt-4o-mini',
        summary_prompt_tokens=1_000_000,
        summary_completion_tokens=0,
    )
    assert token_est == pytest.approx(0.15)  # $0.15 / 1M input

    assert AC.estimate_summary_cost_usd(
        summary_status='ready',
        summary_cost_usd_est=None,
        summary_model=None,
        summary_prompt_tokens=None,
        summary_completion_tokens=None,
        has_summary_json=True,
    ) == pytest.approx(AC.SUMMARY_FALLBACK_USD)

    assert AC.estimate_summary_cost_usd(
        summary_status='skipped',
        summary_cost_usd_est=None,
        summary_model=None,
        summary_prompt_tokens=None,
        summary_completion_tokens=None,
    ) == 0.0


# Fixture all-time OpenAI estimate (whisper-1 @ $0.006/min), used as the PR
# sanity number. See test_collect_costs_fixture_all_time_sanity.
FIXTURE_ALL_TIME_OPENAI_USD = 0.82


def test_collect_costs_fixture_all_time_sanity(monkeypatch):
    """Seed known tasks; assert all-time OpenAI estimate (the PR sanity number).

    Fixture (whisper-1 @ $0.006/min):
      - trial completed 60 min            → $0.36
      - paid completed 30 min             → $0.18
      - trial+paid mixed 20+10 min        → $0.18
      - failed trial that hit OpenAI 15m  → $0.09
      - failed trial never hit (0 spent)  → $0.00
      - BYOK completed 120 min            → $0.00 cost, 120 BYOK minutes
      - ready summary with stored $0.01   → $0.01
    Delta all-time est. OpenAI (excl. fixed) = $0.82
    """
    from models import db, User, TranscriptionTask, CreditPurchase
    import uuid as _uuid

    monkeypatch.delenv('COST_FIXED_MONTHLY_USD', raising=False)
    monkeypatch.delenv('COST_FIXED_MONTHLY_BREAKDOWN', raising=False)
    prefix = 'costfix-%s' % _uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)

    with A.app.app_context():
        before = AC.collect_costs(db, chart_days=30)['windows']['all']

        u_trial = User(email=f'{prefix}-trial@test.com',
                       trial_seconds_limit=10_000)
        u_trial.set_password('password123')
        u_paid = User(email=f'{prefix}-paid@test.com',
                      trial_seconds_limit=10_000)
        u_paid.set_password('password123')
        u_byok = User(email=f'{prefix}-byok@test.com',
                      trial_seconds_limit=10_000,
                      openai_api_key='sk-test-byok')
        u_byok.set_password('password123')
        db.session.add_all([u_trial, u_paid, u_byok])
        db.session.commit()

        tasks = [
            TranscriptionTask(
                id=f'{prefix}-t1', user_id=u_trial.id, episode_title='Trial 60',
                status='completed', audio_duration=3600.0,
                trial_seconds_charged=3600, paid_seconds_charged=0,
                trial_settled=True,
                started_at=now - timedelta(days=3),
                completed_at=now - timedelta(days=3),
                summary_status='ready', summary_cost_usd_est=0.01,
                summary_model='gpt-4o-mini',
            ),
            TranscriptionTask(
                id=f'{prefix}-t2', user_id=u_paid.id, episode_title='Paid 30',
                status='completed', audio_duration=1800.0,
                trial_seconds_charged=0, paid_seconds_charged=1800,
                trial_settled=True,
                started_at=now - timedelta(days=2),
                completed_at=now - timedelta(days=2),
            ),
            TranscriptionTask(
                id=f'{prefix}-t3', user_id=u_trial.id, episode_title='Mixed',
                status='completed', audio_duration=1800.0,
                trial_seconds_charged=1200, paid_seconds_charged=600,
                trial_settled=True,
                started_at=now - timedelta(days=1),
                completed_at=now - timedelta(days=1),
            ),
            TranscriptionTask(
                id=f'{prefix}-t4', user_id=u_trial.id, episode_title='Fail hit',
                status='error', audio_duration=1800.0,
                trial_seconds_charged=900, paid_seconds_charged=0,
                trial_settled=True, chunk_index=0, chunk_total=2,
                error_message='OpenAI had a server error.',
                started_at=now - timedelta(hours=5),
                completed_at=now - timedelta(hours=5),
            ),
            TranscriptionTask(
                id=f'{prefix}-t5', user_id=u_trial.id, episode_title='Fail miss',
                status='error', audio_duration=1800.0,
                trial_seconds_charged=0, paid_seconds_charged=0,
                trial_settled=True, chunk_index=None,
                error_message='Download failed.',
                started_at=now - timedelta(hours=4),
                completed_at=now - timedelta(hours=4),
            ),
            TranscriptionTask(
                id=f'{prefix}-t6', user_id=u_byok.id, episode_title='BYOK 120',
                status='completed', audio_duration=7200.0,
                trial_seconds_charged=None, paid_seconds_charged=None,
                trial_settled=False,
                started_at=now - timedelta(hours=3),
                completed_at=now - timedelta(hours=3),
            ),
        ]
        db.session.add_all(tasks)
        db.session.add(CreditPurchase(
            user_id=u_paid.id,
            stripe_session_id=f'cs_{prefix}',
            amount_cents=900, amount_total_cents=900, currency='usd',
            minutes=300, status='credited',
            created_at=now - timedelta(days=2),
        ))
        db.session.commit()

        after = AC.collect_costs(db, chart_days=30)['windows']['all']

    delta_openai = after['openai_usd'] - before['openai_usd']
    assert delta_openai == pytest.approx(FIXTURE_ALL_TIME_OPENAI_USD, abs=0.001)
    assert after['openai_trial_usd'] - before['openai_trial_usd'] == pytest.approx(
        0.36 + 0.12 + 0.09, abs=0.001)  # 60 + 20 + 15 min trial
    assert after['openai_paid_usd'] - before['openai_paid_usd'] == pytest.approx(
        0.18 + 0.06, abs=0.001)  # 30 + 10 min paid
    assert after['openai_summary_usd'] - before['openai_summary_usd'] == pytest.approx(
        0.01, abs=0.0001)
    assert after['byok_minutes'] - before['byok_minutes'] == pytest.approx(120.0, abs=0.1)
    assert after['failed_with_openai_count'] - before['failed_with_openai_count'] == 1
    assert after['revenue_usd'] - before['revenue_usd'] == pytest.approx(9.0, abs=0.01)
    assert after['platform_trial_minutes'] - before['platform_trial_minutes'] == pytest.approx(
        95.0, abs=0.1)
    assert after['platform_paid_minutes'] - before['platform_paid_minutes'] == pytest.approx(
        40.0, abs=0.1)
    assert after['paid_users'] >= before['paid_users'] + 1
    assert after['active_users'] >= before['active_users'] + 2


def test_collect_costs_prorates_fixed_monthly(monkeypatch):
    from models import db
    import calendar
    from admin_dashboard import ADMIN_TZ

    monkeypatch.setenv('COST_FIXED_MONTHLY_BREAKDOWN', 'hetzner:30,mailgun:0')
    monkeypatch.delenv('COST_FIXED_MONTHLY_USD', raising=False)

    with A.app.app_context():
        costs = AC.collect_costs(db, chart_days=7)
    today = datetime.now(ADMIN_TZ).date()
    days = calendar.monthrange(today.year, today.month)[1]
    daily = 30.0 / days
    assert costs['fixed_daily_usd'] == pytest.approx(daily, abs=0.0001)
    assert costs['windows']['today']['fixed_usd'] == pytest.approx(daily, abs=0.0001)
    assert costs['windows']['7d']['fixed_usd'] == pytest.approx(daily * 7, abs=0.001)


def test_admin_costs_section_renders_for_allowlisted(monkeypatch):
    monkeypatch.setenv('ADMIN_EMAILS', 'costs-admin@test.com')
    uid = _make_user('costs-admin@test.com')
    client = _login(uid)
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'id="adminCosts"' in body
    assert 'data-costs="windows"' in body
    assert 'Est. total cost' in body
    assert 'BYOK minutes (excl.)' in body
    assert 'estimate' in body.lower()
    assert 'chartCostsDaily' in body


def test_admin_costs_still_404_for_non_admin():
    uid = _make_user('not-costs-admin@example.com')
    client = _login(uid)
    assert client.get('/admin').status_code == 404
