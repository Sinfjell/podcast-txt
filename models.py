"""Database models for Podcast Transcriber."""

import os
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

    feeds = db.relationship('SavedFeed', backref='user', lazy=True, cascade='all, delete-orphan')
    transcriptions = db.relationship('Transcription', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_openai_key(self):
        """Return user's key, or fall back to global env key."""
        return self.openai_api_key or os.getenv('OPENAI_API_KEY')


class SavedFeed(db.Model):
    __tablename__ = 'saved_feeds'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    rss_url = db.Column(db.String(1024), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Transcription(db.Model):
    __tablename__ = 'transcriptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    episode_title = db.Column(db.String(512), nullable=False)
    rss_url = db.Column(db.String(1024), nullable=True)
    language = db.Column(db.String(10), nullable=True)
    transcript_text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
