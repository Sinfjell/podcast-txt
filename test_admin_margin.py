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
    # Counted purchases only — refunded buys are passed via refund_rows.
    purchases = [
        {'user_id': 1, 'minutes': 1200, 'cents': 1100, 'tax_cents': 0,
         'price_source': 'slider', 'pricing_version': 'vol_v1', 'pi': 'pi_a',
         'clawed': 0},
        {'user_id': 3, 'minutes': 90, 'cents': 194, 'tax_cents': 0,
         'price_source': 'episode', 'pricing_version': 'vol_v1',
         'pi': 'pi_missing', 'clawed': 0},
    ]
    refund_rows = [{
        'user_id': 2, 'gross_cents': 500, 'refunded_cents': 500,
        'status': 'refunded', 'email': 'buyer@example.com',
    }]
    out = M.contribution_by_plan(purchases, by, 0.01, {1: 0.25, 3: 1.0},
                                 trial_minutes=100, refund_rows=refund_rows)
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
    assert 'legacy' not in p  # refunded legacy pack not in counted purchases
    ep = p['episode']
    assert ep['stripe_matched'] == 0 and ep['net_usd'] == pytest.approx(1.94)
    assert out['trial']['cost_usd'] == pytest.approx(1.0)
    assert out['trial']['cost_list_usd'] == pytest.approx(0.6)
    t = out['totals']
    assert t['count'] == 2
    assert t['refund_count'] == 1 and t['dispute_count'] == 0
    assert t['refunds_disputes_usd'] == pytest.approx(5.0)
    assert t['db_after_trial_usd'] == pytest.approx(t['db_used_so_far_usd'] - 1.0)
    assert [g['plan'] for g in out['plans']] == ['vol_1200', 'episode']


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
    assert 'paid incl. VAT' in body
    assert 'ex VAT' in body
    assert ' credited</div>' not in body
    assert 'UTC' in body and 'partial' in body


def test_cost_per_minute_uses_all_transcribed_minutes():
    assert M.cost_per_minute(64.98, 9663.9, 795.6) == (pytest.approx(64.98 / 10459.5), 'actual')
    assert M.cost_per_minute(10, 0, 0) == (0.006, 'list')
    assert M.cost_per_minute(10, 100, 0, cost_ok=False) == (0.006, 'list')


def test_cost_per_minute_implausible_falls_back_to_list():
    assert M.cost_per_minute(64.68, 12.1, 0) == (0.006, 'list_implausible')
    assert M.cost_per_minute(0.006 * 5 * 100, 100, 0)[1] == 'actual'
