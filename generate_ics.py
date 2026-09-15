"""
generate_ics.py
Costruisce un calendario iCalendar (RFC 5545) a partire dagli eventi gia'
puliti (LessonEvent). Nessuna dipendenza esterna: la generazione ICS
(line-folding, escaping, VTIMEZONE Europe/Rome con passaggio ora
legale/solare) e' scritta con la sola libreria standard di Python.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

TZID = "Europe/Rome"

# Blocco VTIMEZONE standard per l'Europa continentale (stesse regole UE per
# tutti i fusi Europe/*): passaggio a ora legale l'ultima domenica di marzo,
# a ora solare l'ultima domenica di ottobre. Necessario perche' l'orario
# delle lezioni copre sia settembre (CEST, +02:00) sia dicembre (CET,
# +01:00): senza VTIMEZONE gli orari nei mesi dopo fine ottobre
# sfaserebbero di un'ora su Apple Calendar.
VTIMEZONE_BLOCK = "\r\n".join([
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "X-LIC-LOCATION:Europe/Rome",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
])


@dataclass
class LessonEvent:
    uid: str
    summary: str
    start: datetime  # naive, ora locale Europe/Rome
    end: datetime
    location: str
    description: str


def _fold_line(line: str) -> str:
    """RFC 5545 §3.1: le righe vanno spezzate a 75 ottetti, continuazione
    su riga successiva che inizia con uno spazio."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line

    parts = []
    while len(data) > 75:
        chunk = data[:75]
        # non spezzare a meta' di un carattere UTF-8 multi-byte
        while True:
            try:
                chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        data = data[len(chunk):]
    parts.append(data.decode("utf-8"))
    return "\r\n ".join(parts)


def _escape_text(value: str) -> str:
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\r\n", "\\n").replace("\n", "\\n")
    return value


def _fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _fmt_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_uid(course_code: str, event_id: str, domain: str) -> str:
    """UID stabile: dipende SOLO dal codice corso (cc) e dall'id lezione
    assegnato da UNIPA, MAI da aula/docente/altri dettagli descrittivi.
    Cosi', se OFFWEB cambia aula o docente di una lezione gia' pubblicata,
    Apple Calendar la riconosce come lo STESSO evento aggiornato invece di
    crearne uno duplicato.
    """
    safe_id = event_id.replace("-", "n")  # gli id UNIPA possono essere negativi
    return f"unipa-{course_code}-{safe_id}@{domain}"


def render_calendar(events: Iterable[LessonEvent], calendar_name: str, prodid: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold_line(f"X-WR-CALNAME:{_escape_text(calendar_name)}"),
        f"X-WR-TIMEZONE:{TZID}",
        # Suggerimento (non vincolante) ai client che lo supportano a
        # ricontrollare l'URL ogni 3 ore, in linea con l'aggiornamento
        # automatico via GitHub Actions.
        "X-PUBLISHED-TTL:PT3H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT3H",
        VTIMEZONE_BLOCK,
    ]

    dtstamp = _fmt_utc_now()
    for ev in events:
        lines.append("BEGIN:VEVENT")
        lines.append(_fold_line(f"UID:{ev.uid}"))
        lines.append(f"DTSTAMP:{dtstamp}")
        lines.append(f"DTSTART;TZID={TZID}:{_fmt_local(ev.start)}")
        lines.append(f"DTEND;TZID={TZID}:{_fmt_local(ev.end)}")
        lines.append(_fold_line(f"SUMMARY:{_escape_text(ev.summary)}"))
        if ev.location:
            lines.append(_fold_line(f"LOCATION:{_escape_text(ev.location)}"))
        if ev.description:
            lines.append(_fold_line(f"DESCRIPTION:{_escape_text(ev.description)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
