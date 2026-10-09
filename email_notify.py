"""Transactional email content + preferences: transcript-ready and new-episode digests.

Sending is gated by mail.mail_ready(); preference checks and idempotency live here
so callers never need to know about Mailgun. All public helpers never raise into
request / worker paths.
"""

from __future__ import annotations

import hashlib
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
NEW_EPISODES = 'new_episodes'

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


def with_utm(url: str, campaign: str) -> str:
    """Append utm_source=email&utm_campaign=<campaign> (preserves existing query)."""
    if not url:
        return url
    sep = '&' if ('?' in url) else '?'
    return f'{url}{sep}{urlencode({"utm_source": "email", "utm_campaign": campaign})}'


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


def user_wants_feed_alerts(user, feed) -> bool:
    if not user_accepts_email(user):
        return False
    pref = getattr(feed, 'email_new_episodes', True)
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
) -> tuple[str, str, str]:
    """Return (subject, text, html).

    Partial free-preview jobs use “your free preview is ready” wording so we
    never imply the full episode was transcribed.
    """
    show = (podcast_name or '').strip() or 'your podcast'
    ep = (episode_title or '').strip() or 'Episode'
    link = with_utm(transcript_url, TRANSCRIPT_READY)
    if is_partial:
        subject = f'Free preview ready: {ep}'
        lead = (
            f'Your free preview of “{ep}” from {show} is ready. '
            f'Open it to read the first stretch, then buy minutes or add your '
            f'own OpenAI key to finish the rest.'
        )
        cta_label = 'Open your free preview'
        follow = (
            'Follow this podcast in Podskrift to get new episodes and optional alerts.'
        )
    else:
        subject = f'Transcript ready: {ep}'
        lead = f'Your transcript of “{ep}” from {show} is ready.'
        cta_label = 'Open the transcript'
        follow = (
            'Follow this podcast in Podskrift to get new episodes and optional alerts.'
        )
    text = (
        f'{lead}\n\n'
        f'{cta_label}: {link}\n\n'
        f'{follow}\n'
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
    html = (
        f'{html_lead}'
        f'<p><a href="{safe_link}">{safe_cta}</a></p>'
        f'<p>{html_lib.escape(follow)}</p>'
        f'{_footer_html(unsub_url)}'
    )
    return subject, text, html


def build_new_episodes_bodies(
    *,
    items: list[dict[str, Any]],
    unsub_url: str,
) -> tuple[str, str, str]:
    """items: {podcast_name, episode_title, transcribe_url}."""
    n = len(items)
    subject = (
        'New episode from a podcast you follow'
        if n == 1
        else f'{n} new episodes from podcasts you follow'
    )
    lines = ['New episodes from podcasts you follow:', '']
    html_parts = [
        '<p>New episodes from podcasts you follow:</p><ul>',
    ]
    for item in items:
        show = (item.get('podcast_name') or '').strip() or 'Podcast'
        ep = (item.get('episode_title') or '').strip() or 'Episode'
        url = with_utm(item.get('transcribe_url') or '', NEW_EPISODES)
        lines.append(f'• {show} — {ep}')
        lines.append(f'  Transcribe: {url}')
        lines.append('')
        html_parts.append(
            '<li><strong>{}</strong> — {}<br>'
            '<a href="{}">Transcribe this episode</a></li>'.format(
                html_lib.escape(show),
                html_lib.escape(ep),
                html_lib.escape(url, quote=True),
            )
        )
    html_parts.append('</ul>')
    text = '\n'.join(lines) + _footer_text(unsub_url)
    html = ''.join(html_parts) + _footer_html(unsub_url)
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
        unsub = unsubscribe_url(public_base_url, secret_key, user.id)
        is_partial = task_is_partial_preview(task)
        subject, text, html = build_transcript_ready_bodies(
            podcast_name=getattr(task, 'podcast_name', None) or '',
            episode_title=getattr(task, 'episode_title', None) or '',
            transcript_url=transcript_url,
            unsub_url=unsub,
            is_partial=is_partial,
        )
        ok = mailer.send_email(
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
        if not ok:
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


def episode_guid(entry_or_ep: dict) -> str:
    """Stable id for an episode dict from get_episodes_from_rss / poller."""
    for key in ('guid', 'id', 'audio_url', 'title'):
        val = (entry_or_ep.get(key) or '').strip()
        if val:
            return val[:1024]
    return ''


def episode_idempotency_key(user_id: int, feed_id: int, guid: str) -> str:
    digest = hashlib.sha256(guid.encode('utf-8', errors='replace')).hexdigest()[:32]
    return f'episode_alert:{user_id}:{feed_id}:{digest}'


def notify_new_episodes_digest(
    *,
    db,
    user,
    items: list[dict[str, Any]],
    EmailSentLog,
    public_base_url: str,
    secret_key: str,
    claim_keys: list[str],
) -> bool:
    """Send one digest. claim_keys must already be reserved (one per episode)."""
    try:
        if not mailer.mail_ready() or not items:
            return False
        if not user_accepts_email(user):
            return False
        unsub = unsubscribe_url(public_base_url, secret_key, user.id)
        subject, text, html = build_new_episodes_bodies(
            items=items, unsub_url=unsub)
        # Digest covers many episodes; key the Message-Id on the first claim.
        digest_key = claim_keys[0] if claim_keys else ''
        ok = mailer.send_email(
            to=user.email,
            subject=subject,
            text=text,
            html=html,
            headers=_list_unsubscribe_headers(unsub),
            tags=[NEW_EPISODES],
            kind=NEW_EPISODES,
            idempotency_key=digest_key,
        )
        if not ok:
            for key in claim_keys:
                release_email_send(db, EmailSentLog, idempotency_key=key)
            return False
        product_analytics.capture(
            'email_sent',
            user.id,
            {'type': NEW_EPISODES, 'episode_count': len(items)},
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception('notify_new_episodes_digest failed for user %s',
                         getattr(user, 'id', '?'))
        for key in claim_keys:
            release_email_send(db, EmailSentLog, idempotency_key=key)
        return False
