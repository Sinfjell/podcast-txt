"""Remote MCP server for Podskrift (Streamable HTTP at /mcp).

Off unless ``MCP_ENABLED`` is truthy (default off). Tools reuse the same
customer/agent helpers as ``/api/v1/*`` — search, catalog resolve, enqueue,
trial/paid metering, duplicate-guard — and never invent a parallel billing path.

Transport: minimal JSON-RPC 2.0 over Streamable HTTP (POST ``application/json``).
The official Python MCP SDK is ASGI/Starlette-oriented; this module stays in
Flask without duplicating Whisper or reservation logic.
"""

from __future__ import annotations

import collections
import json
import os
import re
import threading
import time
from functools import wraps
from typing import Any
from urllib.parse import urlparse

from flask import Response, g, jsonify, request

import analytics as product_analytics

# Protocol versions we accept on initialize (echo the client's when supported).
_SUPPORTED_PROTOCOL_VERSIONS = (
    '2025-06-18',
    '2025-03-26',
    '2024-11-05',
    '2024-10-07',
)
_DEFAULT_PROTOCOL_VERSION = '2025-03-26'

_MCP_DOC_BEGIN = '<!-- mcp-section -->'
_MCP_DOC_END = '<!-- /mcp-section -->'

# Per-key rate limit (all MCP JSON-RPC calls). Separate from AGENT_WRITE_*.
MCP_MAX_PER_WINDOW = int(os.getenv('MCP_MAX_PER_WINDOW', '60'))
MCP_WINDOW_SECONDS = int(os.getenv('MCP_WINDOW_SECONDS', '60'))
MCP_LIST_EPISODE_LIMIT = int(os.getenv('MCP_LIST_EPISODE_LIMIT', '25'))

_mcp_rate_attempts: dict[str, list[float]] = collections.defaultdict(list)
_mcp_rate_lock = threading.Lock()

# JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_AUTH_ERROR = -32001
_RATE_LIMITED = -32002


def mcp_enabled() -> bool:
    """True when the remote MCP endpoint should be served."""
    return (os.getenv('MCP_ENABLED', '0') or '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )


def filter_mcp_docs_section(markdown: str, *, enabled: bool | None = None) -> str:
    """Include or strip the gated MCP section from customer-api.md."""
    if enabled is None:
        enabled = mcp_enabled()
    begin = _MCP_DOC_BEGIN
    end = _MCP_DOC_END
    if begin not in markdown:
        return markdown
    if enabled:
        return markdown.replace(begin, '').replace(end, '')
    pattern = re.compile(
        re.escape(begin) + r'.*?' + re.escape(end),
        flags=re.DOTALL,
    )
    return pattern.sub('', markdown)


def _app():
    """Lazy import so mcp_server can load before app finishes defining helpers."""
    import app as app_mod
    return app_mod


def _pricing_url() -> str:
    A = _app()
    try:
        return A.public_url('pricing')
    except Exception:  # noqa: BLE001 — outside request / no app context
        base = (os.getenv('PUBLIC_BASE_URL') or 'https://podskrift.com').rstrip('/')
        return f'{base}/pricing'


def _jsonrpc_error(req_id, code: int, message: str, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': req_id, 'error': err}


def _jsonrpc_result(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def _tool_text(payload: Any, *, is_error: bool = False) -> dict:
    """MCP tools/call result: text content block (+ structured when useful)."""
    if isinstance(payload, str):
        text = payload
        structured = None
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        structured = payload
    result = {
        'content': [{'type': 'text', 'text': text}],
        'isError': bool(is_error),
    }
    if structured is not None:
        result['structuredContent'] = structured
    return result


def _rate_limit_ok(bucket: str) -> bool:
    """Reserve one MCP request for ``bucket``. False when the window is full."""
    now = time.time()
    with _mcp_rate_lock:
        seen = [t for t in _mcp_rate_attempts.get(bucket, ())
                if now - t < MCP_WINDOW_SECONDS]
        if len(seen) >= MCP_MAX_PER_WINDOW:
            _mcp_rate_attempts[bucket] = seen
            return False
        seen.append(now)
        _mcp_rate_attempts[bucket] = seen
        return True


def _balance_snapshot(user) -> dict:
    """Minutes remaining + key source for MCP tool payloads (no PII)."""
    A = _app()
    _, key_source = A.resolve_openai_key(user)
    if key_source == 'user':
        return {
            'key_source': 'byok',
            'metered': False,
            'remaining_free_minutes': None,
            'remaining_paid_minutes': None,
            'remaining_minutes': None,
            'note': 'Using your own OpenAI key — Podskrift does not meter minutes.',
        }
    trial_rem, paid_rem = A._platform_remaining_seconds(user.id)
    return {
        'key_source': key_source or 'none',
        'metered': True,
        'remaining_free_minutes': trial_rem // 60,
        'remaining_paid_minutes': paid_rem // 60,
        'remaining_minutes': (trial_rem + paid_rem) // 60,
    }


def _estimate_cost_minutes(duration_min) -> int:
    A = _app()
    return A.trial_estimate_seconds(duration_min) // 60


def _insufficient_balance_payload(user, *, cost_minutes: int, message: str | None = None):
    bal = _balance_snapshot(user)
    pricing = _pricing_url()
    msg = message or (
        f'This episode costs about {cost_minutes} minutes, but you have '
        f'{bal.get("remaining_minutes", 0)} minutes left. '
        f'Buy more minutes or add your own OpenAI key: {pricing}'
    )
    return {
        'error': 'insufficient_balance',
        'message': msg,
        'cost_minutes': cost_minutes,
        'balance': bal,
        'pricing_url': pricing,
    }


def _can_afford(user, duration_min) -> tuple[bool, int, dict | None]:
    """Whether enqueue would accept a full (non-partial) job for this estimate.

    Returns (ok, cost_minutes, refusal_payload_or_None). BYOK always ok.
    When balance is short we refuse up front with a pricing URL — MCP does not
    start a job the account cannot cover (partial web previews stay web-only).
    """
    A = _app()
    cost_minutes = _estimate_cost_minutes(duration_min)
    api_key, key_source = A.resolve_openai_key(user)
    if not api_key:
        return False, cost_minutes, {
            'error': 'no_openai_key',
            'message': (
                'No OpenAI API key available. Add your key in Settings, or buy '
                f'minutes: {_pricing_url()}'
            ),
            'pricing_url': _pricing_url(),
            'cost_minutes': cost_minutes,
            'balance': _balance_snapshot(user),
        }
    if key_source == 'user':
        return True, cost_minutes, None
    estimate = A.trial_estimate_seconds(duration_min)
    trial_rem, paid_rem = A._platform_remaining_seconds(user.id)
    over_free_cap = bool(
        A.TRIAL_MAX_EPISODE_SECONDS and estimate > A.TRIAL_MAX_EPISODE_SECONDS)
    if over_free_cap:
        can = paid_rem >= estimate
    else:
        can = (trial_rem + paid_rem) >= estimate
    if not can:
        return False, cost_minutes, _insufficient_balance_payload(
            user, cost_minutes=cost_minutes)
    # Shared daily budget can still refuse inside enqueue; surface a clearer
    # pre-check when today's free pool is empty and paid cannot cover.
    if (not over_free_cap and paid_rem < estimate
            and A.TRIAL_DAILY_SECONDS > 0
            and A.trial_daily_remaining_seconds() < estimate):
        return False, cost_minutes, _insufficient_balance_payload(
            user,
            cost_minutes=cost_minutes,
            message=(
                f'This episode costs about {cost_minutes} minutes, but the shared '
                f'daily free budget is exhausted (or too low). Buy minutes or add '
                f'your own OpenAI key: {_pricing_url()}'
            ),
        )
    return True, cost_minutes, None


def _tool_defs() -> list[dict]:
    return [
        {
            'name': 'search_podcasts',
            'description': (
                'Search for podcast shows by name (Apple Podcasts / iTunes directory). '
                'Returns show name, artist, feed URL, and Apple URL. Use the feed URL '
                'or show name with list_episodes next.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Show name or search terms (min 2 characters).',
                    },
                },
                'required': ['query'],
            },
        },
        {
            'name': 'list_episodes',
            'description': (
                'List recent episodes for a podcast. Pass a show name, RSS feed URL, '
                'or Apple/Spotify show link. Each episode includes id, title, date, '
                'publisher, duration_min and rss_url; pass id as get_transcript '
                'episode and the other fields alongside it.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'podcast': {
                        'type': 'string',
                        'description': 'Show name, feed URL, or Apple/Spotify show URL.',
                    },
                    'limit': {
                        'type': 'integer',
                        'description': 'Max episodes (default 25, max 50).',
                    },
                },
                'required': ['podcast'],
            },
        },
        {
            'name': 'get_transcript',
            'description': (
                'Get a transcript for an episode. If already transcribed for this '
                'account, returns the text. Otherwise starts transcription (same '
                'trial / paid / BYOK rules as the website), returning a job_id to '
                'poll with get_transcript_status. Includes cost_minutes and remaining '
                'balance. Refuses with pricing_url when the episode exceeds balance.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'episode': {
                        'type': 'string',
                        'description': (
                            'Episode id from list_episodes, an audio URL, a prior '
                            'job/task id, or "Show Name|YYYY-MM-DD".'
                        ),
                    },
                    'language': {
                        'type': 'string',
                        'description': 'Optional Whisper language code (e.g. no, en).',
                    },
                    'title': {
                        'type': 'string',
                        'description': 'Optional episode title from list_episodes.',
                    },
                    'publisher': {
                        'type': 'string',
                        'description': 'Optional show name (publisher) from list_episodes.',
                    },
                    'date': {
                        'type': 'string',
                        'description': 'Optional episode date YYYY-MM-DD from list_episodes.',
                    },
                    'duration_min': {
                        'type': 'number',
                        'description': 'Optional duration_min from list_episodes.',
                    },
                    'rss_url': {
                        'type': 'string',
                        'description': 'Optional rss_url from list_episodes.',
                    },
                },
                'required': ['episode'],
            },
        },
        {
            'name': 'get_transcript_status',
            'description': (
                'Poll a transcription job started by get_transcript. Returns status '
                '(pending|ready|failed) and the transcript text when ready.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'job_id': {
                        'type': 'string',
                        'description': 'Job id returned by get_transcript.',
                    },
                },
                'required': ['job_id'],
            },
        },
    ]


def tool_search_podcasts(query: str) -> dict:
    A = _app()
    query = (query or '').strip()
    if len(query) < 2:
        return {'results': [], 'error': 'query must be at least 2 characters'}
    try:
        raw = A._itunes_search(query, 'podcast')
    except Exception as e:  # noqa: BLE001 — surface directory errors cleanly
        return {'results': [], 'error': f'Podcast directory unreachable: {e}'}
    results = []
    for item in raw:
        if item.get('feedUrl'):
            show = A._itunes_show_result(item)
            results.append({
                'name': show.get('name'),
                'artist': show.get('artist'),
                'feed_url': show.get('feed_url'),
                'apple_url': show.get('apple_url'),
                'artwork': show.get('artwork'),
                'genre': show.get('genre'),
            })
    return {'results': results, 'count': len(results)}


def _resolve_podcast_feed(podcast: str) -> tuple[str | None, str | None, str | None]:
    """Return (feed_url, show_name, error)."""
    A = _app()
    podcast = (podcast or '').strip()
    if not podcast:
        return None, None, 'podcast is required'

    # Direct feed URL
    if podcast.startswith('http://') or podcast.startswith('https://'):
        host = (urlparse(podcast).hostname or '').lower()
        if host == 'apple.com' or host.endswith('.apple.com'):
            rss, err = A.convert_apple_podcasts_url_to_rss(podcast)
            if not rss:
                return None, None, err or 'Could not resolve Apple Podcasts URL.'
            return rss, None, None
        kind, _sid = A.parse_spotify_url(podcast)
        if kind == 'show':
            outcome = A.resolve_spotify_url(podcast)
            results = outcome.get('results') or []
            err = outcome.get('error')
            if err and not results:
                return None, None, err
            if not results:
                return None, None, 'Show not found for that Spotify link.'
            hit = results[0]
            feed = hit.get('feed_url')
            if not feed:
                return None, None, err or 'No public RSS feed for that show.'
            return feed, hit.get('artist') or hit.get('name'), None
        if kind == 'episode':
            return None, None, (
                'That Spotify link is an episode. Pass the show link/name to '
                'list_episodes, or pass the episode to get_transcript.'
            )
        # Treat as RSS
        if A._is_fetchable_url(podcast):
            return podcast, None, None
        return None, None, 'URL is not a supported feed, Apple, or Spotify show link.'

    # Show name via iTunes
    try:
        shows = A._itunes_shows_matching(podcast)
    except RuntimeError as e:
        return None, None, str(e)
    if not shows:
        return None, None, (
            f'No public podcast feed found for "{podcast}". '
            'Spotify-exclusive shows have no RSS Podskrift can fetch.'
        )
    show = shows[0]
    feed = show.get('feedUrl')
    if not feed or not A._is_fetchable_url(feed):
        return None, None, 'Matched show has no fetchable RSS feed.'
    return feed, show.get('collectionName'), None


def _episode_list_id(ep: dict, rss_url: str) -> str:
    """Stable catalog id agents can pass back to get_transcript."""
    pub = (ep.get('podcast_name') or '').strip()
    published = (ep.get('published') or '').strip()[:10]
    title = (ep.get('title') or '').strip()
    audio = (ep.get('audio_url') or '').strip()
    if audio:
        return audio
    return f'{pub}|{published}|{title}'


def tool_list_episodes(podcast: str, limit: int | None = None) -> dict:
    A = _app()
    feed_url, show_name, err = _resolve_podcast_feed(podcast)
    if err:
        return {'error': err, 'episodes': [], 'count': 0}
    try:
        lim = int(limit) if limit is not None else MCP_LIST_EPISODE_LIMIT
    except (TypeError, ValueError):
        lim = MCP_LIST_EPISODE_LIMIT
    lim = max(1, min(lim, 50))

    episodes, feed_err = A.get_episodes_from_rss(feed_url)
    if feed_err or not episodes:
        return {
            'error': feed_err or 'No episodes found in that feed.',
            'episodes': [],
            'count': 0,
            'feed_url': feed_url,
        }
    out = []
    for ep in episodes[:lim]:
        published = A._episode_published_date(ep.get('published'))
        publisher = ep.get('podcast_name') or show_name
        out.append({
            'id': _episode_list_id(ep, feed_url),
            'title': ep.get('title'),
            'date': published.isoformat() if published else (
                (ep.get('published') or '')[:10] or None
            ),
            'duration_min': ep.get('duration_min'),
            'publisher': publisher,
            'audio_url': ep.get('audio_url'),
            'rss_url': feed_url,
            'artwork_url': ep.get('artwork'),
        })
    return {
        'episodes': out,
        'count': len(out),
        'feed_url': feed_url,
        'podcast': show_name or podcast,
    }


def _parse_episode_arg(episode: str) -> dict:
    """Normalize the episode argument into catalog-ish fields or a task id."""
    A = _app()
    raw = (episode or '').strip()
    if not raw:
        return {'error': 'episode is required'}

    # JSON object from a careful client
    if raw.startswith('{'):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return {
                'task_id': data.get('job_id') or data.get('task_id') or data.get('id'),
                'audio_url': data.get('audio_url') or data.get('id'),
                'title': data.get('title'),
                'publisher': data.get('publisher') or data.get('podcast') or data.get('show'),
                'published_at': data.get('published_at') or data.get('date') or data.get('published'),
                'duration_min': data.get('duration_min'),
                'rss_url': data.get('rss_url'),
                'artwork_url': data.get('artwork_url') or data.get('artwork'),
            }

    # publisher|YYYY-MM-DD[|title]
    if '|' in raw and not raw.startswith('http'):
        parts = raw.split('|')
        if len(parts) >= 2 and A._parse_agent_date(parts[1]):
            return {
                'publisher': parts[0].strip(),
                'published_at': parts[1].strip()[:10],
                'title': parts[2].strip() if len(parts) > 2 else None,
            }

    # "Show Name on YYYY-MM-DD"
    on_match = re.match(r'^(.+?)\s+on\s+(\d{4}-\d{2}-\d{2})\s*$', raw, re.I)
    if on_match:
        return {
            'publisher': on_match.group(1).strip(),
            'published_at': on_match.group(2),
        }

    # Audio / page URL
    if raw.startswith('http://') or raw.startswith('https://'):
        path = urlparse(raw).path.lower()
        if any(path.endswith(ext) for ext in (
                '.mp3', '.m4a', '.wav', '.aac', '.ogg', '.mp4', '.mpeg')):
            return {'audio_url': raw}
        return {'url': raw}

    # Bare task / job id
    return {'task_id': raw}


def _user_task(user_id, task_id):
    A = _app()
    from models import TranscriptionTask
    if not task_id:
        return None
    return (TranscriptionTask.query
            .filter_by(id=task_id, user_id=user_id)
            .first())


def _catalog_from_parsed(parsed: dict) -> tuple[dict | None, str | None]:
    A = _app()
    if parsed.get('audio_url') and A._is_fetchable_url(parsed['audio_url']):
        if parsed.get('title') or parsed.get('publisher') or parsed.get('published_at'):
            return {
                'title': parsed.get('title') or 'Episode',
                'publisher': parsed.get('publisher'),
                'published_at': parsed.get('published_at'),
                'audio_url': parsed['audio_url'],
                'rss_url': parsed.get('rss_url') or '',
                'duration_min': A._positive_float_or_none(parsed.get('duration_min')),
                'artwork_url': parsed.get('artwork_url'),
                'transcript_status': 'none',
            }, None
        # audio URL alone — still usable
        return {
            'title': parsed.get('title') or 'Episode',
            'publisher': parsed.get('publisher'),
            'published_at': parsed.get('published_at'),
            'audio_url': parsed['audio_url'],
            'rss_url': parsed.get('rss_url') or '',
            'duration_min': A._positive_float_or_none(parsed.get('duration_min')),
            'artwork_url': parsed.get('artwork_url'),
            'transcript_status': 'none',
        }, None

    target_date = None
    if parsed.get('published_at'):
        target_date = A._parse_agent_date(parsed['published_at'])
        if target_date is None:
            return None, 'date must be ISO YYYY-MM-DD'

    return A.resolve_catalog_episode(
        publisher=parsed.get('publisher'),
        target_date=target_date,
        url=parsed.get('url'),
    )


_EPISODE_META_ARGS = (
    ('title', 'title'),
    ('publisher', 'publisher'),
    ('date', 'published_at'),
    ('duration_min', 'duration_min'),
    ('rss_url', 'rss_url'),
)


def tool_get_transcript(user, episode: str, language: str = '',
                        meta: dict | None = None) -> dict:
    A = _app()
    parsed = _parse_episode_arg(episode)
    if parsed.get('error'):
        return parsed
    # Optional list_episodes fields: keep title/show on the task and use the
    # feed duration for the up-front estimate (billing still uses measured audio).
    if parsed.get('audio_url') and meta:
        for arg, key in _EPISODE_META_ARGS:
            val = meta.get(arg)
            if val not in (None, '') and not parsed.get(key):
                parsed[key] = val

    # Existing job for this account
    task_id = parsed.get('task_id')
    if task_id and not (task_id.startswith('http://') or task_id.startswith('https://')):
        task = _user_task(user.id, task_id)
        if task is not None:
            status = A.agent_transcript_status(task)
            bal = _balance_snapshot(user)
            if status == 'ready':
                return {
                    'job_id': task.id,
                    'transcript_status': 'ready',
                    'title': task.episode_title,
                    'publisher': task.podcast_name,
                    'text': task.transcript_text or '',
                    'cost_minutes': None,
                    'balance': bal,
                    'reused': True,
                }
            if status == 'pending':
                return {
                    'job_id': task.id,
                    'transcript_status': 'pending',
                    'task_status': task.status,
                    'title': task.episode_title,
                    'publisher': task.podcast_name,
                    'message': 'Transcription in progress. Poll get_transcript_status.',
                    'balance': bal,
                    'reused': True,
                }
            if status == 'failed':
                return {
                    'job_id': task.id,
                    'transcript_status': 'failed',
                    'error_message': task.error_message,
                    'title': task.episode_title,
                    'balance': bal,
                    'reused': True,
                }
            # Fall through to resolve if id looked like a task but was not usable

    catalog, resolve_err = _catalog_from_parsed(parsed)
    if resolve_err or not catalog:
        return {'error': resolve_err or 'Episode not found'}

    # Same audio already queued, running or done for this account → reuse it.
    # list_episodes ids are audio URLs, which _find_existing_agent_task cannot
    # match (it keys on show+date / show+title), so check source_audio_url too.
    existing = (A._find_existing_web_task(user.id, catalog.get('audio_url'))
                or A._find_existing_agent_task(user.id, catalog))
    if existing is not None:
        status = A.agent_transcript_status(existing)
        bal = _balance_snapshot(user)
        payload = {
            'job_id': existing.id,
            'transcript_status': status,
            'title': existing.episode_title,
            'publisher': existing.podcast_name,
            'balance': bal,
            'cost_minutes': _estimate_cost_minutes(catalog.get('duration_min')),
            'reused': True,
        }
        if status == 'ready':
            payload['text'] = existing.transcript_text or ''
        elif status == 'pending':
            payload['task_status'] = existing.status
            payload['message'] = 'Transcription in progress. Poll get_transcript_status.'
        elif status == 'failed':
            payload['error_message'] = existing.error_message
        return payload

    ok, cost_minutes, refusal = _can_afford(user, catalog.get('duration_min'))
    balance_before = _balance_snapshot(user)
    if not ok:
        refusal = dict(refusal or {})
        refusal['balance_before'] = balance_before
        refusal['cost_minutes'] = cost_minutes
        return refusal

    if not A._api_write_rate_limit_ok():
        return {
            'error': 'rate_limited',
            'message': 'Too many transcription starts; try again shortly.',
        }
    if A._agent_in_flight_count(user.id) >= A.AGENT_MAX_IN_FLIGHT:
        return {
            'error': 'in_flight_limit',
            'message': (
                f'Already {A.AGENT_MAX_IN_FLIGHT} transcriptions in flight. '
                'Poll an existing job or wait for one to finish.'
            ),
        }

    meta = A._catalog_to_enqueue_meta(catalog)
    result, status = A.enqueue_transcription(
        user, meta, rss_url=catalog.get('rss_url') or None, language=language or '',
        source='mcp')
    balance_after = _balance_snapshot(user)
    if status != 200:
        payload = {
            'error': result.get('error') or 'Could not start transcription',
            'cost_minutes': cost_minutes,
            'balance_before': balance_before,
            'balance_after': balance_after,
            'pricing_url': _pricing_url(),
        }
        if status == 402:
            payload['error'] = 'insufficient_balance'
            payload['message'] = result.get('error')
        elif status == 400 and 'API key' in (result.get('error') or ''):
            payload['error'] = 'no_openai_key'
            payload['message'] = result.get('error')
        else:
            payload['message'] = result.get('error')
        return payload

    from models import TranscriptionTask
    task = A.db.session.get(TranscriptionTask, result['task_id'])
    return {
        'job_id': result['task_id'],
        'transcript_status': A.agent_transcript_status(task) if task else 'pending',
        'task_status': task.status if task else 'downloading',
        'title': catalog.get('title'),
        'publisher': catalog.get('publisher'),
        'cost_minutes': cost_minutes,
        'balance_before': balance_before,
        'balance_after': balance_after,
        'reused': False,
        'message': (
            'Transcription started. Poll get_transcript_status with this job_id '
            '(long episodes can take several minutes).'
        ),
        'pricing_url': _pricing_url(),
    }


def tool_get_transcript_status(user, job_id: str) -> dict:
    A = _app()
    job_id = (job_id or '').strip()
    if not job_id:
        return {'error': 'job_id is required'}
    task = _user_task(user.id, job_id)
    if task is None:
        return {'error': 'Job not found', 'transcript_status': 'none'}
    status = A.agent_transcript_status(task)
    payload = {
        'job_id': task.id,
        'transcript_status': status,
        'task_status': task.status,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'balance': _balance_snapshot(user),
    }
    if status == 'ready':
        payload['text'] = task.transcript_text or ''
    elif status == 'failed':
        payload['error_message'] = task.error_message
    elif status == 'pending':
        payload['message'] = 'Still working. Poll again shortly.'
    return payload


def _capture_tool(tool: str, user_id, success: bool):
    """PostHog mcp_tool_called — tool name + success only (no PII)."""
    product_analytics.capture(
        'mcp_tool_called',
        user_id,
        {'tool': tool, 'success': bool(success)},
    )


def _run_tool(name: str, arguments: dict, user) -> dict:
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == 'search_podcasts':
            out = tool_search_podcasts(args.get('query') or '')
            ok = 'error' not in out or bool(out.get('results'))
            _capture_tool(name, user.id, ok)
            return _tool_text(out, is_error=not ok and not out.get('results'))
        if name == 'list_episodes':
            out = tool_list_episodes(args.get('podcast') or '', args.get('limit'))
            ok = 'error' not in out
            _capture_tool(name, user.id, ok)
            return _tool_text(out, is_error=not ok)
        if name == 'get_transcript':
            out = tool_get_transcript(
                user, args.get('episode') or '', args.get('language') or '',
                meta=args)
            ok = out.get('error') is None and out.get('transcript_status') != 'failed'
            # insufficient_balance is a clean refusal, not a tool crash
            if out.get('error') == 'insufficient_balance':
                ok = False
            _capture_tool(name, user.id, ok and 'error' not in out)
            return _tool_text(out, is_error='error' in out)
        if name == 'get_transcript_status':
            out = tool_get_transcript_status(user, args.get('job_id') or '')
            ok = 'error' not in out
            _capture_tool(name, user.id, ok)
            return _tool_text(out, is_error=not ok)
        _capture_tool(name, getattr(user, 'id', 'mcp'), False)
        return _tool_text({'error': f'Unknown tool: {name}'}, is_error=True)
    except Exception:  # noqa: BLE001 — never 500 a tool call into the LLM
        _app().app.logger.exception('mcp tool %s failed', name)
        _capture_tool(name, getattr(user, 'id', 'mcp'), False)
        return _tool_text(
            {'error': 'internal_error', 'message': 'Tool failed; try again shortly.'},
            is_error=True,
        )


def _handle_initialize(params: dict) -> dict:
    requested = (params or {}).get('protocolVersion') or _DEFAULT_PROTOCOL_VERSION
    version = (
        requested if requested in _SUPPORTED_PROTOCOL_VERSIONS
        else _DEFAULT_PROTOCOL_VERSION
    )
    return {
        'protocolVersion': version,
        'capabilities': {
            'tools': {},
        },
        'serverInfo': {
            'name': 'podskrift',
            'version': '1.0.0',
        },
        'instructions': (
            'Podskrift MCP: search podcasts, list episodes, and fetch transcripts. '
            'Authenticate with Authorization: Bearer psk_… (API key from Settings). '
            'Transcription uses the same free trial / paid minutes / BYOK rules as '
            f'the website. Pricing: {_pricing_url()}'
        ),
    }


def _dispatch_rpc(message: dict, user) -> dict | None:
    """Handle one JSON-RPC message. None = notification (no response body part)."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _jsonrpc_error(None, _INVALID_REQUEST, 'Invalid Request')

    method = message.get('method')
    req_id = message.get('id', None)
    params = message.get('params')
    is_notification = 'id' not in message
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _jsonrpc_error(req_id, _INVALID_PARAMS, 'params must be an object')

    if not method or not isinstance(method, str):
        if is_notification:
            return None
        return _jsonrpc_error(req_id, _INVALID_REQUEST, 'Missing method')

    if method == 'notifications/initialized' or method.startswith('notifications/'):
        return None

    if method == 'initialize':
        return _jsonrpc_result(req_id, _handle_initialize(params))

    if method == 'ping':
        return _jsonrpc_result(req_id, {})

    if method == 'tools/list':
        return _jsonrpc_result(req_id, {'tools': _tool_defs()})

    if method == 'tools/call':
        name = params.get('name')
        name = name.strip() if isinstance(name, str) else ''
        if not name:
            return _jsonrpc_error(req_id, _INVALID_PARAMS, 'tools/call requires name')
        arguments = params.get('arguments') or {}
        result = _run_tool(name, arguments, user)
        return _jsonrpc_result(req_id, result)

    if is_notification:
        return None
    return _jsonrpc_error(req_id, _METHOD_NOT_FOUND, f'Method not found: {method}')


def _authenticate_mcp():
    """Set g.api_* via the same rules as /api/v1, return (user, error_response)."""
    A = _app()
    provided = A._extract_agent_api_key()
    if not provided:
        return None, (jsonify({'error': 'Unauthorized'}), 401)

    expected = A._agent_configured_key()
    agent_ok = (
        bool(expected)
        and len(provided) == len(expected)
        and __import__('hmac').compare_digest(provided, expected)
    )
    customer = None if agent_ok else A._lookup_user_by_api_key(provided)
    if agent_ok:
        g.api_auth_kind = 'agent'
        g.api_user_id = A._agent_scope_user_id()
        user, err_payload, err_status = A._api_write_user()
        if err_payload:
            # Agent key without AGENT_API_USER_ID cannot run write tools; still
            # allow initialize/list with a synthetic refusal on call.
            return None, (jsonify(err_payload), err_status)
        return user, None
    if customer is not None:
        g.api_auth_kind = 'customer'
        g.api_user_id = customer.id
        return customer, None
    return None, (jsonify({'error': 'Unauthorized'}), 401)


def _cors_headers(resp: Response) -> Response:
    origin = request.headers.get('Origin')
    if origin:
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Vary'] = 'Origin'
    else:
        resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = (
        'Authorization, Content-Type, Accept, Mcp-Session-Id, '
        'MCP-Protocol-Version, X-Api-Key, Mcp-Method, Mcp-Name'
    )
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Expose-Headers'] = 'Mcp-Session-Id'
    return resp


def create_mcp_view(app_flask):
    """Return the Flask view function for GET/POST/OPTIONS /mcp."""

    @wraps(create_mcp_view)
    def mcp_endpoint():
        if not mcp_enabled():
            return jsonify({'error': 'Not found'}), 404

        if request.method == 'OPTIONS':
            return _cors_headers(Response(status=204))

        if request.method == 'GET':
            # Tools-only server: no standalone SSE stream required.
            resp = jsonify({
                'error': 'Method Not Allowed',
                'message': (
                    'POST JSON-RPC to this endpoint (Streamable HTTP). '
                    'Authenticate with Authorization: Bearer psk_…'
                ),
            })
            resp.status_code = 405
            return _cors_headers(resp)

        user, auth_err = _authenticate_mcp()
        if auth_err is not None:
            resp, code = auth_err
            resp.status_code = code
            if code == 401:
                resp.headers['WWW-Authenticate'] = 'Bearer realm="podskrift"'
            return _cors_headers(resp)

        bucket = f'mcp:{getattr(g, "api_auth_kind", "?")}:{getattr(g, "api_user_id", "?")}'
        if not _rate_limit_ok(bucket):
            resp = jsonify({'error': 'Too many requests', 'retry_after_seconds': MCP_WINDOW_SECONDS})
            resp.status_code = 429
            return _cors_headers(resp)

        raw = request.get_data(cache=False, as_text=True) or ''
        if not raw.strip():
            resp = jsonify(_jsonrpc_error(None, _PARSE_ERROR, 'Parse error: empty body'))
            resp.status_code = 400
            return _cors_headers(resp)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            resp = jsonify(_jsonrpc_error(None, _PARSE_ERROR, 'Parse error'))
            resp.status_code = 400
            return _cors_headers(resp)

        if isinstance(payload, list):
            if not payload:
                resp = jsonify(_jsonrpc_error(None, _INVALID_REQUEST, 'Empty batch'))
                resp.status_code = 400
                return _cors_headers(resp)
            responses = []
            for i, item in enumerate(payload):
                # Each batch entry beyond the first costs a rate-limit slot, so
                # one POST cannot fan out unlimited tool calls.
                if i > 0 and not _rate_limit_ok(bucket):
                    item_id = item.get('id') if isinstance(item, dict) else None
                    if not (isinstance(item, dict) and 'id' not in item):
                        responses.append(_jsonrpc_error(
                            item_id, _RATE_LIMITED, 'Too many requests'))
                    continue
                out = _dispatch_rpc(item, user)
                if out is not None:
                    responses.append(out)
            if not responses:
                return _cors_headers(Response(status=202))
            body = responses
        else:
            body = _dispatch_rpc(payload, user)
            if body is None:
                # Notification acknowledged
                resp = Response(status=202)
                return _cors_headers(resp)

        resp = jsonify(body)
        resp.status_code = 200
        resp.headers['Content-Type'] = 'application/json'
        return _cors_headers(resp)

    return mcp_endpoint


def register_mcp(app_flask):
    """Attach the /mcp route. Always registered; returns 404 when flag is off."""
    view = create_mcp_view(app_flask)
    app_flask.add_url_rule(
        '/mcp',
        endpoint='mcp',
        view_func=view,
        methods=['GET', 'POST', 'OPTIONS'],
    )
