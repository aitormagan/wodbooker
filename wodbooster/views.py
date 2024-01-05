import logging
from flask import redirect, url_for, request, flash
from markupsafe import Markup
from wtforms import form, fields, validators
from flask_admin.form.fields import TimeField
import flask_login as login
from flask_admin import AdminIndexView, helpers, expose
from flask_admin.contrib import sqla
from requests.exceptions import RequestException
from sqlalchemy import and_
from .models import User, db, Booking, Event
from .booker import start_booking_loop, stop_booking_loop
from .scraper import refresh_scraper
from .exceptions import LoginError, InvalidWodBusterResponse

_DAYS_OF_WEEK = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_NO_EVENTS = "Aún no hay eventos registrados para esta reserva. Los eventos aparecerán aquí " + \
        "cuando la reserva esté activa según vayan ocurriendo."


class LoginForm(form.Form):
    email = fields.StringField(validators=[validators.DataRequired()])
    password = fields.PasswordField(validators=[validators.DataRequired()])

    def __init__(self,
        formdata=None,
        obj=None,
        prefix="",
        data=None,
        meta=None,
        **kwargs,
    ) -> None:
        super().__init__(formdata, obj, prefix, data, meta, **kwargs)
        self._scraper = None

    def validate_password(self, field):
        try:
            self._scraper = refresh_scraper(self.email.data, self.password.data)
        except LoginError as e:
            logging.exception("Login Error")
            raise validators.ValidationError("Las credenciales introducidas son incorrectas") from e
        except InvalidWodBusterResponse as e:
            logging.exception("Invalid WodBuster Response")
            raise validators.ValidationError("La respuesta de WodBuster no fue la esperada. Inténtalo de nuevo en unos minutos...") from e
        except RequestException as e:
            logging.exception("Request Error")
            raise validators.ValidationError("Error inesperado de red al intentar acceder. Inténtalo de nuevo en unos minutos...") from e

    def get_user(self):
        existing_user = db.session.query(User).filter_by(email=self.email.data).first()
        if existing_user:
            existing_user.cookie = self._scraper.get_cookies()
            db.session.commit()
            return existing_user
        else:
            user = User()
            user.email = self.email.data
            user.cookie = self._scraper.get_cookies()
            db.session.add(user)
            db.session.commit()
            return user


# Create customized index view class that handles login & registration
class MyAdminIndexView(AdminIndexView):

    def is_visible(self):
        # This view won't appear in the menu structure
        return False

    @expose('/')
    def index(self):
        if not login.current_user.is_authenticated:
            return redirect(url_for('.login_view'))
        return redirect(url_for('booking.index_view'))

    @expose('/login/', methods=('GET', 'POST'))
    def login_view(self):
        # handle user login
        form = LoginForm(request.form)
        if helpers.validate_form_on_submit(form):
            user = form.get_user()
            login.login_user(user)

        if login.current_user.is_authenticated:
            return redirect(url_for('booking.index_view'))
        self._template_args['form'] = form
        return super(MyAdminIndexView, self).index()

    @expose('/logout/')
    def logout_view(self):
        login.logout_user()
        return redirect(url_for('.index'))


class BookingForm(form.Form):

    dow = fields.SelectField('Día de la semana', choices=[(0, 'Lunes'), (1, 'Martes'), (
        2, 'Miércoles'), (3, 'Jueves'), (4, 'Viernes'), (5, 'Sábado'), (6, 'Domingo')])

    time = TimeField('Hora')
    url = fields.StringField('URL de WodBuster (ej: https://YOUR_BOX.wodbuster.com)')
    offset = fields.IntegerField('Días de antelación para reservar')
    available_at = TimeField('Hora de apertura de reservas')
    is_active = fields.BooleanField('Activo', default=True)

    def validate_dow(self, field):
        if db.session.query(Booking).filter(
            and_(
                Booking.dow==field.data,
                Booking.user==login.current_user,
                Booking.url==self.url.data,
                Booking.time==self.time.data,
                Booking.id!=request.args.get('id'))).first():
            raise validators.ValidationError("Ya existe una reserva para ese día de la semana, hora y box")


def _parse_events(v, c, m, p):
    events = m.events
    if events:
        parsed_events = [f"<li>{x.date.strftime('%d/%m/%Y %H:%M')}: {x.event}</li>" for x in events]
        parsed_events = "<ul>" + "".join(parsed_events) + "</ul>"
        parsed_events = Markup(parsed_events)
    else:
        parsed_events = Markup(f"<i>{_NO_EVENTS}</i>")
    
    return parsed_events


class BookingAdmin(sqla.ModelView):
    form = BookingForm

    column_labels = dict(dow='Día de la semana', time='Hora', events='Eventos',
                         url='Box', is_active='Activo')
    column_list = ('dow', 'time', 'is_active', 'url', 'events')

    column_formatters = dict(
        dow=lambda v, c, m, p: _DAYS_OF_WEEK[m.dow],
        url=lambda v, c, m, p: Markup(f'<a href="{m.url}">{m.url.split("/")[2].split(".")[0]}</a>'),
        events=_parse_events,
    )

    def get_query(self):
        query = super().get_query()
        query = query.filter_by(user_id=login.current_user.id)
        return query

    def get_one(self, id):
        result = super().get_one(id)
        if result.user_id != login.current_user.id:
            return None
        return result

    def is_accessible(self):
        return login.current_user.is_authenticated

    def update_model(self, form, model):
        if login.current_user.is_authenticated and model.user_id != login.current_user.id:
            flash("No estás autorizado a editar este elemento", "warning")
            return False

        stop_booking_loop(model)
        returned_value = super().update_model(form, model)
        if model.is_active:
            start_booking_loop(model)
        return returned_value

    def delete_model(self, model):
        if login.current_user.is_authenticated and model.user_id != login.current_user.id:
            flash("No estás autorizado a borrar este elemento", "warning")
            return False
        stop_booking_loop(model)
        return super().delete_model(model)

    def inaccessible_callback(self, name, **kwargs):
        # redirect to login page if user doesn't have access
        return redirect(url_for('admin.login_view', next=request.url))

    def create_model(self, form):
        booking = super().create_model(form)
        booking.user = login.current_user
        db.session.flush()
        db.session.commit()
        if booking.is_active:
            start_booking_loop(booking)
        return booking

    def create_form(self, obj=None):
        form = super().create_form(obj)
        last_booking = db.session.query(Booking).filter_by(user=login.current_user).order_by(Booking.id.desc()).first()
        if last_booking:
            form.url.data = form.url.data or last_booking.url
            form.offset.data = form.offset.data or last_booking.offset
            form.available_at.data = form.available_at.data or last_booking.available_at

        return form
