"""
portale.py — Accesso al portale JCityGov/Liferay del Comune di Pieve Emanuele.

Modulo condiviso tra scraper giornaliero e backfill storico:
  - sessione HTTP con cookie Liferay
  - scraping delle griglie atti (papca-ap = albo, papca-g = archivio provvedimenti)
  - pagina di dettaglio, link PDF (Base64 in onclick), download
  - estrazione testo PDF (pdfplumber + tabelle + OCR Tesseract di riserva)

Derivato dallo scraper del progetto albo-pretorio (stesso autore, stesso portale).
"""

import re
import json
import time
import base64
import logging
import tempfile
import requests
import pdfplumber
from pathlib import Path
from datetime import date, datetime
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from pdf2image import convert_from_path
    import pytesseract
    OCR_DISPONIBILE = True
except ImportError:
    OCR_DISPONIBILE = False

log = logging.getLogger(__name__)

BASE_URL = "https://pieveemanuele.trasparenza-valutazione-merito.it"

# Griglia dell'Albo Pretorio (pubblicazioni correnti, ~15 giorni)
ALBO_URL = f"{BASE_URL}/web/trasparenza/papca-ap/-/papca/igrid/0/Albo_pretorio/"

# Sezione archivio provvedimenti (storico). La griglia esatta viene scoperta
# a runtime da scopri_griglie_storico(); questi sono i punti di partenza.
STORICO_LANDING_URLS = [
    f"{BASE_URL}/web/trasparenza/papca-g",
    f"{BASE_URL}/web/trasparenza/papca-g9",
]

# Sottocategorie che rappresentano spese
TIPI_SPESA = ["determinazione contabile", "liquidazione"]

SOGLIA_OCR = 50  # caratteri/pagina sotto i quali si attiva l'OCR

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})
_retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://", HTTPAdapter(max_retries=_retry))


def init_sessione() -> None:
    """Visita la homepage per ottenere i cookie di sessione Liferay."""
    try:
        r1 = SESSION.get(f"{BASE_URL}/web/trasparenza", timeout=30)
        log.info(f"Sessione inizializzata: {r1.status_code}, cookie: {list(SESSION.cookies.keys())}")
        r2 = SESSION.get(ALBO_URL, timeout=30)
        log.info(f"Contesto portlet albo: {r2.status_code}")
    except Exception as e:
        log.warning(f"Inizializzazione sessione fallita: {e}")


def _fetch(url: str) -> str:
    resp = SESSION.get(url, timeout=90)
    resp.raise_for_status()
    return resp.text


# ─────────────────────────────────────────────────────────────────────────────
# GRIGLIE ATTI (lista + paginazione)
# ─────────────────────────────────────────────────────────────────────────────

def _ha_tabella_atti(html: str) -> bool:
    """True se la pagina contiene una tabella con le colonne tipiche degli atti."""
    soup = BeautifulSoup(html, "html.parser")
    for tabella in soup.find_all("table"):
        intestazioni = " ".join(th.get_text(strip=True).lower() for th in tabella.find_all("th"))
        if "oggetto" in intestazioni and ("numero" in intestazioni or "registro" in intestazioni):
            return True
    return False


def _url_mostra_lista(pagina_path: str) -> str:
    """URL con l'azione mostraLista del portlet pubblicazioni (lista completa)."""
    return (f"{BASE_URL}/web/trasparenza/{pagina_path}"
            "?p_p_id=jcitygovalbopubblicazioni_WAR_jcitygovalbiportlet"
            "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
            "&p_p_col_id=column-1&p_p_col_count=1"
            "&_jcitygovalbopubblicazioni_WAR_jcitygovalbiportlet_action=mostraLista")


def scopri_griglie_storico() -> list[str]:
    """
    Trova gli URL da cui scaricare l'archivio storico dei provvedimenti.
    Strategie, in ordine:
      1. link /-/papca/igrid/ nelle landing papca-g
      2. link con action mostraLista/cercaPubblicazioni già presenti in pagina
      3. URL mostraLista costruito a mano (pattern JCityGov standard)
      4. la landing stessa, se contiene direttamente la tabella atti
    Se tutto fallisce, logga i link candidati della pagina per diagnosi.
    """
    griglie: list[str] = []

    def aggiungi(url: str, motivo: str):
        if url not in griglie:
            griglie.append(url)
            log.info(f"Griglia storico trovata ({motivo}): {url[:140]}")

    for landing in STORICO_LANDING_URLS:
        try:
            html = _fetch(landing)
        except requests.RequestException as e:
            log.warning(f"Landing storico non raggiungibile ({landing}): {e}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        candidati_diagnosi = []

        # 1+2. Link utili già presenti nell'HTML
        for a in soup.find_all("a", href=True):
            href = a["href"]
            url = href if href.startswith("http") else BASE_URL + href
            if "/-/papca/igrid/" in href:
                aggiungi(url.split("?")[0], "link igrid")
            elif "mostraLista" in href or "cercaPubblicazioni" in href:
                aggiungi(url, "link mostraLista")
            elif "papca" in href or "jcitygov" in href.lower():
                candidati_diagnosi.append(f"{a.get_text(strip=True)[:40]!r} → {href[:120]}")

        # 4. La landing contiene già la tabella?
        if _ha_tabella_atti(html):
            aggiungi(landing, "tabella nella landing")

        # 3. mostraLista costruito a mano
        if not griglie:
            pagina_path = landing.rstrip("/").split("/")[-1]
            url_tentativo = _url_mostra_lista(pagina_path)
            try:
                html_lista = _fetch(url_tentativo)
                if _ha_tabella_atti(html_lista):
                    aggiungi(url_tentativo, "mostraLista costruito")
                else:
                    log.info(f"mostraLista su {pagina_path}: risponde ma senza tabella atti "
                             f"({len(html_lista)} byte)")
            except requests.RequestException as e:
                log.info(f"mostraLista su {pagina_path} non funziona: {e}")

        # Diagnosi: se ancora nulla, mostra cosa c'è davvero in pagina
        if not griglie and candidati_diagnosi:
            log.warning(f"DIAGNOSI {landing} — link papca/jcitygov presenti in pagina:")
            for c in candidati_diagnosi[:40]:
                log.warning(f"  {c}")

    if not griglie:
        log.warning("Nessuna griglia storico trovata. Il backfill userà solo l'albo corrente. "
                    "Controlla i log DIAGNOSI qui sopra per capire la struttura reale.")
    return griglie


def scrape_griglia(url_griglia: str,
                   atti_noti: set[tuple] | None = None,
                   max_pagine: int = 10_000,
                   stop_se_tutti_noti: bool = True) -> list[dict]:
    """
    Scarica una griglia atti JCityGov pagina per pagina.
    Restituisce righe grezze: numero_raw, tipo, oggetto, date, url_dettaglio.

    atti_noti + stop_se_tutti_noti: interruzione anticipata per il run
    giornaliero (i nuovi atti sono sempre nelle prime pagine).
    """
    atti_noti = atti_noti or set()
    atti: list[dict] = []
    url_corrente: str | None = url_griglia
    pagina = 1

    while url_corrente and pagina <= max_pagine:
        log.info(f"Scarico pagina {pagina}: {url_corrente[:120]}")
        try:
            html = _fetch(url_corrente)
        except requests.RequestException as e:
            log.error(f"Errore HTTP sulla pagina {pagina}: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        tabella = soup.find("table")
        if not tabella:
            log.warning(f"Nessuna tabella a pagina {pagina}. Fine elenco.")
            break

        intestazioni = [th.get_text(strip=True) for th in tabella.find_all("th")]
        idx = _trova_indici_colonne(intestazioni)

        nuovi_in_pagina = 0
        for riga in tabella.find_all("tr")[1:]:
            celle = riga.find_all("td")
            if len(celle) < max(idx.values()) + 1:
                continue
            atto = _estrai_atto_da_riga(celle, idx, riga)
            if atto:
                atti.append(atto)
                if (atto["numero_raw"], atto["oggetto"]) not in atti_noti:
                    nuovi_in_pagina += 1

        if stop_se_tutti_noti and atti_noti and nuovi_in_pagina == 0:
            log.info(f"  Pagina {pagina}: tutti gli atti già noti, stop paginazione")
            break

        url_corrente = _trova_link_avanti(soup, url_corrente)
        pagina += 1
        time.sleep(1)  # rispetto del server

    log.info(f"Griglia {url_griglia[:80]}: {len(atti)} righe totali")
    return atti


def _trova_indici_colonne(intestazioni: list[str]) -> dict:
    idx = {"numero": 0, "tipo": 1, "oggetto": 2, "periodo": 3}
    for i, h in enumerate(intestazioni):
        h_lower = h.lower()
        if "numero" in h_lower or "registro" in h_lower:
            idx["numero"] = i
        elif "tipo" in h_lower:
            idx["tipo"] = i
        elif "oggetto" in h_lower:
            idx["oggetto"] = i
        elif "periodo" in h_lower or "pubblicazion" in h_lower or "data" in h_lower:
            idx["periodo"] = i
    return idx


def _estrai_atto_da_riga(celle, idx: dict, riga) -> dict | None:
    try:
        numero_raw = celle[idx["numero"]].get_text(strip=True)
        tipo       = celle[idx["tipo"]].get_text(strip=True)
        oggetto    = celle[idx["oggetto"]].get_text(strip=True)
        periodo    = celle[idx["periodo"]].get_text(strip=True)

        date_pub = _parse_periodo(periodo)

        link_tag = riga.find("a", title="Apri Dettaglio") or riga.find("a", href=True)
        url_dettaglio = None
        if link_tag and link_tag.get("href"):
            href = link_tag["href"]
            url_dettaglio = href if href.startswith("http") else BASE_URL + href

        return {
            "numero_raw": numero_raw,
            "tipo": tipo,
            "oggetto": oggetto,
            "data_inizio": date_pub.get("inizio"),
            "data_fine": date_pub.get("fine"),
            "url_dettaglio": url_dettaglio,
        }
    except Exception as e:
        log.warning(f"Errore nell'estrarre riga: {e}")
        return None


def _parse_periodo(periodo: str) -> dict:
    trovate = re.findall(r"\d{2}/\d{2}/\d{4}", periodo)
    out = {"inizio": None, "fine": None}
    if len(trovate) >= 1:
        out["inizio"] = _iso(trovate[0])
    if len(trovate) >= 2:
        out["fine"] = _iso(trovate[1])
    return out


def _iso(data_it: str) -> str:
    try:
        return datetime.strptime(data_it, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return data_it


def _trova_link_avanti(soup: BeautifulSoup, url_corrente: str) -> str | None:
    paginazione = soup.find("div", class_="pagination pagination-centered")
    candidati = paginazione.find_all("a") if paginazione else soup.find_all("a")
    for link in candidati:
        if link.get_text(strip=True) in ("Avanti", "»", "›", "Next", ">"):
            href = link.get("href", "")
            if href and href != "#" and href != url_corrente:
                return href if href.startswith("http") else BASE_URL + href
    return None


def e_spesa(tipo: str) -> bool:
    """True se il tipo/sottocategoria dell'atto rappresenta una spesa."""
    t = (tipo or "").lower()
    return any(k in t for k in TIPI_SPESA)


# ─────────────────────────────────────────────────────────────────────────────
# DETTAGLIO ATTO + PDF + TESTO
# ─────────────────────────────────────────────────────────────────────────────

def estrai_testo_atto(atto: dict) -> str:
    """
    Visita la pagina di dettaglio, scarica il documento principale (PDF)
    in una cartella temporanea, ne estrae il testo e lo restituisce.
    I PDF NON vengono conservati (il repository resterebbe enorme con anni
    di backfill): resta il link all'atto originale sul portale.
    """
    url = atto.get("url_dettaglio")
    if not url:
        return ""

    urls_da_provare = [url]
    url_no_popup = re.sub(r"[&?]p_p_state=pop_up", "", url)
    if url_no_popup != url:
        urls_da_provare.append(url_no_popup)

    html = None
    for tentativo in urls_da_provare:
        try:
            html = _fetch(tentativo)
            break
        except requests.RequestException as e:
            log.warning(f"  Dettaglio non raggiungibile ({tentativo[:80]}): {e}")
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    link_pdf = _trova_link_pdf(soup)

    if link_pdf:
        with tempfile.TemporaryDirectory() as tmp:
            percorso = Path(tmp) / "atto.pdf"
            if _scarica_pdf(link_pdf[0], percorso):
                testo = _estrai_testo_pdf(percorso)
                if testo:
                    return testo

    # Fallback: testo incorporato nell'HTML della pagina di dettaglio
    return _estrai_testo_inline_html(soup)


def url_display_stabile(url_dettaglio: str) -> str:
    """
    Dall'URL di dettaglio (che contiene p_auth di sessione, volatile) ricava
    l'URL /display/<id> stabile e condivisibile.
    """
    m = re.search(r"(/web/trasparenza/[^?]*/-/papca/display/\d+)", url_dettaglio or "")
    if m:
        return BASE_URL + m.group(1)
    return re.sub(r"[?&]p_auth=[^&]+", "", url_dettaglio or "")


def _trova_link_pdf(soup: BeautifulSoup) -> list[str]:
    """
    JCityGov codifica le URL di download in Base64 dentro onclick:
      onclick="...window.open(atob('BASE64'), '_blank')..."
    Il 'Documento principale' va per primo.
    """
    def _url_da_riga(tr) -> str | None:
        url_unsigned = url_signed = None
        for tag in tr.find_all("a", onclick=True):
            for b64 in re.findall(r"atob\('([A-Za-z0-9+/=]+)'\)", tag.get("onclick", "")):
                try:
                    u = base64.b64decode(b64).decode("utf-8")
                except Exception:
                    continue
                if "downloadAllegato" not in u:
                    continue
                if "downloadSigned=false" in u:
                    url_unsigned = u
                elif "downloadSigned=true" in u:
                    url_signed = u
        return url_unsigned or url_signed

    tabella = soup.find(class_="allegati-table") or soup.find("table")
    principale, altri = None, []
    if tabella:
        for tr in tabella.find_all("tr"):
            celle = tr.find_all("td")
            if len(celle) < 2:
                continue
            descr = celle[1].get_text(strip=True).lower()
            u = _url_da_riga(tr)
            if not u:
                continue
            if "documento principale" in descr:
                principale = u
            else:
                altri.append(u)

    risultato = ([principale] if principale else []) + [u for u in altri if u != principale]
    if risultato:
        return risultato

    # Fallback per strutture diverse
    out, visti = [], set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.lower().endswith(".pdf") or "downloadAllegato" in href:
            u = href if href.startswith("http") else BASE_URL + href
            if u not in visti:
                out.append(u)
                visti.add(u)
    return out


def _scarica_pdf(url: str, destinazione: Path) -> bool:
    try:
        resp = SESSION.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        chunks, primi = [], b""
        for chunk in resp.iter_content(chunk_size=8192):
            chunks.append(chunk)
            if len(primi) < 4:
                primi += chunk
        if primi[:4] != b"%PDF":
            log.debug(f"  ⊘ Allegato non-PDF scartato (magic={primi[:4]!r})")
            return False
        with open(destinazione, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        return True
    except Exception as e:
        log.error(f"  ✗ Errore download PDF: {e}")
        return False


def _estrai_testo_pdf(percorso: Path) -> str:
    testo, media = "", 0.0
    try:
        with pdfplumber.open(percorso) as pdf:
            pagine = []
            for pagina in pdf.pages:
                parti = []
                t = pagina.extract_text() or ""
                if t:
                    parti.append(t)
                for tabella in (pagina.extract_tables() or []):
                    for riga in tabella:
                        cells = [str(c).strip() if c else "" for c in riga]
                        rt = " | ".join(c for c in cells if c)
                        if rt:
                            parti.append(rt)
                pagine.append("\n".join(parti))
            testo = "\n".join(pagine).strip()
            media = len(testo) / max(len(pagine), 1)
    except Exception as e:
        log.warning(f"  pdfplumber fallito: {e}")

    if media < SOGLIA_OCR and OCR_DISPONIBILE:
        log.info(f"  → OCR Tesseract attivato ({media:.0f} char/pagina)")
        testo = _ocr_pdf(percorso) or testo
    return testo.strip()


def _ocr_pdf(percorso: Path) -> str:
    try:
        immagini = convert_from_path(str(percorso), dpi=300)
        return "\n".join(pytesseract.image_to_string(img, lang="ita") for img in immagini).strip()
    except Exception as e:
        log.error(f"  Errore OCR: {e}")
        return ""


def _estrai_testo_inline_html(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "header", "footer", "link", "meta"]):
        tag.decompose()
    contenuto = None
    for sel in ["div.portlet-body", "div.portlet-content", "main", "div#content", "body"]:
        contenuto = soup.select_one(sel)
        if contenuto:
            break
    testo = (contenuto or soup).get_text(separator="\n", strip=True)
    righe = [r.strip() for r in testo.splitlines() if len(r.strip()) > 20]
    pulito = "\n".join(righe)
    return pulito if len(pulito) >= 200 else ""


# ─────────────────────────────────────────────────────────────────────────────
# HELPER IDENTIFICATIVI
# ─────────────────────────────────────────────────────────────────────────────

def estrai_numero(numero_raw: str) -> str:
    parti = re.split(r"[/\-]", (numero_raw or "").strip())
    if len(parti) >= 2:
        return parti[-1].strip().zfill(3)
    return (numero_raw or "").strip()


def estrai_anno(atto: dict) -> int:
    for campo in ("data_inizio", "data_fine"):
        m = re.search(r"\b(20\d{2})\b", atto.get(campo) or "")
        if m:
            return int(m.group(1))
    m = re.search(r"\b(20\d{2})\b", atto.get("numero_raw", ""))
    if m:
        return int(m.group(1))
    return date.today().year


def genera_id(atto: dict) -> str:
    tipo = "liquidazione" if "liquidazione" in (atto.get("tipo", "").lower()) else "determinazione"
    base = f"{tipo}-{estrai_numero(atto.get('numero_raw',''))}-{estrai_anno(atto)}"
    return re.sub(r"[^a-z0-9\-]", "", base.lower())
