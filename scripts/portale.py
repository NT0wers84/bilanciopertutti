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
]

# Radici da cui scoprire TUTTE le sezioni con griglie atti:
# l'albero di Amministrazione Trasparente e la Pubblicità Legale (albo).
RADICI_DISCOVERY = [
    f"{BASE_URL}/web/trasparenza/trasparenza",
    f"{BASE_URL}/web/trasparenza",
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


def scopri_sezioni() -> list[str]:
    """
    Scopre tutte le sezioni del portale (Amministrazione Trasparente +
    Pubblicità Legale) che potrebbero contenere griglie di atti.
    Restituisce gli URL delle pagine di sezione /web/trasparenza/<slug>.
    """
    sezioni: list[str] = list(STORICO_LANDING_URLS)
    for radice in RADICI_DISCOVERY:
        try:
            html = _fetch(radice)
        except requests.RequestException as e:
            log.warning(f"Radice discovery non raggiungibile ({radice}): {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0]
            if not href:
                continue
            url = href if href.startswith("http") else BASE_URL + href
            # Solo pagine di sezione interne, senza parametri (le griglie
            # con parametri vengono scoperte dentro le sezioni)
            m = re.match(rf"{re.escape(BASE_URL)}/web/trasparenza/([a-z0-9\-]+)/?$", url)
            if not m:
                continue
            slug = m.group(1)
            if slug in ("trasparenza",) or url in sezioni:
                continue
            sezioni.append(url)
    log.info(f"Sezioni del portale scoperte: {len(sezioni)}")
    for s in sezioni:
        log.info(f"  sezione: {s}")
    return sezioni


def scopri_griglie_storico() -> list[str]:
    """
    Trova gli URL da cui scaricare l'archivio storico degli atti, scandagliando
    tutte le sezioni del portale. Strategie per ogni sezione, in ordine:
      1. link /-/papca/igrid/ nella pagina
      2. link con action mostraLista/cercaPubblicazioni già presenti in pagina
      3. URL mostraLista costruito a mano (pattern JCityGov standard)
      4. la pagina stessa, se contiene direttamente la tabella atti
    Se tutto fallisce, logga i link candidati della pagina per diagnosi.
    """
    griglie: list[str] = []

    def aggiungi(url: str, motivo: str):
        if url not in griglie:
            griglie.append(url)
            log.info(f"Griglia storico trovata ({motivo}): {url[:140]}")

    for landing in scopri_sezioni():
        try:
            html = _fetch(landing)
        except requests.RequestException as e:
            log.warning(f"Landing storico non raggiungibile ({landing}): {e}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        candidati_diagnosi = []
        griglie_prima = len(griglie)

        # 1+2. Link utili già presenti nell'HTML
        for a in soup.find_all("a", href=True):
            href = a["href"]
            url = href if href.startswith("http") else BASE_URL + href
            if "/-/papca/igrid/" in href:
                aggiungi(url.split("?")[0], f"link igrid in {landing.rsplit('/',1)[-1]}")
            elif "mostraLista" in href or "cercaPubblicazioni" in href:
                aggiungi(url, f"link mostraLista in {landing.rsplit('/',1)[-1]}")
            elif "papca" in href or "jcitygov" in href.lower():
                candidati_diagnosi.append(f"{a.get_text(strip=True)[:40]!r} → {href[:120]}")

        # 4. La pagina contiene già la tabella?
        if _ha_tabella_atti(html):
            aggiungi(landing, "tabella nella pagina")

        # 3. mostraLista costruito a mano (solo se questa sezione non ha dato nulla)
        if len(griglie) == griglie_prima:
            pagina_path = landing.rstrip("/").split("/")[-1]
            url_tentativo = _url_mostra_lista(pagina_path)
            try:
                html_lista = _fetch(url_tentativo)
                if _ha_tabella_atti(html_lista):
                    aggiungi(url_tentativo, "mostraLista costruito")
            except requests.RequestException:
                pass

        # Diagnosi: se questa sezione non ha dato nulla ma ha link sospetti
        if len(griglie) == griglie_prima and candidati_diagnosi:
            log.warning(f"DIAGNOSI {landing} — link papca/jcitygov presenti in pagina:")
            for c in candidati_diagnosi[:20]:
                log.warning(f"  {c}")

    if not griglie:
        log.warning("Nessuna griglia storico trovata. Il backfill userà solo l'albo corrente. "
                    "Controlla i log DIAGNOSI qui sopra per capire la struttura reale.")
    return griglie


def scrape_griglia(url_griglia: str,
                   atti_noti: set[tuple] | None = None,
                   max_pagine: int = 10_000,
                   stop_se_tutti_noti: bool = True,
                   html_prima_pagina: str | None = None) -> list[dict]:
    """
    Scarica una griglia atti JCityGov pagina per pagina.
    Restituisce righe grezze: numero_raw, tipo, oggetto, date, url_dettaglio.

    atti_noti + stop_se_tutti_noti: interruzione anticipata per il run
    giornaliero (i nuovi atti sono sempre nelle prime pagine).
    """
    atti_noti = atti_noti or set()
    atti: list[dict] = []
    chiavi_raccolte: set[tuple] = set()
    url_corrente: str | None = url_griglia
    pagina = 1
    firma_precedente = None
    modalita_cur = False

    while url_corrente and pagina <= max_pagine:
        if pagina == 1 or pagina % 25 == 0 or not modalita_cur:
            log.info(f"Scarico pagina {pagina}: {url_corrente[:120]}")
        if pagina == 1 and html_prima_pagina is not None:
            html = html_prima_pagina
        else:
            try:
                html = _fetch(url_corrente)
            except requests.RequestException as e:
                log.error(f"Errore HTTP sulla pagina {pagina}: {e}")
                break

        soup = BeautifulSoup(html, "html.parser")
        tabella = soup.find("table")
        if not tabella:
            log.info(f"Nessuna tabella a pagina {pagina}. Fine elenco.")
            break

        intestazioni = [th.get_text(strip=True) for th in tabella.find_all("th")]
        idx = _trova_indici_colonne(intestazioni)

        righe_pagina = []
        for riga in tabella.find_all("tr")[1:]:
            celle = riga.find_all("td")
            if len(celle) < max(idx.values()) + 1:
                continue
            atto = _estrai_atto_da_riga(celle, idx, riga)
            if atto:
                righe_pagina.append(atto)

        if not righe_pagina:
            log.info(f"Pagina {pagina} senza righe. Fine elenco.")
            break

        # Stop anti-loop: la paginazione _cur ignorata restituisce sempre pagina 1
        firma = (righe_pagina[0]["numero_raw"], righe_pagina[0]["oggetto"],
                 righe_pagina[-1]["numero_raw"], righe_pagina[-1]["oggetto"])
        if firma == firma_precedente:
            log.info(f"Pagina {pagina} identica alla precedente: fine paginazione.")
            break
        firma_precedente = firma

        nuovi_in_pagina = 0
        duplicate_scrape = 0
        for atto in righe_pagina:
            chiave = (atto["numero_raw"], atto["oggetto"])
            if chiave in chiavi_raccolte:
                duplicate_scrape += 1
                continue
            chiavi_raccolte.add(chiave)
            atti.append(atto)
            if chiave not in atti_noti:
                nuovi_in_pagina += 1

        # Se l'intera pagina è fatta di righe già viste in questo scrape,
        # la paginazione sta girando a vuoto.
        if duplicate_scrape == len(righe_pagina):
            log.info(f"Pagina {pagina}: solo righe già raccolte, stop.")
            break

        if stop_se_tutti_noti and atti_noti and nuovi_in_pagina == 0:
            log.info(f"  Pagina {pagina}: tutti gli atti già noti, stop paginazione")
            break

        prossimo = _trova_link_avanti(soup, url_corrente)
        if prossimo is None and len(righe_pagina) >= 10:
            # 'Avanti' è solo JavaScript: ripiego sul parametro Liferay _cur
            prossimo = _url_con_cur(url_griglia, pagina + 1)
            if not modalita_cur:
                log.info(f"Paginazione JS rilevata: passo al parametro _cur "
                         f"({prossimo[:120]})")
                modalita_cur = True
        url_corrente = prossimo
        pagina += 1
        time.sleep(1)  # rispetto del server

    tipi = {}
    for a in atti:
        tipi[a["tipo"]] = tipi.get(a["tipo"], 0) + 1
    log.info(f"Griglia {url_griglia[:80]}: {len(atti)} righe in {pagina} pagine")
    log.info("Tipi distinti trovati (per capire le etichette delle spese):")
    for t, c in sorted(tipi.items(), key=lambda x: -x[1]):
        marcatore = " ← SPESA" if e_spesa(t) else ""
        log.info(f"  {c:5} × {t!r}{marcatore}")
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
    """
    Cerca il link 'Avanti'. Nelle griglie igrid è un href statico; nelle
    griglie mostraLista è javascript:void(0) con l'URL vero (se c'è) dentro
    l'onclick. Gli href javascript:/# vengono scartati.
    """
    paginazione = soup.find("div", class_="pagination pagination-centered")
    candidati = paginazione.find_all("a") if paginazione else soup.find_all("a")
    for link in candidati:
        if link.get_text(strip=True) not in ("Avanti", "»", "›", "Next", ">"):
            continue
        href = link.get("href", "")
        if href and href not in ("#", url_corrente) and not href.lower().startswith("javascript"):
            return href if href.startswith("http") else BASE_URL + href
        # URL nascosto nell'onclick (pattern Liferay: location.href='...' o open('...'))
        onclick = link.get("onclick", "") or ""
        m = re.search(r"['\"](https?://[^'\"]+|/[^'\"]{10,})['\"]", onclick)
        if m:
            u = m.group(1)
            return u if u.startswith("http") else BASE_URL + u
    return None


def _url_con_cur(url_griglia: str, cur: int) -> str:
    """
    Paginazione Liferay SearchContainer: parametro _<portlet>_cur=N.
    Usata come ripiego quando 'Avanti' è solo JavaScript.
    """
    param = "_jcitygovalbopubblicazioni_WAR_jcitygovalbiportlet_cur"
    base = re.sub(rf"[&?]{param}=\d+", "", url_griglia)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{param}={cur}"


def e_spesa(tipo: str) -> bool:
    """True se il tipo/sottocategoria dell'atto rappresenta una spesa."""
    t = (tipo or "").lower()
    return any(k in t for k in TIPI_SPESA)


PORTLET_PREFIX = "_jcitygovalbopubblicazioni_WAR_jcitygovalbiportlet_"

# Pagine con il form di ricerca del portlet pubblicazioni:
# (nome, pagina col form, slug per costruire l'URL lista)
SORGENTI_RICERCA = [
    ("provvedimenti", f"{BASE_URL}/web/trasparenza/papca-g", "papca-g"),
    ("albo", ALBO_URL, "papca-ap"),
]


def _conta_righe_tabella(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    tabella = soup.find("table")
    if not tabella:
        return 0
    return sum(1 for tr in tabella.find_all("tr") if tr.find_all("td"))


def _iso_a_it(data_iso: str) -> str:
    try:
        return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return data_iso


# ─────────────────────────────────────────────────────────────────────────────
# RICERCA ARCHIVIO — ricetta verificata sul portale reale (browser, 2026-07):
#   1. POST eseguiOrdinamentoLista con annoRegistrazioneDa=<anno> imposta il
#      filtro nella sessione. IL FILTRO DATA (dataPubblicazioneDa/A) È ROTTO
#      LATO SERVER: qualunque valore restituisce 0 risultati. Mai inviarlo.
#      annoRegistrazioneDa è un filtro "da anno in poi" (>=).
#   2. POST eseguiPaginazione con hidden_page_to=N e hidden_page_size=100
#      scorre la lista filtrata (il filtro resta in sessione).
# Senza filtri l'albo espone TUTTO l'archivio storico (7.954 atti al 2026-07,
# dal maggio 2021 in poi).
# ─────────────────────────────────────────────────────────────────────────────


def _parse_tabella(html: str) -> list[dict]:
    """Estrae le righe atti dalla prima tabella di una pagina (senza paginare)."""
    soup = BeautifulSoup(html, "html.parser")
    tabella = soup.find("table")
    if not tabella:
        return []
    intestazioni = [th.get_text(strip=True) for th in tabella.find_all("th")]
    idx = _trova_indici_colonne(intestazioni)
    righe = []
    for riga in tabella.find_all("tr")[1:]:
        celle = riga.find_all("td")
        if len(celle) < max(idx.values()) + 1:
            continue
        atto = _estrai_atto_da_riga(celle, idx, riga)
        if atto:
            righe.append(atto)
    return righe


def _leggi_form_ricerca(url_pagina: str):
    """Trova il form con i campi data. Restituisce (action_url, dati_base) o None."""
    try:
        html = _fetch(url_pagina)
    except requests.RequestException as e:
        log.warning(f"Ricerca: pagina form non raggiungibile ({e})")
        return None
    soup = BeautifulSoup(html, "html.parser")
    for f in soup.find_all("form"):
        if f.find("input", attrs={"name": f"{PORTLET_PREFIX}dataPubblicazioneDa"}):
            azione = f.get("action", "")
            if not azione or azione == "#":
                continue
            dati = {}
            for campo in f.find_all(["input", "button"]):
                nome = campo.get("name")
                if nome:
                    dati[nome] = campo.get("value", "")
            return azione, dati
    log.warning(f"Ricerca: nessun form con campo data in {url_pagina[:100]}")
    return None


def _post_filtro(azione: str, dati_base: dict, nome_azione: str, sse: str,
                 data_da: str, data_a: str, formato: str) -> str | None:
    """Un singolo tentativo di POST del filtro. Restituisce l'HTML di risposta."""
    url_azione = re.sub(r"(_action=)[A-Za-z]+", rf"\g<1>{nome_azione}", azione)
    dati = dict(dati_base)
    dati[f"{PORTLET_PREFIX}simpleSearchEnable"] = sse
    conv = _iso_a_it if formato == "it" else (lambda d: d)
    dati[f"{PORTLET_PREFIX}dataPubblicazioneDa"] = conv(data_da)
    dati[f"{PORTLET_PREFIX}dataPubblicazioneA"] = conv(data_a)
    try:
        r = SESSION.post(url_azione, data=dati, timeout=90)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        log.info(f"    POST {nome_azione}/sse={sse}/{formato}: errore {e}")
        return None


def _righe_in_finestra(html: str, data_da: str, data_a: str) -> tuple[int, int]:
    righe = _parse_tabella(html)
    dentro = sum(1 for r in righe
                 if r.get("data_inizio") and data_da <= r["data_inizio"] <= data_a)
    return dentro, len(righe)


def imposta_filtro_ricerca(url_pagina: str, data_da: str, data_a: str) -> str | None:
    """
    Invia il form di ricerca del portlet con un intervallo di date.
    L'action nel form è quella dell'ordinamento (il JS la riscrive al submit),
    quindi la prima volta CALIBRA: prova le action di ricerca note ×
    simpleSearchEnable × formato data, accettando solo la combinazione le cui
    righe cadono davvero nell'intervallo. La combinazione buona viene
    riusata per tutte le finestre successive.

    Restituisce l'HTML della prima pagina dei risultati (può avere 0 righe se
    la finestra è realmente vuota), o None se la ricerca non è replicabile.
    """
    letto = _leggi_form_ricerca(url_pagina)
    if letto is None:
        return None
    azione, dati_base = letto
    log.debug(f"Ricerca: action del form → {azione}")

    # Combinazione già calibrata per questa pagina?
    if url_pagina in _COMBO_RICERCA:
        nome_azione, sse, formato = _COMBO_RICERCA[url_pagina]
        html = _post_filtro(azione, dati_base, nome_azione, sse, data_da, data_a, formato)
        if html is not None:
            dentro, totale = _righe_in_finestra(html, data_da, data_a)
            log.info(f"Ricerca {data_da}→{data_a} [{nome_azione}/sse={sse}/{formato}]: "
                     f"{dentro} righe in finestra ({totale} totali, prima pagina)")
            return html
        return None

    # Calibrazione: prova le combinazioni finché una restituisce righe in finestra
    log.info(f"Ricerca: calibrazione su {url_pagina[:80]} "
             f"(finestra test {data_da} → {data_a})")
    for nome_azione in AZIONI_RICERCA:
        for sse in ("true", "false"):
            for formato in ("ISO", "it"):
                html = _post_filtro(azione, dati_base, nome_azione, sse,
                                    data_da, data_a, formato)
                if html is None:
                    continue
                dentro, totale = _righe_in_finestra(html, data_da, data_a)
                log.info(f"    {nome_azione}/sse={sse}/{formato}: "
                         f"{dentro} in finestra su {totale} righe")
                if dentro > 0 and (totale == 0 or dentro / totale >= 0.5):
                    log.info(f"Ricerca CALIBRATA: azione={nome_azione}, "
                             f"simpleSearch={sse}, formato={formato}")
                    _COMBO_RICERCA[url_pagina] = (nome_azione, sse, formato)
                    return html
                time.sleep(0.5)
    log.warning("Ricerca: nessuna combinazione ha prodotto righe nella finestra "
                "di calibrazione. La ricerca via HTTP non è replicabile così: "
                "servono i parametri esatti dal browser (network tab / estensione).")
    return None


def dump_moduli_ricerca(url_pagina: str) -> None:
    """
    Diagnostica: logga i form presenti in una pagina del portale con i nomi
    dei campi e le opzioni dei menu a tendina. Serve a scoprire come
    interrogare il modulo di ricerca (action=cercaPubblicazioni), dietro cui
    potrebbe trovarsi l'archivio completo degli atti che le liste di
    default non mostrano.
    """
    try:
        html = _fetch(url_pagina)
    except requests.RequestException as e:
        log.warning(f"dump form: pagina non raggiungibile ({e})")
        return
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    log.info(f"FORM in {url_pagina[:100]}: {len(forms)} trovati")
    for i, form in enumerate(forms, 1):
        azione = form.get("action", "")[:160]
        metodo = form.get("method", "get").upper()
        log.info(f"  form {i}: {metodo} → {azione}")
        for campo in form.find_all(["input", "select", "textarea"]):
            nome = campo.get("name")
            if not nome:
                continue
            if campo.name == "select":
                opzioni = [(o.get("value", ""), o.get_text(strip=True)[:40])
                           for o in campo.find_all("option")]
                log.info(f"    select {nome}:")
                for val, testo in opzioni[:25]:
                    log.info(f"      {val!r} = {testo!r}")
                if len(opzioni) > 25:
                    log.info(f"      … altre {len(opzioni)-25} opzioni")
            else:
                log.info(f"    {campo.name} {nome} (type={campo.get('type','text')}, "
                         f"value={campo.get('value','')[:40]!r})")


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
