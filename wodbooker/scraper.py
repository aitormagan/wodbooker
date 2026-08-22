import datetime
import re
import pickle
import logging
import json
import requests
import sseclient
import pytz
from bs4 import BeautifulSoup
from .exceptions import LoginError, InvalidWodBusterResponse, \
    BookingNotAvailable, ClassIsFull, PasswordRequired, InvalidBox, \
    ClassNotFound, BookingFailed

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
}

_UTC_TZ = pytz.timezone('UTC')
_MADRID_TZ = pytz.timezone('Europe/Madrid')
_WODBUSTER_NOT_ACCEPTING_REQUESTS_MESSAGE = "WodBuster is not accepting more requests at this time. Try again in a minute"
_MORE_THAN_ONE_BOX_MESSAGE = "User can access more than to boxes"


class Scraper():
    """
    WodBuster scraper
    """

    def __init__(self, user: str, password: str=None, cookie: bytes=None):
        self._user = user
        self._password = password
        self.logged = False
        self._session = requests.Session()
        self._cookie = cookie
        self._box_name_by_url = {}
        self._sse_server_by_url = {}

    def get_cookies(self) -> bytes:
        """
        Returns the cookies for the current session
        """
        return pickle.dumps(self._session.cookies)

    def login(self) -> None:
        """
        Attempt to login the user into WodBuster
        :raises LoginError: If user/password combination fails
        :raises PasswordRequired: If the provided cookie is outdated and a password is not provided
        :raises InvalidWodBusterAPIResponse: If the response from WodBuster is not valid (CloudFare
        protection, etc.)
        :raises RequestException: If a network error occurs or an HTTP error code is received
        """
        if self.logged:
            return

        if self._cookie:
            self._session.cookies.update(pickle.loads(self._cookie))
            road_to_box_request = self._session.get("https://wodbuster.com/account/roadtobox.aspx",
                                                    headers=_HEADERS, allow_redirects=False, timeout=10)

            if "Location" in road_to_box_request.headers and "login" in road_to_box_request.headers["Location"]:
                logging.warning("Cookie for user %s is outdated. Attempting logging with password...", self._user)
                self._login_with_username_and_password()
            else:
                logging.info("User %s logged successfully with cookie", self._user)
                self.logged = True
        else:
            self._login_with_username_and_password()

    def _login_with_username_and_password(self):

        if not self._password:
            raise PasswordRequired("Password is required")

        self._session = requests.Session()
        login_url = "https://wodbuster.com/account/login.aspx"
        initial_request = self._session.get(login_url, headers=_HEADERS, timeout=10)

        try:
            soup = BeautifulSoup(initial_request.content, 'lxml')
            viewstatec = soup.find(id='__VIEWSTATEC')['value']
            eventvalidation = soup.find(id='__EVENTVALIDATION')['value']
            csrftoken = soup.find(id='CSRFToken')['value']
        except TypeError as e:
            logging.exception("WodBuster response cannot be parsed")
            raise InvalidWodBusterResponse(_WODBUSTER_NOT_ACCEPTING_REQUESTS_MESSAGE) from e

        data_login = {
            'ctl00$ctl00$body$ctl00': 'ctl00$ctl00$body$ctl00|ctl00$ctl00$body$body$CtlLogin$CtlAceptar',
            'ctl00$ctl00$body$body$CtlLogin$IoTri': '',
            'ctl00$ctl00$body$body$CtlLogin$IoTrg': '',
            'ctl00$ctl00$body$body$CtlLogin$IoTra': '',
            'ctl00$ctl00$body$body$CtlLogin$IoEmail': self._user,
            'ctl00$ctl00$body$body$CtlLogin$IoPassword': self._password,
            'ctl00$ctl00$body$body$CtlLogin$cIoUid': '',
            'ctl00$ctl00$body$body$CtlLogin$CtlAceptar': 'Aceptar\n'
        }

        login_request = self._login_request(login_url, viewstatec, eventvalidation, csrftoken, data_login)

        if login_request.status_code != 200:
            raise InvalidWodBusterResponse(_WODBUSTER_NOT_ACCEPTING_REQUESTS_MESSAGE)

        if 'class="Warning"' in login_request.text:
            raise LoginError('Invalid credentials')

        viewstatec_confirm = self._lookup_header_value(login_request.text, '__VIEWSTATEC')
        eventvalidation_confirm = self._lookup_header_value(login_request.text, '__EVENTVALIDATION')

        data_confirm = {
            'ctl00$ctl00$body$ctl00': 'ctl00$ctl00$body$ctl00|ctl00$ctl00$body$body$CtlConfiar$CtlSeguro',
            'ctl00$ctl00$body$body$CtlConfiar$dispositivo': 'CtlSeguro'
        }

        confirm_login_request = self._login_request(login_url, viewstatec_confirm,
                                                    eventvalidation_confirm, csrftoken,
                                                    data_confirm)

        if confirm_login_request.status_code != 200:
            raise InvalidWodBusterResponse(_WODBUSTER_NOT_ACCEPTING_REQUESTS_MESSAGE)

        logging.info("User %s logged successfully with credentials", self._user)
        self.logged = True
        self._password = None

    def _login_request(self, url, viewstatec, eventvalidation, csrftoken, extra_fields):
        data = {
            'CSRFToken': csrftoken,
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATEC': viewstatec,
            '__VIEWSTATE': '',
            '__EVENTVALIDATION': eventvalidation,
            '__ASYNCPOST': 'true',
        }

        data = {**data, **extra_fields}

        request = self._session.post(url, data=data, headers=_HEADERS, timeout=10)
        request.raise_for_status()
        return request

    @staticmethod
    def _lookup_header_value(text, header_name):
        index = text.index(header_name)
        return text[index + len(header_name) + 1:].split("|")[0]

    def book(self, url: str, booking_datetime: datetime) -> bool:
        """ 
        Book a class at the given box for the given date. True is returned if the booking was successful
        :param url: The WodBuster URL associated to the box where the class has to be booked
        :param booking_datetime: The date and time when the class has to be booked
        :return: True if the action was successful otherwise False
        :raises BookingNotAvailable: If the class is not available for booking
        :raises ClassIsFull: If the class is full
        :raises LoginError: If user/password combination fails.
        :raises InvalidWodBusterResponse: If the response from WodBuster is not valid (CloudFare
        protection, etc.)
        :raises PasswordRequired: If the provided cookie is outdated and a password is not provided
        :raises RequestException: If a network error occurs or an HTTP error code is received
        :raises ClassNotFound: If there is no class at the given date and time
        :raises BookingFailed: If the booking request fails
        """
        self.login()

        classes, epoch = self.get_classes(url, booking_datetime.date())
        hour = booking_datetime.strftime('%H:%M:%S')

        if not classes['Data']:
            avaiable_at = None
            if "PrimeraHoraPublicacion" in classes:
                avaiable_at = _MADRID_TZ.localize(datetime.datetime.strptime(classes["PrimeraHoraPublicacion"],
                                                                             '%m/%d/%Y %H:%M:%S'))
            raise BookingNotAvailable('No classes available', avaiable_at)

        for _class in classes['Data']:
            if _class['Hora'] == hour:
                class_status = _class['Valores'][0]['TipoEstado']

                if class_status == "Borrable":
                    return True

                class_details = _class['Valores'][0]['Valor']
                _id = class_details['Id']
                if len(class_details['AtletasEntrenando']) >= class_details['Plazas']:
                    raise ClassIsFull("Class is full")

                api_path = "Calendario_Mover.ashx" if class_status == "Cambiable" else "Calendario_Inscribir.ashx"
                logging.info("Using API path %s to join user to class", api_path)
                book_result = self._book_request(f'{url}/athlete/handlers/{api_path}?id={_id}&ticks={epoch}')
                if book_result['Res']['EsCorrecto']:
                    return True
                else:
                    raise BookingFailed(book_result.get("Res", {}).get("ErrorMsg"))

        raise ClassNotFound(f"Class for {hour} not found on {booking_datetime.date().strftime('%d/%m/%Y')}")

    def get_classes(self, url: str, date: datetime.date) -> tuple:
        """ 
        Get the classes for a given epoch
        :param url: The WodBuster URL associated to the box where classes has to be obtained
        :param date: The day for which the classes have to be obtained
        :return: A tuple. The first element is the response from WodBuster API for the specified date. 
        The second element is the date in epoch format in case is useful for other operations
        :raises BookingNotAvailable: If the class is not available for booking
        :raises ClassIsFull: If the class is full
        :raises LoginError: If user/password combination fails.
        :raises InvalidWodBusterResponse: If the response from WodBuster is not valid (CloudFare
        protection, etc.)
        :raises PasswordRequired: If the provided cookie is outdated and a password is not provided
        :raises RequestException: If a network error occurs or an HTTP error code is received
        """
        midnight = _UTC_TZ.localize(datetime.datetime.combine(date, datetime.datetime.min.time()))
        epoch = int(midnight.timestamp())
        return self._book_request(f'{url}/athlete/handlers/LoadClass.ashx?ticks={epoch}'), epoch

    def _book_request(self, url):
        try:
            request = self._session.get(url, headers=_HEADERS, allow_redirects=False, timeout=10)
            if request.status_code == 302 and "login" in request.headers["Location"]:
                raise InvalidBox("Provided URL is not accesible for the given user")
            if request.status_code != 200:
                raise InvalidWodBusterResponse('Invalid response status from WodBuster')

            return request.json()
        except requests.exceptions.JSONDecodeError as e:
            raise InvalidWodBusterResponse('WodBuster returned a non JSON response') from e
        except requests.exceptions.RequestException as e:
            raise InvalidWodBusterResponse('WodBuster returned a non expected response') from e

    def wait_until_event(self, url: str, date: datetime.date, expected_events:list,
                         max_datetime: datetime=None) -> bool:
        """ 
        Wait until a specific event is received for a given day
        :param url: The WodBuster URL associated to the box where the event will be received
        :param date: The day associated with the occurrence of the event
        :param expected_events: The list of event to wait for
        :param max_datetime: The maximum date when the event is expected. By default, events will 
        be waited until 23:59:59 of the provided date
        :return: True if the event is found. False otherwise.
        :raises LoginError: If user/password combination fails.
        :raises InvalidWodBusterResponse: If the response from WodBuster is not valid (CloudFare
        protection, etc.)
        :raises PasswordRequired: If the provided cookie is outdated and a password is not provided
        :raises RequestException: If a network error occurs or an HTTP error code is received
        :raises InvalidBox: If box name cannot be determined from the provided URL
        """
        self.login()
        max_datetime = max_datetime or _MADRID_TZ.localize(datetime.datetime.combine(date, datetime.datetime.max.time()))

        if url not in self._box_name_by_url:
            homepage_request = self._session.get(f"{url}/user/", headers=_HEADERS,
                                                 allow_redirects=False, timeout=10)
            look_up = re.search(r"InitAjax\('([^']*)',\s?'([^']*)'", homepage_request.text)
            if not look_up:
                raise InvalidBox("Couldn't determine box name from URL")
            self._box_name_by_url[url] = look_up.group(1)
            self._sse_server_by_url[url] = look_up.group(2)

        box_name = self._box_name_by_url[url]
        sse_server = self._sse_server_by_url[url]
        event_found = False
        timeout = False

        while not event_found and not timeout:
            negotiate_request = self._session.post(f"{sse_server}/bookinghub/negotiate?negotiateVersion=1",
                                        headers=_HEADERS, timeout=10)
            connection_token = negotiate_request.json()["connectionToken"]
            headers = {**_HEADERS, **{"Accept": "text/event-stream"}}
            booking_hub_request = self._session.get(f"{sse_server}/bookinghub?id={connection_token}",
                                                    stream=True, headers=headers, timeout=60)

            self._send_sse_command(sse_server, connection_token, {"protocol":"json","version":1})
            midnight = _UTC_TZ.localize(datetime.datetime.combine(date, datetime.datetime.min.time()))
            epoch = int(midnight.timestamp())
            self._send_sse_command(sse_server, connection_token, {"arguments": [box_name, str(epoch)],
                                                                  "invocationId":"0",
                                                                  "target":"JoinRoom",
                                                                  "type":1})

            client = sseclient.SSEClient(booking_hub_request)
            client_iterator = client.events()

            connection_active = True
            while connection_active and not event_found and not timeout:
                if max_datetime and datetime.datetime.now(_MADRID_TZ) > max_datetime:
                    timeout = True
                else:
                    try:
                        event = next(client_iterator)
                        data = json.loads(event.data[:-1])
                        event_found = "target" in data and data["target"] in expected_events
                    except StopIteration:
                        logging.warning("Iterator without events. Reseting connection...")
                        connection_active = False
                    except requests.exceptions.ConnectionError:
                        connection_active = False
                        logging.warning("No event received after 60 seconds. Reseting connection")

            client.close()

        return event_found

    def _send_sse_command(self, sse_server, connection_token, command):
        headers = {**_HEADERS, **{"Content-Type": "text/plain"}}
        command_str = json.dumps(command) + "\u001e"
        self._session.post(f"{sse_server}/bookinghub?id={connection_token}",
                           data=command_str, headers=headers, timeout=10)

    def get_box_url(self) -> str:
        """
        Get the WodBuster URL associated with the user.
        Box URL is only returned when the user has just one box associated.
        :raises LoginError: If user/password combination fails.
        :raises InvalidWodBusterResponse: If the user has more than one box associated
        :return: The WodBuster URL associated with the user
        """
        self.login()
        road_to_box_request = self._session.get("https://wodbuster.com/account/roadtobox.aspx",
                                                headers=_HEADERS, allow_redirects=False, timeout=10)
        if "Location" in road_to_box_request.headers:
            if "login" in road_to_box_request.headers["Location"]:
                raise LoginError("Invalid credentials")
            else:
                full_url = road_to_box_request.headers["Location"]
                return full_url[:full_url.index("/user")]
        else:
            raise InvalidWodBusterResponse(_MORE_THAN_ONE_BOX_MESSAGE)


__SCRAPERS = {}


def get_scraper(email: str, cookie: bytes) -> Scraper:
    """
    Returns a scrapper for a given user. If a scraper for the given user already exists, the
    existing one will be returned. Otherwise, a new one will be created.
    :param: The user to get the scraper for
    :param: The cookie associated with the user
    """
    if email not in __SCRAPERS:
        __SCRAPERS[email] = Scraper(email, cookie=cookie)

    return __SCRAPERS[email]


def refresh_scraper(email: str, password: str) -> Scraper:
    """
    Force the creation of a new scraper for the given user with the provided password.
    If the provided credentials are invalid, a LoginError will be risen and the old scraper 
    will be kept
    :param email: The email of the user
    :param password: The password of the user
    :raises LoginError: If the provided credentials are invalid
    :raises InvalidWodBusterResponse: If the response from WodBuster is not valid (CloudFare
    protection, etc.)
    :raises RequestException: If a network error occurs or an HTTP error code is received
    """
    scraper = Scraper(email, password)
    scraper.login()
    __SCRAPERS[email] = scraper
    return scraper
