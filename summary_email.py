"""Summary-by-email for followed shows (feature-flagged).

When the poller finds a new episode for feeds with email_summaries=1:
enqueue ONE shared transcription (cache by source_audio_url), summarize,
then email each eligible subscriber a TL;DR (no full transcript). Subscribers
get a read-only TranscriptionTask copy so existing ownership checks work.

Free tier v1: 1 show per user, 14 days from opt-in. Global daily budget
SUMMARY_EMAIL_DAILY_MINUTES. Skip episodes longer than SUMMARY_EMAIL_MAX_MINUTES.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import analytics as product_analytics
import email_notify
import mail as mailer
import summary as summary_mod

logger = logging.getLogger(__name__)

SUMMARY_EMAIL = 'summary_email'
DEFAULT_DAILY_MINUTES = 300
DEFAULT_MAX_EPISODE_MINUTES = 120
FREE_TRIAL_DAYS = 14
MAX_SHOWS_PER_USER = 1

_sentry_kinds: set[str] = set()


def _truthy(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def summary_email_enabled() -> bool:
    return _truthy(os.getenv('SUMMARY_EMAIL_ENABLED', '0'))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return int(default)


def daily_budget_seconds() -> int:
    return _env_int('SUMMARY_EMAIL_DAILY_MINUTES', DEFAULT_DAILY_MINUTES) * 60


def max_episode_seconds() -> int:
    return _env_int('SUMMARY_EMAIL_MAX_MINUTES', DEFAULT_MAX_EPISODE_MINUTES) * 60


def reset_sentry_kinds_for_tests() -> None:
    _sentry_kinds.clear()


def _report_once(kind: str, exc: BaseException | None = None, *, message: str = ''):
    if kind in _sentry_kinds:
        return
    _sentry_kinds.add(kind)
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        if exc is not None:
            sentry_sdk.capture_exception(
                exc,
                fingerprint=['summary_email', kind],
                tags={'summary_email.kind': kind},
            )
        else:
            sentry_sdk.capture_message(
                message or f'Summary-email failure: {kind}',
                level='error',
                fingerprint=['summary_email', kind],
                tags={'summary_email.kind': kind},
            )
    except Exception:  # noqa: BLE001
        pass


def job_idempotency_key(rss_url: str, episode_guid: str) -> str:
    a = hashlib.sha256((rss_url or '').encode('utf-8', errors='replace')).hexdigest()[:24]
    b = hashlib.sha256((episode_guid or '').encode('utf-8', errors='replace')).hexdigest()[:24]
    return f'summary_email_job:{a}:{b}'


def email_idempotency_key(user_id: int, job_id: int) -> str:
    return f'summary_email:{user_id}:{job_id}'


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def feed_summary_trial_active(feed, *, now: Optional[datetime] = None) -> bool:
    """True while within FREE_TRIAL_DAYS of opt-in (or no start stamped yet)."""
    now = now or datetime.now(timezone.utc)
    started = getattr(feed, 'summary_email_trial_started_at', None)
    if started is None:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return now <= started + timedelta(days=FREE_TRIAL_DAYS)


def feed_summary_trial_expired(feed, *, now: Optional[datetime] = None) -> bool:
    return not feed_summary_trial_active(feed, now=now)


def user_wants_summary_email(user, feed) -> bool:
    if not email_notify.user_accepts_email(user):
        return False
    return bool(getattr(feed, 'email_summaries', False))


def count_user_summary_feeds(db, SavedFeed, user_id: int, *, exclude_feed_id=None) -> int:
    q = SavedFeed.query.filter_by(user_id=user_id, email_summaries=True)
    if exclude_feed_id is not None:
        q = q.filter(SavedFeed.id != exclude_feed_id)
    return q.count()


def can_opt_in_summary_email(db, SavedFeed, user_id: int, *, exclude_feed_id=None) -> bool:
    """Free tier v1: at most one show with summary-email per user."""
    return count_user_summary_feeds(
        db, SavedFeed, user_id, exclude_feed_id=exclude_feed_id,
    ) < MAX_SHOWS_PER_USER


def stamp_summary_trial_start(feed, *, now: Optional[datetime] = None) -> None:
    if getattr(feed, 'summary_email_trial_started_at', None) is None:
        feed.summary_email_trial_started_at = now or datetime.now(timezone.utc)


def reserve_daily_budget(db, SummaryEmailBudgetDay, *, seconds: int, day: Optional[str] = None) -> bool:
    """Atomically reserve `seconds` against today's global cap. One UPDATE."""
    from sqlalchemy import text

    seconds = max(0, int(seconds))
    if seconds <= 0:
        return True
    day = day or utc_today()
    limit = daily_budget_seconds()
    # Ensure row exists.
    existing = db.session.get(SummaryEmailBudgetDay, day)
    if existing is None:
        try:
            db.session.add(SummaryEmailBudgetDay(day=day, seconds_used=0))
            db.session.commit()
        except Exception:  # noqa: BLE001 — race with another worker
            db.session.rollback()
    result = db.session.execute(text("""
        UPDATE summary_email_budget_days
           SET seconds_used = seconds_used + :n
         WHERE day = :day
           AND seconds_used + :n <= :limit
    """), {'n': seconds, 'day': day, 'limit': limit})
    db.session.commit()
    return result.rowcount == 1


def release_daily_budget(db, *, seconds: int, day: Optional[str] = None) -> None:
    from sqlalchemy import text

    seconds = max(0, int(seconds))
    if seconds <= 0:
        return
    day = day or utc_today()
    try:
        db.session.execute(text("""
            UPDATE summary_email_budget_days
               SET seconds_used = CASE
                   WHEN seconds_used >= :n THEN seconds_used - :n
                   ELSE 0 END
             WHERE day = :day
        """), {'n': seconds, 'day': day})
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('release_daily_budget failed for %s', day)


def find_cached_completed_task(db, TranscriptionTask, audio_url: str):
    """Reuse any completed non-partial transcript for this audio URL."""
    if not audio_url:
        return None
    rows = (
        TranscriptionTask.query
        .filter_by(source_audio_url=audio_url, status='completed')
        .order_by(TranscriptionTask.completed_at.desc())
        .limit(20)
        .all()
    )
    for task in rows:
        if email_notify.task_is_partial_preview(task):
            continue
        if not (task.transcript_text or '').strip():
            continue
        return task
    return None


def enqueue_job(
    db,
    SummaryEmailJob,
    *,
    rss_url: str,
    audio_url: str,
    episode_guid: str,
    episode_title: str = '',
    podcast_name: str = '',
    duration_seconds: Optional[float] = None,
) -> Optional[Any]:
    """Insert a queued job if new. Returns the row (existing or new) or None."""
    from sqlalchemy.exc import IntegrityError

    key = job_idempotency_key(rss_url, episode_guid)
    existing = SummaryEmailJob.query.filter_by(idempotency_key=key).first()
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    row = SummaryEmailJob(
        idempotency_key=key,
        rss_url=rss_url,
        audio_url=audio_url,
        episode_guid=episode_guid,
        episode_title=(episode_title or 'Episode')[:512],
        podcast_name=(podcast_name or '')[:512] or None,
        duration_seconds=duration_seconds,
        status='queued',
        created_at=now,
        updated_at=now,
    )
    try:
        db.session.add(row)
        db.session.commit()
        return row
    except IntegrityError:
        db.session.rollback()
        return SummaryEmailJob.query.filter_by(idempotency_key=key).first()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('enqueue summary_email job failed')
        _report_once('enqueue', None, message='enqueue failed')
        return None


def _touch(job) -> None:
    job.updated_at = datetime.now(timezone.utc)


def _mark(db, job, status: str, **fields) -> None:
    job.status = status
    for k, v in fields.items():
        setattr(job, k, v)
    _touch(job)
    db.session.commit()


def create_subscriber_copy(
    db,
    TranscriptionTask,
    *,
    source_task,
    user_id: int,
) -> Any:
    """Read-only completed task owned by the subscriber (simplest safe access)."""
    # Reuse an existing copy for this user + source.
    existing = (
        TranscriptionTask.query
        .filter_by(user_id=user_id, summary_source_task_id=source_task.id)
        .first()
    )
    if existing is not None:
        return existing
    # Also reuse if they already transcribed the same audio themselves.
    own = (
        TranscriptionTask.query
        .filter_by(
            user_id=user_id,
            source_audio_url=source_task.source_audio_url,
            status='completed',
        )
        .order_by(TranscriptionTask.completed_at.desc())
        .first()
    )
    if own is not None and not email_notify.task_is_partial_preview(own):
        # Ensure summary fields are present for the email link experience.
        if (getattr(own, 'summary_status', None) != 'ready'
                and getattr(source_task, 'summary_status', None) == 'ready'):
            own.summary_json = source_task.summary_json
            own.summary_status = source_task.summary_status
            own.summary_model = source_task.summary_model
            own.summary_prompt_tokens = source_task.summary_prompt_tokens
            own.summary_completion_tokens = source_task.summary_completion_tokens
            own.summary_cost_usd_est = source_task.summary_cost_usd_est
            db.session.commit()
        return own

    copy = TranscriptionTask(
        id=str(uuid.uuid4()),
        user_id=user_id,
        episode_title=source_task.episode_title or 'Episode',
        rss_url=source_task.rss_url,
        status='completed',
        progress=100,
        download_progress=100,
        transcript_text=source_task.transcript_text,
        segments_json=source_task.segments_json,
        language=source_task.language,
        audio_duration=source_task.audio_duration,
        transcription_time=0,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        podcast_name=source_task.podcast_name,
        artwork_url=source_task.artwork_url,
        episode_published=source_task.episode_published,
        source_audio_url=source_task.source_audio_url,
        phase='completed',
        trial_settled=True,
        summary_json=source_task.summary_json,
        summary_status=source_task.summary_status,
        summary_model=source_task.summary_model,
        summary_prompt_tokens=source_task.summary_prompt_tokens,
        summary_completion_tokens=source_task.summary_completion_tokens,
        summary_cost_usd_est=source_task.summary_cost_usd_est,
        summary_source_task_id=source_task.id,
    )
    db.session.add(copy)
    db.session.commit()
    return copy


def build_summary_email_bodies(
    *,
    podcast_name: str,
    episode_title: str,
    summary: dict,
    transcript_url: str,
    unsub_url: str,
    trial_expired: bool = False,
) -> tuple[str, str, str]:
    show = (podcast_name or '').strip() or 'your podcast'
    ep = (episode_title or '').strip() or 'Episode'
    link = email_notify.with_utm(transcript_url, 'summary')
    tldr = (summary.get('tldr') or '').strip()
    points = summary.get('key_points') or []
    quotes = summary.get('quotes') or []

    subject = f'Summary: {ep}'
    lines = [
        f'{show}',
        f'{ep}',
        '',
    ]
    if summary.get('is_partial'):
        lines.append('(This summary covers a partial preview of the episode.)')
        lines.append('')
    if tldr:
        lines.append(f'TL;DR: {tldr}')
        lines.append('')
    if points:
        lines.append('Key points:')
        for p in points:
            lines.append(f'• {p}')
        lines.append('')
    if quotes:
        lines.append('Notable quotes:')
        for q in quotes:
            lines.append(f'“{q}”')
        lines.append('')
    lines.append(f'Read the full transcript on Podskrift: {link}')
    if trial_expired:
        lines.append('')
        lines.append(
            'Your free summary-email trial for this show has ended. '
            'Buy minutes on Podskrift to keep getting episode summaries.'
        )
    text = '\n'.join(lines) + email_notify._footer_text(unsub_url)

    safe_show = html_lib.escape(show)
    safe_ep = html_lib.escape(ep)
    safe_link = html_lib.escape(link, quote=True)
    html_parts = [
        f'<p><strong>{safe_show}</strong><br>{safe_ep}</p>',
    ]
    if summary.get('is_partial'):
        html_parts.append(
            '<p><em>This summary covers a partial preview of the episode.</em></p>'
        )
    if tldr:
        html_parts.append(f'<p><strong>TL;DR:</strong> {html_lib.escape(tldr)}</p>')
    if points:
        html_parts.append('<p><strong>Key points</strong></p><ul>')
        for p in points:
            html_parts.append(f'<li>{html_lib.escape(p)}</li>')
        html_parts.append('</ul>')
    if quotes:
        html_parts.append('<p><strong>Notable quotes</strong></p><ul>')
        for q in quotes:
            html_parts.append(f'<li>“{html_lib.escape(q)}”</li>')
        html_parts.append('</ul>')
    html_parts.append(
        f'<p><a href="{safe_link}">Read the full transcript on Podskrift</a></p>'
    )
    if trial_expired:
        html_parts.append(
            '<p>Your free summary-email trial for this show has ended. '
            'Buy minutes on Podskrift to keep getting episode summaries.</p>'
        )
    html = ''.join(html_parts) + email_notify._footer_html(unsub_url)
    return subject, text, html


def notify_summary_email(
    *,
    db,
    user,
    task_copy,
    summary: dict,
    EmailSentLog,
    public_base_url: str,
    secret_key: str,
    job_id: int,
    trial_expired: bool = False,
) -> bool:
    """Send one summary email. Idempotent via email_sent_log. Never raises."""
    try:
        if not mailer.mail_ready():
            return False
        if not email_notify.user_accepts_email(user):
            return False
        key = email_idempotency_key(user.id, job_id)
        if not email_notify.claim_email_send(
            db, EmailSentLog, user_id=user.id, kind=SUMMARY_EMAIL,
            idempotency_key=key,
        ):
            return False
        base = (public_base_url or '').rstrip('/')
        path = f'/transcription/{task_copy.id}'
        transcript_url = f'{base}{path}' if base else path
        unsub = email_notify.unsubscribe_url(public_base_url, secret_key, user.id)
        subject, text, html = build_summary_email_bodies(
            podcast_name=getattr(task_copy, 'podcast_name', None) or '',
            episode_title=getattr(task_copy, 'episode_title', None) or '',
            summary=summary,
            transcript_url=transcript_url,
            unsub_url=unsub,
            trial_expired=trial_expired,
        )
        ok = mailer.send_email(
            to=user.email,
            subject=subject,
            text=text,
            html=html,
            headers=email_notify._list_unsubscribe_headers(unsub),
            tags=[SUMMARY_EMAIL],
            kind=SUMMARY_EMAIL,
            timeout=8,
            idempotency_key=key,
        )
        if not ok:
            email_notify.release_email_send(db, EmailSentLog, idempotency_key=key)
            return False
        product_analytics.capture(
            'summary_email_sent',
            user.id,
            {'job_id': job_id, 'task_id': task_copy.id},
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception('notify_summary_email failed for job %s user %s',
                         job_id, getattr(user, 'id', '?'))
        _report_once('notify', None, message='notify_summary_email failed')
        return False


def eligible_subscribers(db, User, SavedFeed, *, rss_url: str, now=None):
    """Feeds opted into summary email for this RSS URL that still accept mail."""
    now = now or datetime.now(timezone.utc)
    feeds = (
        SavedFeed.query
        .filter_by(rss_url=rss_url, email_summaries=True)
        .all()
    )
    out = []
    for feed in feeds:
        user = db.session.get(User, feed.user_id)
        if user is None or not user_wants_summary_email(user, feed):
            continue
        out.append((user, feed, feed_summary_trial_expired(feed, now=now)))
    return out


def process_queued_jobs(
    *,
    app,
    db,
    User,
    SavedFeed,
    TranscriptionTask,
    EmailSentLog,
    SummaryEmailJob,
    SummaryEmailBudgetDay,
    build_openai_client,
    platform_api_key: str,
    start_shared_transcription,
    public_base_url: str,
    secret_key: str,
    max_jobs: int = 5,
) -> dict[str, int]:
    """Advance queued/in-flight summary-email jobs. Called from the poller.

    `start_shared_transcription(job) -> task_id | None` is provided by app.py so
    we reuse download/transcribe without importing the whole worker here.
    """
    stats = {
        'jobs_seen': 0,
        'jobs_started': 0,
        'jobs_done': 0,
        'emails_sent': 0,
        'skipped': 0,
        'failed': 0,
    }
    if not summary_email_enabled():
        return stats
    if not platform_api_key:
        logger.info('summary-email: no platform OpenAI key; skipping')
        return stats

    with app.app_context():
        jobs = (
            SummaryEmailJob.query
            .filter(SummaryEmailJob.status.in_([
                'queued', 'reserved', 'transcribing', 'summarizing', 'notifying',
            ]))
            .order_by(SummaryEmailJob.id.asc())
            .limit(max_jobs)
            .all()
        )
        stats['jobs_seen'] = len(jobs)
        for job in jobs:
            try:
                _process_one_job(
                    db=db,
                    job=job,
                    User=User,
                    SavedFeed=SavedFeed,
                    TranscriptionTask=TranscriptionTask,
                    EmailSentLog=EmailSentLog,
                    SummaryEmailBudgetDay=SummaryEmailBudgetDay,
                    build_openai_client=build_openai_client,
                    platform_api_key=platform_api_key,
                    start_shared_transcription=start_shared_transcription,
                    public_base_url=public_base_url,
                    secret_key=secret_key,
                    stats=stats,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception('summary-email job %s crashed', job.id)
                _report_once('process_job', exc)
                try:
                    _mark(db, job, 'failed', error_message=str(exc)[:500])
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                stats['failed'] += 1
    return stats


def _process_one_job(
    *,
    db,
    job,
    User,
    SavedFeed,
    TranscriptionTask,
    EmailSentLog,
    SummaryEmailBudgetDay,
    build_openai_client,
    platform_api_key,
    start_shared_transcription,
    public_base_url,
    secret_key,
    stats,
) -> None:
    max_sec = max_episode_seconds()
    duration = float(job.duration_seconds or 0)

    # Skip oversized episodes early (v1).
    if duration and duration > max_sec:
        if job.budget_seconds:
            release_daily_budget(db, seconds=job.budget_seconds, day=job.budget_day)
        _mark(db, job, 'skipped', skip_reason='episode_too_long',
              budget_seconds=None)
        stats['skipped'] += 1
        return

    # No subscribers left → done without work.
    subs = eligible_subscribers(db, User, SavedFeed, rss_url=job.rss_url)
    active_subs = [(u, f, expired) for u, f, expired in subs if not expired]
    # Still email expired-trial users once with a "buy minutes" note for this
    # episode only if we already have a transcript; otherwise skip new work.
    if not active_subs and not any(expired for _, _, expired in subs):
        _mark(db, job, 'skipped', skip_reason='no_subscribers')
        stats['skipped'] += 1
        return

    # --- Ensure shared transcription exists ---
    task = None
    if job.task_id:
        task = db.session.get(TranscriptionTask, job.task_id)

    if task is None:
        cached = find_cached_completed_task(
            db, TranscriptionTask, job.audio_url)
        if cached is not None:
            job.task_id = cached.id
            _mark(db, job, 'summarizing' if cached.summary_status != 'ready'
                  else 'notifying')
            task = cached
        elif job.status in ('queued', 'reserved'):
            # Reserve budget before starting work.
            reserve_sec = int(duration) if duration else max_sec
            # Cap reservation at max episode length.
            reserve_sec = min(reserve_sec, max_sec) if reserve_sec else max_sec
            if not active_subs:
                # Only expired-trial subscribers: don't spend new budget.
                _mark(db, job, 'skipped', skip_reason='trial_expired_only')
                stats['skipped'] += 1
                return
            day = utc_today()
            if job.status == 'queued':
                if not reserve_daily_budget(
                    db, SummaryEmailBudgetDay, seconds=reserve_sec, day=day,
                ):
                    _mark(db, job, 'skipped', skip_reason='daily_budget')
                    stats['skipped'] += 1
                    return
                job.budget_seconds = reserve_sec
                job.budget_day = day
                _mark(db, job, 'reserved')
            # Start shared transcription (funded by platform key, no user meter).
            owner = active_subs[0][0]
            task_id = start_shared_transcription(job, owner_user_id=owner.id)
            if not task_id:
                release_daily_budget(
                    db, seconds=job.budget_seconds or 0, day=job.budget_day)
                _mark(db, job, 'failed', error_message='could not start transcription',
                      budget_seconds=None)
                stats['failed'] += 1
                return
            job.task_id = task_id
            _mark(db, job, 'transcribing')
            stats['jobs_started'] += 1
            return  # wait for next poller tick
        else:
            return

    if task is None:
        return

    # Wait while transcription runs.
    if task.status not in ('completed', 'error', 'cancelled'):
        if job.status != 'transcribing':
            _mark(db, job, 'transcribing')
        return

    if task.status != 'completed' or not (task.transcript_text or '').strip():
        release_daily_budget(
            db, seconds=job.budget_seconds or 0, day=job.budget_day)
        _mark(db, job, 'failed',
              error_message=(task.error_message or 'transcription failed')[:500],
              budget_seconds=None)
        stats['failed'] += 1
        return

    # Reconcile budget to measured duration once.
    if job.budget_seconds and task.audio_duration:
        measured = int(task.audio_duration)
        if measured > max_sec:
            release_daily_budget(
                db, seconds=job.budget_seconds, day=job.budget_day)
            _mark(db, job, 'skipped', skip_reason='episode_too_long_measured',
                  budget_seconds=None)
            stats['skipped'] += 1
            return
        if measured < job.budget_seconds:
            release_daily_budget(
                db, seconds=job.budget_seconds - measured, day=job.budget_day)
            job.budget_seconds = measured
            db.session.commit()

    # --- Summarize ---
    if getattr(task, 'summary_status', None) != 'ready':
        _mark(db, job, 'summarizing')
        client = build_openai_client(platform_api_key)
        ok = summary_mod.summarize_task(
            db=db, task=task, openai_client=client,
            user_id=task.user_id, retry=True, force=True,
        )
        db.session.refresh(task)
        if not ok or task.summary_status != 'ready':
            # Transcript exists; still mark failed for email path but keep transcript.
            _mark(db, job, 'failed', error_message='summary failed')
            stats['failed'] += 1
            return

    summary = summary_mod.parse_summary_json(task.summary_json) or {}
    _mark(db, job, 'notifying')

    # Refresh subscribers at notify time.
    subs = eligible_subscribers(db, User, SavedFeed, rss_url=job.rss_url)
    sent = 0
    for user, feed, expired in subs:
        # v1: expired trial → skip new episodes (offer purchase only if we
        # already produced this job before expiry — here we simply skip).
        if expired:
            continue
        copy = create_subscriber_copy(
            db, TranscriptionTask, source_task=task, user_id=user.id)
        if notify_summary_email(
            db=db,
            user=user,
            task_copy=copy,
            summary=summary,
            EmailSentLog=EmailSentLog,
            public_base_url=public_base_url,
            secret_key=secret_key,
            job_id=job.id,
            trial_expired=False,
        ):
            sent += 1
    stats['emails_sent'] += sent
    _mark(db, job, 'done')
    stats['jobs_done'] += 1


def enqueue_from_episodes(
    db,
    SummaryEmailJob,
    *,
    rss_url: str,
    episodes: list[dict],
) -> int:
    """Enqueue jobs for fresh episodes. Returns number newly inserted."""
    if not summary_email_enabled():
        return 0
    created = 0
    for ep in episodes:
        audio = ep.get('audio_url') or ''
        guid = ep.get('guid') or ''
        if not audio or not guid:
            continue
        duration = None
        if ep.get('duration_min') is not None:
            try:
                duration = float(ep['duration_min']) * 60.0
            except (TypeError, ValueError):
                duration = None
        elif ep.get('duration_seconds') is not None:
            try:
                duration = float(ep['duration_seconds'])
            except (TypeError, ValueError):
                duration = None
        key = job_idempotency_key(rss_url, guid)
        existed = SummaryEmailJob.query.filter_by(idempotency_key=key).first()
        if existed is not None:
            continue
        row = enqueue_job(
            db, SummaryEmailJob,
            rss_url=rss_url,
            audio_url=audio,
            episode_guid=guid,
            episode_title=ep.get('title') or 'Episode',
            podcast_name=ep.get('podcast_name') or '',
            duration_seconds=duration,
        )
        if row is not None:
            created += 1
    return created
