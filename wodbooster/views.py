from flask import Flask, redirect, url_for, request
from wtforms import form, fields, validators, widgets
from flask_admin.form.fields import TimeField
import flask_login as login
from flask_admin import Admin, AdminIndexView, helpers, expose
from flask_admin.contrib import sqla
from werkzeug.security import generate_password_hash, check_password_hash
from .models import User, db
import calendar

class LoginForm(form.Form):
    email = fields.StringField(validators=[validators.required()])
    password = fields.PasswordField(validators=[validators.required()])

    def validate_email(self, field):
        user = self.get_user()
        if user is None:
            raise validators.ValidationError('Invalid user')

        # we're comparing the plaintext pw with the the hash from the db
        # if not check_password_hash(user.password, self.password.data):
        # to compare plain text passwords use
        if user.password != self.password.data:
            raise validators.ValidationError('Invalid password')

    def get_user(self):
        return db.session.query(User).filter_by(email=self.email.data).first()


class RegistrationForm(form.Form):
    email = fields.StringField(validators=[validators.required()])
    password = fields.PasswordField(validators=[validators.required()])

    def validate_email(self, field):
        if db.session.query(User).filter_by(email=self.email.data).count() > 0:
            raise validators.ValidationError('Duplicate username')


# Create customized index view class that handles login & registration
class MyAdminIndexView(AdminIndexView):
    def is_visible(self):
        # This view won't appear in the menu structure
        return False

    @expose('/')
    def index(self):
        if not login.current_user.is_authenticated:
            return redirect(url_for('.login_view'))
        return super(MyAdminIndexView, self).index()

    @expose('/login/', methods=('GET', 'POST'))
    def login_view(self):
        # handle user login
        form = LoginForm(request.form)
        if helpers.validate_form_on_submit(form):
            user = form.get_user()
            login.login_user(user)

        if login.current_user.is_authenticated:
            return redirect(url_for('.index'))
        link = '<p>Don\'t have an account? <a href="' + \
            url_for('.register_view') + '">Click here to register.</a></p>'
        self._template_args['form'] = form
        self._template_args['link'] = link
        return super(MyAdminIndexView, self).index()

    @expose('/register/', methods=('GET', 'POST'))
    def register_view(self):
        form = RegistrationForm(request.form)
        if helpers.validate_form_on_submit(form):
            user = User()

            form.populate_obj(user)
            # we hash the users password to avoid saving it as plaintext in the db,
            # remove to use plain text:
            # user.password = generate_password_hash(form.password.data)

            db.session.add(user)
            db.session.commit()

            login.login_user(user)
            return redirect(url_for('.index'))
        link = '<p>Already have an account? <a href="' + \
            url_for('.login_view') + '">Click here to log in.</a></p>'
        self._template_args['form'] = form
        self._template_args['link'] = link
        return super(MyAdminIndexView, self).index()

    @expose('/logout/')
    def logout_view(self):
        login.logout_user()
        return redirect(url_for('.index'))


class BookingForm(form.Form):

    dow = fields.SelectField('Day of the week', choices=[(0, 'Monday'), (1, 'Tuesday'), (
        2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')])

    time = TimeField('Time')


class BookingAdmin(sqla.ModelView):
    form = BookingForm

    column_labels = dict(dow='Day of the week')
    column_list = ('dow', 'time')

    def get_list(self, *args, **kwargs):
        count, data = super(BookingAdmin, self).get_list(*args, **kwargs)
        for item in data:
            item.dow = calendar.day_name[item.dow]
        return count, data

    def validate_form(self, form):
        if 'dow' in form and form.dow.data:
            form.dow.data = int(form.dow.data)
        return super(BookingAdmin, self).validate_form(form)

    def is_accessible(self):
        return login.current_user.is_authenticated

    def inaccessible_callback(self, name, **kwargs):
        # redirect to login page if user doesn't have access
        return redirect(url_for('admin.login_view', next=request.url))

    def create_model(self, form):
        booking = super().create_model(form)
        booking.user = login.current_user
        db.session.flush()
        db.session.commit()
        return booking
