from datetime import datetime, timedelta, date, time
from abc import ABC, abstractmethod
import random
import logging
import pause
import pytz
from flask import current_app as app
from func_timeout import StoppableThread
from requests.exceptions import RequestException
from .constants import EventMessage, UNEXPECTED_ERROR_MAIL_SUBJECT, \
    UNEXPECTED_ERROR_MAIL_BODY, FULL_CLASS_BOOKED_MAIL_SUBJECT, \
    FULL_CLASS_BOOKED_MAIL_BODY, ERROR_AUTOHEALED_MAIL_SUBJECT, \
    ERROR_AUTOHEALED_MAIL_BODY, CLASS_BOOKED_MAIL_SUBJECT, \
    CLASS_BOOKED_MAIL_BODY
from .scraper import get_scraper, Scraper
from .mailer import send_email, ErrorEmail, SuccessAfterErrorEmail, SuccessEmail
from .exceptions import BookingNotAvailable, InvalidWodBusterResponse, \
    ClassIsFull, LoginError, PasswordRequired, InvalidBox, \
    ClassNotFound, BookingFailed
from .models import db, Booking, Event

_MADRID_TZ = pytz.timezone('Europe/Madrid')

_MAX_ERRORS = 5

__CURRENT_THREADS = {
}


def _get_next_date_for_weekday(base_date: date, weekday: int) -> date:
    """ 
    Get the next date for a given weekday. If the weekday is the same as the base date, the base date is returned
    :param base_date: The base date to start the search
    :param weekday: The weekday to search 
    """
    days_ahead = weekday - base_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return base_date + timedelta(days_ahead)


def _get_datetime_to_book(last_booking_date: date, dow: int, booking_time: time) -> datetime:
    """
    Get the day to book for a given date and day of week
    :param last_booking_date: The last booking date
    :param dow: The day of week
    :param booking_time: The time to book
    :return: The datetime to book
    """
    now = datetime.now(_MADRID_TZ)
    base_date = now.date() if not last_booking_date else last_booking_date + timedelta(days=1)
    day_to_book = _get_next_date_for_weekday(base_date, dow)
    datetime_to_book = _MADRID_TZ.localize(datetime.combine(day_to_book, booking_time))

    if now > datetime_to_book:
        day_to_book = _get_next_date_for_weekday(now.date() + timedelta(days=1), dow)
        datetime_to_book = _MADRID_TZ.localize(datetime.combine(day_to_book, booking_time))

    return datetime_to_book


class _StopThreadException(BaseException):
    pass


class Booker(StoppableThread):

    def __init__(self, booking: Booking, app_context):
        """
        :param booking: The booking to run
        :param app_context: The Flask app context
        """
        super(Booker, self).__init__()
        self._booking = None
        self._booking_id = booking.id
        self._session = None
        self._app_context = app_context
        self.name = f"Booker {self._booking_id}"

    def run(self) -> None:
        try:
            self._app_context.push()
            self._booking = db.session.query(Booking).filter_by(id=self._booking_id).first()
            errors = 0
            force_exit = False
            waiter = None
            datetime_to_book = None
            skip_current_week = False
            class_is_full_notification_sent = False
            while errors < _MAX_ERRORS and not force_exit:
                try:
                    book_time = time(self._booking.time.hour, self._booking.time.minute, 0)
                    _datetime_to_book = _get_datetime_to_book(self._booking.last_book_date, self._booking.dow, book_time)

                    if waiter and datetime_to_book != _datetime_to_book:
                        logging.info("Waiting for class %s is over.", datetime_to_book.strftime('%d/%m/%Y %H:%M'))
                        event = Event(booking_id=self._booking.id,
                                      event=EventMessage.CLASS_WAITING_OVER % (datetime_to_book.strftime('%d/%m/%Y'), _datetime_to_book.strftime('%d/%m/%Y')))
                        _add_event(event)
                        class_is_full_notification_sent = False
                        waiter = None
                    elif datetime_to_book == _datetime_to_book and skip_current_week:
                        _datetime_to_book = _datetime_to_book + timedelta(days=7)
                        skip_current_week = False

                    datetime_to_book = _datetime_to_book
                    day_to_book = datetime_to_book.date()

                    book_available_at = _MADRID_TZ.localize(
                        datetime.combine(
                            day_to_book - timedelta(days=self._booking.offset),
                            self._booking.available_at))

                    waiter = waiter or _TimeWaiter(self._booking, EventMessage.WAIT_UNTIL_BOOKING_OPEN % (book_available_at.strftime('%d/%m/%Y a las %H:%M'),
                                                                                                          day_to_book.strftime('%d/%m/%Y')),
                                                   book_available_at)

                    if waiter:
                        waiter.wait()
                    waiter = None

                    # Refresh the scraper in case a new one is avaiable
                    scraper = get_scraper(self._booking.user.email, self._booking.user.cookie)
                    # generate a random number between 30 and 6o seconds to avoid being detected as a bot
                    sleep = random.randint(15, 60)
                    logging.info("Sleeping for %s seconds", sleep)
                    pause.seconds(sleep)
                    scraper.book(self._booking.url, datetime_to_book)
                    logging.info("Booking for user %s at %s completed successfully", self._booking.user.email, datetime_to_book.strftime('%d/%m/%Y %H:%M'))
                    event = Event(booking_id=self._booking.id, event=EventMessage.BOOKING_COMPLETED % day_to_book.strftime('%d/%m/%Y'))
                    _add_event(event)

                    email = None
                    if errors > 0:
                        email = SuccessAfterErrorEmail(self._booking, ERROR_AUTOHEALED_MAIL_SUBJECT, ERROR_AUTOHEALED_MAIL_BODY)
                        errors = 0

                    if class_is_full_notification_sent:
                        email = SuccessAfterErrorEmail(self._booking, FULL_CLASS_BOOKED_MAIL_SUBJECT, FULL_CLASS_BOOKED_MAIL_BODY)
                        class_is_full_notification_sent = False

                    email = email or SuccessEmail(self._booking, CLASS_BOOKED_MAIL_SUBJECT, CLASS_BOOKED_MAIL_BODY)
                    send_email(self._booking.user, email)

                    self._booking.last_book_date = day_to_book
                    self._booking.booked_at = datetime.now().replace(microsecond=0)
                    self._booking.user.cookie = scraper.get_cookies()
                except ClassNotFound as e:
                    logging.warning("Class not found. Ignoring this week and attempting booking for next week %s", e)
                    skip_current_week = True
                    event = Event(booking_id=self._booking.id, event=EventMessage.CLASS_NOT_FOUND % (datetime_to_book.strftime("%d/%m/%Y"), datetime_to_book.strftime("%H:%M")))
                    _add_event(event)
                    send_email(self._booking.user, ErrorEmail(self._booking, "Clase no encontrada", event.event))
                except BookingFailed as e:
                    logging.warning("Class cannot be booked %s", e)
                    skip_current_week = True
                    event = Event(booking_id=self._booking.id, event=EventMessage.BOOKING_ERROR % (datetime_to_book.strftime("%d/%m/%Y"), str(e).rstrip(".")))
                    _add_event(event)
                    send_email(self._booking.user, ErrorEmail(self._booking, "Error en la reserva", event.event))
                except ClassIsFull:
                    logging.info("Class is full. Setting wait for event to 'changedBooking'")
                    waiter = _EventWaiter(self._booking, EventMessage.CLASS_FULL % day_to_book.strftime('%d/%m/%Y'),
                                          scraper, self._booking.url, day_to_book, ['changedBooking'], datetime_to_book)
                    if not class_is_full_notification_sent:
                        send_email(self._booking.user, ErrorEmail(self._booking, "Clase llena", waiter.log_message))
                        class_is_full_notification_sent = True
                except BookingNotAvailable as e:
                    if e.available_at:
                        logging.info("Class is not bookeable yet. Setting wait for datetime to %s", e.available_at.strftime('%d/%m/%Y %H:%M'))
                        waiter = _TimeWaiter(self._booking, EventMessage.WAIT_UNTIL_BOOKING_OPEN % (e.available_at.strftime('%d/%m/%Y a las %H:%M'),
                                                                                                    day_to_book.strftime('%d/%m/%Y')),
                                             e.available_at)
                    else:
                        logging.info("Classes for %s are not loaded yet. Waiting for any type of event", day_to_book.strftime('%d/%m/%Y'))
                        waiter = _EventWaiter(self._booking, EventMessage.WAIT_CLASS_LOADED % day_to_book.strftime('%d/%m/%Y'),
                                              scraper, self._booking.url, day_to_book,
                                              ['changedPizarra', 'changedBooking'], datetime_to_book)
                    continue
                except RequestException as e:
                    sleep_for = (errors + 1) * 60
                    logging.warning("Request Exception: %s", e)
                    waiter = _TimeWaiter(self._booking, EventMessage.UNEXPECTED_NETWORK_ERROR % sleep_for,
                                            datetime.now(_MADRID_TZ) + timedelta(seconds=sleep_for))
                    if errors == 0:
                        send_email(self._booking.user, ErrorEmail(self._booking, UNEXPECTED_ERROR_MAIL_SUBJECT,
                                                                  UNEXPECTED_ERROR_MAIL_BODY))
                    errors += 1
                except InvalidWodBusterResponse as e:
                    sleep_for = (errors + 1) * 60
                    logging.warning("Invalid WodBuster response: %s", e)
                    waiter = _TimeWaiter(self._booking, EventMessage.UNEXPECTED_WODBUSTER_RESPONSE % sleep_for,
                                         datetime.now(_MADRID_TZ) + timedelta(seconds=sleep_for))
                    if errors == 0:
                        send_email(self._booking.user, ErrorEmail(self._booking, UNEXPECTED_ERROR_MAIL_SUBJECT,
                                                                  UNEXPECTED_ERROR_MAIL_BODY))
                    errors += 1
                except PasswordRequired:
                    force_exit = True
                    logging.warning("Credentials for user %s are outdated. Aborting...", self._booking.user.email)
                    self._booking.user.force_login = True
                    event = Event(booking_id=self._booking.id, event=EventMessage.CREDENTIALS_EXPIRED)
                    _add_event(event)
                    send_email(self._booking.user, ErrorEmail(self._booking, "Credenciales caducadas", event.event))
                except LoginError:
                    force_exit = True
                    logging.warning("User %s cannot be logged in into WodBuster. Aborting...", self._booking.user.email)
                    self._booking.user.force_login = True
                    event = Event(booking_id=self._booking.id, event=EventMessage.LOGIN_FAILED)
                    _add_event(event)
                    send_email(self._booking.user, ErrorEmail(self._booking, "Login fallido", event.event))
                except InvalidBox:
                    force_exit = True
                    logging.warning("User %s accessing to an invalid box detected. Aborting...", self._booking.user.email)
                    event = Event(booking_id=self._booking.id, event=EventMessage.INVALID_BOX_URL)
                    _add_event(event)
                    send_email(self._booking.user, ErrorEmail(self._booking, "Box inválido", event.event))
                finally:
                    db.session.commit()

            if errors >= _MAX_ERRORS:
                logging.error("Exiting thread as maximum number of retries has been reached. Review logs for more information")
                event = Event(booking_id=self._booking.id, event=EventMessage.TOO_MANY_ERRORS)
                _add_event(event)
                db.session.commit()
            logging.info("Exiting thread...")
        except _StopThreadException:
            logging.info("Thread %s has been stopped", self._name)
        except Exception:
            logging.exception("Unexpected error while booking. Aborting...")


class _Waiter(ABC):

    def __init__(self, booking: Booking, log_message: str) -> None:
        """
        Waiter construction
        :param booking: The booking to run
        :param log_message: The message to log
        """
        self.booking = booking
        self.log_message = log_message

    @abstractmethod
    def wait(self):
        """
        Wait until the condition is met
        """
        raise NotImplementedError()


class _TimeWaiter(_Waiter):

    def __init__(self, booking: Booking, log_message: str, wait_datetime: datetime) -> None:
        """
        Time Waiter construction
        :param booking: The booking the waiter is related to
        :param log_message: The message related to the waiter
        :param datetime: The datetime to wait for
        """
        super().__init__(booking, log_message)
        self._wait_datetime = wait_datetime

    def wait(self):
        """
        Wait until the provided date is reached
        """
        if self._wait_datetime > datetime.now(_MADRID_TZ):
            logging.info("Waiting until %s", self._wait_datetime.strftime('%d/%m/%Y %H:%M:%S'))
            event = Event(booking_id=self.booking.id, event=self.log_message)
            _add_event(event)
            db.session.commit()
            pause.until(self._wait_datetime)


class _EventWaiter(_Waiter):

    def __init__(self, booking: Booking, log_message: str, scraper: Scraper, url: str,
                 event_date: date, expected_events:list, max_datetime: datetime=None):
        """
        Event Waiter construction
        :param booking: The booking the waiter is related to
        :param log_message: The message related to the waiter
        :param scraper: The scraper to use
        :param url: The WodBuster URL
        :param date: The day associated with the occurrence of the event
        :param expected_events: A list with the expected events
        :param max_datetime: The maximum datetime to wait for
        """
        super().__init__(booking, log_message)
        self._scraper = scraper
        self._url = url
        self._event_date = event_date
        self._expected_events = expected_events
        self._max_datetime = max_datetime

    def wait(self):
        """
        Wait until the event occurs
        """
        event = Event(booking_id=self.booking.id, event=self.log_message)
        _add_event(event)
        db.session.commit()
        self._scraper.wait_until_event(self._url, self._event_date, self._expected_events,
                                       self._max_datetime)


def _add_event(event: Event) -> None:
    """
    Add the evnet to the session only when the last event is different
    :param event: The event to add
    """
    last_event = db.session.query(Event).filter_by(booking_id=event.booking_id).order_by(Event.id.desc()).first()
    if not last_event or last_event.event != event.event:
        db.session.add(event)

def start_booking_loop(booking: Booking) -> None:
    """ 
    Start the booking loop for a given booking 
    :param url: The WodBuster URL
    :param booking: The booking to run
    :param offset: The offset from today to book
    :param availabe_at: The time when the booking is available
    """
    logging.info("Starting thread for booking %s", booking.id)
    booker = Booker(booking, app.app_context())
    __CURRENT_THREADS[booking.id] = booker
    booker.start()

def stop_booking_loop(booking: Booking, log_pause: bool=False) -> None:
    """ 
    Stop the booking loop for a given booking 
    :param booking: The booking to stop
    :param log_pause: If True, a pause event is logged
    """
    logging.info("Stopping thread for booking %s", booking)
    if booking.id in __CURRENT_THREADS:
        booker = __CURRENT_THREADS[booking.id]
        booker.stop(_StopThreadException)
        del __CURRENT_THREADS[booking.id]

        if log_pause:
            event = Event(booking_id=booking.id, event=EventMessage.PAUSED)
            _add_event(event)
            db.session.commit()

def is_booking_running(booking: Booking) -> bool:
    """
    Check if a booking is running
    :param booking: The booking to check
    :return: True if the booking is running, False otherwise
    """
    return booking.id in __CURRENT_THREADS and __CURRENT_THREADS[booking.id].is_alive()
