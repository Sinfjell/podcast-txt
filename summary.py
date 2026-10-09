"""Post-transcript AI summaries (feature-flagged, never blocks completion).

Uses the same OpenAI key that funded the transcription (BYOK → user key,
trial/paid → platform key). Never meters trial/paid minutes. Failures are
logged + Sentry-once and leave the transcript intact.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import analytics as product_analytics

logger = logging.getLogger(__name__)

# Cheapest suitable *mini* chat model on current OpenAI pricing (not nano —
# nano is too weak for TL;DR + verbatim quotes). Override with SUMMARY_MODEL.
DEFAULT_SUMMARY_MODEL = 'gpt-4o-mini'

# Published gpt-4o-mini rates ($ / 1M tokens). Used only for cost_usd_est logs.
# Unknown models fall back to these rates so estimates stay conservative-ish.
_MODEL_RATES_PER_M = {
    'gpt-4o-mini': (0.15, 0.60),
    'gpt-4.1-mini': (0.40, 1.60),
    'gpt-5-mini': (0.25, 2.00),
    'gpt-5-nano': (0.05, 0.40),
    'gpt-4.1-nano': (0.10, 0.40),
}

# ~chars per token heuristic for chunking (English-ish; close enough for limits).
_CHARS_PER_TOKEN = 4
# Leave headroom under the 128k context for system/user/output.
_MAP_CHUNK_TOKENS = 6000
_MAP_CHUNK_CHARS = _MAP_CHUNK_TOKENS * _CHARS_PER_TOKEN
_MAX_KEY_POINTS = 8
_MIN_KEY_POINTS = 5
_MAX_QUOTES = 3
_MAX_QUOTE_WORDS = 25

_sentry_kinds: set[str] = set()


def _truthy(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def summary_enabled() -> bool:
    return _truthy(os.getenv('SUMMARY_ENABLED', '0'))


def summary_model() -> str:
    return (os.getenv('SUMMARY_MODEL') or '').strip() or DEFAULT_SUMMARY_MODEL


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
                fingerprint=['summary', kind],
                tags={'summary.kind': kind},
            )
        else:
            sentry_sdk.capture_message(
                message or f'Summary failure: {kind}',
                level='error',
                fingerprint=['summary', kind],
                tags={'summary.kind': kind},
            )
    except Exception:  # noqa: BLE001
        pass


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _MODEL_RATES_PER_M.get(model) or _MODEL_RATES_PER_M[DEFAULT_SUMMARY_MODEL]
    inp, out = rates
    return round(
        (max(0, int(prompt_tokens)) * inp + max(0, int(completion_tokens)) * out)
        / 1_000_000.0,
        6,
    )


def _word_count(text: str) -> int:
    return len(re.findall(r'\S+', text or ''))


def _clip_quote(q: str) -> str:
    words = re.findall(r'\S+', (q or '').strip())
    if len(words) <= _MAX_QUOTE_WORDS:
        return ' '.join(words)
    return ' '.join(words[:_MAX_QUOTE_WORDS])


def _normalize_summary_payload(
    data: dict[str, Any],
    *,
    is_partial: bool,
    language: Optional[str],
) -> dict[str, Any]:
    tldr = (data.get('tldr') or data.get('tl_dr') or '').strip()
    points_raw = data.get('key_points') or data.get('keyPoints') or []
    if not isinstance(points_raw, list):
        points_raw = []
    points = [str(p).strip() for p in points_raw if str(p).strip()]
    points = points[:_MAX_KEY_POINTS]
    quotes_raw = data.get('quotes') or []
    if not isinstance(quotes_raw, list):
        quotes_raw = []
    quotes = []
    for q in quotes_raw:
        clipped = _clip_quote(str(q))
        if clipped and clipped not in quotes:
            quotes.append(clipped)
        if len(quotes) >= _MAX_QUOTES:
            break
    return {
        'tldr': tldr,
        'key_points': points,
        'quotes': quotes,
        'is_partial': bool(is_partial),
        'language': (language or '').strip() or None,
    }


def parse_summary_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def format_summary_for_txt(summary: dict[str, Any]) -> str:
    """Plain-text Summary section for .txt downloads (not used for .srt)."""
    lines = ['## Summary']
    if summary.get('is_partial'):
        lines.append('(Preview — this summary covers only the free preview portion.)')
        lines.append('')
    tldr = (summary.get('tldr') or '').strip()
    if tldr:
        lines.append(f'TL;DR: {tldr}')
        lines.append('')
    points = summary.get('key_points') or []
    if points:
        lines.append('Key points:')
        for p in points:
            lines.append(f'• {p}')
        lines.append('')
    quotes = summary.get('quotes') or []
    if quotes:
        lines.append('Notable quotes:')
        for q in quotes:
            lines.append(f'“{q}”')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _chunk_transcript(text: str) -> list[str]:
    text = (text or '').strip()
    if not text:
        return []
    if len(text) <= _MAP_CHUNK_CHARS:
        return [text]
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + _MAP_CHUNK_CHARS, n)
        if end < n:
            # Break on paragraph / sentence boundary when possible.
            window = text[start:end]
            br = max(window.rfind('\n\n'), window.rfind('. '), window.rfind('? '))
            if br > _MAP_CHUNK_CHARS // 3:
                end = start + br + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


_SYSTEM_PROMPT = (
    'You summarize podcast transcripts. Reply with JSON only, no markdown. '
    'Schema: {"tldr": string, "key_points": string[5..8], "quotes": string[0..3]}. '
    'Write tldr and key_points in the same language as the transcript. '
    'Each quote must be verbatim from the transcript and at most 25 words. '
    'If the transcript is a partial preview, still summarize only what is present.'
)


def _chat_json(client, *, model: str, user_content: str) -> tuple[dict, int, int]:
    """Call chat.completions; return (parsed dict, prompt_tokens, completion_tokens)."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        response_format={'type': 'json_object'},
        temperature=0.2,
    )
    content = ''
    if resp.choices:
        content = (resp.choices[0].message.content or '').strip()
    usage = getattr(resp, 'usage', None)
    prompt_tokens = int(getattr(usage, 'prompt_tokens', 0) or 0)
    completion_tokens = int(getattr(usage, 'completion_tokens', 0) or 0)
    try:
        data = json.loads(content) if content else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, prompt_tokens, completion_tokens


def _map_chunk_prompt(chunk: str, *, language: Optional[str], is_partial: bool) -> str:
    lang = language or 'the transcript language'
    partial = (
        'This is a PARTIAL free preview of a longer episode.\n'
        if is_partial else ''
    )
    return (
        f'{partial}'
        f'Summarize this transcript chunk in {lang}. '
        f'Return JSON with tldr, key_points (5-8), quotes (up to 3, <=25 words, verbatim).\n\n'
        f'---\n{chunk}\n---'
    )


def _reduce_prompt(partials: list[dict], *, language: Optional[str], is_partial: bool) -> str:
    lang = language or 'the transcript language'
    partial = (
        'The source was a PARTIAL free preview — label accordingly in tldr if helpful.\n'
        if is_partial else ''
    )
    blob = json.dumps(partials, ensure_ascii=False)
    return (
        f'{partial}'
        f'Merge these chunk summaries into one final summary in {lang}. '
        f'Return JSON with one tldr, 5-8 key_points, up to 3 short verbatim quotes.\n\n'
        f'{blob}'
    )


def generate_summary(
    client,
    transcript_text: str,
    *,
    language: Optional[str] = None,
    is_partial: bool = False,
    model: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate a summary. Returns (summary_dict, meta with tokens/cost/model).

    Raises on API failure (caller retries / records error).
    """
    model = (model or summary_model()).strip() or DEFAULT_SUMMARY_MODEL
    text = (transcript_text or '').strip()
    if not text:
        empty = _normalize_summary_payload(
            {'tldr': '', 'key_points': [], 'quotes': []},
            is_partial=is_partial, language=language,
        )
        return empty, {
            'model': model,
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'cost_usd_est': 0.0,
        }

    chunks = _chunk_transcript(text)
    prompt_tokens = 0
    completion_tokens = 0

    if len(chunks) == 1:
        data, pt, ct = _chat_json(
            client, model=model,
            user_content=_map_chunk_prompt(
                chunks[0], language=language, is_partial=is_partial),
        )
        prompt_tokens += pt
        completion_tokens += ct
    else:
        partials = []
        for chunk in chunks:
            data, pt, ct = _chat_json(
                client, model=model,
                user_content=_map_chunk_prompt(
                    chunk, language=language, is_partial=is_partial),
            )
            prompt_tokens += pt
            completion_tokens += ct
            partials.append(_normalize_summary_payload(
                data, is_partial=is_partial, language=language))
        data, pt, ct = _chat_json(
            client, model=model,
            user_content=_reduce_prompt(
                partials, language=language, is_partial=is_partial),
        )
        prompt_tokens += pt
        completion_tokens += ct

    summary = _normalize_summary_payload(
        data, is_partial=is_partial, language=language)
    # Pad key points if the model returned too few (still usable).
    if len(summary['key_points']) < _MIN_KEY_POINTS and summary['tldr']:
        # Don't invent — leave as-is; UI handles short lists.
        pass
    meta = {
        'model': model,
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'cost_usd_est': estimate_cost_usd(model, prompt_tokens, completion_tokens),
    }
    return summary, meta


def summarize_task(
    *,
    db,
    task,
    openai_client,
    user_id: Optional[int] = None,
    retry: bool = True,
    force: bool = False,
) -> bool:
    """Write summary_* columns on a completed task. Never raises. Returns ready?

    `force=True` skips the SUMMARY_ENABLED gate (for callers that already
    decided a summary must run).
    """
    from email_notify import task_is_partial_preview

    task_id = getattr(task, 'id', None)
    try:
        if not force and not summary_enabled():
            return False
        if not openai_client:
            return False
        text = (getattr(task, 'transcript_text', None) or '').strip()
        if not text:
            task.summary_status = 'skipped'
            db.session.commit()
            return False

        # Claim pending (skip if already ready).
        status = getattr(task, 'summary_status', None)
        if status == 'ready' and getattr(task, 'summary_json', None):
            return True
        task.summary_status = 'pending'
        db.session.commit()

        is_partial = task_is_partial_preview(task)
        language = getattr(task, 'language', None)
        last_exc = None
        attempts = 2 if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                summary, meta = generate_summary(
                    openai_client,
                    text,
                    language=language,
                    is_partial=is_partial,
                )
                task.summary_json = json.dumps(summary, ensure_ascii=False)
                task.summary_status = 'ready'
                task.summary_model = meta['model']
                task.summary_prompt_tokens = meta['prompt_tokens']
                task.summary_completion_tokens = meta['completion_tokens']
                task.summary_cost_usd_est = meta['cost_usd_est']
                db.session.commit()
                distinct = user_id or getattr(task, 'user_id', None) or 'system'
                product_analytics.capture(
                    'summary_generated',
                    distinct,
                    {
                        'model': meta['model'],
                        'tokens': meta['prompt_tokens'] + meta['completion_tokens'],
                        'prompt_tokens': meta['prompt_tokens'],
                        'completion_tokens': meta['completion_tokens'],
                        'cost_usd_est': meta['cost_usd_est'],
                        'partial': bool(is_partial),
                        'task_id': task_id,
                    },
                )
                logger.info(
                    'summary ready for task %s model=%s tokens=%s cost_usd_est=%s',
                    task_id, meta['model'],
                    meta['prompt_tokens'] + meta['completion_tokens'],
                    meta['cost_usd_est'],
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    'summary attempt %s/%s failed for %s: %s',
                    attempt, attempts, task_id, type(exc).__name__,
                )
                if attempt < attempts:
                    continue
        task.summary_status = 'error'
        db.session.commit()
        _report_once('generate_failed', last_exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception('summarize_task failed for %s', task_id)
        _report_once('summarize_task', exc)
        try:
            db.session.rollback()
            if task is not None:
                task.summary_status = 'error'
                db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
        return False
