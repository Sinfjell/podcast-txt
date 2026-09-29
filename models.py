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
    password_hash = db.Column(db.String(255), nullable=False)
    openai_api_key = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Trial metering. Only ever touched for users transcribing on OUR key --
    # a user with their own key spends their own quota and is never metered.
    # A NULL limit means "use the configured default", so raising TRIAL_MINUTES
    # lifts every account that has not been given an individual grant.
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

    feeds = db.relationship('SavedFeed', backref='user', lazy=True, cascade='all, delete-orphan')
    tasks = db.relationship('TranscriptionTask', backref='user', lazy=True, cascade='all, delete-orphan')
    credit_purchases = db.relationship('CreditPurchase', backref='user', lazy=True,
                                       cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def has_api_key(self):
        return bool(self.api_key_hash)


class SavedFeed(db.Model):
    __tablename__ = 'saved_feeds'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    rss_url = db.Column(db.String(1024), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


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


class CreditPurchase(db.Model):
    """One Stripe Checkout payment that credited paid minutes.

    Idempotency is the unique stripe_session_id: a duplicate webhook must not
    credit the pack twice. ``refunded_seconds`` is how much of the pack a
    refund has already taken back, so a repeated charge.refunded event claws
    back only the difference.
    """
    __tablename__ = 'credit_purchases'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    stripe_session_id = db.Column(db.String(255), unique=True, nullable=False)
    stripe_event_id = db.Column(db.String(255), nullable=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(16), nullable=False, default='usd')
    minutes = db.Column(db.Integer, nullable=False)
    stripe_payment_intent_id = db.Column(db.String(255), nullable=True)
    refunded_seconds = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


#: Columns added after the first release, applied via ALTER TABLE on startup.
#: Keyed by column name so the migration stays declarative as the model grows.
TASK_COLUMN_MIGRATIONS = {
    'audio_duration': 'FLOAT',
    'podcast_name': 'VARCHAR(512)',
    'artwork_url': 'VARCHAR(1024)',
    'episode_published': 'VARCHAR(128)',
    'phase': 'VARCHAR(20)',
    'phase_started_at': 'DATETIME',
    'chunk_index': 'INTEGER',
    'chunk_total': 'INTEGER',
    'bytes_downloaded': 'BIGINT',
    'bytes_total': 'BIGINT',
    'heartbeat_at': 'DATETIME',
    'trial_seconds_charged': 'INTEGER',
    'paid_seconds_charged': 'INTEGER',
    'trial_settled': 'BOOLEAN NOT NULL DEFAULT 0',
}

#: Same, for the users table.
USER_COLUMN_MIGRATIONS = {
    'trial_seconds_limit': 'INTEGER',
    'trial_seconds_used': 'INTEGER NOT NULL DEFAULT 0',
    'paid_seconds_balance': 'INTEGER NOT NULL DEFAULT 0',
    'api_key_hash': 'VARCHAR(64)',
    'api_key_prefix': 'VARCHAR(16)',
    'api_key_created_at': 'DATETIME',
}

#: Same, for the credit_purchases table.
PURCHASE_COLUMN_MIGRATIONS = {
    'stripe_payment_intent_id': 'VARCHAR(255)',
    'refunded_seconds': 'INTEGER NOT NULL DEFAULT 0',
}
