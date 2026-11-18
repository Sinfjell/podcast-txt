"""Customer feedback writer — shared by MCP, API, and the web widget.

Auth is the caller's job. This module validates payload shape, enforces
per-user DB rate limits (cross-worker), upserts transcript_quality ratings,
emits a PII-free PostHog event, and emails hello@podskrift.com when mail is on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import analytics as product_analytics
import mail as mailer
from models import EmailSentLog, Feedback, TranscriptionTask, db

logger = logging.getLogger(__name__)

FEEDBACK_TO = 'hello@podskrift.com'
FEEDBACK_SOURCES = frozenset({'mcp', 'api', 'web'})
FEEDBACK_KINDS = frozenset({
    'bug', 'feature', 'transcript_quality', 'other',
})
FEEDBACK_REASONS = frozenset({
    'wrong_language', 'bad_speakers', 'missing_parts',
    'bad_timestamps', 'other',
})
MESSAGE_MIN = 10
MESSAGE_MAX = 2000
RATE_LIMIT_HOUR = 5
RATE_LIMIT_DAY = 15
# Max notification emails per user per rolling day (covers upserts too, which
# are not row-rate-limited). Cross-worker via email_sent_log.
EMAIL_LIMIT_DAY = 15

# Thumbs → 1–5 scale used in the rating column.
_THUMBS_MAP = {
    'up': 5, 'down': 1, 'thumbsup': 5, 'thumbsdown': 1,
    'thumbs_up': 5, 'thumbs_down': 1, '+1': 5, '-1': 1,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_rating(raw) -> tuple[Optional[int], Optional[str]]:
    """Return (rating 1–5, error_message). None rating is ok when omitted."""
    if raw is None or raw == '':
        return None, None
    if isinstance(raw, bool):
        return (5 if raw else 1), None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = int(raw)
        if 1 <= n <= 5:
            return n, None
        return None, 'rating must be 1–5 (or thumbs up/down)'
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in _THUMBS_MAP:
            return _THUMBS_MAP[key], None
        try:
            n = int(key)
        except ValueError:
            return None, 'rating must be 1–5 (or thumbs up/down)'
        if 1 <= n <= 5:
            return n, None
        return None, 'rating must be 1–5 (or thumbs up/down)'
    return None, 'rating must be 1–5 (or thumbs up/down)'


def _rate_counts(user_id: int) -> tuple[int, int]:
    """(count_last_hour, count_last_day) of feedback rows for this user."""
    now = _now()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    # SQLite stores naive UTC; compare as naive for the common path.
    hour_naive = hour_ago.replace(tzinfo=None)
    day_naive = day_ago.replace(tzinfo=None)
    hour_n = (Feedback.query
              .filter(Feedback.user_id == user_id,
                      Feedback.created_at >= hour_naive)
              .count())
    day_n = (Feedback.query
             .filter(Feedback.user_id == user_id,
                     Feedback.created_at >= day_naive)
             .count())
    return hour_n, day_n


def _find_quality_row(user_id: int, task_id: str) -> Optional[Feedback]:
    return (Feedback.query
            .filter_by(user_id=user_id, task_id=task_id,
                       kind='transcript_quality')
            .first())


def _notify_email(row: Feedback, *, user_email: str | None = None) -> None:
    """Best-effort notify; never raises into the writer."""
    try:
        if not mailer.email_enabled():
            return
        day_ago = (_now() - timedelta(days=1)).replace(tzinfo=None)
        sent_n = (EmailSentLog.query
                  .filter(EmailSentLog.user_id == row.user_id,
                          EmailSentLog.kind == 'feedback',
                          EmailSentLog.created_at >= day_ago)
                  .count())
        if sent_n >= EMAIL_LIMIT_DAY:
            logger.info('feedback email skipped (daily cap) user=%s', row.user_id)
            return
        stamp = (row.updated_at or row.created_at or _now()).strftime('%Y%m%dT%H%M%S%f')
        claim_key = f'feedback:{row.id}:{stamp}'
        try:
            db.session.add(EmailSentLog(
                user_id=row.user_id, kind='feedback',
                idempotency_key=claim_key,
                created_at=_now().replace(tzinfo=None)))
            db.session.commit()
        except Exception:  # noqa: BLE001 — duplicate claim → already sent
            db.session.rollback()
            return
        subject = (
            f'[Podskrift feedback] {row.kind}'
            + (f' / {row.reason}' if row.reason else '')
            + f' via {row.source}'
        )
        lines = [
            f'kind: {row.kind}',
            f'source: {row.source}',
            f'user_id: {row.user_id}',
            f'user_email: {user_email or "(unknown)"}',
            f'task_id: {row.task_id or "(none)"}',
            f'reason: {row.reason or "(none)"}',
            f'rating: {row.rating if row.rating is not None else "(none)"}',
            f'client: {(row.client or "")[:200] or "(none)"}',
            f'created_at: {row.created_at}',
            '',
            'message:',
            row.message or '(empty)',
        ]
        mailer.send_email(
            to=FEEDBACK_TO,
            subject=subject,
            text='\n'.join(lines),
            kind='feedback',
            idempotency_key=claim_key,
        )
    except Exception:  # noqa: BLE001 — notify must never break submit
        logger.exception('feedback email failed for id=%s', getattr(row, 'id', None))


def submit_feedback(
    *,
    user_id: int,
    source: str,
    kind: str,
    message: str = '',
    task_id: str | None = None,
    reason: str | None = None,
    rating=None,
    client: str | None = None,
    user_email: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Validate and persist one feedback item.

    Returns (payload, http_status). payload always has ``ok`` or ``error``.
    """
    source = (source or '').strip().lower()
    kind = (kind or '').strip().lower()
    reason_raw = (reason or '').strip().lower() or None
    task_id = (task_id or '').strip() or None
    message = (message or '').strip()
    client = (client or '').strip()[:512] or None

    if source not in FEEDBACK_SOURCES:
        return {'error': 'invalid_source',
                'message': f'source must be one of: {", ".join(sorted(FEEDBACK_SOURCES))}'}, 400
    if kind not in FEEDBACK_KINDS:
        return {'error': 'invalid_kind',
                'message': f'kind must be one of: {", ".join(sorted(FEEDBACK_KINDS))}'}, 400

    if reason_raw is not None and reason_raw not in FEEDBACK_REASONS:
        return {'error': 'invalid_reason',
                'message': (
                    'reason must be one of: '
                    + ', '.join(sorted(FEEDBACK_REASONS))
                )}, 400

    rating_n, rating_err = parse_rating(rating)
    if rating_err:
        return {'error': 'invalid_rating', 'message': rating_err}, 400

    # Empty message allowed only for web transcript_quality with a reason.
    allow_empty = (
        kind == 'transcript_quality'
        and source == 'web'
        and reason_raw is not None
    )
    if not message:
        if not allow_empty:
            return {
                'error': 'message_required',
                'message': (
                    f'message must be {MESSAGE_MIN}–{MESSAGE_MAX} characters '
                    '(empty only for web transcript_quality with a reason).'
                ),
            }, 400
    elif len(message) < MESSAGE_MIN or len(message) > MESSAGE_MAX:
        return {
            'error': 'invalid_message',
            'message': f'message must be {MESSAGE_MIN}–{MESSAGE_MAX} characters.',
        }, 400

    if kind == 'transcript_quality' and not task_id:
        return {
            'error': 'task_id_required',
            'message': 'transcript_quality feedback requires task_id.',
        }, 400

    if task_id:
        task = db.session.get(TranscriptionTask, task_id)
        if task is None or task.user_id != user_id:
            return {
                'error': 'task_not_found',
                'message': 'Task not found.',
            }, 404

    existing = None
    if kind == 'transcript_quality' and task_id:
        existing = _find_quality_row(user_id, task_id)

    is_new = existing is None
    if is_new:
        hour_n, day_n = _rate_counts(user_id)
        if hour_n >= RATE_LIMIT_HOUR:
            return {
                'error': 'rate_limited',
                'message': (
                    f'Too many feedback submissions (max {RATE_LIMIT_HOUR}/hour). '
                    'Try again later.'
                ),
            }, 429
        if day_n >= RATE_LIMIT_DAY:
            return {
                'error': 'rate_limited',
                'message': (
                    f'Too many feedback submissions (max {RATE_LIMIT_DAY}/day). '
                    'Try again later.'
                ),
            }, 429

    now = _now().replace(tzinfo=None)
    if existing is not None:
        existing.message = message
        existing.reason = reason_raw
        existing.rating = rating_n
        existing.client = client or existing.client
        existing.source = source
        existing.updated_at = now
        row = existing
        db.session.commit()
        upserted = True
    else:
        row = Feedback(
            user_id=user_id,
            source=source,
            kind=kind,
            message=message,
            task_id=task_id,
            reason=reason_raw,
            rating=rating_n,
            client=client,
            created_at=now,
            updated_at=now,
        )
        db.session.add(row)
        db.session.commit()
        upserted = False

    product_analytics.capture(
        'feedback_submitted',
        user_id,
        {
            'kind': kind,
            'source': source,
            'reason': reason_raw,
            'rating': rating_n,
            'task_id': task_id,
        },
    )
    _notify_email(row, user_email=user_email)

    return {
        'ok': True,
        'id': row.id,
        'upserted': upserted,
        'kind': kind,
        'source': source,
    }, 200 if upserted else 201
