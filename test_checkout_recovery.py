"""Abandoned-checkout recovery email (Mailgun, not Stripe's recovery).

Bodies and skip rules are unit-tested without Mailgun. The webhook hook,
click route, checkout metadata and purchase attribution run against
test_app's throwaway database with Mailgun and Stripe mocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from test_app import (  # noqa: F401 - fixtures used by name
    A, _login, _make_user, _pack_session, _register_and_post,
    ph_events, stripe_on, trial_on,
)
import checkout_recovery as R  # noqa: E402
import lifecycle_emails as L  # noqa: E402
import mail as mailer  # noqa: E402
import pricing_volume as pv  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _recovery_app_ctx():
    """DB helpers need an app context; webhook tests open their own."""
    with A.app.app_context():
        yield


def _user(uid):
    return A.db.session.get(A.User, uid)


def _expired_session(uid, *, cs_id='cs_expired_rec_1', minutes=300,
                     price_source='slider', email='buyer@gmail.com',
                     payment_status='unpaid', **extra_meta):
    meta = {
        'user_id': str(uid),
        'minutes': str(minutes),
        'pack': pv.VOLUME_PACK_SKU,
        'price_source': price_source,
        'location': 'paywall',
        'amount_cents': str(pv.price_cents(minutes)),
    }
    meta.update(extra_meta)
    return {
        'id': cs_id,
        'object': 'checkout.session',
        'client_reference_id': str(uid),
        'customer_email': email,
        'payment_status': payment_status,
        'amount_total': pv.price_cents(minutes),
        'currency': 'usd',
        'created': 1_700_000_000,
        'expires_at': 1_700_000_000 + 2 * 3600,
        'metadata': meta,
    }


def _ns_event(etype, obj_id, obj):
    return type('E', (), {
        'type': etype,
        'id': f'evt_{obj_id}',
        'data': type('D', (), {'object': obj})(),
    })()


def _enable_recovery(monkeypatch):
    monkeypatch.setenv('CHECKOUT_RECOVERY_EMAIL_ENABLED', '1')
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-test')
    monkeypatch.setenv('MAILGUN_DOMAIN', 'podskrift.com')
    monkeypatch.setenv('MAILGUN_BASE_URL', 'https://api.eu.mailgun.net')
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', True)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    monkeypatch.setattr(mailer, 'suppression_status',
                        lambda _addr: mailer.SUPPRESSION_CLEAR)


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------

def test_recovery_email_bodies_are_short_and_named():
    subject, text, html = R.build_bodies(
        minutes=300, price_source='slider', price='$5',
        resume_url='https://podskrift.com/r/tok',
        unsub_url='https://podskrift.com/email/unsubscribe/tok',
    )
    assert subject == R.SUBJECT
    assert subject == "You didn't finish your Podskrift purchase"
    assert '300 minutes for $5' in text
    assert 'Complete your purchase ($5): https://podskrift.com/r/tok' in text
    assert 'Sindre' in text
    assert 'Unsubscribe:' in text
    assert 'class="btn"' in html
    assert 'Complete your purchase ($5)' in html
    assert 'prefers-color-scheme: dark' in html
    assert 'href="https://podskrift.com/email/unsubscribe/tok"' in html


def test_episode_offer_phrase():
    assert R.offer_phrase(75, 'episode', '$1.72') == (
        'a 75-minute transcript for $1.72')
    assert R.offer_phrase(300, 'slider', '$5') == '300 minutes for $5'


def test_click_token_round_trip():
    tok = R.make_click_token(
        'secret', user_id=42, minutes=75, price_source='episode',
        expired_session_id='cs_abc', return_to='/t/xyz')
    data = R.parse_click_token('secret', tok)
    assert data['u'] == 42
    assert data['m'] == 75
    assert data['ps'] == 'episode'
    assert data['cs'] == 'cs_abc'
    assert data['rt'] == '/t/xyz'
    assert R.parse_click_token('other', tok) is None


def test_click_url_carries_utm():
    url = R.click_url(
        'https://podskrift.com', 'secret',
        user_id=1, minutes=300, price_source='slider',
        expired_session_id='cs_1')
    assert url.startswith('https://podskrift.com/r/')
    qs = parse_qs(urlparse(url).query)
    assert qs['utm_campaign'] == [R.CAMPAIGN]
    assert qs['utm_source'] == ['email']


# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------

def test_skip_when_flag_off(monkeypatch, stripe_on):
    monkeypatch.delenv('CHECKOUT_RECOVERY_EMAIL_ENABLED', raising=False)
    uid = _make_user('off@gmail.com', limit=600, used=0)
    assert R.handle_expired_session(_expired_session(uid)) == 'disabled'


def test_skip_paid_session(monkeypatch):
    _enable_recovery(monkeypatch)
    uid = _make_user('paid@gmail.com', limit=600, used=0)
    d = _expired_session(uid, payment_status='paid')
    assert R.decide_skip(d, _user(uid), now=NOW) == 'already_paid'


def test_skip_no_session_email(monkeypatch):
    _enable_recovery(monkeypatch)
    uid = _make_user('hasaccount@gmail.com', limit=600, used=0)
    d = _expired_session(uid, email='')
    d.pop('customer_email')
    assert R.decide_skip(d, _user(uid), now=NOW) == 'no_email'


def test_skip_test_and_unsubscribed(monkeypatch):
    _enable_recovery(monkeypatch)
    # Production exclusion list includes low ids; clear for this check and
    # rely on the podskrift.com domain rule instead.
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('sindre@podskrift.com', limit=600, used=0)
    user = _user(uid)
    d = _expired_session(uid, email=user.email)
    assert R.decide_skip(d, user, now=NOW) == 'test_user'

    uid2 = _make_user('optout@gmail.com', limit=600, used=0)
    user2 = _user(uid2)
    user2.email_unsubscribed_at = NOW
    A.db.session.commit()
    d2 = _expired_session(uid2, email=user2.email, cs_id='cs_opt')
    assert R.decide_skip(d2, user2, now=NOW) == 'unsubscribed'


def test_skip_when_already_bought(monkeypatch, stripe_on):
    _enable_recovery(monkeypatch)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('buyer2@gmail.com', limit=600, used=0)
    # Credit a prior pack so "since bought" is true.
    sess = _pack_session(uid, 'cs_prior_buy')
    assert _register_and_post(stripe_on, sess).status_code == 200
    user = _user(uid)
    d = _expired_session(uid, email=user.email, cs_id='cs_abandon')
    assert R.decide_skip(d, user, now=NOW) == 'bought'


def test_cooldown_one_per_seven_days(monkeypatch):
    _enable_recovery(monkeypatch)
    uid = _make_user('cool@gmail.com', limit=600, used=0)
    A.db.session.add(A.EmailSentLog(
        user_id=uid, kind=R.SENT_LOG_KIND,
        idempotency_key='checkout_recovery:cs_old',
        created_at=NOW - timedelta(days=3)))
    A.db.session.commit()
    user = _user(uid)
    d = _expired_session(uid, email=user.email, cs_id='cs_new')
    monkeypatch.setattr(mailer, 'send_email', lambda **kw: mailer.SEND_SENT)
    assert R.handle_expired_session(d, now=NOW) == 'skipped_cooldown'
    # Claim for the new session must not stick after a cooldown skip.
    assert A.EmailSentLog.query.filter_by(
        idempotency_key=R.sent_log_key('cs_new')).first() is None

    # Older than 7 days: allowed.
    row = A.EmailSentLog.query.filter_by(
        idempotency_key='checkout_recovery:cs_old').one()
    row.created_at = NOW - timedelta(days=8)
    A.db.session.commit()
    assert R.recently_sent(uid, now=NOW) is False
    assert R.handle_expired_session(d, now=NOW) == 'sent'


# ---------------------------------------------------------------------------
# Send + webhook
# ---------------------------------------------------------------------------

def test_sends_and_logs_and_captures(monkeypatch, ph_events):
    _enable_recovery(monkeypatch)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('sendme@gmail.com', limit=600, used=0)
    sent = {}

    def fake_send(**kwargs):
        sent.update(kwargs)
        return mailer.SEND_SENT

    monkeypatch.setattr(mailer, 'send_email', fake_send)
    d = _expired_session(uid, minutes=75, price_source='episode')
    assert R.handle_expired_session(d, now=NOW) == 'sent'

    assert sent['to'] == 'buyer@gmail.com'
    assert sent['subject'] == R.SUBJECT
    assert '75-minute transcript' in sent['text']
    assert '/r/' in sent['text']
    assert sent['kind'] == R.CAMPAIGN
    assert A.EmailSentLog.query.filter_by(
        user_id=uid, kind=R.SENT_LOG_KIND).count() == 1
    assert A.EmailSentLog.query.filter_by(
        idempotency_key=R.sent_log_key('cs_expired_rec_1')).one()

    events = [e for e in ph_events.events if e['event'] == 'checkout_recovery_sent']
    assert len(events) == 1
    assert events[0]['distinct_id'] == str(uid)
    assert events[0]['properties']['minutes'] == 75
    assert events[0]['properties']['price_source'] == 'episode'


def test_idempotent_on_webhook_retry(monkeypatch, ph_events):
    _enable_recovery(monkeypatch)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('once@gmail.com', limit=600, used=0)
    calls = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        return mailer.SEND_SENT

    monkeypatch.setattr(mailer, 'send_email', fake_send)
    d = _expired_session(uid, cs_id='cs_retry_1')
    assert R.handle_expired_session(d, now=NOW) == 'sent'
    assert R.handle_expired_session(d, now=NOW) == 'already_sent'
    assert len(calls) == 1


def test_webhook_fires_recovery_when_enabled(monkeypatch, stripe_on, ph_events):
    _enable_recovery(monkeypatch)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('hook@gmail.com', limit=120 * 60, used=30 * 60)
    sent = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        return mailer.SEND_SENT

    monkeypatch.setattr(mailer, 'send_email', fake_send)
    obj = _expired_session(uid, cs_id='cs_hook_1')
    event = _ns_event('checkout.session.expired', 'cs_hook_1', obj)
    A.stripe.Webhook.construct_event = staticmethod(
        lambda payload, sig, secret: event)
    resp = A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'})
    assert resp.status_code == 200
    assert len(sent) == 1
    # Existing analytics still fire.
    assert any(e['event'] == 'checkout_expired' for e in ph_events.events)
    assert any(e['event'] == 'checkout_recovery_sent' for e in ph_events.events)


def test_webhook_skips_recovery_when_flag_off(monkeypatch, stripe_on, ph_events):
    monkeypatch.delenv('CHECKOUT_RECOVERY_EMAIL_ENABLED', raising=False)
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-test')
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('nofire@gmail.com', limit=600, used=0)
    sent = []
    monkeypatch.setattr(mailer, 'send_email',
                        lambda **kw: sent.append(kw) or mailer.SEND_SENT)
    obj = _expired_session(uid, cs_id='cs_nofire')
    event = _ns_event('checkout.session.expired', 'cs_nofire', obj)
    A.stripe.Webhook.construct_event = staticmethod(
        lambda payload, sig, secret: event)
    resp = A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'})
    assert resp.status_code == 200
    assert sent == []
    assert any(e['event'] == 'checkout_expired' for e in ph_events.events)
    assert not any(
        e['event'] == 'checkout_recovery_sent' for e in ph_events.events)


def test_failed_send_releases_claim(monkeypatch):
    _enable_recovery(monkeypatch)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('fail@gmail.com', limit=600, used=0)
    monkeypatch.setattr(mailer, 'send_email', lambda **kw: mailer.SEND_FAILED)
    d = _expired_session(uid, cs_id='cs_fail_1')
    assert R.handle_expired_session(d, now=NOW) == mailer.SEND_FAILED
    assert A.EmailSentLog.query.filter_by(
        idempotency_key=R.sent_log_key('cs_fail_1')).first() is None
    # Retry can claim again.
    monkeypatch.setattr(mailer, 'send_email', lambda **kw: mailer.SEND_SENT)
    assert R.handle_expired_session(d, now=NOW) == 'sent'


# ---------------------------------------------------------------------------
# Click → checkout attribution
# ---------------------------------------------------------------------------

def test_recovery_click_starts_same_minutes_checkout(
        monkeypatch, stripe_on, ph_events):
    # Leave PUBLIC_BASE_URL unset so the test client is not 301'd to prod.
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', True)
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())
    uid = _make_user('clicker@gmail.com', limit=600, used=0)
    tok = R.make_click_token(
        A.app.secret_key, user_id=uid, minutes=75, price_source='episode',
        expired_session_id='cs_exp_click', return_to='/')
    client = _login(uid)
    resp = client.get(f'/r/{tok}', follow_redirects=False)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'action="/billing/checkout"' in body
    assert 'name="minutes" value="75"' in body
    assert 'name="price_source" value="episode"' in body
    assert f'email_{R.CAMPAIGN}' in body

    clicks = [e for e in ph_events.events
              if e['event'] == 'checkout_recovery_clicked']
    assert len(clicks) == 1
    assert clicks[0]['properties']['minutes'] == 75

    # Auto-submit page → POST checkout with campaign attribution.
    with client.session_transaction() as sess:
        assert sess[L.SESSION_CAMPAIGN_KEY]['c'] == R.CAMPAIGN
        csrf = sess.get('_csrf_token')
    resp = client.post('/billing/checkout', data={
        'csrf_token': csrf, 'source': f'email_{R.CAMPAIGN}',
        'minutes': '75', 'price_source': 'episode',
    }, follow_redirects=False)
    assert resp.status_code == 303
    meta = stripe_on['last_create_params']['metadata']
    assert meta['email_campaign'] == R.CAMPAIGN
    assert meta['minutes'] == '75'
    assert int(stripe_on['last_create_params']['line_items'][0]
               ['price_data']['unit_amount']) == pv.price_cents(75)


def test_recovery_click_requires_matching_user(monkeypatch, stripe_on):
    # No PUBLIC_BASE_URL: the canonical-host redirect would 301 the test client.
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', True)
    uid = _make_user('owner@gmail.com', limit=600, used=0)
    other = _make_user('other@gmail.com', limit=600, used=0)
    tok = R.make_click_token(
        A.app.secret_key, user_id=uid, minutes=300, price_source='slider',
        expired_session_id='cs_x')
    client = _login(other)
    resp = client.get(f'/r/{tok}', follow_redirects=False)
    assert resp.status_code in (302, 303)
    loc = resp.headers.get('Location') or ''
    assert '/r/' not in loc
    assert loc.rstrip('/').endswith('/') or loc.endswith('/index') or loc == '/'


def test_webhook_stores_recovery_email_campaign(stripe_on):
    uid = _make_user('attr@gmail.com', limit=600, used=0)
    sess = _pack_session(uid, 'cs_attr_rec')
    sess['metadata']['email_campaign'] = R.CAMPAIGN
    assert _register_and_post(stripe_on, sess).status_code == 200
    with A.app.app_context():
        row = A.CreditPurchase.query.filter_by(
            stripe_session_id='cs_attr_rec').one()
        assert row.email_campaign == R.CAMPAIGN


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv('CHECKOUT_RECOVERY_EMAIL_ENABLED', raising=False)
    assert R.recovery_email_enabled() is False
    # Stripe's own recovery (after_expiration) stays off by default.
    assert A.CHECKOUT_RECOVERY_ENABLED is False
