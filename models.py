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
    trial_seconds_used = db.Column(db.Integer, nullable=False, default=0)

    feeds = db.relationship('SavedFeed', backref='user', lazy=True, cascade='all, delete-orphan')
    tasks = db.relationship('TranscriptionTask', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


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
    # NULL for tasks run on the user's own key; 0 once a failed task has been
    # refunded, which is also what makes the refund idempotent.
    trial_seconds_charged = db.Column(db.Integer, nullable=True)


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
}

#: Same, for the users table.
USER_COLUMN_MIGRATIONS = {
    'trial_seconds_limit': 'INTEGER',
    'trial_seconds_used': 'INTEGER NOT NULL DEFAULT 0',
}
