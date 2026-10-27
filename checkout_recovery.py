"""Abandoned-checkout recovery email via Mailgun.

Triggered from the Stripe ``checkout.session.expired`` webhook when
``CHECKOUT_RECOVERY_EMAIL_ENABLED`` is on. Separate from
``CHECKOUT_RECOVERY_ENABLED`` (Stripe's own recovery emails — left off in
prod because Norway rejects promotions consent).

At most one recovery email per user per 7 days. Skips internal/test
accounts, unsubscribed users, users who already bought, sessions without
an email, and paid sessions. The CTA opens a fresh Checkout Session for
the same minutes (and price_source) through the normal ``/billing/checkout``
path — no discount, no alternate pricing.
"""

from __future__ import annotations

import html as html_lib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

CAMPAIGN = 'checkout_recovery'
SENT_LOG_KIND = 'checkout_recovery'
#: Kind prefix kept for grepping email_sent_log alongside lifecycle_*.
COOLDOWN = timedelta(days=7)

CLICK_SALT = 'podskrift-checkout-recovery-click-v1'
#: Long enough to click days after the session expired.
CLICK_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

SUBJECT = "You didn't finish your Podskrift purchase"
SIGNOFF = 'Sindre\nPodskrift'


def _truthy(raw: Optional[str]) -> bool:
    return (raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


def recovery_email_enabled() -> bool:
    return _truthy(os.getenv('CHECKOUT_RECOVERY_EMAIL_ENABLED', '0'))


def _app():
    import app as app_module  # noqa: WPS433 - lazy on purpose
    return app_module


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def session_as_dict(obj) -> dict:
    if hasattr(obj, 'to_dict'):
        return obj.to_dict() or {}
    if isinstance(obj, dict):
        return obj
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


def _int_or_none(raw) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def session_email(d: dict) -> str:
    """Best email for the abandoned checkout (session first, then empty)."""
    details = d.get('customer_details') or {}
    for candidate in (details.get('email'), d.get('customer_email')):
        email = (candidate or '').strip()
        if email and '@' in email:
            return email
    return ''


def session_user_id(d: dict) -> Optional[int]:
    meta = d.get('metadata') or {}
    return _int_or_none(d.get('client_reference_id') or meta.get('user_id'))


def session_offer(d: dict) -> Optional[dict]:
    """Minutes + price_source + return_to from the expired session, or None."""
    meta = d.get('metadata') or {}
    minutes = _int_or_none(meta.get('minutes'))
    if minutes is None or minutes <= 0:
        return None
    price_source = (meta.get('price_source') or '').strip()
    if price_source not in ('slider', 'episode', 'default'):
        price_source = 'slider'
    return_to = (meta.get('return_to') or '').strip()[:500]
    return {
        'minutes': minutes,
        'price_source': price_source,
        'return_to': return_to,
        'pack': (meta.get('pack') or '')[:64] or None,
        'location': (meta.get('location') or meta.get('source') or '')[:64] or None,
        # Partial-preview finish: re-priced from the task on the way back.
        'task_id': (meta.get('task_id') or '')[:64] or None,
    }


def finish_offer_for(task_id, user):
    """Live partial-preview finish offer for a recovery email, or None."""
    if not task_id or user is None:
        return None
    A = _app()
    task = A.TranscriptionTask.query.filter_by(
        id=task_id, user_id=user.id).first()
    return A.partial_finish_offer(task, user) if task is not None else None


def offer_price_label(minutes: int) -> str:
    """Display price for *minutes* on the current curve (or fixed pack)."""
    import pricing_volume
    A = _app()
    if A.VOLUME_PRICING_ENABLED:
        return pricing_volume.format_usd(pricing_volume.price_cents(minutes))
    return A.DEFAULT_PACK_PRICE


def offer_phrase(minutes: int, price_source: str, price: str) -> str:
    """One short noun phrase for the body: what they were buying."""
    if price_source == 'episode':
        return f'a {minutes}-minute transcript for {price}'
    return f'{minutes} minutes for {price}'


# ---------------------------------------------------------------------------
# Click tokens
# ---------------------------------------------------------------------------

def _click_serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=CLICK_SALT)


def make_click_token(secret_key: str, *, user_id: int, minutes: int,
                     price_source: str, expired_session_id: str,
                     return_to: str = '', task_id: str = '') -> str:
    data: dict[str, Any] = {
        'u': int(user_id),
        'm': int(minutes),
        'ps': price_source,
        'cs': str(expired_session_id)[:128],
    }
    if return_to:
        data['rt'] = return_to[:500]
    if task_id:
        data['t'] = str(task_id)[:64]
    return _click_serializer(secret_key).dumps(data)


def parse_click_token(secret_key: str, token: str) -> Optional[dict]:
    try:
        data = _click_serializer(secret_key).loads(token, max_age=CLICK_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        data['u'] = int(data.get('u'))
        data['m'] = int(data.get('m'))
    except (TypeError, ValueError):
        return None
    if data['m'] <= 0:
        return None
    ps = (data.get('ps') or '').strip()
    if ps not in ('slider', 'episode', 'default'):
        return None
    data['ps'] = ps
    data['cs'] = str(data.get('cs') or '')[:128]
    data['rt'] = str(data.get('rt') or '')[:500]
    data['t'] = str(data.get('t') or '')[:64]
    return data


def click_url(public_base: str, secret_key: str, *, user_id: int, minutes: int,
              price_source: str, expired_session_id: str,
              return_to: str = '', task_id: str = '') -> str:
    token = make_click_token(
        secret_key, user_id=user_id, minutes=minutes,
        price_source=price_source, expired_session_id=expired_session_id,
        return_to=return_to, task_id=task_id)
    base = (public_base or '').rstrip('/')
    query = urlencode({
        'utm_source': 'email',
        'utm_medium': 'email',
        'utm_campaign': CAMPAIGN,
        'utm_content': 'resume',
    })
    return f'{base}/r/{token}?{query}'


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def build_bodies(*, minutes: int, price_source: str, price: str,
                 resume_url: str, unsub_url: str) -> tuple[str, str, str]:
    """(subject, text, html) for one recovery email."""
    import lifecycle_emails as L

    what = offer_phrase(minutes, price_source, price)
    subject = SUBJECT
    paras: list[tuple[str, Optional[str], Optional[str]]] = [
        (f"You started buying {what} on Podskrift but didn't finish "
         'checkout.', None, None),
        ('No subscription — minutes never expire. One payment picks up '
         'where you left off.',
         f'Complete your purchase ({price})', resume_url),
        ('If something got in the way, just reply. I read every email.',
         None, None),
    ]

    lines = ['Hi,', '']
    for text, label, url in paras:
        lines.append(text)
        if label and url:
            lines.append(f'{label}: {url}')
        lines.append('')
    lines.append(SIGNOFF)
    lines.append('')
    lines.append('\u2014')
    lines.append('Podskrift \u00b7 hello@podskrift.com \u00b7 https://podskrift.com')
    lines.append('You are getting this because you started a Podskrift checkout.')
    lines.append(f'Unsubscribe: {unsub_url}')
    text_body = '\n'.join(lines) + '\n'

    body = ['<p>Hi,</p>']
    first_cta = True
    for text, label, url in paras:
        body.append(f'<p>{html_lib.escape(text)}</p>')
        if label and url:
            safe_url = html_lib.escape(url, quote=True)
            if first_cta:
                body.append(
                    f'<p class="cta"><a class="btn" href="{safe_url}">'
                    f'{html_lib.escape(label)}</a></p>')
                first_cta = False
            else:
                body.append(
                    f'<p><a href="{safe_url}">{html_lib.escape(label)}</a></p>')
    body.append('<p>Sindre<br>Podskrift</p>')
    html_body = L._html_document(subject, ''.join(body), unsub_url)
    return subject, text_body, html_body


def sent_log_key(session_id: str) -> str:
    return f'checkout_recovery:{session_id}'


# ---------------------------------------------------------------------------
# Eligibility + send (webhook path)
# ---------------------------------------------------------------------------

def exclusion_reason(user) -> Optional[str]:
    """Why this user must not get a recovery email, or None if ok."""
    import lifecycle_emails as L
    import email_notify

    if user is None:
        return 'no_user'
    if int(user.id) in L.excluded_user_ids():
        return 'test_user'
    problem = L.email_problem(getattr(user, 'email', None))
    if problem:
        return problem
    if not email_notify.user_accepts_email(user):
        return 'unsubscribed' if getattr(user, 'email_unsubscribed_at', None) else 'no_email'
    # Same mail-pref gate as lifecycle: opting out of transcript-ready means
    # they asked for less mail.
    if getattr(user, 'email_transcript_ready', True) is False:
        return 'mail_pref_off'
    if getattr(user, 'openai_api_key', None):
        return 'byok'
    return None


def recently_sent(user_id: int, *, now: Optional[datetime] = None,
                  except_key: Optional[str] = None) -> bool:
    """True if a recovery email was claimed for this user in the last 7 days.

    ``except_key`` ignores the claim for the session we are handling (so a
    webhook retry returns ``already_sent`` from the unique claim, not cooldown).
    """
    A = _app()
    now = _aware(now) or datetime.now(timezone.utc)
    cutoff = now - COOLDOWN
    # SQLite may return naive UTC; compare as aware.
    rows = (A.EmailSentLog.query
            .filter(A.EmailSentLog.user_id == int(user_id),
                    A.EmailSentLog.kind == SENT_LOG_KIND)
            .order_by(A.EmailSentLog.created_at.desc())
            .limit(8)
            .all())
    for row in rows:
        if except_key and row.idempotency_key == except_key:
            continue
        created = _aware(row.created_at)
        if created is not None and created >= cutoff:
            return True
    return False


def user_has_purchase(user_id: int) -> bool:
    A = _app()
    return (A.CreditPurchase.query
            .filter_by(user_id=int(user_id), status='credited')
            .first()) is not None


def decide_skip(d: dict, user, *, now: Optional[datetime] = None) -> Optional[str]:
    """Pure-ish skip reason for an expired session + loaded user, or None."""
    if d.get('payment_status') == 'paid':
        return 'already_paid'
    # Require an email on the Checkout Session itself (customer_email /
    # customer_details) — do not invent a destination from the account alone.
    if not session_email(d):
        return 'no_email'
    if session_offer(d) is None:
        return 'no_offer'
    reason = exclusion_reason(user)
    if reason:
        return reason
    if user_has_purchase(user.id):
        return 'bought'
    return None


def handle_expired_session(obj, *, now: Optional[datetime] = None) -> str:
    """Best-effort recovery email for one expired Checkout Session.

    Never raises. Returns a short outcome string for logs/tests.
    """
    try:
        import email_notify
        import mail as mailer
        import analytics as product_analytics

        if not recovery_email_enabled():
            return 'disabled'
        if not mailer.mail_ready():
            return 'mail_not_ready'

        d = session_as_dict(obj)
        cs_id = (d.get('id') or '').strip()
        if not cs_id.startswith('cs_'):
            return 'bad_session'

        uid = session_user_id(d)
        if uid is None:
            return 'no_user'

        A = _app()
        user = A.db.session.get(A.User, uid)
        skip = decide_skip(d, user, now=now)
        if skip:
            return f'skipped_{skip}'

        offer = session_offer(d)
        assert offer is not None  # decide_skip checked
        to = session_email(d) or (user.email or '').strip()
        if not to:
            return 'skipped_no_email'

        key = sent_log_key(cs_id)
        if not email_notify.claim_email_send(
                A.db, A.EmailSentLog, user_id=user.id,
                kind=SENT_LOG_KIND, idempotency_key=key):
            return 'already_sent'
        # Cooldown after claim so same-session retries hit already_sent first.
        if recently_sent(user.id, now=now, except_key=key):
            email_notify.release_email_send(
                A.db, A.EmailSentLog, idempotency_key=key)
            return 'skipped_cooldown'

        try:
            price = offer_price_label(offer['minutes'])
            finish = finish_offer_for(offer.get('task_id'), user)
            if finish is not None:
                # Same price the click will charge (remaining-minutes test).
                offer = dict(offer, minutes=finish['minutes'])
                price = finish['price']
            public_base = A.PUBLIC_BASE_URL or ''
            secret_key = A.app.secret_key
            resume = click_url(
                public_base, secret_key,
                user_id=user.id,
                minutes=offer['minutes'],
                price_source=offer['price_source'],
                expired_session_id=cs_id,
                return_to=offer.get('return_to') or '',
                task_id=offer.get('task_id') or '',
            )
            unsub = email_notify.unsubscribe_url(public_base, secret_key, user.id)
            subject, text, html = build_bodies(
                minutes=offer['minutes'],
                price_source=offer['price_source'],
                price=price,
                resume_url=resume,
                unsub_url=unsub,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                'checkout recovery render failed (user %s, %s)', user.id, cs_id)
            email_notify.release_email_send(
                A.db, A.EmailSentLog, idempotency_key=key)
            return 'render_failed'

        suppressed = mailer.suppression_status(to)
        if suppressed not in (mailer.SUPPRESSION_CLEAR, mailer.SUPPRESSION_NO_ACCESS):
            email_notify.release_email_send(
                A.db, A.EmailSentLog, idempotency_key=key)
            return f'skipped_{suppressed}'

        outcome = mailer.send_email(
            to=to, subject=subject, text=text, html=html,
            headers=email_notify._list_unsubscribe_headers(unsub),
            tags=['checkout_recovery', CAMPAIGN],
            kind=CAMPAIGN,
            timeout=8,
            idempotency_key=key,
        )
        if outcome != mailer.SEND_SENT:
            if outcome == mailer.SEND_FAILED:
                email_notify.release_email_send(
                    A.db, A.EmailSentLog, idempotency_key=key)
            return outcome

        product_analytics.capture('checkout_recovery_sent', user.id, {
            'checkout_session_id': cs_id,
            'minutes': offer['minutes'],
            'price_source': offer['price_source'],
            'pack_sku': offer.get('pack'),
            'location': offer.get('location'),
            'campaign': CAMPAIGN,
        })
        return 'sent'
    except Exception:  # noqa: BLE001
        logger.exception('checkout recovery handler failed')
        return 'error'

