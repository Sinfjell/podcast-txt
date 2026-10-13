"""Keyword landing pages (/spotify-podcast-transcript, /no/podcast-til-tekst, ...).

One template (templates/landing.html), one definition per page here. Every
price or trial fact is passed in at request time from the same settings the
app enforces, so the copy cannot drift from what checkout and the trial do.

Each page answers the question its URL asks, with facts specific to that
source (what a Spotify link can and cannot give us, where an Apple link
points, what an RSS feed is). No page is a synonym-swapped copy of another.
"""

# (endpoint, path) in footer / related-links order.
PAGES = (
    ('landing_spotify', '/spotify-podcast-transcript'),
    ('landing_apple', '/apple-podcast-transcript'),
    ('landing_podcast_to_text', '/podcast-to-text'),
    ('landing_rss', '/rss-to-text'),
    ('landing_free', '/transcribe-podcast-free'),
    ('landing_no', '/no/podcast-til-tekst'),
    ('landing_no_transkribere', '/no/transkribere-podcast'),
    ('landing_no_tale', '/no/tale-til-tekst'),
    ('landing_no_gratis', '/no/transkribering-gratis'),
    ('landing_no_spotify', '/no/spotify-podcast-til-tekst'),
)

#: hreflang code for the Norwegian pages. Bokmål ("nb") everywhere: the
#: <html lang>, <link rel=alternate>, sitemap xhtml:link and footer anchors.
NB = 'nb'

NB_PAGES = ('landing_no', 'landing_no_transkribere', 'landing_no_tale',
            'landing_no_gratis', 'landing_no_spotify')

#: Norwegian page -> its English equivalent. Only true equivalents are paired
#: (hreflang must be reciprocal and one-to-one); the others self-reference.
HREFLANG_PAIRS = {
    'landing_no': 'landing_podcast_to_text',
    'landing_no_spotify': 'landing_spotify',
    'landing_no_gratis': 'landing_free',
}

#: Footer shows every English page plus one Norwegian entry point; the other
#: Norwegian pages are linked from the Norwegian pages themselves.
FOOTER = tuple(e for e, _ in PAGES if e not in NB_PAGES) + ('landing_no',)

SHORT_LABELS = {
    'landing_spotify': 'Spotify podcast transcript',
    'landing_apple': 'Apple Podcasts transcript',
    'landing_podcast_to_text': 'Podcast to text',
    'landing_rss': 'RSS feed to text',
    'landing_free': 'Transcribe a podcast free',
    'landing_no': 'Podcast til tekst (norsk)',
    'landing_no_transkribere': 'Transkribere podcast',
    'landing_no_tale': 'Tale til tekst',
    'landing_no_gratis': 'Transkribering gratis',
    'landing_no_spotify': 'Spotify-podcast til tekst',
}


def lang_of(endpoint):
    return NB if endpoint in NB_PAGES else 'en'


def hreflang_alternates(endpoint):
    """[(hreflang, endpoint)] for *endpoint*, reciprocal by construction."""
    en_to_nb = {en: nb for nb, en in HREFLANG_PAIRS.items()}
    if endpoint in HREFLANG_PAIRS:
        nb, en = endpoint, HREFLANG_PAIRS[endpoint]
    elif endpoint in en_to_nb:
        nb, en = en_to_nb[endpoint], endpoint
    elif endpoint in NB_PAGES:
        return [(NB, endpoint)]
    else:
        return []
    return [('en', en), (NB, nb), ('x-default', en)]


def related_links(endpoint):
    """Same-language pages first, then the other-language equivalent."""
    lang = lang_of(endpoint)
    out = [e for e, _ in PAGES if e != endpoint and lang_of(e) == lang]
    out += [e for hl, e in hreflang_alternates(endpoint)
            if hl not in (lang, 'x-default') and e not in out]
    return [(e, SHORT_LABELS[e]) for e in out]


def _usd(cents):
    return f'${cents // 100}' if cents % 100 == 0 else f'${cents / 100:.2f}'


def pack_phrase(pack, lang='en'):
    """Price phrase for *pack* (see app._landing_pack)."""
    if pack.get('volume'):
        lo, top_min, top = _usd(pack['min_cents']), pack['max_minutes'], _usd(pack['max_cents'])
        if lang in ('no', NB):
            return (f'fra {lo}, med volumrabatt opptil {top_min} min for {top}, '
                    f'eller per episode')
        return f'from {lo}, volume discounts up to {top_min} min for {top}, or per episode'
    if lang in ('no', NB):
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
                     f'{pack_phrase(pack, "no")}. Uten abonnement.')
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


def build(endpoint, *, trial, pack, lang_count):
    """Return the page dict for one landing endpoint.

    trial: advertised new-account free minutes, or None when the trial is off.
    pack: app._landing_pack() dict (volume or legacy) when checkout is on, else None.
    """
    free_en = f'{trial} minutes free' if trial else 'a free account'
    common = _common_en(trial, pack, lang_count)

    if endpoint == 'landing_spotify':
        return dict(
            lang='en',
            title='Spotify podcast transcript: paste an episode link, get the text',
            meta=('Get a transcript of a Spotify podcast episode. Paste the open.spotify.com '
                  f'episode link; Podskrift finds the public feed and transcribes it. {free_en.capitalize()}.'),
            h1='Get a transcript of any Spotify podcast episode',
            lede=('Spotify only shows its own transcripts for some shows, and you cannot '
                  'copy or download them. Paste the episode link here and get the full '
                  'text with timestamps.'),
            placeholder='https://open.spotify.com/episode/…',
            input_label='Spotify episode link',
            button='Get transcript',
            steps=[
                ('Copy the link', 'In Spotify, tap ⋯ or Share on the episode, then Copy link to episode.'),
                ('Paste it here', 'Podskrift looks up the same episode in the show’s public feed.'),
                ('Read or download', 'Transcription usually takes a few minutes. Download .txt or .srt.'),
            ],
            faq=[
                ('Does every Spotify podcast work?',
                 'Most do: almost every show on Spotify also publishes a public RSS feed, '
                 'and that is where the audio comes from. Spotify exclusives and '
                 'subscriber-only episodes have no public feed, so they cannot be transcribed.'),
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
            title='Apple Podcasts transcript: copy, download and search the text',
            meta=('Turn an Apple Podcasts episode into text you can copy and download. Paste '
                  f'the podcasts.apple.com link and get .txt and .srt. {free_en.capitalize()}.'),
            h1='Apple Podcasts transcripts you can copy and keep',
            lede=('Apple shows transcripts inside its app, but you cannot export them or '
                  'use them elsewhere. Paste the episode link and get the text as files.'),
            placeholder='https://podcasts.apple.com/…',
            input_label='Apple Podcasts episode link',
            button='Get transcript',
            steps=[
                ('Copy the link', 'In Apple Podcasts, open the episode, tap Share, then Copy Link.'),
                ('Paste it here', 'Podskrift reads the show’s feed from Apple’s directory and finds the episode.'),
                ('Read or download', 'Get the transcript in the browser and as .txt or .srt.'),
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
            button='Start',
            steps=[
                ('Paste a link or search', 'Spotify, Apple Podcasts, an RSS feed or a direct audio URL all work.'),
                ('Pick the episode', 'You see the episode list with lengths before anything is charged.'),
                ('Get the text', 'Plain text for reading and quoting, .srt with timestamps for subtitles.'),
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
                ('Find the feed', 'Hosts like Buzzsprout, Libsyn, Podbean and Acast all publish one. See the RSS guide below.'),
                ('Paste the URL', 'Podskrift lists the latest episodes with titles, dates and lengths.'),
                ('Transcribe', 'Pick an episode and get .txt and timestamped .srt.'),
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
            lang=NB,
            title='Podcast til tekst: transkriber norske podkaster fra en lenke',
            meta=('Gjør en podkastepisode om til tekst uten å laste opp lydfil. Lim inn lenke '
                  f'fra Spotify, Apple Podcasts eller RSS. Støtter norsk. {free_no.capitalize()}.'),
            h1='Podcast til tekst, rett fra en lenke',
            lede=('Lim inn lenken til episoden, så får du hele teksten med tidsstempler. Du '
                  'trenger ikke finne og laste opp lydfilen selv.'),
            placeholder='Lim inn lenke fra Spotify, Apple eller RSS',
            input_label='Lenke til podkast eller episode',
            button='Start',
            steps=[
                ('Kopier lenken', 'I Spotify eller Apple Podcasts: Del, deretter Kopier lenke.'),
                ('Lim den inn', 'Velg episoden og gjerne «Norsk» som språk.'),
                ('Få teksten', 'Les i nettleseren eller last ned .txt og .srt.'),
            ],
            faq=faq,
        )
    if endpoint in ('landing_no_transkribere', 'landing_no_tale',
                    'landing_no_gratis', 'landing_no_spotify'):
        return _build_nb_extra(endpoint, trial=trial, pack=pack, lang_count=lang_count)
    raise KeyError(endpoint)


def _nb_what_you_get():
    return ('Hva får jeg?',
            'Hele teksten som ren tekst (.txt) og som undertekstfil med tidsstempler '
            '(.srt), og en lesevisning i nettleseren. Transkripsjonene er private for '
            'kontoen din.')


def _build_nb_extra(endpoint, *, trial, pack, lang_count):
    free_no = f'{trial} gratisminutter' if trial else 'gratis konto'
    price = ('Hva koster det?', _price_no(trial, pack))

    if endpoint == 'landing_no_transkribere':
        return dict(
            lang=NB,
            title='Transkribere podcast: slik gjør du det, steg for steg',
            meta=('Slik transkriberer du en podkastepisode uten å laste ned lyden: finn '
                  'episoden, velg språk og få teksten med tidsstempler. '
                  f'{free_no.capitalize()}.'),
            h1='Transkribere en podcast, uten å laste ned lyden',
            lede=('For journalister, studenter og podkastere som trenger sitater, '
                  'referater eller tekstversjon av en episode. Finn episoden her, så '
                  'gjør Podskrift resten.'),
            placeholder='Søk etter podkast eller lim inn lenke',
            input_label='Podkastnavn eller lenke',
            button='Finn episoden',
            steps=[
                ('Finn episoden', 'Søk på navnet til podkasten, eller lim inn lenke fra '
                 'Spotify, Apple Podcasts eller RSS.'),
                ('Velg språk', 'Automatisk gjenkjenning er standard. Er episoden på norsk, '
                 'gir det bedre treff å velge «Norsk».'),
                ('Bruk teksten', 'Søk i transkripsjonen, kopier sitater, eller last ned '
                 '.txt og .srt.'),
            ],
            faq=[
                ('Hvor lang tid tar det å transkribere en episode?',
                 'Vanligvis noen få minutter for en time med lyd. Du kan lukke siden; '
                 'jobben fortsetter, og du får e-post når teksten er klar.'),
                ('Kan jeg sitere fra transkripsjonen?',
                 'Ja, men sjekk sitatet mot lyden før publisering. Tidsstemplene i .srt-filen '
                 'gjør det raskt å finne riktig sted i episoden.'),
                ('Skiller den mellom hvem som snakker?',
                 'Ikke ennå. Teksten kommer som ett løpende manus med tidsstempler, uten '
                 'navn på hvem som sier hva.'),
                ('Kan jeg transkribere min egen podkast?',
                 'Ja. Mange bruker teksten til shownotes, artikler eller undertekster på '
                 'video. Lim inn RSS-feeden din for å se alle episodene.'),
                price,
            ],
        )
    if endpoint == 'landing_no_tale':
        return dict(
            lang=NB,
            title='Tale til tekst for podkaster og lydopptak med lenke',
            meta=('Gjør tale om til tekst fra en podkast eller en lydfil på nett. Støtter norsk, '
                  'svensk, dansk og engelsk. Last ned som .txt eller .srt. '
                  f'{free_no.capitalize()}.'),
            h1='Tale til tekst, fra podkaster og lyd på nett',
            lede=('Podskrift gjør tale om til tekst fra opptak som allerede ligger på nett: '
                  'podkastepisoder og direkte lenker til lydfiler. Det er ikke diktering i '
                  'sanntid, men ferdig opptak inn, ferdig tekst ut.'),
            placeholder='Lim inn lenke til podkast eller lydfil (.mp3, .m4a)',
            input_label='Lenke til podkast eller lydfil',
            button='Gjør om til tekst',
            steps=[
                ('Lim inn lenken', 'En podkastlenke, en RSS-feed eller en direkte lenke til '
                 'en lydfil som .mp3 eller .m4a.'),
                ('Velg språket det snakkes', 'Navngi språket for best resultat, særlig ved '
                 'korte opptak og dialekt.'),
                ('Få teksten', 'Ren tekst for lesing og sitater, og .srt med tidsstempler '
                 'for undertekster.'),
            ],
            faq=[
                ('Kan jeg diktere i sanntid?',
                 'Nei. Til diktering fungerer talegjenkjenningen i telefonen eller Word '
                 'bedre. Podskrift er laget for ferdige opptak, som en hel podkastepisode.'),
                ('Jeg har en lydfil på maskinen. Fungerer det?',
                 'Ikke med opplasting ennå. Legg filen et sted der den har en offentlig, '
                 'direkte lenke, og lim inn den.'),
                ('Hvilke språk forstår den?',
                 f'{lang_count} språk, med OpenAI Whisper. Norsk, svensk, dansk og engelsk '
                 'fungerer godt på tydelig tale. Dialekt, mye overlappende prat og musikk '
                 'under tale gir flere feil.'),
                ('Hva med nynorsk og dialekter?',
                 'Teksten skrives ut på bokmål eller nynorsk ut fra hva som blir sagt, men '
                 'sterk dialekt normaliseres ofte mot bokmål. Les korrektur på navn og '
                 'faguttrykk.'),
                price,
            ],
        )
    if endpoint == 'landing_no_gratis':
        if trial:
            free_answer = (f'Nye kontoer får {trial} minutter transkribering gratis, uten '
                           'kort. Er første episode lengre, starter den likevel: du får de '
                           f'første {trial} minuttene som forhåndsvisning og kan kjøpe '
                           'minutter for å fullføre.')
            h1 = f'Transkribering gratis: {trial} minutter uten kort'
            title = f'Transkribering gratis: {trial} gratisminutter, uten kort'
        else:
            free_answer = ('Det er ingen gratis prøveperiode akkurat nå. Du kan likevel '
                           'transkribere til kostpris med din egen OpenAI API-nøkkel.')
            h1 = 'Transkribering gratis eller til kostpris'
            title = 'Transkribering gratis eller til kostpris: hva det faktisk koster'
        return dict(
            lang=NB,
            title=title,
            meta=('Hva er gratis, og hva koster transkribering etterpå? Ærlig oversikt, '
                  'uten abonnement. Lim inn lenke fra Spotify, Apple eller RSS.'),
            h1=h1,
            lede=('Rett svar på hva som er gratis og hva som ikke er det, så du ikke får '
                  'en overraskelse midt i en episode.'),
            placeholder='Lim inn lenke fra Spotify, Apple eller RSS',
            input_label='Lenke til podkast eller episode',
            button='Prøv gratis' if trial else 'Start',
            steps=[
                ('Lim inn en lenke', 'Spotify, Apple Podcasts eller RSS. Du kan også søke '
                 'på navnet.'),
                ('Lag en konto', 'E-post og passord. Ingen kort for gratisminuttene.'),
                ('Få transkripsjonen', 'Last ned .txt og .srt. Teksten blir liggende i '
                 'historikken din.'),
            ],
            faq=[
                ('Er det virkelig gratis?', free_answer),
                ('Hva skjer når gratisminuttene er brukt opp?', _price_no(None, pack)),
                ('Er det et abonnement?',
                 'Nei. Minutter er et engangskjøp, og ingenting fornyes automatisk.'),
                ('Finnes det gratis alternativer?',
                 'Spotify og Apple viser tekst i appene sine for noen programmer, gratis, '
                 'men du kan ikke laste den ned. Whisper kan du kjøre selv gratis hvis du '
                 'har en kraftig maskin og er vant med kommandolinjen.'),
                _nb_what_you_get(),
            ],
        )
    if endpoint == 'landing_no_spotify':
        return dict(
            lang=NB,
            title='Spotify-podcast til tekst: lim inn episodelenken',
            meta=('Få teksten til en podkastepisode på Spotify. Lim inn lenken fra '
                  'open.spotify.com, så finner Podskrift den offentlige feeden og '
                  f'transkriberer. {free_no.capitalize()}.'),
            h1='Fra Spotify-podcast til tekst',
            lede=('Spotify viser bare tekst for noen programmer, og du kan verken kopiere '
                  'eller laste den ned. Lim inn lenken til episoden her, og få hele teksten '
                  'med tidsstempler.'),
            placeholder='https://open.spotify.com/episode/…',
            input_label='Lenke til Spotify-episode',
            button='Hent teksten',
            steps=[
                ('Kopier lenken', 'I Spotify: trykk ⋯ eller Del på episoden, deretter '
                 '«Kopier lenke til episode».'),
                ('Lim den inn her', 'Podskrift finner den samme episoden i programmets '
                 'offentlige feed.'),
                ('Les eller last ned', 'Det tar vanligvis noen minutter. Last ned .txt '
                 'eller .srt.'),
            ],
            faq=[
                ('Fungerer alle podkaster på Spotify?',
                 'De fleste. Nesten alle programmer på Spotify har også en offentlig '
                 'RSS-feed, og det er der lyden hentes fra. Spotify-eksklusive programmer og '
                 'episoder bare for abonnenter har ingen offentlig feed og kan ikke '
                 'transkriberes.'),
                ('Hva med NRK-podkaster på Spotify?',
                 'NRK legger mange podkaster ut på Spotify og Apple. Finnes episoden i en '
                 'offentlig feed, fungerer den. Innhold som bare ligger i NRK Radio-appen, '
                 'gjør det ikke.'),
                ('Kan jeg lime inn lenken til hele programmet?',
                 'Ja. Da får du episodelisten og velger episoden der.'),
                ('Er dette det samme som Spotifys egen tekst?',
                 'Nei. Podskrift lager sin egen transkripsjon fra lyden, så det fungerer '
                 'også der Spotify ikke viser tekst, og du kan kopiere og laste den ned.'),
                price,
            ],
        )
    raise KeyError(endpoint)


UI = {
    'en': dict(how='How it works', faq='Questions', related='More ways to transcribe',
               ai='Use in ChatGPT & Claude', guides='Setup guides', rss_help='How to find an RSS feed',
               pricing='Pricing', shows='Popular podcasts', note='Searching by podcast name works too.'),
    NB: dict(how='Slik fungerer det', faq='Spørsmål og svar', related='Mer om Podskrift',
               ai='Bruk i ChatGPT og Claude', guides='Veiledninger', rss_help='Finn RSS-feeden',
               pricing='Priser', shows='Populære podkaster', note='Du kan også søke etter navnet på podkasten.'),
}
