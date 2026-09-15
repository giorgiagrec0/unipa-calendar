"""
scraper.py
Recupera gli orari delle lezioni dal portale OFFWEB dell'Universita' di
Palermo (weekCalendar.seam) e restituisce la lista di eventi "grezzi".

ANALISI TECNICA (fatta il 15/09/2026 con DevTools/rete reale su
https://offertaformativa.unipa.it/offweb/public/aula/weekCalendar.seam?cc=2392):

- OFFWEB e' effettivamente basato su Java Seam/JSF (RichFaces 3.3.3), MA la
  pagina del calendario NON richiede il classico giro
  GET -> salva JSESSIONID -> estrai javax.faces.ViewState -> POST.
- Una singola richiesta GET anonima, senza alcun cookie e senza ViewState,
  restituisce gia' l'HTML completo con l'intero orario del corso incorporato
  come blocco JSON in uno <script> generato lato server:

      var events = {"result":[{"id":"...","title":"...","start":"2026-09-25 09:00",
                                "descAulaBreve":"Aula 01 - E.19","oidAula":"615",
                                "end":"2026-09-25 12:00"}, ...]};

  Questo e' stato verificato ripetendo la richiesta con fetch(url,
  {credentials:'omit'}) direttamente nel browser (quindi senza alcun cookie
  di sessione preesistente): la risposta e' sempre stata "200 OK" con lo
  stesso identico JSON.
- Il JSON copre l'INTERO semestre (nel test: 141 lezioni, dal 21/09/2026 al
  22/12/2026), non solo la settimana visualizzata: la navigazione
  Prec/Succ/Mese/Settimana/Giorno e' interamente lato client (libreria
  FullCalendar), senza ulteriori chiamate di rete. Quindi UNA sola GET al
  giorno/ogni 3 ore e' sufficiente per avere tutto l'orario.
- I filtri "Corso" (cc), "Anno Corso Insegnamento" (aci), "Indirizzo" (ind) e
  "Tipo Docenza" (docenza) sono passabili anche come querystring GET (es.
  ?cc=2392&aci=1): non serve alcun submit del form ne' POST.
- L'anno accademico non e' selezionabile via querystring in modo affidabile:
  il portale seleziona sempre di default l'anno accademico "corrente" (in
  questo momento 2026/2027), il che e' comodo perche' lo script non deve
  essere aggiornato ad ogni cambio di anno accademico.
- Il campo "id" di ogni evento e' l'identificativo interno della lezione
  lato UNIPA (puo' anche essere negativo, es. "-362892392069126056781"):
  e' stabile nel tempo e viene riusato per generare l'UID iCalendar (vedi
  generate_ics.py), cosi' un cambio di aula/docente NON genera un nuovo
  evento su Apple Calendar ma aggiorna quello esistente.

Nessuna dipendenza esterna e' necessaria (solo libreria standard di Python):
la pagina non richiede JavaScript per essere generata (e' tutto server-side),
quindi non servono Selenium/Playwright.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger("unipa_calendar.scraper")

EVENTS_RE = re.compile(r"var\s+events\s*=\s*(\{.*?\});", re.DOTALL)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ScraperError(Exception):
    """Errore irreversibile durante il recupero o il parsing degli orari."""


def build_url(config: dict) -> str:
    """Costruisce l'URL di OFFWEB a partire dai parametri in config.json.

    Solo 'cc' e' obbligatorio; 'aci', 'indirizzo' (mappato sul parametro
    GET 'ind') e 'docenza' sono opzionali: se lasciati vuoti in config.json,
    il portale applica i propri valori di default (che per il 1 anno
    coincidono con "tutti i gruppi/tutte le cattedre", verificato a mano).
    """
    unipa_cfg = config["unipa"]
    base = unipa_cfg["base_url"]

    params = [("cc", unipa_cfg["cc"])]
    if unipa_cfg.get("aci"):
        params.append(("aci", unipa_cfg["aci"]))
    if unipa_cfg.get("indirizzo"):
        params.append(("ind", unipa_cfg["indirizzo"]))
    if unipa_cfg.get("docenza"):
        params.append(("docenza", unipa_cfg["docenza"]))

    query = "&".join(f"{k}={v}" for k, v in params)
    return f"{base}?{query}"


def fetch_html(url: str, timeout: int = 30, debug: bool = False) -> str:
    """Esegue una GET anonima (nessun cookie/sessione riusato tra le run)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    if debug:
        logger.info("GET %s", url)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        raise ScraperError(f"OFFWEB ha risposto con HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise ScraperError(f"Impossibile raggiungere OFFWEB: {e.reason}") from e

    if debug:
        logger.info("Status: %s | URL finale: %s | dimensione risposta: %d byte",
                     status, final_url, len(raw))

    if status != 200:
        raise ScraperError(f"Status HTTP inatteso da OFFWEB: {status}")

    html = raw.decode(charset, errors="replace")

    if debug:
        preview = html[:300].replace("\n", " ")
        logger.info("Primi 300 caratteri HTML: %r", preview)

    return html


def extract_events_json(html: str, debug: bool = False) -> list:
    """Estrae e valida il blocco 'var events = {...};' incorporato in pagina."""
    match = EVENTS_RE.search(html)
    if not match:
        raise ScraperError(
            "Blocco 'var events = {...}' non trovato nella pagina: la "
            "struttura di OFFWEB potrebbe essere cambiata (vedi log DEBUG "
            "per i primi caratteri dell'HTML ricevuto)."
        )

    raw_json = match.group(1)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ScraperError(f"Il JSON degli eventi non e' valido: {e}") from e

    events = data.get("result")
    if events is None:
        raise ScraperError("Il JSON degli eventi non contiene la chiave 'result'.")

    if debug:
        logger.info("Eventi grezzi trovati nella pagina: %d", len(events))

    return events


def fetch_events(config: dict, debug: bool = False) -> list:
    """Punto d'ingresso principale: GET + estrazione JSON in un solo passo."""
    url = build_url(config)
    html = fetch_html(url, timeout=config.get("http_timeout_seconds", 30), debug=debug)
    return extract_events_json(html, debug=debug)
