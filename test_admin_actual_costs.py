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


def test_fetch_retries_on_429(monkeypatch):
    monkeypatch.setenv('OPENAI_ADMIN_KEY', 'sk-admin-test')
    monkeypatch.setattr(AA.time, 'sleep', lambda s: None)
    sess = _Session([_Resp(429, {}), _Resp(200, {'data': [], 'has_more': False})])
    out = AA.fetch_actual_costs_uncached(date(2026, 10, 1), date(2026, 10, 1), session=sess)
    assert out['ok'] and len(sess.calls) == 2


def test_errors_cached_briefly(monkeypatch):
    AA.clear_cache()
    monkeypatch.setattr(AA, 'fetch_actual_costs_uncached',
                        lambda s, e, session=None: {'ok': False, 'error': 'x'})
    AA.fetch_actual_costs(date(2026, 10, 1), date(2026, 10, 1))
    exp = list(AA._cache.values())[0][0] - AA.time.monotonic()
    assert exp <= AA.ERROR_CACHE_TTL_SEC
    AA.clear_cache()


# --------------------------------------------------------------------------
# USD income / margin (money reporting)
# --------------------------------------------------------------------------
from datetime import timedelta as _td


def _bt(created, btype, amount, fee, net, rate=None, src='usd', fds=(), cur='nok'):
    return {'created': created, 'type': btype, 'currency': cur, 'amount': amount,
            'fee': fee, 'net': net, 'exchange_rate': rate,
            'source_currency': src if rate else None, 'fee_details': list(fds)}


def test_bt_usd_factor_uses_charge_exchange_rate():
    import admin_dashboard as AD
    assert AD._bt_usd_factor('usd', None, None) == 1.0
    assert AD._bt_usd_factor('nok', 9.5, 'usd') == pytest.approx(1 / 9.5)
    assert AD._bt_usd_factor('nok', None, None) is None
    assert AD._bt_usd_factor('nok', 9.5, 'eur') is None  # not a USD charge


def test_summarize_bts_usd_real_shape():
    """Shape copied from prod: $11 charge settled in NOK, fees + withheld tax,
    a standalone NOK stripe_fee (no rate), and a refunded $5 charge."""
    import admin_dashboard as AD
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    t = int(now.timestamp())
    bts = [
        _bt(t - 3600, 'charge', 10501, 2763, 7738, 9.54594,
            fds=[('stripe_fee', 452), ('stripe_fee', 210), ('withheld_tax', 2101)]),
        _bt(t - 7200, 'stripe_fee', -233, 0, -233),
        _bt(t - 20 * 86400, 'charge', 4797, 1371, 3426, 9.59499,
            fds=[('stripe_fee', 96), ('stripe_fee', 315), ('withheld_tax', 960)]),
        _bt(t - 20 * 86400 + 60, 'refund', -4797, -959, -3838, 9.595,
            fds=[('withheld_tax_refund', -959)]),
        _bt(t - 3000, 'payout', -5000, 0, -5000),  # ignored (not income)
    ]
    out = AD.summarize_bts_usd(bts, now=now)
    today = out['today']
    assert today['gross_usd'] == pytest.approx(11.0, abs=0.01)  # 10501/9.54594 = $11
    # net = (7738 - 233) NOK, charge at its own rate, fee row at nearest rate
    assert today['net_usd'] == pytest.approx((7738 - 233) / 9.54594 / 100, abs=0.01)
    assert today['approx_count'] == 1 and today['unconverted_count'] == 0
    assert today['withheld_tax_usd'] == pytest.approx(2101 / 9.54594 / 100, abs=0.01)
    assert out['7d']['net_usd'] == today['net_usd']
    allw = out['all']
    assert allw['refunds_usd'] == pytest.approx(4797 / 9.595 / 100, abs=0.01)
    expected_all = (7738 / 9.54594 - 233 / 9.54594 + 3426 / 9.59499 - 3838 / 9.595) / 100
    assert allw['net_usd'] == pytest.approx(expected_all, abs=0.01)
    assert allw['native_net']['nok'] == 7738 - 233 + 3426 - 3838


def test_summarize_bts_usd_unconvertible_is_counted_not_guessed():
    import admin_dashboard as AD
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    out = AD.summarize_bts_usd(
        [_bt(int(now.timestamp()) - 60, 'stripe_fee', -100, 0, -100)], now=now)
    assert out['all']['unconverted_count'] == 1
    assert out['all']['net_usd'] == 0


def test_headline_kpis_margin_is_usd_minus_actual(monkeypatch):
    import admin_dashboard as AD
    monkeypatch.setattr(AD, 'collect_kpis', lambda *a, **k: {
        'trial_daily_used_minutes': 0, 'trial_daily_limit_minutes': 0})
    stripe = {'ok': True, 'usd': {
        'all': {'net_usd': 24.0, 'native_net': {'nok': 23340}},
        '7d': {'net_usd': 20.0, 'native_net': {}},
        'today': {'net_usd': 7.86, 'native_net': {}}}}
    costs = {'windows': {'all': {'openai_usd': 50.0}, '7d': {'openai_usd': 40.0},
                         'today': {'openai_usd': 4.0}}}
    actual = {'ok': True, 'all': 58.29, '7d': 54.08, 'today': 2.86}
    k = AD.collect_headline_kpis(None, 0, costs=costs, stripe_revenue=stripe,
                                 actual_cost=actual)
    assert k['cost_source'] == 'actual'
    assert k['income_all_usd'] == 24.0 and k['income_source'] == 'stripe_net_usd'
    assert k['margin_all_usd'] == pytest.approx(24.0 - 58.29)
    assert k['margin_7d_usd'] == pytest.approx(20.0 - 54.08)
    assert k['margin_today_usd'] == pytest.approx(7.86 - 2.86)
    # Falls back to estimate when actual is unavailable.
    k2 = AD.collect_headline_kpis(None, 0, costs=costs, stripe_revenue=stripe,
                                  actual_cost={'ok': False})
    assert k2['cost_source'] == 'estimate'
    assert k2['margin_all_usd'] == pytest.approx(24.0 - 50.0)


def test_actual_cost_windows_sums_utc_days():
    today = date(2026, 10, 13)
    days = {(today - _td(days=i)).isoformat(): {'transcription': 1.0, 'summary': 0.5, 'other': 0.0}
            for i in range(10)}
    seen = {}
    def fetcher(s, e):
        seen['range'] = (s, e)
        return {'ok': True, 'days': days}
    out = AA.actual_cost_windows(None, today=today, fetcher=fetcher)
    assert out['today'] == pytest.approx(1.5)
    assert out['7d'] == pytest.approx(10.5)
    assert out['all'] == pytest.approx(15.0)
    assert seen['range'] == (today - _td(days=29), today)
    assert AA.actual_cost_windows(None, today=today,
                                  fetcher=lambda s, e: {'ok': False})['ok'] is False
