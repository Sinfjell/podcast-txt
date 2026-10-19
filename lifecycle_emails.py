"""Lifecycle email: trial-ended (1 h / 24 h) and unused-balance reminders.

Off unless LIFECYCLE_EMAILS_ENABLED is truthy (and mail itself is ready).

Three campaigns, each sent at most once per user (EmailSentLog key
``lifecycle:<campaign>:<user_id>``, unique, claimed before the send):

* ``trial_ended_1h``  -- free trial used up, nothing bought, 1-23 h ago
* ``trial_ended_24h`` -- same user, 24-72 h ago
* ``unused_balance``  -- paid minutes >= LIFECYCLE_UNUSED_MIN_MINUTES left and
  no activity for LIFECYCLE_UNUSED_INACTIVE_DAYS

The windows have an upper bound on purpose: turning the flag on must never
mail everyone whose trial ended last week. A one-time backfill is a separate,
explicit decision (``ops/lifecycle-emails.py``).

``decide_campaigns`` is pure (facts in, campaigns out) so the sending rules
are unit-tested without a database or Mailgun. Everything that touches the
app lives in ``sweep``, which never raises.

Links go through ``/e/<token>``: a signed, non-expiring-for-60-days token
naming the user, campaign and action. The click is recorded server-side
(``lifecycle_email_clicked``) and the campaign is stashed on the session so
the Checkout Session it leads to carries ``email_campaign`` in metadata;
the webhook copies that onto ``credit_purchases.email_campaign``.
"""

from __future__ import annotations

import html as html_lib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

TRIAL_ENDED_1H = 'trial_ended_1h'
TRIAL_ENDED_24H = 'trial_ended_24h'
UNUSED_BALANCE = 'unused_balance'
CAMPAIGNS = (TRIAL_ENDED_1H, TRIAL_ENDED_24H, UNUSED_BALANCE)
#: Kind written to email_sent_log.kind (one per campaign).
SENT_LOG_KIND_PREFIX = 'lifecycle_'

#: Click actions a token may carry.
ACTION_FINISH = 'finish'      # checkout for the episode they were transcribing
ACTION_PACK = 'pack'          # checkout for the default 300-minute pack
ACTION_SHOW = 'show'          # the show's episode list (per-episode prices)
ACTION_EPISODE = 'episode'    # start a suggested episode on paid balance
ACTION_HOME = 'home'
ACTIONS = (ACTION_FINISH, ACTION_PACK, ACTION_SHOW, ACTION_EPISODE, ACTION_HOME)

CLICK_SALT = 'podskrift-lifecycle-click-v1'
CLICK_MAX_AGE = 60 * 60 * 24 * 60  # 60 days
#: How long a click keeps attributing a checkout started from that browser.
CLICK_ATTRIBUTION_SECONDS = 60 * 60 * 24 * 7
SESSION_CAMPAIGN_KEY = 'lifecycle_email_campaign'

#: Test, internal and trial-farming accounts in production (sitrep 2026-10-18
#: test list + the aidenjonkman1-6 farm cleaned up on 2026-10-12). Env
#: LIFECYCLE_EXCLUDE_USER_IDS adds to this; it never removes from it.
DEFAULT_EXCLUDED_USER_IDS = frozenset({
    1, 31, 40, 90, 126, 130, 131, 140, 205, 212, 221, 222,
    207, 209, 210, 213, 214, 215,
})
#: Our own / test domains -- never a customer.
EXCLUDED_EMAIL_DOMAINS = frozenset({
    'podskrift.com', 'test.podskrift.com', 'nettsmed.no', 'nettsmed.dev',
    'example.com', 'example.org', 'example.net',
})
EXCLUDED_EMAIL_TLDS = frozenset({'test', 'invalid', 'example', 'localhost', 'local'})
#: Typos seen (or common) in signups: mail would bounce and hurt the domain.
TYPO_TLDS = frozenset({'comz', 'con', 'cmo', 'coom', 'comm', 'om', 'cm', 'co,'})
TYPO_DOMAINS = frozenset({
    'gmial.com', 'gamil.com', 'gmai.com', 'gmal.com', 'gnail.com',
    'hotmial.com', 'hotmal.com', 'yaho.com', 'outlok.com',
})
_EMAIL_RE = re.compile(
    r'^[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@'
    r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$')
#: Local parts our own smoke/E2E scripts use.
_TEST_LOCAL_RE = re.compile(r'^(smoke|e2e|test|abuseshot|ux-shots)[-+._0-9]', re.I)


def _truthy(raw: Optional[str]) -> bool:
    return (raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or '').strip() or default)
    except ValueError:
        return default


def lifecycle_enabled() -> bool:
    return _truthy(os.getenv('LIFECYCLE_EMAILS_ENABLED', '0'))


@dataclass(frozen=True)
class Rules:
    """Timing and thresholds. Defaults are the proposal; env can tune them."""
    trial_1h_after: timedelta = timedelta(hours=1)
    trial_1h_until: timedelta = timedelta(hours=23)
    trial_24h_after: timedelta = timedelta(hours=24)
    trial_24h_until: timedelta = timedelta(hours=72)
    #: Trial counts as used up below this many seconds left (the partial
    #: preview floor: below it every episode hits the paywall).
    trial_left_below_seconds: int = 5 * 60
    unused_min_seconds: int = 100 * 60
    unused_inactive: timedelta = timedelta(days=7)
    #: Never two lifecycle emails to one person closer than this.
    min_spacing: timedelta = timedelta(hours=12)

    @classmethod
    def from_env(cls, trial_left_below_seconds: Optional[int] = None) -> 'Rules':
        kw: dict[str, Any] = {
            'unused_min_seconds': _env_int('LIFECYCLE_UNUSED_MIN_MINUTES', 100) * 60,
            'unused_inactive': timedelta(
                days=_env_int('LIFECYCLE_UNUSED_INACTIVE_DAYS', 7)),
        }
        if trial_left_below_seconds is not None:
            kw['trial_left_below_seconds'] = int(trial_left_below_seconds)
        return cls(**kw)


def excluded_user_ids() -> frozenset:
    extra = set()
    for part in (os.getenv('LIFECYCLE_EXCLUDE_USER_IDS') or '').split(','):
        part = part.strip()
        if part.isdigit():
            extra.add(int(part))
    return DEFAULT_EXCLUDED_USER_IDS | frozenset(extra)


def email_problem(email: Optional[str]) -> Optional[str]:
    """Why this address must not get lifecycle mail, or None if it looks fine.

    We do not verify addresses at signup, so this is the best static check we
    have; Mailgun's bounce/complaint lists are checked at send time.
    """
    email = (email or '').strip()
    if not email or not _EMAIL_RE.match(email):
        return 'invalid_email'
    local, domain = email.rsplit('@', 1)
    domain = domain.lower()
    tld = domain.rsplit('.', 1)[-1]
    if tld in TYPO_TLDS or domain in TYPO_DOMAINS:
        return 'invalid_email'
    if domain in EXCLUDED_EMAIL_DOMAINS or tld in EXCLUDED_EMAIL_TLDS:
        return 'test_user'
    if any(domain.endswith('.' + d) for d in EXCLUDED_EMAIL_DOMAINS):
        return 'test_user'
    if _TEST_LOCAL_RE.match(local):
        return 'test_user'
    if local.lower().startswith('sindrefjelle'):
        return 'test_user'
    return None


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


@dataclass
class UserFacts:
    """Everything the sending rules look at for one user (no PII but email)."""
    user_id: int
    email: str = ''
    unsubscribed: bool = False
    #: email_transcript_ready=False: the user asked for less mail.
    mail_pref_off: bool = False
    byok: bool = False
    trial_denied: bool = False
    has_purchase: bool = False
    trial_limit_seconds: int = 0
    trial_left_seconds: int = 0
    #: When the last free-trial job finished (completed_at, else started_at).
    trial_ended_at: Optional[datetime] = None
    paid_seconds: int = 0
    #: Latest of: last job started, last purchase, last active day.
    last_activity_at: Optional[datetime] = None
    #: A job is queued or running right now.
    has_running_job: bool = False
    #: A job was started after trial_ended_at (they came back on their own).
    started_after_trial_end: bool = False
    #: campaign -> when it was sent (from email_sent_log).
    sent: dict = field(default_factory=dict)


def exclusion_reason(f: UserFacts, excluded_ids: Iterable[int] = ()) -> Optional[str]:
    """Reasons that block every lifecycle email for this user."""
    if f.user_id in set(excluded_ids):
        return 'test_user'
    problem = email_problem(f.email)
    if problem:
        return problem
    if f.unsubscribed:
        return 'unsubscribed'
    if f.mail_pref_off:
        return 'mail_pref_off'
    if f.trial_denied:
        return 'trial_denied'
    return None


def trial_ended_eligible(f: UserFacts, rules: Rules) -> Optional[str]:
    """None when the user is in the trial-ended audience (timing aside)."""
    if f.byok:
        return 'byok'
    if f.has_purchase:
        return 'bought'
    if f.trial_limit_seconds <= 0:
        return 'no_trial'
    if f.trial_left_seconds >= rules.trial_left_below_seconds:
        return 'trial_left'
    if f.trial_ended_at is None:
        return 'no_trial_job'
    if f.started_after_trial_end or f.has_running_job:
        return 'active'
    return None


def unused_balance_eligible(f: UserFacts, rules: Rules,
                            now: datetime) -> Optional[str]:
    if f.byok:
        return 'byok'
    if f.paid_seconds < rules.unused_min_seconds:
        return 'low_balance'
    if f.has_running_job:
        return 'active'
    last = _aware(f.last_activity_at)
    if last is not None and now - last < rules.unused_inactive:
        return 'recently_active'
    return None


def decide_campaigns(f: UserFacts, now: datetime, rules: Rules,
                     excluded_ids: Iterable[int] = ()) -> list[str]:
    """Campaigns due for this user right now (at most one)."""
    now = _aware(now)
    if exclusion_reason(f, excluded_ids):
        return []
    sent = {k: _aware(v) for k, v in (f.sent or {}).items()}
    last_sent = max((v for v in sent.values() if v is not None), default=None)
    if last_sent is not None and now - last_sent < rules.min_spacing:
        return []

    if trial_ended_eligible(f, rules) is None:
        age = now - _aware(f.trial_ended_at)
        if (TRIAL_ENDED_24H not in sent
                and rules.trial_24h_after <= age < rules.trial_24h_until):
            return [TRIAL_ENDED_24H]
        if (TRIAL_ENDED_1H not in sent and TRIAL_ENDED_24H not in sent
                and rules.trial_1h_after <= age < rules.trial_1h_until):
            return [TRIAL_ENDED_1H]
        return []

    if (UNUSED_BALANCE not in sent
            and unused_balance_eligible(f, rules, now) is None):
        return [UNUSED_BALANCE]
    return []


# ---------------------------------------------------------------------------
# Click tokens and links
# ---------------------------------------------------------------------------

def _click_serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=CLICK_SALT)


def make_click_token(secret_key: str, *, user_id: int, campaign: str,
                     action: str, task_id: Optional[str] = None,
                     episode: Optional[dict] = None) -> str:
    data: dict[str, Any] = {'u': int(user_id), 'c': campaign, 'a': action}
    if task_id:
        data['t'] = str(task_id)
    if episode:
        data['e'] = {k: str(v)[:500] for k, v in episode.items()
                     if k in ('audio_url', 'rss_url', 'title', 'podcast_name')
                     and v}
    return _click_serializer(secret_key).dumps(data)


def parse_click_token(secret_key: str, token: str) -> Optional[dict]:
    try:
        data = _click_serializer(secret_key).loads(token, max_age=CLICK_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get('c') not in CAMPAIGNS or data.get('a') not in ACTIONS:
        return None
    try:
        data['u'] = int(data.get('u'))
    except (TypeError, ValueError):
        return None
    return data


def click_url(public_base: str, secret_key: str, *, user_id: int, campaign: str,
              action: str, task_id: Optional[str] = None,
              episode: Optional[dict] = None) -> str:
    token = make_click_token(secret_key, user_id=user_id, campaign=campaign,
                             action=action, task_id=task_id, episode=episode)
    base = (public_base or '').rstrip('/')
    query = urlencode({'utm_source': 'email', 'utm_medium': 'email',
                       'utm_campaign': campaign, 'utm_content': action})
    return f'{base}/e/{token}?{query}'


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

SIGNOFF = 'Sindre\nPodskrift'


def _q(title: str) -> str:
    return f'\u201c{title}\u201d'


def _clean(text: Optional[str]) -> str:
    """Feed titles carry doubled spaces and newlines; mail shows them raw."""
    return ' '.join((text or '').split())


#: Subject lines get cut in inbox lists; keep the episode part readable.
SUBJECT_TITLE_MAX = 60


def _short(title: str, limit: int = SUBJECT_TITLE_MAX) -> str:
    title = ' '.join((title or '').split())
    if len(title) <= limit:
        return title
    cut = title[:limit - 1].rsplit(' ', 1)[0].rstrip(' |-:,')
    return f'{cut}\u2026'


def _ep_phrase(minutes: Optional[int]) -> str:
    if not minutes or minutes <= 0:
        return ''
    return f'{int(minutes)} min'


def build_bodies(campaign: str, ctx: dict) -> tuple[str, str, str]:
    """(subject, text, html) for one lifecycle email.

    ctx keys (all strings/ints, URLs already built):
      episode_title, podcast_name, partial_minutes, episode_minutes,
      finish_price, finish_url, pack_minutes, pack_price, pack_url,
      show_url, balance_minutes, episodes_phrase, next_title,
      next_minutes, next_url, home_url, unsub_url
    """
    ep = _clean(ctx.get('episode_title')) or 'your episode'
    show = _clean(ctx.get('podcast_name'))
    from_show = f' from {show}' if show else ''
    pack = (f'{ctx.get("pack_minutes", 300)} minutes for '
            f'{ctx.get("pack_price", "$5")}')
    finish = bool(ctx.get('finish_url') and ctx.get('finish_price'))
    paras: list[tuple[str, Optional[str], Optional[str]]] = []
    # Each paragraph: (text, link_label, link_url). Link is rendered as a
    # button for the first CTA and as a plain link for the rest.

    if campaign == TRIAL_ENDED_1H:
        if finish:
            subject = f'Finish {_q(_short(ep))}'
            paras.append((
                f'Your free minutes ran out partway through {_q(ep)}{from_show}. '
                f'You got the first {ctx.get("partial_minutes")} of '
                f'{ctx.get("episode_minutes")} minutes.', None, None))
            paras.append((
                f'You can get the whole episode for {ctx["finish_price"]}. '
                'One payment, no subscription, and it starts transcribing as '
                'soon as you have paid.',
                f'Finish this episode for {ctx["finish_price"]}',
                ctx['finish_url']))
        else:
            subject = 'Your free Podskrift minutes are used up'
            paras.append((
                f'You used your free minutes on {_q(ep)}{from_show}. '
                'I hope the transcript was useful.', None, None))
            if ctx.get('show_url') and show:
                paras.append((
                    f'If you want the next one, every episode of {show} has its '
                    f'price next to it, from {ctx.get("min_price", "$1.50")}.',
                    f'See episodes of {show}', ctx['show_url']))
        paras.append((
            f'If you listen to more than one show, {pack} is cheaper per '
            'episode. Minutes never expire.',
            f'Get {pack}', ctx.get('pack_url')))
        paras.append((
            'If something did not work, just reply. I read every email.',
            None, None))
    elif campaign == TRIAL_ENDED_24H:
        if finish:
            subject = f'The rest of {_q(_short(ep))} is still waiting'
            paras.append((
                f'A quick follow-up: you have the first '
                f'{ctx.get("partial_minutes")} minutes of {_q(ep)}{from_show}. '
                f'The full {ctx.get("episode_minutes")}-minute transcript is '
                f'{ctx["finish_price"]}.',
                f'Finish this episode for {ctx["finish_price"]}',
                ctx['finish_url']))
        else:
            subject = f'Your next episode{(" of " + show) if show else ""}'
            paras.append((
                f'A quick follow-up on {_q(ep)}{from_show}. Your next episode '
                f'costs from {ctx.get("min_price", "$1.50")}, priced by length.',
                (f'See episodes of {show}' if (show and ctx.get('show_url'))
                 else None),
                ctx.get('show_url') if show else None))
        paras.append((
            f'Or get {pack}, enough for several episodes. No subscription.',
            f'Get {pack}', ctx.get('pack_url')))
        paras.append((
            'If Podskrift was not what you needed, I would like to hear why. '
            'Just reply.', None, None))
    elif campaign == UNUSED_BALANCE:
        bal = int(ctx.get('balance_minutes') or 0)
        subject = f'You have {bal} minutes left on Podskrift'
        eps = ctx.get('episodes_phrase') or ''
        paras.append((
            f'You have {bal} minutes left on your Podskrift account'
            f'{(" (" + eps + ")") if eps else ""}. They never expire.',
            None, None))
        if ctx.get('next_url') and ctx.get('next_title'):
            nxt_show = f' from {show}' if show else ''
            length = _ep_phrase(ctx.get('next_minutes'))
            paras.append((
                f'Latest{nxt_show}: {_q(_clean(ctx["next_title"]))}'
                f'{(" (" + length + ")") if length else ""}.',
                'Transcribe this episode', ctx['next_url']))
            paras.append(('Or search for any other episode.',
                          'Open Podskrift', ctx.get('home_url')))
        else:
            paras.append(('Paste any podcast link or search for an episode.',
                          'Open Podskrift', ctx.get('home_url')))
        paras.append((
            'If something got in the way last time, just reply. I read every '
            'email.', None, None))
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f'unknown campaign {campaign}')

    unsub = ctx.get('unsub_url') or ''
    # ---- text
    lines = ['Hi,', '']
    for text, label, url in paras:
        lines.append(text)
        if label and url:
            lines.append(f'{label}: {url}')
        lines.append('')
    lines.append(SIGNOFF)
    lines.append('')
    lines.append('\u2014')
    lines.append('Podskrift \u00b7 hello@podskrift.com \u00b7 https://podskrift.com')
    lines.append('You are getting this because you have a Podskrift account.')
    lines.append(f'Unsubscribe: {unsub}')
    text_body = '\n'.join(lines) + '\n'

    # ---- html
    body = ['<p>Hi,</p>']
    first_cta = True
    for text, label, url in paras:
        body.append(f'<p>{html_lib.escape(text)}</p>')
        if label and url:
            safe_url = html_lib.escape(url, quote=True)
            if first_cta:
                body.append(
                    f'<p class="cta"><a class="btn" href="{safe_url}">'
                    f'{html_lib.escape(label)}</a></p>')
                first_cta = False
            else:
                body.append(
                    f'<p><a href="{safe_url}">{html_lib.escape(label)}</a></p>')
    body.append('<p>Sindre<br>Podskrift</p>')
    html_body = _html_document(subject, ''.join(body), unsub)
    return subject, text_body, html_body


_HTML_STYLE = """
:root { color-scheme: light dark; supported-color-schemes: light dark; }
body { margin:0; padding:0; background:#ffffff; }
.wrap { max-width:560px; margin:0 auto; padding:28px 20px;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  font-size:16px; line-height:1.55; color:#111111; }
.wrap p { margin:0 0 16px; }
.wrap a { color:#047857; }
.cta { margin:4px 0 22px !important; }
a.btn { display:inline-block; background:#059669; color:#ffffff !important;
  text-decoration:none; padding:12px 18px; border-radius:8px; font-weight:600; }
.foot { border-top:1px solid #dddddd; margin-top:28px; padding-top:14px;
  font-size:12px; line-height:1.5; color:#666666; }
.foot a { color:#666666; }
@media (prefers-color-scheme: dark) {
  body, .body { background:#111311 !important; }
  .wrap { color:#e8eae8 !important; }
  .wrap a { color:#34d399 !important; }
  a.btn { background:#10b981 !important; color:#06130d !important; }
  .foot { border-top-color:#333333 !important; color:#9aa09a !important; }
  .foot a { color:#9aa09a !important; }
}
"""


def _html_document(subject: str, inner: str, unsub_url: str) -> str:
    safe_unsub = html_lib.escape(unsub_url, quote=True)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f'<title>{html_lib.escape(subject)}</title>'
        f'<style>{_HTML_STYLE}</style></head>'
        '<body class="body"><div class="wrap">'
        f'{inner}'
        '<div class="foot">Podskrift \u00b7 '
        '<a href="mailto:hello@podskrift.com">hello@podskrift.com</a> \u00b7 '
        '<a href="https://podskrift.com">podskrift.com</a><br>'
        'You are getting this because you have a Podskrift account. '
        f'<a href="{safe_unsub}">Unsubscribe</a></div>'
        '</div></body></html>'
    )


def sent_log_key(campaign: str, user_id: int) -> str:
    return f'lifecycle:{campaign}:{int(user_id)}'


# ---------------------------------------------------------------------------
# App side: facts, context, send, sweep. Lazy-imports app (import cycle).
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES_EXCLUDED = ('completed', 'error', 'cancelled', 'failed')


def _app():
    import app as app_module  # noqa: WPS433 - lazy on purpose
    return app_module


def _oslo_day_end_utc(day: Optional[str]) -> Optional[datetime]:
    if not day:
        return None
    try:
        from zoneinfo import ZoneInfo
        d = datetime.strptime(day, '%Y-%m-%d')
        end = d.replace(hour=23, minute=59, tzinfo=ZoneInfo('Europe/Oslo'))
        return end.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def last_trial_task(user_id: int):
    """The newest job that spent free-trial seconds (what they were reading)."""
    A = _app()
    T = A.TranscriptionTask
    return (T.query
            .filter(T.user_id == user_id, T.trial_seconds_charged > 0)
            .order_by(T.started_at.desc())
            .first())


def gather_facts(user) -> UserFacts:
    """Read one user's facts from the database. Never raises into the sweep."""
    A = _app()
    T = A.TranscriptionTask
    uid = int(user.id)
    limit, _used, remaining = A.trial_status(user)
    f = UserFacts(
        user_id=uid,
        email=(user.email or '').strip(),
        unsubscribed=user.email_unsubscribed_at is not None,
        mail_pref_off=getattr(user, 'email_transcript_ready', True) is False,
        byok=bool(getattr(user, 'openai_api_key', None)),
        trial_denied=bool(getattr(user, 'trial_denied_reason', None)),
        has_purchase=A.CreditPurchase.query.filter_by(user_id=uid).first() is not None,
        trial_limit_seconds=int(limit or 0),
        trial_left_seconds=int(remaining or 0),
        paid_seconds=int(A.paid_balance_seconds(user)),
    )
    last = last_trial_task(uid)
    if last is not None:
        f.trial_ended_at = _aware(last.completed_at or last.started_at)
        if f.trial_ended_at is not None:
            later = (T.query.filter(T.user_id == uid, T.id != last.id,
                                    T.started_at > f.trial_ended_at.replace(tzinfo=None))
                     .first())
            f.started_after_trial_end = later is not None
    f.has_running_job = (
        T.query.filter(T.user_id == uid,
                       ~T.status.in_(_ACTIVE_STATUSES_EXCLUDED)).first()
        is not None)
    newest_task = T.query.filter_by(user_id=uid).order_by(T.started_at.desc()).first()
    newest_buy = (A.CreditPurchase.query.filter_by(user_id=uid)
                  .order_by(A.CreditPurchase.created_at.desc()).first())
    stamps = [
        _aware(newest_task.started_at) if newest_task else None,
        _aware(newest_buy.created_at) if newest_buy else None,
        _oslo_day_end_utc(getattr(user, 'last_active_day', None)),
        _aware(user.created_at),
    ]
    stamps = [s for s in stamps if s is not None]
    f.last_activity_at = max(stamps) if stamps else None
    rows = A.EmailSentLog.query.filter(
        A.EmailSentLog.user_id == uid,
        A.EmailSentLog.kind.like(SENT_LOG_KIND_PREFIX + '%')).all()
    f.sent = {r.kind[len(SENT_LOG_KIND_PREFIX):]: _aware(r.created_at) for r in rows}
    return f


def candidate_users(rules: Rules):
    """Users who could be in any audience. Cheap SQL prefilter; rules decide."""
    A = _app()
    U = A.User
    default_limit = int(A.TRIAL_DEFAULT_SECONDS)
    from sqlalchemy import func, or_
    limit_expr = func.coalesce(U.trial_seconds_limit, default_limit)
    return (U.query
            .filter(U.email_unsubscribed_at.is_(None))
            .filter(or_(
                (limit_expr - U.trial_seconds_used) < rules.trial_left_below_seconds,
                U.paid_seconds_balance >= rules.unused_min_seconds,
            ))
            .order_by(U.id)
            .all())


def _next_episode_suggestion(user, last_task) -> Optional[dict]:
    """Newest episode of their followed / last show they have not transcribed."""
    A = _app()
    rss_url = None
    try:
        feed = (A.SavedFeed.query.filter_by(user_id=user.id)
                .order_by(A.SavedFeed.created_at.desc()).first())
        if feed is not None and feed.rss_url:
            rss_url = feed.rss_url
    except Exception:  # noqa: BLE001
        rss_url = None
    if not rss_url and last_task is not None:
        rss_url = last_task.rss_url
    if not rss_url:
        newest = (A.TranscriptionTask.query
                  .filter(A.TranscriptionTask.user_id == user.id,
                          A.TranscriptionTask.rss_url.isnot(None))
                  .order_by(A.TranscriptionTask.started_at.desc()).first())
        rss_url = newest.rss_url if newest is not None else None
    if not rss_url or not A._is_fetchable_url(rss_url):
        return None
    if A.podcast_optout.match_url(rss_url):
        return None
    episodes, error = A.get_episodes_from_rss(rss_url, timeout=20)
    if error or not episodes:
        return None
    done_audio = {
        t.source_audio_url for t in A.TranscriptionTask.query
        .filter_by(user_id=user.id).all() if t.source_audio_url}
    for ep in episodes[:20]:
        audio = ep.get('audio_url') or ''
        if not audio or audio in done_audio:
            continue
        minutes = ep.get('duration_min')
        try:
            minutes = int(math.ceil(float(minutes))) if minutes else None
        except (TypeError, ValueError):
            minutes = None
        return {
            'audio_url': audio,
            'rss_url': rss_url,
            'title': (ep.get('title') or 'Episode').strip(),
            'podcast_name': (ep.get('podcast_name') or '').strip(),
            'minutes': minutes,
        }
    return None


def build_context(campaign: str, user, *, public_base: str, secret_key: str,
                  fetch_next_episode: bool = True) -> dict:
    """Everything build_bodies needs, from live data. Prices from the live curve."""
    A = _app()
    import email_notify
    import pricing_volume
    base = (public_base or '').rstrip('/')
    uid = int(user.id)

    def link(action, **kw):
        return click_url(base, secret_key, user_id=uid, campaign=campaign,
                         action=action, **kw)

    ctx: dict[str, Any] = {
        'unsub_url': email_notify.unsubscribe_url(public_base, secret_key, uid),
        'pack_minutes': A.CREDIT_PACK_MINUTES,
        'pack_price': A.DEFAULT_PACK_PRICE,
        'pack_url': link(ACTION_PACK),
        'home_url': link(ACTION_HOME),
        'min_price': pricing_volume.format_usd(pricing_volume.VOLUME_MIN_AMOUNT_CENTS)
        if A.VOLUME_PRICING_ENABLED else A.DEFAULT_PACK_PRICE,
        'variant': 'next',
    }
    last = last_trial_task(uid)
    if campaign in (TRIAL_ENDED_1H, TRIAL_ENDED_24H) and last is not None:
        ctx['episode_title'] = last.episode_title or ''
        ctx['podcast_name'] = last.podcast_name or ''
        ctx['task_id'] = last.id
        partial = A.task_partial_meta(last)
        if partial:
            n_min, m_min = A.partial_minutes_pair(partial)
            ctx['partial_minutes'] = n_min
            ctx['episode_minutes'] = m_min
            trial_left, paid_left = A._platform_remaining_seconds(uid)
            offer = A.episode_offer(m_min, (trial_left + paid_left) // 60)
            if offer and last.source_audio_url and A.stripe_checkout_enabled():
                ctx['finish_price'] = offer['price']
                ctx['finish_minutes'] = offer['minutes']
                ctx['finish_amount_cents'] = offer['amount_cents']
                ctx['finish_url'] = link(ACTION_FINISH, task_id=last.id)
                ctx['variant'] = 'finish'
        if last.rss_url:
            ctx['show_url'] = link(ACTION_SHOW, task_id=last.id)
    if campaign == UNUSED_BALANCE:
        paid_min = A.paid_balance_seconds(user) // 60
        ctx['balance_minutes'] = int(paid_min)
        typical = A.user_median_episode_minutes(uid)
        ctx['episodes_phrase'] = email_notify.approx_episodes_phrase(
            paid_min, typical).replace(' more ', ' ')
        nxt = None
        if fetch_next_episode:
            try:
                nxt = _next_episode_suggestion(user, last)
            except Exception:  # noqa: BLE001 - suggestion is optional
                logger.exception('lifecycle next-episode lookup failed')
                nxt = None
        if nxt:
            ctx['next_title'] = nxt['title']
            ctx['next_minutes'] = nxt['minutes']
            ctx['podcast_name'] = nxt['podcast_name']
            ctx['next_url'] = link(ACTION_EPISODE, episode=nxt)
            ctx['variant'] = 'next_episode'
        else:
            ctx['variant'] = 'balance_only'
    return ctx


def send_one(campaign: str, user, *, public_base: str, secret_key: str,
             to_override: Optional[str] = None, claim: bool = True,
             fetch_next_episode: bool = True) -> str:
    """Claim, render and send one lifecycle email. Returns an outcome string.

    ``to_override`` + ``claim=False`` is the ops test path: same content,
    different recipient, nothing recorded against the real user.
    """
    A = _app()
    import email_notify
    import mail as mailer
    import analytics as product_analytics
    key = sent_log_key(campaign, user.id)
    to = to_override or user.email
    if not to_override:
        suppressed = mailer.suppression_status(to)
        if suppressed != mailer.SUPPRESSION_CLEAR:
            # 'suppressed' never sends; 'unknown' retries next sweep.
            return f'skipped_{suppressed}'
    if claim and not email_notify.claim_email_send(
            A.db, A.EmailSentLog, user_id=user.id,
            kind=SENT_LOG_KIND_PREFIX + campaign, idempotency_key=key):
        return 'already_sent'
    try:
        ctx = build_context(campaign, user, public_base=public_base,
                            secret_key=secret_key,
                            fetch_next_episode=fetch_next_episode)
        subject, text, html = build_bodies(campaign, ctx)
    except Exception:  # noqa: BLE001
        logger.exception('lifecycle render failed (%s, user %s)', campaign, user.id)
        if claim:
            email_notify.release_email_send(A.db, A.EmailSentLog, idempotency_key=key)
        return 'render_failed'
    if to_override:
        subject = f'[TEST] {subject}'
    outcome = mailer.send_email(
        to=to, subject=subject, text=text, html=html,
        headers=email_notify._list_unsubscribe_headers(ctx['unsub_url']),
        tags=['lifecycle', campaign],
        kind=campaign,
        timeout=8,
        idempotency_key=key if claim else '',
    )
    if outcome != mailer.SEND_SENT:
        if claim and outcome == mailer.SEND_FAILED:
            email_notify.release_email_send(A.db, A.EmailSentLog, idempotency_key=key)
        return outcome
    if not to_override:
        props = {'campaign': campaign, 'variant': ctx.get('variant')}
        for k in ('episode_minutes', 'partial_minutes', 'finish_minutes',
                  'finish_amount_cents', 'balance_minutes'):
            if ctx.get(k) is not None:
                props[k] = ctx[k]
        product_analytics.capture('lifecycle_email_sent', user.id, props)
    return 'sent'


def sweep(*, now: Optional[datetime] = None, dry_run: bool = False,
          only_user_ids: Optional[Iterable[int]] = None,
          max_sends: Optional[int] = None,
          fetch_next_episode: bool = True) -> dict:
    """One pass over every candidate. Returns counts (and ids in dry-run).

    Never raises. Respects LIFECYCLE_EMAILS_ENABLED unless dry_run.
    """
    result: dict[str, Any] = {'due': {c: [] for c in CAMPAIGNS}, 'sent': {},
                              'outcomes': {}}
    try:
        import mail as mailer
        if not dry_run and not (lifecycle_enabled() and mailer.mail_ready()):
            result['skipped'] = 'disabled'
            return result
        A = _app()
        now = _aware(now) or datetime.now(timezone.utc)
        rules = Rules.from_env(A.TRIAL_PARTIAL_MIN_SECONDS)
        excluded = excluded_user_ids()
        only = set(int(x) for x in only_user_ids) if only_user_ids else None
        cap = max_sends if max_sends is not None else _env_int(
            'LIFECYCLE_MAX_PER_SWEEP', 25)
        sent_n = 0
        for user in candidate_users(rules):
            if only is not None and user.id not in only:
                continue
            try:
                facts = gather_facts(user)
                due = decide_campaigns(facts, now, rules, excluded)
            except Exception:  # noqa: BLE001
                logger.exception('lifecycle facts failed for user %s', user.id)
                continue
            for campaign in due:
                result['due'][campaign].append(user.id)
                if dry_run or sent_n >= cap:
                    continue
                outcome = send_one(
                    campaign, user, public_base=A.PUBLIC_BASE_URL,
                    secret_key=A.app.secret_key,
                    fetch_next_episode=fetch_next_episode)
                result['outcomes'][outcome] = result['outcomes'].get(outcome, 0) + 1
                if outcome == 'sent':
                    sent_n += 1
                    result['sent'][campaign] = result['sent'].get(campaign, 0) + 1
        return result
    except Exception:  # noqa: BLE001
        logger.exception('lifecycle sweep failed')
        result['error'] = True
        return result


def audience_report(*, now: Optional[datetime] = None) -> dict:
    """Backfill sizing: who is in each audience regardless of the time window."""
    A = _app()
    now = _aware(now) or datetime.now(timezone.utc)
    rules = Rules.from_env(A.TRIAL_PARTIAL_MIN_SECONDS)
    excluded = excluded_user_ids()
    out: dict[str, Any] = {
        'due_now': {c: [] for c in CAMPAIGNS},
        'trial_ended_any_time': [],
        'trial_ended_by_age': {'<1h': 0, '1-23h': 0, '23-24h': 0,
                               '24-72h': 0, '>72h': 0},
        'unused_balance_any_activity': [],
        'excluded': {},
    }
    for user in candidate_users(rules):
        f = gather_facts(user)
        reason = exclusion_reason(f, excluded)
        if reason:
            out['excluded'][reason] = out['excluded'].get(reason, 0) + 1
            continue
        for c in decide_campaigns(f, now, rules, excluded):
            out['due_now'][c].append(user.id)
        if trial_ended_eligible(f, rules) is None:
            out['trial_ended_any_time'].append(user.id)
            age = now - _aware(f.trial_ended_at)
            h = age.total_seconds() / 3600
            bucket = ('<1h' if h < 1 else '1-23h' if h < 23 else '23-24h'
                      if h < 24 else '24-72h' if h < 72 else '>72h')
            out['trial_ended_by_age'][bucket] += 1
        elif not f.byok and f.paid_seconds >= rules.unused_min_seconds:
            out['unused_balance_any_activity'].append(
                (user.id, f.paid_seconds // 60,
                 f.last_activity_at.isoformat() if f.last_activity_at else None))
    return out


# ---------------------------------------------------------------------------
# Background loop (gunicorn workers only; both may run, claims dedupe)
# ---------------------------------------------------------------------------

_sweeper_started = False


def _sweeper_loop(interval: int):
    import time
    A = _app()
    # Stagger the first pass so a deploy restart does not mail mid-boot.
    time.sleep(min(interval, 120))
    while True:
        try:
            with A.app.app_context():
                res = sweep()
                if res.get('sent'):
                    logger.info('lifecycle sweep sent %s', res['sent'])
        except Exception:  # noqa: BLE001 - the loop must not die quietly
            logger.exception('lifecycle sweeper pass failed')
        time.sleep(interval)


def start_sweeper() -> bool:
    """Start the daemon sweeper once per process when the flag is on."""
    global _sweeper_started
    if _sweeper_started or not lifecycle_enabled():
        return False
    if _truthy(os.getenv('PODSKRIFT_DISABLE_WATCHDOG')):
        return False
    import threading
    interval = max(60, _env_int('LIFECYCLE_SWEEP_INTERVAL_SECONDS', 600))
    threading.Thread(target=_sweeper_loop, args=(interval,),
                     name='lifecycle-email-sweeper', daemon=True).start()
    _sweeper_started = True
    return True
