"""The mail shape (``observe.notify``): what a findings mail says, that a
quiet check sends nothing, and that settings never show a password."""

from __future__ import annotations

import pytest

from sbh_data_bank.observe.freshness import Finding
from sbh_data_bank.observe.notify import (
    Mail,
    PrintSender,
    SmtpSender,
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


class TestHtml:
    def test_the_findings_mail_carries_an_html_rendering_of_the_same_findings(self):
        findings = [
            Finding("STALE", "t.a", "latest loaded delivery is as of 2026-05-26 (162 days)"),
            Finding(
                "NEW_COLUMNS",
                "t.b",
                "newest loaded file x.csv carries 2 column(s) no spec column claims: "
                "Prim_Group_No, Next_Appointment_DateTime (kept in extra_cells, not typed)",
            ),
        ]
        mail = compose_findings(findings, where="sbh_data_bank as of 2026-11-04")
        assert mail.html is not None
        assert "Stale source" in mail.html and "New vendor columns" in mail.html
        assert mail.html.index("Needs attention") < mail.html.index("For information")
        assert "t.a" in mail.html and "162 days" in mail.html
        # The vendor columns are chips, one each, not one long line.
        assert mail.html.count("Prim_Group_No</span>") == 1
        assert mail.html.count("Next_Appointment_DateTime</span>") == 1
        # The text body is unchanged by the HTML.
        assert mail.body.startswith("sbh_data_bank as of 2026-11-04")
        assert str(findings[0]) in mail.body

    def test_html_escapes_what_a_vendor_might_put_in_a_name(self):
        mail = compose_findings(
            [Finding("OPEN_REFUSAL", "t.a", "<script>alert(1)</script> & co")], where="w"
        )
        assert "<script>" not in mail.html
        assert "&lt;script&gt;" in mail.html and "&amp; co" in mail.html

    def test_a_report_mail_keeps_its_lines_in_a_block(self):
        mail = compose_lines("[SBH ETL] 2 file(s) fetched", ["fetched 2:", "  a.csv"], where="w")
        assert "<pre" in mail.html and "fetched 2:\n  a.csv" in mail.html
        assert mail.body == "w\n\nfetched 2:\n  a.csv\n\nSent by SBH_DATA_BANK."

    def test_the_smtp_message_has_both_parts(self, monkeypatch):
        import smtplib

        captured = {}

        class FakeSmtp:
            def __init__(self, host, port, timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def ehlo(self):
                pass

            def starttls(self):
                pass

            def login(self, u, p):
                pass

            def send_message(self, message):
                captured["message"] = message

        monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
        settings = SmtpSettings("h", 587, "u", "p", "from@x")
        SmtpSender(settings).send(Mail("s", "text", "<p>html</p>"), ["to@x"])
        message = captured["message"]
        assert message.is_multipart()
        parts = [p.get_content_type() for p in message.iter_parts()]
        assert parts == ["text/plain", "text/html"]
