"""Lifecycle email: trial-ended 1 h / 24 h and unused-balance reminders.

Sending rules are pure (``decide_campaigns``) and tested without a database.
The sweep, click route, checkout metadata and webhook attribution run against
test_app's throwaway database with Mailgun and Stripe mocked.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

# test_app pins DATABASE_URL to a temp file before importing app.
from test_app import (  # noqa: F401 - fixtures are used by name
    A, _FakePosthog, _login, _make_user, _pack_session, _register_and_post,
    ph_events, stripe_on, trial_on,
)
import lifecycle_emails as L  # noqa: E402
import mail as mailer  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
RULES = L.Rules()


def _trial_user(**kw):
    base = dict(user_id=5001, email='listener@gmail.com', trial_limit_seconds=3600,
                trial_left_seconds=0, trial_ended_at=NOW - timedelta(hours=2))
    base.update(kw)
    return L.UserFacts(**base)


def _payer(**kw):
    base = dict(user_id=5002, email='payer@gmail.com', has_purchase=True,
                trial_limit_seconds=3600, trial_left_seconds=0,
                paid_seconds=250 * 60,
                last_activity_at=NOW - timedelta(days=8))
    base.update(kw)
    return L.UserFacts(**base)


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('age,expected', [
    (timedelta(minutes=59), []),
    (timedelta(hours=1), [L.TRIAL_ENDED_1H]),
    (timedelta(hours=22, minutes=59), [L.TRIAL_ENDED_1H]),
    (timedelta(hours=23, minutes=30), []),
    (timedelta(hours=24), [L.TRIAL_ENDED_24H]),
    (timedelta(hours=71, minutes=59), [L.TRIAL_ENDED_24H]),
    (timedelta(hours=72), []),
    (timedelta(days=9), []),
])
def test_trial_ended_windows(age, expected):
    """Turning the flag on must not mail everyone whose trial ended last week."""
    f = _trial_user(trial_ended_at=NOW - age)
    assert L.decide_campaigns(f, NOW, RULES) == expected


def test_naive_timestamps_are_utc():
    """SQLite hands back naive UTC; the rules must not shift by the box zone."""
    f = _trial_user(trial_ended_at=(NOW - timedelta(hours=2)).replace(tzinfo=None))
    assert L.decide_campaigns(f, NOW, RULES) == [L.TRIAL_ENDED_1H]


# ---------------------------------------------------------------------------
# Once only
# ---------------------------------------------------------------------------

def test_each_campaign_is_sent_once():
    f = _trial_user(sent={L.TRIAL_ENDED_1H: NOW - timedelta(hours=1)})
    assert L.decide_campaigns(f, NOW, RULES) == []
    later = _trial_user(trial_ended_at=NOW - timedelta(hours=30),
                        sent={L.TRIAL_ENDED_1H: NOW - timedelta(hours=29),
                              L.TRIAL_ENDED_24H: NOW - timedelta(hours=5)})
    assert L.decide_campaigns(later, NOW, RULES) == []


def test_1h_not_resent_late_in_its_window():
    """Spacing alone would allow it after 12 h; the sent log must not."""
    f = _trial_user(trial_ended_at=NOW - timedelta(hours=22),
                    sent={L.TRIAL_ENDED_1H: NOW - timedelta(hours=21)})
    assert L.decide_campaigns(f, NOW, RULES) == []


def test_24h_follows_the_1h():
    f = _trial_user(trial_ended_at=NOW - timedelta(hours=25),
                    sent={L.TRIAL_ENDED_1H: NOW - timedelta(hours=24)})
    assert L.decide_campaigns(f, NOW, RULES) == [L.TRIAL_ENDED_24H]


def test_1h_is_not_sent_after_the_24h():
    """A late sweeper must not send the first email after the follow-up."""
    f = _trial_user(trial_ended_at=NOW - timedelta(hours=2),
                    sent={L.TRIAL_ENDED_24H: NOW - timedelta(days=30)})
    assert L.decide_campaigns(f, NOW, RULES) == []


def test_minimum_spacing_between_lifecycle_emails():
    f = _payer(sent={L.TRIAL_ENDED_24H: NOW - timedelta(hours=3)})
    assert L.decide_campaigns(f, NOW, RULES) == []
    f = _payer(sent={L.TRIAL_ENDED_24H: NOW - timedelta(hours=13)})
    assert L.decide_campaigns(f, NOW, RULES) == [L.UNUSED_BALANCE]


def test_unused_balance_once():
    f = _payer(sent={L.UNUSED_BALANCE: NOW - timedelta(days=20)})
    assert L.decide_campaigns(f, NOW, RULES) == []


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('overrides,reason', [
    ({'unsubscribed': True}, 'unsubscribed'),
    ({'mail_pref_off': True}, 'mail_pref_off'),
    ({'trial_denied': True}, 'trial_denied'),
    ({'email': 'smoke-partial+1@podskrift.com'}, 'test_user'),
    ({'email': 'qa@example.com'}, 'test_user'),
    ({'email': 'someone@nettsmed.no'}, 'test_user'),
    ({'email': 'sindrefjelle+x@gmail.com'}, 'test_user'),
    ({'email': 'zack@hotmail.comz'}, 'invalid_email'),
    ({'email': 'no-at-sign'}, 'invalid_email'),
    ({'email': ''}, 'invalid_email'),
])
def test_global_exclusions_block_every_campaign(overrides, reason):
    for f in (_trial_user(**overrides), _payer(**overrides)):
        assert L.exclusion_reason(f) == reason
        assert L.decide_campaigns(f, NOW, RULES) == []


def test_excluded_ids_block():
    f = _trial_user(user_id=214)  # aidenjonkman5, trial farm
    assert L.decide_campaigns(f, NOW, RULES, L.excluded_user_ids()) == []
    f = _trial_user(user_id=999)
    assert L.decide_campaigns(f, NOW, RULES, {999}) == []


def test_env_adds_excluded_ids(monkeypatch):
    monkeypatch.setenv('LIFECYCLE_EXCLUDE_USER_IDS', '12, 34,x')
    ids = L.excluded_user_ids()
    assert {12, 34} <= ids and L.DEFAULT_EXCLUDED_USER_IDS <= ids


@pytest.mark.parametrize('overrides,reason', [
    ({'has_purchase': True}, 'bought'),
    ({'byok': True}, 'byok'),
    ({'trial_left_seconds': 5 * 60}, 'trial_left'),
    ({'trial_limit_seconds': 0}, 'no_trial'),
    ({'trial_ended_at': None}, 'no_trial_job'),
    ({'has_running_job': True}, 'active'),
    ({'started_after_trial_end': True}, 'active'),
])
def test_trial_ended_audience(overrides, reason):
    f = _trial_user(**overrides)
    assert L.trial_ended_eligible(f, RULES) == reason
    assert L.TRIAL_ENDED_1H not in L.decide_campaigns(f, NOW, RULES)


def test_trial_left_just_under_floor_counts_as_used_up():
    f = _trial_user(trial_left_seconds=5 * 60 - 1)
    assert L.decide_campaigns(f, NOW, RULES) == [L.TRIAL_ENDED_1H]


@pytest.mark.parametrize('overrides,expected', [
    ({}, [L.UNUSED_BALANCE]),
    ({'paid_seconds': 99 * 60}, []),
    ({'last_activity_at': NOW - timedelta(days=6, hours=23)}, []),
    ({'has_running_job': True}, []),
    ({'byok': True}, []),
])
def test_unused_balance_rules(overrides, expected):
    assert L.decide_campaigns(_payer(**overrides), NOW, RULES) == expected


def test_unused_balance_thresholds_from_env(monkeypatch):
    monkeypatch.setenv('LIFECYCLE_UNUSED_MIN_MINUTES', '300')
    monkeypatch.setenv('LIFECYCLE_UNUSED_INACTIVE_DAYS', '3')
    rules = L.Rules.from_env()
    assert L.decide_campaigns(_payer(), NOW, rules) == []
    assert L.decide_campaigns(
        _payer(paid_seconds=300 * 60, last_activity_at=NOW - timedelta(days=4)),
        NOW, rules) == [L.UNUSED_BALANCE]


# ---------------------------------------------------------------------------
# Tokens and copy
# ---------------------------------------------------------------------------

def test_click_token_round_trip_and_tamper():
    tok = L.make_click_token('s3cret', user_id=7, campaign=L.TRIAL_ENDED_1H,
                             action=L.ACTION_FINISH, task_id='abc')
    data = L.parse_click_token('s3cret', tok)
    assert (data['u'], data['c'], data['a'], data['t']) == (
        7, L.TRIAL_ENDED_1H, L.ACTION_FINISH, 'abc')
    assert L.parse_click_token('other', tok) is None
    assert L.parse_click_token('s3cret', tok[:-2] + 'xx') is None
    bad = L._click_serializer('s3cret').dumps({'u': 7, 'c': 'spam', 'a': 'pack'})
    assert L.parse_click_token('s3cret', bad) is None


def test_click_url_carries_utm():
    url = L.click_url('https://podskrift.com', 'k', user_id=3,
                      campaign=L.UNUSED_BALANCE, action=L.ACTION_HOME)
    q = parse_qs(urlparse(url).query)
    assert url.startswith('https://podskrift.com/e/')
    assert q['utm_source'] == ['email'] and q['utm_campaign'] == ['unused_balance']


def _ctx(**kw):
    base = {
        'episode_title': 'Ep <1> & friends', 'podcast_name': 'The Show',
        'partial_minutes': 60, 'episode_minutes': 94,
        'finish_price': '$2.16', 'finish_url': 'https://x/e/f?utm_campaign=c',
        'pack_minutes': 300, 'pack_price': '$5', 'pack_url': 'https://x/e/p',
        'show_url': 'https://x/e/s', 'home_url': 'https://x/e/h',
        'unsub_url': 'https://x/email/unsubscribe/tok', 'min_price': '$1.50',
    }
    base.update(kw)
    return base


def test_trial_ended_copy_leads_with_the_episode():
    for campaign in (L.TRIAL_ENDED_1H, L.TRIAL_ENDED_24H):
        subject, text, html = L.build_bodies(campaign, _ctx())
        assert 'Ep <1> & friends' in subject
        assert '$2.16' in text and 'https://x/e/f' in text
        assert text.index('https://x/e/f') < text.index('https://x/e/p')
        assert '300 minutes for $5' in text
        assert 'Unsubscribe: https://x/email/unsubscribe/tok' in text
        assert 'Ep &lt;1&gt; &amp; friends' in html and '<1>' not in html
        assert 'href="https://x/email/unsubscribe/tok"' in html
        assert 'prefers-color-scheme: dark' in html


def test_trial_ended_without_preview_offers_the_show():
    ctx = _ctx(finish_url=None, finish_price=None)
    subject, text, _ = L.build_bodies(L.TRIAL_ENDED_1H, ctx)
    assert subject == 'Your free Podskrift minutes are used up'
    assert 'See episodes of The Show: https://x/e/s' in text


def test_unused_balance_copy():
    ctx = _ctx(balance_minutes=288, episodes_phrase='about 6 episodes',
               next_title='New one', next_minutes=52, next_url='https://x/e/n')
    subject, text, _ = L.build_bodies(L.UNUSED_BALANCE, ctx)
    assert subject == 'You have 288 minutes left on Podskrift'
    assert 'about 6 episodes' in text and 'never expire' in text
    assert 'Transcribe this episode: https://x/e/n' in text
    _, text2, _ = L.build_bodies(L.UNUSED_BALANCE, _ctx(balance_minutes=288))
    assert 'Open Podskrift: https://x/e/h' in text2


# ---------------------------------------------------------------------------
# Sweep against the database (Mailgun mocked)
# ---------------------------------------------------------------------------

def _enable(monkeypatch):
    monkeypatch.setenv('LIFECYCLE_EMAILS_ENABLED', '1')
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-test')
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', True)
    # Production test-account ids collide with a fresh test database.
    monkeypatch.setattr(L, 'DEFAULT_EXCLUDED_USER_IDS', frozenset())


def _ok(status=200):
    r = mock.Mock()
    r.status_code = status
    return r


def _partial_task(uid, *, ended_ago=timedelta(hours=2), episode_min=94,
                  preview_min=60):
    from models import db, TranscriptionTask
    now = datetime.now(timezone.utc)
    with A.app.app_context():
        t = TranscriptionTask(
            id=str(uuid.uuid4()), user_id=uid, episode_title='Long Talk',
            podcast_name='Deep Show', status='completed', progress=100,
            rss_url='https://feeds.example.org/deep.xml',
            source_audio_url='https://cdn.example.org/long.mp3',
            trial_seconds_charged=preview_min * 60, trial_settled=True,
            partial_meta=json.dumps({'partial_seconds': preview_min * 60,
                                     'episode_seconds': episode_min * 60}),
            started_at=(now - ended_ago - timedelta(minutes=4)).replace(tzinfo=None),
            completed_at=(now - ended_ago).replace(tzinfo=None))
        db.session.add(t)
        db.session.commit()
        return t.id


def _sent_rows(uid):
    from models import EmailSentLog
    with A.app.app_context():
        return [r.idempotency_key for r in
                EmailSentLog.query.filter_by(user_id=uid).all()]


def test_sweep_sends_trial_ended_once(trial_on, stripe_on, ph_events, monkeypatch):
    _enable(monkeypatch)
    uid = _make_user(f'trial-ended-{uuid.uuid4().hex[:6]}@gmail.com',
                     limit=3600, used=3600)
    task_id = _partial_task(uid)
    with mock.patch.object(mailer.requests, 'get', return_value=_ok(404)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok()) as post:
        with A.app.app_context():
            first = L.sweep(only_user_ids=[uid])
            second = L.sweep(only_user_ids=[uid])
    assert first["sent"] == {L.TRIAL_ENDED_1H: 1}, first
    assert second['sent'] == {}
    assert post.call_count == 1
    fields = dict(post.call_args.kwargs['data'])
    assert fields['subject'] == 'Finish \u201cLong Talk\u201d'
    assert fields['h:List-Unsubscribe'].startswith('<https://podskrift.com/email/unsubscribe/')
    assert fields['h:List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'
    # 94 min against ~0 left: 105 min on the curve.
    price = A.episode_offer(94, 0)['price']
    assert f'Finish this episode for {price}' in fields['text']
    assert _sent_rows(uid) == [f'lifecycle:trial_ended_1h:{uid}']
    sent = [e for e in ph_events.events if e['event'] == 'lifecycle_email_sent']
    assert len(sent) == 1
    assert sent[0]['properties']['campaign'] == L.TRIAL_ENDED_1H
    assert sent[0]['properties']['variant'] == 'finish'
    assert '@' not in json.dumps(sent[0]['properties'])
    # the finish link points at that task
    link = [w for w in fields['text'].split() if '/e/' in w][0]
    token = urlparse(link).path.split('/e/')[1]
    assert L.parse_click_token(A.app.secret_key, token)['t'] == task_id


def test_sweep_off_by_default(trial_on, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.delenv('LIFECYCLE_EMAILS_ENABLED')
    uid = _make_user(f'off-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    _partial_task(uid)
    with mock.patch.object(mailer.requests, 'post') as post:
        with A.app.app_context():
            assert L.sweep(only_user_ids=[uid]).get('skipped') == 'disabled'
    post.assert_not_called()


def test_sweep_skips_buyers_and_unsubscribed(trial_on, stripe_on, monkeypatch):
    from models import db, User, CreditPurchase
    _enable(monkeypatch)
    buyer = _make_user(f'b-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    unsub = _make_user(f'u-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    _partial_task(buyer)
    _partial_task(unsub)
    with A.app.app_context():
        db.session.add(CreditPurchase(user_id=buyer, stripe_session_id=f'cs_{buyer}',
                                      amount_cents=150, currency='usd', minutes=60))
        db.session.get(User, unsub).email_unsubscribed_at = datetime.now(timezone.utc)
        db.session.commit()
    with mock.patch.object(mailer.requests, 'get', return_value=_ok(404)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok()) as post:
        with A.app.app_context():
            res = L.sweep(only_user_ids=[buyer, unsub])
    post.assert_not_called()
    assert res['sent'] == {}


def test_mailgun_suppressed_address_is_skipped_without_claim(trial_on, stripe_on, monkeypatch):
    _enable(monkeypatch)
    uid = _make_user(f'bounced-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    _partial_task(uid)
    with mock.patch.object(mailer.requests, 'get', return_value=_ok(200)), \
            mock.patch.object(mailer.requests, 'post') as post:
        with A.app.app_context():
            res = L.sweep(only_user_ids=[uid])
    post.assert_not_called()
    assert res['outcomes'] == {'skipped_suppressed': 1}
    assert _sent_rows(uid) == []


# --- Suppression lookup: key without read access (401/403) -------------------

@pytest.mark.parametrize('codes,expected', [
    ((404, 404, 404), mailer.SUPPRESSION_CLEAR),
    ((200,), mailer.SUPPRESSION_SUPPRESSED),
    ((404, 200), mailer.SUPPRESSION_SUPPRESSED),
    ((401,), mailer.SUPPRESSION_NO_ACCESS),
    ((403,), mailer.SUPPRESSION_NO_ACCESS),
    ((404, 401), mailer.SUPPRESSION_NO_ACCESS),
    ((500,), mailer.SUPPRESSION_UNKNOWN),
    ((503,), mailer.SUPPRESSION_UNKNOWN),
    ((429,), mailer.SUPPRESSION_UNKNOWN),
    ((400,), mailer.SUPPRESSION_UNKNOWN),
])
def test_suppression_status_codes(monkeypatch, codes, expected):
    _enable(monkeypatch)
    with mock.patch.object(mailer.requests, 'get',
                           side_effect=[_ok(c) for c in codes]):
        assert mailer.suppression_status('a@gmail.com') == expected


def test_suppression_status_timeout_is_unknown(monkeypatch):
    _enable(monkeypatch)
    with mock.patch.object(mailer.requests, 'get',
                           side_effect=mailer.requests.Timeout()):
        assert mailer.suppression_status('a@gmail.com') == mailer.SUPPRESSION_UNKNOWN


def test_suppression_no_access_is_not_logged_per_address(monkeypatch, caplog):
    _enable(monkeypatch)
    with caplog.at_level('WARNING', logger='mail'), \
            mock.patch.object(mailer.requests, 'get', return_value=_ok(401)):
        for _ in range(3):
            mailer.suppression_status('a@gmail.com')
    assert [r for r in caplog.records if r.name == 'mail'] == []


def _two_due_users():
    a = _make_user(f'na-a-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    b = _make_user(f'na-b-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    _partial_task(a)
    _partial_task(b)
    return a, b


@pytest.mark.parametrize('code', [401, 403])
def test_sweep_sends_when_suppression_lists_not_readable(
        trial_on, stripe_on, ph_events, monkeypatch, caplog, code):
    _enable(monkeypatch)
    a, b = _two_due_users()
    with caplog.at_level('WARNING'), \
            mock.patch.object(mailer.requests, 'get', return_value=_ok(code)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok()) as post:
        with A.app.app_context():
            first = L.sweep(only_user_ids=[a, b])
            second = L.sweep(only_user_ids=[a, b])
    assert first['sent'] == {L.TRIAL_ENDED_1H: 2}, first
    assert first['outcomes'] == {'sent': 2}
    assert first['notes'] == [L.NOTE_SUPPRESSION_NO_ACCESS]
    assert second['sent'] == {}
    assert post.call_count == 2
    assert _sent_rows(a) == [f'lifecycle:trial_ended_1h:{a}']
    assert _sent_rows(b) == [f'lifecycle:trial_ended_1h:{b}']
    # One warning per sweep (the second sweep never reaches the lookup), not
    # one per user, and nothing from mail.py's per-address logging.
    warnings = [r for r in caplog.records if r.levelname == 'WARNING'
                and 'suppression' in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert warnings[0].name == 'lifecycle_emails'


@pytest.mark.parametrize('get_kwargs', [
    {'return_value': _ok(500)},
    {'return_value': _ok(502)},
    {'side_effect': mailer.requests.Timeout()},
    {'side_effect': mailer.requests.ConnectionError()},
])
def test_sweep_still_skips_on_timeout_and_5xx(trial_on, stripe_on, monkeypatch, get_kwargs):
    _enable(monkeypatch)
    a, b = _two_due_users()
    with mock.patch.object(mailer.requests, 'get', **get_kwargs), \
            mock.patch.object(mailer.requests, 'post') as post:
        with A.app.app_context():
            res = L.sweep(only_user_ids=[a, b])
    post.assert_not_called()
    assert res['outcomes'] == {'skipped_unknown': 2}
    assert res['sent'] == {}
    assert 'notes' not in res
    assert _sent_rows(a) == [] and _sent_rows(b) == []


def test_sweep_clear_lists_add_no_note(trial_on, stripe_on, monkeypatch, caplog):
    _enable(monkeypatch)
    a, _b = _two_due_users()
    with caplog.at_level('WARNING'), \
            mock.patch.object(mailer.requests, 'get', return_value=_ok(404)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok()):
        with A.app.app_context():
            res = L.sweep(only_user_ids=[a])
    assert res['sent'] == {L.TRIAL_ENDED_1H: 1}
    assert 'notes' not in res
    assert not [r for r in caplog.records if 'suppression' in r.getMessage()]


def test_send_one_without_notes_still_sends_and_warns(trial_on, stripe_on, monkeypatch, caplog):
    from models import User
    _enable(monkeypatch)
    a, _b = _two_due_users()
    with caplog.at_level('WARNING'), \
            mock.patch.object(mailer.requests, 'get', return_value=_ok(401)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok()) as post:
        with A.app.app_context():
            out = L.send_one(L.TRIAL_ENDED_1H, A.db.session.get(User, a),
                             public_base='https://podskrift.com',
                             secret_key=A.app.secret_key)
    assert out == 'sent' and post.call_count == 1
    assert len([r for r in caplog.records if 'suppression' in r.getMessage()]) == 1


def test_failed_send_releases_the_claim(trial_on, stripe_on, monkeypatch):
    _enable(monkeypatch)
    uid = _make_user(f'fail-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    _partial_task(uid)
    with mock.patch.object(mailer.requests, 'get', return_value=_ok(404)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok(400)):
        with A.app.app_context():
            L.sweep(only_user_ids=[uid])
    assert _sent_rows(uid) == []


def test_unused_balance_sweep(trial_on, stripe_on, monkeypatch):
    from models import db, User, CreditPurchase
    _enable(monkeypatch)
    uid = _make_user(f'payer-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).replace(tzinfo=None)
    with A.app.app_context():
        u = db.session.get(User, uid)
        u.paid_seconds_balance = 288 * 60
        u.created_at = old
        db.session.add(CreditPurchase(user_id=uid, stripe_session_id=f'cs_p{uid}',
                                      amount_cents=500, currency='usd',
                                      minutes=300, created_at=old))
        db.session.commit()
    with mock.patch.object(mailer.requests, 'get', return_value=_ok(404)), \
            mock.patch.object(mailer.requests, 'post', return_value=_ok()) as post:
        with A.app.app_context():
            res = L.sweep(only_user_ids=[uid], fetch_next_episode=False)
            again = L.sweep(only_user_ids=[uid], fetch_next_episode=False)
    assert res['sent'] == {L.UNUSED_BALANCE: 1} and again['sent'] == {}
    assert 'You have 288 minutes left' in dict(post.call_args.kwargs['data'])['subject']


# ---------------------------------------------------------------------------
# Click route, checkout metadata, webhook attribution
# ---------------------------------------------------------------------------

def _token(uid, action, campaign=L.TRIAL_ENDED_1H, task_id=None):
    return L.make_click_token(A.app.secret_key, user_id=uid, campaign=campaign,
                              action=action, task_id=task_id)


def test_click_finish_lands_on_checkout_form(trial_on, stripe_on, ph_events, monkeypatch):
    monkeypatch.setattr(A, 'VOLUME_PRICING_ENABLED', True)
    uid = _make_user(f'click-{uuid.uuid4().hex[:6]}@gmail.com', limit=3600, used=3600)
    task_id = _partial_task(uid)
    client = _login(uid)
    resp = client.get(f'/e/{_token(uid, L.ACTION_FINISH, task_id=task_id)}?utm_source=email')
    body = resp.data.decode()
    assert resp.status_code == 200
    assert 'action="/billing/checkout"' in body
    offer = A.episode_offer(94, 0)
    assert f'name="minutes" value="{offer["minutes"]}"' in body
    assert 'name="price_source" value="episode"' in body
    assert 'name="audio_url" value="https://cdn.example.org/long.mp3"' in body
    assert 'name="source" value="email_trial_ended_1h"' in body
    clicks = [e for e in ph_events.events if e['event'] == 'lifecycle_email_clicked']
    assert clicks and clicks[0]['properties']['campaign'] == L.TRIAL_ENDED_1H
    with client.session_transaction() as sess:
        assert sess[L.SESSION_CAMPAIGN_KEY]['c'] == L.TRIAL_ENDED_1H

    # The checkout it submits carries the campaign in Stripe metadata.
    with client.session_transaction() as sess:
        csrf = sess.get('_csrf_token')
    client.post('/billing/checkout', data={
        'csrf_token': csrf, 'source': 'email_trial_ended_1h',
        'minutes': str(offer['minutes']), 'price_source': 'episode',
        'audio_url': 'https://cdn.example.org/long.mp3', 'duration_min': '94'})
    params = stripe_on['last_create_params']
    assert params['metadata']['email_campaign'] == L.TRIAL_ENDED_1H
    started = [e for e in ph_events.events if e['event'] == 'checkout_started']
    assert started[-1]['properties']['email_campaign'] == L.TRIAL_ENDED_1H


def test_checkout_without_click_has_no_campaign(trial_on, stripe_on):
    uid = _make_user(f'nocl-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        csrf = sess.get('_csrf_token')
    client.post('/billing/checkout', data={'csrf_token': csrf, 'source': 'settings',
                                           'email_campaign': L.TRIAL_ENDED_1H})
    assert 'email_campaign' not in stripe_on['last_create_params']['metadata']


def test_stale_click_does_not_attribute(trial_on, stripe_on):
    import time as _time
    uid = _make_user(f'stale-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        csrf = sess.get('_csrf_token')
        sess[L.SESSION_CAMPAIGN_KEY] = {
            'c': L.TRIAL_ENDED_24H, 'u': uid,
            'at': _time.time() - L.CLICK_ATTRIBUTION_SECONDS - 5}
    client.post('/billing/checkout', data={'csrf_token': csrf, 'source': 'settings'})
    assert 'email_campaign' not in stripe_on['last_create_params']['metadata']


def test_click_stash_for_another_account_does_not_attribute(trial_on, stripe_on):
    """Shared browser: a click stashed for user A must not credit B's checkout."""
    import time as _time
    uid = _make_user(f'shar-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        csrf = sess.get('_csrf_token')
        sess[L.SESSION_CAMPAIGN_KEY] = {
            'c': L.TRIAL_ENDED_24H, 'u': uid + 1000, 'at': _time.time()}
    client.post('/billing/checkout', data={'csrf_token': csrf, 'source': 'settings'})
    assert 'email_campaign' not in stripe_on['last_create_params']['metadata']


def test_click_signed_out_goes_through_login_once(trial_on, ph_events):
    uid = _make_user(f'out-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    tok = _token(uid, L.ACTION_PACK)
    resp = A.app.test_client().get(f'/e/{tok}?utm_source=email')
    assert resp.status_code == 302
    loc = resp.headers['Location']
    assert '/login' in loc and 'r%3D1' in loc
    A.app.test_client().get(f'/e/{tok}?utm_source=email&r=1')
    clicks = [e for e in ph_events.events if e['event'] == 'lifecycle_email_clicked']
    assert len(clicks) == 1 and clicks[0]['distinct_id'] == str(uid)


def test_click_by_another_user_never_opens_checkout(trial_on, stripe_on):
    owner = _make_user(f'own-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    other = _make_user(f'oth-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    client = _login(other)
    resp = client.get(f'/e/{_token(owner, L.ACTION_PACK)}')
    assert resp.status_code == 302 and '/billing' not in resp.headers['Location']
    with client.session_transaction() as sess:
        assert L.SESSION_CAMPAIGN_KEY not in sess


def test_bad_token_goes_home():
    resp = A.app.test_client().get('/e/not-a-token')
    assert resp.status_code == 302


def test_webhook_stores_email_campaign(stripe_on):
    from models import CreditPurchase
    uid = _make_user(f'wh-{uuid.uuid4().hex[:6]}@gmail.com', limit=600, used=600)
    sid = f'cs_test_lc_{uid}'
    sess = _pack_session(uid, session_id=sid)
    sess['metadata']['email_campaign'] = L.TRIAL_ENDED_24H
    assert _register_and_post(stripe_on, sess).status_code == 200
    sid2 = f'cs_test_lc2_{uid}'
    sess2 = _pack_session(uid, session_id=sid2)
    sess2['metadata']['email_campaign'] = 'made_up'
    _register_and_post(stripe_on, sess2)
    with A.app.app_context():
        assert CreditPurchase.query.filter_by(
            stripe_session_id=sid).one().email_campaign == L.TRIAL_ENDED_24H
        assert CreditPurchase.query.filter_by(
            stripe_session_id=sid2).one().email_campaign is None


def test_sweeper_does_not_start_when_off(monkeypatch):
    monkeypatch.setattr(L, '_sweeper_started', False)
    monkeypatch.delenv('LIFECYCLE_EMAILS_ENABLED', raising=False)
    assert L.start_sweeper() is False


def test_long_titles_are_shortened_in_the_subject_only():
    title = 'The 9 best foods to beat the winter blues, end brain fog and stop anxiety | Prof Tim Spector'
    subject, text, _ = L.build_bodies(L.TRIAL_ENDED_1H, _ctx(episode_title=title))
    assert len(subject) < 75 and subject.endswith('\u2026\u201d')
    assert title in text
