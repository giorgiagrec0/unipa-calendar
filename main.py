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

Oltre a calendar.ics (tutte le lezioni) genera nella cartella "cal/" un
calendario personalizzato per ogni combinazione di gruppi/cattedre definita
in "scelte" dentro config.json, piu' cal/scelte.json che index.html usa per
mostrare a ogni studente il link giusto.

Uso:
    python3 main.py --config config.json [--debug]

    # Modalita' di test locale (nessuna richiesta di rete): legge un file
    # HTML gia' salvato invece di contattare OFFWEB
    python3 main.py --input-html-file fixtures/weekCalendar_real.html --debug
"""
from __future__ import annotations

import argparse
import itertools
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


def parse_title(title: str) -> tuple[str, str, str]:
    """Spezza il campo 'title' di OFFWEB (che contiene insieme insegnamento,
    eventuale modulo, docente e gruppo/cattedra) in:
      - subject: nome insegnamento (+ eventuale "- Mod. ...") -> SUMMARY
      - description: "Docente: ..." + gruppo/cattedra                -> DESCRIPTION
      - variant: gruppo/cattedra (es. "gruppo G1"), "" se assente
    """
    title_norm = re.sub(r"\s+", " ", title).strip()
    segments = [s.strip() for s in title_norm.split(" - ") if s.strip()]
    if not segments:
        return title_norm, "", ""

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
    return subject, "\n".join(desc_parts), (note[-1] if note else "")


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

        subject, description, variant = parse_title(title)
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
            variant=variant,
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


def write_if_changed(path: Path, text: str) -> bool:
    """Scrive il file solo se le lezioni sono cambiate. DTSTAMP (l'ora di
    generazione) cambia a ogni esecuzione e non va considerato, altrimenti
    ogni 3 ore verrebbe fatto un commit anche senza modifiche agli orari."""
    def strip_stamp(s: str) -> str:
        return re.sub(r"^DTSTAMP:.*$", "", s, flags=re.MULTILINE)

    if path.exists():
        # newline="": confronta i \r\n cosi' come sono scritti nel file
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            old = f.read()
        if strip_stamp(old) == strip_stamp(text):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return True


TUTTI = "tutti"


def option_code(label: str) -> str:
    """Es. "gruppo G1" -> "G1", "cattedra A-L" -> "A-L"."""
    return label.split()[-1].upper() if label.strip() else ""


def find_choice(event: LessonEvent, choices: list) -> dict | None:
    summary = event.summary.upper()
    for choice in choices:
        if choice["cerca"].upper() in summary:
            return choice
    return None


def filter_events(events: list[LessonEvent], choices: list, selection: dict) -> list[LessonEvent]:
    """Tiene le lezioni comuni + quelle del gruppo/cattedra scelto per ogni
    insegnamento in "scelte". Le lezioni senza gruppo restano sempre."""
    kept = []
    for ev in events:
        choice = find_choice(ev, choices)
        if choice is None or not ev.variant:
            kept.append(ev)
            continue
        wanted = selection[choice["id"]]
        if wanted == TUTTI or option_code(ev.variant) == wanted:
            kept.append(ev)
    return kept


def check_choices(events: list[LessonEvent], choices: list) -> None:
    """Avvisa nel log se UNIPA pubblica gruppi/cattedre non previsti in
    config.json, cosi' si sa che va aggiornata la lista delle opzioni."""
    unknown: dict[str, set] = {}
    for ev in events:
        if not ev.variant:
            continue
        choice = find_choice(ev, choices)
        if choice is None:
            unknown.setdefault(ev.summary, set()).add(ev.variant)
        elif option_code(ev.variant) not in {option_code(o) for o in choice["opzioni"]}:
            unknown.setdefault(ev.summary, set()).add(ev.variant)
    for summary, variants in unknown.items():
        logger.warning(
            "Gruppi/cattedre non previsti in config.json per %r: %s "
            "(aggiungerli in 'scelte' per poterli selezionare).",
            summary, ", ".join(sorted(variants)),
        )


def write_personal_calendars(events: list[LessonEvent], config: dict) -> None:
    choices = config.get("scelte", [])
    if not choices:
        return
    out_dir = Path(config.get("cartella_personalizzati", "cal"))
    check_choices(events, choices)

    codes_per_choice = [
        [option_code(o) for o in c["opzioni"]] + [TUTTI] for c in choices
    ]
    generated = set()
    changed = 0
    for combo in itertools.product(*codes_per_choice):
        selection = {c["id"]: code for c, code in zip(choices, combo)}
        name = "_".join(f"{c['id']}-{code}" for c, code in zip(choices, combo)) + ".ics"
        chosen = [code for code in combo if code != TUTTI]
        cal_name = config.get("calendar_name", "UNIPA")
        if chosen:
            cal_name = f"{cal_name} ({', '.join(chosen)})"
        ics_text = render_calendar(
            filter_events(events, choices, selection),
            calendar_name=cal_name,
            prodid=config.get("prodid", "-//unipa-calendar//IT"),
        )
        changed += write_if_changed(out_dir / name, ics_text)
        generated.add(name)

    # rimuove calendari di combinazioni non piu' previste in config.json
    for old in out_dir.glob("*.ics"):
        if old.name not in generated:
            old.unlink()

    manifest = {
        "scelte": [
            {
                "id": c["id"],
                "nome": c["nome"],
                "opzioni": [{"codice": option_code(o), "etichetta": o} for o in c["opzioni"]],
            }
            for c in choices
        ]
    }
    write_if_changed(
        out_dir / "scelte.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    logger.info("Calendari personalizzati: %d (modificati: %d) in %s/.",
                len(generated), changed, out_dir)


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

    write_if_changed(output_path, ics_text)
    write_personal_calendars(events, config)

    logger.info(
        "OK: %d lezioni scritte in %s (precedenti: %d).",
        len(events), output_path, previous_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
