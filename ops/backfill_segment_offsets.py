#!/usr/bin/env python3
"""Correct stored segment timestamps shifted by the equal-split chunk bug.

Before the fix, multi-chunk jobs offset each part by
``i * (audio_duration / chunk_total)`` even though ffmpeg cuts fixed
``SEGMENT_SECONDS`` (900 s) parts. Chunk 2+ captions start too early and
overlap; seeking and SRT/VTT drift.

This script rewrites ``segments_json`` only when the stored structure makes
the old formula provable (start regressions between parts whose first
timestamp matches ``i * audio_duration / chunk_total``). Already-correct
rows, single-chunk jobs, and ambiguous layouts are skipped.

Dry-run by default. Not wired to deploy. Back up first:

    bash ops/backup-db.sh
    sudo -u podskrift .venv/bin/python ops/backfill_segment_offsets.py
    sudo -u podskrift .venv/bin/python ops/backfill_segment_offsets.py --apply

Optional filters:

    ... --task-id <id>          # one row
    ... --db /path/to/podcast.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Keep in lockstep with app.SEGMENT_SECONDS (importing app pulls Flask/DB).
SEGMENT_SECONDS = 900
#: First segment of a wrongly-offset chunk should land near i * (D/N).
WRONG_START_TOLERANCE_SECONDS = 20.0
#: Within a chunk Whisper can overlap slightly; a real part boundary jumps back
#: by hundreds of seconds under the bug.
START_REGRESSION_SECONDS = 5.0


def split_chunk_groups(segments):
    """Split ordered segments into groups at absolute-start regressions."""
    if not segments:
        return []
    groups = [[segments[0]]]
    for seg in segments[1:]:
        prev = groups[-1][-1]
        try:
            start = float(seg['start'])
            prev_start = float(prev['start'])
        except (KeyError, TypeError, ValueError):
            groups[-1].append(seg)
            continue
        if start + START_REGRESSION_SECONDS < prev_start:
            groups.append([seg])
        else:
            groups[-1].append(seg)
    return groups


def plan_segment_correction(audio_duration, chunk_total, segments,
                            segment_seconds=SEGMENT_SECONDS,
                            tol=WRONG_START_TOLERANCE_SECONDS):
    """Return (corrected_segments_or_None, reason).

    ``reason`` is ``ok`` when a correction is available; otherwise a short
    skip code for reporting.
    """
    try:
        n = int(chunk_total)
    except (TypeError, ValueError):
        return None, 'bad_chunk_total'
    if n < 2:
        return None, 'not_multi_chunk'
    try:
        duration = float(audio_duration)
    except (TypeError, ValueError):
        return None, 'no_duration'
    if duration <= 0:
        return None, 'no_duration'
    if not isinstance(segments, list) or not segments:
        return None, 'no_segments'

    wrong_dur = duration / n
    # Exact multiples: old and new formulas agree; nothing to rewrite.
    if abs(wrong_dur - float(segment_seconds)) < 0.5:
        return None, 'formulas_agree'

    groups = split_chunk_groups(segments)
    if len(groups) != n:
        return None, f'group_count:{len(groups)}!={n}'

    for i, group in enumerate(groups):
        try:
            actual = float(group[0]['start'])
        except (KeyError, TypeError, ValueError):
            return None, f'bad_segment_chunk_{i}'
        expected_wrong = i * wrong_dur
        expected_right = i * float(segment_seconds)
        near_wrong = abs(actual - expected_wrong) <= tol
        near_right = abs(actual - expected_right) <= tol
        if i == 0:
            # Chunk 0 offset is 0 either way; require a plausible local start.
            if actual > tol + 60:
                return None, 'chunk_0_start_implausible'
            continue
        if near_right and not near_wrong:
            return None, 'already_corrected'
        if not near_wrong:
            return None, f'chunk_{i}_not_wrong_formula'

    corrected = []
    for i, group in enumerate(groups):
        wrong_off = i * wrong_dur
        right_off = i * float(segment_seconds)
        delta = right_off - wrong_off
        for seg in group:
            try:
                start = float(seg['start']) + delta
                end = float(seg['end']) + delta
            except (KeyError, TypeError, ValueError):
                return None, 'bad_segment_fields'
            out = dict(seg)
            out['start'] = start
            out['end'] = end
            corrected.append(out)

    for a, b in zip(corrected, corrected[1:]):
        if float(b['start']) + 0.05 < float(a['start']):
            return None, 'corrected_not_monotonic'
    return corrected, 'ok'


def _load_segments(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def iter_candidates(conn, task_id=None):
    sql = (
        'SELECT id, audio_duration, chunk_total, segments_json '
        'FROM transcription_tasks '
        'WHERE status = ? AND chunk_total IS NOT NULL AND chunk_total >= 2 '
        'AND segments_json IS NOT NULL'
    )
    params = ['completed']
    if task_id:
        sql += ' AND id = ?'
        params.append(task_id)
    sql += ' ORDER BY id'
    for row in conn.execute(sql, params):
        yield row


def plan_db(conn, task_id=None):
    """Scan tasks; return {correct: [...], skip: {reason: count}, samples}."""
    correct = []
    skip = {}
    samples = []
    for tid, audio_duration, chunk_total, segments_json in iter_candidates(
            conn, task_id=task_id):
        segments = _load_segments(segments_json)
        fixed, reason = plan_segment_correction(
            audio_duration, chunk_total, segments or [])
        if reason != 'ok' or fixed is None:
            skip[reason] = skip.get(reason, 0) + 1
            continue
        delta = float(SEGMENT_SECONDS) - float(audio_duration) / int(chunk_total)
        entry = {
            'id': tid,
            'audio_duration': audio_duration,
            'chunk_total': chunk_total,
            'segments': len(fixed),
            'delta_seconds_chunk1': delta,
            'segments_json': json.dumps(fixed, ensure_ascii=False),
        }
        correct.append(entry)
        if len(samples) < 5:
            before_groups = split_chunk_groups(segments)
            chunk0_len = len(before_groups[0])
            samples.append({
                'id': tid,
                'chunk_total': chunk_total,
                'audio_duration': audio_duration,
                'delta_seconds_chunk1': round(delta, 2),
                'first_chunk1_start_before': round(
                    float(before_groups[1][0]['start']), 2),
                # Corrected rows are monotonic, so group-splitting no longer
                # sees a regression; index by the pre-fix chunk-0 length.
                'first_chunk1_start_after': round(
                    float(fixed[chunk0_len]['start']), 2),
            })
    return {'correct': correct, 'skip': skip, 'samples': samples}


def apply_corrections(conn, entries):
    for entry in entries:
        conn.execute(
            'UPDATE transcription_tasks SET segments_json = ? WHERE id = ?',
            (entry['segments_json'], entry['id']),
        )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default=str(ROOT / 'data' / 'podcast.db'))
    ap.add_argument('--task-id', default=None)
    ap.add_argument('--apply', action='store_true',
                    help='Write corrections (default is dry-run)')
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        plan = plan_db(conn, task_id=args.task_id)
        report = {
            'mode': 'apply' if args.apply else 'dry-run',
            'would_correct': len(plan['correct']),
            'skip_reasons': plan['skip'],
            'samples': plan['samples'],
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        if not args.apply:
            return 0
        if not plan['correct']:
            return 0
        with conn:
            apply_corrections(conn, plan['correct'])
        print(json.dumps({'applied': len(plan['correct'])}))
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
