"""Contract test: our Checkout parameters against Stripe's real API (test mode).

Every other checkout test replaces the Stripe client with a fake, so it can only
assert what we already know Stripe rejects. `consent_collection.promotions` is
the example: Stripe refused it for a Norwegian account (PODSKRIFT-W), checkout
failed in production, and no unit test could have said so, because the fake
accepts anything. This file sends the parameters the app really builds to
Stripe and expects a hosted Checkout URL back.

Opt-in: it runs only when STRIPE_TEST_SECRET_KEY holds an `sk_test_` key, and
that key must belong to the same Stripe account as production (the country of
the *account* decides what Stripe accepts, not the buyer's country). Without
the key every test here is skipped. It creates unpaid Checkout Sessions only;
nothing is charged. Live keys are refused.

Run: STRIPE_TEST_SECRET_KEY=sk_test_... python -m pytest test_stripe_contract.py -q
"""

import os

import pytest

import test_app as T  # sets DATABASE_URL to a throwaway file before importing app

A = T.A
trial_on = T.trial_on  # re-export the fixture so pytest finds it here

_KEY = (os.getenv('STRIPE_TEST_SECRET_KEY') or '').strip()

pytestmark = pytest.mark.skipif(
    not _KEY.startswith('sk_test_'),
    reason='STRIPE_TEST_SECRET_KEY (sk_test_...) not set')


@pytest.fixture
def real_stripe(monkeypatch, trial_on):
    if A.stripe is None:
        pytest.skip('stripe package not installed')
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', _KEY)
    monkeypatch.setattr(A, 'STRIPE_WEBHOOK_SECRET', 'whsec_contract_test')
    monkeypatch.setattr(A, 'STRIPE_PRICE_ID', '')
    return True


def _checkout(client, token, **form):
    data = {'csrf_token': token, 'source': 'settings'}
    data.update(form)
    return client.post('/billing/checkout', data=data, follow_redirects=False)


def _client_with_csrf(email):
    uid = T._make_user(email, limit=600, used=600)
    client = T._login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    assert token
    return client, token


def _assert_hosted_checkout(resp):
    # A rejected Session create flashes an error and redirects to /settings, so
    # a 303 to checkout.stripe.com is the only success signal that counts.
    assert resp.status_code == 303, resp.headers.get('Location')
    assert resp.headers['Location'].startswith('https://checkout.stripe.com/')


@pytest.mark.parametrize('managed_payments', [False, True],
                         ids=['plain', 'managed_payments'])
def test_fixed_pack_session_is_accepted_by_stripe(real_stripe, monkeypatch,
                                                  managed_payments):
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', False)
    monkeypatch.setattr(A, 'STRIPE_MANAGED_PAYMENTS', managed_payments)
    client, token = _client_with_csrf(f'contract-pack-{managed_payments}@test.com')
    _assert_hosted_checkout(_checkout(client, token))


@pytest.mark.parametrize('managed_payments', [False, True],
                         ids=['plain', 'managed_payments'])
def test_volume_session_is_accepted_by_stripe(real_stripe, monkeypatch,
                                              managed_payments):
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', True)
    monkeypatch.setattr(A, 'STRIPE_MANAGED_PAYMENTS', managed_payments)
    client, token = _client_with_csrf(f'contract-vol-{managed_payments}@test.com')
    _assert_hosted_checkout(_checkout(
        client, token, source='pricing', minutes='600', price_source='slider'))

