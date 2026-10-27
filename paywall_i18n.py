"""Localized copy for the partial-preview card (the paywall after a free
preview) and an approximate local-currency price next to the USD amount.

Pure functions only (no Flask): app.py picks the locale from the request and
hands the strings to the page. Prices are always charged in USD by Stripe;
the local figure is a display-only approximation.
"""
import math

#: UI locales the card ships in. Anything else falls back to English.
CARD_LOCALES = ('en', 'es', 'de', 'nl', 'fr', 'pt')

_NBSP = '\u00a0'

CARD_COPY = {
    'en': {
        'title': 'Free preview: first {n} minutes of {m} minutes',
        'note_remaining': ('This transcript covers the opening of the episode. '
                           'Pay only for the {missing} minutes that are left '
                           'and get the full transcript.'),
        'note_full': ('This transcript covers the opening of the episode. '
                      'Finish it for {price} (adds {minutes} min).'),
        'note_none': ('This transcript covers the opening of the episode. '
                      'Buy minutes to finish the rest.'),
        'button_remaining': 'Get the remaining {missing} min for {price}',
        'button_full': 'Transcribe this episode ({m} min) for {price}',
        'button_unlock': 'Unlock the full episode',
        'button_finish': 'Transcribe the full episode',
        'byok': 'Add OpenAI key →',
        'local_price': ('≈ {local} · Stripe shows the exact amount in your '
                        'currency at checkout.'),
    },
    'es': {
        'title': 'Vista previa gratis: primeros {n} minutos de {m}',
        'note_remaining': ('Esta transcripción cubre el inicio del episodio. '
                           'Paga solo los {missing} minutos que faltan y '
                           'recibe la transcripción completa.'),
        'note_full': ('Esta transcripción cubre el inicio del episodio. '
                      'Termínalo por {price} (añade {minutes} min).'),
        'note_none': ('Esta transcripción cubre el inicio del episodio. '
                      'Compra minutos para terminar el resto.'),
        'button_remaining': 'Obtén los {missing} min restantes por {price}',
        'button_full': 'Transcribe este episodio ({m} min) por {price}',
        'button_unlock': 'Desbloquear el episodio completo',
        'button_finish': 'Transcribir el episodio completo',
        'byok': 'Añadir clave de OpenAI →',
        'local_price': ('≈ {local} · Stripe te muestra el importe exacto en '
                        'tu moneda al pagar.'),
    },
    'de': {
        'title': 'Kostenlose Vorschau: die ersten {n} von {m} Minuten',
        'note_remaining': ('Dieses Transkript deckt den Anfang der Folge ab. '
                           'Du zahlst nur für die restlichen {missing} Minuten '
                           'und bekommst das komplette Transkript.'),
        'note_full': ('Dieses Transkript deckt den Anfang der Folge ab. '
                      'Den Rest bekommst du für {price} '
                      '(fügt {minutes} Min. hinzu).'),
        'note_none': ('Dieses Transkript deckt den Anfang der Folge ab. '
                      'Kauf Minuten, um den Rest zu transkribieren.'),
        'button_remaining': 'Restliche {missing} Min. für {price}',
        'button_full': 'Diese Folge ({m} Min.) für {price} transkribieren',
        'button_unlock': 'Ganze Folge freischalten',
        'button_finish': 'Ganze Folge transkribieren',
        'byok': 'OpenAI-Schlüssel hinzufügen →',
        'local_price': ('≈ {local} · Stripe zeigt dir an der Kasse den '
                        'genauen Betrag in deiner Währung.'),
    },
    'nl': {
        'title': 'Gratis voorproefje: eerste {n} van {m} minuten',
        'note_remaining': ('Dit transcript dekt het begin van de aflevering. '
                           'Je betaalt alleen voor de resterende {missing} '
                           'minuten en krijgt het volledige transcript.'),
        'note_full': ('Dit transcript dekt het begin van de aflevering. '
                      'Maak het af voor {price} (voegt {minutes} min toe).'),
        'note_none': ('Dit transcript dekt het begin van de aflevering. '
                      'Koop minuten om de rest te transcriberen.'),
        'button_remaining': 'Resterende {missing} min voor {price}',
        'button_full': 'Transcribeer deze aflevering ({m} min) voor {price}',
        'button_unlock': 'Volledige aflevering ontgrendelen',
        'button_finish': 'Volledige aflevering transcriberen',
        'byok': 'OpenAI-sleutel toevoegen →',
        'local_price': ('≈ {local} · Stripe toont bij het afrekenen het '
                        'exacte bedrag in jouw valuta.'),
    },
    'fr': {
        'title': f'Aperçu gratuit{_NBSP}: les {{n}} premières minutes sur {{m}}',
        'note_remaining': ('Cette transcription couvre le début de l’épisode. '
                           'Ne payez que les {missing} minutes restantes et '
                           'obtenez la transcription complète.'),
        'note_full': ('Cette transcription couvre le début de l’épisode. '
                      'Terminez-le pour {price} (ajoute {minutes} min).'),
        'note_none': ('Cette transcription couvre le début de l’épisode. '
                      'Achetez des minutes pour transcrire la suite.'),
        'button_remaining': 'Obtenir les {missing} min restantes pour {price}',
        'button_full': 'Transcrire cet épisode ({m} min) pour {price}',
        'button_unlock': 'Débloquer l’épisode complet',
        'button_finish': 'Transcrire l’épisode complet',
        'byok': 'Ajouter une clé OpenAI →',
        'local_price': ('≈ {local} · Stripe affiche le montant exact dans '
                        'votre devise au moment du paiement.'),
    },
    'pt': {
        'title': 'Prévia grátis: primeiros {n} de {m} minutos',
        'note_remaining': ('Esta transcrição cobre o início do episódio. '
                           'Pague só pelos {missing} minutos que faltam e '
                           'receba a transcrição completa.'),
        'note_full': ('Esta transcrição cobre o início do episódio. '
                      'Termine por {price} (adiciona {minutes} min).'),
        'note_none': ('Esta transcrição cobre o início do episódio. '
                      'Compre minutos para transcrever o resto.'),
        'button_remaining': 'Obter os {missing} min restantes por {price}',
        'button_full': 'Transcrever este episódio ({m} min) por {price}',
        'button_unlock': 'Desbloquear o episódio completo',
        'button_finish': 'Transcrever o episódio completo',
        'byok': 'Adicionar chave da OpenAI →',
        'local_price': ('≈ {local} · O Stripe mostra o valor exato na sua '
                        'moeda no pagamento.'),
    },
}

#: Display-only USD → local rates (ECB reference rates via frankfurter.app,
#: 2026-10-26; COP/CLP/PEN/TWD from open.er-api.com the same day). Refresh
#: by hand now and then; the card always says "≈" and Stripe shows the
#: exact converted amount at checkout.
USD_RATES_DATE = '2026-10-26'
USD_RATES = {
    'EUR': 0.8938, 'GBP': 0.7492, 'CHF': 0.8262, 'SEK': 9.956, 'NOK': 9.344,
    'DKK': 6.681, 'PLN': 3.936, 'CZK': 21.78, 'HUF': 328.4, 'RON': 4.780,
    'BRL': 5.588, 'MXN': 18.62, 'CAD': 1.403, 'AUD': 1.424, 'NZD': 1.760,
    'JPY': 157.5, 'INR': 95.18, 'TRY': 49.76, 'ZAR': 16.63, 'COP': 3155.0,
    'CLP': 995.8, 'PEN': 3.440, 'TWD': 32.59,
}
_ZERO_DECIMAL = frozenset({'JPY', 'HUF', 'COP', 'CLP'})

_EURO_COUNTRIES = frozenset({
    'AT', 'BE', 'BG', 'HR', 'CY', 'EE', 'FI', 'FR', 'DE', 'GR', 'IE', 'IT',
    'LV', 'LT', 'LU', 'MT', 'NL', 'PT', 'SK', 'SI', 'ES',
})
_COUNTRY_CURRENCY = {
    'GB': 'GBP', 'CH': 'CHF', 'LI': 'CHF', 'SE': 'SEK', 'NO': 'NOK',
    'DK': 'DKK', 'PL': 'PLN', 'CZ': 'CZK', 'HU': 'HUF', 'RO': 'RON',
    'BR': 'BRL', 'MX': 'MXN', 'CA': 'CAD', 'AU': 'AUD', 'NZ': 'NZD',
    'JP': 'JPY', 'IN': 'INR', 'TR': 'TRY', 'ZA': 'ZAR', 'CO': 'COP',
    'CL': 'CLP', 'PE': 'PEN', 'TW': 'TWD',
}
#: Region-less browser languages that almost always mean euro. Spanish and
#: Portuguese without a region are ambiguous (Spain vs Latin America, Portugal
#: vs Brazil), so they get no local figure rather than a wrong one.
_LANG_DEFAULT_CURRENCY = {'de': 'EUR', 'nl': 'EUR', 'fr': 'EUR'}

_SYMBOLS = {
    'EUR': '€', 'GBP': '£', 'CHF': 'CHF', 'SEK': 'kr', 'NOK': 'kr',
    'DKK': 'kr', 'PLN': 'zł', 'CZK': 'Kč', 'HUF': 'Ft', 'RON': 'lei',
    'BRL': 'R$', 'MXN': 'MX$', 'CAD': 'CA$', 'AUD': 'A$', 'NZD': 'NZ$',
    'JPY': '¥', 'INR': '₹', 'TRY': '₺', 'ZAR': 'R', 'COP': 'COL$',
    'CLP': 'CLP$', 'PEN': 'S/', 'TWD': 'NT$',
}
#: Written after the number in every locale (Nordic/Central European style).
_SUFFIX_SYMBOL = frozenset({'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'HUF', 'RON'})


def card_locale(accept_languages):
    """Best card locale for a list of (lang, quality) pairs, best first."""
    for lang, _q in accept_languages or ():
        primary = (lang or '').split('-')[0].split('_')[0].strip().lower()
        if primary in CARD_LOCALES:
            return primary
        if primary == '*':
            break
    return 'en'


def currency_for(country, locale):
    """Local currency to approximate, or None (USD, unknown, ambiguous)."""
    c = (country or '').strip().upper()
    if c:
        if c in _EURO_COUNTRIES:
            return 'EUR'
        cur = _COUNTRY_CURRENCY.get(c)
        if cur:
            return cur
        return None
    return _LANG_DEFAULT_CURRENCY.get(locale)


def _number(value, decimals, locale):
    text = f'{value:,.{decimals}f}'
    if locale == 'en':
        return text
    # 1,234.56 → 1.234,56 (es/de/nl/pt) or 1 234,56 (fr)
    thousands = _NBSP if locale == 'fr' else '.'
    return text.replace(',', '\0').replace('.', ',').replace('\0', thousands)


def format_money(amount, currency, locale):
    """Locale-shaped amount, e.g. €1.33 (en), 1,33 € (de), € 1,33 (nl)."""
    decimals = 0 if currency in _ZERO_DECIMAL else 2
    num = _number(amount, decimals, locale)
    sym = _SYMBOLS.get(currency, currency)
    if currency in _SUFFIX_SYMBOL:
        return f'{num}{_NBSP}{sym}'
    if currency == 'USD':
        sym = '$'
    if locale == 'en':
        sep = _NBSP if sym.isalpha() else ''
        return f'{sym}{sep}{num}'
    if locale == 'nl' or (locale == 'pt' and currency == 'BRL'):
        return f'{sym}{_NBSP}{num}'
    return f'{num}{_NBSP}{sym}'


def format_usd(cents, locale):
    """USD in the card's locale: $1.49 (en), 1,49 $ (de/es/fr/pt), $ 1,49 (nl).
    Whole dollars keep no decimals, like pricing_volume.format_usd."""
    cents = int(cents)
    if cents % 100 == 0:
        whole = cents // 100
        num = str(whole)
        if locale == 'en':
            return f'${num}'
        if locale == 'nl':
            return f'${_NBSP}{num}'
        return f'{num}{_NBSP}$'
    return format_money(cents / 100.0, 'USD', locale)


def local_price(cents, country, locale):
    """Approximate local price string for *cents* USD, or None."""
    cur = currency_for(country, locale)
    rate = USD_RATES.get(cur) if cur else None
    if not rate:
        return None
    value = cents / 100.0 * rate
    if cur in _ZERO_DECIMAL:
        value = math.ceil(value)
    return format_money(value, cur, locale)


def partial_card(locale, *, partial_minutes, episode_minutes, offer=None,
                 country=None, buy_available=True):
    """All strings the partial-preview card shows, in *locale*.

    *offer* is app.partial_finish_offer(): its variant decides between the
    remaining-minutes copy and the regular full-episode copy.
    """
    if locale not in CARD_COPY:
        locale = 'en'
    t = CARD_COPY[locale]
    n, m = int(partial_minutes), int(episode_minutes)
    out = {
        'locale': locale,
        'title': t['title'].format(n=n, m=m),
        'button_finish': t['button_finish'],
        'button_unlock': t['button_unlock'],
        'byok': t['byok'],
        'local_price_line': '',
        'note': t['note_none'],
        'button_buy': t['button_unlock'],
    }
    if not buy_available:
        return out
    if offer:
        price = format_usd(offer['amount_cents'], locale)
        if offer.get('pricing_version'):
            missing = int(offer.get('missing_minutes') or 0)
            out['note'] = t['note_remaining'].format(missing=missing)
            out['button_buy'] = t['button_remaining'].format(
                missing=missing, price=price)
        else:
            out['note'] = t['note_full'].format(
                price=price, minutes=int(offer['minutes']))
            out['button_buy'] = t['button_full'].format(m=m, price=price)
        local = local_price(offer['amount_cents'], country, locale)
        if local:
            out['local_price_line'] = t['local_price'].format(local=local)
            out['local_price'] = local
    return out
