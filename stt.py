"""Speech-to-text provider routing, quality guards and cost accounting.

Podskrift transcribes on OpenAI ``whisper-1`` unless ``STT_PROVIDER=together``.
With Together, jobs paid for by the platform key (trial or credit packs) run on
Together AI ``openai/whisper-large-v3`` at a quarter of the price, with
whisper-1 as the per-chunk fallback. Users with their own OpenAI key always stay
on OpenAI (never Together): it is their key and their bill. BYOK users may
pick ``gpt-transcribe`` (Best quality) instead of the default whisper-1.

Everything here is pure (no Flask, no DB) so it can be tested directly; the
calls themselves live in ``app._transcribe_chunk_routed``.

Rollout (all env, read at call time so a restart is not needed in tests):

  STT_PROVIDER              whisper-1 (default) | together
  STT_TOGETHER_FUNDING      trial (default) | all     -- which platform jobs
  STT_TOGETHER_LANGUAGES    en (default)              -- comma list, e.g. en,no,da
  STT_SHADOW_RATE           0.02 (default)            -- share of Together chunks
                                                         also run on whisper-1
  TOGETHER_API_KEY          required for together; without it we stay on whisper-1

Stages: 1) together + trial + en  2) together + all + en  3) together + all + en,no,da.
Rollback: STT_PROVIDER=whisper-1 (or unset) and restart.
"""

from __future__ import annotations

import difflib
import os
import re
import statistics
import unicodedata

OPENAI_MODEL = 'whisper-1'
MODEL_GPT_TRANSCRIBE = 'gpt-transcribe'
# BYOK "Best quality": shorter parts (output token cap ~2k truncates 900 s),
# no segment timestamps from the API — we invent one segment per part.
GPT_TRANSCRIBE_SEGMENT_SECONDS = 300
ALLOWED_BYOK_MODELS = frozenset({OPENAI_MODEL, MODEL_GPT_TRANSCRIBE})

TOGETHER_MODEL = 'openai/whisper-large-v3'
TOGETHER_BASE_URL = 'https://api.together.xyz/v1'

PROVIDER_OPENAI = 'whisper-1'
PROVIDER_TOGETHER = 'together'
PROVIDER_GPT_TRANSCRIBE = MODEL_GPT_TRANSCRIBE

# What each provider actually costs us per audio minute. Internal accounting
# only (admin costs, PostHog). User-facing "est. cost" copy is about BYOK and
# stays on app.WHISPER_COST_PER_MINUTE (OpenAI's list price). BYOK still
# records the model so cost reports can exclude those rows by funding.
PROVIDER_USD_PER_MIN = {
    PROVIDER_OPENAI: 0.006,           # https://openai.com/api/pricing/
    PROVIDER_TOGETHER: 0.0015,        # api.together.xyz/v1/models price_per_minute
    PROVIDER_GPT_TRANSCRIBE: 0.0045,  # gpt-transcribe list price ($/min)
}

# Completeness guard for gpt-transcribe: a part whose words/sec is below this
# fraction of the median neighbouring rate is re-run on whisper-1 (same user
# key). Caught the Car Talk phone-intro drop at ~0.60× in the Oct 2026 test.
COMPLETENESS_RATE_FRACTION = 0.7
COMPLETENESS_MIN_SECONDS = 30.0
# ffmpeg leaves a ~0.1 s crumb on exact-length cuts; gpt-transcribe 400s it.
MIN_PART_SECONDS = 1.0

# Segment-level guard: Together cuts audio on a 30 s grid, and some windows
# come back nearly empty although someone is talking ("Det er mye." for 101
# words). Real silence also trips this, which only costs one small retry.
GAP_MIN_SECONDS = 10.0
GAP_MAX_WORDS_PER_SECOND = 0.6
# Whisper's best-known hallucinations: subtitle credits and outro lines learnt
# from subtitle training data. Matched case-insensitively on a segment.
HALLUCINATION_PATTERNS = [
    r'\bteksting av\b', r'\btekstet av\b', r'\bundertekst(?:er|ing) av\b',
    r'\btekster av\b', r'\bundertekster\b.*\b(?:nrk|amara)\b',
    r'\btak for at du så med\b', r'\btakk for at du så på\b',
    r'\bthanks? (?:you )?for watching\b', r'\bsubtitles? by\b',
    r'\bamara\.org\b', r'\buntertitel(?:ung)? (?:von|im auftrag)\b',
]
_HALLUCINATION_RE = re.compile('|'.join(HALLUCINATION_PATTERNS), re.IGNORECASE)
# Retry window around a flagged segment. +-2 s recovered every dropped window
# in the 2026-10-18 test while the exact same window came back empty again.
RETRY_PAD_SECONDS = 2.0
RETRY_MIN_WORDS_PER_SECOND = 0.4
MAX_RETRY_WINDOWS_PER_CHUNK = 4
# Soft guard for partial drops (a 30 s window that kept 1/3 of its words):
# a long segment at under half the chunk's median speech rate. Rare in the
# test set (en_daily 1, da_politiken 4 of 56). Its retry is only used when
# clearly longer; otherwise the original stays and nothing falls back.
SOFT_RATE_FRACTION = 0.5
MAX_SOFT_RETRIES_PER_CHUNK = 4

# SRT: whisper-1 segments are ~3-5 s; Together's run up to 30 s. Long cues are
# split on word boundaries with time interpolated by character position.
SRT_MAX_CUE_SECONDS = 7.0
SRT_MAX_CUE_CHARS = 84


def _env(name, default=''):
    return (os.getenv(name) or default).strip()


def provider_setting():
    """The configured provider name: 'together' or 'whisper-1' (default)."""
    raw = _env('STT_PROVIDER', PROVIDER_OPENAI).lower()
    return PROVIDER_TOGETHER if raw == PROVIDER_TOGETHER else PROVIDER_OPENAI


def together_api_key():
    return _env('TOGETHER_API_KEY') or None


def together_languages():
    raw = _env('STT_TOGETHER_LANGUAGES', 'en')
    return {p.strip().lower() for p in raw.split(',') if p.strip()}


def together_funding():
    """'trial' (default: free-trial minutes only) or 'all' (trial + paid)."""
    return 'all' if _env('STT_TOGETHER_FUNDING', 'trial').lower() == 'all' else 'trial'


def shadow_rate():
    try:
        rate = float(_env('STT_SHADOW_RATE', '0.02'))
    except ValueError:
        return 0.0
    return min(1.0, max(0.0, rate))


def usd_per_minute(provider):
    if provider == PROVIDER_TOGETHER:
        try:
            return float(_env('STT_TOGETHER_USD_PER_MIN',
                              str(PROVIDER_USD_PER_MIN[PROVIDER_TOGETHER])))
        except ValueError:
            return PROVIDER_USD_PER_MIN[PROVIDER_TOGETHER]
    if provider in PROVIDER_USD_PER_MIN:
        return PROVIDER_USD_PER_MIN[provider]
    return PROVIDER_USD_PER_MIN[PROVIDER_OPENAI]


def cost_usd(provider, seconds):
    """What `seconds` of audio sent to `provider` cost us."""
    return max(0.0, float(seconds or 0)) / 60.0 * usd_per_minute(provider)


def normalize_transcription_model(value):
    """BYOK model pick → canonical name. Unknown / empty → whisper-1."""
    raw = (value or '').strip().lower()
    if raw == MODEL_GPT_TRANSCRIBE:
        return MODEL_GPT_TRANSCRIBE
    return OPENAI_MODEL


def segment_seconds_for_model(model):
    """Part length for this OpenAI model, or None to keep the app default (900)."""
    if normalize_transcription_model(model) == MODEL_GPT_TRANSCRIBE:
        return GPT_TRANSCRIBE_SEGMENT_SECONDS
    return None


def part_word_rate(text, seconds):
    """Words per second for a part; 0 when duration is non-positive."""
    dur = float(seconds or 0)
    if dur <= 0:
        return 0.0
    return _words(text) / dur


def incomplete_part_indexes(texts, seconds_list,
                            fraction=COMPLETENESS_RATE_FRACTION,
                            min_seconds=COMPLETENESS_MIN_SECONDS):
    """Indexes whose word rate is below ``fraction`` × median of the set.

    Needs at least two parts long enough to compare; short crumbs and
    single-part jobs are never flagged (nothing to compare against).
    """
    texts = list(texts or [])
    seconds_list = list(seconds_list or [])
    n = min(len(texts), len(seconds_list))
    rates = []
    for i in range(n):
        sec = float(seconds_list[i] or 0)
        if sec < min_seconds:
            rates.append(None)
            continue
        rates.append(part_word_rate(texts[i], sec))
    comparable = [r for r in rates if r is not None]
    if len(comparable) < 2:
        return []
    median = statistics.median(comparable)
    if median <= 0:
        return []
    threshold = fraction * median
    return [i for i, r in enumerate(rates)
            if r is not None and r < threshold]


def job_funding(key_source, trial_seconds, paid_seconds):
    """'user' (own key), 'paid' (any paid seconds) or 'trial'."""
    if key_source != 'trial':
        return 'user'
    return 'paid' if int(paid_seconds or 0) > 0 else 'trial'


def route_job(key_source, language, trial_seconds=0, paid_seconds=0):
    """Decide the provider for a job. Returns (provider, reason).

    `language` is the user's pick ('' = auto). With auto, Together is tried and
    the first chunk's detected language decides (see `language_allowed`).
    """
    if key_source != 'trial':
        return PROVIDER_OPENAI, 'own_key'
    if provider_setting() != PROVIDER_TOGETHER:
        return PROVIDER_OPENAI, 'disabled'
    if not together_api_key():
        return PROVIDER_OPENAI, 'no_together_key'
    funding = job_funding(key_source, trial_seconds, paid_seconds)
    if funding == 'paid' and together_funding() != 'all':
        return PROVIDER_OPENAI, 'paid_not_rolled_out'
    lang = (language or '').strip().lower()
    if lang and lang not in together_languages():
        return PROVIDER_OPENAI, 'language_not_rolled_out'
    return PROVIDER_TOGETHER, 'rollout'


def language_allowed(detected):
    """True when an auto-detected language may stay on Together.

    Catches both "not rolled out yet" and "looks wrong" (Together once read
    accented English as Swahili and returned gibberish): anything outside the
    rollout list goes back to whisper-1, which detected that clip correctly.
    """
    code = (detected or '').strip().lower()
    return bool(code) and code in together_languages()


def _words(text):
    return len((text or '').split())


def is_hallucination(text):
    return bool(_HALLUCINATION_RE.search(text or ''))


def suspicious_segments(segments):
    """Indexes of segments that look like dropped speech or invented text.

    `segments` are dicts with start/end/text (chunk-relative seconds).
    """
    flagged = []
    for i, seg in enumerate(segments or []):
        start, end = float(seg.get('start') or 0), float(seg.get('end') or 0)
        dur = end - start
        text = seg.get('text') or ''
        if is_hallucination(text):
            flagged.append(i)
        elif dur >= GAP_MIN_SECONDS and _words(text) / dur < GAP_MAX_WORDS_PER_SECOND:
            flagged.append(i)
    return flagged


def _rate(seg):
    dur = float(seg.get('end') or 0) - float(seg.get('start') or 0)
    return _words(seg.get('text')) / dur if dur > 0 else 0.0


def soft_suspicious_segments(segments, exclude=()):
    """Indexes of long segments much slower than the chunk's median rate."""
    segs = list(segments or [])
    rates = sorted(_rate(s) for s in segs
                   if float(s.get('end') or 0) - float(s.get('start') or 0) >= 5)
    if len(rates) < 5:
        return []
    median = rates[len(rates) // 2]
    out = []
    for i, seg in enumerate(segs):
        dur = float(seg.get('end') or 0) - float(seg.get('start') or 0)
        if i in exclude or dur < GAP_MIN_SECONDS:
            continue
        if _rate(seg) < SOFT_RATE_FRACTION * median:
            out.append(i)
    return out[:MAX_SOFT_RETRIES_PER_CHUNK]


def retry_improved(original_text, retry_text):
    """Soft guard: use the retry only when it is clean and clearly longer."""
    if is_hallucination(retry_text):
        return False
    return _words(retry_text) >= 1.25 * _words(original_text) + 5


def retry_window(seg, chunk_seconds=None):
    """(offset, length) of the shifted window to re-send for a flagged segment."""
    start = max(0.0, float(seg['start']) - RETRY_PAD_SECONDS)
    end = float(seg['end']) + RETRY_PAD_SECONDS
    if chunk_seconds:
        end = min(end, float(chunk_seconds))
    return start, max(0.5, end - start)


def retry_recovered(original_text, retry_text, window_seconds):
    """Did the retry bring back real speech the first pass dropped?

    For an invented line ("Teksting av ...") any clean retry is a repair, even
    an empty one: the window was silence or music and the line goes away.
    """
    if is_hallucination(retry_text):
        return False
    if is_hallucination(original_text):
        return True
    words = _words(retry_text)
    # Lower bar than the flag itself: a 30 s window that comes back with 20
    # words over a music bed (da_politiken 737-767 s) is a real recovery.
    if window_seconds and words / float(window_seconds) < RETRY_MIN_WORDS_PER_SECOND:
        return False
    return words >= max(2 * _words(original_text), 3)


def _norm_tokens(text):
    t = unicodedata.normalize('NFKC', text or '').lower()
    return re.sub(r"[^\w\s']", ' ', t).split()


def trim_overlap(prev_text, new_text, next_text='', max_words=15):
    """Drop words the padded retry window repeats from its neighbours."""
    words = (new_text or '').split()
    prev = _norm_tokens(prev_text)
    norm_new = [(_norm_tokens(w) or [''])[0] for w in words]
    for k in range(min(max_words, len(prev), len(words)), 0, -1):
        if prev[-k:] == norm_new[:k]:
            words, norm_new = words[k:], norm_new[k:]
            break
    nxt = _norm_tokens(next_text)
    for k in range(min(max_words, len(nxt), len(words)), 0, -1):
        if nxt[:k] == norm_new[-k:]:
            words = words[:-k]
            break
    return ' '.join(words).strip()


def similarity(a, b):
    """1 - word-level diff ratio, normalised like the offline test (0..1)."""
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta and not tb:
        return 1.0
    return round(difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio(), 4)


def split_segments_for_srt(segments, max_seconds=SRT_MAX_CUE_SECONDS,
                           max_chars=SRT_MAX_CUE_CHARS):
    """Split long segments into SRT-sized cues; short ones pass through as-is.

    Time is interpolated by character offset (Together returns no word timings
    for Norwegian, so there is nothing better to align on).
    """
    out = []
    for seg in segments or []:
        start, end = float(seg.get('start') or 0), float(seg.get('end') or 0)
        text = (seg.get('text') or '').strip()
        dur = end - start
        if dur <= max_seconds or not text:
            out.append({'start': start, 'end': end, 'text': text})
            continue
        n_cues = max(int(-(-dur // max_seconds)), int(-(-len(text) // max_chars)))
        words = text.split()
        n_cues = max(1, min(n_cues, len(words)))
        target = len(text) / n_cues
        cues, cur = [], []
        for w in words:
            cur.append(w)
            if len(' '.join(cur)) >= target and len(cues) < n_cues - 1:
                cues.append(' '.join(cur))
                cur = []
        if cur:
            cues.append(' '.join(cur))
        total = sum(len(c) for c in cues) or 1
        t = start
        for i, cue in enumerate(cues):
            t_end = end if i == len(cues) - 1 else t + dur * len(cue) / total
            out.append({'start': round(t, 3), 'end': round(t_end, 3), 'text': cue})
            t = t_end
    return out
