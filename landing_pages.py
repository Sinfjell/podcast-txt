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
)

SHORT_LABELS = {
    'landing_spotify': 'Spotify podcast transcript',
    'landing_apple': 'Apple Podcasts transcript',
    'landing_podcast_to_text': 'Podcast to text',
    'landing_rss': 'RSS feed to text',
    'landing_free': 'Transcribe a podcast free',
    'landing_no': 'Podcast til tekst (norsk)',
}


def _usd(cents):
    return f'${cents // 100}' if cents % 100 == 0 else f'${cents / 100:.2f}'


def pack_phrase(pack, lang='en'):
    """Price phrase for *pack* (see app._landing_pack)."""
    if pack.get('volume'):
        lo, top_min, top = _usd(pack['min_cents']), pack['max_minutes'], _usd(pack['max_cents'])
        if lang == 'no':
            return (f'fra {lo}, med volumrabatt opptil {top_min} min for {top}, '
                    f'eller per episode')
        return f'from {lo}, volume discounts up to {top_min} min for {top}, or per episode'
    if lang == 'no':
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
            lang='no',
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
    raise KeyError(endpoint)


UI = {
    'en': dict(how='How it works', faq='Questions', related='More ways to transcribe',
               ai='Use in ChatGPT & Claude', guides='Setup guides', rss_help='How to find an RSS feed',
               pricing='Pricing', shows='Popular podcasts', note='Searching by podcast name works too.'),
    'no': dict(how='Slik fungerer det', faq='Spørsmål og svar', related='Mer om Podskrift',
               ai='Bruk i ChatGPT og Claude', guides='Veiledninger', rss_help='Finn RSS-feeden',
               pricing='Priser', shows='Populære podkaster', note='Du kan også søke etter navnet på podkasten.'),
}
