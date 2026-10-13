"""Tests for admin_actual_costs (OpenAI Costs API vs estimates)."""
from datetime import date, datetime, timezone

import pytest

import admin_actual_costs as AA


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Session:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(dict(params or {}))
        return self.pages.pop(0)


def _ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def test_resolve_range_presets_and_custom_clamp():
    today = date(2026, 10, 13)
    assert AA.resolve_range('today', today=today) == ('today', today, today)
    assert AA.resolve_range('7d', today=today)[1] == date(2026, 10, 7)
    assert AA.resolve_range('bogus', today=today)[0] == '30d'
    p, s, e = AA.resolve_range('custom', '2026-10-20', '2026-10-01', today=today)
    assert (s, e) == (date(2026, 10, 1), date(2026, 10, 13))  # swapped + clamped to today
    p, s, e = AA.resolve_range('custom', '2025-01-01', '2026-10-13', today=today)
    assert (e - s).days + 1 == AA.MAX_RANGE_DAYS
    assert AA.resolve_range('custom', 'x', 'y', today=today)[0] == '30d'


def test_classify_line_item():
    assert AA.classify_line_item('whisper-1') == 'transcription'
    assert AA.classify_line_item('gpt-4o-mini-transcribe, input') == 'transcription'
    assert AA.classify_line_item('gpt-4o-mini-2024-07-18, input') == 'summary'
    assert AA.classify_line_item('web search') == 'other'


def test_fetch_without_admin_key_reports_missing(monkeypatch):
    monkeypatch.delenv('OPENAI_ADMIN_KEY', raising=False)
    out = AA.fetch_actual_costs_uncached(date(2026, 10, 1), date(2026, 10, 2))
    assert out['ok'] is False and out['missing_key'] is True
    # A normal project key is not accepted as an admin key.
    monkeypatch.setenv('OPENAI_ADMIN_KEY', 'sk-proj-abc')
    assert AA.fetch_actual_costs_uncached(date(2026, 10, 1), date(2026, 10, 2))['missing_key']


def test_fetch_paginates_and_sums_by_type(monkeypatch):
    monkeypatch.setenv('OPENAI_ADMIN_KEY', 'sk-admin-test')
    monkeypatch.setenv('OPENAI_COST_PROJECT_ID', 'proj_1')
    d1, d2 = date(2026, 10, 11), date(2026, 10, 12)
    sess = _Session([
        _Resp(200, {'data': [{'start_time': _ts(d1), 'results': [
            {'amount': {'value': 1.5}, 'line_item': 'whisper-1'},
            {'amount': {'value': 0.25}, 'line_item': 'gpt-4o-mini, input'},
        ]}], 'has_more': True, 'next_page': 'p2'}),
        _Resp(200, {'data': [{'start_time': _ts(d2), 'results': [
            {'amount': {'value': '2.0'}, 'line_item': 'whisper-1'},
        ]}], 'has_more': False}),
    ])
    out = AA.fetch_actual_costs_uncached(d1, d2, session=sess)
    assert out['ok'] and out['project_scoped']
    assert out['days']['2026-10-11']['transcription'] == pytest.approx(1.5)
    assert out['days']['2026-10-11']['summary'] == pytest.approx(0.25)
    assert out['days']['2026-10-12']['transcription'] == pytest.approx(2.0)
    assert out['line_items'][0] == {'name': 'whisper-1', 'usd': 3.5}
    assert sess.calls[0]['end_time'] == _ts(date(2026, 10, 13))
    assert sess.calls[0]['project_ids'] == 'proj_1'
    assert sess.calls[1]['page'] == 'p2'


def test_fetch_http_error_is_soft_and_secret_free(monkeypatch):
    monkeypatch.setenv('OPENAI_ADMIN_KEY', 'sk-admin-secretvalue')
    out = AA.fetch_actual_costs_uncached(
        date(2026, 10, 1), date(2026, 10, 1), session=_Session([_Resp(401, {})]))
    assert out['ok'] is False and '401' in out['error']
    assert 'secretvalue' not in out['error']


def test_collect_actual_vs_estimated_filters_and_totals(monkeypatch):
    today = date(2026, 10, 13)
    est = {'2026-10-12': {'transcription': 1.0, 'summary': 0.1},
           '2026-10-13': {'transcription': 0.5, 'summary': 0.0}}
    monkeypatch.setattr(AA, 'estimated_daily_by_utc', lambda db, s, e: est)

    def fetcher(s, e):
        return {'ok': True, 'days': {
            '2026-10-12': {'transcription': 1.2, 'summary': 0.2, 'other': 0.0},
            '2026-10-13': {'transcription': 0.3, 'summary': 0.0, 'other': 0.0}}}

    out = AA.collect_actual_vs_estimated(None, '7d', cost_type='all',
                                         today=today, fetcher=fetcher)
    assert out['total_actual_usd'] == pytest.approx(1.7)
    assert out['total_estimated_usd'] == pytest.approx(1.6)
    assert out['diff_usd'] == pytest.approx(0.1)
    assert out['today']['partial'] and out['today']['actual_usd'] == pytest.approx(0.3)
    assert len(out['chart']['labels']) == 7
    tr = AA.collect_actual_vs_estimated(None, '7d', cost_type='transcription',
                                        today=today, fetcher=fetcher)
    assert tr['total_actual_usd'] == pytest.approx(1.5)
    assert tr['total_estimated_usd'] == pytest.approx(1.5)


def test_collect_without_key_keeps_estimates(monkeypatch):
    monkeypatch.setattr(AA, 'estimated_daily_by_utc',
                        lambda db, s, e: {'2026-10-13': {'transcription': 0.5, 'summary': 0}})
    out = AA.collect_actual_vs_estimated(
        None, 'today', today=date(2026, 10, 13),
        fetcher=lambda s, e: {'ok': False, 'missing_key': True, 'error': 'x'})
    assert out['missing_key'] and out['total_actual_usd'] is None
    assert out['total_estimated_usd'] == pytest.approx(0.5)
    assert out['chart']['actual_usd'] == [None]
