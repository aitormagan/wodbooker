from datetime import datetime, timedelta, date, time
import logging
import threading
import pause
import pytz
from flask import current_app as app
from requests.exceptions import RequestException

from .scraper import get_scraper
from .exceptions import BookingNotAvailable, InvalidWodBusterResponse, \
    ClassIsFull, LoginError, PasswordRequired, InvalidBox
from .models import db, Booking

_MADRID_TZ = pytz.timezone('Europe/Madrid')

__CURRENT_THREADS = {
}

def _get_next_date_for_weekday(base_date: date, weekday: int) -> date:
    """ 
    Get the next date for a given weekday
    :param base_date: The base date to start the search
    :param weekday: The weekday to search 
    """
    days_ahead = weekday - base_date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return base_date + timedelta(days_ahead)


class Booker:

    def __init__(self, booking: Booking, app_context):
        """
        :param booking: The booking to run
        :param app_context: The Flask app context
        """
        self._booking = None
        self._is_active = True
        self._booking_id = booking.id
        self._session = None
        self._app_context = app_context
    
    def stop(self) -> None:
        """
        Stops the booker. If the booker is waiting for an event, the booker will be stopped
        after the event is triggered. No booking will be performed after this method is called.
        """
        self._is_active = False

    def _execute(self) -> None:
        try:
            self._app_context.push()
            self._booking = db.session.query(Booking).filter_by(id=self._booking_id).first()
            errors = 0
            while errors < 5 and self._is_active:
                try:
                    # Refresh the scraper in case a new one is avaiable
                    scraper = get_scraper(self._booking.user.email, self._booking.user.cookie)
                    current_date = self._booking.last_book_date or date.today()
                    day_to_book = _get_next_date_for_weekday(current_date, self._booking.dow)
                    datetime_to_book = datetime.combine(day_to_book, time(self._booking.time.hour, self._booking.time.minute, 0))

                    book_available_at = _MADRID_TZ.localize(
                        datetime.combine(
                            day_to_book - timedelta(days=self._booking.offset),
                            self._booking.available_at))

                    if book_available_at > datetime.now(_MADRID_TZ):
                        self._set_booking_status(f"Waiting until {book_available_at.strftime('%d/%m/%Y %H:%M')} when booking for {day_to_book.strftime('%d/%m/%Y')} will be available")
                        pause.until(book_available_at)

                    if self._is_active:
                        if scraper.book(self._booking.url, datetime_to_book):
                            logging.info("Booking for user %s at %s completed successfully", self._booking.user.email, datetime_to_book.strftime('%d/%m/%Y %H:%M'))
                            self._set_booking_status("Booking completed successfully")
                            errors = 0
                        else:
                            logging.warning("Impossible to book classes for %s for %s. Class is already booked or user cannot book. Igoning week and attempting booking for next week", 
                                            self._booking.user.email, datetime_to_book)
                            self._set_booking_status("Class cannot be booked for unknown reason. Ignoring week and attempting booking for next week")
                            errors = 0
                    
                    self._booking.last_book_date = day_to_book
                    self._booking.booked_at = datetime.now().replace(microsecond=0)
                    self._booking.user.cookie = scraper.get_cookies()
                    db.session.commit()
                except ClassIsFull:
                    self._set_booking_status(f"Class at {day_to_book.strftime('%d/%m/%Y')} is full. Waiting for available seats")
                    scraper.wait_until_event(self._booking.url, day_to_book, 'changedBooking', datetime_to_book)
                except BookingNotAvailable as e:
                    if e.available_at:
                        self._set_booking_status(f"Waiting until {e.available_at.strftime('%d/%m/%Y %H:%M')} when booking for {day_to_book.strftime('%d/%m/%Y')} will be available")
                        pause.until(e.available_at)
                    else:
                        self._set_booking_status(f"Waiting until classes are loaded for {day_to_book.strftime('%d/%m/%Y')}")
                        scraper.wait_until_event(self._booking.url, day_to_book, 'changedPizarra', datetime_to_book)

                    continue
                except RequestException as e:
                    sleep_for = (errors + 1) * 60
                    logging.warning("Request Exception: %s", e)
                    self._set_booking_status(f"Unexpected network error. Waiting {sleep_for} seconds before retrying...")
                    errors += 1
                    pause.seconds(sleep_for)
                except InvalidWodBusterResponse:
                    sleep_for = (errors + 1) * 60
                    self._set_booking_status(f"Invalid response from WodBuster. Waiting {sleep_for} seconds before retrying...")
                    errors += 1
                    pause.seconds(sleep_for)
                except PasswordRequired:
                    self._is_active = False
                    logging.warning("Credentials for user %s are outdated. Aborting...", self._booking.user.email)
                    self._set_booking_status("Cookies are outdated. Please, login again and update this entry...")
                except LoginError:
                    self._is_active = False
                    logging.warning("User %s cannot be logged in into WodBuster. Aborting...", self._booking.user.email)
                    self._set_booking_status("Login failed: invalid credenctials...")
                except InvalidBox:
                    self._is_active = False
                    logging.warning("User %s accessing to an invalid box detected. Aborting...", self._booking.user.email)
                    self._set_booking_status("The box URL is not valid or you don't have access to it. Please, check the URL and try again...")

            if errors >= 5:
                logging.error("Exiting thread as maximum number of retries has been reached. Review logs for more information")
                self._set_booking_status("Impossible to book. Aborted...")
                self._session.commit()
            elif not self._is_active:
                logging.info("Exiting because thread is marked as inactive")
        except Exception:
            logging.exception("Unexpected error while booking. Aborting...")
    
    def _set_booking_status(self, new_status: str) -> None:
        split_sep = '\n'
        previous_status = self._booking.status.split(split_sep) if self._booking.status else []
        current_date = datetime.now().strftime('%d/%m/%Y %H:%M')
        updated_status = previous_status[-10:] + [f"{current_date}: {new_status}"]
        self._booking.status = split_sep.join(updated_status)
        db.session.commit()

    def run(self) -> None:
        """
        Start the booking loop for a given booking 
        """
        thread = threading.Thread(target=self._execute)
        thread.name = f"Booker {self._booking_id}"
        thread.start()


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
    __CURRENT_THREADS[booking] = booker
    booker.run()

def stop_booking_loop(booking: Booking) -> None:
    """ 
    Stop the booking loop for a given booking 
    :param booking: The booking to stop
    """
    logging.info("Stopping thread for booking %s", booking)
    if booking in __CURRENT_THREADS:
        booker = __CURRENT_THREADS[booking]
        booker.stop()
        del __CURRENT_THREADS[booking]
