"""PostHog health checks for the Notion daily metrics job.

Pure evaluation over query result snapshots (unit-tested). HTTP lives in
notion-daily-metrics.py. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo

OSLO = ZoneInfo('Europe/Oslo')
UTC = timezone.utc

# EU Cloud project Podskrift — same as dashboard "Podskrift — aktivering".
DEFAULT_POSTHOG_HOST = 'https://eu.posthog.com'
DEFAULT_POSTHOG_PROJECT_ID = '283916'
POSTHOG_QUERY_TIMEOUT_SEC = 45

# Dashboard filterTestAccounts → test_account_filters: person id NOT IN this cohort.
# Cohort "Internal / Test users" (id 251446). Apply on every Podskrift query.
INTERNAL_COHORT_ID = 251446

DASHBOARD_URL = (
    f'{DEFAULT_POSTHOG_HOST}/project/{DEFAULT_POSTHOG_PROJECT_ID}/dashboard/975121'
)
ACTIVITY_EXPLORE_URL = (
    f'{DEFAULT_POSTHOG_HOST}/project/{DEFAULT_POSTHOG_PROJECT_ID}/activity/explore'
)

# productivitytech.io tracking landed 27.09.2026 (still zero events as of that day).
PT_TRACKING_ADDED_NOTE = 'sporing lagt inn 27.09'

# Baseline = the 14 calendar days immediately before the check window.
HEALTH_BASELINE_DAYS = 14
# Low volume (~2 signups/week): need this many baseline events before "stopped"
# or "sharp drop" can fire, so a quiet day does not alarm.
HEALTH_MIN_BASELINE_COUNT = 3
# Window daily rate / baseline daily rate below this → sharp drop.
HEALTH_SHARP_DROP_RATIO = 0.25
# Failure-rate alerts: need enough starts in both periods, a minimum of failures
# in the window, and a large absolute increase vs baseline.
HEALTH_MIN_STARTS_FOR_RATE = 3
HEALTH_MIN_FAILURES_FOR_RATE_ALERT = 2
HEALTH_FAILURE_RATE_DELTA = 0.40

NOTION_NOTES_MAX_CHARS = 2000

PODSKRIFT_EVENTS = (
    'podcast_searched',
    'user_signed_up',
    'settings_viewed',
    'openai_key_saved',
    'openai_key_validation_failed',
    'transcript_started',
    'transcript_completed',
    'transcript_failed',
    'trial_limit_hit',
    'offer_shown',
    'pricing_viewed',
    'paywall_shown',
    'buy_modal_opened',
    'buy_modal_closed',
    'buy_clicked',
    'checkout_started',
    'checkout_returned',
    'purchase_completed',
    'purchase_failed',
    'purchase_refunded',
    'refund_processed',
    'stripe_webhook_error',
    'paid_minutes_exhausted',
    'minutes_exhausted',
)

PT_HOSTS = ('productivitytech.io', 'www.productivitytech.io')


@dataclass
class HealthSnapshot:
    """Counts from PostHog for one metric day (window + baseline)."""

    window_days: int
    baseline_days: int = HEALTH_BASELINE_DAYS
    pt_events_window: int = 0
    cta_total_window: int = 0
    cta_total_baseline: int = 0
    # (product, location) -> count
    cta_by_key_window: dict[tuple[str, str], int] = field(default_factory=dict)
    cta_by_key_baseline: dict[tuple[str, str], int] = field(default_factory=dict)
    utm_pageviews_window: int = 0
    event_counts_window: dict[str, int] = field(default_factory=dict)
    event_counts_baseline: dict[str, int] = field(default_factory=dict)
    failed_by_reason_window: dict[str, int] = field(default_factory=dict)
    failed_by_reason_baseline: dict[str, int] = field(default_factory=dict)


def health_window_days(metric_day: date) -> int:
    """Inclusive calendar days ending on metric_day.

    Weekday cron (Mon–Fri) measures yesterday, so Friday and Saturday never get
    their own Notion row. Monday's run (metric day = Sunday) uses 3 days so the
    window covers Fri–Sun. Off-schedule Saturday uses 2 (Fri–Sat).
    """
    wd = metric_day.weekday()  # Mon=0 … Sun=6
    if wd == 6:
        return 3
    if wd == 5:
        return 2
    return 1


def window_and_baseline_bounds(
    metric_day: date,
    window_days: int | None = None,
    baseline_days: int = HEALTH_BASELINE_DAYS,
) -> tuple[datetime, datetime, datetime, datetime]:
    """Return (window_start, window_end, baseline_start, baseline_end) in UTC.

    Half-open intervals. Window ends at the start of the Oslo day after metric_day.
    """
    days = health_window_days(metric_day) if window_days is None else window_days
    window_end = datetime(
        metric_day.year, metric_day.month, metric_day.day, tzinfo=OSLO,
    ).astimezone(UTC) + timedelta(days=1)
    window_start = window_end - timedelta(days=days)
    baseline_end = window_start
    baseline_start = baseline_end - timedelta(days=baseline_days)
    return window_start, window_end, baseline_start, baseline_end


def _daily_rate(count: int, days: int) -> float:
    if days <= 0:
        return 0.0
    return count / days


def _pct(n: int, d: int) -> float:
    if d <= 0:
        return 0.0
    return n / d


def _fmt_key(product: str, location: str) -> str:
    return f'{product}/{location}'


def evaluate_health(snap: HealthSnapshot) -> list[str]:
    """Return Norwegian warning lines (no emoji prefix) for anomalies only.

    Caller adds the ⚠️ prefix and joins for Notion Notes. Empty list = healthy
    (omit Notes from the Notion upsert).
    """
    warnings: list[str] = []
    wdays = snap.window_days
    bdays = snap.baseline_days
    pt_silent = snap.pt_events_window == 0

    if pt_silent:
        warnings.append(
            f'productivitytech.io: 0 events siste {wdays} dager '
            f'({PT_TRACKING_ADDED_NOTE}). Sjekk samtykke/proxy /pt-e. '
            f'Se {ACTIVITY_EXPLORE_URL}'
        )
    else:
        # CTA still arriving + distribution vs baseline.
        if (
            snap.cta_total_window == 0
            and snap.cta_total_baseline >= HEALTH_MIN_BASELINE_COUNT
        ):
            warnings.append(
                f'cta_clicked: 0 i vinduet ({wdays}d) mot '
                f'{snap.cta_total_baseline} i baseline ({bdays}d). '
                f'Se {ACTIVITY_EXPLORE_URL}'
            )
        else:
            for key, bcount in sorted(snap.cta_by_key_baseline.items()):
                wcount = snap.cta_by_key_window.get(key, 0)
                if bcount >= HEALTH_MIN_BASELINE_COUNT and wcount == 0:
                    warnings.append(
                        f'cta_clicked {_fmt_key(*key)}: 0 i vinduet ({wdays}d) '
                        f'mot {bcount} i baseline ({bdays}d). '
                        f'Se {ACTIVITY_EXPLORE_URL}'
                    )

        # Referrals to Podskrift with utm_source=productivitytech.
        if snap.utm_pageviews_window == 0:
            warnings.append(
                f'Podskrift-besøk med utm_source=productivitytech: 0 siste '
                f'{wdays} dager mens productivitytech.io har trafikk '
                f'({snap.pt_events_window} events). Sjekk lenker/UTM. '
                f'Se {ACTIVITY_EXPLORE_URL}'
            )

    # Named Podskrift funnel events (internal cohort excluded upstream).
    for event in PODSKRIFT_EVENTS:
        w = snap.event_counts_window.get(event, 0)
        b = snap.event_counts_baseline.get(event, 0)
        if b < HEALTH_MIN_BASELINE_COUNT:
            continue
        if w == 0:
            warnings.append(
                f'Podskrift-event «{event}»: 0 i vinduet ({wdays}d) mot '
                f'{b} i baseline ({bdays}d). Se {DASHBOARD_URL}'
            )
            continue
        w_rate = _daily_rate(w, wdays)
        b_rate = _daily_rate(b, bdays)
        if b_rate > 0 and w_rate < HEALTH_SHARP_DROP_RATIO * b_rate:
            warnings.append(
                f'Podskrift-event «{event}»: kraftig fall — {w} i vinduet '
                f'({wdays}d) mot {b} i baseline ({bdays}d). Se {DASHBOARD_URL}'
            )

    started_w = snap.event_counts_window.get('transcript_started', 0)
    started_b = snap.event_counts_baseline.get('transcript_started', 0)
    failed_w = snap.event_counts_window.get('transcript_failed', 0)
    failed_b = snap.event_counts_baseline.get('transcript_failed', 0)

    if (
        started_w >= HEALTH_MIN_STARTS_FOR_RATE
        and started_b >= HEALTH_MIN_STARTS_FOR_RATE
        and failed_w >= HEALTH_MIN_FAILURES_FOR_RATE_ALERT
    ):
        rate_w = _pct(failed_w, started_w)
        rate_b = _pct(failed_b, started_b)
        if rate_w - rate_b >= HEALTH_FAILURE_RATE_DELTA:
            warnings.append(
                f'Feilrate transcript_failed/started: {rate_w:.0%} i vinduet '
                f'({failed_w}/{started_w}) mot {rate_b:.0%} i baseline '
                f'({failed_b}/{started_b}). Se {DASHBOARD_URL}'
            )

        for reason, rw in sorted(snap.failed_by_reason_window.items()):
            rb = snap.failed_by_reason_baseline.get(reason, 0)
            if rw < HEALTH_MIN_FAILURES_FOR_RATE_ALERT:
                continue
            r_w = _pct(rw, started_w)
            r_b = _pct(rb, started_b)
            if r_w - r_b >= HEALTH_FAILURE_RATE_DELTA:
                warnings.append(
                    f'Feilårsak «{reason}»: {r_w:.0%} i vinduet ({rw}/{started_w}) '
                    f'mot {r_b:.0%} i baseline ({rb}/{started_b}). '
                    f'Se {DASHBOARD_URL}'
                )

    return warnings


def prefix_warning_lines(lines: list[str]) -> list[str]:
    return [f'⚠️ {line}' for line in lines]


def format_notes_content(lines: list[str]) -> str | None:
    """Join prefixed warning lines for Notion Notes, or None if empty.

    Truncates to NOTION_NOTES_MAX_CHARS with a final ellipsis line.
    """
    if not lines:
        return None
    prefixed = prefix_warning_lines(lines)
    text = '\n'.join(prefixed)
    if len(text) <= NOTION_NOTES_MAX_CHARS:
        return text
    ellipsis = '\n…'
    budget = NOTION_NOTES_MAX_CHARS - len(ellipsis)
    clipped = text[:budget]
    # Prefer cutting on a line boundary.
    if '\n' in clipped:
        clipped = clipped.rsplit('\n', 1)[0]
    return clipped + ellipsis


def notes_rich_text_property(content: str) -> dict:
    return {
        'rich_text': [{'type': 'text', 'text': {'content': content}}],
    }


def hogql_dt(dt: datetime) -> str:
    """UTC timestamp literal for HogQL toDateTime(...)."""
    return dt.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def build_health_queries(
    window_start: datetime,
    window_end: datetime,
    baseline_start: datetime,
    baseline_end: datetime,
) -> dict[str, str]:
    """Named HogQL queries. Podskrift ones exclude INTERNAL_COHORT_ID."""
    ws, we = hogql_dt(window_start), hogql_dt(window_end)
    bs, be = hogql_dt(baseline_start), hogql_dt(baseline_end)
    hosts = ', '.join(f"'{h}'" for h in PT_HOSTS)
    events_list = ', '.join(f"'{e}'" for e in PODSKRIFT_EVENTS)
    cohort = INTERNAL_COHORT_ID
    not_internal = f'NOT person_id IN COHORT {cohort}'

    return {
        'pt_cta_utm': f'''
SELECT
  countIf(
    properties.$host IN ({hosts})
    AND timestamp >= toDateTime('{ws}') AND timestamp < toDateTime('{we}')
  ) AS pt_window,
  countIf(
    event = 'cta_clicked'
    AND timestamp >= toDateTime('{ws}') AND timestamp < toDateTime('{we}')
  ) AS cta_window,
  countIf(
    event = 'cta_clicked'
    AND timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{be}')
  ) AS cta_baseline,
  countIf(
    event = '$pageview'
    AND (
      properties.utm_source = 'productivitytech'
      OR properties.$current_url LIKE '%utm_source=productivitytech%'
    )
    AND timestamp >= toDateTime('{ws}') AND timestamp < toDateTime('{we}')
  ) AS utm_window
FROM events
WHERE timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{we}')
'''.strip(),
        'cta_breakdown': f'''
SELECT
  coalesce(nullIf(toString(properties.product), ''), '(ukjent)') AS product,
  coalesce(nullIf(toString(properties.location), ''), '(ukjent)') AS location,
  countIf(timestamp >= toDateTime('{ws}') AND timestamp < toDateTime('{we}')) AS w,
  countIf(timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{be}')) AS b
FROM events
WHERE event = 'cta_clicked'
  AND timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{we}')
GROUP BY product, location
'''.strip(),
        'podskrift_events': f'''
SELECT
  event,
  countIf(timestamp >= toDateTime('{ws}') AND timestamp < toDateTime('{we}')) AS w,
  countIf(timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{be}')) AS b
FROM events
WHERE event IN ({events_list})
  AND timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{we}')
  AND {not_internal}
GROUP BY event
'''.strip(),
        'failed_reasons': f'''
SELECT
  coalesce(nullIf(toString(properties.reason), ''), '(ukjent)') AS reason,
  countIf(timestamp >= toDateTime('{ws}') AND timestamp < toDateTime('{we}')) AS w,
  countIf(timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{be}')) AS b
FROM events
WHERE event = 'transcript_failed'
  AND timestamp >= toDateTime('{bs}') AND timestamp < toDateTime('{we}')
  AND {not_internal}
GROUP BY reason
'''.strip(),
    }


def snapshot_from_query_results(
    *,
    window_days: int,
    baseline_days: int,
    pt_cta_utm_row: Mapping[str, int] | list | tuple,
    cta_breakdown_rows: list,
    podskrift_event_rows: list,
    failed_reason_rows: list,
) -> HealthSnapshot:
    """Build a HealthSnapshot from HogQL result rows (column order from queries)."""

    def _row_dict(row, keys):
        if isinstance(row, Mapping):
            return {k: int(row.get(k) or 0) for k in keys}
        return {k: int(row[i] or 0) for i, k in enumerate(keys)}

    totals = _row_dict(
        pt_cta_utm_row,
        ('pt_window', 'cta_window', 'cta_baseline', 'utm_window'),
    )

    cta_w: dict[tuple[str, str], int] = {}
    cta_b: dict[tuple[str, str], int] = {}
    for row in cta_breakdown_rows:
        if isinstance(row, Mapping):
            product = str(row.get('product') or '(ukjent)')
            location = str(row.get('location') or '(ukjent)')
            cta_w[(product, location)] = int(row.get('w') or 0)
            cta_b[(product, location)] = int(row.get('b') or 0)
        else:
            product = str(row[0] or '(ukjent)')
            location = str(row[1] or '(ukjent)')
            cta_w[(product, location)] = int(row[2] or 0)
            cta_b[(product, location)] = int(row[3] or 0)

    events_w: dict[str, int] = {}
    events_b: dict[str, int] = {}
    for row in podskrift_event_rows:
        if isinstance(row, Mapping):
            ev = str(row.get('event') or '')
            events_w[ev] = int(row.get('w') or 0)
            events_b[ev] = int(row.get('b') or 0)
        else:
            ev = str(row[0] or '')
            events_w[ev] = int(row[1] or 0)
            events_b[ev] = int(row[2] or 0)

    fail_w: dict[str, int] = {}
    fail_b: dict[str, int] = {}
    for row in failed_reason_rows:
        if isinstance(row, Mapping):
            reason = str(row.get('reason') or '(ukjent)')
            fail_w[reason] = int(row.get('w') or 0)
            fail_b[reason] = int(row.get('b') or 0)
        else:
            reason = str(row[0] or '(ukjent)')
            fail_w[reason] = int(row[1] or 0)
            fail_b[reason] = int(row[2] or 0)

    return HealthSnapshot(
        window_days=window_days,
        baseline_days=baseline_days,
        pt_events_window=totals['pt_window'],
        cta_total_window=totals['cta_window'],
        cta_total_baseline=totals['cta_baseline'],
        cta_by_key_window=cta_w,
        cta_by_key_baseline=cta_b,
        utm_pageviews_window=totals['utm_window'],
        event_counts_window=events_w,
        event_counts_baseline=events_b,
        failed_by_reason_window=fail_w,
        failed_by_reason_baseline=fail_b,
    )
