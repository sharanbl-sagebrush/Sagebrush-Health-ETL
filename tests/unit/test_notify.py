"""The mail shape (``observe.notify``): what a findings mail says, that a
quiet check sends nothing, and that settings never show a password."""

from __future__ import annotations

import pytest

from sbh_data_bank.observe.freshness import Finding
from sbh_data_bank.observe.notify import (
    PrintSender,
    SmtpSettings,
    compose_findings,
    compose_lines,
    recipients_from_env,
)


def test_a_quiet_check_has_no_mail():
    assert compose_findings([], where="db") is None


def test_alerts_lead_and_notes_follow():
    findings = [
        Finding("OPEN_QUARANTINE", "t.a", "3 quarantined rows unresolved"),
        Finding("STALE", "t.b", "late"),
        Finding("RUN_FAILED", None, "boom"),
    ]
    mail = compose_findings(findings, where="sbh_data_bank as of 2026-11-03")
    assert mail.subject == "[SBH ETL] 2 alerts: RUN_FAILED, STALE"
    body = mail.body.splitlines()
    assert body[0] == "sbh_data_bank as of 2026-11-03"
    assert body[2].startswith("STALE") and body[3].startswith("RUN_FAILED")
    assert "Notes:" in body and body[-1] == "Sent by SBH_DATA_BANK."
    assert any(line.startswith("OPEN_QUARANTINE") for line in body)


def test_notes_alone_are_a_note_mail():
    mail = compose_findings([Finding("OPEN_QUARANTINE", "t.a", "x")], where="db")
    assert mail.subject == "[SBH ETL] 1 note"


def test_compose_lines_and_the_print_sender():
    sender = PrintSender()
    sender.send(compose_lines("s", ["a", "b"], where="w"), ["x@y"])
    ((mail, to),) = sender.sent
    assert (mail.subject, to) == ("s", ["x@y"])
    assert mail.body == "w\n\na\nb\n\nSent by SBH_DATA_BANK."


def test_smtp_settings_from_env_hide_the_password(monkeypatch):
    monkeypatch.setenv("SBH_SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SBH_SMTP_USER", "etl@example.org")
    monkeypatch.setenv("SBH_SMTP_PASSWORD", "hunter2")
    monkeypatch.setenv("SBH_ALERT_TO", "a@example.org, b@example.org,")
    s = SmtpSettings.from_env()
    assert (s.host, s.port, s.sender) == ("smtp.office365.com", 587, "etl@example.org")
    assert "hunter2" not in repr(s)
    assert recipients_from_env() == ["a@example.org", "b@example.org"]
    monkeypatch.delenv("SBH_SMTP_HOST")
    with pytest.raises(ValueError, match="SBH_SMTP_HOST"):
        SmtpSettings.from_env()
