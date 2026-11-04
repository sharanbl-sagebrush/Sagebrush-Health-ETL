"""Telling people: the one mail shape every alert uses, and how it is sent.

Every alert the ETL raises (a stale source, a held file, a failed run, a
spec that changed since its last load, a fetch that brought new files)
becomes one :class:`Mail`: a subject that says what and how many, a
plain-text body that lists the findings one per line, exactly as the
scripts print them, and an HTML rendering of the same findings for mail
clients (one card per finding, alerts before notes, vendor columns as
chips). The text is what the terminal shows and what a client without
HTML falls back to, so what the mail says is what the operator would
have seen; the HTML only lays it out.

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
import re
import smtplib
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from typing import Protocol

from sbh_data_bank.observe.freshness import Finding

__all__ = [
    "LABELS",
    "Mail",
    "PrintSender",
    "Sender",
    "SmtpSender",
    "SmtpSettings",
    "compose_findings",
    "compose_lines",
    "recipients_from_env",
    "render_findings",
    "render_lines",
]


@dataclass(frozen=True, slots=True)
class Mail:
    subject: str
    body: str
    html: str | None = None
    """The same content laid out for a mail client; ``None`` sends text only."""


#: What each finding kind is called when a person reads it.
LABELS = {
    "STALE": "Stale source",
    "NEVER_LOADED": "Never loaded",
    "OPEN_REFUSAL": "Held file",
    "RUN_FAILED": "Run failed",
    "RUN_UNFINISHED": "Run left open",
    "SPEC_CHANGED": "Spec changed",
    "NEW_COLUMNS": "New vendor columns",
    "OPEN_QUARANTINE": "Open quarantine",
}


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
        if mail.html:
            message.add_alternative(mail.html, subtype="html")
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
    lines = list(lines)
    body = "\n".join([where, "", *lines, "", "Sent by SBH_DATA_BANK."])
    return Mail(subject, body, render_lines(subject, lines, where=where))


# ------------------------------------------------------------------ HTML

_FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "font-family:Consolas,'SFMono-Regular',Menlo,monospace"
_ALERT = ("#b42318", "#fef3f2")
_NOTE = ("#b54708", "#fffaeb")
_COLUMNS = re.compile(
    r"^(?P<lead>.*? carries \d+ column\(s\) no spec column claims: )"
    r"(?P<columns>.*?)(?P<tail> \(kept in extra_cells, not typed\))$"
)


def _page(title: str, where: str, inner: str) -> str:
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:24px;background:#f4f4f2;'
        f'{_FONT};color:#1f2a2e;font-size:15px;line-height:1.5">'
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="max-width:720px;margin:0 auto"><tr><td>'
        f'<div style="font-size:20px;font-weight:600;margin:0 0 4px">{escape(title)}</div>'
        f'<div style="color:#5d6b70;font-size:13px;margin:0 0 20px">{escape(where)}</div>'
        f"{inner}"
        '<div style="color:#8a9499;font-size:12px;margin-top:24px">Sent by SBH_DATA_BANK.</div>'
        "</td></tr></table></body></html>"
    )


def _chips(columns: str) -> str:
    return " ".join(
        f'<span style="display:inline-block;{_MONO};font-size:12px;background:#eef1f0;'
        f'border-radius:4px;padding:1px 6px;margin:2px 2px 0 0">{escape(c.strip())}</span>'
        for c in columns.split(",")
        if c.strip()
    )


def _message(text: str) -> str:
    m = _COLUMNS.match(text)
    if m is None:
        return escape(text)
    return f'{escape(m["lead"])}<br>{_chips(m["columns"])}<br><span style="color:#5d6b70">{escape(m["tail"].strip())}</span>'  # noqa: E501


def _card(finding: Finding, colours: tuple[str, str]) -> str:
    fg, bg = colours
    label = LABELS.get(finding.kind, finding.kind)
    source = finding.source_key or ""
    return (
        f'<div style="background:#fff;border-left:4px solid {fg};border-radius:0 8px 8px 0;'
        'padding:12px 16px;margin:0 0 10px">'
        f'<span style="display:inline-block;background:{bg};color:{fg};font-size:11px;'
        "font-weight:600;letter-spacing:.04em;text-transform:uppercase;border-radius:4px;"
        f'padding:2px 8px;margin-right:8px">{escape(label)}</span>'
        f'<span style="{_MONO};font-size:13px">{escape(source)}</span>'
        f'<div style="margin-top:6px">{_message(finding.message)}</div></div>'
    )


def _section(heading: str, cards: str) -> str:
    return (
        f'<div style="font-size:12px;font-weight:600;color:#5d6b70;letter-spacing:.06em;'
        f'text-transform:uppercase;margin:16px 0 8px">{escape(heading)}</div>{cards}'
    )


def render_findings(title: str, alerts: list[Finding], notes: list[Finding], *, where: str) -> str:
    """The freshness mail as HTML: one card per finding, alerts first."""
    inner = ""
    if alerts:
        inner += _section("Needs attention", "".join(_card(f, _ALERT) for f in alerts))
    if notes:
        inner += _section("For information", "".join(_card(f, _NOTE) for f in notes))
    return _page(title, where, inner)


def render_lines(title: str, lines: list[str], *, where: str) -> str:
    """A report mail as HTML: the lines as delivered, in a monospace block
    that wraps, so a table printed by a script keeps its columns."""
    block = (
        f'<pre style="{_MONO};font-size:13px;line-height:1.45;white-space:pre-wrap;'
        'word-break:break-word;background:#fff;border-radius:8px;padding:14px 16px;margin:0">'
        f"{escape(chr(10).join(lines))}</pre>"
    )
    return _page(title, where, block)


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
    mail = compose_lines(subject, lines, where=where)
    title = subject.removeprefix("[SBH ETL] ")
    return Mail(mail.subject, mail.body, render_findings(title, alerts, notes, where=where))
