"""
irpef_estrai.py — Redditi dichiarati per fascia, dai dati IRPEF comunali del MEF.

Lo usa istat_estrai.py: i redditi finiscono nello stesso data/territorio.json
e si aggiornano con lo stesso workflow annuale.

LA FONTE
Il Dipartimento delle Finanze pubblica ogni anno un file con tutti i comuni
italiani: numero di contribuenti, redditi per tipo, redditi per fascia.
L'anno d'imposta N esce nella primavera dell'anno N+2. Il file si cerca
all'indirizzo noto, dall'anno più recente all'indietro; la pagina del MEF è
solo un ripiego. Un file più vecchio di RITARDO_MASSIMO anni viene scartato.

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


# L'anno d'imposta più recente pubblicato è di solito quello di due anni fa
# (a settembre 2026 c'è il 2024). Un file più vecchio di così è un errore di
# ricerca, non un dato da pubblicare.
RITARDO_MASSIMO = 4


def _esiste(url: str, user_agent: str, timeout: int) -> bool:
    """GET senza leggere il corpo: alcuni server rifiutano HEAD."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def trova_file(user_agent: str, timeout: int) -> tuple[str, int] | None:
    """(url, anno d'imposta) del file più recente.

    Prima l'indirizzo noto, dall'anno più recente plausibile all'indietro. La
    pagina del MEF viene solo dopo: al primo giro l'avevamo messa davanti e ha
    restituito il file del 2012, perché la pagina richiesta non era quella
    attesa e il link più recente che conteneva era di dieci anni prima."""
    oggi = date.today().year
    for anno in range(oggi - 1, oggi - RITARDO_MASSIMO - 1, -1):
        url = NOTO.format(anno=anno)
        if _esiste(url, user_agent, timeout):
            return url, anno
    for anno_pagina in (oggi, oggi - 1):
        html = _scarica(PAGINA.format(anno=anno_pagina), user_agent, timeout)
        if not html:
            continue
        trovati = [(p, int(a)) for p, a in RE_LINK.findall(html.decode("utf-8", "ignore"))
                   if int(a) >= oggi - RITARDO_MASSIMO]
        if trovati:
            percorso, anno = max(trovati, key=lambda t: t[1])
            url = percorso if percorso.startswith("http") else BASE_FILE + percorso.lstrip("./")
            return url, anno
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
        # Il totale del reddito complessivo, se il file lo ha: per il reddito
        # medio vale più della somma delle fasce, che per la regione perde le
        # celle oscurate dal MEF (29.483 € contro i 30.202 € ufficiali).
        t = re.fullmatch(r"reddito complessivo\s*-\s*(frequenza|ammontare)(?: in euro)?", basso)
        if t:
            m.setdefault("totale", {})["freq" if t.group(1) == "frequenza" else "ammontare"] = i
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
    comuni).

    CELLE OSCURATE. Il MEF lascia vuote le celle che permetterebbero di
    risalire a pochi contribuenti, e ne oscura altre per non far ricavare le
    prime per differenza. Nel file del 2024 Rozzano, Opera e Locate hanno
    vuota la fascia oltre 120 mila, Basiglio quella 10–15 mila. Una cella
    vuota NON è zero: per un comune la fascia resta «non disponibile» (None),
    e il reddito medio si calcola solo dal totale ufficiale, perché dalle
    fasce verrebbe sottostimato. Per la Lombardia le celle oscurate dei
    comuni piccoli si perdono nella somma; si conta quante sono."""
    m = mappa_colonne(intestazione)
    mancano = [c for c, _ in FASCE if "freq" not in m["fasce"].get(c, {})]
    if "comune" not in m or mancano:
        raise ValueError(f"colonne non riconosciute: comune={'comune' in m}, "
                         f"fasce mancanti={mancano}")
    tot = m.get("totale", {})
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
            s = somme.setdefault(a, {"fasce": {c: [0.0, 0.0] for c, _ in FASCE},
                                     "oscurate": {c: 0 for c, _ in FASCE},
                                     "totale": [0.0, 0.0]})
            for c, _ in FASCE:
                col = m["fasce"][c]
                freq = _numero(r[col["freq"]])
                amm = _numero(r[col["ammontare"]]) if "ammontare" in col else 0
                if freq is None or amm is None:
                    s["oscurate"][c] += 1
                    continue
                s["fasce"][c][0] += freq
                s["fasce"][c][1] += amm
            if "freq" in tot and "ammontare" in tot:
                s["totale"][0] += _numero(r[tot["freq"]]) or 0
                s["totale"][1] += _numero(r[tot["ammontare"]]) or 0
    anno = None
    if "anno" in m and righe:
        anno = int(_numero(righe[0][m["anno"]]) or 0) or None
    valori = {}
    for a, s in somme.items():
        f, osc, (tot_n, tot_amm) = s["fasce"], s["oscurate"], s["totale"]
        e_comune = a != lombardia
        noti = sum(v[0] for c, v in f.items() if not (e_comune and osc[c]))
        # Denominatore: il totale ufficiale, se il file lo ha; altrimenti la
        # somma delle fasce note (e le quote sono allora sulle sole fasce note)
        n = tot_n or noti
        if tot_n:
            media = round(tot_amm / tot_n)
        elif any(osc.values()) and e_comune:
            media = None   # dalle sole fasce note verrebbe sottostimato
        else:
            media = round(sum(v[1] for v in f.values()) / noti) if noti else None
        valori[a] = {
            "contribuenti_con_reddito": round(n),
            "reddito_medio": media,
            "reddito_medio_da": "totale" if tot_n else "somma delle fasce",
            "fasce_pct": {et: (None if e_comune and osc[c]
                               else round(100 * f[c][0] / n, 1) if n else None)
                          for c, et in FASCE},
            "celle_oscurate": {et: osc[c] for c, et in FASCE if osc[c]},
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
        m = mappa_colonne(intestazione)
        # Sempre, non solo in ispezione: dice da dove viene il reddito medio
        log.info(f"  colonna del totale del reddito complessivo: "
                 f"{m.get('totale') or 'ASSENTE (media dalle fasce)'}")
        if ispeziona:
            log.info(f"\n══ redditi IRPEF ══\n  righe: {len(righe)}  colonne: {len(intestazione)}")
            log.info(f"  riconosciute: comune={m.get('comune')} regione={m.get('regione')} "
                     f"anno={m.get('anno')} contribuenti={m.get('contribuenti')}")
            for c, _ in FASCE:
                log.info(f"  fascia {c}: {m['fasce'].get(c)}")
            log.info("  tutte le colonne: " + " | ".join(intestazione))
        risultato = calcola(intestazione, righe, comuni, lombardia)
        risultato["fonte"] = url
        # Il file dichiara il proprio anno: se non torna con quello cercato o
        # è troppo vecchio, meglio nessun dato che un dato di dieci anni fa.
        dichiarato = risultato.get("anno_imposta")
        if dichiarato != anno or anno < date.today().year - RITARDO_MASSIMO:
            log.error(f"  anno del file {dichiarato}, atteso {anno}: scartato")
            return None
        return risultato
    except Exception as e:  # noqa: BLE001
        log.error(f"  file IRPEF illeggibile: {type(e).__name__}: {e}")
        return None
