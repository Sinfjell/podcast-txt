"""Database models for Podcast Transcriber."""

from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    # All accounts today are email+password; there is no OAuth/magic-link path.
    # password_reset still calls set_password() so a future passwordless signup
    # can gain a password the same way.
    password_hash = db.Column(db.String(255), nullable=False)
    openai_api_key = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Trial metering. Only ever touched for users transcribing on OUR key --
    # a user with their own key spends their own quota and is never metered.
    # A NULL limit means "use TRIAL_MINUTES" (legacy accounts). New signups
    # get NEW_USER_TRIAL_MINUTES stamped here at registration.
    trial_seconds_limit = db.Column(db.Integer, nullable=True)
    trial_seconds_used = db.Column(db.Integer, nullable=False, default=0,
                                   server_default='0')
    # Paid credit-pack balance (seconds). Credited only from a verified Stripe
    # webhook; spent after free trial minutes, never counted against the
    # global free-trial ceiling.
    paid_seconds_balance = db.Column(db.Integer, nullable=False, default=0,
                                     server_default='0')

    # Customer HTTP API key (v1: one active key per user). Plaintext is shown
    # once on generate and never stored — only the SHA-256 hash + a short
    # display prefix. Distinct from openai_api_key (BYOK) and from the host
    # AGENT_API_KEY (CoS-only shared secret).
    api_key_hash = db.Column(db.String(64), nullable=True, index=True)
    api_key_prefix = db.Column(db.String(16), nullable=True)
    api_key_created_at = db.Column(db.DateTime, nullable=True)

    # Transactional email prefs. Default ON for transcript-ready; global
    # unsubscribe stamps email_unsubscribed_at and suppresses all mail.
    # We do not verify addresses today — mail goes to the registered email.
    email_transcript_ready = db.Column(db.Boolean, nullable=False, default=True,
                                       server_default='1')
    email_unsubscribed_at = db.Column(db.DateTime, nullable=True)

    # Bumped on password reset so Flask-Login sessions (and remember cookies)
    # that still carry the old version stop loading the user.
    session_version = db.Column(db.Integer, nullable=False, default=0,
                                server_default='0')

    feeds = db.relationship('SavedFeed', backref='user', lazy=True, cascade='all, delete-orphan')
    tasks = db.relationship('TranscriptionTask', backref='user', lazy=True, cascade='all, delete-orphan')
    credit_purchases = db.relationship('CreditPurchase', backref='user', lazy=True,
                                       cascade='all, delete-orphan')
    transcript_shares = db.relationship('TranscriptShare', backref='user', lazy=True,
                                        cascade='all, delete-orphan')
    password_reset_tokens = db.relationship(
        'PasswordResetToken', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        """Flask-Login id including session_version for logout-on-password-change."""
        ver = int(getattr(self, 'session_version', 0) or 0)
        return f'{self.id}:{ver}'

    @property
    def has_api_key(self):
        return bool(self.api_key_hash)


class PasswordResetToken(db.Model):
    """One-time password-reset link. Store only the SHA-256 of the secret.

    Plaintext token lives in the email URL only. used_at marks both successful
    consumption and supersession by a newer request.
    """
    __tablename__ = 'password_reset_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class SavedFeed(db.Model):
    __tablename__ = 'saved_feeds'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    rss_url = db.Column(db.String(1024), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # New-episode email alerts (opt-in at follow time, default checked).
    email_new_episodes = db.Column(db.Boolean, nullable=False, default=True,
                                   server_default='1')
    # First poller run only records a baseline (no flood). Subsequent runs
    # email episodes newer than this watermark.
    alerts_initialized = db.Column(db.Boolean, nullable=False, default=False,
                                   server_default='0')
    last_seen_episode_guid = db.Column(db.String(1024), nullable=True)
    last_seen_published_ts = db.Column(db.Float, nullable=True)
    # Summary-by-email for followed shows (opt-in, default off; separate from alerts).
    email_summaries = db.Column(db.Boolean, nullable=False, default=False,
                                server_default='0')
    summary_email_trial_started_at = db.Column(db.DateTime, nullable=True)


class TranscriptionTask(db.Model):
    __tablename__ = 'transcription_tasks'

    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    episode_title = db.Column(db.String(512), nullable=False)
    rss_url = db.Column(db.String(1024), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    progress = db.Column(db.Integer, nullable=False, default=0)
    download_progress = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)
    transcript_text = db.Column(db.Text, nullable=True)
    segments_json = db.Column(db.Text, nullable=True)
    language = db.Column(db.String(10), nullable=True)
    audio_duration = db.Column(db.Float, nullable=True)
    transcription_time = db.Column(db.Float, nullable=True)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)

    # Episode metadata, so the progress page has something to show while it works
    podcast_name = db.Column(db.String(512), nullable=True)
    artwork_url = db.Column(db.String(1024), nullable=True)
    episode_published = db.Column(db.String(128), nullable=True)
    # Source audio URL so a failed job can be retried without re-searching.
    source_audio_url = db.Column(db.String(1024), nullable=True)
    # Optional listen destinations for share / result pages (http(s) only).
    # Populated at enqueue when known; Apple may be filled later via iTunes
    # lookup by feed URL (cached; never blocks a slow page render).
    source_spotify_url = db.Column(db.String(1024), nullable=True)
    source_apple_url = db.Column(db.String(1024), nullable=True)
    source_website_url = db.Column(db.String(1024), nullable=True)

    # Fine-grained progress state, read by /status to interpolate between checkpoints
    phase = db.Column(db.String(20), nullable=True)
    phase_started_at = db.Column(db.DateTime, nullable=True)
    chunk_index = db.Column(db.Integer, nullable=True)
    chunk_total = db.Column(db.Integer, nullable=True)
    bytes_downloaded = db.Column(db.BigInteger, nullable=True)
    bytes_total = db.Column(db.BigInteger, nullable=True)
    # Touched on every progress write, so a restart can tell a live task
    # (owned by another gunicorn worker) from one abandoned by a crash.
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    # Last /status poll from the result page — used to skip transcript-ready
    # email when the owner is still watching a short job.
    last_polled_at = db.Column(db.DateTime, nullable=True)

    # Seconds of audio currently reserved against the owner's trial allowance.
    # NULL for tasks run on the user's own key.
    trial_seconds_charged = db.Column(db.Integer, nullable=True)
    # Seconds reserved against paid credit-pack balance. NULL/0 when none used.
    paid_seconds_charged = db.Column(db.Integer, nullable=True)
    # Set once the charge above is final. The pro-rata refund computes a
    # fraction OF trial_seconds_charged and then overwrites it, so a second
    # refund would re-apply the fraction to the already-reduced value and hand
    # back seconds that had been spent. Settling is the claim; the amount is not.
    # Also covers paid_seconds_charged: both are settled together.
    # server_default matches the ALTER TABLE in USER/TASK_COLUMN_MIGRATIONS, so a
    # freshly created database and a migrated one have the same schema.
    trial_settled = db.Column(db.Boolean, nullable=False, default=False,
                              server_default='0')
    # JSON blob for free-preview jobs: {"partial_seconds":N,"episode_seconds":M}.
    # Dedicated column so completed previews never look like errors (error_message
    # stays reserved for real failures / cancel).
    partial_meta = db.Column(db.Text, nullable=True)
    # How many times boot recovery has re-queued this task after a process
    # death. 0 = never resumed; 1 = resumed once (second failure is terminal).
    resume_attempts = db.Column(db.Integer, nullable=False, default=0,
                                server_default='0')

    # Optional post-transcript AI summary (feature-flagged; never blocks completion).
    # summary_json shape: {tldr, key_points[], quotes[], is_partial, language}.
    summary_json = db.Column(db.Text, nullable=True)
    # null / pending / ready / error / skipped
    summary_status = db.Column(db.String(20), nullable=True)
    summary_model = db.Column(db.String(64), nullable=True)
    summary_prompt_tokens = db.Column(db.Integer, nullable=True)
    summary_completion_tokens = db.Column(db.Integer, nullable=True)
    summary_cost_usd_est = db.Column(db.Float, nullable=True)
    # When this row is a read-only copy for a summary-email subscriber, points at
    # the shared source task (same audio transcribed once).
    summary_source_task_id = db.Column(db.String(36), nullable=True)


class TranscriptShare(db.Model):
    """Opt-in public share link for a completed transcript.

    Default is not shared: a row exists only after the owner creates a link.
    Revoking sets revoked_at; the token then 404s. Tokens are unguessable
    (>=128-bit url-safe random). Never expose owner email or user id on the
    public page.
    """
    __tablename__ = 'transcript_shares'

    id = db.Column(db.Integer, primary_key=True)
    # secrets.token_urlsafe(22) is ~30 chars; 64 leaves headroom for rotation.
    token = db.Column(db.String(64), unique=True, nullable=False)
    task_id = db.Column(db.String(36), db.ForeignKey('transcription_tasks.id'),
                        nullable=False, unique=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    revoked_at = db.Column(db.DateTime, nullable=True)

    task = db.relationship(
        'TranscriptionTask',
        backref=db.backref('share', uselist=False),
    )


class CreditPurchase(db.Model):
    """One Stripe Checkout payment that credited paid minutes (or needs review).

    Idempotency is the unique stripe_session_id: a duplicate webhook must not
    credit the pack twice. status='needs_review' rows record paid-but-unmatched
    sessions without changing the balance.
    """
    __tablename__ = 'credit_purchases'

    id = db.Column(db.Integer, primary_key=True)
    # Nullable so a paid session with a missing/deleted user can still be
    # recorded as needs_review without a FK failure blocking the webhook 2xx.
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    stripe_session_id = db.Column(db.String(255), unique=True, nullable=False)
    stripe_event_id = db.Column(db.String(255), nullable=True)
    stripe_payment_intent_id = db.Column(db.String(255), nullable=True, index=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    amount_subtotal_cents = db.Column(db.Integer, nullable=True)
    amount_tax_cents = db.Column(db.Integer, nullable=True)
    amount_total_cents = db.Column(db.Integer, nullable=True)
    currency = db.Column(db.String(16), nullable=False, default='usd')
    customer_country = db.Column(db.String(2), nullable=True)
    minutes = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='credited',
                       server_default='credited')
    seconds_clawed_back = db.Column(db.Integer, nullable=False, default=0,
                                    server_default='0')
    amount_refunded_cents = db.Column(db.Integer, nullable=False, default=0,
                                      server_default='0')
    refunded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class EmailSentLog(db.Model):
    """Idempotency log for transactional mail — never send the same key twice."""
    __tablename__ = 'email_sent_log'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    kind = db.Column(db.String(64), nullable=False)
    idempotency_key = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class SummaryEmailJob(db.Model):
    """Queue for follow-show summary emails: one transcription per episode.

    Survives restarts (poller re-queues / continues). Funded by the platform
    key under SUMMARY_EMAIL_DAILY_MINUTES — never against user trial/paid.
    """
    __tablename__ = 'summary_email_jobs'

    id = db.Column(db.Integer, primary_key=True)
    # Stable dedupe key: sha256(rss_url)|sha256(episode_guid) truncated.
    idempotency_key = db.Column(db.String(255), unique=True, nullable=False)
    rss_url = db.Column(db.String(1024), nullable=False)
    audio_url = db.Column(db.String(1024), nullable=False)
    episode_guid = db.Column(db.String(1024), nullable=False)
    episode_title = db.Column(db.String(512), nullable=True)
    podcast_name = db.Column(db.String(512), nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)
    # queued | reserved | transcribing | summarizing | notifying | done | failed | skipped
    status = db.Column(db.String(32), nullable=False, default='queued',
                       server_default='queued')
    task_id = db.Column(db.String(36), nullable=True)
    skip_reason = db.Column(db.String(128), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    # Seconds reserved against the global daily summary-email budget.
    budget_seconds = db.Column(db.Integer, nullable=True)
    budget_day = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD UTC
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))


class SummaryEmailBudgetDay(db.Model):
    """Atomic daily spend counter for summary-email transcriptions (UTC day)."""
    __tablename__ = 'summary_email_budget_days'

    day = db.Column(db.String(10), primary_key=True)  # YYYY-MM-DD UTC
    seconds_used = db.Column(db.Integer, nullable=False, default=0,
                             server_default='0')


class TrialBudgetDay(db.Model):
    """Atomic daily free-trial spend counter (Europe/Oslo calendar day).

    Reservations bump ``seconds_used`` in the same transaction as the per-user
    ``users.trial_seconds_used`` UPDATE so concurrent gunicorn workers cannot
    overshoot ``TRIAL_DAILY_MINUTES``. Refunds / failed-before-Whisper releases
    decrement the Oslo day the task was started on.
    """
    __tablename__ = 'trial_budget_days'

    day = db.Column(db.String(10), primary_key=True)  # YYYY-MM-DD Europe/Oslo
    seconds_used = db.Column(db.Integer, nullable=False, default=0,
                             server_default='0')


#: Columns added after the first release, applied via ALTER TABLE on startup.
#: Keyed by column name so the migration stays declarative as the model grows.
TASK_COLUMN_MIGRATIONS = {
    'audio_duration': 'FLOAT',
    'podcast_name': 'VARCHAR(512)',
    'artwork_url': 'VARCHAR(1024)',
    'episode_published': 'VARCHAR(128)',
    'source_audio_url': 'VARCHAR(1024)',
    'source_spotify_url': 'VARCHAR(1024)',
    'source_apple_url': 'VARCHAR(1024)',
    'source_website_url': 'VARCHAR(1024)',
    'phase': 'VARCHAR(20)',
    'phase_started_at': 'DATETIME',
    'chunk_index': 'INTEGER',
    'chunk_total': 'INTEGER',
    'bytes_downloaded': 'BIGINT',
    'bytes_total': 'BIGINT',
    'heartbeat_at': 'DATETIME',
    'last_polled_at': 'DATETIME',
    'trial_seconds_charged': 'INTEGER',
    'paid_seconds_charged': 'INTEGER',
    'trial_settled': 'BOOLEAN NOT NULL DEFAULT 0',
    'partial_meta': 'TEXT',
    'resume_attempts': 'INTEGER NOT NULL DEFAULT 0',
    'summary_json': 'TEXT',
    'summary_status': 'VARCHAR(20)',
    'summary_model': 'VARCHAR(64)',
    'summary_prompt_tokens': 'INTEGER',
    'summary_completion_tokens': 'INTEGER',
    'summary_cost_usd_est': 'FLOAT',
    'summary_source_task_id': 'VARCHAR(36)',
}

#: Same, for the users table.
USER_COLUMN_MIGRATIONS = {
    'trial_seconds_limit': 'INTEGER',
    'trial_seconds_used': 'INTEGER NOT NULL DEFAULT 0',
    'paid_seconds_balance': 'INTEGER NOT NULL DEFAULT 0',
    'api_key_hash': 'VARCHAR(64)',
    'api_key_prefix': 'VARCHAR(16)',
    'api_key_created_at': 'DATETIME',
    'email_transcript_ready': 'BOOLEAN NOT NULL DEFAULT 1',
    'email_unsubscribed_at': 'DATETIME',
    'session_version': 'INTEGER NOT NULL DEFAULT 0',
}

#: Additive columns for password_reset_tokens. Applied by
#: ensure_password_reset_tokens_table AFTER the table exists; indexes that
#: mention a column are created only after that column is present.
PASSWORD_RESET_TOKEN_COLUMN_MIGRATIONS = {
    'used_at': 'DATETIME',
    'created_at': 'DATETIME',
}

#: Additive columns for saved_feeds (new-episode email alerts + summary email).
SAVED_FEED_COLUMN_MIGRATIONS = {
    'email_new_episodes': 'BOOLEAN NOT NULL DEFAULT 1',
    'alerts_initialized': 'BOOLEAN NOT NULL DEFAULT 0',
    'last_seen_episode_guid': 'VARCHAR(1024)',
    'last_seen_published_ts': 'FLOAT',
    # Opt-in: email a TL;DR summary of each new episode (separate from alerts).
    'email_summaries': 'BOOLEAN NOT NULL DEFAULT 0',
    # UTC timestamp when summary-email trial started for this follow (14-day free).
    'summary_email_trial_started_at': 'DATETIME',
}

#: Additive columns for credit_purchases (Stripe hardening). Applied by
#: ensure_credit_purchases_table / apply_column_migrations.
CREDIT_PURCHASE_COLUMN_MIGRATIONS = {
    'stripe_payment_intent_id': 'VARCHAR(255)',
    'amount_subtotal_cents': 'INTEGER',
    'amount_tax_cents': 'INTEGER',
    'amount_total_cents': 'INTEGER',
    'customer_country': 'VARCHAR(2)',
    'status': "VARCHAR(20) NOT NULL DEFAULT 'credited'",
    'seconds_clawed_back': 'INTEGER NOT NULL DEFAULT 0',
    'amount_refunded_cents': 'INTEGER NOT NULL DEFAULT 0',
    'refunded_at': 'DATETIME',
}

#: Additive columns for transcript_shares. Applied by
#: ensure_transcript_shares_table AFTER the table exists; indexes that mention
#: a column are created only after that column is present.
TRANSCRIPT_SHARE_COLUMN_MIGRATIONS = {
    'revoked_at': 'DATETIME',
}


class OAuthClient(db.Model):
    """OAuth 2.1 client from Dynamic Client Registration (RFC 7591).

    ChatGPT / Claude.ai connectors register themselves here. Public clients
    (token_endpoint_auth_method=none) store no secret.
    """
    __tablename__ = 'oauth_clients'

    id = db.Column(db.Integer, primary_key=True)
    # DCR mint is short (poc_…); CIMD client_ids are HTTPS metadata URLs.
    client_id = db.Column(db.String(512), unique=True, nullable=False)
    # NULL for public clients; SHA-256 hex when a secret was issued.
    client_secret_hash = db.Column(db.String(64), nullable=True)
    client_name = db.Column(db.String(255), nullable=True)
    # JSON arrays stored as text (same pattern as partial_meta elsewhere).
    redirect_uris_json = db.Column(db.Text, nullable=False)
    grant_types_json = db.Column(db.Text, nullable=False)
    response_types_json = db.Column(db.Text, nullable=False)
    token_endpoint_auth_method = db.Column(db.String(64), nullable=False,
                                           default='none', server_default='none')
    # JSON list of accepted methods (e.g. ["none","private_key_jwt"] for ChatGPT).
    token_endpoint_auth_methods_json = db.Column(db.Text, nullable=True)
    # CIMD jwks_uri for private_key_jwt (same host as client_id).
    jwks_uri = db.Column(db.String(512), nullable=True)
    # 'dcr' | 'cimd' — how this client was first learned.
    registration_source = db.Column(db.String(16), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class OAuthAuthorizationCode(db.Model):
    """Single-use authorization code (PKCE S256). Store only the hash."""
    __tablename__ = 'oauth_authorization_codes'

    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), unique=True, nullable=False)
    client_id = db.Column(db.String(512), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                        index=True)
    redirect_uri = db.Column(db.String(1024), nullable=False)
    code_challenge = db.Column(db.String(128), nullable=False)
    code_challenge_method = db.Column(db.String(16), nullable=False)
    scope = db.Column(db.String(255), nullable=True)
    # RFC 8707 resource indicator (audience), e.g. https://podskrift.com/mcp
    resource = db.Column(db.String(512), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class OAuthAccessToken(db.Model):
    """Short-lived access token. Plaintext shown once to the client; DB has hash."""
    __tablename__ = 'oauth_access_tokens'

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    client_id = db.Column(db.String(512), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                        index=True)
    scope = db.Column(db.String(255), nullable=True)
    resource = db.Column(db.String(512), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class OAuthRefreshToken(db.Model):
    """Refresh token with rotation. Revoking one client connection clears these."""
    __tablename__ = 'oauth_refresh_tokens'

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    client_id = db.Column(db.String(512), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                        index=True)
    scope = db.Column(db.String(255), nullable=True)
    resource = db.Column(db.String(512), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    # Set when this token is rotated; the replacement's hash (audit trail).
    replaced_by_hash = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class OAuthJwtJti(db.Model):
    """Single-use jti values from private_key_jwt client assertions (RFC 7523)."""
    __tablename__ = 'oauth_jwt_jtis'

    jti_hash = db.Column(db.String(64), primary_key=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


#: Additive columns for oauth_* tables (future ALTERs). Applied by
#: ensure_oauth_tables AFTER CREATE TABLE IF NOT EXISTS.
OAUTH_CLIENT_COLUMN_MIGRATIONS = {
    'registration_source': 'VARCHAR(16)',
    'jwks_uri': 'VARCHAR(512)',
    'token_endpoint_auth_methods_json': 'TEXT',
}
OAUTH_AUTHORIZATION_CODE_COLUMN_MIGRATIONS = {}
OAUTH_ACCESS_TOKEN_COLUMN_MIGRATIONS = {}
OAUTH_REFRESH_TOKEN_COLUMN_MIGRATIONS = {
    'replaced_by_hash': 'VARCHAR(64)',
}
