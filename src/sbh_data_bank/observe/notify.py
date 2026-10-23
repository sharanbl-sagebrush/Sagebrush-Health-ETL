"""Telling people: the one mail shape every alert uses, and how it is sent.

Every alert the ETL raises (a stale source, a held file, a failed run, a
spec that changed since its last load, a fetch that brought new files)
becomes one :class:`Mail`: a subject that says what and how many, and a
plain-text body that lists the findings one per line, exactly as the
scripts print them. The same text goes to the terminal and to the
recipients, so what the mail says is what the operator would have seen.

Sending is one of two :class:`Sender` implementations. ``SmtpSender``
speaks SMTP with STARTTLS from ``SBH_SMTP_*`` (Microsoft 365 is
``smtp.office365.com`` port 587 with a mailbox login); ``PrintSender``
prints the mail instead, for ``--dry-run`` and for tests. Recipients come
from ``SBH_ALERT_TO``, comma-separated. A send failure is reported and
never fails the run that raised the alert: the batch record is the
record, the mail is the courtesy.

PHI: a mail carries source keys, filenames, dates, counts and outcome
text. Never a cell value, and never a password.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from sbh_data_bank.observe.freshness import Finding

__all__ = [
    "Mail",
    "PrintSender",
    "Sender",
    "SmtpSender",
    "SmtpSettings",
    "compose_findings",
    "compose_lines",
    "recipients_from_env",
]


@dataclass(frozen=True, slots=True)
class Mail:
    subject: str
    body: str


class Sender(Protocol):
    def send(self, mail: Mail, to: list[str]) -> None: ...


@dataclass(frozen=True)
class SmtpSettings:
    """``SBH_SMTP_HOST``, ``SBH_SMTP_PORT`` (587), ``SBH_SMTP_USER``,
    ``SBH_SMTP_PASSWORD`` and ``SBH_MAIL_FROM`` (defaults to the user).
    The password is excluded from ``repr``."""

    host: str
    port: int
    user: str | None
    password: str | None
    sender: str

    def __repr__(self) -> str:
        return f"SmtpSettings(host={self.host!r}, port={self.port}, sender={self.sender!r})"

    @classmethod
    def from_env(cls) -> SmtpSettings:
        host = os.environ.get("SBH_SMTP_HOST")
        if not host:
            raise ValueError("missing environment setting: SBH_SMTP_HOST")
        user = os.environ.get("SBH_SMTP_USER") or None
        sender = os.environ.get("SBH_MAIL_FROM") or user
        if not sender:
            raise ValueError("missing environment setting: SBH_MAIL_FROM (or SBH_SMTP_USER)")
        return cls(
            host=host,
            port=int(os.environ.get("SBH_SMTP_PORT", "587")),
            user=user,
            password=os.environ.get("SBH_SMTP_PASSWORD") or None,
            sender=sender,
        )


def recipients_from_env() -> list[str]:
    """``SBH_ALERT_TO``: comma-separated addresses; empty when unset."""
    raw = os.environ.get("SBH_ALERT_TO", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


class SmtpSender:
    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    def send(self, mail: Mail, to: list[str]) -> None:
        s = self._settings
        message = EmailMessage()
        message["Subject"] = mail.subject
        message["From"] = s.sender
        message["To"] = ", ".join(to)
        message.set_content(mail.body)
        with smtplib.SMTP(s.host, s.port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if s.user and s.password:
                smtp.login(s.user, s.password)
            smtp.send_message(message)


class PrintSender:
    """Prints what would be sent; keeps every mail for a test to read."""

    def __init__(self) -> None:
        self.sent: list[tuple[Mail, list[str]]] = []

    def send(self, mail: Mail, to: list[str]) -> None:
        self.sent.append((mail, list(to)))
        print(
            f"\n--- mail to {', '.join(to) or '(nobody)'}\nSubject: {mail.subject}\n\n{mail.body}"
        )


def compose_lines(subject: str, lines: Iterable[str], *, where: str) -> Mail:
    """A mail whose body is ``where`` (which database) and the lines."""
    body = "\n".join([where, "", *lines, "", "Sent by SBH_DATA_BANK."])
    return Mail(subject, body)


def compose_findings(findings: list[Finding], *, where: str) -> Mail | None:
    """The freshness mail: alerts first, notes after; ``None`` when there is
    nothing to say, so a quiet check sends nothing."""
    if not findings:
        return None
    alerts = [f for f in findings if f.is_alert]
    notes = [f for f in findings if not f.is_alert]
    kinds = sorted({f.kind for f in alerts})
    if alerts:
        subject = f"[SBH ETL] {len(alerts)} alert{'s' if len(alerts) != 1 else ''}: " + ", ".join(
            kinds
        )
    else:
        subject = f"[SBH ETL] {len(notes)} note{'s' if len(notes) != 1 else ''}"
    lines = [str(f) for f in alerts]
    if notes:
        lines += ["", "Notes:", *(str(f) for f in notes)]
    return compose_lines(subject, lines, where=where)
