#!/usr/bin/env python3
"""
main.py
Orchestratore del progetto unipa-calendar:

  OFFWEB UNIPA -> scraper.py -> transform() -> generate_ics.py -> calendar.ics

Include le protezioni richieste contro pubblicazioni "silenziosamente
rotte": se vengono trovate 0 lezioni valide, il file calendar.ics esistente
NON viene sovrascritto e lo script termina con codice di uscita 1 (che in
GitHub Actions fa fallire il workflow, cosi' non pubblichi mai un
calendario vuoto per errore).

Uso:
    python3 main.py --config config.json [--debug]

    # Modalita' di test locale (nessuna richiesta di rete): legge un file
    # HTML gia' salvato invece di contattare OFFWEB
    python3 main.py --input-html-file fixtures/weekCalendar_real.html --debug
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from generate_ics import LessonEvent, build_uid, render_calendar
from scraper import ScraperError, extract_events_json, fetch_events

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("unipa_calendar.main")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_title(title: str) -> tuple[str, str]:
    """Spezza il campo 'title' di OFFWEB (che contiene insieme insegnamento,
    eventuale modulo, docente e gruppo/cattedra) in:
      - subject: nome insegnamento (+ eventuale "- Mod. ...") -> SUMMARY
      - description: "Docente: ..." + gruppo/cattedra                -> DESCRIPTION
    """
    title_norm = re.sub(r"\s+", " ", title).strip()
    segments = [s.strip() for s in title_norm.split(" - ") if s.strip()]
    if not segments:
        return title_norm, ""

    subject = segments[0]
    docente = None
    note = []
    for seg in segments[1:]:
        if re.match(r"^(gruppo\s+G?\d+|cattedra\s+[A-Za-z]-[A-Za-z])$", seg, re.IGNORECASE):
            note.append(seg)
        elif seg.lower().startswith("mod."):
            subject = f"{subject} - {seg}"
        else:
            docente = seg

    desc_parts = []
    if docente:
        desc_parts.append(f"Docente: {docente}")
    if note:
        desc_parts.append(", ".join(note))
    return subject, "\n".join(desc_parts)


def transform(raw_events: list, cc: str, domain: str) -> list[LessonEvent]:
    out: list[LessonEvent] = []
    seen_uids = set()
    skipped = 0

    for raw in raw_events:
        try:
            event_id = str(raw["id"])
            title = raw["title"]
            start = datetime.strptime(raw["start"], "%Y-%m-%d %H:%M")
            end = datetime.strptime(raw["end"], "%Y-%m-%d %H:%M")
            location = (raw.get("descAulaBreve") or "").strip()
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Evento scartato (campo mancante/non valido: %s): %r", e, raw)
            skipped += 1
            continue

        subject, description = parse_title(title)
        uid = build_uid(cc, event_id, domain)
        if uid in seen_uids:
            logger.warning("UID duplicato ignorato: %s", uid)
            skipped += 1
            continue
        seen_uids.add(uid)

        out.append(LessonEvent(
            uid=uid,
            summary=subject,
            start=start,
            end=end,
            location=location,
            description=description,
        ))

    out.sort(key=lambda e: e.start)
    if skipped:
        logger.warning("Eventi scartati durante la trasformazione: %d", skipped)
    return out


def count_existing_events(ics_path: Path) -> int:
    if not ics_path.exists():
        return 0
    text = ics_path.read_text(encoding="utf-8", errors="replace")
    return len(re.findall(r"^BEGIN:VEVENT", text, re.MULTILINE))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Genera calendar.ics dagli orari UNIPA OFFWEB.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output", default=None, help="Sovrascrive 'output_file' di config.json")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--input-html-file", default=None,
        help="Solo per test locali: usa questo file HTML invece di contattare OFFWEB",
    )
    args = parser.parse_args(argv)

    if args.debug:
        logging.getLogger("unipa_calendar").setLevel(logging.DEBUG)
        for h in logging.getLogger().handlers:
            h.setLevel(logging.DEBUG)

    config = load_config(args.config)
    output_path = Path(args.output or config.get("output_file", "calendar.ics"))
    domain = config.get("uid_domain", "unipa-calendar.local")
    cc = config["unipa"]["cc"]

    try:
        if args.input_html_file:
            html = Path(args.input_html_file).read_text(encoding="utf-8")
            raw_events = extract_events_json(html, debug=args.debug)
        else:
            raw_events = fetch_events(config, debug=args.debug)
    except ScraperError as e:
        logger.error("Recupero/parsing orari fallito: %s", e)
        return 1

    events = transform(raw_events, cc, domain)
    previous_count = count_existing_events(output_path)

    # PROTEZIONE CONTRO ERRORI: 0 lezioni trovate => non sovrascrivere mai
    # un calendario gia' pubblicato, fallire in modo rumoroso.
    if len(events) == 0:
        logger.error(
            "Trovate 0 lezioni valide (calendario precedente ne conteneva: %d). "
            "NON sovrascrivo %s: probabile errore di scraping o pagina OFFWEB cambiata.",
            previous_count, output_path,
        )
        return 1

    if previous_count > 0 and len(events) < previous_count * 0.3:
        logger.warning(
            "Il numero di lezioni e' calato molto rispetto all'ultima pubblicazione "
            "(%d -> %d). Procedo comunque (potrebbe essere fine semestre), ma "
            "verificare manualmente il calendario pubblicato.",
            previous_count, len(events),
        )

    ics_text = render_calendar(
        events,
        calendar_name=config.get("calendar_name", f"UNIPA {cc}"),
        prodid=config.get("prodid", "-//unipa-calendar//IT"),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ics_text, encoding="utf-8", newline="")

    logger.info(
        "OK: %d lezioni scritte in %s (precedenti: %d).",
        len(events), output_path, previous_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
