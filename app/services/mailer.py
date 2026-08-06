"""Minimal SMTP sender built on the standard library.

No mail extension is installed and none is needed: a password reset is a single
plain-text message. Configure via environment:

    SMTP_HOST, SMTP_PORT (default 587), SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_USE_TLS (default true), MAIL_FROM (default SMTP_USERNAME)

When SMTP_HOST is unset the message is logged instead of sent, so local and
CI runs work without a mail server.
"""

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default

    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def send_email(to_address, subject, body):
    """Send a plain-text email. Returns True if it was handed to an SMTP server.

    Never raises: callers are user-facing endpoints whose response must not
    depend on mail delivery (and must not leak whether delivery happened).
    """
    host = os.environ.get('SMTP_HOST')
    if not host:
        logger.warning(
            'SMTP_HOST not set; not sending mail. Would have sent to %s: %s\n%s',
            to_address, subject, body,
        )

        return False

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = os.environ.get('MAIL_FROM') or os.environ.get('SMTP_USERNAME', 'no-reply@ossprey.org')
    message['To'] = to_address
    message.set_content(body)

    port = int(os.environ.get('SMTP_PORT', 587))
    username = os.environ.get('SMTP_USERNAME')
    password = os.environ.get('SMTP_PASSWORD')

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if _env_flag('SMTP_USE_TLS', True):
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)

        return True
    except Exception as exc:
        logger.error('Failed to send mail to %s: %s', to_address, exc)

        return False
