"""Launch volume pricing: buy any number of minutes, cheaper per minute in bulk.

Pure functions only (no Flask, no DB) so the curve can be unit-tested and the
same table can be handed to the browser. The server is the only authority on
price: the client sends a minute count, never an amount.

Curve: piecewise-linear in *total* price between the anchor points below,
rounded up to the cent. Every segment has a positive intercept, so the price
per minute falls monotonically as the quantity rises (tested on the full
purchase grid). Below the first anchor the price is the $1 minimum.
"""
import math
import os

#: Purchase grid: minutes are bought in steps of this size.
VOLUME_STEP_MINUTES = 15
VOLUME_MIN_MINUTES = 60
VOLUME_MAX_MINUTES = 1200
#: Stripe takes a fixed fee per charge, so nothing sells for less than this.
VOLUME_MIN_AMOUNT_CENTS = 100
VOLUME_CURRENCY = 'usd'
#: Metadata marker on Checkout Sessions created by the volume path.
VOLUME_PACK_SKU = 'volume_v1'
#: Quantity the slider opens on (and legacy one-click Buy buttons purchase).
VOLUME_DEFAULT_MINUTES = 300
#: Allowed price_source values on checkout / purchase analytics.
PRICE_SOURCES = ('slider', 'episode', 'default')

#: (minutes, total price in cents). Approved by Sindre 2026-10-11; 1200 min
#: is $11 (not $10) so the top pack stays positive after 25% VAT + Stripe fees.
VOLUME_ANCHORS = (
    (60, 100),
    (300, 400),
    (600, 600),
    (1200, 1100),
)

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


def _curve_cents(minutes):
    if minutes <= VOLUME_ANCHORS[0][0]:
        return VOLUME_ANCHORS[0][1]
    for (m0, c0), (m1, c1) in zip(VOLUME_ANCHORS, VOLUME_ANCHORS[1:]):
        if minutes <= m1:
            exact = c0 + (c1 - c0) * (minutes - m0) / (m1 - m0)
            return int(math.ceil(exact - 1e-9))
    # Past the last anchor: continue at the last segment's marginal rate.
    (m0, c0), (m1, c1) = VOLUME_ANCHORS[-2], VOLUME_ANCHORS[-1]
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
    """Total price in cents for *minutes* on the curve (min $1, never < floor)."""
    minutes = int(minutes)
    return max(VOLUME_MIN_AMOUNT_CENTS, _curve_cents(minutes), floor_cents(minutes))


def cents_per_minute(minutes):
    return price_cents(minutes) / float(minutes)


def savings_percent(minutes):
    """Whole-percent saving per minute versus the smallest purchase."""
    base = cents_per_minute(VOLUME_MIN_MINUTES)
    here = cents_per_minute(minutes)
    return max(0, int(round((1 - here / base) * 100)))


def episode_quantity(episode_minutes, balance_minutes=0):
    """Minutes to buy so an episode fits: the shortfall, rounded up to the grid.

    At least the minimum purchase ($1 buys 60 minutes, so never sell fewer),
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
