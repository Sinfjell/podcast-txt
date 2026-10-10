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

# Protocol versions we accept on initialize (newest first).
# Echo the client's version when supported; otherwise fall back to newest.
_SUPPORTED_PROTOCOL_VERSIONS = (
    '2025-11-25',
    '2025-06-18',
    '2025-03-26',
    '2024-11-05',
    '2024-10-07',
)
_DEFAULT_PROTOCOL_VERSION = _SUPPORTED_PROTOCOL_VERSIONS[0]

_MCP_DOC_BEGIN = '<!-- mcp-section -->'
_MCP_DOC_END = '<!-- /mcp-section -->'

# Per-key rate limit (all MCP JSON-RPC calls). Separate from AGENT_WRITE_*.
MCP_MAX_PER_WINDOW = int(os.getenv('MCP_MAX_PER_WINDOW', '60'))
MCP_WINDOW_SECONDS = int(os.getenv('MCP_WINDOW_SECONDS', '60'))
MCP_LIST_EPISODE_LIMIT = int(os.getenv('MCP_LIST_EPISODE_LIMIT', '25'))
# Bounded server-side wait for get_transcript / get_transcript_status when the
# job is still running. Keeps ChatGPT/Claude from giving up on the first poll
# without blocking a worker for minutes. Cap hard so a bad env cannot hang
# gunicorn threads (workers=2, threads=4).
_MCP_WAIT_SECONDS_DEFAULT = 45.0
_MCP_WAIT_SECONDS_MAX = 90.0
_MCP_WAIT_POLL_DEFAULT = 1.0
_MCP_LIST_DEFAULT_LIMIT = 20
_MCP_LIST_MAX_LIMIT = 100
_MCP_TEXT_DEFAULT_CHARS = 24000
_MCP_TEXT_MAX_CHARS = 100000


def _mcp_wait_seconds() -> float:
    raw = (os.getenv('MCP_WAIT_SECONDS') or str(int(_MCP_WAIT_SECONDS_DEFAULT))).strip()
    try:
        return max(0.0, min(float(raw), _MCP_WAIT_SECONDS_MAX))
    except ValueError:
        return _MCP_WAIT_SECONDS_DEFAULT


def _mcp_wait_poll_seconds() -> float:
    raw = (os.getenv('MCP_WAIT_POLL_SECONDS') or str(_MCP_WAIT_POLL_DEFAULT)).strip()
    try:
        return max(0.05, min(float(raw), 5.0))
    except ValueError:
        return _MCP_WAIT_POLL_DEFAULT

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


def _public_base() -> str:
    return (os.getenv('PUBLIC_BASE_URL') or 'https://podskrift.com').rstrip('/')


def _pricing_url() -> str:
    A = _app()
    try:
        return A.public_url('pricing')
    except Exception:  # noqa: BLE001 — outside request / no app context
        return f'{_public_base()}/pricing'


def _history_url() -> str:
    A = _app()
    try:
        return A.public_url('history')
    except Exception:  # noqa: BLE001
        return f'{_public_base()}/history'


def _transcript_page_url(task_id: str) -> str:
    A = _app()
    try:
        return A.public_url('transcription_page', task_id=task_id)
    except Exception:  # noqa: BLE001
        return f'{_public_base()}/transcription/{task_id}'


def _wait_while_pending(user_id, task_id: str, *,
                        wait_seconds: float | None = None,
                        sleep_fn=None,
                        monotonic_fn=None):
    """Poll the DB until the task leaves pending or ``wait_seconds`` elapses.

    Uses short sleeps (not a busy loop). Returns the latest task row (may still
    be pending). ``sleep_fn`` / ``monotonic_fn`` are for tests.
    """
    from models import db

    wait = _mcp_wait_seconds() if wait_seconds is None else max(0.0, float(wait_seconds))
    sleep_fn = sleep_fn or time.sleep
    monotonic_fn = monotonic_fn or time.monotonic
    poll = _mcp_wait_poll_seconds()
    deadline = monotonic_fn() + wait
    task = _user_task(user_id, task_id)
    if task is None or wait <= 0:
        return task
    A = _app()
    while A.agent_transcript_status(task) == 'pending':
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            break
        sleep_fn(min(poll, remaining))
        db.session.expire_all()
        task = _user_task(user_id, task_id)
        if task is None:
            break
    return task


def _eta_fields_for_task(task) -> dict:
    """Historical p25–p75 ETA window for an in-flight (or just-started) task."""
    A = _app()
    percent, live_eta = A.compute_live_progress(task)
    pct = int(percent) if percent is not None else 0
    ahead = A.count_transcriptions_ahead(task)
    est = A.estimate_transcription_eta(
        audio_duration_sec=task.audio_duration,
        progress_pct=pct,
        queue_ahead=ahead,
    )
    # Prefer the historical window; if live phase ETA is longer, stretch high.
    if live_eta is not None and live_eta > est['eta_seconds_high']:
        est['eta_seconds_high'] = int(round(live_eta))
        if est['eta_seconds'] > est['eta_seconds_high']:
            est['eta_seconds'] = est['eta_seconds_high']
        est['eta_text'] = A.format_eta_window_text(
            est['eta_seconds_low'], est['eta_seconds_high'])
    return {
        'progress_pct': pct,
        'eta_seconds': est['eta_seconds'],
        'eta_seconds_low': est['eta_seconds_low'],
        'eta_seconds_high': est['eta_seconds_high'],
        'eta_text': est['eta_text'],
        'eta_basis': est['eta_basis'],
        'eta_sample_count': est['sample_count'],
        'queue_ahead': est['queue_ahead'],
    }


def _next_step_instruction(task_id: str, eta: dict, *, still_prefix: str) -> str:
    """Explicit next-step copy: poll get_transcript_status after ~N seconds."""
    low = int(eta.get('eta_seconds_low') or 60)
    poll_after = max(30, min(low, 120))
    eta_text = eta.get('eta_text') or 'usually ready in a few minutes'
    return (
        f'{still_prefix} Rough ETA: {eta_text} '
        f'(not a promise). Call get_transcript_status with task_id '
        f'{task_id} after ~{poll_after} s; do not ask the user to remind '
        f'you. A “transcript ready” email will be sent when it finishes '
        f'(if enabled in Settings). Progress is also on History: '
        f'{_history_url()}'
    )


def _in_progress_fields(task) -> dict:
    """Shared fields for pending / in_progress MCP responses."""
    eta = _eta_fields_for_task(task)
    still = f'Still transcribing ({eta["eta_text"]}).'
    instruction = _next_step_instruction(task.id, eta, still_prefix=still)
    return {
        'status': 'in_progress',
        'transcript_status': 'pending',
        'task_status': task.status,
        'progress_pct': eta['progress_pct'],
        'eta_seconds': eta['eta_seconds'],
        'eta_seconds_low': eta['eta_seconds_low'],
        'eta_seconds_high': eta['eta_seconds_high'],
        'eta_text': eta['eta_text'],
        'eta_basis': eta['eta_basis'],
        'queue_ahead': eta['queue_ahead'],
        'message': instruction,
        'instruction': instruction,
        'next_step': (
            f'call get_transcript_status with task_id {task.id} '
            f'after ~{max(30, min(int(eta["eta_seconds_low"]), 120))} s'
        ),
        'history_url': _history_url(),
        'email_note': (
            'A “transcript ready” email will be sent when transcription '
            'finishes (if enabled in Settings).'
        ),
    }


def _attach_balance_fact(payload: dict, user) -> dict:
    """Add ``balance_fact`` for metered users (skip BYOK)."""
    A = _app()
    fact = A.balance_fact_for_user(user, pricing_url=_pricing_url())
    if fact:
        payload['balance_fact'] = fact
    return payload


def _list_status_bucket(task) -> str | None:
    """Map a task to completed|in_progress|failed for list_my_transcripts."""
    A = _app()
    st = A.agent_transcript_status(task)
    if st == 'pending':
        return 'in_progress'
    if st == 'failed':
        return 'failed'
    if st == 'ready' or (task.status or '') == 'completed':
        return 'completed'
    return None


def _parse_mcp_date(value: str):
    """YYYY-MM-DD → date, or None."""
    from datetime import date
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


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


def _pricing_info_url(*, ref: str | None = None) -> str:
    """Information-page pricing link (no checkout deep link)."""
    base = _pricing_url()
    if not ref:
        return base
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}ref={ref}'


def _insufficient_balance_payload(user, *, cost_minutes: int,
                                  duration_min=None,
                                  message: str | None = None,
                                  reason: str = 'low_balance'):
    """Structured shortfall for MCP — facts + pricing info link, no upsell."""
    A = _app()
    bal = _balance_snapshot(user)
    minutes_left = int(bal.get('remaining_minutes') or 0)
    episode_minutes = int(cost_minutes)
    try:
        if duration_min is not None:
            episode_minutes = max(1, int(round(float(duration_min))))
    except (TypeError, ValueError):
        pass
    shortfall = max(0, episode_minutes - minutes_left)
    pricing = _pricing_info_url(ref='mcp_shortfall')

    # ETA if the user topped up enough to cover this episode (informational).
    eta = A.estimate_transcription_eta(
        audio_duration_sec=float(episode_minutes) * 60.0,
        progress_pct=0,
        queue_ahead=max(0, A.count_in_flight_transcriptions()),
    )

    trial_rem, paid_rem = A._platform_remaining_seconds(user.id)
    estimate = A.trial_estimate_seconds(duration_min if duration_min is not None
                                        else episode_minutes)
    partial_ok = (
        paid_rem <= 0
        and trial_rem >= A.TRIAL_PARTIAL_MIN_SECONDS
        and estimate > trial_rem
    )
    partial_note = None
    if partial_ok:
        partial_min = max(1, trial_rem // 60)
        partial_note = (
            f'On the website you can start a free partial preview of the first '
            f'{partial_min} minutes (MCP starts full episodes only).'
        )

    msg = message or (
        f'This episode is about {episode_minutes} minutes; you have '
        f'{minutes_left} minutes left (shortfall {shortfall}). '
        f'See {pricing} for how Podskrift minutes work.'
    )
    if partial_note and 'partial' not in msg.lower():
        msg = f'{msg} {partial_note}'

    product_analytics.capture(
        'mcp_insufficient_balance',
        getattr(user, 'id', None),
        {
            'episode_minutes': episode_minutes,
            'minutes_left': minutes_left,
            'shortfall_minutes': shortfall,
            'reason': reason,
            'partial_preview_available': bool(partial_ok),
        },
    )

    payload = {
        'error': 'insufficient_balance',
        'message': msg,
        'cost_minutes': cost_minutes,
        'episode_minutes': episode_minutes,
        'minutes_left': minutes_left,
        'shortfall_minutes': shortfall,
        'balance': bal,
        'pricing_url': pricing,
        'eta_if_topped_up': {
            'eta_seconds_low': eta['eta_seconds_low'],
            'eta_seconds_high': eta['eta_seconds_high'],
            'eta_text': eta['eta_text'],
        },
        'partial_preview_available': bool(partial_ok),
    }
    if partial_note:
        payload['partial_preview_note'] = partial_note
    return payload


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
                'No OpenAI API key available. Add your key in Settings, or see '
                f'{_pricing_url()} for how minutes work.'
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
            user, cost_minutes=cost_minutes, duration_min=duration_min,
            reason='episode_too_long' if over_free_cap else 'low_balance')
    # Shared daily budget can still refuse inside enqueue; surface a clearer
    # pre-check when today's free pool is empty and paid cannot cover.
    if (not over_free_cap and paid_rem < estimate
            and A.TRIAL_DAILY_SECONDS > 0
            and A.trial_daily_remaining_seconds() < estimate):
        return False, cost_minutes, _insufficient_balance_payload(
            user,
            cost_minutes=cost_minutes,
            duration_min=duration_min,
            reason='daily_cap',
            message=(
                f'This episode costs about {cost_minutes} minutes, but the shared '
                f'daily free budget is exhausted (or too low). '
                f'See {_pricing_info_url(ref="mcp_shortfall")} for how minutes work.'
            ),
        )
    return True, cost_minutes, None


def _tool_defs() -> list[dict]:
    # annotations: MCP tool hints for hosts (ChatGPT Pro filters to read/fetch).
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
            'annotations': {
                'readOnlyHint': True,
                'openWorldHint': True,
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
            'annotations': {
                'readOnlyHint': True,
                'openWorldHint': True,
            },
        },
        {
            'name': 'list_my_transcripts',
            'description': (
                'List the signed-in user’s existing Podskrift transcripts (History). '
                'Use this when the user asks about “my transcripts”, what they have '
                'already transcribed, or past episodes — do not invent a new job. '
                'Filter by query, podcast, dates, or status; newest first. Each item '
                'includes task_id for get_my_transcript.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': (
                            'Optional search in episode title, podcast name, or '
                            'transcript text.'
                        ),
                    },
                    'podcast': {
                        'type': 'string',
                        'description': 'Optional podcast / publisher name filter.',
                    },
                    'since': {
                        'type': 'string',
                        'description': 'Optional start date YYYY-MM-DD (inclusive).',
                    },
                    'until': {
                        'type': 'string',
                        'description': 'Optional end date YYYY-MM-DD (inclusive).',
                    },
                    'status': {
                        'type': 'string',
                        'description': (
                            'completed | in_progress | failed | all (default all).'
                        ),
                    },
                    'limit': {
                        'type': 'integer',
                        'description': 'Max items (default 20, max 100).',
                    },
                    'cursor': {
                        'type': 'string',
                        'description': (
                            'Opaque pagination cursor from a previous response’s '
                            'next_cursor.'
                        ),
                    },
                },
            },
            'annotations': {
                'readOnlyHint': True,
                'openWorldHint': False,
            },
        },
        {
            'name': 'get_my_transcript',
            'description': (
                'Fetch an existing transcript by task_id from list_my_transcripts. '
                'Never starts a new job and never charges minutes. For long text, '
                'page with offset / max_chars (follow next_offset). Optional format: '
                'txt (default), srt, or segments (timestamped).'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task_id': {
                        'type': 'string',
                        'description': 'task_id from list_my_transcripts.',
                    },
                    'format': {
                        'type': 'string',
                        'description': 'txt | srt | segments (default txt).',
                    },
                    'offset': {
                        'type': 'integer',
                        'description': 'Character offset into the text (default 0).',
                    },
                    'max_chars': {
                        'type': 'integer',
                        'description': (
                            'Max characters to return (default 24000, max 100000). '
                            'Use next_offset to continue.'
                        ),
                    },
                },
                'required': ['task_id'],
            },
            'annotations': {
                'readOnlyHint': True,
                'openWorldHint': False,
            },
        },
        {
            'name': 'get_transcript',
            'description': (
                'Get a transcript for an episode. If this account already has it, '
                'returns the text for free (cost_minutes 0) — also use '
                'list_my_transcripts / get_my_transcript for “my past transcripts”. '
                'Otherwise starts transcription (same trial / paid / BYOK rules as '
                'the website) and returns task_id / job_id plus a rough ETA window '
                '(eta_seconds_low/high + eta_text; not a promise) and next_step: '
                'call get_transcript_status with that task_id after ~N seconds. '
                'While in progress the server may wait briefly, then return '
                'in_progress with progress and the same ETA fields (do not ask the '
                'user to remind you; a transcript-ready email is sent when done). '
                'When balance is too low, returns a structured insufficient_balance '
                'payload with shortfall and a pricing information link.'
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
            'annotations': {
                'readOnlyHint': False,
                'destructiveHint': False,
                'idempotentHint': True,
            },
        },
        {
            'name': 'get_transcript_status',
            'description': (
                'Poll a transcription job by task_id / job_id from get_transcript. '
                'Waits briefly server-side if still running, then returns ready, '
                'failed, or in_progress. When ready, includes transcript text by '
                'default (same offset / max_chars / next_offset paging as '
                'get_my_transcript; set include_text false for status only). '
                'In progress includes eta_seconds_low/high + eta_text and next_step '
                'to call this tool again after ~N seconds — do not ask the user to '
                'remind you. A transcript-ready email is sent when done.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'job_id': {
                        'type': 'string',
                        'description': (
                            'Job id / task_id returned by get_transcript '
                            '(alias: task_id).'
                        ),
                    },
                    'task_id': {
                        'type': 'string',
                        'description': 'Alias for job_id.',
                    },
                    'include_text': {
                        'type': 'boolean',
                        'description': (
                            'When ready, include transcript text (default true). '
                            'Set false for status-only.'
                        ),
                    },
                    'offset': {
                        'type': 'integer',
                        'description': (
                            'Character offset into the text when include_text is '
                            'true (default 0).'
                        ),
                    },
                    'max_chars': {
                        'type': 'integer',
                        'description': (
                            'Max characters when include_text is true '
                            '(default 24000, max 100000). Follow next_offset.'
                        ),
                    },
                },
                'required': ['job_id'],
            },
            'annotations': {
                'readOnlyHint': True,
                'openWorldHint': True,
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

    # Show name via iTunes: prefer exact/substring matches, then the same
    # directory ranking search_podcasts uses. Apostrophe/hyphen differences
    # (e.g. "Merriam-Webster Word of the Day" vs "Merriam-Webster's …") often
    # miss the strict filter but rank first in Apple's search.
    try:
        shows = A._itunes_shows_matching(podcast)
    except RuntimeError as e:
        return None, None, str(e)
    if not shows:
        try:
            raw = A._itunes_search(podcast, 'podcast')
        except Exception as e:  # noqa: BLE001 — surface directory errors cleanly
            return None, None, f'Podcast directory unreachable: {e}'
        shows = [item for item in raw if item.get('feedUrl')]
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


def _page_transcript_text(task, *, offset=None, max_chars=None) -> dict:
    """Character window over transcript_text (same rules as get_my_transcript)."""
    try:
        off = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        off = 0
    off = max(0, off)
    try:
        cap = int(max_chars) if max_chars is not None else _MCP_TEXT_DEFAULT_CHARS
    except (TypeError, ValueError):
        cap = _MCP_TEXT_DEFAULT_CHARS
    cap = max(1, min(cap, _MCP_TEXT_MAX_CHARS))
    full = task.transcript_text or ''
    chunk = full[off: off + cap]
    next_off = off + len(chunk) if off + len(chunk) < len(full) else None
    return {
        'format': 'txt',
        'text': chunk,
        'offset': off,
        'max_chars': cap,
        'next_offset': next_off,
        'total_chars': len(full),
    }


def _task_ready_payload(task, user, *, reused: bool, cost_minutes: int = 0,
                        extra: dict | None = None,
                        include_text: bool = True,
                        offset=None, max_chars=None) -> dict:
    payload = {
        'job_id': task.id,
        'task_id': task.id,
        'transcript_status': 'ready',
        'status': 'ready',
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'cost_minutes': cost_minutes,
        'balance': _balance_snapshot(user),
        'reused': reused,
        'url': _transcript_page_url(task.id),
    }
    if include_text:
        payload.update(_page_transcript_text(
            task, offset=offset, max_chars=max_chars))
    if extra:
        payload.update(extra)
    return _attach_balance_fact(payload, user)


def _task_failed_payload(task, user, *, reused: bool, cost_minutes: int = 0,
                         extra: dict | None = None) -> dict:
    payload = {
        'job_id': task.id,
        'task_id': task.id,
        'transcript_status': 'failed',
        'status': 'failed',
        'error_message': task.error_message,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'cost_minutes': cost_minutes,
        'balance': _balance_snapshot(user),
        'reused': reused,
        'history_url': _history_url(),
    }
    if extra:
        payload.update(extra)
    return _attach_balance_fact(payload, user)


def _task_pending_payload(task, user, *, reused: bool, cost_minutes: int = 0,
                          extra: dict | None = None) -> dict:
    payload = {
        'job_id': task.id,
        'task_id': task.id,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'cost_minutes': cost_minutes,
        'balance': _balance_snapshot(user),
        'reused': reused,
    }
    payload.update(_in_progress_fields(task))
    if extra:
        payload.update(extra)
    return _attach_balance_fact(payload, user)


def _resolve_task_status_payload(task, user, *, reused: bool,
                                 cost_minutes: int = 0,
                                 extra: dict | None = None,
                                 include_text: bool = True,
                                 offset=None, max_chars=None) -> dict:
    """Wait briefly if pending, then return ready / failed / in_progress."""
    A = _app()
    if A.agent_transcript_status(task) == 'pending':
        task = _wait_while_pending(user.id, task.id) or task
    status = A.agent_transcript_status(task)
    if status == 'ready':
        return _task_ready_payload(
            task, user, reused=reused, cost_minutes=cost_minutes, extra=extra,
            include_text=include_text, offset=offset, max_chars=max_chars)
    if status == 'failed':
        return _task_failed_payload(
            task, user, reused=reused, cost_minutes=cost_minutes, extra=extra)
    if status == 'pending':
        return _task_pending_payload(
            task, user, reused=reused, cost_minutes=cost_minutes, extra=extra)
    payload = {
        'job_id': task.id,
        'task_id': task.id,
        'transcript_status': status,
        'status': status,
        'title': task.episode_title,
        'cost_minutes': cost_minutes,
        'balance': _balance_snapshot(user),
        'reused': reused,
    }
    return _attach_balance_fact(payload, user)


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
            if status in ('ready', 'pending', 'failed'):
                return _resolve_task_status_payload(task, user, reused=True)
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
        return _resolve_task_status_payload(existing, user, reused=True)

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
    source_client = getattr(g, 'mcp_source_client', None)
    result, status = A.enqueue_transcription(
        user, meta, rss_url=catalog.get('rss_url') or None, language=language or '',
        source='mcp', source_client=source_client)
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

    task = _user_task(user.id, result['task_id'])
    if task is None:
        # Task row not readable yet — still return ID + fallback ETA + next step.
        A = _app()
        dur = catalog.get('duration_min')
        try:
            audio_sec = float(dur) * 60.0 if dur is not None else None
        except (TypeError, ValueError):
            audio_sec = None
        eta = A.estimate_transcription_eta(
            audio_duration_sec=audio_sec,
            progress_pct=0,
            queue_ahead=max(0, A.count_in_flight_transcriptions() - 1),
        )
        poll_after = max(30, min(int(eta['eta_seconds_low']), 120))
        tid = result['task_id']
        payload = {
            'job_id': tid,
            'task_id': tid,
            'transcript_status': 'pending',
            'status': 'in_progress',
            'title': catalog.get('title'),
            'publisher': catalog.get('publisher'),
            'cost_minutes': cost_minutes,
            'balance_before': balance_before,
            'balance_after': balance_after,
            'reused': False,
            'pricing_url': _pricing_url(),
            'history_url': _history_url(),
            'eta_seconds': eta['eta_seconds'],
            'eta_seconds_low': eta['eta_seconds_low'],
            'eta_seconds_high': eta['eta_seconds_high'],
            'eta_text': eta['eta_text'],
            'eta_basis': eta['eta_basis'],
            'next_step': (
                f'call get_transcript_status with task_id {tid} '
                f'after ~{poll_after} s'
            ),
            'message': (
                f'Transcription started ({eta["eta_text"]}). '
                f'Call get_transcript_status with task_id {tid} '
                f'after ~{poll_after} s.'
            ),
            'instruction': (
                f'Transcription started ({eta["eta_text"]}). '
                f'Call get_transcript_status with task_id {tid} '
                f'after ~{poll_after} s; do not ask the user to remind you.'
            ),
        }
        return _attach_balance_fact(payload, user)
    extra = {
        'balance_before': balance_before,
        'balance_after': balance_after,
        'pricing_url': _pricing_url(),
    }
    # Drop the post-wait balance snapshot key clash: pending payload uses balance.
    return _resolve_task_status_payload(
        task, user, reused=False, cost_minutes=cost_minutes, extra=extra)


def tool_get_transcript_status(user, job_id: str = '', *,
                               task_id: str = '',
                               include_text=True,
                               offset=None, max_chars=None) -> dict:
    job_id = (job_id or task_id or '').strip()
    if not job_id:
        return {'error': 'job_id is required'}
    task = _user_task(user.id, job_id)
    if task is None:
        return {'error': 'Job not found', 'transcript_status': 'none'}
    if isinstance(include_text, str):
        include_text = include_text.strip().lower() not in (
            '0', 'false', 'no', 'off')
    elif include_text is None:
        include_text = True
    else:
        include_text = bool(include_text)
    return _resolve_task_status_payload(
        task, user, reused=True, include_text=include_text,
        offset=offset, max_chars=max_chars)


def _list_item_from_task(task) -> dict | None:
    A = _app()
    bucket = _list_status_bucket(task)
    if bucket is None:
        return None
    published = None
    if task.episode_published:
        pub = str(task.episode_published).strip()
        if pub and not pub.lower().startswith('unknown'):
            # Prefer ISO date when parseable; otherwise pass through short forms.
            d = _parse_mcp_date(pub)
            published = d.isoformat() if d else pub[:32]
    when = task.completed_at or task.started_at
    duration_min = None
    if task.audio_duration:
        duration_min = round(float(task.audio_duration) / 60.0, 1)
    return {
        'task_id': task.id,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'published': published,
        'duration_min': duration_min,
        'language': task.language,
        'status': bucket,
        'source': A.task_source_label(task.source, task.source_client),
        'created_at': when.isoformat() if when else None,
        'url': _transcript_page_url(task.id),
    }


def tool_list_my_transcripts(user, *, query: str = '', podcast: str = '',
                             since: str = '', until: str = '',
                             status: str = 'all', limit=None,
                             cursor: str = '') -> dict:
    """List this user's tasks for MCP — never other accounts."""
    from datetime import timezone
    from models import TranscriptionTask

    A = _app()
    status_key = (status or 'all').strip().lower() or 'all'
    if status_key not in ('completed', 'in_progress', 'failed', 'all'):
        return {
            'error': 'invalid_status',
            'message': 'status must be completed, in_progress, failed, or all.',
        }
    try:
        lim = int(limit) if limit is not None else _MCP_LIST_DEFAULT_LIMIT
    except (TypeError, ValueError):
        lim = _MCP_LIST_DEFAULT_LIMIT
    lim = max(1, min(lim, _MCP_LIST_MAX_LIMIT))

    offset = 0
    if cursor not in (None, ''):
        try:
            offset = max(0, int(str(cursor).strip()))
        except ValueError:
            return {
                'error': 'invalid_cursor',
                'message': 'cursor must be an integer offset from next_cursor.',
            }

    since_d = _parse_mcp_date(since) if since else None
    until_d = _parse_mcp_date(until) if until else None
    if since and since_d is None:
        return {'error': 'invalid_since', 'message': 'since must be YYYY-MM-DD.'}
    if until and until_d is None:
        return {'error': 'invalid_until', 'message': 'until must be YYYY-MM-DD.'}

    q = (TranscriptionTask.query
         .filter(TranscriptionTask.user_id == user.id)
         .order_by(TranscriptionTask.started_at.desc()))

    podcast_f = (podcast or '').strip()
    if podcast_f:
        needle = f'%{A._like_contains(podcast_f.lower())}%'
        from sqlalchemy import func as sa_func
        q = q.filter(
            sa_func.lower(TranscriptionTask.podcast_name).like(needle, escape='\\')
        )

    # Pull a bounded window, then apply status/date/query in Python so
    # in_progress matches agent_transcript_status (incl. "transcribing 2/5").
    scan_cap = min(500, max(100, offset + lim * 5))
    candidates = q.limit(scan_cap).all()

    query_words = [w for w in re.split(r'\W+', (query or '').strip()) if w]
    query_re = (
        re.compile(r'\W+'.join(map(re.escape, query_words)), re.IGNORECASE)
        if query_words else None
    )

    matched = []
    for task in candidates:
        bucket = _list_status_bucket(task)
        if bucket is None:
            continue
        if status_key != 'all' and bucket != status_key:
            continue
        when = task.completed_at or task.started_at
        if when is not None:
            # Normalize to date in UTC for since/until.
            if when.tzinfo is None:
                when_utc = when.replace(tzinfo=timezone.utc)
            else:
                when_utc = when.astimezone(timezone.utc)
            day = when_utc.date()
            if since_d and day < since_d:
                continue
            if until_d and day > until_d:
                continue
        if query_re is not None:
            hay = ' '.join(filter(None, [
                task.episode_title or '',
                task.podcast_name or '',
                task.transcript_text or '',
            ]))
            if not query_re.search(hay):
                continue
        item = _list_item_from_task(task)
        if item:
            matched.append(item)

    page = matched[offset: offset + lim]
    next_cursor = None
    if offset + lim < len(matched):
        next_cursor = str(offset + lim)
    elif len(candidates) >= scan_cap and len(matched) >= offset + lim:
        # More may exist beyond the scan window — offer the next offset anyway.
        next_cursor = str(offset + lim)

    return {
        'transcripts': page,
        'count': len(page),
        'next_cursor': next_cursor,
        'history_url': _history_url(),
    }


def tool_get_my_transcript(user, task_id: str, *, format: str = 'txt',
                           offset=None, max_chars=None) -> dict:
    """Return an existing transcript; never enqueue / never charge."""
    A = _app()
    task_id = (task_id or '').strip()
    if not task_id:
        return {'error': 'task_id is required'}
    task = _user_task(user.id, task_id)
    if task is None:
        return {'error': 'Transcript not found', 'transcript_status': 'none'}

    bucket = _list_status_bucket(task)
    meta = {
        'task_id': task.id,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'language': task.language,
        'status': bucket or A.agent_transcript_status(task),
        'source': A.task_source_label(task.source, task.source_client),
        'url': _transcript_page_url(task.id),
        'cost_minutes': 0,
        'charged': False,
    }
    if task.audio_duration:
        meta['duration_min'] = round(float(task.audio_duration) / 60.0, 1)
    if task.episode_published:
        pub = str(task.episode_published).strip()
        if pub and not pub.lower().startswith('unknown'):
            d = _parse_mcp_date(pub)
            meta['published'] = d.isoformat() if d else pub[:32]

    agent_st = A.agent_transcript_status(task)
    meta['transcript_status'] = agent_st
    if agent_st == 'pending':
        meta.update(_in_progress_fields(task))
        return _attach_balance_fact(meta, user)
    if agent_st != 'ready':
        if agent_st == 'failed':
            meta['error_message'] = task.error_message
        return _attach_balance_fact(meta, user)

    fmt = (format or 'txt').strip().lower()
    if fmt not in ('txt', 'text', 'plain', 'srt', 'segments'):
        return {
            'error': 'invalid_format',
            'message': 'format must be txt, srt, or segments.',
            **meta,
        }

    try:
        off = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        off = 0
    off = max(0, off)
    try:
        cap = int(max_chars) if max_chars is not None else _MCP_TEXT_DEFAULT_CHARS
    except (TypeError, ValueError):
        cap = _MCP_TEXT_DEFAULT_CHARS
    cap = max(1, min(cap, _MCP_TEXT_MAX_CHARS))

    if fmt == 'segments':
        segs = []
        if task.segments_json:
            try:
                raw = json.loads(task.segments_json)
                if isinstance(raw, list):
                    segs = raw
            except (TypeError, ValueError, json.JSONDecodeError):
                segs = []
        # Page by segment index when offset/max_chars used as segment window.
        window = segs[off: off + cap]
        next_off = off + len(window) if off + len(window) < len(segs) else None
        return _attach_balance_fact({
            **meta,
            'format': 'segments',
            'segments': window,
            'offset': off,
            'next_offset': next_off,
            'total_segments': len(segs),
        }, user)

    if fmt == 'srt':
        full = A._segments_to_srt(task.segments_json, task.transcript_text or '')
        if full is None:
            return {
                **meta,
                'error': 'SRT unavailable (no segment timestamps stored)',
                'format': 'srt',
                'text': None,
            }
    else:
        full = task.transcript_text or ''
        fmt = 'txt'

    chunk = full[off: off + cap]
    next_off = off + len(chunk) if off + len(chunk) < len(full) else None
    return _attach_balance_fact({
        **meta,
        'format': fmt,
        'text': chunk,
        'offset': off,
        'max_chars': cap,
        'next_offset': next_off,
        'total_chars': len(full),
    }, user)


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
        if name == 'list_my_transcripts':
            out = tool_list_my_transcripts(
                user,
                query=args.get('query') or '',
                podcast=args.get('podcast') or '',
                since=args.get('since') or '',
                until=args.get('until') or '',
                status=args.get('status') or 'all',
                limit=args.get('limit'),
                cursor=args.get('cursor') or '',
            )
            ok = 'error' not in out
            _capture_tool(name, user.id, ok)
            return _tool_text(out, is_error=not ok)
        if name == 'get_my_transcript':
            out = tool_get_my_transcript(
                user,
                args.get('task_id') or '',
                format=args.get('format') or 'txt',
                offset=args.get('offset'),
                max_chars=args.get('max_chars'),
            )
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
            out = tool_get_transcript_status(
                user,
                args.get('job_id') or '',
                task_id=args.get('task_id') or '',
                include_text=args.get('include_text', True),
                offset=args.get('offset'),
                max_chars=args.get('max_chars'),
            )
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
    requested = (params or {}).get('protocolVersion')
    if requested in _SUPPORTED_PROTOCOL_VERSIONS:
        version = requested
    else:
        # Unknown / missing → newest we speak (not a stale pinned default).
        version = _SUPPORTED_PROTOCOL_VERSIONS[0]
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
            'Podskrift MCP: search podcasts, list episodes, list the user’s past '
            'transcripts (list_my_transcripts / get_my_transcript), and fetch or '
            'start transcripts (get_transcript). For “my transcripts / what have I '
            'transcribed”, call list_my_transcripts — do not start a new job. '
            'Re-fetching an already-transcribed episode is free. While a new job '
            'runs, call get_transcript_status yourself every 30–60s; do not ask the '
            'user to remind you — a transcript-ready email is sent when done. '
            'Authenticate with Authorization: Bearer psk_… (API key from Settings) '
            'or an OAuth access token from the Podskrift authorization server. '
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
    """Set g.api_* via the same rules as /api/v1, return (user, error_response).

    Accepts the CoS agent key, a customer ``psk_…`` API key, or (when
    ``MCP_OAUTH_ENABLED``) a short-lived OAuth access token mapped to the user.
    Billing / trial metering is unchanged — tools still run as that user.
    """
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

    # OAuth access token (poa_…) — only when the OAuth flag is on.
    import oauth_server as oauth_mod
    if oauth_mod.mcp_oauth_enabled():
        oauth_user = oauth_mod.lookup_access_token_user(provided)
        if oauth_user is not None:
            g.api_auth_kind = 'oauth'
            g.api_user_id = oauth_user.id
            g.mcp_source_client = oauth_mod.lookup_access_token_client_name(provided)
            return oauth_user, None

    return None, (jsonify({'error': 'Unauthorized'}), 401)


def _mcp_www_authenticate() -> str:
    """401 challenge; includes resource_metadata when OAuth is enabled."""
    import oauth_server as oauth_mod
    if oauth_mod.mcp_oauth_enabled():
        return oauth_mod._www_authenticate_header()
    return 'Bearer realm="podskrift"'


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
                resp.headers['WWW-Authenticate'] = _mcp_www_authenticate()
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
