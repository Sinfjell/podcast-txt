"""Launch volume pricing: buy any number of minutes, cheaper per minute in bulk.

Pure functions only (no Flask, no DB) so the curve can be unit-tested and the
same table can be handed to the browser. The server is the only authority on
price: the client sends a minute count, never an amount.

Curve: piecewise-linear in *total* price between the anchor points below,
rounded up to the cent. Every segment has a positive intercept, so the price
per minute falls monotonically as the quantity rises (tested on the full
purchase grid). Below the first anchor the price is the first anchor's
(the minimum purchase).
"""
import math
import os

#: Purchase grid: minutes are bought in steps of this size.
VOLUME_STEP_MINUTES = 15
VOLUME_MIN_MINUTES = 60
VOLUME_MAX_MINUTES = 1200
VOLUME_CURRENCY = 'usd'
#: Metadata marker on Checkout Sessions created by the volume path.
VOLUME_PACK_SKU = 'volume_v1'
#: Quantity the slider opens on (and legacy one-click Buy buttons purchase).
VOLUME_DEFAULT_MINUTES = 300
#: Allowed price_source values on checkout / purchase analytics.
PRICE_SOURCES = ('slider', 'episode', 'default')

#: Versioned price curves. A Checkout Session records the version it was
#: priced with (metadata pricing_version), so changing the curve later never
#: invalidates a session that is already open: validation uses the version
#: the session names. Add a new key for a new curve; never edit a released one.
#:
#: vol_v1 (locked by Sindre 2026-10-11): 60 min $1.50, 300 min $5 (the
#: default, same as the legacy fixed pack), 600 min $6, 1200 min $11.
#: $1.50 is also the floor of any purchase, per-episode included.
PRICING_VERSIONS = {
    'vol_v1': {
        'anchors': ((60, 150), (300, 500), (600, 600), (1200, 1100)),
        'min_amount_cents': 150,
    },
}
#: Version new Checkout Sessions are priced with.
CURRENT_PRICING_VERSION = 'vol_v1'
#: Minimum for sessions created before pricing_version existed (staging-only
#: drafts of this PR; the flag was never on in production).
UNVERSIONED_MIN_AMOUNT_CENTS = 100

#: (minutes, total price in cents) of the current version.
VOLUME_ANCHORS = PRICING_VERSIONS[CURRENT_PRICING_VERSION]['anchors']
#: Stripe takes a fixed fee per charge, so nothing sells for less than this.
VOLUME_MIN_AMOUNT_CENTS = PRICING_VERSIONS[CURRENT_PRICING_VERSION]['min_amount_cents']


def min_amount_cents_for(version):
    """Lowest valid amount for a session priced with *version*, or None if
    the version is unknown (reject it)."""
    if version in (None, ''):
        return UNVERSIONED_MIN_AMOUNT_CENTS
    spec = PRICING_VERSIONS.get(str(version))
    return None if spec is None else spec['min_amount_cents']


#: Our marginal cost: OpenAI whisper-1 is $0.006 per audio minute.
COST_CENTS_PER_MINUTE = 0.6
#: Stripe fee model used for the floor: fixed + percentage. 2.9% card +
#: 3.5% Managed Payments (merchant of record), rounded up to be safe.
STRIPE_FEE_FIXED_CENTS = 30
STRIPE_FEE_PCT = 0.064


def volume_pricing_enabled(environ=None):
    """VOLUME_PRICING_ENABLED, off unless explicitly turned on."""
    env = os.environ if environ is None else environ
    raw = (env.get('VOLUME_PRICING_ENABLED') or '0').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def floor_cents(minutes):
    """Lowest price that still covers OpenAI cost plus the Stripe fee."""
    minutes = max(0, int(minutes))
    gross = (minutes * COST_CENTS_PER_MINUTE + STRIPE_FEE_FIXED_CENTS) / (
        1 - STRIPE_FEE_PCT)
    return int(math.ceil(gross - 1e-9))


def _curve_cents(minutes, anchors=None):
    if anchors is None:
        anchors = VOLUME_ANCHORS
    if minutes <= anchors[0][0]:
        return anchors[0][1]
    for (m0, c0), (m1, c1) in zip(anchors, anchors[1:]):
        if minutes <= m1:
            exact = c0 + (c1 - c0) * (minutes - m0) / (m1 - m0)
            return int(math.ceil(exact - 1e-9))
    # Past the last anchor: continue at the last segment's marginal rate.
    (m0, c0), (m1, c1) = anchors[-2], anchors[-1]
    exact = c1 + (c1 - c0) * (minutes - m1) / (m1 - m0)
    return int(math.ceil(exact - 1e-9))


def is_valid_quantity(minutes):
    """True for a purchasable minute count (on the grid, within bounds)."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return False
    return (
        VOLUME_MIN_MINUTES <= minutes <= VOLUME_MAX_MINUTES
        and minutes % VOLUME_STEP_MINUTES == 0
    )


def parse_quantity(raw):
    """Form value → valid minute count, or None. Never rounds a bad value."""
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return minutes if is_valid_quantity(minutes) else None


def price_cents(minutes):
    """Total price in cents for *minutes* on the curve (never below the
    minimum purchase or the cost floor)."""
    minutes = int(minutes)
    return max(VOLUME_MIN_AMOUNT_CENTS, _curve_cents(minutes), floor_cents(minutes))


def price_cents_for(minutes, version):
    """Price of *minutes* on the curve of *version*, or None if the version
    is unknown. The webhook uses this to check a paid session against the
    curve it was quoted on, not against today's curve."""
    spec = PRICING_VERSIONS.get(str(version)) if version else None
    if spec is None:
        return None
    minutes = int(minutes)
    return max(spec['min_amount_cents'], _curve_cents(minutes, spec['anchors']),
               floor_cents(minutes))


def cents_per_minute(minutes):
    return price_cents(minutes) / float(minutes)


def savings_percent(minutes):
    """Whole-percent saving per minute versus the smallest purchase."""
    base = cents_per_minute(VOLUME_MIN_MINUTES)
    here = cents_per_minute(minutes)
    return max(0, int(round((1 - here / base) * 100)))


def episode_quantity(episode_minutes, balance_minutes=0):
    """Minutes to buy so an episode fits: the shortfall, rounded up to the grid.

    At least the minimum purchase (60 minutes, so never sell fewer),
    at most the top of the slider. Returns None when the balance already
    covers the episode or the duration is unknown.
    """
    try:
        need = float(episode_minutes)
    except (TypeError, ValueError):
        return None
    if not (need > 0):
        return None
    try:
        have = max(0.0, float(balance_minutes or 0))
    except (TypeError, ValueError):
        have = 0.0
    shortfall = math.ceil(need - have - 1e-9)
    if shortfall <= 0:
        return None
    step = VOLUME_STEP_MINUTES
    qty = int(math.ceil(shortfall / step) * step)
    return max(VOLUME_MIN_MINUTES, min(VOLUME_MAX_MINUTES, qty))


def format_usd(cents):
    """$1, $1.19, $10 — whole dollars without trailing zeros."""
    cents = int(cents)
    if cents % 100 == 0:
        return f'${cents // 100}'
    return f'${cents / 100:.2f}'


def quote(minutes):
    """Everything the UI and analytics need for one quantity."""
    cents = price_cents(minutes)
    return {
        'minutes': int(minutes),
        'amount_cents': cents,
        'price': format_usd(cents),
        'cents_per_minute': round(cents / float(minutes), 3),
        'savings_percent': savings_percent(minutes),
        'bucket': minutes_bucket(minutes),
    }


def price_table():
    """Every purchasable quantity → quote. Sent to the browser as JSON."""
    return [
        quote(m)
        for m in range(VOLUME_MIN_MINUTES, VOLUME_MAX_MINUTES + 1, VOLUME_STEP_MINUTES)
    ]


def minutes_bucket(minutes):
    """Coarse analytics bucket so slider events don't carry every value."""
    m = int(minutes)
    if m <= 60:
        return '60'
    if m <= 150:
        return '61-150'
    if m <= 300:
        return '151-300'
    if m <= 600:
        return '301-600'
    return '601-1200'


# ---------------------------------------------------------------------------
# Remaining-minutes offer (7-day test, approved by Sindre 2026-10-27)
# ---------------------------------------------------------------------------
#: A partial free preview already covered the opening of the episode. This
#: offer prices the finish on the minutes the preview did NOT cover, while
#: still granting enough minutes to re-run the whole episode (the customer
#: gets the full transcript). Sindre explicitly overrode the $1.50 minimum
#: for this offer only; the cost floor (OpenAI + Stripe fee) still holds.
#: Like a curve version, a released tier table is never edited: a new table
#: gets a new version key.
REMAINING_OFFER_VERSION = 'rem_v1'
#: Analytics value for offer_variant while the test is on.
REMAINING_OFFER_VARIANT = 'remaining_v1'
#: Baseline variant name (price of the full episode on the volume curve).
FULL_EPISODE_OFFER_VARIANT = 'full_episode'
#: (max missing minutes, price in cents). Past the last tier the volume
#: curve (vol_v1) is applied to the missing minutes, rounded up to the grid.
REMAINING_OFFER_TIERS = {
    'rem_v1': ((30, 99), (60, 149), (120, 199)),
}


def remaining_offer_cents(missing_minutes, grant_minutes,
                          version=REMAINING_OFFER_VERSION):
    """Price in cents of finishing an episode with *missing_minutes* left,
    granting *grant_minutes*; None when the inputs or version are invalid.

    Deterministic in (missing, grant, version) so the webhook can recompute
    it from session metadata. Never below the cost floor of what is granted
    and never above the regular price of the same grant.
    """
    tiers = REMAINING_OFFER_TIERS.get(str(version)) if version else None
    if tiers is None:
        return None
    try:
        missing = int(missing_minutes)
        grant = int(grant_minutes)
    except (TypeError, ValueError):
        return None
    if missing <= 0 or not is_valid_quantity(grant):
        return None
    cents = None
    for cap, tier_cents in tiers:
        if missing <= cap:
            cents = tier_cents
            break
    if cents is None:
        step = VOLUME_STEP_MINUTES
        qty = int(math.ceil(missing / step) * step)
        qty = max(VOLUME_MIN_MINUTES, min(VOLUME_MAX_MINUTES, qty))
        cents = price_cents_for(qty, 'vol_v1')
    regular = price_cents_for(grant, 'vol_v1')
    return min(regular, max(cents, floor_cents(grant)))

