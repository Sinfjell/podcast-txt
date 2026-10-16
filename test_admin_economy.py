"""Economy overview: revenue, income, cost and result per UTC day."""
import os
import tempfile
from datetime import datetime, timezone

import pytest

if not os.environ.get('DATABASE_URL'):
    _TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-eco-'), 'test.db')
    os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ.setdefault('SENTRY_DSN', '')
os.environ.setdefault('POSTHOG_KEY', '')
os.environ.setdefault('POSTHOG_HOST', '')
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')

import app as A  # noqa: E402
import admin_economy as E  # noqa: E402
from models import CreditPurchase, TranscriptionTask, User, db  # noqa: E402

# A window far from "now" so rows other test modules leave behind never land in it.
START = datetime(2024, 3, 10).date()
END = datetime(2024, 3, 13).date()


def _dt(day, hour=12):
    return datetime(2024, 3, day, hour, 0, 0)


def _user(email, created_day):
    u = User(email=email, trial_seconds_limit=0)
    u.set_password('password123')
    u.created_at = _dt(created_day)
    db.session.add(u)
    db.session.commit()
    return u.id


def _purchase(uid, sid, day, *, total=500, tax=100, minutes=300, refunded=0,
              status='credited', pi=None, country='NO'):
    db.session.add(CreditPurchase(
        user_id=uid, stripe_session_id=sid, stripe_payment_intent_id=pi,
        amount_cents=total, amount_total_cents=total, amount_tax_cents=tax,
        amount_refunded_cents=refunded, minutes=minutes, status=status,
        customer_country=country, created_at=_dt(day)))
    db.session.commit()


def _task(uid, tid, day, *, trial=0, paid=0, minutes=0):
    db.session.add(TranscriptionTask(
        id=tid, user_id=uid, episode_title='x', status='completed',
        started_at=_dt(day), completed_at=_dt(day),
        audio_duration=minutes * 60.0,
        trial_seconds_charged=trial, paid_seconds_charged=paid))
    db.session.commit()


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setenv('COST_FIXED_MONTHLY_USD', '31')
    monkeypatch.delenv('COST_FIXED_MONTHLY_BREAKDOWN', raising=False)
    with A.app.app_context():
        payer = _user('eco-payer@example.com', 10)
        other = _user('eco-other@example.com', 11)
        _purchase(payer, 'eco-s1', 12, pi='pi_eco1')
        _task(payer, 'eco-t1', 12, trial=3600, minutes=60)
        _task(payer, 'eco-t2', 12, paid=1800, minutes=30)
        _task(other, 'eco-t3', 11, trial=600, minutes=10)
    # Not yielded inside the app context: a held context would cache the
    # anonymous user and make every later request in the test look logged out.
    yield {'payer': payer, 'other': other}
    with A.app.app_context():
        db.session.query(CreditPurchase).filter(
            CreditPurchase.stripe_session_id.like('eco-%')).delete(synchronize_session=False)
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.id.like('eco-%')).delete(synchronize_session=False)
        db.session.query(User).filter(User.email.like('eco-%')).delete(synchronize_session=False)
        db.session.commit()


def _actual(days):
    return lambda s, e: {'ok': True, 'days': days}


def _no_actual(s, e):
    return {'ok': False, 'error': 'no key', 'missing_key': True}


def test_money_per_day_with_actual_cost(seeded):
    with A.app.app_context():
        out = E.collect_economy(
            db, {'ok': False}, start=START, end=END, today=END,
            actual_fetcher=_actual({'2024-03-12': {'transcription': 0.9}}))
    i = out['days'].index('2024-03-12')
    s = out['series']
    assert s['gross'][i] == pytest.approx(5.0)
    assert s['vat'][i] == pytest.approx(1.0)
    assert s['revenue'][i] == pytest.approx(4.0)
    fee = 0.30 + 5.0 * 0.064  # no Stripe row: the model in pricing_volume
    assert s['fees'][i] == pytest.approx(fee)
    assert s['income'][i] == pytest.approx(4.0 - fee)
    # 60 trial + 30 paid minutes that day: the $0.90 splits 2/3 trial, 1/3 paid.
    assert s['cost_trial'][i] == pytest.approx(0.6)
    assert s['cost_paid'][i] == pytest.approx(0.3)
    assert s['cost_fixed'][i] == pytest.approx(1.0)  # $31 / 31 days in March
    assert s['result'][i] == pytest.approx(4.0 - fee - 0.9 - 1.0)
    assert out['sources']['cost'] == 'actual'
    assert out['sources']['fees'] == 'model'
    # a day with no activity still carries fixed cost and nothing else
    j = out['days'].index('2024-03-13')
    assert s['revenue'][j] == 0 and s['cost'][j] == pytest.approx(1.0)


def test_estimate_used_without_admin_key(seeded):
    with A.app.app_context():
        out = E.collect_economy(db, {'ok': False}, start=START, end=END,
                                today=END, actual_fetcher=_no_actual)
    i = out['days'].index('2024-03-12')
    # 90 minutes at $0.006 on 2024-03-12 (list price estimate).
    assert out['series']['cost_trial'][i] + out['series']['cost_paid'][i] == pytest.approx(0.54)
    assert out['sources']['cost'] == 'estimate'
    assert out['sources']['cost_error'] == 'no key'


def test_stripe_fee_used_when_balance_transaction_matches(seeded):
    ts = int(datetime(2024, 3, 12, 12, tzinfo=timezone.utc).timestamp())
    bts = [{'created': ts, 'type': 'charge', 'currency': 'usd', 'amount': 500,
            'fee': 61, 'net': 439, 'exchange_rate': None, 'source_currency': None,
            'payment_intent': 'pi_eco1', 'fee_details': [('stripe_fee', 61)]}]
    with A.app.app_context():
        out = E.collect_economy(db, {'ok': True, 'bts_norm': bts}, start=START,
                                end=END, today=END, actual_fetcher=_no_actual)
    p = out['purchases'][0]
    assert p['fee_src'] == 'stripe' and p['fee'] == pytest.approx(0.61)
    assert out['sources']['fees'] == 'stripe'


def test_refund_reduces_revenue_pro_rata(seeded):
    with A.app.app_context():
        _purchase(seeded['other'], 'eco-s2', 13, total=1000, tax=200, refunded=500,
                  status='refunded')
        out = E.collect_economy(db, {'ok': False}, start=START, end=END,
                                today=END, actual_fetcher=_no_actual)
    j = out['days'].index('2024-03-13')
    # $10 paid, $2 VAT, half refunded: $8 ex VAT, $4 kept, $4 refunded.
    assert out['series']['revenue'][j] == pytest.approx(4.0)
    assert out['series']['refunds'][j] == pytest.approx(4.0)


def test_first_purchase_flag_and_plan(seeded):
    with A.app.app_context():
        _purchase(seeded['payer'], 'eco-s3', 13)
        out = E.collect_economy(db, {'ok': False}, start=START, end=END,
                                today=END, actual_fetcher=_no_actual)
    by_day = {p['d']: p for p in out['purchases']}
    assert by_day['2024-03-12']['new'] is True
    assert by_day['2024-03-13']['new'] is False
    assert by_day['2024-03-12']['plan'] == 'legacy'


def test_activity_splits_paying_users(seeded):
    with A.app.app_context():
        out = E.collect_economy(db, {'ok': False}, start=START, end=END,
                                today=END, actual_fetcher=_no_actual)
    a, days = out['activity'], out['days']
    i10, i11, i12 = days.index('2024-03-10'), days.index('2024-03-11'), days.index('2024-03-12')
    assert a['signups'][i10] == 1 and a['signups_paying'][i10] == 1
    assert a['signups'][i11] == 1 and a['signups_paying'][i11] == 0
    assert a['transcripts'][i12] == 2 and a['transcripts_paying'][i12] == 2
    assert a['transcripts'][i11] == 1 and a['transcripts_paying'][i11] == 0
    assert a['minutes'][i12] == pytest.approx(90.0)
    assert a['new_payers'][i12] == 1
    assert out['paying_users'] >= 1


def test_payers_list_has_net_paid_and_usage(seeded):
    with A.app.app_context():
        out = E.collect_economy(db, {'ok': False}, start=START, end=END,
                                today=END, actual_fetcher=_no_actual)
    row = [p for p in out['payers'] if p['email'] == 'eco-payer@example.com'][0]
    assert row['purchases'] == 1 and row['paid_usd'] == pytest.approx(4.0)
    assert row['minutes_bought'] == 300 and row['transcripts'] == 2
    assert not [p for p in out['payers'] if p['email'] == 'eco-other@example.com']


def test_economy_page_admin_only(seeded, monkeypatch):
    monkeypatch.setenv('ADMIN_EMAILS', 'eco-payer@example.com')
    anon = A.app.test_client()
    assert anon.get('/admin/economy').status_code == 404
    with A.app.test_client() as c:
        with c.session_transaction() as sess:
            sess['_user_id'] = str(seeded['payer'])
            sess['_fresh'] = True
        resp = c.get('/admin/economy')
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'id="econData"' in body and 'admin-economy.js' in body
        assert 'noindex' in body
        assert 'id="econResultChart"' in body
        assert 'id="payers"' in body and 'href="#payers"' in body
        assert 'eco-payer@example.com' in body
        assert c.get('/admin').status_code == 200
        assert 'href="/admin/economy"' in c.get('/admin').data.decode()
    monkeypatch.setenv('ADMIN_EMAILS', 'someone-else@example.com')
    with A.app.test_client() as c:
        with c.session_transaction() as sess:
            sess['_user_id'] = str(seeded['other'])
            sess['_fresh'] = True
        assert c.get('/admin/economy').status_code == 404
