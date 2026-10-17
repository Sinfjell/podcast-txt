"""Money reporting tests: daily margin + contribution margin per plan."""
from datetime import date, datetime, timezone

import pytest

import admin_margin as M


def _bt(created, btype, amount, fee, net, rate=None, pi=None, fds=(), cur='nok'):
    return {'created': created, 'type': btype, 'currency': cur, 'amount': amount,
            'fee': fee, 'net': net, 'exchange_rate': rate,
            'source_currency': 'usd' if rate else None, 'payment_intent': pi,
            'fee_details': list(fds)}


T = int(datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp())
BTS = [
    # $11 charge in NOK (prod shape)
    _bt(T, 'charge', 10501, 2763, 7738, 9.54594, 'pi_a',
        [('stripe_fee', 452), ('stripe_fee', 210), ('withheld_tax', 2101)]),
    # standalone fee next day, no rate
    _bt(T + 86400, 'stripe_fee', -233, 0, -233),
    # $5 charge then full refund
    _bt(T - 86400, 'charge', 4797, 1371, 3426, 9.59499, 'pi_b',
        [('stripe_fee', 96), ('stripe_fee', 315), ('withheld_tax', 960)]),
    _bt(T - 86400 + 60, 'refund', -4797, -959, -3838, 9.595, 'pi_b',
        [('withheld_tax_refund', -959)]),
    _bt(T, 'payout', -9999, 0, -9999),
]


def test_plan_key():
    assert M.plan_key(300, None, None) == 'legacy'
    assert M.plan_key(90, 'episode', 'vol_v1') == 'episode'
    assert M.plan_key(1200, 'slider', 'vol_v1') == 'vol_1200'
    assert M.plan_key(300, 'default', 'vol_v1') == 'vol_300'
    assert M.plan_key(75, 'slider', 'vol_v1') == 'vol_custom'


def test_net_by_utc_day_and_payment_intent():
    days = M.stripe_net_usd_by_utc_day(BTS)
    assert days['2026-10-12'] == pytest.approx(7738 / 9.54594 / 100)
    assert days['2026-10-13'] == pytest.approx(-233 / 9.54594 / 100)  # nearest rate
    assert days['2026-10-11'] == pytest.approx((3426 / 9.59499 - 3838 / 9.595) / 100)
    assert '2026-10-12' in days and sum(days.values()) < 100  # payout excluded
    by = M.stripe_usd_by_payment_intent(BTS)
    a = by['pi_a']
    assert a['gross'] == pytest.approx(11.0, abs=0.01)
    assert a['fees'] == pytest.approx(662 / 9.54594 / 100)
    assert a['tax'] == pytest.approx(2101 / 9.54594 / 100)
    assert a['net'] == pytest.approx(a['gross'] - a['fees'] - a['tax'], abs=0.01)
    b = by['pi_b']
    assert b['refunds'] == pytest.approx(5.0, abs=0.01)
    assert b['net'] == pytest.approx((3426 / 9.59499 - 3838 / 9.595) / 100)


def test_daily_margin_marks_today_and_sums():
    out = M.daily_margin(date(2026, 10, 11), date(2026, 10, 13),
                         {'2026-10-12': 8.0}, {'2026-10-12': 3.0, '2026-10-13': 1.0},
                         today=date(2026, 10, 13))
    assert [r['partial'] for r in out['rows']] == [False, False, True]
    assert out['rows'][1]['margin_usd'] == pytest.approx(5.0)
    assert out['total_income_usd'] == 8.0 and out['total_cost_usd'] == 4.0
    assert out['total_margin_usd'] == 4.0
    no = M.daily_margin(date(2026, 10, 13), date(2026, 10, 13), {}, {},
                        today=date(2026, 10, 13), cost_ok=False)
    assert no['total_cost_usd'] is None and no['rows'][0]['margin_usd'] is None


def test_contribution_by_plan_all_used_vs_so_far():
    by = M.stripe_usd_by_payment_intent(BTS)
    purchases = [
        {'user_id': 1, 'minutes': 1200, 'cents': 1100, 'price_source': 'slider',
         'pricing_version': 'vol_v1', 'pi': 'pi_a', 'clawed': 0},
        {'user_id': 2, 'minutes': 300, 'cents': 500, 'price_source': None,
         'pricing_version': None, 'pi': 'pi_b', 'clawed': 300 * 60},  # refunded
        {'user_id': 3, 'minutes': 90, 'cents': 194, 'price_source': 'episode',
         'pricing_version': 'vol_v1', 'pi': 'pi_missing', 'clawed': 0},
    ]
    out = M.contribution_by_plan(purchases, by, 0.01, {1: 0.25, 3: 1.0},
                                 trial_minutes=100)
    p = {g['plan']: g for g in out['plans']}
    v = p['vol_1200']
    assert v['count'] == 1 and v['minutes_sold'] == 1200
    assert v['cost_all_used_usd'] == pytest.approx(12.0)
    assert v['minutes_used'] == pytest.approx(300)
    assert v['cost_used_so_far_usd'] == pytest.approx(3.0)
    assert v['db_all_used_usd'] == pytest.approx(v['net_usd'] - 12.0, abs=1e-3)
    assert v['db_used_so_far_usd'] == pytest.approx(v['net_usd'] - 3.0, abs=1e-3)
    assert v['db_pct_all_used'] == pytest.approx(
        round(100 * v['db_all_used_usd'] / v['gross_usd'], 1))
    leg = p['legacy']
    assert leg['minutes_sold'] == 0 and leg['cost_all_used_usd'] == 0  # clawed back
    assert leg['refunds_usd'] == pytest.approx(5.0, abs=0.01)
    ep = p['episode']
    assert ep['stripe_matched'] == 0 and ep['net_usd'] == pytest.approx(1.94)
    assert out['trial']['cost_usd'] == pytest.approx(1.0)
    assert out['trial']['cost_list_usd'] == pytest.approx(0.6)
    t = out['totals']
    assert t['count'] == 3
    assert t['db_after_trial_usd'] == pytest.approx(t['db_used_so_far_usd'] - 1.0)
    assert [g['plan'] for g in out['plans']] == ['vol_1200', 'episode', 'legacy']


def test_theoretical_table_covers_every_anchor():
    rows = M.theoretical_table(0.006)
    assert [r['minutes'] for r in rows] == [60, 300, 600, 1200]
    r300 = rows[1]
    assert r300['price_usd'] == 5.0
    assert r300['model_fee_usd'] == pytest.approx((30 + 500 * 0.064) / 100)
    assert r300['cost_usd'] == pytest.approx(1.8)
    assert r300['db_usd'] == pytest.approx(5.0 - r300['model_fee_usd'] - 1.8)
    obs = M.theoretical_table(0.006, 0.2)
    assert obs[3]['fee_tax_usd'] == pytest.approx(2.2)
    assert obs[3]['db_usd'] == pytest.approx(11 - 2.2 - 7.2)


def test_admin_page_renders_margin_panels_and_purchase_wording(monkeypatch):
    import test_app as TA
    monkeypatch.setenv('ADMIN_EMAILS', 'admin-margin@test.com')
    monkeypatch.delenv('OPENAI_ADMIN_KEY', raising=False)
    client, _uid = TA._admin_login('admin-margin@test.com')
    body = client.get('/admin?cost_range=7d').data.decode()
    assert 'Contribution margin per plan' in body
    assert 'Margin unavailable' not in body
    assert 'Per price point (vol_v1' in body
    assert 'gross paid (all payments, list price)' in body
    assert ' credited</div>' not in body
    assert 'UTC' in body and 'partial' in body


def test_cost_per_minute_uses_all_transcribed_minutes():
    assert M.cost_per_minute(64.98, 9663.9, 795.6) == (pytest.approx(64.98 / 10459.5), 'actual')
    assert M.cost_per_minute(10, 0, 0) == (0.006, 'list')
    assert M.cost_per_minute(10, 100, 0, cost_ok=False) == (0.006, 'list')


def test_cost_per_minute_implausible_falls_back_to_list():
    assert M.cost_per_minute(64.68, 12.1, 0) == (0.006, 'list_implausible')
    assert M.cost_per_minute(0.006 * 5 * 100, 100, 0)[1] == 'actual'


def test_format_usd_negative_uses_true_minus():
    import admin_dashboard as AD
    assert AD.format_usd(-0.35) == '\u2212$0.35'
    assert AD.format_usd(0.35) == '$0.35'
    assert AD.format_usd(0.35, sign=True) == '+$0.35'
    assert AD.format_usd(-0.001) == '$0.00'
    assert AD.format_usd(-0.001, sign=True) == '+$0.00'
    assert AD.format_usd(None) == '—'
    assert AD.format_usd(-1234.5) == '\u2212$1234.50'


def test_admin_page_has_no_dollar_minus_and_purchases_same_basis(monkeypatch):
    import re
    import test_app as TA
    from models import db, CreditPurchase
    monkeypatch.setenv('ADMIN_EMAILS', 'admin-nits@test.com')
    monkeypatch.delenv('OPENAI_ADMIN_KEY', raising=False)
    client, uid = TA._admin_login('admin-nits@test.com')
    with TA.A.app.app_context():
        db.session.add(CreditPurchase(
            user_id=uid, stripe_session_id='cs_nits_1', amount_cents=500,
            amount_total_cents=500, currency='usd', minutes=300, status='credited'))
        db.session.add(CreditPurchase(
            user_id=uid, stripe_session_id='cs_nits_2', amount_cents=500,
            amount_total_cents=500, currency='usd', minutes=300, status='refunded',
            amount_refunded_cents=500, seconds_clawed_back=18000))
        db.session.commit()
    body = client.get('/admin?cost_range=7d').data.decode()
    assert not re.search(r'\$-\d', body)
    assert '+-' not in body
    m = re.search(r'data-kpi="purchase_refunds">([^<]+)<', body)
    assert m and m.group(1).startswith('\u2212$') and 'refunded' in m.group(1)
    assert 'gross paid (all payments, list price)' in body
    with TA.A.app.app_context():
        import admin_dashboard as AD
        import app as A
        k = AD.collect_kpis(db, A.TRIAL_DAILY_SECONDS)
    assert k['paid_refunded_count'] >= 1
    assert k['paid_count_all'] >= k['purchase_count'] + k['paid_refunded_count']
    assert k['paid_gross_all_usd'] >= k['purchase_gross_usd'] + 5.0
