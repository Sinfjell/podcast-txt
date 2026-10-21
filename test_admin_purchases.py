"""Canonical purchase accounting: counted vs refunded/disputed, exclude internal."""
import os
import tempfile
from datetime import datetime

import pytest

if not os.environ.get('DATABASE_URL'):
    _TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-ap-'), 'test.db')
    os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ.setdefault('SENTRY_DSN', '')
os.environ.setdefault('POSTHOG_KEY', '')
os.environ.setdefault('POSTHOG_HOST', '')
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')

import app as A  # noqa: E402
import admin_purchases as AP  # noqa: E402
from models import CreditPurchase, User, db  # noqa: E402


def _user(email):
    u = User(email=email, trial_seconds_limit=0)
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    return u


def _buy(uid, sid, *, total=500, tax=100, status='credited', refunded=0,
         created=None):
    row = CreditPurchase(
        user_id=uid, stripe_session_id=sid,
        amount_cents=total, amount_total_cents=total, amount_tax_cents=tax,
        amount_refunded_cents=refunded, minutes=300, status=status,
        created_at=created or datetime(2026, 10, 1, 12, 0, 0))
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def buyers(monkeypatch):
    # Production excluded ids (1, 40, …) collide with autoincrement test users.
    monkeypatch.setattr('lifecycle_emails.DEFAULT_EXCLUDED_USER_IDS', frozenset())
    monkeypatch.setenv('LIFECYCLE_EXCLUDE_USER_IDS', '')
    with A.app.app_context():
        customer = _user('buyer-ok@gmail.com')
        internal = _user('sindrefjelle+acct@gmail.com')
        yield {'customer': customer, 'internal': internal}
        db.session.query(CreditPurchase).filter(
            CreditPurchase.stripe_session_id.like('ap-%')).delete(
            synchronize_session=False)
        db.session.query(User).filter(
            User.email.in_(['buyer-ok@gmail.com',
                            'sindrefjelle+acct@gmail.com'])
        ).delete(synchronize_session=False)
        db.session.commit()


def test_is_internal_or_test_user_by_email_and_id(buyers, monkeypatch):
    assert AP.is_internal_or_test_user(buyers['customer'].id,
                                      buyers['customer'].email) is False
    assert AP.is_internal_or_test_user(buyers['internal'].id,
                                      buyers['internal'].email) is True
    monkeypatch.setenv('LIFECYCLE_EXCLUDE_USER_IDS',
                       str(buyers['customer'].id))
    assert AP.is_internal_or_test_user(buyers['customer'].id,
                                      buyers['customer'].email) is True


def test_summarize_excludes_refunded_and_internal(buyers):
    with A.app.app_context():
        _buy(buyers['customer'].id, 'ap-ok', total=500, tax=0)
        _buy(buyers['customer'].id, 'ap-ref', total=500, tax=0,
             status='refunded', refunded=500)
        _buy(buyers['internal'].id, 'ap-int', total=500, tax=0)
        # Scope to this test's sessions (shared suite DB may hold others).
        mine = [r for r in AP.fetch_purchase_rows(db)
                if (r.get('stripe_session_id') or '').startswith('ap-')]
        summary = AP.summarize_purchase_money(mine)
    assert summary['purchase_count'] == 1
    assert summary['revenue_usd'] == pytest.approx(5.0)
    assert summary['ex_vat_usd'] == pytest.approx(5.0)
    assert summary['refund_count'] == 1
    assert summary['dispute_count'] == 0
    assert summary['refunds_disputes_usd'] == pytest.approx(5.0)


def test_revenue_incl_vat_with_ex_vat_alongside(buyers):
    with A.app.app_context():
        before = AP.purchase_totals_all_time(db)
        _buy(buyers['customer'].id, 'ap-vat', total=500, tax=100)
        after = AP.purchase_totals_all_time(db)
    assert after['purchase_count'] == before['purchase_count'] + 1
    assert after['revenue_usd'] == pytest.approx(before['revenue_usd'] + 5.0)
    assert after['vat_usd'] == pytest.approx(before['vat_usd'] + 1.0)
    assert after['ex_vat_usd'] == pytest.approx(before['ex_vat_usd'] + 4.0)


def test_dashboard_kpis_use_shared_definition(buyers, monkeypatch):
    import admin_dashboard as AD
    monkeypatch.setenv('ADMIN_EMAILS', 'buyer-ok@gmail.com')
    with A.app.app_context():
        before = AD.collect_kpis(db, 2000 * 60)
        _buy(buyers['customer'].id, 'ap-dash', total=500, tax=0)
        _buy(buyers['customer'].id, 'ap-dash-ref', total=500, tax=0,
             status='refunded', refunded=500)
        kpis = AD.collect_kpis(db, 2000 * 60)
    assert kpis['purchase_count'] == before['purchase_count'] + 1
    assert kpis['purchase_gross_usd'] == pytest.approx(
        before['purchase_gross_usd'] + 5.0)
    assert kpis['purchase_ex_vat_usd'] == pytest.approx(
        before['purchase_ex_vat_usd'] + 5.0)
    assert kpis['purchase_refund_count'] == before['purchase_refund_count'] + 1
    assert kpis['purchase_refunds_disputes_usd'] == pytest.approx(
        before['purchase_refunds_disputes_usd'] + 5.0)
