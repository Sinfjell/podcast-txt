"""Unit tests for ops/posthog_daily_health.py (stdlib + pytest)."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

OPS = Path(__file__).resolve().parent / 'ops'
sys.path.insert(0, str(OPS))

import posthog_daily_health as phh  # noqa: E402


OSLO = ZoneInfo('Europe/Oslo')


def test_health_window_days_covers_weekend_gap():
    assert phh.health_window_days(date(2026, 9, 27)) == 3  # Sunday
    assert phh.health_window_days(date(2026, 9, 26)) == 2  # Saturday
    assert phh.health_window_days(date(2026, 9, 25)) == 1  # Friday
    assert phh.health_window_days(date(2026, 9, 22)) == 1  # Monday


def test_evaluate_health_all_normal_returns_no_warnings():
    snap = phh.HealthSnapshot(
        window_days=1,
        baseline_days=14,
        pt_events_window=40,
        cta_total_window=8,
        cta_total_baseline=20,
        cta_by_key_window={
            ('podskrift', 'hero'): 4,
            ('podskrift', 'nav'): 4,
        },
        cta_by_key_baseline={
            ('podskrift', 'hero'): 10,
            ('podskrift', 'nav'): 10,
        },
        utm_pageviews_window=3,
        event_counts_window={
            'podcast_searched': 5,
            'user_signed_up': 1,
            'settings_viewed': 2,
            'transcript_started': 4,
            'transcript_completed': 3,
            'transcript_failed': 1,
        },
        event_counts_baseline={
            'podcast_searched': 40,
            'user_signed_up': 4,
            'settings_viewed': 10,
            'transcript_started': 20,
            'transcript_completed': 18,
            'transcript_failed': 2,
        },
        failed_by_reason_window={'other': 1},
        failed_by_reason_baseline={'other': 2},
    )
    assert phh.evaluate_health(snap) == []
    assert phh.format_notes_content([]) is None


def test_evaluate_health_productivitytech_silent_folds_cta_and_utm():
    """Today's real situation: PT silent, cta 0, no utm — one folded warning."""
    snap = phh.HealthSnapshot(
        window_days=3,
        baseline_days=14,
        pt_events_window=0,
        cta_total_window=0,
        cta_total_baseline=0,
        utm_pageviews_window=0,
        event_counts_window={
            'podcast_searched': 28,
            'user_signed_up': 2,
            'transcript_started': 4,
            'transcript_failed': 3,
        },
        event_counts_baseline={
            # Below MIN_BASELINE_COUNT so Podskrift events do not also alarm
            # on a brand-new project.
            'podcast_searched': 2,
            'user_signed_up': 0,
            'transcript_started': 1,
            'transcript_failed': 0,
        },
    )
    lines = phh.evaluate_health(snap)
    assert len(lines) == 1
    assert lines[0].startswith('productivitytech.io: 0 events siste 3 dager')
    assert 'cta_clicked' not in lines[0]
    assert 'utm_source' not in '\n'.join(lines[1:])  # no extra lines
    notes = phh.format_notes_content(lines)
    assert notes is not None
    assert notes.startswith('⚠️ productivitytech.io:')
    assert 'https://eu.posthog.com/project/283916/activity/explore' in notes


def test_evaluate_health_utm_broken_when_pt_has_traffic():
    snap = phh.HealthSnapshot(
        window_days=1,
        pt_events_window=12,
        cta_total_window=3,
        cta_total_baseline=10,
        cta_by_key_window={('podskrift', 'hero'): 3},
        cta_by_key_baseline={('podskrift', 'hero'): 10},
        utm_pageviews_window=0,
    )
    lines = phh.evaluate_health(snap)
    assert any('utm_source=productivitytech' in line for line in lines)


def test_evaluate_health_cta_location_drop():
    snap = phh.HealthSnapshot(
        window_days=1,
        pt_events_window=20,
        cta_total_window=5,
        cta_total_baseline=20,
        cta_by_key_window={('podskrift', 'hero'): 5},
        cta_by_key_baseline={
            ('podskrift', 'hero'): 10,
            ('podskrift', 'footer'): 8,  # was active, now zero
        },
        utm_pageviews_window=2,
    )
    lines = phh.evaluate_health(snap)
    assert any('cta_clicked podskrift/footer' in line for line in lines)


def test_evaluate_health_event_stopped_and_sharp_drop():
    snap = phh.HealthSnapshot(
        window_days=3,
        pt_events_window=10,
        cta_total_window=2,
        cta_total_baseline=5,
        cta_by_key_window={('podskrift', 'nav'): 2},
        cta_by_key_baseline={('podskrift', 'nav'): 5},
        utm_pageviews_window=1,
        event_counts_window={
            'podcast_searched': 1,  # ~0.33/day vs ~2.9/day baseline → sharp drop
            'user_signed_up': 0,    # stopped
            'transcript_started': 4,
            'transcript_completed': 3,
            'transcript_failed': 0,
        },
        event_counts_baseline={
            'podcast_searched': 40,
            'user_signed_up': 6,
            'transcript_started': 20,
            'transcript_completed': 18,
            'transcript_failed': 1,
        },
    )
    text = '\n'.join(phh.evaluate_health(snap))
    assert '«user_signed_up»: 0 i vinduet' in text
    assert '«podcast_searched»: kraftig fall' in text


def test_evaluate_health_skips_events_below_min_baseline():
    snap = phh.HealthSnapshot(
        window_days=1,
        pt_events_window=5,
        cta_total_window=1,
        cta_total_baseline=1,
        utm_pageviews_window=1,
        event_counts_window={'trial_limit_hit': 0},
        event_counts_baseline={'trial_limit_hit': 2},  # < MIN (3)
    )
    assert phh.evaluate_health(snap) == []


def test_evaluate_health_failure_rate_spike():
    snap = phh.HealthSnapshot(
        window_days=3,
        pt_events_window=5,
        cta_total_window=1,
        cta_total_baseline=4,
        utm_pageviews_window=1,
        event_counts_window={
            'transcript_started': 10,
            'transcript_failed': 8,
            'podcast_searched': 5,
        },
        event_counts_baseline={
            'transcript_started': 20,
            'transcript_failed': 2,
            'podcast_searched': 30,
        },
        failed_by_reason_window={'invalid_key': 7, 'other': 1},
        failed_by_reason_baseline={'invalid_key': 1, 'other': 1},
    )
    lines = phh.evaluate_health(snap)
    joined = '\n'.join(lines)
    assert 'Feilrate transcript_failed/started' in joined
    assert 'Feilårsak «invalid_key»' in joined


def test_format_notes_truncates_under_limit():
    lines = [f'x{i} ' + ('y' * 80) for i in range(40)]
    content = phh.format_notes_content(lines)
    assert content is not None
    assert len(content) <= phh.NOTION_NOTES_MAX_CHARS
    assert content.endswith('…')


def test_notion_properties_omit_notes_when_healthy():
    # Import the metrics script via path load to avoid package issues.
    import importlib.util
    path = OPS / 'notion-daily-metrics.py'
    spec = importlib.util.spec_from_file_location('notion_daily_metrics', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    metrics = {
        'day': '2026-09-26',
        'Users total': 1,
        'Signups 1d': 0,
        'Active users 1d': 0,
        'Completions 1d': 0,
        'Errors 1d': 0,
        'Never started': 0,
        'Ever completed': 0,
        'Returned 2+ days': 0,
        'Trial minutes used': 0.0,
        'Trial exhausted': 0,
        'Saved feeds': 0,
    }
    props = mod.notion_properties(
        metrics, 'cron', datetime(2026, 9, 27, tzinfo=OSLO), notes_content=None,
    )
    assert 'Notes' not in props

    props_warn = mod.notion_properties(
        metrics,
        'cron',
        datetime(2026, 9, 27, tzinfo=OSLO),
        notes_content='⚠️ PostHog-helsesjekk kjørte ikke: POSTHOG_PERSONAL_API_KEY mangler',
    )
    assert 'Notes' in props_warn
    assert 'POSTHOG_PERSONAL_API_KEY mangler' in (
        props_warn['Notes']['rich_text'][0]['text']['content']
    )


def _minimal_podcast_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        '''
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            created_at TEXT,
            trial_seconds_used INTEGER,
            trial_seconds_limit INTEGER
        );
        CREATE TABLE transcription_tasks (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            status TEXT,
            started_at TEXT,
            completed_at TEXT,
            heartbeat_at TEXT,
            trial_seconds_charged INTEGER
        );
        CREATE TABLE saved_feeds (
            id INTEGER PRIMARY KEY,
            created_at TEXT
        );
        CREATE TABLE trial_budget_days (
            day TEXT PRIMARY KEY,
            seconds_used INTEGER NOT NULL DEFAULT 0
        );
        '''
    )
    conn.commit()
    conn.close()


def test_dry_run_missing_key_prints_could_not_run_and_skips_notion(capsys):
    import importlib.util
    path = OPS / 'notion-daily-metrics.py'
    spec = importlib.util.spec_from_file_location('notion_daily_metrics_main', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / 'podcast.db'
        _minimal_podcast_db(db)
        env_backup = {
            k: os.environ.get(k)
            for k in (
                'DRY_RUN',
                'POSTHOG_PERSONAL_API_KEY',
                'NOTION_TOKEN',
                'PODSKRIFT_DB',
            )
        }
        try:
            os.environ['DRY_RUN'] = '1'
            os.environ.pop('POSTHOG_PERSONAL_API_KEY', None)
            os.environ.pop('NOTION_TOKEN', None)
            os.environ['PODSKRIFT_DB'] = str(db)
            rc = mod.main(['--day', '2026-09-26', '--db', str(db)])
            assert rc == 0
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    out = capsys.readouterr().out
    assert 'PostHog-helsesjekk kjørte ikke: POSTHOG_PERSONAL_API_KEY mangler' in out
    assert '"Signups 1d"' in out  # metrics JSON still printed


def test_snapshot_from_query_results_accepts_row_lists():
    snap = phh.snapshot_from_query_results(
        window_days=3,
        baseline_days=14,
        pt_cta_utm_row=[0, 0, 0, 0],
        cta_breakdown_rows=[['podskrift', 'hero', 0, 5]],
        podskrift_event_rows=[['podcast_searched', 2, 10]],
        failed_reason_rows=[['network', 1, 0]],
    )
    assert snap.pt_events_window == 0
    assert snap.cta_by_key_baseline[('podskrift', 'hero')] == 5
    assert snap.event_counts_window['podcast_searched'] == 2
    assert snap.failed_by_reason_window['network'] == 1


def test_trial_global_cap_warning_lines_at_thresholds():
    import importlib.util
    path = OPS / 'notion-daily-metrics.py'
    spec = importlib.util.spec_from_file_location('notion_daily_metrics_cap', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    cap = 1800 * 60  # seconds
    assert mod.trial_daily_cap_warning_lines(0, cap) == []
    assert mod.trial_daily_cap_warning_lines(int(cap * 0.69), cap) == []
    at70 = mod.trial_daily_cap_warning_lines(int(cap * 0.70), cap)
    assert len(at70) == 1
    assert '70%+' in at70[0]
    assert 'TRIAL_DAILY_MINUTES' in at70[0]
    at90 = mod.trial_daily_cap_warning_lines(int(cap * 0.91), cap)
    assert len(at90) == 2  # both 70 and 90
    assert any('90%+' in line for line in at90)
    # Cap disabled → no warnings.
    assert mod.trial_daily_cap_warning_lines(10**9, 0) == []
    # Back-compat alias still works.
    assert mod.trial_global_cap_warning_lines(int(cap * 0.70), cap) == at70


def test_collect_metrics_includes_daily_trial_used_vs_cap():
    import importlib.util
    path = OPS / 'notion-daily-metrics.py'
    spec = importlib.util.spec_from_file_location('notion_daily_metrics_collect', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / 'podcast.db'
        _minimal_podcast_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO users (id, created_at, trial_seconds_used, trial_seconds_limit) "
            "VALUES (1, '2026-01-01 00:00:00', ?, 10800)",
            (1260 * 60,),  # lifetime personal burn (still reported)
        )
        conn.execute(
            "INSERT INTO trial_budget_days (day, seconds_used) VALUES (?, ?)",
            ('2026-09-26', 680 * 60),
        )
        conn.commit()
        metrics = mod.collect_metrics(
            conn, date(2026, 9, 26), trial_default_seconds=180 * 60,
            trial_daily_seconds=750 * 60,
        )
        conn.close()

    assert metrics['Trial minutes used'] == 1260.0
    assert metrics['Trial daily used minutes'] == 680.0
    assert metrics['Trial daily cap minutes'] == 750.0
    assert metrics['_trial_daily_used_seconds'] == 680 * 60
    assert metrics['_trial_daily_cap_seconds'] == 750 * 60
