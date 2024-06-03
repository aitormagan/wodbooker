import logging
import string
from enum import Enum
from abc import abstractmethod, ABC
from queue import Queue
import boto3
from botocore.exceptions import ClientError
from .models import User, Booking
from .constants import DAYS_OF_WEEK

client = boto3.client('ses',region_name="eu-west-1")

_queue = Queue()

_SENDER = "WodBooker <wodbooker@aitormagan.es>"
_CHARSET = "UTF-8"
_HOST = "home.aitormagan.es"
_HTML_TEMPLATE = """<html>
    <head></head>
    <body>
        <h3>WodBooker - {1}:</h3>
        <p>{2}</p>
        <p style="font-size: small">Box: <a href="{3}">{3}</a>
        Puedes consultar todos los eventos asociados a esta reserva <a href="https://{0}/event/?search=%3D{4}">aquí</a>.</p>
        <p style="font-size: small">Mensaje automático generado por <a href="https://{0}">WodBooker</a>.
        Gestiona tus preferencias de notificaciones <a href="https://{0}/user/">aquí</a>.
        </p>
    </body>
</html>"""


class EmailPermissions(Enum):
    FAILURE = "mail_permission_failure"
    SUCCESS = "mail_permission_success"


class Email(ABC):
    """
    Email templates
    """

    def __init__(self, subject: str):
        """
        Creates an instance of an email
        :param subject: The email subject
        """
        self.subject = subject

    @abstractmethod
    def get_html(self) -> str:
        """
        Returns the mail HTML
        """

    @abstractmethod
    def get_plain_body(self) -> str:
        """
        Returns the mail plain body
        """

    def get_subject(self) -> str:
        """
        Returns the mail subject
        """
        return f"[WodBooker] {self.subject}"

    @abstractmethod
    def required_permission(self) -> EmailPermissions:
        """
        Returns the permission required to send the email
        """


class ErrorEmail(Email):
    """
    Error email template
    """
    def __init__(self, booking: Booking, subject: str, error: str):
        """
        Creates an instance of an error email
        :param booking: The booking associated with the email
        :param subject: The email subject
        :param error: The email error message
        """
        super().__init__(subject)
        self.booking_dow = booking.dow
        self.booking_time = booking.time
        self.booking_url = booking.url
        self.booking_id = booking.id
        self.error = error if error[-1] in string.punctuation else error + "."
        self.subject = subject

    def required_permission(self):
        return EmailPermissions.FAILURE

    def get_html(self):
        title = f"Error en la reserva del " \
                f"{DAYS_OF_WEEK[self.booking_dow]} a las " \
                f"{self.booking_time.strftime('%H:%M')}"
        return _HTML_TEMPLATE.format(_HOST, title, self.error, self.booking_url,
                                     self.booking_id)

    def get_plain_body(self):
        return self.error


class SuccessEmail(Email):
    """
    Success email template
    """
    def __init__(self, booking: Booking, subject: str, message: str):
        """
        Creates an instance of a success email
        :param booking: The booking associated with the email
        :param subject: The email subject
        :param message: The email message
        """
        super().__init__(subject)
        self.booking_dow = booking.dow
        self.booking_time = booking.time
        self.booking_url = booking.url
        self.booking_id = booking.id
        self.message = message if message[-1] in string.punctuation else message + "."

    def required_permission(self):
        return EmailPermissions.SUCCESS

    def get_html(self):
        title = f"Reservada con éxito la clase del " \
                f"{DAYS_OF_WEEK[self.booking_dow]} a las " \
                f"{self.booking_time.strftime('%H:%M')}"
        return _HTML_TEMPLATE.format(_HOST, title, self.message, self.booking_url,
                                     self.booking_id)

    def get_plain_body(self):
        return self.message


class SuccessAfterErrorEmail(SuccessEmail):
    """
    Mails sent with a success but when an error event occurred before
    """

    def required_permission(self):
        return EmailPermissions.FAILURE


def send_email(user: User, email: Email):
    """
    Send an email asynchronously
    :param user: The user to send the email to
    :param email: The mail to be sent
    """
    to = user.email
    mail_allowed = getattr(user, email.required_permission().value, False)
    if mail_allowed:
        _queue.put((to, email))
    else:
        logging.info("Email to '%s' not scheduled to be sent because of permissions", to)


def _send_email(to: str, email: Email):
    """
    Send an email using Amazon SES
    :param to: The email to send the email to
    :param email: The mail to be sent
    """

    try:
        client.send_email(
            Destination={
                'ToAddresses': [
                    to,
                ],
            },
            Message={
                'Body': {
                    'Html': {
                        'Charset': _CHARSET,
                        'Data': email.get_html(),
                    },
                    'Text': {
                        'Charset': _CHARSET,
                        'Data': email.get_plain_body(),
                    },
                },
                'Subject': {
                    'Charset': _CHARSET,
                    'Data': email.get_subject(),
                },
            },
            Source=_SENDER,
        )
        logging.info("Email '%s' sent to %s successfully", email.get_subject(), to)
    except ClientError:
        logging.exception("Error sending email")


def process_maling_queue():
    """
    Process the email queue
    """
    while True:
        user, email = _queue.get()
        _send_email(user, email)
        _queue.task_done()
