"""
irpef_estrai.py — Redditi dichiarati per fascia, dai dati IRPEF comunali del MEF.

Lo usa istat_estrai.py: i redditi finiscono nello stesso data/territorio.json
e si aggiornano con lo stesso workflow annuale.

LA FONTE
Il Dipartimento delle Finanze pubblica ogni anno un file con tutti i comuni
italiani: numero di contribuenti, redditi per tipo, redditi per fascia.
L'anno d'imposta N esce nella primavera dell'anno N+2. Il file si trova
leggendo la pagina dell'anno di pubblicazione, così un cambio di percorso sul
sito del MEF non rompe niente; se la pagina non lo elenca si prova l'indirizzo
noto.

Il tracciato non si fissa per posizione: le colonne delle fasce si
riconoscono dal nome («Reddito complessivo da 10000 a 15000 euro - Frequenza»),
e --ispeziona mostra cosa è stato riconosciuto.
"""

import csv
import io
import logging
import re
import urllib.request
import zipfile
from datetime import date

log = logging.getLogger(__name__)

PAGINA = "https://www1.finanze.gov.it/finanze/analisi_stat/public/index.php?tree={anno}"
BASE_FILE = "https://www1.finanze.gov.it/finanze/analisi_stat/public/"
NOTO = (BASE_FILE + "v_4_0_0/contenuti/"
        "Redditi_e_principali_variabili_IRPEF_su_base_comunale_CSV_{anno}.zip")
RE_LINK = re.compile(
    r"""["']([^"']*Redditi_e_principali_variabili_IRPEF_su_base_comunale_CSV_(\d{4})\.zip)["']""")

# Le fasce del MEF, con l'etichetta da mostrare
FASCE = [("zero", "zero o meno"), ("0-10000", "fino a 10 mila"),
         ("10000-15000", "10–15 mila"), ("15000-26000", "15–26 mila"),
         ("26000-55000", "26–55 mila"), ("55000-75000", "55–75 mila"),
         ("75000-120000", "75–120 mila"), ("oltre-120000", "oltre 120 mila")]
RE_FASCIA = re.compile(
    r"reddito complessivo\s+(?:da\s+(\d+)\s+a\s+(\d+)|oltre\s+(\d+)|"
    r"(minore o uguale a zero))", re.IGNORECASE)


def _scarica(url: str, user_agent: str, timeout: int) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001 — qui ogni errore vuol dire «non c'è»
        log.info(f"  {url}: {type(e).__name__} {e}")
        return None


def trova_file(user_agent: str, timeout: int) -> tuple[str, int] | None:
    """(url, anno d'imposta) del file più recente. Si parte dalla pagina
    dell'anno in corso e si torna indietro: il primo elenco che contiene il
    file dà l'anno più recente pubblicato."""
    oggi = date.today().year
    for anno_pagina in (oggi, oggi - 1, oggi - 2):
        html = _scarica(PAGINA.format(anno=anno_pagina), user_agent, timeout)
        if not html:
            continue
        trovati = RE_LINK.findall(html.decode("utf-8", "ignore"))
        if trovati:
            percorso, anno = max(trovati, key=lambda t: t[1])
            url = percorso if percorso.startswith("http") else BASE_FILE + percorso.lstrip("./")
            return url, int(anno)
    # Ripiego: l'indirizzo noto, per gli anni d'imposta plausibili
    for anno in (oggi - 2, oggi - 3):
        url = NOTO.format(anno=anno)
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return url, anno
        except Exception:  # noqa: BLE001
            continue
    return None


def _numero(v: str) -> float | None:
    v = (v or "").strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def mappa_colonne(intestazione: list[str]) -> dict:
    """Riconosce le colonne dal nome. Restituisce le posizioni utili."""
    m = {"fasce": {}}
    for i, nome in enumerate(intestazione):
        basso = nome.lower().strip()
        if "codice istat comune" in basso:
            m["comune"] = i
        elif basso.startswith("regione") and "codice" not in basso:
            m["regione"] = i
        elif basso.startswith("anno"):
            m["anno"] = i
        elif basso.startswith("numero contribuenti"):
            m["contribuenti"] = i
        f = RE_FASCIA.search(basso)
        if not f:
            continue
        if f.group(4):
            chiave = "zero"
        elif f.group(3):
            chiave = f"oltre-{f.group(3)}"
        else:
            chiave = f"{f.group(1)}-{f.group(2)}"
        tipo = "freq" if "frequenza" in basso else "ammontare" if "ammontare" in basso else None
        if tipo:
            m["fasce"].setdefault(chiave, {})[tipo] = i
    return m


def leggi(contenuto_zip: bytes) -> tuple[list[str], list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(contenuto_zip)) as z:
        nome = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        grezzo = z.read(nome)
    for codifica in ("utf-8-sig", "latin-1"):
        try:
            testo = grezzo.decode(codifica)
            break
        except UnicodeDecodeError:
            continue
    righe = list(csv.reader(io.StringIO(testo), delimiter=";"))
    return righe[0], righe[1:]


def calcola(intestazione, righe, comuni: dict, lombardia: str) -> dict:
    """Fasce in percentuale dei contribuenti con reddito complessivo, e
    reddito medio, per i comuni cercati e per la Lombardia (somma dei suoi
    comuni). Le celle oscurate dal MEF per tutela statistica restano fuori:
    per un comune medio non ce ne sono, per la regione pesano pochissimo."""
    m = mappa_colonne(intestazione)
    mancano = [c for c, _ in FASCE if "freq" not in m["fasce"].get(c, {})]
    if "comune" not in m or mancano:
        raise ValueError(f"colonne non riconosciute: comune={'comune' in m}, "
                         f"fasce mancanti={mancano}")
    somme = {}
    for r in righe:
        if len(r) <= m["comune"]:
            continue
        codice = r[m["comune"]].strip().zfill(6)
        aree = []
        if codice in comuni:
            aree.append(codice)
        if "regione" in m and r[m["regione"]].strip().lower() == "lombardia":
            aree.append(lombardia)
        for a in aree:
            s = somme.setdefault(a, {c: [0.0, 0.0] for c, _ in FASCE})
            for c, _ in FASCE:
                col = m["fasce"][c]
                s[c][0] += _numero(r[col["freq"]]) or 0
                if "ammontare" in col:
                    s[c][1] += _numero(r[col["ammontare"]]) or 0
    anno = None
    if "anno" in m and righe:
        anno = int(_numero(righe[0][m["anno"]]) or 0) or None
    valori = {}
    for a, s in somme.items():
        n = sum(v[0] for v in s.values())
        valori[a] = {
            "contribuenti_con_reddito": round(n),
            "reddito_medio": round(sum(v[1] for v in s.values()) / n) if n else None,
            "fasce_pct": {etichetta: round(100 * s[c][0] / n, 1) if n else None
                          for c, etichetta in FASCE},
        }
    return {"anno_imposta": anno, "valori": valori}


def scarica_redditi(comuni: dict, lombardia: str, user_agent: str, timeout: int,
                    ispeziona: bool = False) -> dict | None:
    """Scarica e calcola. None se il file non si trova o non si legge: i dati
    ISTAT non devono cadere per colpa del MEF, e viceversa."""
    log.info("· redditi IRPEF (MEF)")
    trovato = trova_file(user_agent, timeout)
    if not trovato:
        log.error("  file IRPEF comunale non trovato sul sito del MEF")
        return None
    url, anno = trovato
    log.info(f"  file: {url} (anno d'imposta {anno})")
    contenuto = _scarica(url, user_agent, timeout)
    if not contenuto:
        return None
    try:
        intestazione, righe = leggi(contenuto)
        if ispeziona:
            m = mappa_colonne(intestazione)
            log.info(f"\n══ redditi IRPEF ══\n  righe: {len(righe)}  colonne: {len(intestazione)}")
            log.info(f"  riconosciute: comune={m.get('comune')} regione={m.get('regione')} "
                     f"anno={m.get('anno')} contribuenti={m.get('contribuenti')}")
            for c, _ in FASCE:
                log.info(f"  fascia {c}: {m['fasce'].get(c)}")
            log.info("  intestazione: " + " | ".join(intestazione[:14]) + " | …")
        risultato = calcola(intestazione, righe, comuni, lombardia)
        risultato["fonte"] = url
        return risultato
    except Exception as e:  # noqa: BLE001
        log.error(f"  file IRPEF illeggibile: {type(e).__name__}: {e}")
        return None
