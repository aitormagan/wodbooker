from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Booking(db.Model):

    STATUS_SEPARATOR = '\n'

    id = db.Column(db.Integer, primary_key=True)
    dow = db.Column(db.Integer)
    time = db.Column(db.Time)
    booked_at = db.Column(db.DateTime)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User')
    last_book_date = db.Column(db.Date)
    url = db.Column(db.String(128))
    available_at = db.Column(db.Time)
    offset = db.Column(db.Integer)
    events = db.relationship('Event', backref='booking', lazy=True, cascade="all, delete-orphan")
    is_active = db.Column(db.Boolean, default=True)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'))
    date = db.Column(db.DateTime, default=datetime.now())
    event = db.Column(db.String(256))

    def __str__(self):
        return f"{self.date.strftime('%d/%m/%Y %H:%M')}: {self.event}"


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    cookie = db.Column(db.String(1024))

    # Flask-Login integration
    # NOTE: is_authenticated, is_active, and is_anonymous
    # are methods in Flask-Login < 0.3.0
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def is_active(self):
        return True

    def get_id(self):
        return self.id

    # Required for administrative interface
    def __unicode__(self):
        return self.email
