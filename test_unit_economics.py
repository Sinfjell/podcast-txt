"""ops/unit_economics.py -- per-day variable-cost unit economics (no real data)."""

import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parent / 'ops'
sys.path.insert(0, str(OPS))
import unit_economics as ue  # noqa: E402


def _db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, created_at TEXT);
        CREATE TABLE credit_purchases (
            id INTEGER PRIMARY KEY, user_id INTEGER, created_at TEXT,
            amount_cents INTEGER, amount_total_cents INTEGER,
            amount_tax_cents INTEGER, status TEXT,
            stripe_payment_intent_id TEXT);
        CREATE TABLE transcription_tasks (
            id TEXT PRIMARY KEY, user_id INTEGER, status TEXT,
            started_at TEXT, completed_at TEXT,
            trial_seconds_charged INTEGER, paid_seconds_charged INTEGER,
            stt_provider TEXT, stt_cost_usd REAL, summary_cost_usd_est REAL);
    ''')
    conn.executemany('INSERT INTO users VALUES (?,?,?)', [
        (500, 'a@gmail.com', '2026-10-01'),
        (501, 'b@gmail.com', '2026-10-01'),
        (90, 'x@gmail.com', '2026-10-01'),          # excluded id
        (502, 'smoke-1@gmail.com', '2026-10-01'),   # test email
    ])
    conn.executemany(
        'INSERT INTO credit_purchases VALUES (?,?,?,?,?,?,?,?)', [
            # 2026-10-10 Oslo
            (1, 500, '2026-10-10 10:00:00', 172, 172, 0, 'credited', 'pi_a1'),
            # 2026-10-12: repeat buyer 500 + new buyer 501 (late UTC = 13th Oslo? no: 20:00Z = 22:00 Oslo)
            (2, 500, '2026-10-12 20:00:00', 282, 282, 57, 'credited', 'pi_a2'),
            (3, 501, '2026-10-12 21:30:00', 172, 172, 0, 'credited', 'pi_b1'),  # 23:30 Oslo
            (4, 90, '2026-10-12 11:00:00', 999, 999, 0, 'credited', 'pi_t1'),
            (5, 502, '2026-10-12 11:00:00', 999, 999, 0, 'credited', 'pi_t2'),
            (6, 501, '2026-10-12 12:00:00', 500, 500, 0, 'refunded', 'pi_r1'),
        ])
    conn.executemany(
        'INSERT INTO transcription_tasks VALUES (?,?,?,?,?,?,?,?,?,?)', [
            # legacy whisper-1, 100 charged min -> estimate 0.60
            ('t1', 500, 'completed', '2026-10-12 09:00', '2026-10-12 09:30', 6000, 0, None, None, 0.002),
            # BYOK: nothing charged -> no cost even with a number recorded
            ('t2', 500, 'completed', '2026-10-12 09:00', '2026-10-12 09:30', 0, 0, 'whisper-1', 0.5, None),
            ('t3', 501, 'completed', '2026-10-12 10:00', '2026-10-12 10:20', 600, 600, 'together', 0.03, 0.001),
            ('t4', 501, 'completed', '2026-10-12 10:00', '2026-10-12 10:20', 600, 0, 'mixed', 0.2, None),
            ('t5', 501, 'completed', '2026-10-10 10:00', '2026-10-10 10:20', 600, 0, 'whisper-1', 0.06, None),
            ('t6', 501, 'running', '2026-10-12 10:00', None, 600, 0, 'whisper-1', 9.0, None),
        ])
    return conn


def _bt(pi, amount, net, created):
    ts = int(datetime.fromisoformat(created).replace(tzinfo=timezone.utc).timestamp())
    return {'id': 'txn_' + pi, 'type': 'charge', 'amount': amount, 'net': net,
            'created': ts, 'source': {'object': 'charge', 'payment_intent': pi}}


def _bts():
    # Settlement currency ~10x USD cents (e.g. NOK).
    return [
        _bt('pi_a1', 1720, 1400, '2026-10-10 10:00:00'),
        _bt('pi_a2', 2820, 1900, '2026-10-12 20:00:00'),
        _bt('pi_b1', 1720, 1400, '2026-10-12 21:30:00'),
        _bt('pi_t1', 9990, 9000, '2026-10-12 11:00:00'),
        {'id': 'txn_fee', 'type': 'stripe_fee', 'amount': -100, 'net': -100,
         'created': 0, 'source': None,
         'description': 'Managed Payments Transaction Fee (2026-10-12)'},
    ]


def test_exclusion_list_matches_lifecycle_emails():
    pytest.importorskip('itsdangerous')
    import lifecycle_emails
    assert ue.DEFAULT_EXCLUDED_USER_IDS == lifecycle_emails.DEFAULT_EXCLUDED_USER_IDS
    assert ue.EXCLUDED_EMAIL_DOMAINS == lifecycle_emails.EXCLUDED_EMAIL_DOMAINS


def test_counted_purchases_excludes_test_and_refunded():
    rows = ue.counted_purchases(_db(), date(2026, 10, 31))
    assert [r['id'] for r in rows] == [1, 2, 3]
    assert rows[2]['day'] == date(2026, 10, 12)  # 21:30Z = 23:30 Oslo


def test_costs_skip_byok_and_estimate_legacy():
    c = ue.variable_costs_by_day(_db(), date(2026, 10, 9), date(2026, 10, 12))
    d = c[date(2026, 10, 12)]
    assert d['stt_est'] == pytest.approx(0.6)
    assert d['stt_openai'] == pytest.approx(0.6)
    assert d['stt_together'] == pytest.approx(0.03)
    assert d['stt_mixed'] == pytest.approx(0.2)
    assert d['stt'] == pytest.approx(0.83)        # BYOK t2 and running t6 excluded
    assert d['llm'] == pytest.approx(0.003)
    assert c[date(2026, 10, 10)]['stt'] == pytest.approx(0.06)


def test_stripe_net_and_prorated_managed_payments_fee():
    conn = _db()
    out = ue.collect_unit_economics(conn, date(2026, 10, 12), bts=_bts())
    # net: a2 1900*0.1 + b1 1400*0.1 = 3.30; fee -100*0.1 * (4540/14530)
    fee = -100 * 0.1 / 100 * (4540 / 14530)
    assert out['Net revenue (USD)'] == pytest.approx(round(19.0 / 10 + 14.0 / 10 + fee, 2))
    assert out['Gross revenue (USD)'] == 4.54
    assert out['Net revenue source'] == 'stripe'
    assert out['Purchases 1d'] == 2
    assert out['Buyers 1d'] == 2
    assert out['New buyers 1d'] == 1               # 500 bought first on the 10th
    assert out['Variable cost (USD)'] == pytest.approx(0.833)
    assert out['Contribution margin (USD)'] == round(out['Net revenue (USD)'] - 0.833, 2)
    # cumulative = day 10 (1.40 - 0.06) + day 12 CM
    expected = (1.40 - 0.06) + (1.90 + 1.40 + fee - 0.833)
    assert out['Cumulative CM (USD)'] == pytest.approx(expected, abs=0.011)


def test_estimate_when_stripe_unavailable():
    out = ue.collect_unit_economics(_db(), date(2026, 10, 10), bts=None)
    assert out['Net revenue source'] == 'estimate'
    assert out['Net revenue (USD)'] == round(1.72 - (0.30 + 1.72 * 0.064), 2)


def test_day_without_purchases():
    out = ue.collect_unit_economics(_db(), date(2026, 10, 11), bts=_bts())
    assert out['Net revenue source'] == 'n/a'
    assert out['Purchases 1d'] == 0 and out['Gross revenue (USD)'] == 0
    assert out['Contribution margin (USD)'] == 0


def test_provider_bucket():
    assert ue.provider_bucket('together') == 'together'
    assert ue.provider_bucket(None) == 'openai'
    assert ue.provider_bucket('whisper-1') == 'openai'
    assert ue.provider_bucket('mixed') == 'mixed'
    assert ue.provider_bucket('scribe') == 'mixed'


def test_fetch_paginates(monkeypatch):
    pages = [
        {'data': [{'id': 'a'}], 'has_more': True},
        {'data': [{'id': 'b'}], 'has_more': False},
    ]
    seen = []

    class R:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps(self.body).encode()

    def opener(req, timeout):
        seen.append(req.full_url)
        return R(pages[len(seen) - 1])

    out = ue.fetch_balance_transactions('sk_test_x', date(2026, 10, 9), opener=opener)
    assert [b['id'] for b in out] == ['a', 'b']
    assert 'starting_after=a' in seen[1]


def test_stripe_key_reads_only_that_line(tmp_path, monkeypatch):
    monkeypatch.delenv('STRIPE_SECRET_KEY', raising=False)
    (tmp_path / '.env').write_text('OTHER=1\nSTRIPE_SECRET_KEY="sk_test_abc"\n')
    assert ue.stripe_key(tmp_path) == 'sk_test_abc'
    assert ue.stripe_key(tmp_path / 'missing') == ''


def test_notion_properties_include_unit_economics():
    spec = importlib.util.spec_from_file_location(
        'notion_daily_metrics_ue', OPS / 'notion-daily-metrics.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    metrics = {k: 0 for k in (
        'Users total', 'Signups 1d', 'Active users 1d', 'Completions 1d',
        'Errors 1d', 'Never started', 'Ever completed', 'Returned 2+ days',
        'Trial minutes used', 'Trial exhausted')}
    metrics['day'] = '2026-10-12'
    metrics.update(ue.collect_unit_economics(_db(), date(2026, 10, 12), bts=_bts()))
    props = mod.notion_properties(metrics, 'cron', datetime.now(timezone.utc))
    for key in ue.NOTION_NUMBER_KEYS:
        assert props[key] == {'number': metrics[key]}
    assert props['Net revenue source'] == {'select': {'name': 'stripe'}}


def test_managed_payments_fee_follows_purchase_oslo_day():
    """A charge at 22:44Z belongs to the next Oslo day -- so does its fee share."""
    conn = _db()
    conn.execute("INSERT INTO credit_purchases VALUES "
                 "(7, 501, '2026-10-12 22:44:00', 172, 172, 0, 'credited', 'pi_late')")
    bts = _bts() + [_bt('pi_late', 1720, 1400, '2026-10-12 22:44:00')]
    total = 1720 + 2820 + 1720 + 9990 + 1720           # all UTC-12 charges
    f = sum([172 / 1720, 282 / 2820, 172 / 1720, 172 / 1720]) / 4
    fee = -100 * f / 100
    d12 = ue.collect_unit_economics(conn, date(2026, 10, 12), bts=bts)
    d13 = ue.collect_unit_economics(conn, date(2026, 10, 13), bts=bts)
    assert d12['Net revenue (USD)'] == round(1.90 + 1.40 + fee * (2820 + 1720) / total, 2)
    assert d13['Net revenue (USD)'] == round(1.40 + fee * 1720 / total, 2)
    # Cumulative is the running sum of the daily rows (no fee leaking across days).
    assert d13['Cumulative CM (USD)'] == pytest.approx(
        d12['Cumulative CM (USD)'] + d13['Contribution margin (USD)'], abs=0.011)
