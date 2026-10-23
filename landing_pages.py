"""Keyword landing pages (/spotify-podcast-transcript, /no/podcast-til-tekst, …).

One template (templates/landing.html), one definition per page here. Every
price or trial fact is passed in at request time from the same settings the
app enforces, so the copy cannot drift from what checkout and the trial do.

Each page answers the question its URL asks, with facts specific to that
intent (Spotify exclusives, Apple export limits, language tips, YouTube vs
podcast). No page is a synonym-swapped copy of another.
"""

# (endpoint, path) — full set for sitemap / related links / uniqueness tests.
PAGES = (
    ('landing_generator', '/podcast-transcript-generator'),
    ('landing_spotify', '/spotify-podcast-transcript'),
    ('landing_apple', '/apple-podcast-transcript'),
    ('landing_podcast_to_text', '/podcast-to-text'),
    ('landing_rss', '/rss-to-text'),
    ('landing_free', '/transcribe-podcast-free'),
    ('landing_youtube_vs', '/youtube-vs-podcast-transcript'),
    ('landing_lang_en', '/podcast-to-text/english'),
    ('landing_lang_es', '/podcast-to-text/spanish'),
    ('landing_lang_de', '/podcast-to-text/german'),
    ('landing_lang_fr', '/podcast-to-text/french'),
    ('landing_lang_pt', '/podcast-to-text/portuguese'),
    ('landing_no', '/no/podcast-til-tekst'),
)

#: Compact footer column — language variants are linked from related + /podcast-to-text.
FOOTER = (
    'landing_generator',
    'landing_spotify',
    'landing_apple',
    'landing_podcast_to_text',
    'landing_rss',
    'landing_free',
    'landing_youtube_vs',
    'landing_no',
)

#: English content about transcribing *in* that spoken language (not UI locales).
LANG_PAGES = (
    'landing_lang_en',
    'landing_lang_es',
    'landing_lang_de',
    'landing_lang_fr',
    'landing_lang_pt',
)

#: Norwegian page <-> English podcast-to-text (reciprocal hreflang).
HREFLANG_PAIRS = {
    'landing_no': 'landing_podcast_to_text',
}

SHORT_LABELS = {
    'landing_generator': 'Podcast transcript generator',
    'landing_spotify': 'Transcribe a Spotify podcast',
    'landing_apple': 'Transcribe from Apple Podcasts',
    'landing_podcast_to_text': 'Podcast to text',
    'landing_rss': 'RSS feed to text',
    'landing_free': 'Transcribe a podcast free',
    'landing_youtube_vs': 'YouTube vs podcast transcript',
    'landing_lang_en': 'Podcast to text in English',
    'landing_lang_es': 'Podcast to text in Spanish',
    'landing_lang_de': 'Podcast to text in German',
    'landing_lang_fr': 'Podcast to text in French',
    'landing_lang_pt': 'Podcast to text in Portuguese',
    'landing_no': 'Podcast til tekst (norsk)',
}


def lang_of(endpoint):
    return 'nb' if endpoint == 'landing_no' else 'en'


def hreflang_alternates(endpoint):
    """[(hreflang, endpoint)] for *endpoint*, reciprocal by construction."""
    en_to_nb = {en: nb for nb, en in HREFLANG_PAIRS.items()}
    if endpoint in HREFLANG_PAIRS:
        nb, en = endpoint, HREFLANG_PAIRS[endpoint]
    elif endpoint in en_to_nb:
        nb, en = en_to_nb[endpoint], endpoint
    else:
        return []
    return [('en', en), ('nb', nb), ('x-default', en)]


def related_links(endpoint):
    """Curated same-language related pages (keeps the list scannable)."""
    core = [
        'landing_generator', 'landing_spotify', 'landing_apple',
        'landing_podcast_to_text', 'landing_rss', 'landing_free',
        'landing_youtube_vs',
    ]
    if endpoint in LANG_PAGES or endpoint == 'landing_podcast_to_text':
        peers = [e for e in LANG_PAGES if e != endpoint]
        if endpoint != 'landing_podcast_to_text':
            peers = ['landing_podcast_to_text'] + peers
        out = peers + [e for e in core if e != endpoint and e not in peers]
    elif endpoint == 'landing_no':
        out = ['landing_podcast_to_text', 'landing_spotify', 'landing_apple',
               'landing_rss', 'landing_free', 'landing_generator']
    else:
        out = [e for e in core if e != endpoint] + list(LANG_PAGES[:3])
        if endpoint != 'landing_no':
            out.append('landing_no')
    # Dedupe while preserving order.
    seen, ordered = set(), []
    for e in out:
        if e not in seen and e != endpoint:
            seen.add(e)
            ordered.append(e)
    return [(e, SHORT_LABELS[e]) for e in ordered]


def _usd(cents):
    return f'${cents // 100}' if cents % 100 == 0 else f'${cents / 100:.2f}'


def pack_phrase(pack, lang='en'):
    """Price phrase for *pack* (see app._landing_pack)."""
    if pack.get('volume'):
        lo, top_min, top = _usd(pack['min_cents']), pack['max_minutes'], _usd(pack['max_cents'])
        if lang in ('no', 'nb'):
            return (f'fra {lo}, med volumrabatt opptil {top_min} min for {top}, '
                    f'eller per episode')
        return f'from {lo}, volume discounts up to {top_min} min for {top}, or per episode'
    if lang in ('no', 'nb'):
        return f'{pack["price"]:g} dollar for {pack["minutes"]} minutter'
    return f'${pack["price"]:g} for {pack["minutes"]} minutes'


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


def _common_en(trial, pack, lang_count):
    return [
        ('What does it cost?', _price_en(trial, pack)),
        ('What do I get back?',
         'The full transcript as plain text (.txt) and as SubRip subtitles (.srt) '
         'with timestamps, plus a reading view in the browser. Transcripts are '
         'private to your account.'),
        ('Which languages work?',
         f'{lang_count} languages, transcribed with OpenAI Whisper. Auto-detect is '
         'the default; choosing the language yourself helps on short or accented audio.'),
    ]


def _lang_page(*, code, english, native, trial, pack, lang_count):
    """English landing for “podcast to text in <Language>”."""
    free_en = f'{trial} minutes free' if trial else 'a free account'
    tip = {
        'en': ('English is Whisper’s strongest language. Still pick “English” in the '
               'language menu when the episode is clearly English — auto-detect '
               'sometimes mislabels short intros or music beds.'),
        'es': ('Spanish (Castilian and Latin American) works well on clear speech. '
               'Pick “Spanish” rather than auto-detect when the show mixes Spanish '
               'with English guest segments.'),
        'de': ('German works well on studio speech. Strong dialects and heavy '
               'crosstalk add errors; naming “German” beats auto-detect on short clips.'),
        'fr': ('French (France and Canadian) is solid on clear speech. Pick “French” '
               'when guests switch into English mid-episode so Whisper stays on French.'),
        'pt': ('Portuguese (Brazil and Portugal) works well. Choose “Portuguese” '
               'explicitly — auto-detect sometimes confuses it with Spanish on short audio.'),
    }[code]
    return dict(
        lang='en',
        title=f'Podcast to text in {english}: transcribe {english}-language episodes',
        meta=(f'Transcribe a {english}-language podcast to text. Paste a Spotify, Apple '
              f'Podcasts or RSS link, choose {english} in the language picker, get .txt '
              f'and .srt. {free_en.capitalize()}.'),
        h1=f'Podcast to text in {english}',
        lede=(f'Paste a link to a {english}-language episode ({native}). Podskrift '
              f'downloads the public audio and returns searchable text with timestamps — '
              f'no file upload.'),
        placeholder='Paste a Spotify, Apple or RSS link',
        input_label='Podcast or episode link',
        button='Start free transcript',
        steps=[
            ('Paste a link or search',
             'Spotify, Apple Podcasts, an RSS feed, or search by show name.'),
            (f'Choose {english}',
             f'Set the language picker to {english} ({native}) instead of auto-detect.'),
            ('Read or download',
             'Get the full transcript in the browser, plus .txt and .srt.'),
        ],
        faq=[
            (f'How good is {english} transcription?', tip),
            ('Does Podskrift translate into another language?',
             'No. It transcribes what was spoken. For a translation, paste the '
             'transcript into ChatGPT or Claude after you have the text.'),
            ('Can I use this from ChatGPT or Claude?',
             'Yes. Connect Podskrift as an MCP connector and ask for the episode; '
             'the same language tip applies when you start a new transcription.'),
        ] + _common_en(trial, pack, lang_count),
    )


def build(endpoint, *, trial, pack, lang_count):
    """Return the page dict for one landing endpoint.

    trial: advertised new-account free minutes, or None when the trial is off.
    pack: app._landing_pack() dict (volume or legacy) when checkout is on, else None.
    """
    free_en = f'{trial} minutes free' if trial else 'a free account'
    common = _common_en(trial, pack, lang_count)

    if endpoint == 'landing_generator':
        return dict(
            lang='en',
            title='Podcast transcript generator: paste a link, get searchable text',
            meta=('Generate a podcast transcript from a Spotify, Apple Podcasts or RSS '
                  f'link — no audio upload. Download .txt and .srt. {free_en.capitalize()}.'),
            h1='Podcast transcript generator',
            lede=('Turn any public podcast episode into text you can search, quote and '
                  'paste into ChatGPT or Claude. Paste a link or search by show name; '
                  'Podskrift fetches the audio and runs OpenAI Whisper.'),
            placeholder='Paste a Spotify, Apple or RSS link',
            input_label='Podcast or episode link',
            button='Start free transcript',
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
        return dict(
            lang='en',
            title='Transcribe a Spotify podcast: paste the episode link, get the text',
            meta=('Transcribe a Spotify podcast episode to text. Paste the open.spotify.com '
                  'link; Podskrift finds the public RSS feed and runs Whisper. Spotify-only '
                  f'exclusives are not supported — see how to work around that. '
                  f'{free_en.capitalize()}.'),
            h1='Transcribe a Spotify podcast from a link',
            lede=('Paste an open.spotify.com episode (or show) link. Podskrift looks up the '
                  'same episode in the show’s public RSS feed and transcribes that audio — '
                  'Spotify’s in-app transcript is not exported.'),
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify episode or show link',
            button='Start free transcript',
            steps=[
                ('Copy the Spotify link',
                 'In Spotify, open the episode, tap Share (or ⋯), then Copy link.'),
                ('Paste it here',
                 'Podskrift matches the episode in the show’s public feed and lists it.'),
                ('Read or download',
                 'Transcription usually takes a few minutes. Download .txt or .srt.'),
            ],
            faq=[
                ('Does every Spotify podcast work?',
                 'Most do: almost every show on Spotify also publishes a public RSS feed, '
                 'and that is where the audio comes from. Spotify exclusives and '
                 'subscriber-only episodes have no public feed, so they cannot be transcribed.'),
                ('What if the show is Spotify-only?',
                 'Podskrift cannot pull audio that only exists inside Spotify. Workarounds '
                 'that work today: find the same show on Apple Podcasts or its host site '
                 'and paste that link, or paste the public RSS URL if the creator publishes '
                 'one. Searching by show name sometimes finds the Apple / RSS listing.'),
                ('Can I paste a show link instead of an episode link?',
                 'Yes. A show link opens the episode list, and you pick the episode there.'),
                ('Is this the same as Spotify’s own transcript?',
                 'No. Podskrift makes its own transcript from the audio, so it also works '
                 'for shows where Spotify shows none, and you can copy and download it.'),
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
            lede=('Apple shows transcripts inside its app for some shows, but you cannot '
                  'export them. Paste the Apple Podcasts link here and get the text as files.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Apple Podcasts episode or show link',
            button='Start free transcript',
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
            title='Podcast to text: transcribe an episode from a link, no upload',
            meta=('Convert a podcast episode to text without downloading the audio. Paste a '
                  'Spotify, Apple Podcasts or RSS link, or search by name. Get .txt and .srt.'),
            h1='Podcast to text, straight from a link',
            lede=('No audio file to find, download or upload. Paste a link or search for the '
                  'show, pick the episode, and Podskrift fetches the audio and transcribes it.'),
            placeholder='Paste a Spotify, Apple or RSS link',
            input_label='Podcast or episode link',
            button='Start free transcript',
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
                ('What about other languages?',
                 'English, Spanish, German, French, Portuguese and Norwegian each have their '
                 'own page under “More ways to transcribe” below — pick the spoken language '
                 'in the picker for best results.'),
            ] + common,
        )

    if endpoint == 'landing_rss':
        return dict(
            lang='en',
            title='RSS to text: transcribe podcast episodes from a feed URL',
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
            title = f'Transcribe a podcast for free: {trial} free minutes, no card'
        else:
            free_answer = ('There is no free trial right now. You can still transcribe at cost '
                           'by adding your own OpenAI API key.')
            h1 = 'Transcribe a podcast for free or at cost'
            title = 'Transcribe a podcast free or at cost: what it really costs'
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
            button='Try it free' if trial else 'Start',
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
                 'Spotify and Apple show transcripts in their apps for some shows, free but not '
                 'exportable. Running Whisper yourself is free if you have the hardware and are '
                 'comfortable with the command line.'),
            ] + common[1:],
        )

    if endpoint == 'landing_youtube_vs':
        return dict(
            lang='en',
            title='YouTube vs podcast transcript: what Podskrift is for',
            meta=('YouTube has captions; most podcasts do not publish exportable transcripts. '
                  'Podskrift transcribes podcast episodes from Spotify, Apple Podcasts or RSS — '
                  'not YouTube videos. Honest comparison and the workaround when a show is on both.'),
            h1='YouTube captions vs a podcast transcript',
            lede=('YouTube already gives most videos auto-captions. Podcasts usually do not '
                  'publish a transcript you can download. Podskrift is built for that podcast '
                  'gap — it is not a YouTube downloader or YouTube caption tool.'),
            placeholder='Paste a Spotify, Apple or RSS podcast link',
            input_label='Podcast or episode link (not a YouTube URL)',
            button='Start free transcript',
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

    if endpoint == 'landing_lang_en':
        return _lang_page(code='en', english='English', native='English',
                          trial=trial, pack=pack, lang_count=lang_count)
    if endpoint == 'landing_lang_es':
        return _lang_page(code='es', english='Spanish', native='Español',
                          trial=trial, pack=pack, lang_count=lang_count)
    if endpoint == 'landing_lang_de':
        return _lang_page(code='de', english='German', native='Deutsch',
                          trial=trial, pack=pack, lang_count=lang_count)
    if endpoint == 'landing_lang_fr':
        return _lang_page(code='fr', english='French', native='Français',
                          trial=trial, pack=pack, lang_count=lang_count)
    if endpoint == 'landing_lang_pt':
        return _lang_page(code='pt', english='Portuguese', native='Português',
                          trial=trial, pack=pack, lang_count=lang_count)

    if endpoint == 'landing_no':
        free_no = f'{trial} gratisminutter' if trial else 'gratis konto'
        faq = [
            ('Hva koster det?', _price_no(trial, pack)),
            ('Hvor godt fungerer det på norsk?',
             'Godt på tydelig bokmål og nynorsk. Sterk dialekt, mye prat i munnen på '
             'hverandre og musikk under tale gir flere feil. Velg «Norsk» som språk i '
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
             '(.srt). Transkripsjonene er private for kontoen din.'),
        ]
        return dict(
            lang='nb',
            title='Podcast til tekst: transkriber norske podkaster fra en lenke',
            meta=('Gjør en podkastepisode om til tekst uten å laste opp lydfil. Lim inn lenke '
                  f'fra Spotify, Apple Podcasts eller RSS. Støtter norsk. {free_no.capitalize()}.'),
            h1='Podcast til tekst, rett fra en lenke',
            lede=('Lim inn lenken til episoden, så får du hele teksten med tidsstempler. Du '
                  'trenger ikke finne og laste opp lydfilen selv.'),
            placeholder='Lim inn lenke fra Spotify, Apple eller RSS',
            input_label='Lenke til podkast eller episode',
            button='Start gratis transkripsjon',
            steps=[
                ('Kopier lenken', 'I Spotify eller Apple Podcasts: Del, deretter Kopier lenke.'),
                ('Lim den inn', 'Velg episoden og gjerne «Norsk» som språk.'),
                ('Få teksten', 'Les i nettleseren eller last ned .txt og .srt.'),
            ],
            faq=faq,
        )
    raise KeyError(endpoint)


UI = {
    'en': dict(how='How it works', faq='Questions', related='More ways to transcribe',
               ai='Use in ChatGPT & Claude', guides='Setup guides', rss_help='How to find an RSS feed',
               pricing='Pricing', shows='Popular podcasts',
               note='Searching by podcast name works too.',
               cta_hint='Free trial minutes on a new account — no card.'),
    'nb': dict(how='Slik fungerer det', faq='Spørsmål og svar', related='Mer om Podskrift',
               ai='Bruk i ChatGPT og Claude', guides='Veiledninger', rss_help='Finn RSS-feeden',
               pricing='Priser', shows='Populære podkaster',
               note='Du kan også søke etter navnet på podkasten.',
               cta_hint='Gratisminutter på ny konto — uten kort.'),
    # Back-compat if any caller still asks for lang='no'.
    'no': None,
}
UI['no'] = UI['nb']
