"""New-episode email digest poller.

Dedupes RSS fetches per feed URL, SSRF-checks and caps body size, baselines
first sight of a follow (no email flood), then digests new episodes per user.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

import feedparser

import email_notify
import mail as mailer

logger = logging.getLogger(__name__)

#: Poller cap — large catalogues (e.g. The Daily ~20 MB) must still alert.
#: Spotify resolve keeps its own lower public-route cap in app.py.
FEED_MAX_BYTES = 40 * 1024 * 1024
#: Stop reading once this many item/entry closes are seen (newest-first feeds).
FEED_EARLY_STOP_ITEMS = 40
#: Soft cap on episodes listed in one digest email.
MAX_EPISODES_PER_DIGEST = 20


def _entry_published_ts(entry) -> Optional[float]:
    for key in ('published_parsed', 'updated_parsed'):
        parsed = entry.get(key) if hasattr(entry, 'get') else getattr(entry, key, None)
        if parsed:
            try:
                return time.mktime(parsed)
            except (OverflowError, TypeError, ValueError):
                continue
    return None


def _entry_guid(entry) -> str:
    for key in ('id', 'guid', 'link'):
        val = entry.get(key) if hasattr(entry, 'get') else getattr(entry, key, None)
        if isinstance(val, str) and val.strip():
            return val.strip()[:1024]
        if val is not None and not isinstance(val, str):
            getter = val.get if isinstance(val, dict) else getattr(val, 'get', None)
            if callable(getter):
                for sub in ('href', 'value'):
                    candidate = getter(sub)
                    if candidate and str(candidate).strip():
                        return str(candidate).strip()[:1024]
            text = str(val).strip()
            if text:
                return text[:1024]
    title = entry.get('title') if hasattr(entry, 'get') else getattr(entry, 'title', '')
    return (title or '').strip()[:1024]


def _entry_audio_url(entry) -> Optional[str]:
    enclosures = (
        entry.get('enclosures') if hasattr(entry, 'get')
        else getattr(entry, 'enclosures', None)
    )
    if not enclosures:
        return None
    for enc in enclosures:
        typ = enc.get('type', '') if isinstance(enc, dict) else getattr(enc, 'type', '')
        href = enc.get('href') if isinstance(enc, dict) else getattr(enc, 'href', None)
        if href and (not typ or str(typ).startswith('audio/')):
            return href
    return None


def parse_feed_episodes(body: bytes) -> list[dict[str, Any]]:
    """Parse feed bytes into episode dicts with guid + published_ts."""
    feed = feedparser.parse(body)
    feed_title = getattr(feed.feed, 'title', '') or ''
    out = []
    for entry in feed.entries or []:
        audio = _entry_audio_url(entry)
        if not audio:
            continue
        guid = _entry_guid(entry)
        if not guid:
            continue
        out.append({
            'guid': guid,
            'title': (
                (entry.get('title') if hasattr(entry, 'get')
                 else getattr(entry, 'title', ''))
                or 'Episode'
            ),
            'audio_url': audio,
            'published_ts': _entry_published_ts(entry),
            'podcast_name': feed_title,
            'index': len(out),
        })
    return out


def newer_than_baseline(
    episodes: list[dict],
    *,
    guid: Optional[str],
    published_ts: Optional[float],
) -> list[dict]:
    """Episodes strictly newer than the stored watermark."""
    if not episodes:
        return []
    if published_ts is not None:
        fresh = []
        for ep in episodes:
            ts = ep.get('published_ts')
            if ts is None:
                if guid and ep.get('guid') == guid:
                    break
                fresh.append(ep)
                continue
            if ts > published_ts:
                fresh.append(ep)
            elif guid and ep.get('guid') == guid:
                break
        return fresh
    if not guid:
        return []
    fresh = []
    for ep in episodes:
        if ep.get('guid') == guid:
            break
        fresh.append(ep)
    return fresh


def baseline_from_episodes(episodes: list[dict]) -> tuple[Optional[str], Optional[float]]:
    if not episodes:
        return None, None
    top = episodes[0]
    return top.get('guid'), top.get('published_ts')


def transcribe_link(public_base: str, *, rss_url: str, episode: dict) -> str:
    base = (public_base or '').rstrip('/')
    params = {
        'rss_url': rss_url,
        'episode_index': str(episode.get('index', 0)),
        'audio_url': episode.get('audio_url') or '',
        'episode_title': episode.get('title') or '',
        'podcast_name': episode.get('podcast_name') or '',
    }
    qs = urlencode({k: v for k, v in params.items() if v != ''}, quote_via=quote)
    path = f'/go/transcribe?{qs}'
    return f'{base}{path}' if base else path


def set_feed_watermark(feed, episodes: list[dict], *, initialized: bool = True) -> None:
    guid, ts = baseline_from_episodes(episodes)
    if guid:
        feed.last_seen_episode_guid = guid
    if ts is not None:
        feed.last_seen_published_ts = ts
    if initialized:
        feed.alerts_initialized = True


def reset_feed_alert_baseline(feed) -> None:
    """Clear watermark so the next poll re-baselines (no backlog flood)."""
    feed.alerts_initialized = False
    feed.last_seen_episode_guid = None
    feed.last_seen_published_ts = None


def run_new_episode_poll(
    *,
    app,
    db,
    User,
    SavedFeed,
    EmailSentLog,
    fetch_feed: Callable[[str], Optional[bytes]],
    public_base_url: str,
    secret_key: str,
    SummaryEmailJob=None,
    enqueue_summary_jobs: Optional[Callable] = None,
) -> dict[str, int]:
    """Scan followed feeds and send digests. Returns counters for the CLI log.

    Additive optional args: when SUMMARY_EMAIL_ENABLED, also consider feeds
    with email_summaries and enqueue one shared summary job per new episode.
    """
    stats = {
        'feeds_considered': 0,
        'feeds_fetched': 0,
        'baselines_set': 0,
        'emails_sent': 0,
        'episodes_announced': 0,
        'skipped_disabled': 0,
        'summary_jobs_enqueued': 0,
    }
    email_on = mailer.mail_ready()
    if not email_on:
        logger.info(
            'new-episode poll: email not ready; advancing baselines only'
        )

    with app.app_context():
        # Plain alerts OR summary-email opt-in (union). Watermarks are shared.
        from sqlalchemy import or_ as sa_or_
        feeds = (
            SavedFeed.query
            .filter(sa_or_(
                SavedFeed.email_new_episodes.is_(True),
                SavedFeed.email_summaries.is_(True),
            ))
            .order_by(SavedFeed.id.asc())
            .all()
        )
        stats['feeds_considered'] = len(feeds)
        by_url: dict[str, list] = {}
        for feed in feeds:
            by_url.setdefault(feed.rss_url, []).append(feed)

        parsed_by_url: dict[str, list[dict]] = {}
        for rss_url in by_url:
            try:
                body = fetch_feed(rss_url)
            except Exception:  # noqa: BLE001
                logger.exception('feed fetch failed for %s', rss_url[:120])
                body = None
            if body is None:
                continue
            stats['feeds_fetched'] += 1
            try:
                parsed_by_url[rss_url] = parse_feed_episodes(body)
            except Exception:  # noqa: BLE001
                logger.exception('feed parse failed for %s', rss_url[:120])

        # user_id -> digest rows awaiting send
        pending: dict[int, list[dict]] = {}
        feeds_by_id = {}
        episodes_for_feed: dict[int, list[dict]] = {}
        # Feeds that should advance watermark this run (baseline / skip / no-op).
        advance_now: set[int] = set()
        # Feeds that advance only after a successful digest that mentioned them.
        advance_after_send: set[int] = set()
        # rss_url -> fresh episodes for summary-email enqueue (once per URL).
        summary_fresh_by_url: dict[str, list[dict]] = {}

        for rss_url, group in by_url.items():
            episodes = parsed_by_url.get(rss_url)
            if episodes is None:
                continue
            for feed in group:
                feeds_by_id[feed.id] = feed
                episodes_for_feed[feed.id] = episodes

                if not feed.alerts_initialized:
                    advance_now.add(feed.id)
                    stats['baselines_set'] += 1
                    continue

                fresh = newer_than_baseline(
                    episodes,
                    guid=feed.last_seen_episode_guid,
                    published_ts=feed.last_seen_published_ts,
                )
                if not fresh:
                    advance_now.add(feed.id)
                    continue

                # Summary-email enqueue (additive): one job per episode URL.
                if (
                    getattr(feed, 'email_summaries', False)
                    and enqueue_summary_jobs is not None
                    and SummaryEmailJob is not None
                ):
                    summary_fresh_by_url.setdefault(rss_url, fresh)

                user = db.session.get(User, feed.user_id)
                wants_alert = (
                    bool(getattr(feed, 'email_new_episodes', False))
                    and email_on
                    and user is not None
                    and email_notify.user_wants_feed_alerts(user, feed)
                )
                if not wants_alert:
                    # Summary-only feeds still need watermarks advanced after
                    # enqueue; plain-alert skip path advances immediately.
                    if getattr(feed, 'email_summaries', False):
                        advance_now.add(feed.id)
                    else:
                        stats['skipped_disabled'] += 1
                        advance_now.add(feed.id)
                    continue

                claimed = 0
                for ep in fresh[:MAX_EPISODES_PER_DIGEST]:
                    key = email_notify.episode_idempotency_key(
                        user.id, feed.id, ep['guid'])
                    if not email_notify.claim_email_send(
                        db, EmailSentLog,
                        user_id=user.id,
                        kind=email_notify.NEW_EPISODES,
                        idempotency_key=key,
                    ):
                        continue
                    claimed += 1
                    pending.setdefault(user.id, []).append({
                        'podcast_name': ep.get('podcast_name') or feed.name,
                        'episode_title': ep.get('title') or 'Episode',
                        'transcribe_url': transcribe_link(
                            public_base_url, rss_url=rss_url, episode=ep),
                        'feed_id': feed.id,
                        '_key': key,
                    })
                # Already-sent duplicates: nothing new to email; advance.
                # New claims: hold watermark until the digest succeeds.
                if claimed:
                    advance_after_send.add(feed.id)
                else:
                    advance_now.add(feed.id)

        if enqueue_summary_jobs is not None and SummaryEmailJob is not None:
            for rss_url, fresh in summary_fresh_by_url.items():
                try:
                    n = enqueue_summary_jobs(
                        db, SummaryEmailJob,
                        rss_url=rss_url, episodes=fresh[:MAX_EPISODES_PER_DIGEST],
                    )
                    stats['summary_jobs_enqueued'] += int(n or 0)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        'summary-email enqueue failed for %s', rss_url[:120])

        for feed_id in advance_now:
            set_feed_watermark(feeds_by_id[feed_id], episodes_for_feed[feed_id])
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception('failed to persist feed alert watermarks')

        if not email_on:
            return stats

        for user_id, items in pending.items():
            user = db.session.get(User, user_id)
            if user is None:
                for item in items:
                    email_notify.release_email_send(
                        db, EmailSentLog, idempotency_key=item['_key'])
                continue
            capped = items[:MAX_EPISODES_PER_DIGEST]
            for item in items[len(capped):]:
                email_notify.release_email_send(
                    db, EmailSentLog, idempotency_key=item['_key'])
            use_keys = [item['_key'] for item in capped]
            public_items = [
                {
                    'podcast_name': item['podcast_name'],
                    'episode_title': item['episode_title'],
                    'transcribe_url': item['transcribe_url'],
                }
                for item in capped
            ]
            ok = email_notify.notify_new_episodes_digest(
                db=db,
                user=user,
                items=public_items,
                EmailSentLog=EmailSentLog,
                public_base_url=public_base_url,
                secret_key=secret_key,
                claim_keys=use_keys,
            )
            if not ok:
                continue
            stats['emails_sent'] += 1
            stats['episodes_announced'] += len(capped)
            for feed_id in {item['feed_id'] for item in capped}:
                if feed_id in advance_after_send and feed_id in feeds_by_id:
                    set_feed_watermark(
                        feeds_by_id[feed_id], episodes_for_feed[feed_id])
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                logger.exception(
                    'failed to advance watermarks after digest for user %s',
                    user_id,
                )
    return stats
