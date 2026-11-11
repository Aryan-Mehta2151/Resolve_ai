import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.core.config import settings


def email_configuration_errors() -> list[str]:
    errors = []
    if not settings.customer_email_smtp_host:
        errors.append("CUSTOMER_EMAIL_SMTP_HOST is missing")
    if not settings.customer_email_from_address.strip():
        errors.append("CUSTOMER_EMAIL_FROM_ADDRESS is missing")
    if bool(settings.customer_email_smtp_username) != (
        settings.customer_email_smtp_password is not None
    ):
        errors.append("SMTP username and password must be configured together")
    parsed_url = urlsplit(settings.customer_email_verification_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        errors.append("CUSTOMER_EMAIL_VERIFICATION_URL must be an absolute URL without query")
    return errors


def _verification_link(token: str) -> str:
    errors = email_configuration_errors()
    if errors:
        raise RuntimeError("; ".join(errors))
    parsed_url = urlsplit(settings.customer_email_verification_url)
    return urlunsplit(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            urlencode({"token": token}),
            "",
        )
    )


def _send_email(recipient: str, organization_name: str, token: str) -> None:
    host = settings.customer_email_smtp_host
    if not host:
        raise RuntimeError("Customer email is not configured")
    message = EmailMessage()
    message["Subject"] = f"Verify your ResolveAI account for {organization_name}"
    message["From"] = settings.customer_email_from_address
    message["To"] = recipient
    message.set_content(
        f"Hello,\n\n"
        f"Verify your ResolveAI customer account for {organization_name} by opening this link:\n"
        f"{_verification_link(token)}\n\n"
        f"This link expires in {settings.customer_email_verification_minutes} minutes. "
        "If you did not request this, you can ignore this message."
    )
    with smtplib.SMTP(host, settings.customer_email_smtp_port, timeout=10) as server:
        if settings.customer_email_smtp_starttls:
            server.starttls(context=ssl.create_default_context())
        if settings.customer_email_smtp_username:
            password = settings.customer_email_smtp_password
            if password is None:
                raise RuntimeError("SMTP password is not configured")
            server.login(settings.customer_email_smtp_username, password.get_secret_value())
        server.send_message(message)


def _send_voice_verification_code(recipient: str, organization_name: str, code: str) -> None:
    host = settings.customer_email_smtp_host
    if not host:
        raise RuntimeError("Customer email is not configured")
    message = EmailMessage()
    message["Subject"] = f"ResolveAI phone-call verification for {organization_name}"
    message["From"] = settings.customer_email_from_address
    message["To"] = recipient
    message.set_content(
        f"Your ResolveAI phone-call verification code is {code}.\n\n"
        f"It expires in {settings.voice_verification_minutes} minutes. "
        "ResolveAI support will never ask for your password or payment details."
    )
    with smtplib.SMTP(host, settings.customer_email_smtp_port, timeout=10) as server:
        if settings.customer_email_smtp_starttls:
            server.starttls(context=ssl.create_default_context())
        if settings.customer_email_smtp_username:
            password = settings.customer_email_smtp_password
            if password is None:
                raise RuntimeError("SMTP password is not configured")
            server.login(settings.customer_email_smtp_username, password.get_secret_value())
        server.send_message(message)


async def send_verification_email(recipient: str, organization_name: str, token: str) -> None:
    await asyncio.to_thread(_send_email, recipient, organization_name, token)


async def send_voice_verification_code(
    recipient: str,
    organization_name: str,
    code: str,
) -> None:
    await asyncio.to_thread(_send_voice_verification_code, recipient, organization_name, code)
