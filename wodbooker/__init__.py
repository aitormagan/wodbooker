import os
import os.path as op
from datetime import datetime, timedelta
import time
import threading
import subprocess
import pickle
import requests
import logging
from flask import Flask, redirect, request, session, g

from flask_admin import Admin
import flask_login as login
from flask_babel import Babel
from flask_wtf.csrf import CSRFProtect
from .views import MyAdminIndexView, BookingAdmin, EventView, UserView
from .models import User, Booking, Event, db
from .booker import start_booking_loop
from .mailer import process_maling_queue

# Configure logging
logging.basicConfig(format='%(asctime)s - %(threadName)s - %(message)s', level=logging.INFO)

# Get version
__git_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/.git"
_VERSION = subprocess.check_output(["git", f"--git-dir={__git_dir}",
                                        "describe", "--tags"]).strip().decode('utf-8')


def get_locale():
    if request.args.get('lang'):
        session['lang'] = request.args.get('lang')
    return session.get('lang', 'es')

# Create application
app = Flask(__name__)
babel = Babel(app, locale_selector=get_locale)
csrf = CSRFProtect()
csrf.init_app(app)

# Create dummy secrey key so we can use sessions
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '123456790')

# Create in-memory database
app.config['DATABASE_FILE'] = 'db.sqlite'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + \
    app.config['DATABASE_FILE'] + '?check_same_thread=False'
app.config['SQLALCHEMY_ECHO'] = False
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['CSRF_ENABLED'] = True
app.config['RECAPTCHA_PUBLIC_KEY'] = os.environ.get('RECAPTCHA_PUBLIC_KEY')
app.config['RECAPTCHA_PRIVATE_KEY'] = os.environ.get('RECAPTCHA_PRIVATE_KEY')

# Build a sample db on the fly, if one does not exist yet.
app_dir = op.realpath(os.path.dirname(__file__))
database_path = op.join(app_dir, app.config['DATABASE_FILE'])
if not os.path.exists(database_path):
    db.app = app
    with app.app_context():
        db.init_app(app)
        db.create_all()
else:
    db.init_app(app)


def _init_login():
    login_manager = login.LoginManager()
    login_manager.init_app(app)

    # Create user loader function
    @login_manager.user_loader
    def load_user(user_id):
        return db.session.query(User).get(user_id)


@app.before_request
def check_session_expired():
    """
    Check if the session has expired and logout the user if it has
    """
    if "static" not in request.path and login.current_user.is_authenticated:
        if login.current_user.force_login:
            login.logout_user()
        else:
            _session = requests.Session()
            _session.cookies.update(pickle.loads(login.current_user.cookie))
            try:
                expiration_timestamp = next(x for x in _session.cookies if x.name == '.WBAuth').expires
                expiration_date = datetime.fromtimestamp(expiration_timestamp)
                if datetime.now() > expiration_date:
                    login.logout_user()
            except (StopIteration, TypeError):
                logging.exception("Error while getting expiration date of cookie")


@app.before_request
def set_version():
    """
    Set version in g object
    """
    g.version = _VERSION


@app.before_request
def redirect_admin():
    """
    Redirect users from deprecated /admin/... to /...
    """
    if request.path.startswith('/admin'):
        return redirect(request.full_path.replace('/admin', ''))

_init_login()

# Create admin
admin = Admin(app, name='WodBooker', index_view=MyAdminIndexView(url="/"),
              base_template='base.html', template_mode='bootstrap4')

# Add views
admin.add_view(BookingAdmin(Booking, db.session, 'Reservas'))
admin.add_view(EventView(Event, db.session, 'Eventos'))
admin.add_view(UserView(User, db.session, 'Usuarios'))

# Start booking loop
with app.app_context():
    _bookings = db.session.query(Booking).all()
    for _booking in _bookings:
        if _booking.is_active:
            start_booking_loop(_booking)

# Start events cleaning loop
def _cleaning_loop(app_context):
    app_context.push()
    with app_context:
        while True:
            logging.info("Cleaning events older than 15 days")
            bookings = db.session.query(Booking).all()
            for booking in bookings:
                events_older_than_15_days = list(filter(lambda x: x.date < datetime.now() - timedelta(days=15),
                                                        booking.events[:-1]))
                events_older_than_15_days = sorted(events_older_than_15_days, key=lambda x: x.date)
                for event in events_older_than_15_days:
                    db.session.delete(event)
            db.session.commit()
            time.sleep(60 * 60 * 24)

thread_cleaner = threading.Thread(target=_cleaning_loop,
                                  args=(app.app_context(),),
                                  daemon=True, name="dbcleaner")
thread_cleaner.start()

thread_mailer = threading.Thread(target=process_maling_queue,
                                 daemon=True, name="mailer")
thread_mailer.start()
