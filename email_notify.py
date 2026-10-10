"""Transactional email content + preferences: transcript-ready mail.

Sending is gated by mail.mail_ready(); preference checks and idempotency live here
so callers never need to know about Mailgun. All public helpers never raise into
request / worker paths.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import analytics as product_analytics
import mail as mailer

logger = logging.getLogger(__name__)

UNSUBSCRIBE_SALT = 'podskrift-email-unsub-v1'
UNSUBSCRIBE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # five years

TRANSCRIPT_READY = 'transcript_ready'
PASSWORD_RESET = 'password_reset'
PASSWORD_CHANGED = 'password_changed'

# Job finished quickly *and* the owner was still polling → skip the email.
TRANSCRIPT_READY_MIN_DURATION_SEC = 60
TRANSCRIPT_READY_AWAY_SEC = 120


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=UNSUBSCRIBE_SALT)


def make_unsubscribe_token(secret_key: str, user_id: int) -> str:
    return _serializer(secret_key).dumps({'uid': int(user_id)})


def parse_unsubscribe_token(secret_key: str, token: str) -> Optional[int]:
    try:
        data = _serializer(secret_key).loads(token, max_age=UNSUBSCRIBE_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return int(data.get('uid'))
    except (TypeError, ValueError):
        return None


def unsubscribe_url(public_base: str, secret_key: str, user_id: int) -> str:
    token = make_unsubscribe_token(secret_key, user_id)
    base = (public_base or '').rstrip('/')
    path = f'/email/unsubscribe/{token}'
    return f'{base}{path}' if base else path


def with_utm(url: str, campaign: str, content: str | None = None,
             ref: str | None = None) -> str:
    """Append utm_source=email&utm_campaign=<campaign> (preserves existing query).

    Optional utm_content / ref mark pack CTAs so the browser can fire
    email_offer_clicked on landing (cookieless-safe capture in base.html).
    """
    if not url:
        return url
    params = {'utm_source': 'email', 'utm_campaign': campaign}
    if content:
        params['utm_content'] = content
    if ref:
        params['ref'] = ref
    sep = '&' if ('?' in url) else '?'
    return f'{url}{sep}{urlencode(params)}'


def _list_unsubscribe_headers(unsub_url: str) -> dict[str, str]:
    return {
        'List-Unsubscribe': f'<{unsub_url}>',
        'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
    }


def _footer_text(unsub_url: str) -> str:
    return (
        '\n\n—\n'
        'Podskrift · hello@podskrift.com · https://podskrift.com\n'
        f'Unsubscribe: {unsub_url}\n'
    )


def _footer_html(unsub_url: str) -> str:
    safe = html_lib.escape(unsub_url, quote=True)
    return (
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">'
        '<p style="font-size:12px;color:#666;line-height:1.5">'
        'Podskrift · '
        '<a href="mailto:hello@podskrift.com">hello@podskrift.com</a> · '
        '<a href="https://podskrift.com">podskrift.com</a><br>'
        f'<a href="{safe}">Unsubscribe</a>'
        '</p>'
    )


def user_accepts_email(user) -> bool:
    """Global gate: registered address present and not globally unsubscribed."""
    if user is None:
        return False
    if getattr(user, 'email_unsubscribed_at', None) is not None:
        return False
    email = (getattr(user, 'email', None) or '').strip()
    return bool(email and '@' in email)


def user_wants_transcript_ready(user) -> bool:
    if not user_accepts_email(user):
        return False
    # Default ON when the column is missing/NULL on a half-migrated row.
    pref = getattr(user, 'email_transcript_ready', True)
    return pref is not False


def claim_email_send(db, EmailSentLog, *, user_id: int, kind: str,
                     idempotency_key: str) -> bool:
    """Insert a sent-log row. Returns True if this process won the claim.

    Unique on idempotency_key — a concurrent duplicate loses and must not send.
    """
    from sqlalchemy.exc import IntegrityError

    row = EmailSentLog(
        user_id=user_id,
        kind=kind,
        idempotency_key=idempotency_key,
    )
    try:
        db.session.add(row)
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('email sent-log claim failed for %s', idempotency_key)
        return False


def release_email_send(db, EmailSentLog, *, idempotency_key: str) -> None:
    """Delete a claim after a failed send so a later retry can try again."""
    try:
        row = EmailSentLog.query.filter_by(idempotency_key=idempotency_key).first()
        if row is not None:
            db.session.delete(row)
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('email sent-log release failed for %s', idempotency_key)


def should_send_transcript_ready(task, *, now: Optional[datetime] = None) -> bool:
    """True when the job was slow or the owner left the result page.

    Rule: send if duration > 60s OR last status poll older than 2 minutes
    (or never polled). Instant jobs where the user is still watching are skipped.
    """
    now = now or datetime.now(timezone.utc)
    started = getattr(task, 'started_at', None)
    completed = getattr(task, 'completed_at', None) or now
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        duration = (completed - started).total_seconds()
        if duration > TRANSCRIPT_READY_MIN_DURATION_SEC:
            return True
    last = getattr(task, 'last_polled_at', None)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() > TRANSCRIPT_READY_AWAY_SEC


def task_is_partial_preview(task) -> bool:
    """True when the completed job is a free trial preview (partial_meta set)."""
    raw = getattr(task, 'partial_meta', None) or ''
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get('partial_seconds') is not None


def build_transcript_ready_bodies(
    *,
    podcast_name: str,
    episode_title: str,
    transcript_url: str,
    unsub_url: str,
    is_partial: bool = False,
    pack_url: str | None = None,
) -> tuple[str, str, str]:
    """Return (subject, text, html).

    Partial free-preview jobs use “your free preview is ready” wording so we
    never imply the full episode was transcribed. When ``pack_url`` is set,
    a single plain line offers the 300 min / $5 pack (unsubscribe unchanged).
    """
    show = (podcast_name or '').strip() or 'your podcast'
    ep = (episode_title or '').strip() or 'Episode'
    link = with_utm(transcript_url, TRANSCRIPT_READY)
    pack_link = ''
    if pack_url:
        pack_link = with_utm(
            pack_url, TRANSCRIPT_READY, content='pack_offer', ref='email_pack')
    if is_partial:
        subject = f'Free preview ready: {ep}'
        lead = (
            f'Your free preview of “{ep}” from {show} is ready. '
            f'Open it to read the first stretch, then buy minutes or add your '
            f'own OpenAI key to finish the rest.'
        )
        cta_label = 'Open your free preview'
        tip = 'Search for another episode whenever you are ready.'
    else:
        subject = f'Transcript ready: {ep}'
        lead = f'Your transcript of “{ep}” from {show} is ready.'
        cta_label = 'Open the transcript'
        tip = 'Search for another episode whenever you are ready.'
    pack_line = (
        f'Need more minutes? 300 min for $5: {pack_link}\n\n'
        if pack_link else ''
    )
    text = (
        f'{lead}\n\n'
        f'{cta_label}: {link}\n\n'
        f'{pack_line}'
        f'{tip}\n'
        f'{_footer_text(unsub_url)}'
    )
    safe_ep = html_lib.escape(ep)
    safe_show = html_lib.escape(show)
    safe_link = html_lib.escape(link, quote=True)
    safe_cta = html_lib.escape(cta_label)
    if is_partial:
        html_lead = (
            f'<p>Your free preview of <strong>{safe_ep}</strong> from '
            f'{safe_show} is ready. Open it to read the first stretch, then '
            f'buy minutes or add your own OpenAI key to finish the rest.</p>'
        )
    else:
        html_lead = (
            f'<p>Your transcript of <strong>{safe_ep}</strong> from '
            f'{safe_show} is ready.</p>'
        )
    html_pack = ''
    if pack_link:
        safe_pack = html_lib.escape(pack_link, quote=True)
        html_pack = (
            f'<p>Need more minutes? '
            f'<a href="{safe_pack}">300 min for $5</a>.</p>'
        )
    html = (
        f'{html_lead}'
        f'<p><a href="{safe_link}">{safe_cta}</a></p>'
        f'{html_pack}'
        f'<p>{html_lib.escape(tip)}</p>'
        f'{_footer_html(unsub_url)}'
    )
    return subject, text, html


def notify_transcript_ready(
    *,
    db,
    user,
    task,
    EmailSentLog,
    public_base_url: str,
    secret_key: str,
) -> bool:
    """Send transcript-ready mail if prefs + timing + idempotency allow. Never raises."""
    try:
        if not mailer.mail_ready():
            return False
        if not user_wants_transcript_ready(user):
            return False
        if not should_send_transcript_ready(task):
            return False
        task_id = getattr(task, 'id', None)
        if not task_id:
            return False
        key = f'transcript_ready:{task_id}'
        if not claim_email_send(
            db, EmailSentLog, user_id=user.id, kind=TRANSCRIPT_READY,
            idempotency_key=key,
        ):
            return False
        base = (public_base_url or '').rstrip('/')
        transcript_path = f'/transcription/{task_id}'
        transcript_url = f'{base}{transcript_path}' if base else transcript_path
        pack_url = f'{base}/pricing' if base else '/pricing'
        unsub = unsubscribe_url(public_base_url, secret_key, user.id)
        is_partial = task_is_partial_preview(task)
        subject, text, html = build_transcript_ready_bodies(
            podcast_name=getattr(task, 'podcast_name', None) or '',
            episode_title=getattr(task, 'episode_title', None) or '',
            transcript_url=transcript_url,
            unsub_url=unsub,
            is_partial=is_partial,
            pack_url=pack_url,
        )
        outcome = mailer.send_email(
            to=user.email,
            subject=subject,
            text=text,
            html=html,
            headers=_list_unsubscribe_headers(unsub),
            tags=[TRANSCRIPT_READY],
            kind=TRANSCRIPT_READY,
            timeout=8,
            idempotency_key=key,
        )
        if outcome != mailer.SEND_SENT:
            # Clear failures can retry later; ambiguous (timeout) keeps the
            # claim so a late accept is not duplicated.
            if outcome == mailer.SEND_FAILED:
                release_email_send(db, EmailSentLog, idempotency_key=key)
            return False
        props = {'type': TRANSCRIPT_READY}
        if is_partial:
            props['partial'] = True
        product_analytics.capture('email_sent', user.id, props)
        return True
    except Exception:  # noqa: BLE001
        logger.exception('notify_transcript_ready failed for task %s',
                         getattr(task, 'id', '?'))
        return False


# ---------------------------------------------------------------------------
# Password reset (security mail — always sent when mail_ready, ignore prefs)
# ---------------------------------------------------------------------------

def build_password_reset_bodies(reset_url: str) -> tuple[str, str, str]:
    """Subject, text, html for a one-time reset link. No email address in body."""
    safe = html_lib.escape(reset_url, quote=True)
    subject = 'Reset your Podskrift password'
    text = (
        'Reset your Podskrift password\n\n'
        'We received a request to reset the password for your Podskrift account.\n'
        'Open this link within 60 minutes to choose a new password:\n\n'
        f'{reset_url}\n\n'
        'If you did not ask for this, you can ignore this email — your password '
        'stays the same.\n\n'
        '—\nPodskrift · hello@podskrift.com · https://podskrift.com\n'
    )
    html = (
        '<p style="font-size:15px;line-height:1.5;color:#111">'
        'We received a request to reset the password for your Podskrift account.'
        '</p>'
        f'<p style="margin:24px 0"><a href="{safe}" '
        'style="display:inline-block;background:#059669;color:#fff;'
        'text-decoration:none;padding:12px 18px;border-radius:8px;'
        'font-weight:600">Choose a new password</a></p>'
        '<p style="font-size:13px;line-height:1.5;color:#666">'
        'This link expires in 60 minutes. If you did not ask for this, ignore '
        'this email — your password stays the same.</p>'
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">'
        '<p style="font-size:12px;color:#666;line-height:1.5">'
        'Podskrift · '
        '<a href="mailto:hello@podskrift.com">hello@podskrift.com</a> · '
        '<a href="https://podskrift.com">podskrift.com</a>'
        '</p>'
    )
    return subject, text, html


def build_password_changed_bodies() -> tuple[str, str, str]:
    """Short confirmation that the password was changed."""
    subject = 'Your Podskrift password was changed'
    text = (
        'Your Podskrift password was changed\n\n'
        'The password for your Podskrift account was just updated. If you did '
        'this, no further action is needed.\n\n'
        'If you did not change your password, contact us at hello@podskrift.com '
        'right away.\n\n'
        '—\nPodskrift · hello@podskrift.com · https://podskrift.com\n'
    )
    html = (
        '<p style="font-size:15px;line-height:1.5;color:#111">'
        'The password for your Podskrift account was just updated. If you did '
        'this, no further action is needed.'
        '</p>'
        '<p style="font-size:13px;line-height:1.5;color:#666">'
        'If you did not change your password, contact us at '
        '<a href="mailto:hello@podskrift.com">hello@podskrift.com</a> '
        'right away.</p>'
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">'
        '<p style="font-size:12px;color:#666;line-height:1.5">'
        'Podskrift · '
        '<a href="mailto:hello@podskrift.com">hello@podskrift.com</a> · '
        '<a href="https://podskrift.com">podskrift.com</a>'
        '</p>'
    )
    return subject, text, html


def send_password_reset_email(*, to: str, reset_url: str, user_id: int) -> str:
    """Send the reset link. Never logs the address. Returns mailer outcome."""
    if not mailer.mail_ready():
        return mailer.SEND_FAILED
    subject, text, html = build_password_reset_bodies(reset_url)
    # No unsubscribe footer: this is account-security mail.
    return mailer.send_email(
        to=to,
        subject=subject,
        text=text,
        html=html,
        tags=[PASSWORD_RESET],
        kind=PASSWORD_RESET,
        idempotency_key=f'password-reset:{user_id}:{hashlib.sha256(reset_url.encode()).hexdigest()[:16]}',
    )


def send_password_changed_email(*, to: str, user_id: int) -> str:
    """Confirm a successful password change. Ignores marketing prefs."""
    if not mailer.mail_ready():
        return mailer.SEND_FAILED
    subject, text, html = build_password_changed_bodies()
    return mailer.send_email(
        to=to,
        subject=subject,
        text=text,
        html=html,
        tags=[PASSWORD_CHANGED],
        kind=PASSWORD_CHANGED,
        # Fresh Message-Id each send; a second change is a new notice.
        idempotency_key='',
    )
