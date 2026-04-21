"""Google Calendar invites for EON Next dispatch windows.

Sends iCalendar (.ics) events via Gmail SMTP. Gmail automatically
recognises .ics attachments and adds the event to the recipient's
Google Calendar — no OAuth or Google API credentials required.

Requires a Gmail App Password (not your account password):
  Google account → Security → 2-Step Verification → App passwords

Configuration (config.yaml):
  gcal:
    smtp_user: "sender@gmail.com"
    smtp_pass: "xxxx xxxx xxxx xxxx"   # 16-char app password
    invite_to:  "recipient@gmail.com"  # calendar to receive events
    cheap_rate_pence: 6.19             # p/kWh during dispatch
    standard_rate_pence: 26.073        # p/kWh standard rate
"""

import smtplib
import uuid
from datetime import datetime, timedelta, time as dtime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from zoneinfo import ZoneInfo
from typing import Optional

UK_TZ = ZoneInfo("Europe/London")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def _fmt_uk(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(UK_TZ).strftime("%Y%m%dT%H%M%S")


def _cap_end_at_midnight(start_utc: str, end_utc: str) -> str:
    """Cap end time at midnight UK on the dispatch start day.

    EON Next cheap rate applies from 00:00 UK onwards so events
    extending past midnight are misleading.
    """
    start_uk = datetime.fromisoformat(start_utc).astimezone(UK_TZ)
    midnight_uk = datetime.combine(
        start_uk.date() + timedelta(days=1), dtime(0, 0), tzinfo=UK_TZ
    )
    end_dt = datetime.fromisoformat(end_utc).astimezone(UK_TZ)
    return midnight_uk.isoformat() if end_dt > midnight_uk else end_utc


def _send(smtp_user: str, smtp_pass: str, msg) -> None:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def _request_ics(start_utc: str, end_utc: str, summary: str, description: str,
                 uid: str, organizer: str, attendee: str, sequence: int = 0) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//SmartChargeTesla//EON Dispatch//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SEQUENCE:{sequence}\r\n"
        f"DTSTART;TZID=Europe/London:{_fmt_uk(start_utc)}\r\n"
        f"DTEND;TZID=Europe/London:{_fmt_uk(end_utc)}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DESCRIPTION:{description.replace(chr(10), chr(92) + 'n')}\r\n"
        f"ORGANIZER;CN=SmartChargeTesla:mailto:{organizer}\r\n"
        f"ATTENDEE;RSVP=TRUE:mailto:{attendee}\r\n"
        "STATUS:CONFIRMED\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT10M\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Reminder\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _cancel_ics(start_utc: str, end_utc: str, summary: str,
                uid: str, organizer: str, attendee: str, sequence: int) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//SmartChargeTesla//EON Dispatch//EN\r\n"
        "METHOD:CANCEL\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SEQUENCE:{sequence}\r\n"
        f"DTSTART;TZID=Europe/London:{_fmt_uk(start_utc)}\r\n"
        f"DTEND;TZID=Europe/London:{_fmt_uk(end_utc)}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"ORGANIZER;CN=SmartChargeTesla:mailto:{organizer}\r\n"
        f"ATTENDEE;RSVP=TRUE:mailto:{attendee}\r\n"
        "STATUS:CANCELLED\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _build_email(smtp_user: str, invite_to: str, subject: str,
                 body: str, ics_data: str, method: str) -> MIMEMultipart:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = invite_to
    msg.attach(MIMEText(body, "plain"))
    ics_part = MIMEBase("text", "calendar", method=method, name="invite.ics")
    ics_part.set_payload(ics_data.encode("utf-8"))
    encoders.encode_base64(ics_part)
    ics_part.add_header("Content-Disposition", 'attachment; filename="invite.ics"')
    msg.attach(ics_part)
    return msg


def _dispatch_summary(start_utc: str, end_utc: str,
                      delta_kwh: Optional[float], suffix: str = "") -> str:
    start_dt = datetime.fromisoformat(start_utc).astimezone(UK_TZ)
    end_dt = datetime.fromisoformat(end_utc).astimezone(UK_TZ)
    kwh_str = f" (~{abs(delta_kwh):.1f} kWh)" if delta_kwh else ""
    return f"⚡ EON EV Dispatch {start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M %Z')}{kwh_str}{suffix}"


class GCalAPI:
    """Google Calendar invite sender via Gmail SMTP + iCalendar."""

    def __init__(self, smtp_user: str, smtp_pass: str, invite_to: str,
                 cheap_rate_pence: float = 6.19, standard_rate_pence: float = 26.073):
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.invite_to = invite_to
        self.cheap_rate = cheap_rate_pence
        self.standard_rate = standard_rate_pence

    def _description(self, delta_kwh: Optional[float], location: Optional[str]) -> str:
        saving = round(self.standard_rate - self.cheap_rate, 3)
        return (
            "EON Next Drive Smart off-schedule cheap-rate EV charging window.\n"
            f"Rate: {self.cheap_rate}p/kWh  (vs standard {self.standard_rate}p/kWh)\n"
            f"Saving vs standard: ~{saving}p/kWh\n"
            + (f"Estimated charge: {abs(delta_kwh):.2f} kWh\n" if delta_kwh else "")
            + f"Location: {location or 'AT_HOME'}\n\n"
            "Ensure car is plugged in and zappi is in Auto/Eco mode."
        )

    def create_dispatch_event(self, start_utc: str, end_utc: str,
                              delta_kwh: Optional[float] = None,
                              location: Optional[str] = None) -> str:
        """Send a calendar invite. Returns the UID (store as gcal_event_id)."""
        end_utc = _cap_end_at_midnight(start_utc, end_utc)
        summary = _dispatch_summary(start_utc, end_utc, delta_kwh)
        description = self._description(delta_kwh, location)
        uid = str(uuid.uuid4()) + "@smartcharge"
        ics = _request_ics(start_utc, end_utc, summary, description,
                           uid, self.smtp_user, self.invite_to)
        msg = _build_email(self.smtp_user, self.invite_to, summary, description, ics, "REQUEST")
        _send(self.smtp_user, self.smtp_pass, msg)
        return uid

    def update_dispatch_event(self, uid: str, start_utc: str, end_utc: str,
                               sequence: int, delta_kwh: Optional[float] = None,
                               location: Optional[str] = None) -> None:
        """Send an updated invite (same UID, higher SEQUENCE)."""
        end_utc = _cap_end_at_midnight(start_utc, end_utc)
        summary = _dispatch_summary(start_utc, end_utc, delta_kwh, suffix=" [updated]")
        description = self._description(delta_kwh, location)
        ics = _request_ics(start_utc, end_utc, summary, description,
                           uid, self.smtp_user, self.invite_to, sequence=sequence)
        msg = _build_email(self.smtp_user, self.invite_to, summary, description, ics, "REQUEST")
        _send(self.smtp_user, self.smtp_pass, msg)

    def cancel_dispatch_event(self, uid: str, start_utc: str, end_utc: str,
                               sequence: int) -> None:
        """Send a METHOD:CANCEL to remove the event from the recipient's calendar."""
        summary = _dispatch_summary(start_utc, end_utc, None, suffix=" [cancelled]")
        body = "This EV dispatch calendar event has been cancelled."
        ics = _cancel_ics(start_utc, end_utc, summary,
                          uid, self.smtp_user, self.invite_to, sequence=sequence)
        msg = _build_email(self.smtp_user, self.invite_to, summary, body, ics, "CANCEL")
        _send(self.smtp_user, self.smtp_pass, msg)
