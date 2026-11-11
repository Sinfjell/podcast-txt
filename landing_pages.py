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
    ('landing_de_spotify', '/de/spotify-podcast-transkript'),
    ('landing_es_spotify', '/es/transcribir-podcast-spotify'),
    ('landing_fr_spotify', '/fr/transcrire-podcast-spotify'),
    ('landing_nl_spotify', '/nl/spotify-podcast-naar-tekst'),
    ('landing_it_spotify', '/it/trascrivere-podcast-spotify'),
    ('landing_pt_spotify', '/pt/transcrever-podcast-spotify'),
    ('landing_no_spotify', '/no/spotify-podcast-transkript'),
    ('landing_spotify_no_rss', '/spotify-episode-without-rss'),
    ('landing_spotify_no_transcript', '/spotify-transcript-not-available'),
    ('landing_video_podcast', '/video-podcast-transcript'),
    ('landing_apple', '/apple-podcast-transcript'),
    ('landing_de_apple', '/de/apple-podcast-transkript'),
    ('landing_es_apple', '/es/transcribir-apple-podcasts'),
    ('landing_fr_apple', '/fr/transcrire-apple-podcasts'),
    ('landing_nl_apple', '/nl/apple-podcast-transcriptie'),
    ('landing_it_apple', '/it/trascrivere-apple-podcasts'),
    ('landing_pt_apple', '/pt/transcrever-apple-podcasts'),
    ('landing_no_apple', '/no/apple-podcasts-transkript'),
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
    'landing_de_spotify',
    'landing_es_spotify',
    'landing_fr_spotify',
    'landing_nl_spotify',
    'landing_it_spotify',
    'landing_pt_spotify',
    'landing_no_spotify',
    'landing_spotify_no_rss',
    'landing_spotify_no_transcript',
    'landing_video_podcast',
    'landing_apple',
    'landing_de_apple',
    'landing_es_apple',
    'landing_fr_apple',
    'landing_nl_apple',
    'landing_it_apple',
    'landing_pt_apple',
    'landing_no_apple',
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
#: Reciprocal hreflang group for the Spotify-transcript family (x-default = en).
HREFLANG_SPOTIFY_GROUP = {
    'en': 'landing_spotify',
    'de': 'landing_de_spotify',
    'es': 'landing_es_spotify',
    'fr': 'landing_fr_spotify',
    'nl': 'landing_nl_spotify',
    'it': 'landing_it_spotify',
    'pt': 'landing_pt_spotify',
    'nb': 'landing_no_spotify',
}
#: Reciprocal hreflang group for the Apple Podcasts family (x-default = en).
HREFLANG_APPLE_GROUP = {
    'en': 'landing_apple',
    'de': 'landing_de_apple',
    'es': 'landing_es_apple',
    'fr': 'landing_fr_apple',
    'nl': 'landing_nl_apple',
    'it': 'landing_it_apple',
    'pt': 'landing_pt_apple',
    'nb': 'landing_no_apple',
}
#: All reciprocal hreflang groups (podcast-to-text, summarize, Spotify, Apple, …).
HREFLANG_GROUPS = (
    HREFLANG_GROUP, HREFLANG_SUMMARIZE_GROUP, HREFLANG_SPOTIFY_GROUP,
    HREFLANG_APPLE_GROUP,
)
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
    'landing_de_spotify': 'Spotify-Podcast transkribieren (Deutsch)',
    'landing_es_spotify': 'Podcast de Spotify a texto (español)',
    'landing_fr_spotify': 'Podcast Spotify en texte (français)',
    'landing_nl_spotify': 'Spotify-podcast naar tekst (Nederlands)',
    'landing_it_spotify': 'Podcast Spotify in testo (italiano)',
    'landing_pt_spotify': 'Podcast do Spotify em texto (português)',
    'landing_no_spotify': 'Spotify-podcast til tekst (norsk)',
    'landing_spotify_no_rss': 'Spotify episode without RSS',
    'landing_spotify_no_transcript': 'Spotify transcript not available',
    'landing_video_podcast': 'Video podcast transcript',
    'landing_apple': 'Transcribe from Apple Podcasts',
    'landing_de_apple': 'Apple Podcasts transkribieren (Deutsch)',
    'landing_es_apple': 'Transcribir Apple Podcasts (español)',
    'landing_fr_apple': 'Transcrire Apple Podcasts (français)',
    'landing_nl_apple': 'Apple Podcasts naar tekst (Nederlands)',
    'landing_it_apple': 'Trascrivere Apple Podcasts (italiano)',
    'landing_pt_apple': 'Transcrever Apple Podcasts (português)',
    'landing_no_apple': 'Apple Podcasts til tekst (norsk)',
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
#: Native Spotify landings beyond English (Europe/Oslo).
SPOTIFY_NATIVE_UPDATED = '2026-11-11'
#: Native Apple Podcasts landings beyond English (Europe/Oslo).
APPLE_NATIVE_UPDATED = '2026-11-11'
#: Shared dateModified for the Nov 2026 intent landings (Europe/Oslo).
INTENT_PAGES_UPDATED = '2026-11-04'

#: Endpoints that publish an explicit ``dateModified`` / ``page.updated``.
#: Used by the sitemap ``<lastmod>`` without building the full page copy.
DECLARED_UPDATED = {
    'landing_spotify': SPOTIFY_PAGE_UPDATED,
    'landing_de_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_es_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_fr_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_nl_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_it_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_pt_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_no_spotify': SPOTIFY_NATIVE_UPDATED,
    'landing_spotify_no_rss': INTENT_PAGES_UPDATED,
    'landing_spotify_no_transcript': INTENT_PAGES_UPDATED,
    'landing_video_podcast': INTENT_PAGES_UPDATED,
    'landing_de_apple': APPLE_NATIVE_UPDATED,
    'landing_es_apple': APPLE_NATIVE_UPDATED,
    'landing_fr_apple': APPLE_NATIVE_UPDATED,
    'landing_nl_apple': APPLE_NATIVE_UPDATED,
    'landing_it_apple': APPLE_NATIVE_UPDATED,
    'landing_pt_apple': APPLE_NATIVE_UPDATED,
    'landing_no_apple': APPLE_NATIVE_UPDATED,
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
    elif endpoint == 'landing_de_spotify':
        out = ['landing_de', 'landing_de_summarize', 'landing_de_apple',
               'landing_spotify', 'landing_apple', 'landing_podcast_to_text',
               'landing_free', 'landing_generator']
    elif endpoint == 'landing_es_spotify':
        out = ['landing_es', 'landing_es_summarize', 'landing_es_apple',
               'landing_spotify', 'landing_apple', 'landing_podcast_to_text',
               'landing_free', 'landing_generator']
    elif endpoint == 'landing_fr_spotify':
        out = ['landing_fr', 'landing_fr_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_free',
               'landing_generator']
    elif endpoint == 'landing_nl_spotify':
        out = ['landing_nl', 'landing_nl_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_free',
               'landing_generator']
    elif endpoint == 'landing_it_spotify':
        out = ['landing_it', 'landing_it_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_free',
               'landing_generator']
    elif endpoint == 'landing_pt_spotify':
        out = ['landing_pt', 'landing_pt_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_free',
               'landing_generator']
    elif endpoint == 'landing_no_spotify':
        out = ['landing_no', 'landing_no_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_free',
               'landing_generator']
    elif endpoint == 'landing_de_apple':
        out = ['landing_de', 'landing_de_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_es_apple':
        out = ['landing_es', 'landing_es_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_fr_apple':
        out = ['landing_fr', 'landing_fr_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_nl_apple':
        out = ['landing_nl', 'landing_nl_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_it_apple':
        out = ['landing_it', 'landing_it_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_pt_apple':
        out = ['landing_pt', 'landing_pt_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
    elif endpoint == 'landing_no_apple':
        out = ['landing_no', 'landing_no_spotify', 'landing_apple',
               'landing_spotify', 'landing_podcast_to_text', 'landing_free']
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
        out = ['landing_podcast_to_text', 'landing_no_spotify', 'landing_no_apple',
               'landing_spotify', 'landing_apple', 'landing_rss', 'landing_free',
               'landing_generator', 'landing_youtube_vs']
    elif endpoint == 'landing_es':
        out = ['landing_es_spotify', 'landing_es_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_rss',
               'landing_free']
    elif endpoint == 'landing_de':
        out = ['landing_de_spotify', 'landing_de_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_rss',
               'landing_free']
    elif endpoint == 'landing_pt':
        out = ['landing_pt_spotify', 'landing_pt_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_rss',
               'landing_free']
    elif endpoint == 'landing_fr':
        out = ['landing_fr_spotify', 'landing_fr_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_rss',
               'landing_free']
    elif endpoint == 'landing_nl':
        out = ['landing_nl_spotify', 'landing_nl_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_rss',
               'landing_free']
    elif endpoint == 'landing_it':
        out = ['landing_it_spotify', 'landing_it_apple', 'landing_spotify',
               'landing_apple', 'landing_podcast_to_text', 'landing_rss',
               'landing_free']
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
        'landing_no_spotify': 'Spotify-podcast til tekst (norsk)',
        'landing_no_apple': 'Apple Podcasts til tekst (norsk)',
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
        'landing_es': 'Transcribir podcast (español)',
        'landing_es_summarize': 'Resumir un podcast (español)',
        'landing_es_spotify': 'Podcast de Spotify a texto (español)',
        'landing_es_apple': 'Transcribir Apple Podcasts (español)',
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
        'landing_de': 'Podcast transkribieren (Deutsch)',
        'landing_de_summarize': 'Podcast zusammenfassen (Deutsch)',
        'landing_de_spotify': 'Spotify-Podcast transkribieren (Deutsch)',
        'landing_de_apple': 'Apple Podcasts transkribieren (Deutsch)',
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
        'landing_pt_spotify': 'Podcast do Spotify em texto (português)',
        'landing_pt_apple': 'Transcrever Apple Podcasts (português)',
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
        'landing_fr_spotify': 'Podcast Spotify en texte (français)',
        'landing_fr_apple': 'Transcrire Apple Podcasts (français)',
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
        'landing_nl_spotify': 'Spotify-podcast naar tekst (Nederlands)',
        'landing_nl_apple': 'Apple Podcasts naar tekst (Nederlands)',
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
        'landing_it_spotify': 'Podcast Spotify in testo (italiano)',
        'landing_it_apple': 'Trascrivere Apple Podcasts (italiano)',
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


def _spotify_whole_episode_cost(trial, pack, episode_prices, free_trial=False,
                                lang='en'):
    """Spotify FAQ: live per-episode prices, or pack_phrase when volume is off.

    episode_prices maps episode length (minutes) → price string from
    app.episode_offer(...); empty/None means VOLUME_PRICING is off.
    """
    q = {
        'en': 'What does a whole episode cost?',
        'de': 'Was kostet eine ganze Folge?',
        'es': '¿Cuánto cuesta un episodio completo?',
        'fr': 'Combien coûte un épisode entier ?',
        'nl': 'Wat kost een hele aflevering?',
        'it': 'Quanto costa un episodio intero?',
        'pt': 'Quanto custa um episódio inteiro?',
        'nb': 'Hva koster en hel episode?',
    }.get(lang, 'What does a whole episode cost?')
    price_fn = {
        'de': _price_de, 'es': _price_es, 'fr': _price_fr, 'nl': _price_nl,
        'it': _price_it, 'pt': _price_pt, 'nb': _price_no, 'no': _price_no,
    }.get(lang, _price_en)
    pack_lang = 'nb' if lang in ('nb', 'no') else lang

    if episode_prices:
        bits = []
        for ep in (90, 120, 180):
            price = episode_prices.get(ep)
            if not price:
                continue
            if isinstance(trial, int) and trial > 0:
                if lang == 'de':
                    bits.append(
                        f'{ep} Min.: erste {trial} Min. gratis, Rest {price}')
                elif lang == 'es':
                    bits.append(
                        f'{ep} min: primeros {trial} min gratis, el resto {price}')
                elif lang == 'fr':
                    bits.append(
                        f'{ep} min : {trial} min gratuites d’abord, le reste {price}')
                elif lang == 'nl':
                    bits.append(
                        f'{ep} min: eerste {trial} min gratis, rest {price}')
                elif lang == 'it':
                    bits.append(
                        f'{ep} min: primi {trial} min gratis, il resto {price}')
                elif lang == 'pt':
                    bits.append(
                        f'{ep} min: primeiros {trial} min grátis, o resto {price}')
                elif lang in ('nb', 'no'):
                    bits.append(
                        f'{ep} min: første {trial} min gratis, resten {price}')
                else:
                    bits.append(
                        f'{ep} min: first {trial} min free, the rest {price}')
            else:
                if lang == 'de':
                    bits.append(f'{ep} Min.: {price}')
                else:
                    bits.append(f'{ep} min: {price}')
        if bits:
            joined = '; '.join(bits)
            if lang == 'de':
                return (q, f'Beispiele — {joined}. '
                        'Einmalig, MwSt. inklusive, Minuten verfallen nie.')
            if lang == 'es':
                return (q, f'Ejemplos — {joined}. '
                        'Pago único, IVA incluido, los minutos no caducan.')
            if lang == 'fr':
                return (q, f'Exemples — {joined}. '
                        'Paiement unique, TVA incluse, les minutes n’expirent jamais.')
            if lang == 'nl':
                return (q, f'Voorbeelden — {joined}. '
                        'Eenmalig, btw inbegrepen, minuten verlopen nooit.')
            if lang == 'it':
                return (q, f'Esempi — {joined}. '
                        'Pagamento unico, IVA inclusa, i minuti non scadono.')
            if lang == 'pt':
                return (q, f'Exemplos — {joined}. '
                        'Pagamento único, impostos inclusos, os minutos não expiram.')
            if lang in ('nb', 'no'):
                return (q, f'Eksempler — {joined}. '
                        'Engangskjøp, MVA inkludert, minuttene utløper aldri.')
            return (q, f'Examples — {joined}. '
                    'One-time, VAT included, minutes never expire.')
    if pack:
        phrase = pack_phrase(pack, pack_lang)
        if lang == 'de':
            return (q, f'Minuten kaufst du per Einmalzahlung: {phrase}. '
                    'Einmalig, MwSt. inklusive, Minuten verfallen nie, ohne Abo.')
        if lang == 'es':
            return (q, f'Los minutos se compran con un pago único: {phrase}. '
                    'Pago único, IVA incluido, los minutos no caducan, sin suscripción.')
        if lang == 'fr':
            return (q, f'Les minutes s’achètent en un paiement unique : {phrase}. '
                    'Paiement unique, TVA incluse, les minutes n’expirent jamais, '
                    'sans abonnement.')
        if lang == 'nl':
            return (q, f'Minuten koopt u als eenmalige aankoop: {phrase}. '
                    'Eenmalig, btw inbegrepen, minuten verlopen nooit, geen abonnement.')
        if lang == 'it':
            return (q, f'I minuti si acquistano con un pagamento unico: {phrase}. '
                    'Pagamento unico, IVA inclusa, i minuti non scadono, senza abbonamento.')
        if lang == 'pt':
            return (q, f'Os minutos são uma compra única: {phrase}. '
                    'Pagamento único, impostos inclusos, os minutos não expiram, sem assinatura.')
        if lang in ('nb', 'no'):
            return (q, f'Minutter kjøpes én gang: {phrase}. '
                    'Engangskjøp, MVA inkludert, minuttene utløper aldri, uten abonnement.')
        return (q, f'Minutes are a one-time purchase: {phrase}. '
                'One-time, VAT included, minutes never expire, no subscription.')
    return (q, price_fn(trial, pack, free_trial=free_trial))


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


    if endpoint == 'landing_de_spotify':
        # Native German (du-form, matching /de/podcast-transkribieren). Same
        # facts as landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Illustratives Beispiel — keine echte Folge]\n'
            'Willkommen zurück. Heute sprechen wir darüber, wie Teams leise ausliefern.\n'
            'Der interessante Teil ist, was nach dem Launch passiert ist.\n'
            'Zu den Zahlen kommen wir gleich.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Willkommen zurück. Heute sprechen wir darüber, wie Teams leise ausliefern.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'Der interessante Teil ist, was nach dem Launch passiert ist.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='de')
        free_lede = (
            ' Link einfügen und Gratisminuten nutzen, wenn die Show einen öffentlichen '
            'Feed hat — ohne Karte zum Start.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Kostenloses Transkript starten' if offer
                  else 'Transkript starten')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Spotify-Podcast-Transkript als .txt und .srt. Gratisminuten bei '
                    'öffentlichem Feed; auch wenn Spotify keines zeigt.')
        else:
            meta = ('Spotify-Podcast-Transkript als .txt und .srt. Mit öffentlichem '
                    'RSS-Feed — auch wenn Spotify keines zeigt.')
        return dict(
            lang='de',
            title='Spotify-Podcast in Text umwandeln',
            meta=meta,
            h1='Spotify-Podcast per Link transkribieren',
            lede=('Füge einen open.spotify.com-Folgen- oder Shownamen-Link ein. '
                  'Podskrift findet dieselbe Folge im öffentlichen RSS-Feed der Show '
                  'und macht aus dem Audio Text zum Kopieren, Durchsuchen und '
                  'Herunterladen als .txt oder .srt.'
                  + free_lede),
            near_top={
                'before': 'Brauchst du zuerst Spotifys eigenen App-Text? Siehe ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'wie du Spotifys eigenes Transkript findest und warum es oft fehlt (Englisch)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify-Folgen- oder Showlink',
            button=button,
            will_work=('Funktioniert, wenn die Show auch einen öffentlichen RSS-Feed '
                       'hat — die meisten haben einen. Spotify Originals, Exklusives '
                       'und rein videobasierte Shows nicht. Kein YouTube-Tool.'),
            cta_note=('Du siehst Folge und Preis, bevor du etwas zahlst. Ein '
                      'kostenloses Konto legst du an, nachdem du die Folge gewählt hast.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Spotifys Transkript vs. Podskrift',
            compare_head=('Funktion', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Beim Hören mitlesen', 'Ja, wenn Spotify eines hat',
                 'Creator können Folgen-Transkripte prüfen',
                 'Ja, im Browser nach der Transkription'),
                ('Volltext kopieren', 'Nein — Hörer können das Transkript nicht vollständig kopieren',
                 'Creator können mit eigenen Dateien arbeiten',
                 'Ja — vom Ergebnis kopieren'),
                ('.txt / .srt herunterladen', 'Kein Export für Hörer',
                 'Berechtigte Creator können Transkript-Dateien laden oder hochladen',
                 'Ja — .txt und .srt'),
                ('Über deine Folgen suchen', 'Nur Mitlesen in dieser Folge',
                 'Creator-Tools für ihren Katalog',
                 'Ja — deine Transkript-Bibliothek durchsuchen'),
                ('Wenn Spotify kein Transkript zeigt', 'Nichts zum Lesen in der App',
                 'Hängt von Berechtigung und Creator-Einstellungen ab',
                 'Funktioniert weiter, wenn ein öffentlicher RSS-/Audio-Feed existiert'),
                ('Sprecherkennzeichnung', 'Kein Export mit Sprechernamen für Hörer',
                 'Creator-Tools variieren',
                 'Noch nicht — ein durchgehendes Transkript mit Zeitstempeln'),
            ],
            compare_note=('Podskrift gibt Spotifys App-Text nicht als eigenen aus: es '
                          'transkribiert das öffentliche Audio. Keine Zusammenfassungen '
                          'auf dieser Seite — den Text kannst du selbst in ChatGPT oder '
                          'Claude einfügen.'),
            platform_steps_title='So kopierst du einen Spotify-Folgenlink',
            platform_steps=[
                ('iPhone und iPad', [
                    'Öffne die Folge in der Spotify-App (Folgenseite, nicht die Showseite).',
                    'Tippe auf ⋯ (Mehr), dann Teilen.',
                    'Tippe auf Link kopieren.',
                ]),
                ('Android', [
                    'Öffne die Folge in der Spotify-App.',
                    'Tippe auf ⋮ oder ⋯ (Mehr), dann Teilen.',
                    'Tippe auf Link kopieren.',
                ]),
                ('Desktop-App und Webplayer (open.spotify.com)', [
                    'Öffne die Folge oder finde sie in der Folgenliste der Show.',
                    'Klicke auf ⋯ (Weitere Optionen), dann Teilen.',
                    'Klicke auf Folgenlink kopieren. Er beginnt mit open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Menünamen folgen Spotifys eigener Hilfe; sie können '
                                 'zwischen App-Versionen wandern. Ein Showlink funktioniert '
                                 'auch: du wählst die Folge aus der Liste.'),
            troubleshoot_title='Wenn ein Spotify-Link nicht funktioniert',
            troubleshoot_head=('Was du siehst', 'Warum', 'Was du tun kannst'),
            troubleshoot=[
                ('Ein Spotify-Exklusiv oder Original',
                 'Die Show hat keinen öffentlichen RSS-Feed, also kein Audio, das '
                 'Podskrift holen darf.',
                 'Suche hier nach dem Shownamen oder prüfe Apple Podcasts. Wenn nichts '
                 'kommt, lässt sich die Folge nicht transkribieren.'),
                ('Eine Folge nur für Abonnenten oder bezahlt',
                 'Das Audio liegt hinter einem Abo, nicht im öffentlichen Feed.',
                 'Nicht möglich. Kostenlose Folgen derselben Show funktionieren meist.'),
                ('Eine rein videobasierte Folge',
                 'Es gibt keine Audioversion im öffentlichen Feed der Show.',
                 'Nur wenn die Show die Folge auch als Audio veröffentlicht.'),
                ('Ein Kurzlink (spotify.link/…)',
                 'Kurze Share-Links werden noch nicht gelesen.',
                 'Öffne den Kurzlink im Browser, kopiere die Adresse '
                 'open.spotify.com/episode/… aus der Adresszeile und füge die ein.'),
            ],
            example_label=('Illustratives Beispiel — nur .txt-/.srt-Format. '
                           'Nicht von einer echten Show oder einem Nutzertranskript.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Füge den Text in ChatGPT oder Claude ein zum Zusammenfassen oder '
                     'Nachfragen, oder verbinde Podskrift, damit sie Transkripte für dich holen.'),
            steps=[
                ('Spotify-Link kopieren',
                 'In Spotify die Folge öffnen, Teilen (oder ⋯), dann Link kopieren.'),
                ('Hier einfügen',
                 'Podskrift findet die Folge im öffentlichen RSS-Feed der Show und listet sie. '
                 'Wähle am besten „Deutsch“ als Sprache.'),
                ('Lesen oder herunterladen',
                 'Die Transkription dauert meist ein paar Minuten. Lade .txt oder .srt herunter.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Leitfaden: Spotify-Podcast-Transkript holen (Englisch)'),
            ],
            faq=[
                ('Funktioniert das mit jedem Spotify-Podcast?',
                 'Mit den meisten ja: fast jede Show auf Spotify veröffentlicht auch einen '
                 'öffentlichen RSS-Feed, und von dort kommt das Audio. Spotify-Exklusives und '
                 'Folgen nur für Abonnenten haben keinen öffentlichen Feed und lassen sich '
                 'daher nicht transkribieren. YouTube-URLs werden nicht unterstützt.'),
                ('Was, wenn die Show nur auf Spotify ist?',
                 'Podskrift kann sie nicht transkribieren. Manche Shows, die Spotify-only '
                 'wirken, haben trotzdem einen öffentlichen Feed. Suche hier nach dem '
                 'Shownamen oder prüfe Apple Podcasts. Wenn nichts kommt, ist es ein '
                 'echtes Exklusiv.'),
                ('Kann ich einen Showlink statt eines Folgenlinks einfügen?',
                 'Ja. Ein Showlink öffnet die Folgenliste; dort wählst du die Folge. '
                 'Suche nach dem Shownamen funktioniert auch.'),
                ('Ist das dasselbe wie Spotifys eigenes Transkript?',
                 'Nein. Podskrift erstellt ein eigenes Transkript aus dem Audio — auch '
                 'wenn Spotify keines zeigt — und du kannst es kopieren und herunterladen.'),
                ('Kann ich das Transkript aus der Spotify-App herunterladen?',
                 'Nein — Spotify exportiert das Hörer-Transkript nicht. Füge den Link hier '
                 'ein, um deine eigene Kopie als .txt und .srt zu bekommen.'),
                ('Ist es kostenlos?',
                 'Neue Konten erhalten Gratisminuten, wenn eine Testphase angeboten wird — '
                 'ohne Karte zum Start. Längere Folgen zeigen den genauen Preis, bevor du '
                 'zahlst. Wir nennen hier keine feste Minutenanzahl, weil das Kontingent '
                 'variieren kann.'),
                cost_faq,
                ('Welche Sprachen funktionieren?',
                 f'{lang_count} Sprachen, mit moderner Spracherkennung. Automatische '
                 'Erkennung ist Standard; wähle „Deutsch“ für deutsche Folgen — das hilft '
                 'bei kurzem oder akzentuiertem Audio.'),
                ('Was bekomme ich?',
                 'Die vollständige Transkription als Text (.txt) und als Untertitel mit '
                 'Zeitstempeln (.srt), plus eine Leseansicht im Browser. Transkripte sind '
                 'privat, es sei denn, du erstellst einen Freigabelink. Noch keine '
                 'Sprecherkennzeichnung — ein durchgehendes Transkript mit Zeitstempeln.'),
                ('Was, wenn die Folge länger ist als die Gratisminuten?',
                 'Du bekommst die ersten Gratisminuten als Vorschau und siehst den genauen '
                 'Preis für den Rest, bevor du etwas zahlst. Einmalig, Minuten verfallen nie.'),
            ],
        )

    if endpoint == 'landing_es_spotify':
        # Neutral Spanish (tú-form, matching /es/transcribir-podcast). Same
        # facts as landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Ejemplo ilustrativo — no es un episodio real]\n'
            'Bienvenidos de nuevo. Hoy hablamos de cómo los equipos lanzan en silencio.\n'
            'Lo interesante es lo que pasó después del lanzamiento.\n'
            'En un momento llegamos a los números.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Bienvenidos de nuevo. Hoy hablamos de cómo los equipos lanzan en silencio.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'Lo interesante es lo que pasó después del lanzamiento.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='es')
        free_lede = (
            ' Pega un enlace y usa minutos de prueba cuando el programa tenga un feed '
            'público — sin tarjeta para empezar.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Empezar transcripción gratis' if offer
                  else 'Empezar transcripción')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Transcripción de podcast de Spotify (.txt y .srt). Minutos gratis '
                    'si hay feed público; también cuando Spotify no muestra ninguna.')
        else:
            meta = ('Transcripción de podcast de Spotify (.txt y .srt). Con feed RSS '
                    'público — también cuando Spotify no muestra ninguna.')
        return dict(
            lang='es',
            title='Transcribir un podcast de Spotify con un enlace',
            meta=meta,
            h1='Transcribir un podcast de Spotify desde un enlace',
            lede=('Pega un enlace de episodio o de programa de open.spotify.com. '
                  'Podskrift encuentra el mismo episodio en el feed RSS público del '
                  'programa y convierte el audio en texto que puedes copiar, buscar '
                  'y descargar como .txt o .srt.'
                  + free_lede),
            near_top={
                'before': '¿Necesitas primero el texto propio de Spotify en la app? Mira ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'cómo encontrar la transcripción de Spotify y por qué a menudo falta (en inglés)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Enlace de episodio o programa de Spotify',
            button=button,
            will_work=('Funciona cuando el programa también tiene un feed RSS público — '
                       'la mayoría lo tiene. Los Originals de Spotify, los exclusivos y '
                       'los que son solo vídeo, no. No es una herramienta de YouTube.'),
            cta_note=('Ves el episodio y su precio antes de pagar nada. Creas una cuenta '
                      'gratis después de elegir el episodio.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Transcripción de Spotify vs Podskrift',
            compare_head=('Función', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Leer mientras escuchas', 'Sí, cuando Spotify tiene una',
                 'Los creadores pueden revisar transcripciones del episodio',
                 'Sí, en el navegador tras la transcripción'),
                ('Copiar el texto completo', 'No — los oyentes no pueden copiar la transcripción completa',
                 'Los creadores pueden trabajar con sus propios archivos',
                 'Sí — copia desde la página de resultado'),
                ('Descargar .txt / .srt', 'Sin exportación para oyentes',
                 'Creadores elegibles pueden descargar o subir archivos de transcripción',
                 'Sí — .txt y .srt'),
                ('Buscar en tus episodios', 'Solo seguimiento en ese episodio',
                 'Herramientas de creador para su catálogo',
                 'Sí — busca en tu biblioteca de transcripciones'),
                ('Cuando Spotify no muestra transcripción', 'Nada que leer en la app',
                 'Depende de la elegibilidad y de la configuración del creador',
                 'Sigue funcionando si existe un feed RSS/audio público'),
                ('Etiquetas de hablantes', 'Sin exportación con nombres de hablantes para oyentes',
                 'Las herramientas de creador varían',
                 'Todavía no — una transcripción continua con marcas de tiempo'),
            ],
            compare_note=('Podskrift no presenta el texto de la app de Spotify como propio: '
                          'transcribe el audio público. Sin resúmenes en esta página — puedes '
                          'pegar el texto en ChatGPT o Claude tú mismo.'),
            platform_steps_title='Cómo copiar un enlace de episodio de Spotify',
            platform_steps=[
                ('iPhone e iPad', [
                    'Abre el episodio en la app de Spotify (la página del episodio, no la del programa).',
                    'Toca ⋯ (Más) y luego Compartir.',
                    'Toca Copiar enlace.',
                ]),
                ('Android', [
                    'Abre el episodio en la app de Spotify.',
                    'Toca ⋮ o ⋯ (Más) y luego Compartir.',
                    'Toca Copiar enlace.',
                ]),
                ('App de escritorio y reproductor web (open.spotify.com)', [
                    'Abre el episodio o búscalo en la lista de episodios del programa.',
                    'Haz clic en ⋯ (Más opciones) y luego Compartir.',
                    'Haz clic en Copiar enlace del episodio. Empieza por open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Los nombres de menú siguen la ayuda de Spotify; pueden '
                                 'cambiar entre versiones de la app. Un enlace de programa '
                                 'también vale: eliges el episodio de la lista.'),
            troubleshoot_title='Cuando un enlace de Spotify no funciona',
            troubleshoot_head=('Lo que ves', 'Por qué', 'Qué hacer'),
            troubleshoot=[
                ('Un exclusivo u Original de Spotify',
                 'El programa no tiene feed RSS público, así que no hay audio que '
                 'Podskrift pueda descargar.',
                 'Busca el nombre del programa aquí o mira en Apple Podcasts. Si no '
                 'aparece nada, no se puede transcribir.'),
                ('Un episodio solo para suscriptores o de pago',
                 'El audio está detrás de una suscripción, no en el feed público.',
                 'No es posible. Los episodios gratuitos del mismo programa suelen funcionar.'),
                ('Un episodio solo en vídeo',
                 'No hay versión de audio en el feed público del programa.',
                 'Solo funciona si el programa también publica el episodio en audio.'),
                ('Un enlace corto (spotify.link/…)',
                 'Los enlaces cortos de compartir aún no se leen.',
                 'Abre el enlace corto en el navegador, copia la dirección '
                 'open.spotify.com/episode/… de la barra de direcciones y pégala.'),
            ],
            example_label=('Ejemplo ilustrativo — solo el formato .txt / .srt. '
                           'No pertenece a un programa real ni a la transcripción de un usuario.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Pégalo en ChatGPT o Claude para resumir o preguntar, o conecta '
                     'Podskrift para que obtengan las transcripciones por ti.'),
            steps=[
                ('Copia el enlace de Spotify',
                 'En Spotify, abre el episodio, toca Compartir (o ⋯) y luego Copiar enlace.'),
                ('Pégalo aquí',
                 'Podskrift encuentra el episodio en el feed RSS público y lo lista. '
                 'Elige «Español» como idioma si puedes.'),
                ('Lee o descarga',
                 'La transcripción suele tardar unos minutos. Descarga .txt o .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Guía: cómo obtener una transcripción de podcast de Spotify (en inglés)'),
            ],
            faq=[
                ('¿Funciona con todos los podcasts de Spotify?',
                 'Con la mayoría sí: casi todos los programas en Spotify también publican '
                 'un feed RSS público, y de ahí sale el audio. Los exclusivos de Spotify y '
                 'los episodios solo para suscriptores no tienen feed público, así que no '
                 'se pueden transcribir. Las URL de YouTube no están soportadas.'),
                ('¿Y si el programa es solo de Spotify?',
                 'Podskrift no puede transcribirlo. Algunos programas que parecen solo de '
                 'Spotify también publican un feed público. Busca el nombre aquí o mira '
                 'Apple Podcasts. Si no aparece nada, es un exclusivo de verdad.'),
                ('¿Puedo pegar un enlace de programa en lugar de un episodio?',
                 'Sí. Un enlace de programa abre la lista de episodios y eliges ahí. '
                 'Buscar por el nombre del programa también funciona.'),
                ('¿Es lo mismo que la transcripción propia de Spotify?',
                 'No. Podskrift crea su propia transcripción a partir del audio, así que '
                 'también funciona cuando Spotify no muestra ninguna, y puedes copiarla '
                 'y descargarla.'),
                ('¿Puedo descargar la transcripción que muestra Spotify en la app?',
                 'No — Spotify no exporta la transcripción del oyente. Pega el enlace aquí '
                 'para obtener tu propia copia como .txt y .srt.'),
                ('¿Es gratis?',
                 'Las cuentas nuevas reciben minutos de prueba cuando hay oferta — sin '
                 'tarjeta para empezar. Los episodios más largos muestran el precio exacto '
                 'antes de pagar. No fijamos aquí un número concreto de minutos gratis '
                 'porque la concesión puede variar.'),
                cost_faq,
                ('¿Qué idiomas funcionan?',
                 f'{lang_count} idiomas, con reconocimiento de voz moderno. La detección '
                 'automática es la opción por defecto; elige «Español» para episodios en '
                 'español: ayuda con audio corto o con acento.'),
                ('¿Qué recibo?',
                 'La transcripción completa como texto (.txt) y como subtítulos con marcas '
                 'de tiempo (.srt), más una vista de lectura en el navegador. Las '
                 'transcripciones son privadas salvo que crees un enlace para compartir. '
                 'Todavía no hay etiquetas de hablantes: una transcripción continua con '
                 'marcas de tiempo.'),
                ('¿Y si el episodio dura más que los minutos gratis?',
                 'Obtienes los primeros minutos gratis como vista previa y ves el precio '
                 'exacto del resto antes de pagar. Pago único; los minutos no caducan.'),
            ],
        )

    if endpoint == 'landing_fr_spotify':
        # Native French (vous-form, matching /fr/transcrire-podcast). Same
        # facts as landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Exemple illustratif — pas un vrai épisode]\n'
            'Bienvenue. Aujourd’hui nous parlons de la façon dont les équipes livrent en silence.\n'
            'Ce qui est intéressant, c’est ce qui s’est passé après le lancement.\n'
            'Nous arrivons aux chiffres dans un instant.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Bienvenue. Aujourd’hui nous parlons de la façon dont les équipes livrent en silence.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'Ce qui est intéressant, c’est ce qui s’est passé après le lancement.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='fr')
        free_lede = (
            ' Collez un lien et utilisez des minutes gratuites quand l’émission a un flux '
            'public — sans carte pour commencer.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Commencer la transcription gratuite' if offer
                  else 'Commencer la transcription')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Transcription de podcast Spotify en .txt et .srt. Minutes gratuites '
                    'si flux public ; aussi quand Spotify n’en affiche aucune.')
        else:
            meta = ('Transcription de podcast Spotify en .txt et .srt. Avec un flux RSS '
                    'public — aussi quand Spotify n’en affiche aucune.')
        return dict(
            lang='fr',
            title='Transcrire un podcast Spotify depuis un lien',
            meta=meta,
            h1='Transcrire un podcast Spotify depuis un lien',
            lede=('Collez un lien d’épisode ou d’émission open.spotify.com. '
                  'Podskrift retrouve le même épisode dans le flux RSS public de '
                  'l’émission et transforme l’audio en texte que vous pouvez copier, '
                  'rechercher et télécharger en .txt ou .srt.'
                  + free_lede),
            near_top={
                'before': 'Vous cherchez d’abord le texte intégré de Spotify dans l’app ? Consultez ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'comment trouver la transcription de Spotify et pourquoi elle manque souvent (en anglais)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Lien d’épisode ou d’émission Spotify',
            button=button,
            will_work=('Fonctionne quand l’émission dispose aussi d’un flux RSS public — '
                       'la plupart en ont un. Les Originals Spotify, les exclusivités et '
                       'les émissions uniquement vidéo, non. Ce n’est pas un outil YouTube.'),
            cta_note=('Vous voyez l’épisode et son prix avant de payer quoi que ce soit. '
                      'Vous créez un compte gratuit après avoir choisi l’épisode.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Transcription Spotify vs Podskrift',
            compare_head=('Fonction', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Lire en écoutant', 'Oui, quand Spotify en propose une',
                 'Les créateurs peuvent consulter les transcriptions d’épisode',
                 'Oui, dans le navigateur après la transcription'),
                ('Copier le texte complet', 'Non — les auditeurs ne peuvent pas copier toute la transcription',
                 'Les créateurs peuvent travailler avec leurs propres fichiers',
                 'Oui — copie depuis la page de résultat'),
                ('Télécharger .txt / .srt', 'Pas d’export pour les auditeurs',
                 'Les créateurs éligibles peuvent télécharger ou déposer des fichiers de transcription',
                 'Oui — .txt et .srt'),
                ('Rechercher dans vos épisodes', 'Suivi de lecture dans cet épisode seulement',
                 'Outils créateur pour leur catalogue',
                 'Oui — recherche dans votre bibliothèque de transcriptions'),
                ('Quand Spotify n’affiche pas de transcription', 'Rien à lire dans l’app',
                 'Dépend de l’éligibilité et des réglages du créateur',
                 'Fonctionne tant qu’un flux RSS/audio public existe'),
                ('Étiquettes d’intervenants', 'Pas d’export avec noms d’intervenants pour les auditeurs',
                 'Les outils créateur varient',
                 'Pas encore — une transcription continue avec horodatages'),
            ],
            compare_note=('Podskrift ne présente pas le texte de l’app Spotify comme le sien : '
                          'il transcrit l’audio public. Pas de résumés sur cette page — vous '
                          'pouvez coller le texte dans ChatGPT ou Claude vous-même.'),
            platform_steps_title='Comment copier un lien d’épisode Spotify',
            platform_steps=[
                ('iPhone et iPad', [
                    'Ouvrez l’épisode dans l’app Spotify (la page épisode, pas celle de l’émission).',
                    'Touchez ⋯ (Plus), puis Partager.',
                    'Touchez Copier le lien.',
                ]),
                ('Android', [
                    'Ouvrez l’épisode dans l’app Spotify.',
                    'Touchez ⋮ ou ⋯ (Plus), puis Partager.',
                    'Touchez Copier le lien.',
                ]),
                ('Application de bureau et lecteur web (open.spotify.com)', [
                    'Ouvrez l’épisode ou retrouvez-le dans la liste des épisodes de l’émission.',
                    'Cliquez sur ⋯ (Plus d’options), puis Partager.',
                    'Cliquez sur Copier le lien de l’épisode. Il commence par open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Les libellés de menu suivent l’aide de Spotify ; ils peuvent '
                                 'varier selon les versions de l’app. Un lien d’émission convient '
                                 'aussi : vous choisissez l’épisode dans la liste.'),
            troubleshoot_title='Quand un lien Spotify ne fonctionne pas',
            troubleshoot_head=('Ce que vous voyez', 'Pourquoi', 'Que faire'),
            troubleshoot=[
                ('Une exclusivité ou un Original Spotify',
                 'L’émission n’a pas de flux RSS public, donc pas d’audio que '
                 'Podskrift peut récupérer.',
                 'Recherchez le nom de l’émission ici ou vérifiez Apple Podcasts. Si rien '
                 'n’apparaît, l’épisode ne peut pas être transcrit.'),
                ('Un épisode réservé aux abonnés ou payant',
                 'L’audio est derrière un abonnement, pas dans le flux public.',
                 'Impossible. Les épisodes gratuits de la même émission fonctionnent en général.'),
                ('Un épisode uniquement en vidéo',
                 'Il n’y a pas de version audio dans le flux public de l’émission.',
                 'Uniquement si l’émission publie aussi l’épisode en audio.'),
                ('Un lien court (spotify.link/…)',
                 'Les liens courts de partage ne sont pas encore lus.',
                 'Ouvrez le lien court dans le navigateur, copiez l’adresse '
                 'open.spotify.com/episode/… depuis la barre d’adresse et collez-la ici.'),
            ],
            example_label=('Exemple illustratif — format .txt / .srt seulement. '
                           'Issu d’aucune vraie émission ni d’une transcription utilisateur.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Collez-le dans ChatGPT ou Claude pour résumer ou poser des questions, '
                     'ou connectez Podskrift pour qu’ils récupèrent les transcriptions pour vous.'),
            steps=[
                ('Copiez le lien Spotify',
                 'Dans Spotify, ouvrez l’épisode, touchez Partager (ou ⋯), puis Copier le lien.'),
                ('Collez-le ici',
                 'Podskrift retrouve l’épisode dans le flux RSS public de l’émission et l’affiche. '
                 'Choisissez de préférence « Français » comme langue.'),
                ('Lisez ou téléchargez',
                 'La transcription prend en général quelques minutes. Téléchargez .txt ou .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Guide : obtenir une transcription de podcast Spotify (en anglais)'),
            ],
            faq=[
                ('Est-ce que ça marche avec tous les podcasts Spotify ?',
                 'Pour la plupart oui : presque toutes les émissions sur Spotify publient '
                 'aussi un flux RSS public, et c’est de là que vient l’audio. Les exclusivités '
                 'Spotify et les épisodes réservés aux abonnés n’ont pas de flux public, donc '
                 'ils ne peuvent pas être transcrits. Les URL YouTube ne sont pas prises en charge.'),
                ('Et si l’émission est uniquement sur Spotify ?',
                 'Podskrift ne peut pas la transcrire. Certaines émissions qui semblent '
                 'exclusivement sur Spotify publient quand même un flux public. Recherchez le '
                 'nom ici ou vérifiez Apple Podcasts. Si rien n’apparaît, c’est une vraie '
                 'exclusivité.'),
                ('Puis-je coller un lien d’émission plutôt qu’un lien d’épisode ?',
                 'Oui. Un lien d’émission ouvre la liste des épisodes ; vous choisissez '
                 'l’épisode. La recherche par nom d’émission fonctionne aussi.'),
                ('Est-ce la même chose que la transcription propre à Spotify ?',
                 'Non. Podskrift crée sa propre transcription à partir de l’audio — elle '
                 'fonctionne aussi quand Spotify n’en affiche aucune — et vous pouvez la '
                 'copier et la télécharger.'),
                ('Puis-je télécharger la transcription affichée dans l’app Spotify ?',
                 'Non — Spotify n’exporte pas la transcription auditeur. Collez le lien ici '
                 'pour obtenir votre propre copie en .txt et .srt.'),
                ('Est-ce gratuit ?',
                 'Les nouveaux comptes reçoivent des minutes gratuites quand une offre d’essai '
                 'est disponible — sans carte pour commencer. Les épisodes plus longs affichent '
                 'le prix exact avant paiement. Nous n’indiquons pas ici un nombre fixe de '
                 'minutes gratuites, car l’allocation peut varier.'),
                cost_faq,
                ('Quelles langues sont prises en charge ?',
                 f'{lang_count} langues, avec reconnaissance vocale moderne. La détection '
                 'automatique est le réglage par défaut ; choisissez « Français » pour les '
                 'épisodes en français — cela aide sur un audio court ou accentué.'),
                ('Qu’est-ce que je reçois ?',
                 'La transcription complète en texte (.txt) et en sous-titres avec horodatage '
                 '(.srt), plus une vue lecture dans le navigateur. Les transcriptions restent '
                 'privées sauf si vous créez un lien de partage. Pas encore d’étiquettes '
                 'd’intervenants — une transcription continue avec horodatages.'),
                ('Et si l’épisode dépasse les minutes gratuites ?',
                 'Vous obtenez les premières minutes gratuites en aperçu et voyez le prix exact '
                 'du reste avant de payer. Paiement unique ; les minutes n’expirent jamais.'),
            ],
        )

    if endpoint == 'landing_nl_spotify':
        # Native Dutch (polite u-form, matching /nl/podcast-naar-tekst). Same
        # facts as landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Illustratief voorbeeld — geen echte aflevering]\n'
            'Welkom terug. Vandaag bespreken we hoe teams stilletjes uitrollen.\n'
            'Het interessante deel is wat er na de launch gebeurde.\n'
            'Straks komen we bij de cijfers.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Welkom terug. Vandaag bespreken we hoe teams stilletjes uitrollen.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'Het interessante deel is wat er na de launch gebeurde.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='nl')
        free_lede = (
            ' Plak een link en gebruik gratis minuten wanneer de show een openbare feed '
            'heeft — geen kaart nodig om te starten.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Start gratis transcriptie' if offer
                  else 'Start transcriptie')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Spotify-podcasttranscriptie als .txt en .srt. Gratis minuten bij '
                    'openbare feed; ook wanneer Spotify er geen toont.')
        else:
            meta = ('Spotify-podcasttranscriptie als .txt en .srt. Met openbare RSS-feed '
                    '— ook wanneer Spotify er geen toont.')
        return dict(
            lang='nl',
            title='Spotify-podcast naar tekst met een link',
            meta=meta,
            h1='Spotify-podcast naar tekst vanaf een link',
            lede=('Plak een open.spotify.com-link van een aflevering of show. '
                  'Podskrift vindt dezelfde aflevering in de openbare RSS-feed van de '
                  'show en zet de audio om in tekst die u kunt kopiëren, doorzoeken '
                  'en downloaden als .txt of .srt.'
                  + free_lede),
            near_top={
                'before': 'Zoekt u eerst Spotifys eigen tekst in de app? Zie ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'hoe u Spotifys eigen transcript vindt en waarom die vaak ontbreekt (Engels)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify-link van aflevering of show',
            button=button,
            will_work=('Werkt wanneer de show ook een openbare RSS-feed heeft — de meeste '
                       'wel. Spotify Originals, exclusives en puur videoshows niet. Geen '
                       'YouTube-tool.'),
            cta_note=('U ziet de aflevering en de prijs voordat u iets betaalt. U maakt een '
                      'gratis account nadat u de aflevering hebt gekozen.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Spotifys transcript vs Podskrift',
            compare_head=('Functie', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Meelezen tijdens het luisteren', 'Ja, wanneer Spotify er een heeft',
                 'Makers kunnen afleveringstranscripties bekijken',
                 'Ja, in de browser na de transcriptie'),
                ('Volledige tekst kopiëren', 'Nee — luisteraars kunnen het volledige transcript niet kopiëren',
                 'Makers kunnen met eigen bestanden werken',
                 'Ja — kopiëren vanaf de resultaatpagina'),
                ('.txt / .srt downloaden', 'Geen export voor luisteraars',
                 'In aanmerking komende makers kunnen transcriptbestanden downloaden of uploaden',
                 'Ja — .txt en .srt'),
                ('Zoeken in uw afleveringen', 'Alleen meelezen in die aflevering',
                 'Makershulpmiddelen voor hun catalogus',
                 'Ja — doorzoek uw transcriptbibliotheek'),
                ('Wanneer Spotify geen transcript toont', 'Niets om te lezen in de app',
                 'Hangt af van geschiktheid en instellingen van de maker',
                 'Werkt nog steeds als er een openbare RSS-/audiofeed bestaat'),
                ('Sprekerlabels', 'Geen export met sprekernamen voor luisteraars',
                 'Makershulpmiddelen verschillen',
                 'Nog niet — één doorlopend transcript met tijdstempels'),
            ],
            compare_note=('Podskrift presenteert Spotifys app-tekst niet als eigen werk: het '
                          'transcribeert de openbare audio. Geen samenvattingen op deze pagina — '
                          'u kunt de tekst zelf in ChatGPT of Claude plakken.'),
            platform_steps_title='Zo kopieert u een Spotify-afleveringslink',
            platform_steps=[
                ('iPhone en iPad', [
                    'Open de aflevering in de Spotify-app (de afleveringspagina, niet die van de show).',
                    'Tik op ⋯ (Meer), daarna Delen.',
                    'Tik op Link kopiëren.',
                ]),
                ('Android', [
                    'Open de aflevering in de Spotify-app.',
                    'Tik op ⋮ of ⋯ (Meer), daarna Delen.',
                    'Tik op Link kopiëren.',
                ]),
                ('Desktop-app en webplayer (open.spotify.com)', [
                    'Open de aflevering of zoek hem in de afleveringenlijst van de show.',
                    'Klik op ⋯ (Meer opties), daarna Delen.',
                    'Klik op Afleveringslink kopiëren. Die begint met open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Menunamen volgen Spotifys eigen hulp; ze kunnen per app-versie '
                                 'wisselen. Een showlink werkt ook: u kiest de aflevering uit de lijst.'),
            troubleshoot_title='Als een Spotify-link niet werkt',
            troubleshoot_head=('Wat u ziet', 'Waarom', 'Wat te doen'),
            troubleshoot=[
                ('Een Spotify-exclusief of Original',
                 'De show heeft geen openbare RSS-feed, dus geen audio die Podskrift '
                 'kan ophalen.',
                 'Zoek hier op shownaam of controleer Apple Podcasts. Als er niets '
                 'verschijnt, kan de aflevering niet worden getranscribeerd.'),
                ('Een aflevering alleen voor abonnees of betaald',
                 'De audio staat achter een abonnement, niet in de openbare feed.',
                 'Niet mogelijk. Gratis afleveringen van dezelfde show werken meestal wel.'),
                ('Een puur video-aflevering',
                 'Er is geen audioversie in de openbare feed van de show.',
                 'Alleen als de show de aflevering ook als audio publiceert.'),
                ('Een korte link (spotify.link/…)',
                 'Korte deellinks worden nog niet ingelezen.',
                 'Open de korte link in de browser, kopieer het adres '
                 'open.spotify.com/episode/… uit de adresbalk en plak dat.'),
            ],
            example_label=('Illustratief voorbeeld — alleen .txt-/.srt-formaat. '
                           'Geen echte show of gebruikerstranscript.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Plak het in ChatGPT of Claude om samen te vatten of vragen te stellen, '
                     'of koppel Podskrift zodat zij transcripties voor u ophalen.'),
            steps=[
                ('Kopieer de Spotify-link',
                 'Open in Spotify de aflevering, tik op Delen (of ⋯), daarna Link kopiëren.'),
                ('Plak hem hier',
                 'Podskrift vindt de aflevering in de openbare RSS-feed en toont hem. '
                 'Kies bij voorkeur "Nederlands" als taal.'),
                ('Lees of download',
                 'Transcriptie duurt meestal een paar minuten. Download .txt of .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Gids: een Spotify-podcasttranscript ophalen (Engels)'),
            ],
            faq=[
                ('Werkt dit met alle Spotify-podcasts?',
                 'Met de meeste wel: bijna elke show op Spotify publiceert ook een openbare '
                 'RSS-feed, en vandaar komt de audio. Exclusieve Spotify-shows en afleveringen '
                 'alleen voor abonnees hebben geen openbare feed en kunnen dus niet worden '
                 'getranscribeerd. YouTube-URL’s worden niet ondersteund.'),
                ('Wat als de show alleen op Spotify staat?',
                 'Podskrift kan hem niet transcriberen. Sommige shows die Spotify-only lijken '
                 'publiceren toch een openbare feed. Zoek hier op shownaam of controleer Apple '
                 'Podcasts. Als er niets verschijnt, is het een echte exclusief.'),
                ('Kan ik een showlink plakken in plaats van een afleveringslink?',
                 'Ja. Een showlink opent de afleveringenlijst; daar kiest u de aflevering. '
                 'Zoeken op shownaam werkt ook.'),
                ('Is dit hetzelfde als Spotifys eigen transcript?',
                 'Nee. Podskrift maakt een eigen transcript van de audio — het werkt ook als '
                 'Spotify er geen toont — en u kunt het kopiëren en downloaden.'),
                ('Kan ik het transcript uit de Spotify-app downloaden?',
                 'Nee — Spotify exporteert het luisteraarstranscript niet. Plak de link hier '
                 'voor uw eigen kopie als .txt en .srt.'),
                ('Is het gratis?',
                 'Nieuwe accounts krijgen gratis minuten wanneer een proefaanbod loopt — geen '
                 'kaart nodig om te starten. Langere afleveringen tonen de exacte prijs voordat '
                 'u betaalt. We noemen hier geen vast aantal gratis minuten, omdat de toekenning '
                 'kan variëren.'),
                cost_faq,
                ('Welke talen worden ondersteund?',
                 f'{lang_count} talen, met moderne spraakherkenning. Automatische detectie is '
                 'standaard; kies "Nederlands" voor Nederlandse afleveringen — dat helpt bij '
                 'korte of geaccentueerde audio.'),
                ('Wat krijg ik?',
                 'De volledige transcriptie als tekst (.txt) en als ondertitels met '
                 'tijdstempels (.srt), plus een leesweergave in de browser. Transcripties '
                 'blijven privé tenzij u een deellink aanmaakt. Nog geen sprekerlabels — '
                 'één doorlopend transcript met tijdstempels.'),
                ('Wat als de aflevering langer is dan de gratis minuten?',
                 'U krijgt de eerste gratis minuten als voorbeeld en ziet de exacte prijs voor '
                 'de rest voordat u betaalt. Eenmalig; minuten verlopen nooit.'),
            ],
        )

    if endpoint == 'landing_it_spotify':
        # Native Italian (lei-form, matching /it/trascrivere-podcast). Same
        # facts as landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Esempio illustrativo — non è un episodio reale]\n'
            'Bentornati. Oggi parliamo di come i team rilasciano in silenzio.\n'
            'La parte interessante è cosa è successo dopo il lancio.\n'
            'Tra poco arriviamo ai numeri.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Bentornati. Oggi parliamo di come i team rilasciano in silenzio.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'La parte interessante è cosa è successo dopo il lancio.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='it')
        free_lede = (
            ' Incolli un link e usi minuti gratis quando il programma ha un feed pubblico '
            '— senza carta per iniziare.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Inizia la trascrizione gratuita' if offer
                  else 'Inizia la trascrizione')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Trascrizione podcast Spotify in .txt e .srt. Minuti gratis con feed '
                    'pubblico; anche quando Spotify non ne mostra nessuna.')
        else:
            meta = ('Trascrizione podcast Spotify in .txt e .srt. Con feed RSS pubblico '
                    '— anche quando Spotify non ne mostra nessuna.')
        return dict(
            lang='it',
            title='Trascrivere un podcast Spotify da un link',
            meta=meta,
            h1='Trascrivere un podcast Spotify da un link',
            lede=('Incolli un link di episodio o programma da open.spotify.com. '
                  'Podskrift trova lo stesso episodio nel feed RSS pubblico del programma '
                  'e trasforma l’audio in testo che può copiare, cercare e scaricare in '
                  '.txt o .srt.'
                  + free_lede),
            near_top={
                'before': 'Le serve prima il testo integrato di Spotify nell’app? Veda ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'come trovare la trascrizione di Spotify e perché spesso manca (in inglese)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Link Spotify di episodio o programma',
            button=button,
            will_work=('Funziona quando il programma ha anche un feed RSS pubblico — la maggior '
                       'parte sì. Gli Original di Spotify, le esclusive e i programmi solo video, '
                       'no. Non è uno strumento per YouTube.'),
            cta_note=('Vede l’episodio e il prezzo prima di pagare qualsiasi cosa. Crea un account '
                      'gratuito dopo aver scelto l’episodio.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Trascrizione Spotify vs Podskrift',
            compare_head=('Funzione', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Leggere mentre ascolta', 'Sì, quando Spotify ne ha una',
                 'I creator possono rivedere le trascrizioni dell’episodio',
                 'Sì, nel browser dopo la trascrizione'),
                ('Copiare il testo completo', 'No — gli ascoltatori non possono copiare tutta la trascrizione',
                 'I creator possono lavorare con i propri file',
                 'Sì — copia dalla pagina dei risultati'),
                ('Scaricare .txt / .srt', 'Nessun export per gli ascoltatori',
                 'I creator idonei possono scaricare o caricare file di trascrizione',
                 'Sì — .txt e .srt'),
                ('Cercare nei suoi episodi', 'Solo follow-along in quell’episodio',
                 'Strumenti creator per il loro catalogo',
                 'Sì — cerca nella sua libreria di trascrizioni'),
                ('Quando Spotify non mostra una trascrizione', 'Nulla da leggere nell’app',
                 'Dipende da idoneità e impostazioni del creator',
                 'Funziona ancora se esiste un feed RSS/audio pubblico'),
                ('Etichette parlanti', 'Nessun export con nomi dei parlanti per gli ascoltatori',
                 'Gli strumenti creator variano',
                 'Non ancora — una trascrizione continua con timestamp'),
            ],
            compare_note=('Podskrift non presenta il testo dell’app Spotify come proprio: '
                          'trascrive l’audio pubblico. Nessun riassunto su questa pagina — '
                          'può incollare il testo in ChatGPT o Claude da solo.'),
            platform_steps_title='Come copiare un link episodio Spotify',
            platform_steps=[
                ('iPhone e iPad', [
                    'Apra l’episodio nell’app Spotify (la pagina episodio, non quella del programma).',
                    'Tocchi ⋯ (Altro), poi Condividi.',
                    'Tocchi Copia link.',
                ]),
                ('Android', [
                    'Apra l’episodio nell’app Spotify.',
                    'Tocchi ⋮ o ⋯ (Altro), poi Condividi.',
                    'Tocchi Copia link.',
                ]),
                ('App desktop e web player (open.spotify.com)', [
                    'Apra l’episodio o lo trovi nell’elenco episodi del programma.',
                    'Clicchi su ⋯ (Altre opzioni), poi Condividi.',
                    'Clicchi Copia link episodio. Inizia con open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('I nomi dei menu seguono l’aiuto di Spotify; possono cambiare '
                                 'tra versioni dell’app. Va bene anche un link del programma: '
                                 'sceglie l’episodio dall’elenco.'),
            troubleshoot_title='Quando un link Spotify non funziona',
            troubleshoot_head=('Cosa vede', 'Perché', 'Cosa fare'),
            troubleshoot=[
                ('Un’esclusiva o un Original Spotify',
                 'Il programma non ha un feed RSS pubblico, quindi nessun audio che '
                 'Podskrift possa scaricare.',
                 'Cerchi qui il nome del programma o controlli Apple Podcasts. Se non '
                 'compare nulla, non si può trascrivere.'),
                ('Un episodio solo per abbonati o a pagamento',
                 'L’audio è dietro un abbonamento, non nel feed pubblico.',
                 'Non possibile. Gli episodi gratuiti dello stesso programma di solito funzionano.'),
                ('Un episodio solo video',
                 'Non c’è una versione audio nel feed pubblico del programma.',
                 'Solo se il programma pubblica anche l’episodio in audio.'),
                ('Un link corto (spotify.link/…)',
                 'I link brevi di condivisione non sono ancora letti.',
                 'Apra il link corto nel browser, copi l’indirizzo '
                 'open.spotify.com/episode/… dalla barra degli indirizzi e lo incolli qui.'),
            ],
            example_label=('Esempio illustrativo — solo formato .txt / .srt. '
                           'Non proviene da un programma reale né da una trascrizione utente.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Lo incolli in ChatGPT o Claude per riassumere o fare domande, '
                     'oppure colleghi Podskrift perché recuperino le trascrizioni per lei.'),
            steps=[
                ('Copi il link Spotify',
                 'In Spotify apra l’episodio, tocchi Condividi (o ⋯), poi Copia link.'),
                ('Lo incolli qui',
                 'Podskrift trova l’episodio nel feed RSS pubblico e lo elenca. '
                 'Se può, selezioni « Italiano » come lingua.'),
                ('Legga o scarichi',
                 'La trascrizione richiede di solito alcuni minuti. Scarichi .txt o .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Guida: come ottenere una trascrizione podcast Spotify (in inglese)'),
            ],
            faq=[
                ('Funziona con tutti i podcast di Spotify?',
                 'Con la maggior parte sì: quasi ogni programma su Spotify pubblica anche '
                 'un feed RSS pubblico, ed è da lì che arriva l’audio. Le esclusive Spotify e '
                 'gli episodi riservati agli abbonati non hanno un feed pubblico, quindi non '
                 'si possono trascrivere. Gli URL YouTube non sono supportati.'),
                ('E se il programma è solo su Spotify?',
                 'Podskrift non può trascriverlo. Alcuni programmi che sembrano solo Spotify '
                 'pubblicano comunque un feed pubblico. Cerchi il nome qui o controlli Apple '
                 'Podcasts. Se non compare nulla, è una vera esclusiva.'),
                ('Può incollare un link del programma invece dell’episodio?',
                 'Sì. Un link del programma apre l’elenco episodi; sceglie l’episodio lì. '
                 'Funziona anche la ricerca per nome del programma.'),
                ('È la stessa cosa della trascrizione propria di Spotify?',
                 'No. Podskrift crea una propria trascrizione dall’audio — funziona anche '
                 'quando Spotify non ne mostra nessuna — e può copiarla e scaricarla.'),
                ('Può scaricare la trascrizione che Spotify mostra nell’app?',
                 'No — Spotify non esporta la trascrizione per l’ascoltatore. Incolli il link '
                 'qui per ottenere la sua copia in .txt e .srt.'),
                ('È gratis?',
                 'I nuovi account ricevono minuti gratis quando c’è un’offerta di prova — '
                 'senza carta per iniziare. Gli episodi più lunghi mostrano il prezzo esatto '
                 'prima del pagamento. Non indichiamo qui un numero fisso di minuti gratis '
                 'perché l’assegnazione può variare.'),
                cost_faq,
                ('Quali lingue sono supportate?',
                 f'{lang_count} lingue, con riconoscimento vocale moderno. Il rilevamento '
                 'automatico è l’impostazione predefinita; scelga « Italiano » per episodi in '
                 'italiano — aiuta con audio breve o con accento.'),
                ('Cosa riceve?',
                 'La trascrizione completa come testo (.txt) e come sottotitoli con timestamp '
                 '(.srt), più una vista di lettura nel browser. Le trascrizioni restano private '
                 'a meno che non crei un link di condivisione. Ancora nessuna etichetta '
                 'parlante — una trascrizione continua con timestamp.'),
                ('E se l’episodio dura più dei minuti gratis?',
                 'Riceve i primi minuti gratis come anteprima e vede il prezzo esatto del resto '
                 'prima di pagare. Pagamento unico; i minuti non scadono.'),
            ],
        )

    if endpoint == 'landing_pt_spotify':
        # Brazilian Portuguese (você-form, matching /pt/transcrever-podcast). Same
        # facts as landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Exemplo ilustrativo — não é um episódio real]\n'
            'Bem-vindos de volta. Hoje falamos de como as equipes lançam em silêncio.\n'
            'A parte interessante é o que aconteceu depois do lançamento.\n'
            'Já já chegamos aos números.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Bem-vindos de volta. Hoje falamos de como as equipes lançam em silêncio.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'A parte interessante é o que aconteceu depois do lançamento.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='pt')
        free_lede = (
            ' Cole um link e use minutos grátis quando o podcast tiver um feed '
            'público — sem cartão para começar.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Começar transcrição grátis' if offer
                  else 'Começar transcrição')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Transcrição de podcast do Spotify (.txt e .srt). Minutos grátis '
                    'com feed público; também quando o Spotify não mostra nenhuma.')
        else:
            meta = ('Transcrição de podcast do Spotify (.txt e .srt). Com feed RSS '
                    'público — também quando o Spotify não mostra nenhuma.')
        return dict(
            lang='pt',
            title='Transcrever podcast do Spotify por link',
            meta=meta,
            h1='Transcrever podcast do Spotify a partir do link',
            lede=('Cole um link de episódio ou de podcast em open.spotify.com. '
                  'O Podskrift encontra o mesmo episódio no feed RSS público do '
                  'podcast e transforma o áudio em texto que você pode copiar, '
                  'pesquisar e baixar como .txt ou .srt.'
                  + free_lede),
            near_top={
                'before': 'Precisa primeiro do texto da própria Spotify no app? Veja ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'como encontrar a transcrição da Spotify e por que muitas vezes falta (em inglês)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Link de episódio ou podcast do Spotify',
            button=button,
            will_work=('Funciona quando o podcast também tem um feed RSS público — '
                       'a maioria tem. Spotify Originals, exclusivos e podcasts só em '
                       'vídeo, não. Não é uma ferramenta para YouTube.'),
            cta_note=('Você vê o episódio e o preço antes de pagar qualquer coisa. '
                      'Você cria uma conta grátis depois de escolher o episódio.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Transcrição do Spotify vs Podskrift',
            compare_head=('Recurso', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Ler enquanto ouve', 'Sim, quando o Spotify tem uma',
                 'Criadores podem revisar transcrições do episódio',
                 'Sim, no navegador após a transcrição'),
                ('Copiar o texto completo', 'Não — ouvintes não podem copiar a transcrição inteira',
                 'Criadores podem trabalhar com os próprios arquivos',
                 'Sim — copie na página do resultado'),
                ('Baixar .txt / .srt', 'Sem exportação para ouvintes',
                 'Criadores elegíveis podem baixar ou enviar arquivos de transcrição',
                 'Sim — .txt e .srt'),
                ('Pesquisar nos seus episódios', 'Só acompanhar leitura nesse episódio',
                 'Ferramentas de criador para o catálogo deles',
                 'Sim — pesquise na sua biblioteca de transcrições'),
                ('Quando o Spotify não mostra transcrição', 'Nada para ler no app',
                 'Depende da elegibilidade e das configurações do criador',
                 'Continua funcionando se existir feed RSS/áudio público'),
                ('Identificação de falantes', 'Sem exportação com nomes para ouvintes',
                 'Ferramentas de criador variam',
                 'Ainda não — transcrição contínua com timestamps'),
            ],
            compare_note=('O Podskrift não repassa o texto do app do Spotify como '
                          'próprio: ele transcreve o áudio público. Sem resumos nesta '
                          'página — você pode colar o texto no ChatGPT ou no Claude.'),
            platform_steps_title='Como copiar um link de episódio do Spotify',
            platform_steps=[
                ('iPhone e iPad', [
                    'Abra o episódio no app do Spotify (página do episódio, não do podcast).',
                    'Toque em ⋯ (Mais) e depois em Compartilhar.',
                    'Toque em Copiar link.',
                ]),
                ('Android', [
                    'Abra o episódio no app do Spotify.',
                    'Toque em ⋮ ou ⋯ (Mais) e depois em Compartilhar.',
                    'Toque em Copiar link.',
                ]),
                ('App desktop e player web (open.spotify.com)', [
                    'Abra o episódio ou encontre-o na lista de episódios do podcast.',
                    'Clique em ⋯ (Mais opções) e depois em Compartilhar.',
                    'Clique em Copiar link do episódio. Começa com open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Os nomes dos menus seguem a ajuda do Spotify; podem '
                                 'mudar entre versões do app. Um link de podcast também '
                                 'vale: você escolhe o episódio na lista.'),
            troubleshoot_title='Quando um link do Spotify não funciona',
            troubleshoot_head=('O que você vê', 'Por quê', 'O que fazer'),
            troubleshoot=[
                ('Um exclusivo ou Original do Spotify',
                 'O podcast não tem feed RSS público, então não há áudio que o '
                 'Podskrift possa baixar.',
                 'Pesquise o nome do podcast aqui ou veja no Apple Podcasts. Se não '
                 'aparecer nada, não dá para transcrever.'),
                ('Episódio só para assinantes ou pago',
                 'O áudio está atrás de uma assinatura, não no feed público.',
                 'Não é possível. Episódios gratuitos do mesmo podcast costumam funcionar.'),
                ('Episódio só em vídeo',
                 'Não há versão em áudio no feed público do podcast.',
                 'Só se o podcast também publicar o episódio em áudio.'),
                ('Link curto (spotify.link/…)',
                 'Links curtos de compartilhamento ainda não são lidos.',
                 'Abra o link curto no navegador, copie o endereço '
                 'open.spotify.com/episode/… da barra de endereços e cole aqui.'),
            ],
            example_label=('Exemplo ilustrativo — apenas formato .txt / .srt. '
                           'Não é de um podcast real nem de transcrição de usuário.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Cole no ChatGPT ou no Claude para resumir ou perguntar, ou conecte '
                     'o Podskrift para que eles busquem transcrições para você.'),
            steps=[
                ('Copie o link do Spotify',
                 'No Spotify, abra o episódio, toque em Compartilhar (ou ⋯) e depois em Copiar link.'),
                ('Cole aqui',
                 'O Podskrift encontra o episódio no feed RSS público e lista. '
                 'Escolha «Português» como idioma, se puder.'),
                ('Leia ou baixe',
                 'A transcrição costuma levar alguns minutos. Baixe .txt ou .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Guia: como obter transcrição de podcast do Spotify (em inglês)'),
            ],
            faq=[
                ('Funciona com todos os podcasts do Spotify?',
                 'Com a maioria, sim: quase todo podcast no Spotify também publica um '
                 'feed RSS público, e o áudio vem dali. Exclusivos do Spotify e episódios '
                 'só para assinantes não têm feed público, então não dá para transcrever. '
                 'URLs do YouTube não são suportadas.'),
                ('E se o podcast for só no Spotify?',
                 'O Podskrift não consegue transcrever. Alguns podcasts que parecem só do '
                 'Spotify ainda têm feed público. Pesquise o nome aqui ou veja no Apple '
                 'Podcasts. Se não aparecer nada, é um exclusivo de verdade.'),
                ('Posso colar um link de podcast em vez de episódio?',
                 'Sim. Um link de podcast abre a lista de episódios; você escolhe ali. '
                 'Pesquisar pelo nome do podcast também funciona.'),
                ('É a mesma coisa que a transcrição da própria Spotify?',
                 'Não. O Podskrift cria sua própria transcrição a partir do áudio — também '
                 'quando o Spotify não mostra nenhuma — e você pode copiar e baixar.'),
                ('Posso baixar a transcrição que o Spotify mostra no app?',
                 'Não — o Spotify não exporta a transcrição do ouvinte. Cole o link aqui '
                 'para obter sua própria cópia em .txt e .srt.'),
                ('É grátis?',
                 'Contas novas recebem minutos grátis quando há oferta — sem cartão para '
                 'começar. Episódios mais longos mostram o preço exato antes de pagar. '
                 'Não fixamos aqui um número concreto de minutos grátis porque a '
                 'concessão pode variar.'),
                cost_faq,
                ('Quais idiomas funcionam?',
                 f'{lang_count} idiomas, com reconhecimento de voz moderno. A detecção '
                 'automática é o padrão; escolha «Português» para episódios em português — '
                 'ajuda com áudio curto ou com sotaque.'),
                ('O que eu recebo?',
                 'A transcrição completa em texto (.txt) e em legendas com timestamps '
                 '(.srt), mais uma visualização de leitura no navegador. As transcrições '
                 'ficam privadas, a menos que você crie um link para compartilhar. '
                 'Ainda sem identificação de falantes — transcrição contínua com timestamps.'),
                ('E se o episódio for mais longo que os minutos grátis?',
                 'Você recebe os primeiros minutos grátis como prévia e vê o preço exato '
                 'do restante antes de pagar. Pagamento único; os minutos não expiram.'),
            ],
        )

    if endpoint == 'landing_no_spotify':
        # Norwegian bokmål (du-form, matching /no/podcast-til-tekst). Same facts as
        # landing_spotify; own Spotify hreflang group.
        example_txt = (
            '[Illustrativt eksempel — ikke en ekte episode]\n'
            'Velkommen tilbake. I dag snakker vi om hvordan team lanserer stille.\n'
            'Det interessante er hva som skjedde etter lanseringen.\n'
            'Om et øyeblikk kommer vi til tallene.'
        )
        example_srt = (
            '1\n'
            '00:00:04,100 --> 00:00:08,400\n'
            'Velkommen tilbake. I dag snakker vi om hvordan team lanserer stille.\n'
            '\n'
            '2\n'
            '00:00:08,400 --> 00:00:11,200\n'
            'Det interessante er hva som skjedde etter lanseringen.'
        )
        cost_faq = _spotify_whole_episode_cost(
            trial, pack, episode_prices, free_trial=free_trial, lang='nb')
        free_lede = (
            ' Lim inn en lenke og bruk gratisminutter når podkasten har en offentlig '
            'feed — uten kort for å starte.'
            if (free_trial or (isinstance(trial, int) and trial > 0)) else ''
        )
        offer = bool(trial) or free_trial
        button = ('Start gratis transkripsjon' if offer
                  else 'Start transkripsjon')
        if free_trial or (isinstance(trial, int) and trial > 0):
            meta = ('Spotify-podcasttranskripsjon som .txt og .srt. Gratisminutter med '
                    'offentlig feed; også når Spotify ikke viser noen.')
        else:
            meta = ('Spotify-podcasttranskripsjon som .txt og .srt. Med offentlig '
                    'RSS-feed — også når Spotify ikke viser noen.')
        return dict(
            lang='nb',
            title='Spotify-podcast til tekst fra lenke',
            meta=meta,
            h1='Transkriber Spotify-podcast fra lenke',
            lede=('Lim inn en open.spotify.com-lenke til episode eller podkast. '
                  'Podskrift finner samme episode i podkastens offentlige RSS-feed '
                  'og gjør lyden om til tekst du kan kopiere, søke i og laste ned '
                  'som .txt eller .srt.'
                  + free_lede),
            near_top={
                'before': 'Trenger du Spotifys egen app-tekst først? Se ',
                'endpoint': 'guide_spotify_transcript',
                'anchor': 'hvordan du finner Spotifys egen transkripsjon og hvorfor den ofte mangler (engelsk)',
                'after': '.',
            },
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify-lenke til episode eller podkast',
            button=button,
            will_work=('Fungerer når podkasten også har en offentlig RSS-feed — de '
                       'fleste har det. Spotify Originals, eksklusive og kun video, ikke. '
                       'Ikke et YouTube-verktøy.'),
            cta_note=('Du ser episoden og prisen før du betaler noe. Du oppretter en '
                      'gratis konto etter at du har valgt episoden.'),
            updated=SPOTIFY_NATIVE_UPDATED,
            compare_title='Spotifys transkripsjon vs Podskrift',
            compare_head=('Funksjon', 'Spotify Read Along',
                          'Spotify for Creators', 'Podskrift'),
            compare=[
                ('Les mens du lytter', 'Ja, når Spotify har en',
                 'Skapere kan gå gjennom episodetranskripsjoner',
                 'Ja, i nettleseren etter transkripsjonen'),
                ('Kopiere full tekst', 'Nei — lyttere kan ikke kopiere hele transkripsjonen',
                 'Skapere kan jobbe med egne filer',
                 'Ja — kopier fra resultatsiden'),
                ('Laste ned .txt / .srt', 'Ingen eksport for lyttere',
                 'Kvalifiserte skapere kan laste ned eller laste opp transkripsjonsfiler',
                 'Ja — .txt og .srt'),
                ('Søke på tvers av episodene dine', 'Bare les med i denne episoden',
                 'Skaperverktøy for katalogen deres',
                 'Ja — søk i transkripsjonsbiblioteket ditt'),
                ('Når Spotify ikke viser transkripsjon', 'Ingenting å lese i appen',
                 'Avhenger av tilgang og skaperinnstillinger',
                 'Fungerer fortsatt når det finnes offentlig RSS-/lyd-feed'),
                ('Taleridentifikasjon', 'Ingen eksport med talernavn for lyttere',
                 'Skaperverktøy varierer',
                 'Ikke ennå — én sammenhengende transkripsjon med tidsstempler'),
            ],
            compare_note=('Podskrift leverer ikke Spotifys app-tekst som sin egen: den '
                          'transkriberer den offentlige lyden. Ingen sammendrag på denne '
                          'siden — du kan lime teksten inn i ChatGPT eller Claude selv.'),
            platform_steps_title='Slik kopierer du en Spotify-episodelenke',
            platform_steps=[
                ('iPhone og iPad', [
                    'Åpne episoden i Spotify-appen (episodesiden, ikke podkastsiden).',
                    'Trykk på ⋯ (Mer), deretter Del.',
                    'Trykk på Kopier lenke.',
                ]),
                ('Android', [
                    'Åpne episoden i Spotify-appen.',
                    'Trykk på ⋮ eller ⋯ (Mer), deretter Del.',
                    'Trykk på Kopier lenke.',
                ]),
                ('Skrivebordsapp og webspiller (open.spotify.com)', [
                    'Åpne episoden eller finn den i episodelisten til podkasten.',
                    'Klikk på ⋯ (Flere alternativer), deretter Del.',
                    'Klikk på Kopier episodelenke. Den starter med open.spotify.com/episode/.',
                ]),
            ],
            platform_steps_note=('Menynavn følger Spotifys egen hjelp; de kan endre seg '
                                 'mellom appversjoner. En podkastlenke fungerer også: '
                                 'du velger episoden fra listen.'),
            troubleshoot_title='Når en Spotify-lenke ikke fungerer',
            troubleshoot_head=('Hva du ser', 'Hvorfor', 'Hva du kan gjøre'),
            troubleshoot=[
                ('En ren Spotify-eksklusiv podkast eller Original',
                 'Podkasten har ingen offentlig RSS-feed, altså ingen lyd Podskrift '
                 'kan hente.',
                 'Søk etter podkastnavnet her eller sjekk Apple Podcasts. Hvis ingenting '
                 'dukker opp, kan episoden ikke transkriberes.'),
                ('Episode kun for abonnenter eller betalt',
                 'Lyden ligger bak et abonnement, ikke i den offentlige feeden.',
                 'Ikke mulig. Gratis episoder fra samme podkast fungerer som regel.'),
                ('Episode kun med video',
                 'Det finnes ingen lydversjon i podkastens offentlige feed.',
                 'Bare hvis podkasten også publiserer episoden som lyd.'),
                ('Kortlenke (spotify.link/…)',
                 'Korte delingslenker leses ikke ennå.',
                 'Åpne kortlenken i nettleseren, kopier adressen '
                 'open.spotify.com/episode/… fra adressefeltet og lim den inn.'),
            ],
            example_label=('Illustrativt eksempel — bare .txt / .srt-format. '
                           'Ikke fra en ekte podkast eller brukertranskripsjon.'),
            example_txt=example_txt,
            example_srt=example_srt,
            ai_note=('Lim inn teksten i ChatGPT eller Claude for å oppsummere eller '
                     'spørre videre, eller koble Podskrift slik at de henter '
                     'transkripsjoner for deg.'),
            steps=[
                ('Kopier Spotify-lenken',
                 'I Spotify: åpne episoden, Del (eller ⋯), deretter Kopier lenke.'),
                ('Lim den inn her',
                 'Podskrift finner episoden i den offentlige RSS-feeden og lister den. '
                 'Velg helst «Norsk» som språk.'),
                ('Les eller last ned',
                 'Transkripsjonen tar vanligvis noen minutter. Last ned .txt eller .srt.'),
            ],
            also_related=[
                ('guide_spotify_transcript',
                 'Veiledning: Spotify-podcasttranskripsjon (engelsk)'),
            ],
            faq=[
                ('Fungerer det med alle Spotify-podkaster?',
                 'Med de fleste, ja: nesten hver podkast på Spotify publiserer også en '
                 'offentlig RSS-feed, og derfra kommer lyden. Spotify-eksklusive og '
                 'episoder kun for abonnenter har ingen offentlig feed og kan derfor ikke '
                 'transkriberes. YouTube-URL-er støttes ikke.'),
                ('Hva om podkasten bare finnes på Spotify?',
                 'Podskrift kan ikke transkribere den. Noen podkaster som virker '
                 'Spotify-only har likevel offentlig feed. Søk navnet her eller sjekk '
                 'Apple Podcasts. Hvis ingenting dukker opp, er det en ren '
                 'Spotify-eksklusiv podkast.'),
                ('Kan jeg lime inn en podkastlenke i stedet for episodelenke?',
                 'Ja. En podkastlenke åpner episodelisten; der velger du episoden. '
                 'Å søke etter podkastnavnet fungerer også.'),
                ('Er det det samme som Spotifys egen transkripsjon?',
                 'Nei. Podskrift lager sin egen transkripsjon fra lyden — også når '
                 'Spotify ikke viser noen — og du kan kopiere og laste den ned.'),
                ('Kan jeg laste ned transkripsjonen fra Spotify-appen?',
                 'Nei — Spotify eksporterer ikke lyttertranskripsjonen. Lim inn lenken '
                 'her for å få din egen kopi som .txt og .srt.'),
                ('Er det gratis?',
                 'Nye kontoer får gratisminutter når det tilbys en prøve — uten kort '
                 'for å starte. Lengre episoder viser nøyaktig pris før du betaler. '
                 'Vi oppgir ikke et fast antall gratisminutter her fordi tilbudet '
                 'kan variere.'),
                cost_faq,
                ('Hvilke språk fungerer?',
                 f'{lang_count} språk, med moderne talegjenkjenning. Automatisk gjenkjenning '
                 'er standard; velg «Norsk» for norske episoder — det hjelper på kort '
                 'eller aksentert lyd.'),
                ('Hva får jeg?',
                 'Hele transkripsjonen som tekst (.txt) og som undertekster med '
                 'tidsstempler (.srt), pluss en lesevisning i nettleseren. '
                 'Transkripsjonene er private med mindre du lager en delelenke. '
                 'Ingen taleridentifikasjon ennå — én sammenhengende transkripsjon med '
                 'tidsstempler.'),
                ('Hva om episoden er lengre enn gratisminuttene?',
                 'Du får de første gratisminuttene som forhåndsvisning og ser nøyaktig '
                 'pris for resten før du betaler. Engangskjøp; minuttene utløper aldri.'),
            ],
        )

    if endpoint == 'landing_de_apple':
        free_de = _free_bit(trial, free_trial, 'de')
        return dict(
            lang='de',
            title='Apple-Podcast per Link transkribieren',
            meta=('Apple-Podcast-Link einfügen, Transkript als .txt und .srt. '
                  f'Nur öffentliche Feeds. {free_de}.'),
            h1='Apple Podcasts-Folge per Link transkribieren',
            lede=('Apple zeigt in der App für viele Shows Transkripte, aber du kannst sie nicht exportieren. Füge den Apple-Podcasts-Link hier ein und bekommst den Text als Dateien.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Apple-Podcasts-Episoden- oder Showlink',
            button=_start_button(trial, 'de', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Link kopieren',
                 'In Apple Podcasts die Folge oder Show öffnen, Teilen, dann Link kopieren.'),
                ('Hier einfügen',
                 'Podskrift liest den Feed der Show aus Apples Verzeichnis und findet die Folge.'),
                ('Lesen oder herunterladen',
                 'Transkript im Browser und als .txt oder .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Leitfaden: Apple-Podcasts-Transkript holen (Englisch)'),
            ],
            faq=[
                ('Warum nicht einfach das Transkript in der Apple-Podcasts-App?',
                 'Es bleibt in der App: du kannst beim Hören mitlesen, aber nicht das Ganze exportieren, über Folgen hinweg suchen oder in ein anderes Tool einfügen. Podskrift liefert den Text als Dateien.'),
                ('Funktionieren Folgen nur für Abonnenten (Apple Podcasts Subscriptions)?',
                 'Nein. Nur Episoden im öffentlichen Feed lassen sich transkribieren.'),
                ('Funktioniert ein Showlink?',
                 'Ja. Ein Showlink öffnet die Folgenliste, dort wählst du eine Folge.'),
                ('Was kostet es?', _price_de(trial, pack, free_trial=free_trial)),
                ('Welche Sprachen funktionieren?',
                 f'{lang_count} Sprachen, mit moderner Spracherkennung. Automatische Erkennung ist Standard; wähle die Sprache selbst bei kurzem oder akzentuiertem Audio.'),
                ('Was bekomme ich?',
                 'Die vollständige Transkription als Text (.txt) und als Untertitel mit Zeitstempeln (.srt), plus eine Leseansicht im Browser. Transkripte sind privat, es sei denn, du erstellst einen Freigabelink.'),
            ],
        )

    if endpoint == 'landing_es_apple':
        free_es = _free_bit(trial, free_trial, 'es')
        return dict(
            lang='es',
            title='Transcribir podcast de Apple Podcasts',
            meta=('Pega un enlace de Apple Podcasts y descarga .txt y .srt. '
                  f'Solo feeds públicos. {free_es.capitalize()}.'),
            h1='Transcribir un podcast de Apple Podcasts desde un enlace',
            lede=('Apple muestra transcripciones en la app de muchos programas, pero no puedes exportarlas. Pega aquí el enlace de Apple Podcasts y obtén el texto como archivos.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Enlace de episodio o programa de Apple Podcasts',
            button=_start_button(trial, 'es', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Copia el enlace',
                 'En Apple Podcasts, abre el episodio o el programa, toca Compartir y luego Copiar enlace.'),
                ('Pégalo aquí',
                 'Podskrift lee el feed del programa en el directorio de Apple y encuentra el episodio.'),
                ('Lee o descarga',
                 'Transcripción en el navegador y como .txt o .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Guía: cómo obtener una transcripción de Apple Podcasts (en inglés)'),
            ],
            faq=[
                ('¿Por qué no usar la transcripción en la app de Apple Podcasts?',
                 'Se queda en la app: puedes leer mientras escuchas, pero no exportar todo el texto, buscar entre episodios ni pegarlo en otra herramienta. Podskrift te da el texto como archivos.'),
                ('¿Funcionan los episodios solo para suscriptores (Apple Podcasts Subscriptions)?',
                 'No. Solo se pueden transcribir episodios del feed público.'),
                ('¿Funciona con un enlace de programa?',
                 'Sí. Un enlace de programa abre la lista de episodios y eliges uno ahí.'),
                ('¿Cuánto cuesta?', _price_es(trial, pack, free_trial=free_trial)),
                ('¿Qué idiomas funcionan?',
                 f'{lang_count} idiomas, con reconocimiento de voz moderno. La detección automática es la opción por defecto; elige el idioma tú mismo en audio corto o con acento.'),
                ('¿Qué recibo?',
                 'La transcripción completa como texto (.txt) y como subtítulos con marcas de tiempo (.srt), más una vista de lectura en el navegador. Las transcripciones son privadas salvo que crees un enlace para compartir.'),
            ],
        )

    if endpoint == 'landing_fr_apple':
        free_fr = _free_bit(trial, free_trial, 'fr')
        return dict(
            lang='fr',
            title='Transcrire un podcast Apple Podcasts',
            meta=('Collez un lien Apple Podcasts et téléchargez .txt et .srt. '
                  f'Flux publics uniquement. {free_fr.capitalize()}.'),
            h1='Transcrire un podcast depuis Apple Podcasts',
            lede=('Apple affiche des transcriptions dans l’app pour beaucoup d’émissions, mais vous ne pouvez pas les exporter. Collez le lien Apple Podcasts ici et obtenez le texte en fichiers.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Lien d’épisode ou d’émission Apple Podcasts',
            button=_start_button(trial, 'fr', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Copiez le lien',
                 'Dans Apple Podcasts, ouvrez l’épisode ou l’émission, Partager, puis Copier le lien.'),
                ('Collez-le ici',
                 'Podskrift lit le flux de l’émission dans l’annuaire Apple et trouve l’épisode.'),
                ('Lisez ou téléchargez',
                 'Transcription dans le navigateur et en .txt ou .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Guide : transcription Apple Podcasts (en anglais)'),
            ],
            faq=[
                ('Pourquoi ne pas utiliser la transcription dans l’app Apple Podcasts ?',
                 'Elle reste dans l’app : vous pouvez lire en écoutant, mais pas exporter l’intégralité, chercher parmi les épisodes ni la coller dans un autre outil. Podskrift vous donne le texte en fichiers.'),
                ('Les épisodes réservés aux abonnés (Apple Podcasts Subscriptions) fonctionnent-ils ?',
                 'Non. Seuls les épisodes du flux public peuvent être transcrits.'),
                ('Un lien d’émission fonctionne-t-il ?',
                 'Oui. Un lien d’émission ouvre la liste des épisodes pour en choisir un.'),
                ('Combien ça coûte ?', _price_fr(trial, pack, free_trial=free_trial)),
                ('Quelles langues sont prises en charge ?',
                 f'{lang_count} langues, avec reconnaissance vocale moderne. La détection automatique est le défaut ; choisissez la langue vous-même sur un audio court ou accentué.'),
                ('Qu’est-ce que je reçois ?',
                 'La transcription complète en texte (.txt) et en sous-titres horodatés (.srt), plus une vue de lecture dans le navigateur. Les transcriptions restent privées sauf si vous créez un lien de partage.'),
            ],
        )

    if endpoint == 'landing_nl_apple':
        free_nl = _free_bit(trial, free_trial, 'nl')
        return dict(
            lang='nl',
            title='Apple Podcasts naar tekst',
            meta=('Plak een Apple Podcasts-link en download .txt en .srt. '
                  f'Alleen openbare feeds. {free_nl.capitalize()}.'),
            h1='Podcast van Apple Podcasts transcriberen via een link',
            lede=('Apple toont in de app transcripties voor veel shows, maar u kunt ze niet exporteren. Plak de Apple Podcasts-link hier en krijg de tekst als bestanden.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Apple Podcasts-link naar aflevering of show',
            button=_start_button(trial, 'nl', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Kopieer de link',
                 'Open in Apple Podcasts de aflevering of show, tik op Delen en daarna Link kopiëren.'),
                ('Plak hem hier',
                 'Podskrift leest de feed van de show uit Apples directory en vindt de aflevering.'),
                ('Lees of download',
                 'Transcriptie in de browser en als .txt of .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Gids: Apple Podcasts-transcriptie (Engels)'),
            ],
            faq=[
                ('Waarom niet gewoon de transcriptie in de Apple Podcasts-app?',
                 'Die blijft in de app: u kunt meelezen, maar niet alles exporteren, tussen afleveringen zoeken of in een andere tool plakken. Podskrift levert de tekst als bestanden.'),
                ('Werken afleveringen alleen voor abonnees (Apple Podcasts Subscriptions)?',
                 'Nee. Alleen afleveringen in de openbare feed kunnen worden getranscribeerd.'),
                ('Werkt een showlink?',
                 'Ja. Een showlink opent de afleveringslijst om er een te kiezen.'),
                ('Wat kost het?', _price_nl(trial, pack, free_trial=free_trial)),
                ('Welke talen werken?',
                 f'{lang_count} talen, met moderne spraakherkenning. Automatische detectie is standaard; kies zelf de taal bij korte of geaccentueerde audio.'),
                ('Wat krijg ik?',
                 'De volledige transcriptie als tekst (.txt) en ondertitels met tijdstempels (.srt), plus een leesweergave in de browser. Transcripties blijven privé tenzij u een deellink aanmaakt.'),
            ],
        )

    if endpoint == 'landing_it_apple':
        free_it = _free_bit(trial, free_trial, 'it')
        return dict(
            lang='it',
            title='Trascrivere podcast da Apple Podcasts',
            meta=('Incolli un link Apple Podcasts e scarichi .txt e .srt. '
                  f'Solo feed pubblici. {free_it.capitalize()}.'),
            h1='Trascrivere un podcast da Apple Podcasts con un link',
            lede=('Apple mostra trascrizioni nell’app per molti programmi, ma non è '
                  'possibile esportarle. Incolli qui il link Apple Podcasts e ottenga '
                  'il testo come file.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Link episodio o programma Apple Podcasts',
            button=_start_button(trial, 'it', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Copi il link',
                 'In Apple Podcasts apra episodio o programma, Condividi, poi Copia link.'),
                ('Lo incolli qui',
                 'Podskrift legge il feed del programma dalla directory Apple e trova l’episodio.'),
                ('Legga o scarichi',
                 'Trascrizione nel browser e come .txt o .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Guida: trascrizione Apple Podcasts (in inglese)'),
            ],
            faq=[
                ('Perché non usare la trascrizione nell’app Apple Podcasts?',
                 'Resta nell’app: può leggere mentre ascolta, ma non esportare tutto il testo, cercare tra gli episodi o incollarlo in un altro strumento. Podskrift fornisce il testo come file.'),
                ('Gli episodi solo per abbonati (Apple Podcasts Subscriptions) funzionano?',
                 'No. Si possono trascrivere solo episodi nel feed pubblico.'),
                ('Funziona un link al programma?',
                 'Sì. Un link al programma apre l’elenco episodi per sceglierne uno.'),
                ('Quanto costa?', _price_it(trial, pack, free_trial=free_trial)),
                ('Quali lingue sono supportate?',
                 f'{lang_count} lingue, con riconoscimento vocale moderno. Il rilevamento automatico è predefinito; scelga la lingua su audio breve o con accento marcato.'),
                ('Cosa ricevo?',
                 'La trascrizione completa in testo (.txt) e come sottotitoli con timestamp (.srt), più una vista di lettura nel browser. Le trascrizioni restano private a meno che non crei un link di condivisione.'),
            ],
        )

    if endpoint == 'landing_pt_apple':
        free_pt = _free_bit(trial, free_trial, 'pt')
        return dict(
            lang='pt',
            title='Transcrever podcast do Apple Podcasts',
            meta=('Cole um link do Apple Podcasts e baixe .txt e .srt. '
                  f'Apenas feeds públicos. {free_pt.capitalize()}.'),
            h1='Transcrever podcast do Apple Podcasts pelo link',
            lede=('A Apple mostra transcrições no app de muitos podcasts, mas você não pode exportá-las. Cole o link do Apple Podcasts aqui e receba o texto em arquivos.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Link de episódio ou podcast do Apple Podcasts',
            button=_start_button(trial, 'pt', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Copie o link',
                 'No Apple Podcasts, abra o episódio ou o podcast, toque em Compartilhar e depois em Copiar link.'),
                ('Cole aqui',
                 'O Podskrift lê o feed do podcast no diretório da Apple e encontra o episódio.'),
                ('Leia ou baixe',
                 'Transcrição no navegador e como .txt ou .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Guia: transcrição do Apple Podcasts (em inglês)'),
            ],
            faq=[
                ('Por que não usar a transcrição no app Apple Podcasts?',
                 'Ela fica no app: você pode ler enquanto ouve, mas não exportar o texto inteiro, pesquisar entre episódios nem colar em outra ferramenta. O Podskrift entrega o texto em arquivos.'),
                ('Episódios só para assinantes (Apple Podcasts Subscriptions) funcionam?',
                 'Não. Só dá para transcrever episódios do feed público.'),
                ('Funciona com link de podcast?',
                 'Sim. Um link de podcast abre a lista de episódios para você escolher um.'),
                ('Quanto custa?', _price_pt(trial, pack, free_trial=free_trial)),
                ('Quais idiomas funcionam?',
                 f'{lang_count} idiomas, com reconhecimento de voz moderno. A detecção automática é o padrão; escolha o idioma em áudio curto ou com sotaque.'),
                ('O que eu recebo?',
                 'A transcrição completa em texto (.txt) e em legendas com timestamps (.srt), mais uma visualização de leitura no navegador. As transcrições ficam privadas a menos que você crie um link para compartilhar.'),
            ],
        )

    if endpoint == 'landing_no_apple':
        free_nb = _free_bit(trial, free_trial, 'nb')
        return dict(
            lang='nb',
            title='Apple Podcasts til tekst',
            meta=('Lim inn en Apple Podcasts-lenke og last ned .txt og .srt. '
                  f'Bare offentlige feeds. {free_nb.capitalize()}.'),
            h1='Transkriber podkast fra Apple Podcasts-lenke',
            lede=('Apple viser transkripsjoner i appen for mange podkaster, men du kan ikke eksportere dem. Lim inn Apple Podcasts-lenken her og få teksten som filer.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Apple Podcasts-lenke til episode eller podkast',
            button=_start_button(trial, 'nb', free_trial=free_trial),
            updated=APPLE_NATIVE_UPDATED,
            steps=[
                ('Kopier lenken',
                 'I Apple Podcasts: åpne episoden eller podkasten, Del, deretter Kopier lenke.'),
                ('Lim den inn her',
                 'Podskrift leser podkastens feed fra Apples katalog og finner episoden.'),
                ('Les eller last ned',
                 'Transkripsjon i nettleseren og som .txt eller .srt.'),
            ],
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Veiledning: Apple Podcasts-transkripsjon (engelsk)'),
            ],
            faq=[
                ('Hvorfor ikke bare bruke transkripsjonen i Apple Podcasts-appen?',
                 'Den blir i appen: du kan lese mens du lytter, men ikke eksportere hele teksten, søke på tvers av episoder eller lime den inn i et annet verktøy. Podskrift gir deg teksten som filer.'),
                ('Fungerer abonnent-eksklusive episoder (Apple Podcasts Subscriptions)?',
                 'Nei. Bare episoder i den offentlige feeden kan transkriberes.'),
                ('Fungerer en podkastlenke?',
                 'Ja. En podkastlenke åpner episodelisten slik at du kan velge en.'),
                ('Hva koster det?', _price_no(trial, pack, free_trial=free_trial)),
                ('Hvilke språk fungerer?',
                 f'{lang_count} språk, med moderne talegjenkjenning. Automatisk gjenkjenning er standard; velg språket selv på kort eller aksentert lyd.'),
                ('Hva får jeg?',
                 'Hele transkripsjonen som tekst (.txt) og undertekster med tidsstempler (.srt), pluss en lesevisning i nettleseren. Transkripsjonene er private med mindre du lager en delelenke.'),
            ],
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
            also_related=[
                ('guide_apple_podcasts_transcript',
                 'Guide: how to get an Apple Podcasts transcript'),
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
            also_related=[
                ('guide_any_podcast_transcript',
                 'Guide: how to get a transcript of any podcast episode'),
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
