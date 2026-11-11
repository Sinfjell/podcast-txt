"""Keyword landing pages (/spotify-podcast-transcript, /no/podcast-til-tekst, …).

One template (templates/landing.html), one definition per page here. Every
price or trial fact is passed in at request time from the same settings the
app enforces, so the copy cannot drift from what checkout and the trial do.

Each page answers the question its URL asks, with facts specific to that
intent (Spotify exclusives, Apple export limits, YouTube vs podcast). No page
is a synonym-swapped copy of another.

``title`` is the <title> stem; the template appends `` — Podskrift`` (12 chars).
Keep stem + suffix ≤ 60 characters.
"""

# (endpoint, path) — full set for sitemap / related links / uniqueness tests.
# landing_summarize is gated by SUMMARY_ENABLED (see public_pages()).
PAGES = (
    ('landing_generator', '/podcast-transcript-generator'),
    ('landing_spotify', '/spotify-podcast-transcript'),
    ('landing_spotify_no_rss', '/spotify-episode-without-rss'),
    ('landing_spotify_no_transcript', '/spotify-transcript-not-available'),
    ('landing_video_podcast', '/video-podcast-transcript'),
    ('landing_apple', '/apple-podcast-transcript'),
    ('landing_podcast_to_text', '/podcast-to-text'),
    ('landing_rss', '/rss-to-text'),
    ('landing_free', '/transcribe-podcast-free'),
    ('landing_youtube_vs', '/youtube-vs-podcast-transcript'),
    ('landing_summarize', '/summarize-podcast-episode'),
    ('landing_de_summarize', '/de/podcast-zusammenfassen'),
    ('landing_es_summarize', '/es/resumir-podcast'),
    ('landing_whole_podcast', '/transcribe-whole-podcast'),
    ('landing_no', '/no/podcast-til-tekst'),
    ('landing_es', '/es/transcribir-podcast'),
    ('landing_de', '/de/podcast-transkribieren'),
    ('landing_pt', '/pt/transcrever-podcast'),
    ('landing_fr', '/fr/transcrire-podcast'),
    ('landing_nl', '/nl/podcast-naar-tekst'),
    ('landing_it', '/it/trascrivere-podcast'),
)

#: Compact footer column.
FOOTER = (
    'landing_generator',
    'landing_spotify',
    'landing_spotify_no_rss',
    'landing_spotify_no_transcript',
    'landing_video_podcast',
    'landing_apple',
    'landing_podcast_to_text',
    'landing_rss',
    'landing_free',
    'landing_youtube_vs',
    'landing_summarize',
    'landing_de_summarize',
    'landing_es_summarize',
    'landing_whole_podcast',
    'landing_no',
    'landing_es',
    'landing_de',
    'landing_pt',
    'landing_fr',
    'landing_nl',
    'landing_it',
)

#: Reciprocal hreflang group for the podcast-to-text family (x-default = en).
HREFLANG_GROUP = {
    'en': 'landing_podcast_to_text',
    'nb': 'landing_no',
    'es': 'landing_es',
    'de': 'landing_de',
    'pt': 'landing_pt',
    'fr': 'landing_fr',
    'nl': 'landing_nl',
    'it': 'landing_it',
}
#: Reciprocal hreflang group for the summarize-episode family (x-default = en).
HREFLANG_SUMMARIZE_GROUP = {
    'en': 'landing_summarize',
    'de': 'landing_de_summarize',
    'es': 'landing_es_summarize',
}
#: All reciprocal hreflang groups (podcast-to-text, summarize, …).
HREFLANG_GROUPS = (HREFLANG_GROUP, HREFLANG_SUMMARIZE_GROUP)
#: Kept for callers/tests that still look up the old Norwegian↔English pair.
HREFLANG_PAIRS = {
    'landing_no': 'landing_podcast_to_text',
}

#: Endpoints that 404 / stay out of sitemap·footer·llms when their flag is off.
_SUMMARY_GATED = frozenset({
    'landing_summarize',
    'landing_de_summarize',
    'landing_es_summarize',
})

SHORT_LABELS = {
    'landing_generator': 'Podcast transcript generator',
    'landing_spotify': 'Transcribe a Spotify podcast',
    'landing_spotify_no_rss': 'Spotify episode without RSS',
    'landing_spotify_no_transcript': 'Spotify transcript not available',
    'landing_video_podcast': 'Video podcast transcript',
    'landing_apple': 'Transcribe from Apple Podcasts',
    'landing_podcast_to_text': 'Podcast to text',
    'landing_rss': 'RSS feed to text',
    'landing_free': 'Transcribe a podcast free',
    'landing_youtube_vs': 'YouTube vs podcast transcript',
    'landing_summarize': 'Summarize a podcast episode',
    'landing_de_summarize': 'Podcast zusammenfassen (Deutsch)',
    'landing_es_summarize': 'Resumir un podcast (español)',
    'landing_whole_podcast': 'Transcribe a whole podcast',
    'landing_no': 'Podcast til tekst (norsk)',
    'landing_es': 'Transcribir podcast (español)',
    'landing_de': 'Podcast transkribieren (Deutsch)',
    'landing_pt': 'Transcrever podcast (português)',
    'landing_fr': 'Transcrire un podcast (français)',
    'landing_nl': 'Podcast naar tekst (Nederlands)',
    'landing_it': 'Trascrivere un podcast (italiano)',
}

#: English labels for non-English landings in llms.txt / llms-full.txt.
LANG_LABELS_EN = {
    'nb': 'Norwegian',
    'no': 'Norwegian',
    'es': 'Spanish',
    'de': 'German',
    'pt': 'Portuguese',
    'fr': 'French',
    'nl': 'Dutch',
    'it': 'Italian',
}

#: Last copy review of /spotify-podcast-transcript (ISO date, Europe/Oslo).
SPOTIFY_PAGE_UPDATED = '2026-11-04'
#: Shared dateModified for the Nov 2026 intent landings (Europe/Oslo).
INTENT_PAGES_UPDATED = '2026-11-04'

#: Endpoints that publish an explicit ``dateModified`` / ``page.updated``.
#: Used by the sitemap ``<lastmod>`` without building the full page copy.
DECLARED_UPDATED = {
    'landing_spotify': SPOTIFY_PAGE_UPDATED,
    'landing_spotify_no_rss': INTENT_PAGES_UPDATED,
    'landing_spotify_no_transcript': INTENT_PAGES_UPDATED,
    'landing_video_podcast': INTENT_PAGES_UPDATED,
}


def declared_updated(endpoint):
    """ISO date this landing already declares as modified, or None."""
    return DECLARED_UPDATED.get(endpoint)

TITLE_SUFFIX = ' — Podskrift'
TITLE_MAX = 60
META_MAX = 160

_ENDPOINT_LANG = {
    ep: lang
    for group in HREFLANG_GROUPS
    for lang, ep in group.items()
}


def lang_of(endpoint):
    """BCP 47 lang for *endpoint* (defaults to English for ungrouped pages)."""
    return _ENDPOINT_LANG.get(endpoint, 'en')


def summary_landing_active():
    """True when /summarize-podcast-episode may be served and listed."""
    from summary import summary_enabled
    return summary_enabled()


def page_is_public(endpoint):
    """False for feature-gated landings whose flag is currently off."""
    if endpoint in _SUMMARY_GATED:
        return summary_landing_active()
    return True


def public_pages():
    """PAGES entries that are currently routable (feature-flag aware)."""
    return tuple((ep, path) for ep, path in PAGES if page_is_public(ep))


def public_footer():
    """FOOTER endpoints that are currently listed."""
    return tuple(ep for ep in FOOTER if page_is_public(ep))


def hreflang_alternates(endpoint):
    """[(hreflang, endpoint)] for *endpoint*; full reciprocal set for its group.

    Gated group members (SUMMARY_ENABLED off) are omitted. Ungrouped endpoints
    return an empty list.
    """
    for group in HREFLANG_GROUPS:
        if endpoint not in group.values():
            continue
        if not page_is_public(endpoint):
            return []
        out = [(lang, ep) for lang, ep in group.items() if page_is_public(ep)]
        x_default = group['en']
        if page_is_public(x_default):
            out.append(('x-default', x_default))
        return out
    return []


def related_links(endpoint):
    """Other landings first; native pages prefer Spotify/Apple/English siblings."""
    core = [
        'landing_generator', 'landing_spotify', 'landing_spotify_no_rss',
        'landing_spotify_no_transcript', 'landing_video_podcast',
        'landing_apple', 'landing_podcast_to_text', 'landing_rss',
        'landing_free', 'landing_youtube_vs', 'landing_whole_podcast',
    ]
    native = ('landing_no', 'landing_es', 'landing_de', 'landing_pt', 'landing_fr',
              'landing_nl', 'landing_it')
    if endpoint == 'landing_summarize':
        out = ['landing_spotify', 'landing_podcast_to_text', 'landing_generator',
               'landing_free', 'landing_apple']
    elif endpoint == 'landing_de_summarize':
        out = ['landing_de', 'landing_summarize', 'landing_spotify',
               'landing_podcast_to_text', 'landing_generator', 'landing_free',
               'landing_apple']
    elif endpoint == 'landing_es_summarize':
        out = ['landing_es', 'landing_summarize', 'landing_spotify',
               'landing_podcast_to_text', 'landing_generator', 'landing_free',
               'landing_apple']
    elif endpoint == 'landing_whole_podcast':
        # Brief: RSS, podcast-to-text, Spotify (/ai is always appended by the template).
        out = ['landing_rss', 'landing_podcast_to_text', 'landing_spotify']
    elif endpoint == 'landing_spotify':
        out = ['landing_spotify_no_rss', 'landing_spotify_no_transcript',
               'landing_video_podcast', 'landing_apple', 'landing_rss',
               'landing_generator', 'landing_podcast_to_text', 'landing_free',
               'landing_youtube_vs', 'landing_whole_podcast'] + list(native)
    elif endpoint == 'landing_spotify_no_rss':
        out = ['landing_spotify', 'landing_rss', 'landing_apple',
               'landing_spotify_no_transcript', 'landing_video_podcast',
               'landing_generator', 'landing_free', 'landing_youtube_vs']
    elif endpoint == 'landing_spotify_no_transcript':
        out = ['landing_spotify', 'landing_spotify_no_rss', 'landing_apple',
               'landing_generator', 'landing_free', 'landing_video_podcast',
               'landing_youtube_vs']
    elif endpoint == 'landing_video_podcast':
        out = ['landing_youtube_vs', 'landing_spotify', 'landing_spotify_no_rss',
               'landing_rss', 'landing_apple', 'landing_generator',
               'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_youtube_vs':
        out = ['landing_video_podcast', 'landing_spotify', 'landing_spotify_no_rss',
               'landing_rss', 'landing_apple', 'landing_generator',
               'landing_podcast_to_text', 'landing_free',
               'landing_whole_podcast'] + list(native)
    elif endpoint == 'landing_no':
        out = ['landing_podcast_to_text', 'landing_spotify', 'landing_apple',
               'landing_rss', 'landing_free', 'landing_generator',
               'landing_youtube_vs']
    elif endpoint in ('landing_es', 'landing_de', 'landing_pt', 'landing_fr',
                      'landing_nl', 'landing_it'):
        out = ['landing_spotify', 'landing_apple', 'landing_podcast_to_text',
               'landing_rss', 'landing_free']
    else:
        out = [e for e in core if e != endpoint] + list(native)
    seen, ordered = set(), []
    for e in out:
        if e not in seen and e != endpoint and page_is_public(e):
            seen.add(e)
            ordered.append(e)
    labels = RELATED_LABELS.get(lang_of(endpoint), {})
    return [(e, labels.get(e, SHORT_LABELS[e])) for e in ordered]


#: "More ways" labels on the native-language landings. The targets are English
#: pages, so each label says so in the reader's language.
RELATED_LABELS = {
    'nb': {
        'landing_podcast_to_text': 'Podcast til tekst (engelsk)',
        'landing_spotify': 'Transkriber en Spotify-podcast (engelsk)',
        'landing_spotify_no_rss': 'Spotify-episode uten RSS (engelsk)',
        'landing_spotify_no_transcript': 'Spotify-transkript mangler (engelsk)',
        'landing_video_podcast': 'Video-podcast til tekst (engelsk)',
        'landing_apple': 'Transkriber fra Apple Podcasts (engelsk)',
        'landing_rss': 'RSS-feed til tekst (engelsk)',
        'landing_free': 'Transkriber en podcast gratis (engelsk)',
        'landing_generator': 'Generator for podcasttranskripsjon (engelsk)',
        'landing_youtube_vs': 'YouTube eller podcast-transkripsjon (engelsk)',
    },
    'es': {
        'landing_spotify': 'Transcribir un podcast de Spotify (en inglés)',
        'landing_spotify_no_rss': 'Episodio de Spotify sin RSS (en inglés)',
        'landing_spotify_no_transcript': 'Transcripción de Spotify no disponible (en inglés)',
        'landing_video_podcast': 'Transcribir un podcast en vídeo (en inglés)',
        'landing_apple': 'Transcribir desde Apple Podcasts (en inglés)',
        'landing_podcast_to_text': 'De podcast a texto (en inglés)',
        'landing_rss': 'De feed RSS a texto (en inglés)',
        'landing_free': 'Transcribir un podcast gratis (en inglés)',
        'landing_summarize': 'Resumir un episodio de podcast (en inglés)',
        'landing_generator': 'Generador de transcripciones de podcast (en inglés)',
    },
    'de': {
        'landing_spotify': 'Spotify-Podcast transkribieren (Englisch)',
        'landing_spotify_no_rss': 'Spotify-Folge ohne RSS (Englisch)',
        'landing_spotify_no_transcript': 'Spotify-Transkript nicht verfügbar (Englisch)',
        'landing_video_podcast': 'Video-Podcast transkribieren (Englisch)',
        'landing_apple': 'Aus Apple Podcasts transkribieren (Englisch)',
        'landing_podcast_to_text': 'Podcast zu Text (Englisch)',
        'landing_rss': 'RSS-Feed zu Text (Englisch)',
        'landing_free': 'Podcast kostenlos transkribieren (Englisch)',
        'landing_summarize': 'Podcast-Folge zusammenfassen (Englisch)',
        'landing_generator': 'Generator für Podcast-Transkripte (Englisch)',
    },
    'pt': {
        'landing_spotify': 'Transcrever um podcast do Spotify (em inglês)',
        'landing_spotify_no_rss': 'Episódio do Spotify sem RSS (em inglês)',
        'landing_spotify_no_transcript': 'Transcrição do Spotify indisponível (em inglês)',
        'landing_video_podcast': 'Transcrever podcast em vídeo (em inglês)',
        'landing_apple': 'Transcrever do Apple Podcasts (em inglês)',
        'landing_podcast_to_text': 'Podcast para texto (em inglês)',
        'landing_rss': 'Feed RSS para texto (em inglês)',
        'landing_free': 'Transcrever um podcast grátis (em inglês)',
    },
    'fr': {
        'landing_spotify': 'Transcrire un podcast Spotify (en anglais)',
        'landing_spotify_no_rss': 'Épisode Spotify sans flux RSS (en anglais)',
        'landing_spotify_no_transcript': 'Transcription Spotify indisponible (en anglais)',
        'landing_video_podcast': 'Transcrire un podcast vidéo (en anglais)',
        'landing_apple': 'Transcrire depuis Apple Podcasts (en anglais)',
        'landing_podcast_to_text': 'Podcast en texte (en anglais)',
        'landing_rss': 'Flux RSS en texte (en anglais)',
        'landing_free': 'Transcrire un podcast gratuitement (en anglais)',
    },
    'nl': {
        'landing_spotify': 'Spotify-podcast transcriberen (Engels)',
        'landing_spotify_no_rss': 'Spotify-aflevering zonder RSS (Engels)',
        'landing_spotify_no_transcript': 'Spotify-transcript niet beschikbaar (Engels)',
        'landing_video_podcast': 'Videopodcast transcriberen (Engels)',
        'landing_apple': 'Transcriberen vanaf Apple Podcasts (Engels)',
        'landing_podcast_to_text': 'Podcast naar tekst (Engels)',
        'landing_rss': 'RSS-feed naar tekst (Engels)',
        'landing_free': 'Podcast gratis transcriberen (Engels)',
    },
    'it': {
        'landing_spotify': 'Trascrivere un podcast Spotify (in inglese)',
        'landing_spotify_no_rss': 'Episodio Spotify senza RSS (in inglese)',
        'landing_spotify_no_transcript': 'Trascrizione Spotify non disponibile (in inglese)',
        'landing_video_podcast': 'Trascrivere un podcast video (in inglese)',
        'landing_apple': 'Trascrivere da Apple Podcasts (in inglese)',
        'landing_podcast_to_text': 'Podcast in testo (in inglese)',
        'landing_rss': 'Feed RSS in testo (in inglese)',
        'landing_free': 'Trascrivere un podcast gratis (in inglese)',
    },
}
RELATED_LABELS['no'] = RELATED_LABELS['nb']


#: Header and footer strings for the native-language landings. base.html falls
#: back to English for any key missing here, and for every other page.
CHROME = {
    'nb': dict(pricing='Priser', help='Hjelp', login='Logg inn', start_free='Start gratis',
               menu='Meny', new_transcript='Ny transkripsjon', history='Historikk',
               settings='Innstillinger', logout='Logg ut', buy='Kjøp minutter',
               tagline='Podcasttranskripsjoner du kan lese, søke i og bruke i KI.',
               product='Produkt', transcribe='Transkriber', support='Støtte', legal='Juridisk',
               podcasts='Podkaster', ai='Bruk i ChatGPT og Claude',
               ai_guide='Guide: KI-transkripsjoner', whats_new='Nyheter',
               contact='Kontakt', feedback='Tilbakemelding / ideer',
               privacy='Personvern', terms='Vilkår', cookies='Informasjonskapsler',
               footer_label='Bunntekst'),
    'es': dict(pricing='Precios', help='Ayuda', login='Iniciar sesión', start_free='Empezar gratis',
               menu='Menú', new_transcript='Nueva transcripción', history='Historial',
               settings='Ajustes', logout='Cerrar sesión', buy='Comprar minutos',
               tagline='Transcripciones de podcasts que puedes leer, buscar y usar con IA.',
               product='Producto', transcribe='Transcribir', support='Soporte', legal='Legal',
               podcasts='Podcasts', ai='Usar en ChatGPT y Claude',
               ai_guide='Guía de transcripciones con IA', whats_new='Novedades',
               contact='Contacto', feedback='Comentarios / ideas',
               privacy='Privacidad', terms='Condiciones', cookies='Configurar cookies',
               footer_label='Pie de página'),
    'de': dict(pricing='Preise', help='Hilfe', login='Anmelden', start_free='Kostenlos starten',
               menu='Menü', new_transcript='Neue Transkription', history='Verlauf',
               settings='Einstellungen', logout='Abmelden', buy='Minuten kaufen',
               tagline='Podcast-Transkripte zum Lesen, Durchsuchen und Nutzen mit KI.',
               product='Produkt', transcribe='Transkribieren', support='Support', legal='Rechtliches',
               podcasts='Podcasts', ai='In ChatGPT und Claude nutzen',
               ai_guide='Leitfaden: KI-Transkripte', whats_new='Neuigkeiten',
               contact='Kontakt', feedback='Feedback / Ideen',
               privacy='Datenschutz', terms='AGB', cookies='Cookie-Einstellungen',
               footer_label='Fußzeile'),
    'pt': dict(pricing='Preços', help='Ajuda', login='Entrar', start_free='Começar grátis',
               menu='Menu', new_transcript='Nova transcrição', history='Histórico',
               settings='Configurações', logout='Sair', buy='Comprar minutos',
               tagline='Transcrições de podcasts para ler, pesquisar e usar com IA.',
               product='Produto', transcribe='Transcrever', support='Suporte', legal='Legal',
               podcasts='Podcasts', ai='Usar no ChatGPT e no Claude',
               ai_guide='Guia de transcrições com IA', whats_new='Novidades',
               contact='Contato', feedback='Feedback / ideias',
               privacy='Privacidade', terms='Termos', cookies='Configurações de cookies',
               footer_label='Rodapé'),
    'fr': dict(pricing='Tarifs', help='Aide', login='Se connecter', start_free='Commencer gratuitement',
               menu='Menu', new_transcript='Nouvelle transcription', history='Historique',
               settings='Paramètres', logout='Se déconnecter', buy='Acheter des minutes',
               tagline='Des transcriptions de podcasts à lire, chercher et utiliser avec l’IA.',
               product='Produit', transcribe='Transcrire', support='Assistance', legal='Mentions légales',
               podcasts='Podcasts', ai='Utiliser dans ChatGPT et Claude',
               ai_guide='Guide des transcriptions IA', whats_new='Nouveautés',
               contact='Contact', feedback='Avis / idées',
               privacy='Confidentialité', terms='Conditions', cookies='Paramètres des cookies',
               footer_label='Pied de page'),
    'nl': dict(pricing='Prijzen', help='Help', login='Inloggen', start_free='Gratis starten',
               menu='Menu', new_transcript='Nieuwe transcriptie', history='Geschiedenis',
               settings='Instellingen', logout='Uitloggen', buy='Minuten kopen',
               tagline='Podcasttranscripties die u kunt lezen, doorzoeken en gebruiken met AI.',
               product='Product', transcribe='Transcriberen', support='Ondersteuning', legal='Juridisch',
               podcasts='Podcasts', ai='Gebruiken in ChatGPT en Claude',
               ai_guide='Gids: AI-transcripties', whats_new='Nieuw',
               contact='Contact', feedback='Feedback / ideeën',
               privacy='Privacy', terms='Voorwaarden', cookies='Cookie-instellingen',
               footer_label='Voettekst'),
    'it': dict(pricing='Prezzi', help='Aiuto', login='Accedi', start_free='Inizia gratis',
               menu='Menu', new_transcript='Nuova trascrizione', history='Cronologia',
               settings='Impostazioni', logout='Esci', buy='Acquista minuti',
               tagline='Trascrizioni di podcast da leggere, cercare e usare con l’IA.',
               product='Prodotto', transcribe='Trascrivere', support='Assistenza', legal='Note legali',
               podcasts='Podcast', ai='Usare in ChatGPT e Claude',
               ai_guide='Guida alle trascrizioni IA', whats_new='Novità',
               contact='Contatti', feedback='Feedback / idee',
               privacy='Privacy', terms='Termini', cookies='Impostazioni cookie',
               footer_label='Piè di pagina'),
}
CHROME['no'] = CHROME['nb']

#: Language-root URLs people type or guess (/no/, /fr …) → that language's landing.
LANGUAGE_ROOT_REDIRECTS = {
    'no': 'landing_no',
    'nb': 'landing_no',
    'fr': 'landing_fr',
    'de': 'landing_de',
    'es': 'landing_es',
    'pt': 'landing_pt',
    'nl': 'landing_nl',
    'it': 'landing_it',
}


#: Languages that write a decimal comma and put the currency sign after the
#: amount ("1,50 $"). Portuguese (Brazil) writes "US$ 1,50".
_COMMA_DECIMAL_LANGS = ('no', 'nb', 'fr', 'de', 'es', 'pt', 'nl', 'it')


def _amount(value, lang='en'):
    """Plain amount: 5 / 1.5 → '1.50' (en) or '1,50' (comma-decimal langs)."""
    cents = int(round(float(value) * 100))
    text = str(cents // 100) if cents % 100 == 0 else f'{cents / 100:.2f}'
    return text.replace('.', ',') if lang in _COMMA_DECIMAL_LANGS else text


def _usd(cents, lang='en'):
    """USD amount in the page's convention: sign first (en), sign after with a
    decimal comma (fr/de/es/nb), or "US$ " first with a decimal comma (pt)."""
    amount = _amount(cents / 100, lang)
    if lang == 'pt':
        return f'US$ {amount}'
    if lang in _COMMA_DECIMAL_LANGS:
        return f'{amount} $'
    return f'${amount}'


def _fmt_minutes(n, lang='en'):
    """1,200 (en) / 1 200 (nb, fr) / 1.200 (es, de, pt, nl, it) for thousands."""
    if n >= 1000:
        if lang in ('no', 'nb', 'fr'):
            sep = ' '
        elif lang in ('es', 'de', 'pt', 'nl', 'it'):
            sep = '.'
        else:
            sep = ','
        return f'{n // 1000}{sep}{n % 1000:03d}'
    return str(n)


def pack_phrase(pack, lang='en'):
    """Price phrase for *pack* (see app._landing_pack). Matches /pricing tone."""
    if pack.get('volume'):
        lo = _usd(pack['min_cents'], lang)
        top_min = _fmt_minutes(pack['max_minutes'], lang)
        top = _usd(pack['max_cents'], lang)
        if lang in ('no', 'nb'):
            return (f'fra {lo} for 60 minutter, billigere per minutt jo mer du kjøper '
                    f'({top_min} min for {top}), eller bare minuttene én episode trenger')
        if lang == 'es':
            return (f'desde {lo} por 60 minutos, más barato por minuto cuanto más compras '
                    f'({top_min} min por {top}), o solo los minutos que dura un episodio')
        if lang == 'de':
            return (f'ab {lo} für 60 Minuten, günstiger pro Minute, je mehr du kaufst '
                    f'({top_min} Min. für {top}), oder nur die Minuten einer Folge')
        if lang == 'pt':
            return (f'a partir de {lo} por 60 minutos, mais barato por minuto quanto mais '
                    f'você compra ({top_min} min por {top}), ou só os minutos que um '
                    f'episódio precisa')
        if lang == 'fr':
            return (f'à partir de {lo} pour 60 minutes, moins cher à la minute plus vous '
                    f'achetez ({top_min} min pour {top}), ou seulement les minutes dont '
                    f'un épisode a besoin')
        if lang == 'nl':
            return (f'vanaf {lo} voor 60 minuten, goedkoper per minuut naarmate u meer koopt '
                    f'({top_min} min voor {top}), of alleen de minuten die één aflevering '
                    f'nodig heeft')
        if lang == 'it':
            return (f'a partire da {lo} per 60 minuti, più conveniente al minuto quanto più '
                    f'ne acquista ({top_min} min per {top}), oppure solo i minuti di cui '
                    f'un episodio ha bisogno')
        return (f'from {lo} for 60 minutes, cheaper per minute the more you buy '
                f'({top_min} min for {top}), or just the minutes one episode needs')
    if lang in ('no', 'nb'):
        return f'{_amount(pack["price"], "nb")} dollar for {pack["minutes"]} minutter'
    if lang == 'es':
        return f'{_amount(pack["price"], lang)} dólares por {pack["minutes"]} minutos'
    if lang == 'de':
        return f'{_amount(pack["price"], "de")} Dollar für {pack["minutes"]} Minuten'
    if lang == 'pt':
        return f'{_amount(pack["price"], lang)} dólares por {pack["minutes"]} minutos'
    if lang == 'fr':
        return f'{_amount(pack["price"], "fr")} dollars pour {pack["minutes"]} minutes'
    if lang == 'nl':
        return f'{_amount(pack["price"], "nl")} dollar voor {pack["minutes"]} minuten'
    if lang == 'it':
        return f'{_amount(pack["price"], "it")} dollari per {pack["minutes"]} minuti'
    return f'${pack["price"]:g} for {pack["minutes"]} minutes'


def _start_button(trial, lang='en', free_trial=False):
    offer = bool(trial) or free_trial
    if lang in ('no', 'nb'):
        return 'Start gratis transkripsjon' if offer else 'Start transkripsjon'
    if lang == 'es':
        return 'Empezar transcripción gratis' if offer else 'Empezar transcripción'
    if lang == 'de':
        return 'Kostenlos transkribieren' if offer else 'Transkription starten'
    if lang == 'pt':
        return 'Começar transcrição grátis' if offer else 'Começar transcrição'
    if lang == 'fr':
        return ('Commencer la transcription gratuite' if offer
                else 'Commencer la transcription')
    if lang == 'nl':
        return 'Start gratis transcriptie' if offer else 'Start transcriptie'
    if lang == 'it':
        return ('Inizia la trascrizione gratuita' if offer
                else 'Inizia la trascrizione')
    return 'Start free transcript' if offer else 'Start transcript'


def _free_bit(trial, free_trial, lang='en'):
    """Short free-offer phrase for meta/title — never invents a minute count."""
    if isinstance(trial, int) and trial > 0:
        return {
            'en': f'{trial} minutes free',
            'nb': f'{trial} gratisminutter',
            'es': f'{trial} minutos gratis',
            'de': f'{trial} Minuten gratis',
            'pt': f'{trial} minutos grátis',
            'fr': f'{trial} minutes gratuites',
            'nl': f'{trial} gratis minuten',
            'it': f'{trial} minuti gratis',
        }.get(lang, f'{trial} minutes free')
    if free_trial or trial:
        return {
            'en': 'free to start',
            'nb': 'gratis å starte',
            'es': 'gratis para empezar',
            'de': 'kostenlos starten',
            'pt': 'grátis para começar',
            'fr': 'gratuit pour commencer',
            'nl': 'gratis om te starten',
            'it': 'gratis per iniziare',
        }.get(lang, 'free to start')
    return {
        'en': 'a free account',
        'nb': 'gratis konto',
        'es': 'cuenta gratuita',
        'de': 'Kostenloses Konto',
        'pt': 'conta gratuita',
        'fr': 'compte gratuit',
        'nl': 'gratis account',
        'it': 'account gratuito',
    }.get(lang, 'a free account')


def _price_en(trial, pack, free_trial=False):
    """One honest sentence on cost, in English."""
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'New accounts get {trial} minutes free, no card needed.')
    elif free_trial:
        parts.append('Start free, no card needed.')
    if pack:
        parts.append(f'After that, minutes are a one-time purchase: '
                     f'{pack_phrase(pack)}. No subscription.')
    parts.append('You can also add your own OpenAI API key and pay OpenAI directly '
                 'instead; that is optional.')
    return ' '.join(parts)


def _spotify_whole_episode_cost(trial, pack, episode_prices, free_trial=False):
    """Spotify FAQ: live per-episode prices, or pack_phrase when volume is off.

    episode_prices maps episode length (minutes) → price string from
    app.episode_offer(...); empty/None means VOLUME_PRICING is off.
    """
    if episode_prices:
        bits = []
        for ep in (90, 120, 180):
            price = episode_prices.get(ep)
            if not price:
                continue
            if isinstance(trial, int) and trial > 0:
                bits.append(f'{ep} min: first {trial} min free, the rest {price}')
            else:
                bits.append(f'{ep} min: {price}')
        if bits:
            return ('What does a whole episode cost?',
                    'Examples — ' + '; '.join(bits) + '. '
                    'One-time, VAT included, minutes never expire.')
    if pack:
        return ('What does a whole episode cost?',
                f'Minutes are a one-time purchase: {pack_phrase(pack)}. '
                'One-time, VAT included, minutes never expire, no subscription.')
    return ('What does a whole episode cost?',
            _price_en(trial, pack, free_trial=free_trial))


def _price_no(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'Nye kontoer får {trial} gratisminutter, uten kort.')
    elif free_trial:
        parts.append('Start gratis, uten kort.')
    if pack:
        parts.append(f'Deretter kjøper du minutter én gang: '
                     f'{pack_phrase(pack, "nb")}. Uten abonnement.')
    parts.append('Du kan også legge inn din egen OpenAI API-nøkkel og betale OpenAI '
                 'direkte. Det er valgfritt.')
    return ' '.join(parts)


def _price_es(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'Las cuentas nuevas reciben {trial} minutos gratis, sin tarjeta.')
    elif free_trial:
        parts.append('Empieza gratis, sin tarjeta.')
    if pack:
        parts.append(f'Después compras minutos con un pago único: '
                     f'{pack_phrase(pack, "es")}. Sin suscripción.')
    parts.append('También puedes añadir tu propia clave de API de OpenAI y pagar a '
                 'OpenAI directamente; es opcional.')
    return ' '.join(parts)


def _price_de(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'Neue Konten erhalten {trial} Minuten gratis, ohne Kreditkarte.')
    elif free_trial:
        parts.append('Kostenlos starten, ohne Kreditkarte.')
    if pack:
        parts.append(f'Danach kaufst du Minuten per Einmalzahlung: '
                     f'{pack_phrase(pack, "de")}. Ohne Abo.')
    parts.append('Du kannst auch deinen eigenen OpenAI-API-Schlüssel hinterlegen und '
                 'OpenAI direkt bezahlen. Das ist optional.')
    return ' '.join(parts)


def _price_pt(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'Novas contas ganham {trial} minutos grátis, sem cartão.')
    elif free_trial:
        parts.append('Comece grátis, sem cartão.')
    if pack:
        parts.append(f'Depois você compra minutos com um pagamento único: '
                     f'{pack_phrase(pack, "pt")}. Sem assinatura.')
    parts.append('Você também pode adicionar sua própria chave de API da OpenAI e '
                 'pagar a OpenAI diretamente; isso é opcional.')
    return ' '.join(parts)


def _price_fr(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'Les nouveaux comptes reçoivent {trial} minutes gratuites, '
                     'sans carte.')
    elif free_trial:
        parts.append('Commencez gratuitement, sans carte.')
    if pack:
        parts.append(f'Ensuite vous achetez des minutes en un seul paiement : '
                     f'{pack_phrase(pack, "fr")}. Sans abonnement.')
    parts.append('Vous pouvez aussi ajouter votre propre clé API OpenAI et payer '
                 'OpenAI directement ; c’est facultatif.')
    return ' '.join(parts)


def _price_nl(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'Nieuwe accounts krijgen {trial} gratis minuten, zonder kaart.')
    elif free_trial:
        parts.append('Start gratis, zonder kaart.')
    if pack:
        parts.append(f'Daarna koopt u minuten in één keer: '
                     f'{pack_phrase(pack, "nl")}. Zonder abonnement.')
    parts.append('U kunt ook uw eigen OpenAI API-sleutel toevoegen en OpenAI '
                 'direct betalen; dat is optioneel.')
    return ' '.join(parts)


def _price_it(trial, pack, free_trial=False):
    parts = []
    if isinstance(trial, int) and trial > 0:
        parts.append(f'I nuovi account ricevono {trial} minuti gratis, senza carta.')
    elif free_trial:
        parts.append('Inizi gratis, senza carta.')
    if pack:
        parts.append(f'Poi acquista i minuti con un pagamento unico: '
                     f'{pack_phrase(pack, "it")}. Senza abbonamento.')
    parts.append('Può anche aggiungere la propria chiave API OpenAI e pagare '
                 'OpenAI direttamente; è facoltativo.')
    return ' '.join(parts)


def _common_en(trial, pack, lang_count, free_trial=False):
    return [
        ('What does it cost?', _price_en(trial, pack, free_trial=free_trial)),
        ('What do I get back?',
         'The full transcript as plain text (.txt) and as SubRip subtitles (.srt) '
         'with timestamps, plus a reading view in the browser. Transcripts are '
         'private to your account unless you create a share link.'),
        ('Which languages work?',
         f'{lang_count} languages, transcribed with modern speech recognition. Auto-detect is '
         'the default; choosing the language yourself helps on short or accented audio.'),
    ]


def build(endpoint, *, trial, pack, lang_count, episode_prices=None,
          free_trial=False):
    """Return the page dict for one landing endpoint.

    trial: advertised new-account free minutes, or None when the trial is off
    or when TRIAL_AB hides the minute count.
    free_trial: True when signup still grants free minutes but the count must
    not be quoted (TRIAL_AB_ENABLED).
    pack: app._landing_pack() dict (volume or legacy) when checkout is on, else None.
    episode_prices: optional {90|120|180: price_str} from app.episode_offer for
        Spotify cost copy; omit or pass None when volume pricing is off.
    """
    free_en = _free_bit(trial, free_trial, 'en')
    common = _common_en(trial, pack, lang_count, free_trial=free_trial)
    start = _start_button(trial, free_trial=free_trial)
    episode_prices = episode_prices or {}

    if endpoint == 'landing_generator':
        return dict(
            lang='en',
            title='Podcast Transcript Generator: Link to Text',
            meta=('Generate a podcast transcript from a Spotify, Apple Podcasts or RSS '
                  f'link — no audio upload. Download .txt and .srt. {free_en.capitalize()}.'),
            h1='Podcast transcript generator',
            lede=('Turn any public podcast episode into text you can search, quote and '
                  'paste into ChatGPT or Claude. Paste a link or search by show name; '
                  'Podskrift fetches the audio and transcribes it with modern speech recognition.'),
            placeholder='Paste a Spotify, Apple or RSS link',
            input_label='Podcast or episode link',
            button=start,
            steps=[
                ('Paste or search',
                 'A Spotify episode link, Apple Podcasts link, RSS feed URL, or the show name.'),
                ('Pick the episode',
                 'You see titles and lengths before anything is charged.'),
                ('Get the transcript',
                 'Plain text for reading and quoting, plus .srt with timestamps.'),
            ],
            faq=[
                ('Is this an AI transcript generator?',
                 'Yes — AI speech recognition turns the audio into text. You do not need to '
                 'upload a file; Podskrift downloads the episode from the show’s public feed.'),
                ('Do I need to upload the audio?',
                 'No. If you only have a local file, host it somewhere with a direct URL '
                 'and paste that instead.'),
                ('Can I generate transcripts inside ChatGPT?',
                 'Yes. Add Podskrift as a custom MCP server, then ask ChatGPT for the '
                 'episode. Setup guides are linked below.'),
                ('Is there speaker labelling (diarisation)?',
                 'Not yet. You get one continuous transcript with timestamps, not named speakers.'),
            ] + common,
        )

    if endpoint == 'landing_spotify':
        if free_trial or (isinstance(trial, int) and trial > 0):
            spotify_meta = ('Spotify podcast transcript you can copy and download as '
                            '.txt and .srt. Free trial minutes when a public feed exists. '
                            'Works even when Spotify shows none.')
        else:
            spotify_meta = ('Spotify podcast transcript you can copy and download as '
                            '.txt and .srt. Works when the show has a public RSS feed, '
                            'even if Spotify shows none.')
        # Illustrative format sample — not from a real show or any user account.
        example_txt = (
            '[Illustrative example — not a real episode]\n'
            'Welcome back. Today we are talking about how teams ship quietly.\n'
            'The interesting part is what happened after the launch.\n'
            'We will get to the numbers in a minute.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Welcome back. Today we are talking about how teams ship quietly.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'The interesting part is what happened after the launch.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial)
        # Drop the generic "What does it cost?" — Spotify has the episode FAQ.
        common_rest = [qa for qa in common if qa[0] != 'What does it cost?']
        free_lede = (
            ' Paste a link and use free trial minutes when the show has a public feed — '
            'no card needed to start.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        # Preferred "(Free Trial)" stem is 61 with the brand suffix — use the
        # brief's shorter action/intent stem so title stays ≤ TITLE_MAX.
        return dict(
            lang='en',
            title='Transcribe a Spotify Podcast to Text',
            meta=spotify_meta,
            h1='Spotify podcast transcript: copy and download as text',
            lede=('Paste an open.spotify.com episode or show link. Podskrift finds the '
                  'same episode in the show’s public RSS feed and turns the audio into '
                  'text you can copy, search in your library, and download as .txt or .srt.'
                  + free_lede),
            near_top={
                'before': 'Need Spotify’s own in-app text first? See ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': "how to find Spotify's own transcript and why it's often missing",
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify episode or show link',
            button=start,
            will_work=('Works when the show also has a public RSS feed — most do. '
                       'Spotify Originals, exclusives and video-only shows don’t.'),
            cta_note=('You see the episode and its price before you pay anything. '
                      'You create a free account after you pick the episode.'),
            # Bump when the page copy is reviewed; shown on the page and as
            # dateModified in the JSON-LD.
            updated=SPOTIFY_PAGE_UPDATED,
            compare_title='Spotify’s transcript vs Podskrift',
            compare_head=('Feature', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Read while listening', 'Yes, when Spotify has one',
                 'Creators can review episode transcripts',
                 'Yes, in your browser after transcription'),
                ('Copy the full text', 'No — listeners cannot copy the full transcript',
                 'Creators can work with their own files',
                 'Yes — copy from the result page'),
                ('Download .txt / .srt', 'No export for listeners',
                 'Eligible creators can download or upload transcript files',
                 'Yes — .txt and .srt'),
                ('Search across your episodes', 'Follow-along in that episode only',
                 'Creator tools for their catalogue',
                 'Yes — search your transcript library'),
                ('When Spotify shows no transcript', 'Nothing to read in the app',
                 'Depends on eligibility and creator settings',
                 'Still works if a public RSS/audio feed exists'),
                ('Speaker labels', 'Not a named-speaker export for listeners',
                 'Creator tooling varies',
                 'Not yet — one continuous transcript with timestamps'),
            ],
            compare_note=('Podskrift does not claim Spotify’s in-app text as its own: it '
                          'transcribes the public audio. No summaries on this page — you can '
                          'paste the text into ChatGPT or Claude yourself.'),
            platform_steps_title='How to copy a Spotify episode link',
            platform_steps=[
                ('iPhone and iPad', [
                    'Open the episode in the Spotify app (the episode page, not the show page).',
                    'Tap ⋯ (More), then Share.',
                    'Tap Copy link.',
                ]),
                ('Android', [
                    'Open the episode in the Spotify app.',
                    'Tap ⋮ or ⋯ (More), then Share.',
                    'Tap Copy link.',
                ]),
                ('Desktop app and web player (open.spotify.com)', [
                    'Open the episode, or find it in the show’s episode list.',
                    'Click ⋯ (More options), then Share.',
                    'Click Copy Episode Link. It starts with open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Menu names follow Spotify’s own help pages; they can move '
                                 'between app versions. A show link works too: you pick the '
                                 'episode from the list.'),
            troubleshoot_title='When a Spotify link doesn’t work',
            troubleshoot_head=('What you see', 'Why', 'What to do'),
            troubleshoot=[
                ('A Spotify exclusive or Original',
                 'The show has no public RSS feed, so there is no audio Podskrift can fetch.',
                 'Search the show name here or check Apple Podcasts. If nothing turns up, '
                 'it can’t be transcribed.'),
                ('A subscriber-only or paid episode',
                 'The audio sits behind a subscription, not in the public feed.',
                 'Not possible. Free episodes of the same show usually work.'),
                ('A video-only episode',
                 'There is no audio version in the show’s public feed.',
                 'It works only if the show also publishes the episode as audio.'),
                ('A short link (spotify.link/…)',
                 'Short share links are not read yet.',
                 'Open the short link in your browser, copy the open.spotify.com/episode/… '
                 'address from the address bar, and paste that.'),
            ],
            example_label=('Illustrative example — sample .txt / .srt format only. '
                           'Not from a real show or any user’s transcript.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Paste it into ChatGPT or Claude to summarise or ask questions, '
                     'or connect Podskrift so they fetch transcripts for you.'),
            steps=[
                ('Copy the Spotify link',
                 'In Spotify, open the episode, tap Share (or ⋯), then Copy link.'),
                ('Paste it here',
                 'Podskrift matches the episode in the show’s public RSS feed and lists it.'),
                ('Read or download',
                 'Transcription usually takes a few minutes. Download .txt or .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Guide: how to get a Spotify podcast transcript'),
            ],
            # related_links() already prioritises the Nov 2026 intent landings.
            faq=[
                ('Does every Spotify podcast work?',
                 'Most do: almost every show on Spotify also publishes a public RSS feed, '
                 'and that is where the audio comes from. Spotify exclusives and '
                 'subscriber-only episodes have no public feed, so they cannot be transcribed.'),
                ('What if the show is Spotify-only?',
                 'Podskrift can\'t transcribe it. Some shows that look Spotify-only also '
                 'publish a public feed. Search the show name here or check Apple Podcasts. '
                 'If nothing turns up, it\'s a true exclusive.'),
                ('Can I paste a show link instead of an episode link?',
                 'Yes. A show link opens the episode list, and you pick the episode there.'),
                ('Is this the same as Spotify’s own transcript?',
                 'No. Podskrift makes its own transcript from the audio, so it also works '
                 'for shows where Spotify shows none, and you can copy and download it.'),
                ('Can I download the transcript Spotify shows in the app?',
                 'No — Spotify doesn’t export the listener transcript. Paste the link here '
                 'to get your own copy as .txt and .srt.'),
                ('Is it free?',
                 'New accounts get free trial minutes when a trial is offered — no card '
                 'needed to start. Longer episodes show the exact price before you pay. '
                 'We do not list a fixed free-minute count here because the grant can vary.'),
                cost_faq,
            ] + common_rest,
        )

    if endpoint == 'landing_spotify_no_rss':
        # Honest about resolve_spotify_url: Spotify links only work when the
        # show can be matched to a public iTunes/RSS feed. True exclusives,
        # paid episodes and video-only shows with no feed cannot be transcribed.
        free_bit = _free_bit(trial, free_trial, 'en')
        return dict(
            lang='en',
            title='Spotify Episode Without an RSS Feed',
            meta=('How to transcribe a Spotify episode when the show has no '
                  'public RSS feed — what Podskrift can and cannot do. '
                  f'{free_bit.capitalize()}.'),
            h1='Transcribe a Spotify episode that has no public RSS',
            lede=('Straight answer: Podskrift can only transcribe a Spotify '
                  'episode when the same show also publishes a public RSS feed '
                  '(most do). True Spotify exclusives, Originals without a '
                  'public feed, subscriber-only episodes, and video-only shows '
                  'with no audio feed cannot be transcribed — there is no audio '
                  'URL Podskrift is allowed to fetch.'),
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify episode or show link',
            button=start,
            updated=INTENT_PAGES_UPDATED,
            will_work=('Works only when a public podcast feed exists for the '
                       'show. Spotify-only exclusives and paid episodes don’t.'),
            cta_note=('Paste the Spotify link first. If no public feed matches, '
                      'you’ll see that before anything is charged.'),
            steps=[
                ('Paste the Spotify link',
                 'An open.spotify.com episode or show link. Podskrift looks up '
                 'the show name in the public podcast directory and tries to '
                 'match the episode in that feed.'),
                ('If a public feed is found',
                 'You see the episode (or the show’s episode list). Pick it and '
                 'start the transcript as usual.'),
                ('If nothing matches',
                 'Search the show name here, or check Apple Podcasts / the '
                 'host’s site for an RSS URL. If the show truly has no public '
                 'feed, Podskrift cannot transcribe it — there is no workaround '
                 'that downloads Spotify’s private audio.'),
            ],
            troubleshoot_title='What works and what doesn’t',
            troubleshoot_head=('Situation', 'Why', 'What to do'),
            troubleshoot=[
                ('Show also on Apple Podcasts or has its own RSS',
                 'A public feed exists; Spotify is just how you found the episode.',
                 'Paste the Spotify link, or the Apple / RSS link — both work.'),
                ('Looks Spotify-only but a feed still exists',
                 'Some shows stay off the big apps yet publish RSS for directories.',
                 'Search the show name here, or paste the feed URL from the host.'),
                ('True Spotify exclusive / Original, no public feed',
                 'There is no public audio URL. Podskrift never scrapes Spotify’s CDN.',
                 'Cannot be transcribed. No workaround.'),
                ('Subscriber-only / paid Spotify episode',
                 'The audio sits behind a Spotify subscription, not in the public feed.',
                 'Not possible. Free episodes of the same show often still work.'),
                ('Spotify video show with no audio feed',
                 'Video-only Spotify shows usually have no public podcast RSS.',
                 'Only works if the same episode is also published as audio elsewhere.'),
            ],
            faq=[
                ('Can Podskrift transcribe Spotify-exclusive podcasts?',
                 'No. It needs a public RSS feed (or the same episode via Apple '
                 'Podcasts / a direct audio URL). Exclusives without a public '
                 'feed have no audio Podskrift can fetch.'),
                ('How does Podskrift use a Spotify link at all?',
                 'It reads the public show/episode metadata, finds a matching '
                 'show in the public podcast directory, then pulls the episode '
                 'audio from that show’s RSS feed — not from Spotify’s private '
                 'stream.'),
                ('I pasted a Spotify link and got “no public RSS feed” — now what?',
                 'Try searching the show name, check Apple Podcasts, or paste an '
                 'RSS URL from the host. If none of those find the show, it is a '
                 'true exclusive and cannot be transcribed here.'),
                ('Can I upload the audio I downloaded from Spotify?',
                 'No file upload. If you have a direct public audio URL (hosted '
                 'somewhere you control), paste that URL. Ripping Spotify’s '
                 'stream is not supported and is against Spotify’s terms.'),
                ('Is this the same as Spotify’s built-in transcript?',
                 'No. Podskrift makes its own transcript from public-feed audio. '
                 'If Spotify’s in-app transcript is missing, see the “Spotify '
                 'transcript not available” page — that still requires a public '
                 'feed.'),
            ] + common,
        )

    if endpoint == 'landing_spotify_no_transcript':
        free_bit = _free_bit(trial, free_trial, 'en')
        return dict(
            lang='en',
            title='Spotify Transcript Not Available',
            meta=('When Spotify has no transcript for an episode: why it '
                  'happens, what you can do, and how to get a downloadable '
                  f'.txt and .srt. {free_bit.capitalize()}.'),
            h1='Spotify transcript not available — get your own',
            lede=('If Spotify’s built-in transcript is missing or greyed out, '
                  'Podskrift can still make one — but only when the episode also '
                  'exists in a public podcast RSS feed. Paste the Spotify link; '
                  'you get plain text and .srt you can copy and download. Spotify '
                  'does not let you export the transcript it shows in the app.'),
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify episode or show link',
            button=start,
            updated=INTENT_PAGES_UPDATED,
            will_work=('Works when the show has a public RSS feed — most do. '
                       'Exclusives, paid episodes and video-only shows without a '
                       'feed don’t.'),
            cta_note=('You see the episode and its price before you pay anything. '
                      'You create a free account after you pick the episode.'),
            steps=[
                ('Copy the Spotify episode link',
                 'In Spotify: open the episode → Share → Copy link '
                 '(open.spotify.com/episode/…).'),
                ('Paste it here',
                 'Podskrift matches the episode in the show’s public RSS feed '
                 'and lists it. If no feed exists, it tells you up front.'),
                ('Download .txt or .srt',
                 'Transcription usually takes a few minutes. Unlike Spotify’s '
                 'in-app view, you can copy, search and keep the files.'),
            ],
            faq=[
                ('Why does Spotify say transcript not available?',
                 'Spotify only generates in-app transcripts for some shows and '
                 'languages, and it can lag behind new episodes. That is a Spotify '
                 'product limit — it does not mean the audio cannot be transcribed '
                 'elsewhere.'),
                ('Can I export Spotify’s own transcript?',
                 'No. Spotify does not offer a .txt or .srt download. Paste the '
                 'episode link here to get your own copy from the public feed.'),
                ('Is Podskrift’s transcript the same as Spotify’s?',
                 'No. Podskrift runs its own speech recognition on the public-feed '
                 'audio. Wording can differ; you get timestamps and downloadable '
                 'files either way.'),
                ('Does every Spotify episode work?',
                 'Most do, when the show also publishes a public RSS feed. '
                 'Spotify exclusives, subscriber-only episodes and video-only '
                 'shows without a feed cannot be transcribed.'),
                ('What if the show has no public RSS?',
                 'Podskrift cannot fetch Spotify’s private audio. Search the show '
                 'name or check Apple Podcasts; if nothing turns up, see the '
                 '“Spotify episode without RSS” page — there is no workaround for '
                 'true exclusives.'),
            ] + common,
            also_related=[
                ('guide_spotify_transcript',
                 'Guide: how to get a Spotify podcast transcript'),
            ],
        )

    if endpoint == 'landing_video_podcast':
        free_bit = _free_bit(trial, free_trial, 'en')
        return dict(
            lang='en',
            title='Transcribe a Video Podcast to Text',
            meta=('How to get a transcript of a video podcast. Works when the '
                  'episode is also in a public audio RSS feed — not for raw '
                  f'YouTube URLs. {free_bit.capitalize()}.'),
            h1='Transcribe a video podcast (when audio is public)',
            lede=('Podskrift transcribes podcast audio from a public feed — not '
                  'YouTube videos and not Spotify’s private video stream. If your '
                  'video podcast is also published as a normal podcast (Apple '
                  'Podcasts, Spotify with a public RSS feed, or a direct audio '
                  'URL), paste that podcast link and get .txt and .srt.'),
            placeholder='Paste a Spotify, Apple or RSS podcast link',
            input_label='Podcast or episode link (not a YouTube URL)',
            button=start,
            updated=INTENT_PAGES_UPDATED,
            will_work=('Works for video podcasts that also ship a public audio '
                       'feed. YouTube URLs and Spotify video-only shows without '
                       'RSS don’t.'),
            steps=[
                ('Prefer the podcast link, not the video link',
                 'Find the same episode on Apple Podcasts, Spotify (with a public '
                 'feed), or the show’s RSS — not youtube.com or youtu.be.'),
                ('Paste and pick the episode',
                 'Podskrift fetches the public-feed audio. You see the title and '
                 'length before anything is charged.'),
                ('Get downloadable text',
                 'Read in the browser, or download .txt and timestamped .srt for '
                 'captions or notes.'),
            ],
            troubleshoot_title='Video podcast sources',
            troubleshoot_head=('Source', 'Supported?', 'What to do'),
            troubleshoot=[
                ('Video podcast also on Apple / RSS / Spotify with a public feed',
                 'Yes — Podskrift uses the public audio enclosure.',
                 'Paste that podcast or feed link.'),
                ('YouTube-only video (youtube.com / youtu.be)',
                 'No. Podskrift does not accept YouTube URLs or download YouTube audio.',
                 'Use YouTube’s captions, or publish/find the episode as a podcast.'),
                ('Spotify video show with no public RSS',
                 'No. There is no public audio URL to fetch.',
                 'Only works if the same episode appears as audio elsewhere.'),
                ('Direct .mp3 / .m4a / .mp4 audio URL you host',
                 'Often yes, when the URL is publicly fetchable.',
                 'Paste the direct URL. Very large video files that cannot be '
                 'streamed may be refused.'),
            ],
            faq=[
                ('Can Podskrift transcribe a YouTube video?',
                 'No. It does not accept YouTube URLs. For YouTube-only content, '
                 'use YouTube’s own captions. If the same episode is also a '
                 'podcast, paste the podcast link instead.'),
                ('What about Spotify video podcasts?',
                 'Only when the show also has a public RSS (or Apple) audio feed. '
                 'Spotify video-only shows without a public feed cannot be '
                 'transcribed — Podskrift never pulls Spotify’s private video.'),
                ('Can I paste an .mp4 link?',
                 'If it is a direct, publicly fetchable media URL, try it. Huge '
                 'video files that need a full download before any audio can be '
                 'read may be refused; an audio-only feed is more reliable.'),
                ('Is this a YouTube caption or downloader tool?',
                 'No. It is a podcast transcription tool. See “YouTube vs podcast '
                 'transcript” for when YouTube captions are enough.'),
                ('Do I get speaker labels?',
                 'Not yet. You get one continuous transcript with timestamps, not '
                 'named speakers.'),
            ] + common,
        )

    if endpoint == 'landing_apple':
        return dict(
            lang='en',
            title='Transcribe a podcast from an Apple Podcasts link',
            meta=('Paste a podcasts.apple.com episode or show link and get a transcript '
                  'you can copy and download as .txt and .srt. Public feeds only. '
                  f'{free_en.capitalize()}.'),
            h1='Transcribe a podcast from an Apple Podcasts link',
            lede=('Apple shows transcripts inside its app for many shows, but you cannot '
                  'export them. Paste the Apple Podcasts link here and get the text as files.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Apple Podcasts episode or show link',
            button=start,
            steps=[
                ('Copy the link',
                 'In Apple Podcasts, open the episode or show, tap Share, then Copy Link.'),
                ('Paste it here',
                 'Podskrift reads the show’s feed from Apple’s directory and finds the episode.'),
                ('Read or download',
                 'Get the transcript in the browser and as .txt or .srt.'),
            ],
            faq=[
                ('Why not just use the transcript in the Apple Podcasts app?',
                 'It stays in the app: you can read it while listening, but not export the '
                 'whole thing, search across episodes, or paste it into another tool. '
                 'Podskrift gives you the text as files.'),
                ('Do subscriber-only (Apple Podcasts Subscriptions) episodes work?',
                 'No. Only episodes in the public feed can be transcribed.'),
                ('Does it work with a show link?',
                 'Yes. A show link opens the episode list so you can pick one.'),
            ] + common,
        )

    if endpoint == 'landing_podcast_to_text':
        return dict(
            lang='en',
            title='Podcast to Text: Transcribe From a Link',
            meta=('Convert a podcast episode to text without downloading the audio. Paste a '
                  'Spotify, Apple Podcasts or RSS link, or search by name. Get .txt and .srt.'),
            h1='Podcast to text, straight from a link',
            lede=('No audio file to find, download or upload. Paste a link or search for the '
                  'show, pick the episode, and Podskrift fetches the audio and transcribes it.'),
            placeholder='Paste a Spotify, Apple or RSS link',
            input_label='Podcast or episode link',
            button=start,
            steps=[
                ('Paste a link or search',
                 'Spotify, Apple Podcasts, an RSS feed or a direct audio URL all work.'),
                ('Pick the episode',
                 'You see the episode list with lengths before anything is charged.'),
                ('Get the text',
                 'Plain text for reading and quoting, .srt with timestamps for subtitles.'),
            ],
            faq=[
                ('Do I need to upload the audio file?',
                 'No. Podskrift downloads the episode from the podcast’s public feed. If you '
                 'only have a file, host it somewhere and paste the direct audio URL.'),
                ('How accurate is it?',
                 'It uses modern AI speech recognition, which is good on clear speech. Crosstalk, heavy '
                 'accents and music beds reduce accuracy, and there is no speaker labelling yet.'),
                ('How long does it take?',
                 'Usually a few minutes for an hour of audio. You can close the page; the job keeps going.'),
                ('Can I use the transcript in ChatGPT or Claude?',
                 'Yes. Connect Podskrift as an MCP connector and ask for summaries or quotes '
                 'from episodes, or paste the text yourself.'),
                ('Which languages work best?',
                 f'{lang_count} languages in the picker. Name the spoken language instead of '
                 'auto-detect for short or accented audio. Norwegian has its own page under '
                 '“More ways to transcribe”.'),
            ] + common,
        )

    if endpoint == 'landing_rss':
        return dict(
            lang='en',
            title='RSS to Text: Transcribe Podcast Feeds',
            meta=('Paste a podcast RSS feed URL, pick an episode and get a transcript. Works for '
                  'shows outside Spotify and Apple, private-label hosts and your own feed.'),
            h1='Transcribe podcast episodes from an RSS feed',
            lede=('Every public podcast has an RSS feed. Paste it here to transcribe shows that '
                  'are hard to find by search: small or local shows, new launches, your own podcast.'),
            placeholder='https://feeds.example.com/show.xml',
            input_label='Podcast RSS feed URL',
            button='Open feed',
            steps=[
                ('Find the feed',
                 'Hosts like Buzzsprout, Libsyn, Podbean and Acast all publish one. See the RSS guide below.'),
                ('Paste the URL',
                 'Podskrift lists the latest episodes with titles, dates and lengths.'),
                ('Transcribe',
                 'Pick an episode and get .txt and timestamped .srt.'),
            ],
            faq=[
                ('Where do I find a podcast’s RSS feed?',
                 'On the host’s website, in the show’s “Subscribe” or “RSS” link, or via its '
                 'Apple Podcasts page. Our RSS guide walks through it.'),
                ('Do private or password-protected feeds work?',
                 'Only if the access token is part of the feed URL itself. Feeds that need a '
                 'separate login do not work.'),
                ('Can I transcribe my own podcast to publish the text?',
                 'Yes. Download the .txt for show notes or the .srt for captions.'),
            ] + common,
        )

    if endpoint == 'landing_free':
        if isinstance(trial, int) and trial > 0:
            free_answer = (f'New accounts get {trial} minutes of transcription free, no card '
                           'needed. A longer first episode still starts: you get the first '
                           f'{trial} minutes as a preview and can buy minutes to finish it.')
            h1 = f'Transcribe a podcast free: {trial} minutes on us'
            title = f'Transcribe a Podcast Free: {trial} Minutes, No Card'
        elif free_trial:
            free_answer = ('New accounts can start free, no card needed. Longer episodes still '
                           'start as a free preview; buy minutes or add your own key to finish.')
            h1 = 'Transcribe a podcast free — no card needed'
            title = 'Transcribe a Podcast Free, No Card'
        else:
            free_answer = ('There is no free trial right now. You can still transcribe at cost '
                           'by adding your own OpenAI API key.')
            h1 = 'Transcribe a podcast for free or at cost'
            title = 'Transcribe a Podcast Free or at Cost'
        return dict(
            lang='en',
            title=title,
            meta=('How to transcribe a podcast episode for free, and what it costs after. '
                  'Paste a Spotify, Apple or RSS link. No subscription.'),
            h1=h1,
            lede=('Straight answer on what is free and what is not, so there are no surprises '
                  'at the end of an episode.'),
            placeholder='Paste a Spotify, Apple or RSS link',
            input_label='Podcast or episode link',
            button='Try it free' if (trial or free_trial) else 'Start transcript',
            steps=[
                ('Paste a link', 'Spotify, Apple Podcasts or RSS. Searching by name works too.'),
                ('Create an account', 'Email and password. No card for the free minutes.'),
                ('Get the transcript', 'Download .txt and .srt; the transcript stays in your history.'),
            ],
            faq=[
                ('Is it really free?', free_answer),
                ('What happens when the free minutes run out?',
                 _price_en(None, pack, free_trial=False)),
                ('Is there a subscription?',
                 'No. Minutes are a one-time purchase; nothing renews.'),
                ('Are there free alternatives?',
                 'Spotify and Apple show transcripts in their apps for many shows, free but not '
                 'exportable. Running Whisper yourself is free if you have the hardware and are '
                 'comfortable with the command line.'),
            ] + common[1:],
        )

    if endpoint == 'landing_youtube_vs':
        return dict(
            lang='en',
            title='YouTube vs Podcast Transcripts',
            meta=('Podskrift transcribes podcasts from Spotify, Apple Podcasts or RSS, not '
                  'YouTube videos. When YouTube captions are enough, and when they are not.'),
            h1='YouTube captions vs a podcast transcript',
            lede=('YouTube already gives most videos auto-captions. Podcasts usually do not '
                  'publish a transcript you can download. Podskrift is built for that podcast '
                  'gap — it is not a YouTube downloader or YouTube caption tool.'),
            placeholder='Paste a Spotify, Apple or RSS podcast link',
            input_label='Podcast or episode link (not a YouTube URL)',
            button=start,
            steps=[
                ('Use a podcast link',
                 'Spotify, Apple Podcasts, or the show’s RSS feed — not a youtube.com URL.'),
                ('If the show is also on YouTube',
                 'Find the same episode in the podcast feed (Apple / Spotify / RSS) and paste that.'),
                ('Get downloadable text',
                 'Unlike most YouTube caption UIs, you get .txt and .srt you can keep.'),
            ],
            faq=[
                ('Can Podskrift transcribe a YouTube video?',
                 'No. It does not accept YouTube URLs or download YouTube audio. Use YouTube’s '
                 'own captions for video-only content.'),
                ('My podcast is uploaded to YouTube as video — what then?',
                 'If the same episode is published as a podcast (Apple Podcasts, Spotify, or '
                 'an RSS feed), paste that podcast link. Podskrift uses the public podcast '
                 'audio, not the YouTube file.'),
                ('Why not just copy YouTube captions?',
                 'You can, for YouTube. Podcasts often have no captions at all, and in-app '
                 'podcast transcripts (Spotify / Apple) usually cannot be exported as files.'),
                ('What does Podskrift actually support?',
                 'Public podcast audio via Spotify links (when a public feed exists), Apple '
                 'Podcasts links, RSS feeds, catalogue search, and direct audio URLs.'),
            ] + common,
        )

    if endpoint == 'landing_no':
        free_no = _free_bit(trial, free_trial, 'nb')
        faq = [
            ('Hva koster det?', _price_no(trial, pack, free_trial=free_trial)),
            ('Hvor godt fungerer det på norsk?',
             'Godt på tydelig tale. Sterke dialekter, folk som snakker i munnen på '
             'hverandre og musikk under praten gir flere feil. Velg «Norsk» som språk i '
             'stedet for automatisk gjenkjenning, så blir resultatet bedre.'),
            ('Må jeg laste opp lydfilen?',
             'Nei. Lim inn en lenke fra Spotify eller Apple Podcasts, eller en RSS-adresse, '
             'så henter Podskrift lyden fra podkastens offentlige feed.'),
            ('Fungerer NRK-podkaster?',
             'Ja, så lenge episoden ligger i en offentlig RSS-feed eller på Apple Podcasts. '
             'Episoder som bare finnes i NRK-appen, eller eksklusivt på én plattform, kan '
             'ikke transkriberes.'),
            ('Hva får jeg?',
             'Hele transkripsjonen som tekst (.txt) og som undertekster med tidsstempler '
             '(.srt). Transkripsjonene er private for kontoen din med mindre du lager en '
             'delelenke.'),
        ]
        return dict(
            lang='nb',
            title='Podcast til tekst: norske podkaster fra lenke',
            meta=('Gjør en podkastepisode om til tekst uten å laste opp lydfil. Lim inn lenke '
                  f'fra Spotify, Apple Podcasts eller RSS. Støtter norsk. {free_no.capitalize()}.'),
            h1='Podcast til tekst, rett fra en lenke',
            lede=('Lim inn lenken til episoden, så får du hele teksten med tidsstempler. Du '
                  'trenger ikke finne og laste opp lydfilen selv.'),
            placeholder='Lim inn lenke fra Spotify, Apple eller RSS',
            input_label='Lenke til podkast eller episode',
            button=_start_button(trial, 'nb', free_trial=free_trial),
            steps=[
                ('Kopier lenken', 'I Spotify eller Apple Podcasts: Del, deretter Kopier lenke.'),
                ('Lim den inn', 'Velg episoden og gjerne «Norsk» som språk.'),
                ('Få teksten', 'Les i nettleseren eller last ned .txt og .srt.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_es':
        # Neutral Spanish for Spain and Latin America; Spotify-first (≈89% of jobs).
        free_es = _free_bit(trial, free_trial, 'es')
        faq = [
            ('¿Cuánto cuesta?', _price_es(trial, pack, free_trial=free_trial)),
            ('¿Funciona bien en español?',
             'Sí, cuando se habla con claridad, tanto en España como en Latinoamérica. Si el episodio '
             'mezcla español e inglés, hay acentos marcados o varias personas hablan a la vez, '
             'elige «Español» en lugar de la detección automática.'),
            ('¿Funciona con todos los podcasts de Spotify?',
             'Con la mayoría sí: casi todos tienen también un feed RSS público, y de ahí '
             'sale el audio. Los exclusivos de Spotify y los episodios solo para '
             'suscriptores no tienen feed público, así que no se pueden transcribir.'),
            ('¿Tengo que subir el archivo de audio?',
             'No. Pega un enlace de Spotify o Apple Podcasts, o la URL de un feed RSS, y '
             'Podskrift descarga el audio del feed público del programa.'),
            ('¿Qué recibo?',
             'La transcripción completa como texto (.txt) y como subtítulos con marcas de '
             'tiempo (.srt). Las transcripciones son privadas y solo tú las ves, salvo que '
             'crees un enlace para compartir.'),
            ('¿Podskrift traduce el podcast?',
             'No: lo transcribe en el idioma del audio. Si necesitas una traducción, pega '
             'el texto en ChatGPT o Claude.'),
        ]
        return dict(
            lang='es',
            title='Transcribir podcast a texto desde un enlace',
            meta=('Pasa un podcast a texto sin subir audio. Pega el enlace de Spotify o '
                  f'Apple Podcasts y descarga .txt y .srt. {free_es.capitalize()}.'),
            h1='Transcribir un podcast a texto desde un enlace',
            lede=('Pega el enlace de Spotify o Apple Podcasts. Podskrift encuentra el '
                  'episodio en el feed público del programa y convierte el audio en texto '
                  'que puedes copiar y descargar.'),
            placeholder='Pega el enlace de Spotify o Apple Podcasts',
            input_label='Enlace de Spotify, Apple Podcasts o RSS',
            button=_start_button(trial, 'es', free_trial=free_trial),
            steps=[
                ('Copia el enlace',
                 'En Spotify o Apple Podcasts: «Compartir» y luego «Copiar enlace».'),
                ('Pégalo aquí',
                 'Elige el episodio y, si puedes, selecciona «Español» como idioma.'),
                ('Recibe el texto',
                 'Léelo en el navegador o descarga .txt y .srt.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_de':
        free_de = _free_bit(trial, free_trial, 'de')
        faq = [
            ('Was kostet es?', _price_de(trial, pack, free_trial=free_trial)),
            ('Wie gut funktioniert es auf Deutsch?',
             'Gut, wenn deutlich gesprochen wird. Starker Dialekt, Durcheinanderreden und '
             'Hintergrundmusik führen zu mehr Fehlern. Wähle „Deutsch“ statt der '
             'automatischen Erkennung, dann wird das Ergebnis besser.'),
            ('Funktioniert das mit jedem Spotify-Podcast?',
             'Mit den meisten ja: fast jede Show hat auch einen öffentlichen RSS-Feed, '
             'und von dort kommt das Audio. Exklusive Spotify-Podcasts und Folgen nur für '
             'Abonnenten haben keinen öffentlichen Feed und lassen sich daher nicht '
             'transkribieren.'),
            ('Muss ich die Audiodatei hochladen?',
             'Nein. Füge einen Spotify- oder Apple-Podcasts-Link oder eine RSS-URL ein. '
             'Podskrift holt das Audio aus dem öffentlichen Feed der Show.'),
            ('Was bekomme ich?',
             'Die vollständige Transkription als Text (.txt) und als Untertitel mit '
             'Zeitstempeln (.srt). Deine Transkripte sind privat, es sei denn, du '
             'erstellst einen Freigabelink.'),
            ('Übersetzt Podskrift den Podcast?',
             'Nein, Podskrift transkribiert in der Sprache der Aufnahme. Wenn du eine '
             'Übersetzung brauchst, füge den Text in ChatGPT oder Claude ein.'),
        ]
        return dict(
            lang='de',
            title='Podcast transkribieren: vom Link zum Text',
            # free_de starts with a digit when the trial is on; .capitalize()
            # would lowercase «Minuten», which German spelling forbids.
            meta=('Podcast-Folge ohne Audiodatei in Text umwandeln. Spotify- oder '
                  f'Apple-Podcasts-Link einfügen, .txt und .srt herunterladen. {free_de}.'),
            h1='Podcast transkribieren, direkt vom Link',
            lede=('Füge einen Spotify- oder Apple-Podcasts-Link ein. Podskrift findet '
                  'dieselbe Folge im öffentlichen RSS-Feed der Show und macht aus dem '
                  'Audio Text zum Kopieren und Herunterladen.'),
            placeholder='Spotify- oder Apple-Podcasts-Link einfügen',
            input_label='Spotify-, Apple-Podcasts- oder RSS-Link',
            button=_start_button(trial, 'de', free_trial=free_trial),
            steps=[
                ('Link kopieren',
                 'In Spotify oder Apple Podcasts: Teilen, dann Link kopieren.'),
                ('Hier einfügen',
                 'Folge auswählen und am besten „Deutsch“ als Sprache wählen.'),
                ('Text erhalten',
                 'Im Browser lesen oder als .txt und .srt herunterladen.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_summarize':
        # Auto-summary is gated by SUMMARY_ENABLED (production on). Facts match
        # summary.py + templates/transcription.html renderSummary.
        free_bit = _free_bit(trial, free_trial, 'en')
        return dict(
            lang='en',
            title='Summarize a Podcast Episode from a Link',
            meta=('Paste a Spotify or Apple episode link. Get an AI summary '
                  '(TL;DR, key points, quotes) above the full transcript. '
                  f'{free_bit.capitalize()}.'),
            h1='Summarize a podcast episode from a link',
            lede=('ChatGPT cannot listen to a Spotify episode. Paste the episode link '
                  'here: Podskrift turns the audio into a transcript, then shows an AI '
                  'summary — TL;DR, key points and a few short quotes — above the full '
                  'text so you can check every point.'),
            placeholder='Paste a Spotify, Apple or RSS episode link',
            input_label='Podcast or episode link',
            button='Summarize this episode',
            will_work=('Works when the show has a public RSS feed — most do. '
                       'Spotify exclusives, subscriber-only and video-only shows don’t. '
                       'Not a YouTube tool.'),
            # /ai is already listed by the template; add the ChatGPT guide.
            also_related=[
                ('guide_chatgpt', 'ChatGPT setup guide'),
            ],
            steps=[
                ('Paste the episode link',
                 'A Spotify, Apple Podcasts or RSS episode link. Searching by show name works too.'),
                ('Get the transcript',
                 'Podskrift fetches the public-feed audio and transcribes it. Only the '
                 'transcript uses your minutes — the summary does not.'),
                ('Read the summary and the full text',
                 'When the transcript is ready, an AI summary appears above it: TL;DR, '
                 'key points and up to a few short quotes. Copy or download .txt / .srt.'),
            ],
            faq=[
                ('What does it cost?', _price_en(trial, pack, free_trial=free_trial)),
                ('Does every show work?',
                 'Most do, when the show also has a public RSS feed. Spotify exclusives, '
                 'subscriber-only episodes and video-only shows have no public feed, so '
                 'they cannot be transcribed or summarised. YouTube URLs are not supported.'),
                ('What is in the summary?',
                 'A short TL;DR, a list of key points, and up to a few short quotes taken '
                 'verbatim from the transcript. The summary is written in the same '
                 'language as the transcript. You also get the full transcript to check '
                 'every point and quote.'),
                ('Does the summary use extra minutes?',
                 'No. Only the transcription uses trial or paid minutes. The summary comes '
                 'with the transcript at no extra cost.'),
                ('What if I only get a free preview of a long episode?',
                 'The summary covers only the part that was transcribed. A “Preview” badge '
                 'marks that the summary is partial.'),
                ('Can I get a different kind of summary?',
                 'Copy the transcript into ChatGPT or Claude for chapter notes, study notes '
                 'or a tweet thread, or connect Podskrift as a connector so they can fetch '
                 'transcripts for you. See the AI and ChatGPT guide links below.'),
                ('Is this the same as Spotify’s or ChatGPT’s own summary?',
                 'No. ChatGPT can’t listen to the episode audio, and Spotify’s own '
                 'transcripts stay inside the Spotify app. Podskrift transcribes the '
                 'public-feed audio itself, then summarises that transcript.'),
                ('Is there speaker labelling?',
                 'Not yet. You get one continuous transcript with timestamps, not named speakers.'),
                ('Do I need an OpenAI API key?',
                 'No. An API key is optional; you can use the free minutes or buy minutes '
                 'instead.'),
            ],
        )

    if endpoint == 'landing_de_summarize':
        # Native German (du-form, matching /de/podcast-transkribieren). Same
        # facts as landing_summarize; SUMMARY_ENABLED gated.
        free_de = _free_bit(trial, free_trial, 'de')
        return dict(
            lang='de',
            title='Podcast-Folge zusammenfassen per Link',
            meta=('Spotify- oder Apple-Link einfügen. KI-Zusammenfassung '
                  '(TL;DR, Kernpunkte, Zitate) über dem vollständigen Transkript. '
                  f'{free_de}.'),
            h1='Podcast-Folge per Link zusammenfassen',
            lede=('ChatGPT kann keine Spotify-Folge anhören. Füge den Folgenlink hier '
                  'ein: Podskrift macht aus dem Audio ein Transkript und zeigt darüber '
                  'eine KI-Zusammenfassung — TL;DR, Kernpunkte und ein paar kurze Zitate '
                  '— damit du jeden Punkt prüfen kannst.'),
            placeholder='Spotify-, Apple-Podcasts- oder RSS-Folgenlink einfügen',
            input_label='Podcast- oder Folgenlink',
            button='Folge zusammenfassen',
            will_work=('Funktioniert, wenn die Show einen öffentlichen RSS-Feed hat — '
                       'die meisten haben einen. Spotify-Exklusives, Folgen nur für '
                       'Abonnenten und rein videobasierte Shows nicht. Kein YouTube-Tool.'),
            also_related=[
                ('guide_chatgpt', 'ChatGPT-Einrichtungsleitfaden'),
            ],
            steps=[
                ('Folgenlink einfügen',
                 'Ein Spotify-, Apple-Podcasts- oder RSS-Folgenlink. Suche nach dem '
                 'Shownamen funktioniert auch.'),
                ('Transkript erhalten',
                 'Podskrift holt das Audio aus dem öffentlichen Feed und transkribiert '
                 'es. Nur das Transkript verbraucht Minuten — die Zusammenfassung nicht.'),
                ('Zusammenfassung und Volltext lesen',
                 'Wenn das Transkript fertig ist, erscheint darüber eine KI-Zusammenfassung: '
                 'TL;DR, Kernpunkte und bis zu ein paar kurze Zitate. Kopieren oder als '
                 '.txt / .srt herunterladen.'),
            ],
            faq=[
                ('Was kostet es?', _price_de(trial, pack, free_trial=free_trial)),
                ('Funktioniert das mit jeder Show?',
                 'Mit den meisten ja, wenn die Show auch einen öffentlichen RSS-Feed hat. '
                 'Spotify-Exklusives, Folgen nur für Abonnenten und rein videobasierte Shows '
                 'haben keinen öffentlichen Feed und lassen sich daher weder transkribieren '
                 'noch zusammenfassen. YouTube-URLs werden nicht unterstützt.'),
                ('Was steckt in der Zusammenfassung?',
                 'Ein kurzes TL;DR, eine Liste mit Kernpunkten und bis zu ein paar kurze '
                 'Zitate wörtlich aus dem Transkript. Die Zusammenfassung steht in derselben '
                 'Sprache wie das Transkript — wähle also „Deutsch“ für deutsche Folgen. '
                 'Du bekommst außerdem das vollständige Transkript, um jeden Punkt und jedes '
                 'Zitat zu prüfen.'),
                ('Verbraucht die Zusammenfassung extra Minuten?',
                 'Nein. Nur die Transkription verbraucht Gratis- oder bezahlte Minuten. Die '
                 'Zusammenfassung kommt mit dem Transkript ohne Aufpreis.'),
                ('Was, wenn ich nur eine kostenlose Vorschau einer langen Folge bekomme?',
                 'Die Zusammenfassung deckt nur den transkribierten Teil ab. Ein „Preview“-Badge '
                 'kennzeichnet, dass die Zusammenfassung unvollständig ist.'),
                ('Kann ich eine andere Art von Zusammenfassung bekommen?',
                 'Kopiere das Transkript in ChatGPT oder Claude für Kapitelnotizen, '
                 'Lernnotizen oder einen Tweet-Thread, oder verbinde Podskrift als Connector, '
                 'damit sie Transkripte für dich holen. Siehe die KI- und ChatGPT-Links unten.'),
                ('Ist das dieselbe Zusammenfassung wie bei Spotify oder ChatGPT?',
                 'Nein. ChatGPT kann das Audio der Folge nicht anhören, und Spotifys eigene '
                 'Transkripte bleiben in der Spotify-App. Podskrift transkribiert das Audio '
                 'aus dem öffentlichen Feed selbst und fasst dieses Transkript dann zusammen.'),
                ('Gibt es Sprecherkennzeichnung?',
                 'Noch nicht. Du bekommst ein durchgehendes Transkript mit Zeitstempeln, '
                 'keine benannten Sprecher.'),
                ('Brauche ich einen OpenAI-API-Schlüssel?',
                 'Nein. Ein API-Schlüssel ist optional; du kannst die Gratisminuten nutzen '
                 'oder Minuten kaufen.'),
            ],
        )

    if endpoint == 'landing_es_summarize':
        # Neutral Spanish (tú-form, matching /es/transcribir-podcast). Same
        # facts as landing_summarize; SUMMARY_ENABLED gated.
        free_es = _free_bit(trial, free_trial, 'es')
        return dict(
            lang='es',
            title='Resumir un episodio de podcast por enlace',
            meta=('Pega un enlace de Spotify o Apple. Resumen con IA '
                  '(TL;DR, puntos clave, citas) sobre la transcripción completa. '
                  f'{free_es.capitalize()}.'),
            h1='Resumir un episodio de podcast desde un enlace',
            lede=('ChatGPT no puede escuchar un episodio de Spotify. Pega el enlace '
                  'aquí: Podskrift convierte el audio en una transcripción y muestra '
                  'encima un resumen con IA — TL;DR, puntos clave y unas cuantas citas '
                  'cortas — para que puedas comprobar cada punto.'),
            placeholder='Pega un enlace de episodio de Spotify, Apple o RSS',
            input_label='Enlace del podcast o del episodio',
            button='Resumir este episodio',
            will_work=('Funciona cuando el programa tiene un feed RSS público — la '
                       'mayoría lo tiene. Los exclusivos de Spotify, los episodios solo '
                       'para suscriptores y los que son solo vídeo, no. No es una '
                       'herramienta de YouTube.'),
            also_related=[
                ('guide_chatgpt', 'Guía de configuración de ChatGPT'),
            ],
            steps=[
                ('Pega el enlace del episodio',
                 'Un enlace de episodio de Spotify, Apple Podcasts o RSS. Buscar por '
                 'nombre del programa también funciona.'),
                ('Obtén la transcripción',
                 'Podskrift descarga el audio del feed público y lo transcribe. Solo la '
                 'transcripción usa tus minutos; el resumen, no.'),
                ('Lee el resumen y el texto completo',
                 'Cuando la transcripción está lista, aparece encima un resumen con IA: '
                 'TL;DR, puntos clave y hasta unas cuantas citas cortas. Copia o descarga '
                 '.txt / .srt.'),
            ],
            faq=[
                ('¿Cuánto cuesta?', _price_es(trial, pack, free_trial=free_trial)),
                ('¿Funciona con todos los programas?',
                 'Con la mayoría sí, cuando el programa también tiene un feed RSS '
                 'público. Los exclusivos de Spotify, los episodios solo para '
                 'suscriptores y los que son solo vídeo no tienen feed público, así que '
                 'no se pueden transcribir ni resumir. Las URL de YouTube no están '
                 'soportadas.'),
                ('¿Qué incluye el resumen?',
                 'Un TL;DR breve, una lista de puntos clave y hasta unas cuantas citas '
                 'cortas tomadas literalmente de la transcripción. El resumen se escribe '
                 'en el mismo idioma que la transcripción: elige «Español» para '
                 'episodios en español. También recibes la transcripción completa para '
                 'comprobar cada punto y cada cita.'),
                ('¿El resumen consume minutos extra?',
                 'No. Solo la transcripción usa minutos de prueba o de pago. El resumen '
                 'viene con la transcripción sin coste adicional.'),
                ('¿Y si solo obtengo una vista previa gratuita de un episodio largo?',
                 'El resumen cubre solo la parte que se transcribió. Una insignia '
                 '«Preview» indica que el resumen es parcial.'),
                ('¿Puedo obtener otro tipo de resumen?',
                 'Copia la transcripción en ChatGPT o Claude para notas por capítulos, '
                 'apuntes de estudio o un hilo, o conecta Podskrift como conector para '
                 'que ellos obtengan las transcripciones. Mira los enlaces de IA y '
                 'ChatGPT más abajo.'),
                ('¿Es lo mismo que el resumen de Spotify o de ChatGPT?',
                 'No. ChatGPT no puede escuchar el audio del episodio, y las '
                 'transcripciones propias de Spotify se quedan dentro de la app. '
                 'Podskrift transcribe el audio del feed público y luego resume esa '
                 'transcripción.'),
                ('¿Hay etiquetas de hablantes?',
                 'Todavía no. Obtienes una transcripción continua con marcas de tiempo, '
                 'sin hablantes con nombre.'),
                ('¿Necesito una clave de API de OpenAI?',
                 'No. Una clave de API es opcional; puedes usar los minutos gratis o '
                 'comprar minutos.'),
            ],
        )

    if endpoint == 'landing_pt':
        # Brazilian Portuguese, Spotify-first; hreflang "pt" (group pattern).
        free_pt = _free_bit(trial, free_trial, 'pt')
        faq = [
            ('Quanto custa?', _price_pt(trial, pack, free_trial=free_trial)),
            ('Funciona bem em português?',
             'Sim, quando a fala está clara — no Brasil e em Portugal. Se o episódio '
             'mistura português e inglês, tem sotaque bem marcado ou várias pessoas '
             'falam ao mesmo tempo, escolha «Português» em vez da detecção automática.'),
            ('Funciona com todos os podcasts do Spotify?',
             'Com a maioria, sim: quase todos também têm um feed RSS público, e o áudio '
             'vem dali. Exclusivos do Spotify e episódios só para assinantes não têm '
             'feed público, então não dá para transcrever.'),
            ('Preciso enviar o arquivo de áudio?',
             'Não. Cole um link do Spotify ou do Apple Podcasts, ou a URL de um feed RSS, '
             'e o Podskrift baixa o áudio do feed público do podcast.'),
            ('O que eu recebo?',
             'A transcrição completa em texto (.txt) e em legendas com timestamps '
             '(.srt). Dá para copiar e colar no ChatGPT ou no Claude. As transcrições '
             'ficam privadas na sua conta, a não ser que você crie um link para '
             'compartilhar.'),
            ('Preciso de chave de API ou assinatura?',
             'Não. Não precisa de chave da OpenAI, e não tem assinatura — os minutos '
             'são compra única.'),
        ]
        return dict(
            lang='pt',
            title='Transcrever podcast em texto pelo link',
            meta=('Transforme um podcast em texto sem enviar áudio. Cole o link do '
                  f'Spotify ou Apple Podcasts e baixe .txt e .srt. {free_pt.capitalize()}.'),
            h1='Transforme um podcast em texto a partir do link',
            lede=('Cole o link do episódio do Spotify ou Apple Podcasts. O Podskrift '
                  'encontra o episódio no feed público do podcast e transforma o áudio '
                  'em texto que você pode copiar e baixar.'),
            placeholder='Cole o link do episódio do Spotify ou Apple Podcasts',
            input_label='Link do Spotify, Apple Podcasts ou RSS',
            button=_start_button(trial, 'pt', free_trial=free_trial),
            steps=[
                ('Copie o link',
                 'No Spotify ou Apple Podcasts: toque em Compartilhar e depois em '
                 'Copiar link.'),
                ('Cole aqui',
                 'Escolha o episódio e, se puder, selecione «Português» como idioma.'),
                ('Leia ou baixe',
                 'Leia no navegador ou baixe .txt e .srt.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_whole_podcast':
        # Honest about the real flow: show/feed → episode list → one episode at
        # a time (episode_selection.html is single-select). No bulk button.
        # MCP list_episodes + get_transcript can chain several via /ai.
        free_bit = _free_bit(trial, free_trial, 'en')
        return dict(
            lang='en',
            title='Transcribe a Whole Podcast from Its Feed',
            meta=('Paste a show link or RSS feed, pick episodes from the list, and '
                  'transcribe a podcast’s back catalogue. Download .txt and .srt. '
                  f'{free_bit.capitalize()}.'),
            h1='Transcribe a whole podcast from its feed',
            lede=('Paste a show link — RSS feed, Apple Podcasts show, or Spotify show '
                  '— and Podskrift lists the episodes. Pick one, get the transcript, '
                  'then come back for the next. Jobs keep running if you close the page; '
                  'finished transcripts sit in History.'),
            placeholder='Paste a show link or RSS feed URL',
            input_label='Show link or podcast RSS feed',
            button='List the episodes',
            will_work=('Works when the show has a public RSS feed — most do. '
                       'Private, password-protected and Spotify-exclusive shows don’t.'),
            steps=[
                ('Paste the show link',
                 'An RSS feed URL, an Apple Podcasts show link, or a Spotify show link. '
                 'Podskrift opens the episode list with titles, dates and lengths.'),
                ('Pick an episode',
                 'Choose one episode at a time and start the transcript. There is no '
                 '“transcribe all” button — each job is one episode.'),
                ('Collect transcripts in History',
                 'Each job keeps running if you close the page. Download .txt or .srt '
                 'per episode from History when it finishes.'),
            ],
            faq=[
                ('Can I transcribe every episode of a show?',
                 'Yes, episode by episode. Paste the show or feed link, pick an episode, '
                 'start the job, then pick the next. There is no single bulk button that '
                 'queues the whole catalogue at once.'),
                ('What does it cost for a whole show?',
                 'Minutes are charged per episode length — a two-hour episode uses about '
                 'two hours of minutes. '
                 + (_price_en(trial, pack, free_trial=free_trial)
                    if (isinstance(trial, int) and trial > 0) or free_trial or pack else
                    'Buy minutes once; no subscription.')
                 + (' Minutes never expire.' if pack else '')),
                ('Can ChatGPT or Claude fetch several episodes for me?',
                 'Yes. Connect Podskrift as an MCP connector, then ask for episodes one '
                 'after another (list_episodes, then get_transcript). Setup is on the '
                 'AI page.'),
                ('Does this work for my own podcast?',
                 'Yes. Paste your public feed and transcribe episodes for show notes or '
                 'captions. The RSS landing page covers creator workflows in more detail.'),
                ('What about private or subscriber-only feeds?',
                 'Private feeds that need a separate login, password-protected feeds, and '
                 'Spotify exclusives without a public RSS feed cannot be transcribed. '
                 'Only public feed audio works.'),
                ('Which languages work?',
                 f'{lang_count} languages in the picker. Choosing the spoken language '
                 'yourself usually beats auto-detect on short or accented audio.'),
                ('Is there speaker labelling?',
                 'Not yet. You get one continuous transcript with timestamps, not named '
                 'speakers.'),
                ('Do I need an OpenAI API key?',
                 'No. An API key is optional; you can use free minutes or buy minutes '
                 'instead.'),
            ],
        )

    if endpoint == 'landing_fr':
        # Native French (vous-form), Spotify-first; hreflang "fr".
        free_fr = _free_bit(trial, free_trial, 'fr')
        faq = [
            ('Combien ça coûte ?', _price_fr(trial, pack, free_trial=free_trial)),
            ('Faut-il choisir « Français » plutôt que la détection auto ?',
             'Oui, de préférence. La détection automatique marche souvent, mais '
             'choisir « Français » donne en général un meilleur résultat, surtout '
             'sur un audio court, un accent marqué ou un mélange de langues.'),
            ('Est-ce que ça marche avec tous les podcasts Spotify ?',
             'Avec la plupart oui : presque tous publient aussi un flux RSS public, '
             'et c’est de là que vient l’audio. Les exclusivités Spotify et les '
             'épisodes réservés aux abonnés n’ont pas de flux public, donc on ne '
             'peut pas les transcrire.'),
            ('Dois-je envoyer un fichier audio ?',
             'Non. Collez le lien de l’épisode Spotify ou Apple Podcasts, ou l’URL '
             'd’un flux RSS, et Podskrift télécharge l’audio depuis le flux public '
             'du podcast.'),
            ('Qu’est-ce que je reçois ?',
             'La transcription complète en texte (.txt) et en sous-titres avec '
             'horodatage (.srt). Vous pouvez la coller dans ChatGPT ou Claude. Les '
             'transcriptions restent privées sur votre compte, sauf si vous créez '
             'un lien de partage.'),
            ('Faut-il une clé API ou un abonnement ?',
             'Non. Pas de clé OpenAI obligatoire, et pas d’abonnement — les minutes '
             's’achètent une seule fois.'),
        ]
        return dict(
            lang='fr',
            title='Transcrire un podcast en texte depuis un lien',
            meta=('Passez un podcast en texte sans envoyer d’audio. Collez le lien '
                  'Spotify ou Apple Podcasts et téléchargez .txt et .srt. '
                  f'{free_fr.capitalize()}.'),
            h1='Transcrire un podcast en texte depuis un lien',
            lede=('Collez le lien de l’épisode Spotify ou Apple Podcasts. Podskrift '
                  'trouve l’épisode dans le flux public du podcast et transforme '
                  'l’audio en texte que vous pouvez copier et télécharger.'),
            placeholder='Collez le lien de l’épisode Spotify ou Apple Podcasts',
            input_label='Lien Spotify, Apple Podcasts ou RSS',
            button=_start_button(trial, 'fr', free_trial=free_trial),
            steps=[
                ('Copiez le lien',
                 'Dans Spotify ou Apple Podcasts : Partager, puis Copier le lien.'),
                ('Collez-le ici',
                 'Choisissez l’épisode et, si possible, sélectionnez « Français » '
                 'comme langue.'),
                ('Lisez ou téléchargez',
                 'Lisez dans le navigateur ou téléchargez .txt et .srt.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_nl':
        # Native Dutch (polite u-form), Spotify-first; hreflang "nl".
        free_nl = _free_bit(trial, free_trial, 'nl')
        faq = [
            ('Wat kost het?', _price_nl(trial, pack, free_trial=free_trial)),
            ('Moet u «Nederlands» kiezen in plaats van automatische detectie?',
             'Ja, bij voorkeur. Automatische detectie werkt vaak, maar «Nederlands» '
             'kiezen geeft meestal een beter resultaat — vooral bij kort audio, een '
             'sterk accent of een mix van talen.'),
            ('Werkt dit met alle Spotify-podcasts?',
             'Met de meeste wel: bijna elke show heeft ook een openbare RSS-feed, en '
             'daarvandaan komt de audio. Exclusieve Spotify-podcasts en afleveringen '
             'alleen voor abonnees hebben geen openbare feed, dus die kunnen we niet '
             'transcriberen.'),
            ('Moet ik een audiobestand uploaden?',
             'Nee. Plak de Spotify- of Apple Podcasts-link, of de URL van een '
             'RSS-feed, en Podskrift downloadt de audio uit de openbare feed van '
             'de podcast.'),
            ('Wat krijg ik?',
             'De volledige transcriptie als tekst (.txt) en als ondertitels met '
             'tijdstempels (.srt). U kunt die plakken in ChatGPT of Claude. '
             'Transcripties blijven privé op uw account, tenzij u een deellink '
             'aanmaakt.'),
            ('Heb ik een API-sleutel of abonnement nodig?',
             'Nee. Geen OpenAI-sleutel verplicht, en geen abonnement — minuten '
             'koopt u eenmalig.'),
        ]
        return dict(
            lang='nl',
            title='Podcast naar tekst vanaf een link',
            meta=('Zet een podcast om naar tekst zonder audio te uploaden. Plak de '
                  'Spotify- of Apple Podcasts-link en download .txt en .srt. '
                  f'{free_nl.capitalize()}.'),
            h1='Podcast naar tekst vanaf een link',
            lede=('Plak de Spotify- of Apple Podcasts-link. Podskrift vindt de '
                  'aflevering in de openbare feed van de podcast en zet de audio om '
                  'in tekst die u kunt kopiëren en downloaden.'),
            placeholder='Plak de Spotify- of Apple Podcasts-link',
            input_label='Spotify-, Apple Podcasts- of RSS-link',
            button=_start_button(trial, 'nl', free_trial=free_trial),
            steps=[
                ('Kopieer de link',
                 'In Spotify of Apple Podcasts: Delen, daarna Link kopiëren.'),
                ('Plak hem hier',
                 'Kies de aflevering en selecteer bij voorkeur «Nederlands» als taal.'),
                ('Lees of download',
                 'Lees in de browser of download .txt en .srt.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_it':
        # Native Italian (lei-form), Spotify-first; hreflang "it".
        free_it = _free_bit(trial, free_trial, 'it')
        faq = [
            ('Quanto costa?', _price_it(trial, pack, free_trial=free_trial)),
            ('Conviene scegliere «Italiano» invece del rilevamento automatico?',
             'Sì, di preferenza. Il rilevamento automatico spesso funziona, ma '
             'scegliere «Italiano» di solito dà un risultato migliore — soprattutto '
             'con audio breve, un accento marcato o un mix di lingue.'),
            ('Funziona con tutti i podcast di Spotify?',
             'Con la maggior parte sì: quasi tutti pubblicano anche un feed RSS '
             'pubblico, ed è da lì che arriva l’audio. Le esclusive Spotify e gli '
             'episodi riservati agli abbonati non hanno un feed pubblico, quindi '
             'non si possono trascrivere.'),
            ('Devo caricare un file audio?',
             'No. Incolla il link di Spotify o Apple Podcasts, oppure l’URL di un '
             'feed RSS, e Podskrift scarica l’audio dal feed pubblico del podcast.'),
            ('Cosa ricevo?',
             'La trascrizione completa in testo (.txt) e come sottotitoli con '
             'timestamp (.srt). Può incollarla in ChatGPT o Claude. Le trascrizioni '
             'restano private sul suo account, a meno che non crei un link di '
             'condivisione.'),
            ('Serve una chiave API o un abbonamento?',
             'No. Nessuna chiave OpenAI obbligatoria e nessun abbonamento: i minuti '
             'si acquistano una sola volta.'),
        ]
        return dict(
            lang='it',
            title='Trascrivere un podcast in testo dal link',
            meta=('Trasforma un podcast in testo senza caricare audio. Incolla il '
                  'link di Spotify o Apple Podcasts e scarica .txt e .srt. '
                  f'{free_it.capitalize()}.'),
            h1='Trascrivere un podcast in testo da un link',
            lede=('Incolla il link di Spotify o Apple Podcasts. Podskrift trova '
                  'l’episodio nel feed pubblico del podcast e trasforma l’audio in '
                  'testo che può copiare e scaricare.'),
            placeholder='Incolla il link di Spotify o Apple Podcasts',
            input_label='Link di Spotify, Apple Podcasts o RSS',
            button=_start_button(trial, 'it', free_trial=free_trial),
            steps=[
                ('Copia il link',
                 'In Spotify o Apple Podcasts: Condividi, poi Copia link.'),
                ('Incollalo qui',
                 'Scegli l’episodio e, se puoi, seleziona «Italiano» come lingua.'),
                ('Leggi o scarica',
                 'Leggi nel browser oppure scarica .txt e .srt.'),
            ],
            faq=faq,
        )
    raise KeyError(endpoint)


UI = {
    'en': dict(how='How it works', faq='Questions', related='More ways to transcribe',
               ai='Use in ChatGPT & Claude', guides='Setup guides', rss_help='How to find an RSS feed',
               pricing='Pricing', shows='Popular podcasts',
               note='Searching by podcast name works too.', example='What you get'),
    'nb': dict(how='Slik fungerer det', faq='Spørsmål og svar', related='Mer om Podskrift',
               ai='Bruk i ChatGPT og Claude', guides='Veiledninger', rss_help='Finn RSS-feeden',
               pricing='Priser', shows='Populære podkaster',
               note='Du kan også søke etter navnet på podkasten.', example='Dette får du'),
    'es': dict(how='Cómo funciona', faq='Preguntas', related='Más formas de transcribir',
               ai='Usar en ChatGPT y Claude', guides='Guías de configuración',
               rss_help='Cómo encontrar un feed RSS',
               pricing='Precios', shows='Podcasts populares',
               note='También puedes buscar por el nombre del podcast.', example='Lo que recibes'),
    'de': dict(how='So funktioniert es', faq='Fragen', related='Weitere Wege zur Transkription',
               ai='In ChatGPT und Claude nutzen', guides='Einrichtungsanleitungen',
               rss_help='RSS-Feed finden',
               pricing='Preise', shows='Beliebte Podcasts',
               note='Du kannst auch nach dem Podcastnamen suchen.', example='Das bekommst du'),
    'pt': dict(how='Como funciona', faq='Perguntas frequentes',
               related='Outras formas de transcrever',
               ai='Usar no ChatGPT e no Claude', guides='Guias de configuração',
               rss_help='Como encontrar um feed RSS',
               pricing='Preços', shows='Podcasts populares',
               note='Você também pode buscar pelo nome do podcast.', example='O que você recebe'),
    'fr': dict(how='Comment ça marche', faq='Questions fréquentes',
               related='D’autres façons de transcrire',
               ai='Utiliser dans ChatGPT et Claude', guides='Guides de configuration',
               rss_help='Comment trouver un flux RSS',
               pricing='Tarifs', shows='Podcasts populaires',
               note='Vous pouvez aussi chercher par le nom du podcast.', example='Ce que vous obtenez'),
    'nl': dict(how='Hoe het werkt', faq='Veelgestelde vragen',
               related='Meer manieren om te transcriberen',
               ai='Gebruiken in ChatGPT en Claude', guides='Installatiegidsen',
               rss_help='Zo vindt u een RSS-feed',
               pricing='Prijzen', shows='Populaire podcasts',
               note='U kunt ook zoeken op de naam van de podcast.', example='Dit krijgt u'),
    'it': dict(how='Come funziona', faq='Domande frequenti',
               related='Altri modi per trascrivere',
               ai='Usare in ChatGPT e Claude', guides='Guide di configurazione',
               rss_help='Come trovare un feed RSS',
               pricing='Prezzi', shows='Podcast popolari',
               note='Può anche cercare per nome del podcast.', example='Cosa ottiene'),
    'no': None,
}
UI['no'] = UI['nb']
