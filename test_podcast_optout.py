"""Creator opt-out: a blocked show is refused and hidden at every entry point.

Covers the matcher itself (URL variants, ids, exact enclosures on a shared
host), the feed parser, the single enqueue path, web routes, the HTTP API,
MCP tools, transcript retrieval, show pages and the sitemap.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from test_app import (  # noqa: F401 - fixtures are used by name
    A, MCP, _agent_get, _agent_post, _login, _make_user, _mcp_tool, _purge,
    customer_api, mcp_on, trial_on,
)
import podcast_optout as P  # noqa: E402
import show_pages as SP  # noqa: E402

MSG = P.OPTOUT_MESSAGE
FEED = 'https://feed.podbean.com/divineenergy/feed.xml'
AUDIO = ('https://mcdn.podbean.com/mf/web/bmnp7gcj2ravep7a/300_300_Episodes-_'
         'Overcome_Fear_Be_Authentic_Remember_Your_Purpose_AUPHONIC_Rheanna_'
         'Aviy_2026-10-17_pod8mxfw.mp3')
OTHER_PODBEAN_AUDIO = 'https://mcdn.podbean.com/mf/web/zzzz/other-show-ep1.mp3'
EPISODE_PAGE = 'https://divineenergy.podbean.com/e/some-episode/'
APPLE = 'https://podcasts.apple.com/us/podcast/dna-remembrance/id1669936175'
SPOTIFY = 'https://open.spotify.com/show/4SJFFfLMOPPsIe1FP0xarz?si=abc'
TITLE = 'DNA Remembrance with Rheanna Aviy'


def _feed_xml(title, audio_urls, link='https://example.org/'):
    items = ''.join(
        f'<item><title>Ep {i}</title><enclosure url="{u}" type="audio/mpeg"'
        f' length="1"/></item>' for i, u in enumerate(audio_urls))
    return (f'<?xml version="1.0"?><rss version="2.0"><channel>'
            f'<title>{title}</title><link>{link}</link>{items}</channel></rss>')


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    FEED,
    'http://feed.podbean.com/divineenergy/feed.xml',
    'https://feed.podbean.com/divineenergy/feed.xml/',
    'https://www.feed.podbean.com/DivineEnergy/feed.xml?utm=x#top',
    'feed.podbean.com/divineenergy/feed.xml',
    'https://feed.podbean.com/divineenergy/',
    'https://divineenergy.podbean.com/feed.xml',
    'https://divineenergy.podbean.com/feed/',
    EPISODE_PAGE,
    'https://dnaremembrance.me/podcast',
    APPLE,
    APPLE + '?i=1000123',
    'https://podcasts.apple.com/no/podcast/id1669936175',
    SPOTIFY,
    AUDIO,
    AUDIO.replace('https://', 'http://') + '?download=1',
    'https://op3.dev/e/' + AUDIO.replace('https://', ''),
    'https://dts.podtrac.com/redirect.mp3/' + AUDIO.replace('https://', ''),
])
def test_blocked_urls(url):
    assert P.match_url(url) is not None


@pytest.mark.parametrize('url', [
    'https://feed.podbean.com/otherpodcast/feed.xml',
    'https://feed.podbean.com/divineenergyx/feed.xml',
    'https://otherdivineenergy.podbean.com.example.com/feed',
    OTHER_PODBEAN_AUDIO,
    'https://podcasts.apple.com/us/podcast/id1669936176',
    'https://open.spotify.com/show/4SJFFfLMOPPsIe1FP0xarZ',
    'https://example.com/ep.mp3',
    # Same file name on an unrelated host is not this show's enclosure.
    AUDIO.replace('mcdn.podbean.com', 'cdn.example.com'),
    '',
    None,
])
def test_unrelated_urls_pass(url):
    assert P.match_url(url) is None


def test_names_exact_only():
    assert P.match_name(TITLE)
    assert P.match_name('  dna   REMEMBRANCE with Rheanna Aviy! ')
    assert P.match_name('Navigating Spirituality and Life with Divine Energy')
    # Author alone or a near title is not this show.
    assert not P.match_name('Rheanna Aviy')
    assert not P.match_name('DNA Remembrance')
    assert not P.match_name('')


def test_static_audio_list_covers_the_feed():
    data = json.loads(P.AUDIO_LIST_PATH.read_text())
    assert len(data['divineenergy']) >= 300
    assert P.audio_key(AUDIO) in data['divineenergy']


def test_refresh_learns_new_enclosures():
    new = 'https://mcdn.podbean.com/mf/web/newid123/brand-new-episode.mp3'
    assert P.match_url(new) is None
    added = P.refresh_once(fetch=lambda url: _feed_xml(TITLE, [new, AUDIO]))
    assert added == 1
    assert P.match_url(new) is not None
    assert P.refresh_once(fetch=lambda url: (_ for _ in ()).throw(OSError())) == 0


def test_match_item_and_task_shapes():
    assert P.match_item({'collectionId': 1669936175, 'collectionName': 'x'})
    assert P.match_item({'type': 'show', 'name': TITLE})
    assert P.match_item({'artist': TITLE, 'audio_url': 'https://e.com/a.mp3'})
    assert P.match_item({'feedUrl': FEED})
    assert not P.match_item({'type': 'episode', 'name': TITLE})  # episode title
    assert not P.match_item({'collectionName': 'Other', 'feedUrl': 'https://e.com/f'})

    class T:
        rss_url = None
        source_audio_url = AUDIO
        source_apple_url = source_spotify_url = source_website_url = None
        podcast_name = 'Something'
    assert P.is_opted_out_task(T())
    T.source_audio_url = OTHER_PODBEAN_AUDIO
    assert not P.is_opted_out_task(T())
    T.podcast_name = TITLE
    assert P.is_opted_out_task(T())


# ---------------------------------------------------------------------------
# Feed parser
# ---------------------------------------------------------------------------

def test_feed_parser_refuses_before_fetching(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError('blocked feed must not be fetched')
    monkeypatch.setattr(A.feedparser, 'parse', boom)
    monkeypatch.setattr(A, '_fetch_feed_capped', boom)
    for url in (FEED, 'http://feed.podbean.com/divineenergy/feed.xml/'):
        assert A.get_episodes_from_rss(url) == (None, MSG)
        assert A.get_episodes_from_rss(url, timeout=5) == (None, MSG)


def test_feed_parser_refuses_mirror_by_title_and_learns_audio(monkeypatch):
    real = A.feedparser.parse
    fresh = 'https://mcdn.podbean.com/mf/web/mirror1/learned-from-mirror.mp3'
    monkeypatch.setattr(A.feedparser, 'parse',
                        lambda u: real(_feed_xml(TITLE, [fresh])))
    assert A.get_episodes_from_rss('https://mirror.example.com/rss') == (None, MSG)
    assert P.match_url(fresh) is not None


def test_feed_parser_refuses_feed_with_known_enclosure(monkeypatch):
    real = A.feedparser.parse
    monkeypatch.setattr(A.feedparser, 'parse',
                        lambda u: real(_feed_xml('Renamed show', [AUDIO])))
    assert A.get_episodes_from_rss('https://mirror.example.com/rss') == (None, MSG)


def test_feed_parser_refuses_body_string_of_blocked_show():
    body = _feed_xml(TITLE, ['https://cdn.example.com/x.mp3'])
    assert A.get_episodes_from_rss(body) == (None, MSG)


def test_feed_parser_leaves_other_shows_alone(monkeypatch):
    real = A.feedparser.parse
    monkeypatch.setattr(A.feedparser, 'parse', lambda u: real(
        _feed_xml('Another podbean show', [OTHER_PODBEAN_AUDIO])))
    episodes, err = A.get_episodes_from_rss('https://feed.podbean.com/other/feed.xml')
    assert err is None and episodes[0]['audio_url'] == OTHER_PODBEAN_AUDIO


# ---------------------------------------------------------------------------
# Enqueue (web + MCP + API share it)
# ---------------------------------------------------------------------------

def _task_count():
    from models import TranscriptionTask
    with A.app.app_context():
        return TranscriptionTask.query.count()


@pytest.mark.parametrize('meta,rss', [
    ({'title': 'Ep', 'audio_url': AUDIO}, None),
    ({'title': 'Ep', 'audio_url': 'https://cdn.example.com/a.mp3'}, FEED),
    ({'title': 'Ep', 'audio_url': 'https://cdn.example.com/a.mp3',
      'podcast_name': TITLE}, None),
    ({'title': 'Ep', 'audio_url': 'https://cdn.example.com/a.mp3',
      'apple_url': APPLE}, None),
    ({'title': 'Ep', 'audio_url': 'https://cdn.example.com/a.mp3',
      'episode_link': EPISODE_PAGE}, None),
])
def test_enqueue_refuses(trial_on, meta, rss):
    uid = _make_user('optout-enqueue@example.com', limit=3600)
    from models import db, User
    before = _task_count()
    with A.app.app_context():
        user = db.session.get(User, uid)
        payload, status = A.enqueue_transcription(user, meta, rss_url=rss)
    assert status == 403
    assert payload == {'error': MSG, 'reason': 'creator_opt_out'}
    assert _task_count() == before
    _purge(['optout-enqueue@example.com'])


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

@pytest.fixture
def web_user():
    uid = _make_user('optout-web@example.com', limit=3600)
    yield uid
    from models import db, TranscriptionTask, TranscriptShare
    with A.app.app_context():
        ids = [t.id for t in TranscriptionTask.query.filter_by(user_id=uid)]
        TranscriptShare.query.filter(TranscriptShare.task_id.in_(ids or [''])).delete()
        TranscriptionTask.query.filter_by(user_id=uid).delete()
        db.session.commit()
    _purge(['optout-web@example.com'])


def test_web_paste_feed_url_refused(web_user):
    r = _login(web_user).post('/parse_rss', data={'rss_url': FEED},
                              follow_redirects=True)
    assert MSG in r.get_data(as_text=True)


def test_web_paste_episode_audio_refused(web_user, trial_on):
    before = _task_count()
    r = _login(web_user).post('/start_transcription', data={
        'audio_url': AUDIO, 'episode_title': 'Ep', 'duration_min': '5'})
    assert r.status_code == 403
    assert r.get_json()['error'] == MSG
    assert _task_count() == before


def test_web_episode_list_pick_refused(web_user, trial_on):
    r = _login(web_user).post('/start_transcription', data={
        'rss_url': FEED, 'episode_index': '0'})
    assert r.status_code == 403
    assert r.get_json()['error'] == MSG


def test_web_episode_list_pick_from_mirror_feed_refused(web_user, trial_on,
                                                       monkeypatch):
    real = A.feedparser.parse
    monkeypatch.setattr(A.feedparser, 'parse', lambda u: real(
        _feed_xml(TITLE, ['https://cdn.example.com/mirror-ep.mp3'])))
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    r = _login(web_user).post('/start_transcription', data={
        'rss_url': 'https://mirror.example.com/rss', 'episode_index': '0'})
    assert r.status_code == 403
    assert r.get_json()['error'] == MSG


def test_web_go_transcribe_refused(web_user):
    r = _login(web_user).get('/go/transcribe', query_string={
        'rss_url': FEED, 'episode_index': '0'}, follow_redirects=True)
    assert MSG in r.get_data(as_text=True)


def test_web_apple_and_spotify_paste_refused(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError('no directory lookup for a blocked show')
    monkeypatch.setattr(A.requests, 'get', boom)
    monkeypatch.setattr(A, 'fetch_spotify_metadata', boom)
    c = A.app.test_client()
    r = c.post('/convert-apple-url', json={'apple_url': APPLE})
    assert r.get_json() == {'success': False, 'error': MSG}
    r = c.get('/resolve-apple', query_string={'url': APPLE + '?i=1000'})
    assert r.get_json()['error'] == MSG and r.get_json()['results'] == []
    r = c.get('/resolve-spotify', query_string={'url': SPOTIFY})
    assert r.get_json()['error'] == MSG and r.get_json()['results'] == []


def test_spotify_episode_of_blocked_show_refused(monkeypatch):
    monkeypatch.setattr(A, 'fetch_spotify_metadata', lambda kind, sid: (
        {'show': TITLE, 'title': 'Ep'}, None))
    r = A.app.test_client().get('/resolve-spotify', query_string={
        'url': 'https://open.spotify.com/episode/abcdefghijklmnopqrstuv'})
    assert r.get_json()['error'] == MSG


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


_ITUNES = {'results': [
    {'collectionId': 1669936175, 'collectionName': TITLE, 'feedUrl': FEED,
     'trackName': 'Ep', 'episodeUrl': AUDIO},
    # Same show under a wrong id/name: the feed URL alone must catch it.
    {'collectionId': 1, 'collectionName': 'Mirror', 'feedUrl': FEED,
     'trackName': 'Ep', 'episodeUrl': AUDIO},
    {'collectionId': 42, 'collectionName': 'Another show',
     'feedUrl': 'https://feeds.example.com/another.xml',
     'trackName': 'Other ep', 'episodeUrl': 'https://cdn.example.com/o.mp3'},
]}


@pytest.mark.parametrize('kind', ['show', 'episode'])
def test_web_search_hides_blocked_show(monkeypatch, kind):
    monkeypatch.setattr(A.requests, 'get', lambda *a, **kw: _Resp(_ITUNES))
    r = A.app.test_client().get('/search-podcasts', query_string={
        'q': 'dna remembrance', 'type': kind})
    results = r.get_json()['results']
    assert len(results) == 1
    assert 'Another' in json.dumps(results)
    assert 'divineenergy' not in json.dumps(results)


def test_itunes_helpers_filter(monkeypatch):
    monkeypatch.setattr(A.requests, 'get', lambda *a, **kw: _Resp(_ITUNES))
    assert [i['collectionId'] for i in A._itunes_search('x', 'podcast')] == [42]
    assert [i['collectionId'] for i in A._itunes_lookup('42')] == [42]


# ---------------------------------------------------------------------------
# Retrieval of an existing transcript (defence in depth after deletion)
# ---------------------------------------------------------------------------

@pytest.fixture
def blocked_task(web_user):
    from models import db, TranscriptionTask, TranscriptShare
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='optout-task', user_id=web_user, status='completed',
            episode_title='Blocked ep', podcast_name=TITLE, rss_url=FEED,
            source_audio_url=AUDIO, transcript_text='secret words here',
            segments_json='[]',
            completed_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        db.session.add(TranscriptShare(task_id='optout-task', user_id=web_user,
                                       token='optout-share-token'))
        db.session.commit()
    return 'optout-task'


def test_owner_routes_refuse_blocked_task(web_user, blocked_task):
    c = _login(web_user)
    r = c.get(f'/transcription/{blocked_task}')
    assert r.status_code == 410 and MSG in r.get_data(as_text=True)
    assert c.get(f'/status/{blocked_task}').status_code == 410
    r = c.get(f'/download/{blocked_task}/txt')
    assert r.status_code == 410 and b'secret words' not in r.data
    assert c.post(f'/transcription/{blocked_task}/share').status_code == 410
    assert c.get(f'/transcription/{blocked_task}/related-episodes').status_code == 410
    assert b'Blocked ep' not in c.get('/history').data
    assert b'Blocked ep' not in c.get('/history?q=secret').data


def test_public_share_refused(blocked_task):
    c = A.app.test_client()
    assert c.get('/t/optout-share-token').status_code == 404
    assert c.get('/t/optout-share-token/download/txt').status_code == 404


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

def test_api_resolve_and_start_refused(customer_api):
    key = customer_api['key_a']
    before = _task_count()
    r = _agent_get('/api/v1/resolve', key, query_string={
        'url': FEED, 'date': '2026-10-17'})
    assert r.status_code == 403 and r.get_json()['error'] == MSG
    r = _agent_get('/api/v1/resolve', key, query_string={
        'publisher': TITLE, 'date': '2026-10-17'})
    assert r.status_code == 403
    r = _agent_post('/api/v1/transcriptions', key, json_body={
        'audio_url': AUDIO, 'title': 'Ep'})
    assert r.status_code == 403 and r.get_json()['reason'] == 'creator_opt_out'
    r = _agent_post('/api/v1/transcriptions', key, json_body={'url': APPLE})
    assert r.status_code == 403
    assert _task_count() == before


def test_api_never_reuses_a_task_for_blocked_audio(customer_api):
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='optout-api-reuse', user_id=customer_api['a'], status='completed',
            episode_title='Ep', podcast_name='Mirror name',
            episode_published='2026-10-17', transcript_text='words',
            completed_at=datetime(2026, 10, 17, tzinfo=timezone.utc)))
        db.session.commit()
    r = _agent_post('/api/v1/transcriptions', customer_api['key_a'], json_body={
        'audio_url': AUDIO, 'title': 'Ep', 'publisher': 'Mirror name',
        'published_at': '2026-10-17'})
    assert r.status_code == 403
    assert 'words' not in r.get_data(as_text=True)


def test_api_reads_hide_blocked_task(customer_api):
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='optout-api-task', user_id=customer_api['a'], status='completed',
            episode_title='Blocked', podcast_name=TITLE, rss_url=FEED,
            episode_published='2026-10-17', transcript_text='words'))
        db.session.commit()
    key = customer_api['key_a']
    r = _agent_get('/api/v1/episodes', key, query_string={'publisher': 'DNA'})
    assert r.get_json()['count'] == 0
    assert _agent_get('/api/v1/episodes/optout-api-task', key).status_code == 410
    r = _agent_get('/api/v1/episodes/optout-api-task/transcript', key)
    assert r.status_code == 410 and 'words' not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('episode', [AUDIO, FEED, EPISODE_PAGE, APPLE,
                                     f'{TITLE}|2026-10-17'])
def test_mcp_get_transcript_refused(mcp_on, episode):
    before = _task_count()
    payload, _ = _mcp_tool(mcp_on['key_a'], 'get_transcript', {'episode': episode})
    assert payload['error'] == 'creator_opt_out'
    assert payload['message'] == MSG
    assert _task_count() == before


def test_mcp_search_and_list_episodes(mcp_on, monkeypatch):
    monkeypatch.setattr(A, '_itunes_search', lambda term, entity: list(_ITUNES['results']))
    payload, _ = _mcp_tool(mcp_on['key_a'], 'search_podcasts', {'query': 'dna'})
    assert [r['name'] for r in payload['results']] == ['Another show']
    for podcast in (TITLE, FEED, APPLE, SPOTIFY, 'dna remembrance'):
        payload, _ = _mcp_tool(mcp_on['key_a'], 'list_episodes', {'podcast': podcast})
        assert payload.get('message') == MSG, podcast
        assert payload['episodes'] == []


def test_mcp_existing_transcript_hidden(mcp_on):
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='optout-mcp-task', user_id=mcp_on['a'], status='completed',
            episode_title='Blocked', podcast_name=TITLE, rss_url=FEED,
            transcript_text='words',
            completed_at=datetime(2026, 10, 2, tzinfo=timezone.utc)))
        db.session.commit()
    key = mcp_on['key_a']
    payload, _ = _mcp_tool(key, 'get_transcript', {'episode': 'optout-mcp-task'})
    assert payload['error'] == 'creator_opt_out'
    payload, _ = _mcp_tool(key, 'get_my_transcript', {'task_id': 'optout-mcp-task'})
    assert payload['error'] == 'creator_opt_out' and 'text' not in payload
    payload, _ = _mcp_tool(key, 'get_transcript_status', {'job_id': 'optout-mcp-task'})
    assert payload['error'] == 'creator_opt_out'
    payload, _ = _mcp_tool(key, 'list_my_transcripts', {})
    assert 'optout-mcp-task' not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Show pages, sitemap, llms.txt
# ---------------------------------------------------------------------------

@pytest.fixture
def curated(tmp_path, monkeypatch):
    path = tmp_path / 'show_pages.json'
    path.write_text(json.dumps({'shows': [
        {'slug': 'dna-remembrance-with-rheanna-aviy', 'name': TITLE,
         'feed_url': FEED, 'itunes_id': '1669936175'},
        {'slug': 'renamed', 'name': 'Renamed', 'feed_url':
         'http://feed.podbean.com/divineenergy/feed.xml/'},
        {'slug': 'by-id', 'name': 'By id', 'itunes_id': '1669936175',
         'feed_url': 'https://feeds.example.com/x.xml'},
        {'slug': 'another-show', 'name': 'Another show',
         'feed_url': 'https://feeds.example.com/another.xml'},
    ]}))
    monkeypatch.setattr(SP, 'SHOW_PAGES_PATH', path)
    yield path


def test_curated_list_drops_blocked_show(curated):
    assert [s['slug'] for s in SP.load_curated_shows()] == ['another-show']
    assert SP.find_show('renamed') is None


def test_show_page_gone_and_sitemap_clean(curated):
    c = A.app.test_client()
    r = c.get('/podcasts/dna-remembrance-with-rheanna-aviy')
    assert r.status_code == 410
    assert MSG in r.get_data(as_text=True)
    assert c.get('/podcasts/dna-remembrance-with-rheanna-aviy-2').status_code == 410
    assert c.get('/podcasts/another-show').status_code != 410
    index = c.get('/podcasts').get_data(as_text=True)
    assert 'Rheanna' not in index and 'Another show' in index
    sitemap = c.get('/sitemap.xml').get_data(as_text=True)
    assert 'another-show' in sitemap
    for bad in ('dna-remembrance', 'renamed', 'by-id'):
        assert bad not in sitemap
    llms = c.get('/llms.txt').get_data(as_text=True)
    assert 'Rheanna' not in llms and 'divineenergy' not in llms


def test_community_shows_skip_blocked(monkeypatch):
    class Col:
        def isnot(self, *_):
            return True

        def __ne__(self, other):
            return True

        def __eq__(self, other):
            return True

    class T:
        podcast_name = rss_url = status = Col()

    class Q:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *a):
            return self

        def distinct(self):
            return self

        def all(self):
            return self.rows

    class S:
        def query(self, *a):
            return Q([(TITLE, 'https://x.example.com/feed'),
                      ('Mirror', FEED),
                      ('Keep me', 'https://feeds.example.com/keep.xml')])

    shows = SP.community_shows_from_db(S(), T)
    assert [s['name'] for s in shows] == ['Keep me']


def test_opted_out_slug():
    assert SP.is_opted_out_slug('dna-remembrance-with-rheanna-aviy')
    assert SP.is_opted_out_slug('DNA-Remembrance-with-Rheanna-Aviy-3')
    assert not SP.is_opted_out_slug('dna-remembrance')
    assert not SP.is_opted_out_slug('')


def test_lifecycle_suggestion_skips_blocked_feed(monkeypatch):
    import lifecycle_emails as L
    calls = []
    monkeypatch.setattr(A, 'get_episodes_from_rss',
                        lambda *a, **kw: calls.append(a) or ([], None))
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)

    class Task:
        rss_url = FEED
    uid = _make_user('optout-life@example.com')
    from models import db, User
    with A.app.app_context():
        user = db.session.get(User, uid)
        assert L._next_episode_suggestion(user, Task()) is None
    assert calls == []
    _purge(['optout-life@example.com'])


def test_purge_script_deletes_only_the_blocked_show(tmp_path):
    import importlib.util
    import sqlite3
    spec = importlib.util.spec_from_file_location(
        'purge_optout', A.os.path.join(A.os.path.dirname(A.__file__),
                                       'ops', 'purge-optout-podcast.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / 'p.db'
    conn = sqlite3.connect(db)
    conn.executescript('''
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE transcription_tasks (id TEXT PRIMARY KEY, user_id INT,
            podcast_name TEXT, rss_url TEXT, source_audio_url TEXT,
            source_apple_url TEXT, source_spotify_url TEXT,
            source_website_url TEXT, summary_source_task_id TEXT);
        CREATE TABLE transcript_shares (id INTEGER PRIMARY KEY, task_id TEXT);
        CREATE TABLE summary_email_jobs (id INTEGER PRIMARY KEY, task_id TEXT,
            rss_url TEXT, audio_url TEXT, podcast_name TEXT);
        CREATE TABLE saved_feeds (id INTEGER PRIMARY KEY, user_id INT,
            name TEXT, rss_url TEXT);
        INSERT INTO users VALUES (7, 'a@example.com'), (8, 'b@example.com'),
                                 (9, 'c@example.com');
    ''')
    conn.executemany('INSERT INTO transcription_tasks VALUES (?,?,?,?,?,?,?,?,?)', [
        ('t1', 7, TITLE, FEED, AUDIO, None, None, None, None),
        ('t2', 8, 'Search result', None, AUDIO, None, None, None, None),
        ('t3', 9, 'Other', 'https://feed.podbean.com/other/feed.xml',
         OTHER_PODBEAN_AUDIO, None, None, None, None),
        ('t4', 9, 'Other summary', None, None, None, None, None, 't1'),
    ])
    conn.executemany('INSERT INTO transcript_shares VALUES (?,?)',
                     [(1, 't1'), (2, 't3')])
    conn.executemany('INSERT INTO summary_email_jobs VALUES (?,?,?,?,?)', [
        (1, None, FEED, AUDIO, TITLE), (2, 't3', 'https://x.example.com/f',
                                         OTHER_PODBEAN_AUDIO, 'Other')])
    conn.executemany('INSERT INTO saved_feeds VALUES (?,?,?,?)', [
        (1, 9, 'DNA', 'http://feed.podbean.com/divineenergy/feed.xml/'),
        (2, 9, 'Other', 'https://feed.podbean.com/other/feed.xml')])
    conn.commit()
    conn.close()
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / 'dna-remembrance-with-rheanna-aviy.json').write_text('{}')
    (cache / 'mirror.json').write_text(json.dumps({'feed_url': FEED}))
    (cache / 'another-show.json').write_text(json.dumps({
        'feed_url': 'https://feeds.example.com/another.xml',
        'episodes': [{'audio_url': OTHER_PODBEAN_AUDIO}]}))

    assert mod.main(['--db', str(db), '--cache-dir', str(cache)]) == 0
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT count(*) FROM transcription_tasks').fetchone()[0] == 4

    assert mod.main(['--db', str(db), '--cache-dir', str(cache), '--apply']) == 0
    q = lambda sql: [r[0] for r in conn.execute(sql)]  # noqa: E731
    assert q('SELECT id FROM transcription_tasks ORDER BY id') == ['t3']
    assert q('SELECT id FROM transcript_shares') == [2]
    assert q('SELECT id FROM summary_email_jobs') == [2]
    assert q('SELECT id FROM saved_feeds') == [2]
    assert q('SELECT id FROM users ORDER BY id') == [7, 8, 9]
    assert sorted(p.name for p in cache.iterdir()) == ['another-show.json']
