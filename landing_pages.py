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
    ('landing_apple', '/apple-podcast-transcript'),
    ('landing_podcast_to_text', '/podcast-to-text'),
    ('landing_rss', '/rss-to-text'),
    ('landing_free', '/transcribe-podcast-free'),
    ('landing_youtube_vs', '/youtube-vs-podcast-transcript'),
    ('landing_summarize', '/summarize-podcast-episode'),
    ('landing_no', '/no/podcast-til-tekst'),
    ('landing_es', '/es/transcribir-podcast'),
    ('landing_de', '/de/podcast-transkribieren'),
    ('landing_pt', '/pt/transcrever-podcast'),
)

#: Compact footer column.
FOOTER = (
    'landing_generator',
    'landing_spotify',
    'landing_apple',
    'landing_podcast_to_text',
    'landing_rss',
    'landing_free',
    'landing_youtube_vs',
    'landing_summarize',
    'landing_no',
    'landing_es',
    'landing_de',
    'landing_pt',
)

#: Reciprocal hreflang group for the podcast-to-text family (x-default = en).
HREFLANG_GROUP = {
    'en': 'landing_podcast_to_text',
    'nb': 'landing_no',
    'es': 'landing_es',
    'de': 'landing_de',
    'pt': 'landing_pt',
}
#: Kept for callers/tests that still look up the old Norwegian↔English pair.
HREFLANG_PAIRS = {
    'landing_no': 'landing_podcast_to_text',
}

#: Endpoints that 404 / stay out of sitemap·footer·llms when their flag is off.
_SUMMARY_GATED = frozenset({'landing_summarize'})

SHORT_LABELS = {
    'landing_generator': 'Podcast transcript generator',
    'landing_spotify': 'Transcribe a Spotify podcast',
    'landing_apple': 'Transcribe from Apple Podcasts',
    'landing_podcast_to_text': 'Podcast to text',
    'landing_rss': 'RSS feed to text',
    'landing_free': 'Transcribe a podcast free',
    'landing_youtube_vs': 'YouTube vs podcast transcript',
    'landing_summarize': 'Summarize a podcast episode',
    'landing_no': 'Podcast til tekst (norsk)',
    'landing_es': 'Transcribir podcast (español)',
    'landing_de': 'Podcast transkribieren (Deutsch)',
    'landing_pt': 'Transcrever podcast (português)',
}

#: English labels for non-English landings in llms.txt / llms-full.txt.
LANG_LABELS_EN = {
    'nb': 'Norwegian',
    'no': 'Norwegian',
    'es': 'Spanish',
    'de': 'German',
    'pt': 'Portuguese',
}

TITLE_SUFFIX = ' — Podskrift'
TITLE_MAX = 60
META_MAX = 160

_ENDPOINT_LANG = {ep: lang for lang, ep in HREFLANG_GROUP.items()}


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
    """[(hreflang, endpoint)] for *endpoint*; full reciprocal set for the group."""
    if endpoint not in HREFLANG_GROUP.values():
        return []
    out = [(lang, ep) for lang, ep in HREFLANG_GROUP.items()]
    out.append(('x-default', HREFLANG_GROUP['en']))
    return out


def related_links(endpoint):
    """Other landings first; native pages prefer Spotify/Apple/English siblings."""
    core = [
        'landing_generator', 'landing_spotify', 'landing_apple',
        'landing_podcast_to_text', 'landing_rss', 'landing_free',
        'landing_youtube_vs',
    ]
    native = ('landing_no', 'landing_es', 'landing_de', 'landing_pt')
    if endpoint == 'landing_summarize':
        out = ['landing_spotify', 'landing_podcast_to_text', 'landing_generator',
               'landing_free', 'landing_apple']
    elif endpoint == 'landing_no':
        out = ['landing_podcast_to_text', 'landing_spotify', 'landing_apple',
               'landing_rss', 'landing_free', 'landing_generator',
               'landing_youtube_vs']
    elif endpoint in ('landing_es', 'landing_de', 'landing_pt'):
        out = ['landing_spotify', 'landing_apple', 'landing_podcast_to_text',
               'landing_rss', 'landing_free']
    else:
        out = [e for e in core if e != endpoint] + list(native)
    seen, ordered = set(), []
    for e in out:
        if e not in seen and e != endpoint and page_is_public(e):
            seen.add(e)
            ordered.append(e)
    return [(e, SHORT_LABELS[e]) for e in ordered]


def _usd(cents):
    return f'${cents // 100}' if cents % 100 == 0 else f'${cents / 100:.2f}'


def _fmt_minutes(n, lang='en'):
    """1,200 (en) / 1 200 (nb) / 1.200 (es, de, pt) for thousands."""
    if n >= 1000:
        if lang in ('no', 'nb'):
            sep = ' '
        elif lang in ('es', 'de', 'pt'):
            sep = '.'
        else:
            sep = ','
        return f'{n // 1000}{sep}{n % 1000:03d}'
    return str(n)


def pack_phrase(pack, lang='en'):
    """Price phrase for *pack* (see app._landing_pack). Matches /pricing tone."""
    if pack.get('volume'):
        lo = _usd(pack['min_cents'])
        top_min = _fmt_minutes(pack['max_minutes'], lang)
        top = _usd(pack['max_cents'])
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
        return (f'from {lo} for 60 minutes, cheaper per minute the more you buy '
                f'({top_min} min for {top}), or just the minutes one episode needs')
    if lang in ('no', 'nb'):
        return f'{pack["price"]:g} dollar for {pack["minutes"]} minutter'
    if lang == 'es':
        return f'{pack["price"]:g} dólares por {pack["minutes"]} minutos'
    if lang == 'de':
        return f'{pack["price"]:g} Dollar für {pack["minutes"]} Minuten'
    if lang == 'pt':
        return f'{pack["price"]:g} dólares por {pack["minutes"]} minutos'
    return f'${pack["price"]:g} for {pack["minutes"]} minutes'


def _start_button(trial, lang='en'):
    if lang in ('no', 'nb'):
        return 'Start gratis transkripsjon' if trial else 'Start transkripsjon'
    if lang == 'es':
        return 'Empezar transcripción gratis' if trial else 'Empezar transcripción'
    if lang == 'de':
        return 'Kostenlos transkribieren' if trial else 'Transkription starten'
    if lang == 'pt':
        return 'Começar transcrição grátis' if trial else 'Começar transcrição'
    return 'Start free transcript' if trial else 'Start transcript'


def _price_en(trial, pack):
    """One honest sentence on cost, in English."""
    parts = []
    if trial:
        parts.append(f'New accounts get {trial} minutes free, no card needed.')
    if pack:
        parts.append(f'After that, minutes are a one-time purchase: '
                     f'{pack_phrase(pack)}. No subscription.')
    parts.append('You can also add your own OpenAI API key and pay OpenAI directly '
                 'instead; that is optional.')
    return ' '.join(parts)


def _spotify_whole_episode_cost(trial, pack, episode_prices):
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
            if trial:
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
    return ('What does a whole episode cost?', _price_en(trial, pack))


def _price_no(trial, pack):
    parts = []
    if trial:
        parts.append(f'Nye kontoer får {trial} gratisminutter, uten kort.')
    if pack:
        parts.append(f'Deretter kjøper du minutter én gang: '
                     f'{pack_phrase(pack, "nb")}. Uten abonnement.')
    parts.append('Du kan også legge inn din egen OpenAI API-nøkkel og betale OpenAI '
                 'direkte. Det er valgfritt.')
    return ' '.join(parts)


def _price_es(trial, pack):
    parts = []
    if trial:
        parts.append(f'Las cuentas nuevas reciben {trial} minutos gratis, sin tarjeta.')
    if pack:
        parts.append(f'Después compras minutos con un pago único: '
                     f'{pack_phrase(pack, "es")}. Sin suscripción.')
    parts.append('También puedes añadir tu propia clave de API de OpenAI y pagar a '
                 'OpenAI directamente; es opcional.')
    return ' '.join(parts)


def _price_de(trial, pack):
    parts = []
    if trial:
        parts.append(f'Neue Konten erhalten {trial} Minuten gratis, ohne Kreditkarte.')
    if pack:
        parts.append(f'Danach kaufst du Minuten per Einmalzahlung: '
                     f'{pack_phrase(pack, "de")}. Ohne Abo.')
    parts.append('Du kannst auch deinen eigenen OpenAI-API-Schlüssel hinterlegen und '
                 'OpenAI direkt bezahlen. Das ist optional.')
    return ' '.join(parts)


def _price_pt(trial, pack):
    parts = []
    if trial:
        parts.append(f'Contas novas recebem {trial} minutos grátis, sem cartão.')
    if pack:
        parts.append(f'Depois você compra minutos com um pagamento único: '
                     f'{pack_phrase(pack, "pt")}. Sem assinatura.')
    parts.append('Você também pode adicionar sua própria chave de API da OpenAI e '
                 'pagar a OpenAI diretamente; é opcional.')
    return ' '.join(parts)


def _common_en(trial, pack, lang_count):
    return [
        ('What does it cost?', _price_en(trial, pack)),
        ('What do I get back?',
         'The full transcript as plain text (.txt) and as SubRip subtitles (.srt) '
         'with timestamps, plus a reading view in the browser. Transcripts are '
         'private to your account unless you create a share link.'),
        ('Which languages work?',
         f'{lang_count} languages, transcribed with OpenAI Whisper. Auto-detect is '
         'the default; choosing the language yourself helps on short or accented audio.'),
    ]


def build(endpoint, *, trial, pack, lang_count, episode_prices=None):
    """Return the page dict for one landing endpoint.

    trial: advertised new-account free minutes, or None when the trial is off.
    pack: app._landing_pack() dict (volume or legacy) when checkout is on, else None.
    episode_prices: optional {90|120|180: price_str} from app.episode_offer for
        Spotify cost copy; omit or pass None when volume pricing is off.
    """
    free_en = f'{trial} minutes free' if trial else 'a free account'
    common = _common_en(trial, pack, lang_count)
    start = _start_button(trial)
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
                  'Podskrift fetches the audio and runs OpenAI Whisper.'),
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
                 'Yes — OpenAI Whisper turns the audio into text. You do not need to '
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
        if trial:
            spotify_meta = (f'Paste a Spotify episode link and get the transcript as .txt '
                            f'and .srt. Works when the show has a public RSS feed. '
                            f'{trial} minutes free.')
        else:
            spotify_meta = ('Paste a Spotify episode link and get the transcript as .txt '
                            'and .srt. Works when the show has a public RSS feed.')
        # Invented sample — not from a real show.
        example_txt = (
            'And then she said the quiet part out loud.\n'
            'We sat with that for a moment.\n'
            'Anyway — back to the story.'
        )
        example_srt = (
            '12\n'
            '00:12:31,200 --> 00:12:35,900\n'
            'Anyway — back to the story.'
        )
        cost_faq = _spotify_whole_episode_cost(trial, pack, episode_prices)
        # Drop the generic "What does it cost?" — Spotify has the episode FAQ.
        common_rest = [qa for qa in common if qa[0] != 'What does it cost?']
        return dict(
            lang='en',
            title='Transcribe a Spotify Podcast to Text',
            meta=spotify_meta,
            h1='Transcribe a Spotify podcast from a link',
            lede=('Paste an open.spotify.com episode or show link. Podskrift finds the '
                  'same episode in the show’s public RSS feed and turns the audio into '
                  'text you can copy and download.'),
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify episode or show link',
            button=start,
            will_work=('Works when the show also has a public RSS feed — most do. '
                       'Spotify Originals, exclusives and video-only shows don’t.'),
            cta_note=('You create a free account after you pick the episode.'),
            example_label='Example format',
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
                 'No — Spotify doesn’t export it. Paste the link here to get your own '
                 'copy as .txt and .srt.'),
                cost_faq,
            ] + common_rest,
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
                 'It uses OpenAI Whisper, which is good on clear speech. Crosstalk, heavy '
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
        if trial:
            free_answer = (f'New accounts get {trial} minutes of transcription free, no card '
                           'needed. A longer first episode still starts: you get the first '
                           f'{trial} minutes as a preview and can buy minutes to finish it.')
            h1 = f'Transcribe a podcast free: {trial} minutes on us'
            title = f'Transcribe a Podcast Free: {trial} Minutes, No Card'
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
            button='Try it free' if trial else 'Start transcript',
            steps=[
                ('Paste a link', 'Spotify, Apple Podcasts or RSS. Searching by name works too.'),
                ('Create an account', 'Email and password. No card for the free minutes.'),
                ('Get the transcript', 'Download .txt and .srt; the transcript stays in your history.'),
            ],
            faq=[
                ('Is it really free?', free_answer),
                ('What happens when the free minutes run out?', _price_en(None, pack)),
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
        free_no = f'{trial} gratisminutter' if trial else 'gratis konto'
        faq = [
            ('Hva koster det?', _price_no(trial, pack)),
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
            button=_start_button(trial, 'nb'),
            steps=[
                ('Kopier lenken', 'I Spotify eller Apple Podcasts: Del, deretter Kopier lenke.'),
                ('Lim den inn', 'Velg episoden og gjerne «Norsk» som språk.'),
                ('Få teksten', 'Les i nettleseren eller last ned .txt og .srt.'),
            ],
            faq=faq,
        )

    if endpoint == 'landing_es':
        # Neutral Spanish for Spain and Latin America; Spotify-first (≈89% of jobs).
        free_es = f'{trial} minutos gratis' if trial else 'cuenta gratuita'
        faq = [
            ('¿Cuánto cuesta?', _price_es(trial, pack)),
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
            button=_start_button(trial, 'es'),
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
        free_de = f'{trial} Minuten gratis' if trial else 'Kostenloses Konto'
        faq = [
            ('Was kostet es?', _price_de(trial, pack)),
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
            button=_start_button(trial, 'de'),
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
        free_bit = f'{trial} minutes free' if trial else 'a free account'
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
                ('What does it cost?', _price_en(trial, pack)),
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
                 'No. Only the transcription uses trial or paid minutes. The summary runs '
                 'on the same OpenAI key that funded the transcript and is not metered '
                 'against your balance.'),
                ('What if I only get a free preview of a long episode?',
                 'The summary covers only the part that was transcribed. A “Preview” badge '
                 'marks that the summary is partial.'),
                ('Can I get a different kind of summary?',
                 'Copy the transcript into ChatGPT or Claude for chapter notes, study notes '
                 'or a tweet thread, or connect Podskrift as a connector so they can fetch '
                 'transcripts for you. See the AI and ChatGPT guide links below.'),
                ('Is this the same as Spotify’s or ChatGPT’s own summary?',
                 'No. Spotify’s in-app summary (when it exists) stays in the app. ChatGPT '
                 'cannot listen to the episode audio. Podskrift transcribes the public-feed '
                 'audio itself, then summarises that transcript.'),
                ('Is there speaker labelling?',
                 'Not yet. You get one continuous transcript with timestamps, not named speakers.'),
                ('Do I need an OpenAI API key?',
                 'No. An API key is optional; you can use the free minutes or buy minutes '
                 'instead.'),
            ],
        )

    if endpoint == 'landing_pt':
        # Brazilian Portuguese, Spotify-first; hreflang "pt" (group pattern).
        free_pt = f'{trial} minutos grátis' if trial else 'conta gratuita'
        faq = [
            ('Quanto custa?', _price_pt(trial, pack)),
            ('Funciona bem em português?',
             'Sim, quando a fala está clara — no Brasil e em Portugal. Se o episódio '
             'mistura português e inglês, tem sotaques marcados ou várias pessoas falam '
             'ao mesmo tempo, escolha «Português» em vez da detecção automática.'),
            ('Funciona com todos os podcasts do Spotify?',
             'Com a maioria, sim: quase todos também têm um feed RSS público, e é daí '
             'que sai o áudio. Exclusivos do Spotify e episódios só para assinantes não '
             'têm feed público, então não dá para transcrever.'),
            ('Preciso enviar o arquivo de áudio?',
             'Não. Cole um link do Spotify ou do Apple Podcasts, ou a URL de um feed RSS, '
             'e o Podskrift baixa o áudio do feed público do programa.'),
            ('O que eu recebo?',
             'A transcrição completa como texto (.txt) e como legendas com marcas de '
             'tempo (.srt). Dá para copiar e colar no ChatGPT ou no Claude. As '
             'transcrições são privadas na sua conta, a menos que você crie um link para '
             'compartilhar.'),
            ('Preciso de chave de API ou assinatura?',
             'Não. Não é obrigatório ter chave da OpenAI, e não há assinatura — os '
             'minutos são compra única.'),
        ]
        return dict(
            lang='pt',
            title='Transcrever podcast a texto a partir do link',
            meta=('Passe um podcast para texto sem enviar áudio. Cole o link do Spotify '
                  f'ou Apple Podcasts e baixe .txt e .srt. {free_pt.capitalize()}.'),
            h1='Transcrever um podcast a texto a partir do link',
            lede=('Cole o link do episódio do Spotify ou Apple Podcasts. O Podskrift '
                  'encontra o episódio no feed público do programa e transforma o áudio '
                  'em texto que você pode copiar e baixar.'),
            placeholder='Cole o link do episódio do Spotify ou Apple Podcasts',
            input_label='Link do Spotify, Apple Podcasts ou RSS',
            button=_start_button(trial, 'pt'),
            steps=[
                ('Copie o link',
                 'No Spotify ou Apple Podcasts: «Compartilhar» e depois «Copiar link».'),
                ('Cole aqui',
                 'Escolha o episódio e, se puder, selecione «Português» como idioma.'),
                ('Receba o texto',
                 'Leia no navegador ou baixe .txt e .srt.'),
            ],
            faq=faq,
        )
    raise KeyError(endpoint)


UI = {
    'en': dict(how='How it works', faq='Questions', related='More ways to transcribe',
               ai='Use in ChatGPT & Claude', guides='Setup guides', rss_help='How to find an RSS feed',
               pricing='Pricing', shows='Popular podcasts',
               note='Searching by podcast name works too.'),
    'nb': dict(how='Slik fungerer det', faq='Spørsmål og svar', related='Mer om Podskrift',
               ai='Bruk i ChatGPT og Claude', guides='Veiledninger', rss_help='Finn RSS-feeden',
               pricing='Priser', shows='Populære podkaster',
               note='Du kan også søke etter navnet på podkasten.'),
    'es': dict(how='Cómo funciona', faq='Preguntas', related='Más formas de transcribir',
               ai='Usar en ChatGPT y Claude', guides='Guías de configuración',
               rss_help='Cómo encontrar un feed RSS',
               pricing='Precios', shows='Podcasts populares',
               note='También puedes buscar por el nombre del podcast.'),
    'de': dict(how='So funktioniert es', faq='Fragen', related='Weitere Wege zur Transkription',
               ai='In ChatGPT und Claude nutzen', guides='Einrichtungsanleitungen',
               rss_help='RSS-Feed finden',
               pricing='Preise', shows='Beliebte Podcasts',
               note='Du kannst auch nach dem Podcastnamen suchen.'),
    'pt': dict(how='Como funciona', faq='Perguntas', related='Mais formas de transcrever',
               ai='Usar no ChatGPT e no Claude', guides='Guias de configuração',
               rss_help='Como encontrar um feed RSS',
               pricing='Preços', shows='Podcasts populares',
               note='Você também pode buscar pelo nome do podcast.'),
    'no': None,
}
UI['no'] = UI['nb']
