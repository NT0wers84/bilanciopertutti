#!/usr/bin/env python3
"""
istat_estrai.py — Chi abita a Pieve Emanuele, dai dati ISTAT.

PERCHÉ SERVE
I conti del Comune si leggono meglio sapendo chi li paga e chi usa i servizi:
quanti anziani, quanti laureati, quanti lavorano, quanti escono ogni giorno.
Qui si raccolgono, per Pieve, i comuni vicini e la Lombardia, dalla banca dati
ISTAT (esploradati.istat.it, interfaccia SDMX).

IL LIMITE DI RICHIESTE
ISTAT blocca per ore l'indirizzo che fa troppe richieste ravvicinate. Per
questo:
  · ogni tabella si chiede UNA volta sola per tutti i comuni insieme
    (chiave 015173+015189+...), non una volta per comune;
  · fra una richiesta e l'altra c'è una pausa fissa;
  · al primo segno di blocco (429, 403, connessione rifiutata o scaduta) lo
    script si ferma subito invece di riprovare, perché ogni tentativo in più
    allunga il blocco.

COME SI USA
    python3 scripts/istat_estrai.py --ispeziona   # mostra cosa c'è, non scrive
    python3 scripts/istat_estrai.py --applica     # scrive data/territorio.json

Da GitHub Actions: workflow «Conti in chiaro — Dati ISTAT».
"""

import argparse
import csv
import io
import json
import logging
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

BASE = "https://esploradati.istat.it/SDMXWS/rest"
USER_AGENT = "ContiInChiaro/1.0 (+https://github.com/NT0wers84/bilanciopertutti)"
PAUSA = 15          # secondi fra due richieste: resta sotto le 5 al minuto
TIMEOUT = 180

DESTINAZIONE = Path("data/territorio.json")

PIEVE = "015173"
# Gli stessi comuni vicini di «Come siamo messi» (fabbisogni_estrai.py)
COMUNI = {
    "015173": "Pieve Emanuele",
    "015189": "Rozzano",
    "015015": "Basiglio",
    "015115": "Lacchiarella",
    "015125": "Locate di Triulzi",
    "015159": "Opera",
}
LOMBARDIA = "ITC4"
AREE = {**COMUNI, LOMBARDIA: "Lombardia"}

# nome → (flusso per i comuni, flusso per la Lombardia, filtri fissi).
# Per popolazione e stranieri ISTAT tiene i comuni lombardi e le regioni in
# due flussi diversi; le tabelle del censimento hanno tutto nello stesso.
# I filtri riducono il download e valgono solo se il codice esiste: se ISTAT
# lo cambia, la tabella risponde «nessun dato» e --ispeziona lo mostra.
TABELLE = {
    "popolazione": ("22_289_DF_DCIS_POPRES1_6", "22_289_DF_DCIS_POPRES1_1",
                    {"DATA_TYPE": "JAN", "SEX": "9", "MARITAL_STATUS": "99"}),
    "stranieri": ("29_7_DF_DCIS_POPSTRRES1_5", "29_7_DF_DCIS_POPSTRRES1_1",
                  {"DATA_TYPE": "JAN", "SEX": "9", "AGE": "TOTAL"}),
    "istruzione": ("DF_DCSS_ISTR_LAV_PEN_2_TV_1", None,
                   {"GENDER": "T", "CITIZENSHIP": "TOTAL"}),
    "lavoro": ("DF_DCSS_ISTR_LAV_PEN_2_TV_3", None,
               {"GENDER": "T", "CITIZENSHIP": "TOTAL"}),
    "pendolari": ("DF_DCSS_ISTR_LAV_PEN_2_TV_5", None, {"CITIZENSHIP": "TOTAL"}),
    # Da qui in giù le tabelle non sono ancora state viste coi dati veri:
    # --ispeziona serve a decidere quali indicatori ricavarne.
    "famiglie": ("DF_DCSS_FAMIGLIE_TV_1", None, {}),
    "componenti_famiglia": ("DF_DCSS_FAM_POP_TV_2", None, {}),
    "occupati_settore": ("DF_DCSS_EMPLP_2_COM", None, {}),
    "occupati_posizione": ("DF_DCSS_EMPLP_1_COM", None, {}),
    "previsioni": ("165_1245_DF_DCIS_PREVCOM_4", None, {}),
}


class Bloccato(Exception):
    """ISTAT ha smesso di rispondere: si interrompe tutto, senza riprovare."""


_ultima_richiesta = 0.0


def richiesta(url: str, accept: str) -> tuple[int, bytes]:
    """Una richiesta HTTP, distanziata dalla precedente. 404 non è un errore:
    è la risposta ISTAT a «nessun dato con questi filtri»."""
    global _ultima_richiesta
    attesa = PAUSA - (time.monotonic() - _ultima_richiesta)
    if _ultima_richiesta and attesa > 0:
        time.sleep(attesa)
    _ultima_richiesta = time.monotonic()
    req = urllib.request.Request(url, headers={"Accept": accept,
                                               "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, e.read()
        if e.code in (403, 429, 503):
            raise Bloccato(f"HTTP {e.code} su {url}")
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise Bloccato(f"{type(e).__name__}: {e} su {url}")


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def struttura(flusso: str, con_etichette: bool) -> dict:
    """Dimensioni del flusso nell'ordine della chiave, versione, e (se chiesto)
    le etichette italiane dei codici. Le etichette servono solo a --ispeziona:
    costano un download più pesante, perché includono le liste dei codici."""
    rif = "descendants" if con_etichette else "datastructure"
    stato, corpo = richiesta(f"{BASE}/dataflow/IT1/{flusso}/latest?references={rif}",
                             "application/vnd.sdmx.structure+xml;version=2.1")
    if stato != 200:
        raise RuntimeError(f"struttura di {flusso} non disponibile ({stato})")
    radice = ET.fromstring(corpo)

    versione = "1.0"
    for el in radice.iter():
        if _tag(el) == "Dataflow" and el.get("id") == flusso:
            versione = el.get("version", "1.0")

    dimensioni = []
    for lista in radice.iter():
        if _tag(lista) != "DimensionList":
            continue
        for dim in lista:
            if _tag(dim) != "Dimension":
                continue
            codelist = next((r.get("id") for r in dim.iter()
                             if _tag(r) == "Ref" and r.get("class") == "Codelist"), None)
            dimensioni.append((int(dim.get("position", 0)), dim.get("id"), codelist))
    dimensioni.sort()

    etichette = defaultdict(dict)
    if con_etichette:
        for cl in radice.iter():
            if _tag(cl) != "Codelist":
                continue
            for code in cl:
                if _tag(code) != "Code":
                    continue
                nome = next((n.text for n in code if _tag(n) == "Name"
                             and n.get("{http://www.w3.org/XML/1998/namespace}lang") == "it"), "")
                etichette[cl.get("id")][code.get("id")] = nome
    return {"versione": versione,
            "dimensioni": [(d, cl) for _, d, cl in dimensioni],
            "etichette": etichette}


def dati(flusso: str, st: dict, aree: list[str], filtri: dict) -> list[dict]:
    """Le osservazioni del flusso per le aree richieste, in una sola chiamata."""
    chiave = ".".join("+".join(aree) if d == "REF_AREA" else filtri.get(d, "")
                      for d, _ in st["dimensioni"])
    url = f"{BASE}/data/IT1,{flusso},{st['versione']}/{chiave}/ALL/?detail=dataonly"
    stato, corpo = richiesta(url, "application/vnd.sdmx.data+csv;version=1.0.0")
    if stato == 404:
        log.warning(f"  {flusso}: nessun dato con la chiave {chiave}")
        return []
    righe = list(csv.DictReader(io.StringIO(corpo.decode("utf-8-sig"))))
    for r in righe:
        try:
            r["OBS_VALUE"] = float(r["OBS_VALUE"])
        except (TypeError, ValueError):
            r["OBS_VALUE"] = None
    return righe


def scarica_tutto(con_etichette: bool) -> dict:
    """{nome tabella: {"righe": [...], "struttura": {...}}}. Si ferma al primo
    blocco; una tabella senza dati resta vuota e non ferma le altre."""
    esito = {}
    for nome, (flusso_comuni, flusso_regione, filtri) in TABELLE.items():
        log.info(f"· {nome}")
        st = struttura(flusso_comuni, con_etichette)
        if flusso_regione:
            righe = dati(flusso_comuni, st, list(COMUNI), filtri)
            st_reg = struttura(flusso_regione, con_etichette)
            righe += dati(flusso_regione, st_reg, [LOMBARDIA], filtri)
        else:
            righe = dati(flusso_comuni, st, list(AREE), filtri)
        esito[nome] = {"righe": righe, "struttura": st, "flusso": flusso_comuni}
    return esito


# ── Indicatori ───────────────────────────────────────────────────────────────

def somma(righe, **uguale) -> float:
    return sum(r["OBS_VALUE"] or 0 for r in righe
               if all(r.get(k) == v for k, v in uguale.items()))


def anno_completo(righe) -> str | None:
    """L'anno più recente in cui TUTTE le aree hanno dati: confrontare Pieve
    del 2026 con un vicino del 2025 sarebbe un confronto falso."""
    per_anno = defaultdict(set)
    for r in righe:
        per_anno[r["TIME_PERIOD"]].add(r["REF_AREA"])
    completi = [a for a, aree in per_anno.items() if set(AREE) <= aree]
    return max(completi) if completi else None


def eta(codice: str) -> int | None:
    if codice == "Y_GE100":
        return 100
    if codice.startswith("Y") and codice[1:].isdigit():
        return int(codice[1:])
    return None


def pct(parte, totale, cifre=1):
    return round(100 * parte / totale, cifre) if totale else None


def ind_popolazione(righe) -> dict:
    anni = sorted({r["TIME_PERIOD"] for r in righe})
    ultimo = anno_completo(righe)
    serie = {a: {anno: somma(righe, REF_AREA=a, TIME_PERIOD=anno, AGE="TOTAL")
                 for anno in anni} for a in AREE}
    struttura_eta, piramide = {}, {}
    for a in AREE:
        r_a = [r for r in righe if r["REF_AREA"] == a and r["TIME_PERIOD"] == ultimo
               and eta(r["AGE"]) is not None]
        tot = sum(r["OBS_VALUE"] or 0 for r in r_a)
        giovani = sum(r["OBS_VALUE"] or 0 for r in r_a if eta(r["AGE"]) <= 14)
        anziani = sum(r["OBS_VALUE"] or 0 for r in r_a if eta(r["AGE"]) >= 65)
        struttura_eta[a] = {"under15_pct": pct(giovani, tot), "over65_pct": pct(anziani, tot),
                            "indice_vecchiaia": round(100 * anziani / giovani) if giovani else None}
        classi = defaultdict(float)
        for r in r_a:
            classi[min(eta(r["AGE"]) // 5 * 5, 90)] += r["OBS_VALUE"] or 0
        piramide[a] = {f"{c}-{c + 4}" if c < 90 else "90+": pct(v, tot, 2)
                       for c, v in sorted(classi.items())}
    return {"anno": ultimo, "serie": serie, "struttura_eta": struttura_eta,
            "piramide": {a: piramide[a] for a in (PIEVE, LOMBARDIA)}}


def ind_stranieri(righe, popolazione: dict) -> dict:
    anno = anno_completo(righe)
    return {"anno": anno, "valori": {
        a: pct(somma(righe, REF_AREA=a, TIME_PERIOD=anno),
               popolazione["serie"][a].get(anno)) for a in AREE}}


def ind_istruzione(righe) -> dict:
    anno = anno_completo(righe)
    valori = {}
    for a in AREE:
        r_a = [r for r in righe if r["REF_AREA"] == a and r["TIME_PERIOD"] == anno
               and r.get("AGE_NOCLASS") == "Y25-49"]
        tot = somma(r_a, EDU_ATTAIN="ALL")
        laureati = somma(r_a, EDU_ATTAIN="BL") + somma(r_a, EDU_ATTAIN="ML_RDD")
        bassi = sum(somma(r_a, EDU_ATTAIN=c) for c in ("NED", "IL", "LBNA", "PSE", "LSE"))
        valori[a] = {"laureati_25_49_pct": pct(laureati, tot),
                     "al_piu_licenza_media_25_49_pct": pct(bassi, tot)}
    return {"anno": anno, "valori": valori}


def ind_lavoro(righe) -> dict:
    anno = anno_completo(righe)
    valori = {}
    for a in AREE:
        r_a = [r for r in righe if r["REF_AREA"] == a and r["TIME_PERIOD"] == anno]
        f = lambda classi, stato: sum(somma(r_a, AGE_NOCLASS=c, CUR_ACT_STAT=stato)
                                      for c in classi)
        valori[a] = {
            "occupati_25_64_pct": pct(f(("Y25-49", "Y50-64"), "1"),
                                      f(("Y25-49", "Y50-64"), "99")),
            "disoccupazione_pct": pct(f(("Y_GE15",), "12"), f(("Y_GE15",), "22")),
        }
    return {"anno": anno, "valori": valori}


def ind_pendolari(righe) -> dict:
    anno = anno_completo(righe)
    valori = {}
    for a in AREE:
        r_a = [r for r in righe if r["REF_AREA"] == a and r["TIME_PERIOD"] == anno
               and r.get("REAS_COMMUTING") == "ALL"]
        # Il totale per sesso a volte manca: si sommano maschi e femmine.
        sessi = {r["GENDER"] for r in r_a}
        g = ["T"] if "T" in sessi else ["M", "F"]
        f = lambda dest: sum(somma(r_a, GENDER=s, LOC_DEST=dest) for s in g)
        valori[a] = {"pendolari": round(f("ALL")),
                     "fuori_comune_pct": pct(f("OMPUR"), f("ALL"))}
    return {"anno": anno, "valori": valori}


def calcola(tab: dict) -> dict:
    pop = ind_popolazione(tab["popolazione"]["righe"])
    return {
        "popolazione": pop,
        "stranieri": ind_stranieri(tab["stranieri"]["righe"], pop),
        "istruzione": ind_istruzione(tab["istruzione"]["righe"]),
        "lavoro": ind_lavoro(tab["lavoro"]["righe"]),
        "pendolari": ind_pendolari(tab["pendolari"]["righe"]),
    }


# ── Ispezione ────────────────────────────────────────────────────────────────

def descrivi(nome: str, t: dict) -> None:
    righe, st = t["righe"], t["struttura"]
    log.info(f"\n══ {nome} ({t['flusso']}, versione {st['versione']}) ══")
    log.info(f"  righe: {len(righe)}")
    if not righe:
        return
    aree = Counter(r["REF_AREA"] for r in righe)
    mancanti = [AREE[a] for a in AREE if a not in aree]
    log.info(f"  aree: {', '.join(f'{AREE.get(a, a)}={n}' for a, n in aree.items())}")
    if mancanti:
        log.info(f"  AREE MANCANTI: {', '.join(mancanti)}")
    log.info(f"  anni: {' '.join(sorted({r['TIME_PERIOD'] for r in righe}))}")
    log.info(f"  anno completo più recente: {anno_completo(righe)}")
    for dim, cl in st["dimensioni"]:
        if dim in ("FREQ", "REF_AREA"):
            continue
        codici = Counter(r.get(dim) for r in righe)
        nomi = st["etichette"].get(cl, {})
        elenco = "; ".join(f"{c}={nomi.get(c, '?')}" for c in sorted(codici, key=str)[:25])
        altro = f" (+{len(codici) - 25})" if len(codici) > 25 else ""
        log.info(f"  {dim}: {elenco}{altro}")
    pieve = [r for r in righe if r["REF_AREA"] == PIEVE][:3]
    for r in pieve:
        log.info("  esempio Pieve: " + ", ".join(
            f"{d}={r.get(d)}" for d, _ in st["dimensioni"] if d not in ("FREQ", "REF_AREA"))
            + f", {r['TIME_PERIOD']} → {r['OBS_VALUE']}")


def stampa_indicatori(ind: dict) -> None:
    log.info("\n══ Indicatori calcolati ══")
    blocchi = [("struttura età", ind["popolazione"]["anno"], ind["popolazione"]["struttura_eta"]),
               ("stranieri %", ind["stranieri"]["anno"], ind["stranieri"]["valori"]),
               ("istruzione", ind["istruzione"]["anno"], ind["istruzione"]["valori"]),
               ("lavoro", ind["lavoro"]["anno"], ind["lavoro"]["valori"]),
               ("pendolari", ind["pendolari"]["anno"], ind["pendolari"]["valori"])]
    for titolo, anno, valori in blocchi:
        log.info(f"  {titolo} ({anno})")
        for a, v in valori.items():
            log.info(f"    {AREE[a]:<18} {v}")
    log.info("  abitanti per anno")
    for a, s in ind["popolazione"]["serie"].items():
        log.info(f"    {AREE[a]:<18} " + " ".join(f"{k}:{round(v)}" for k, v in s.items()))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    modo = p.add_mutually_exclusive_group(required=True)
    modo.add_argument("--ispeziona", action="store_true",
                      help="mostra cosa c'è nelle tabelle, non scrive niente")
    modo.add_argument("--applica", action="store_true",
                      help=f"scrive {DESTINAZIONE}")
    args = p.parse_args()

    try:
        tab = scarica_tutto(con_etichette=args.ispeziona)
    except Bloccato as e:
        log.error(f"\nISTAT NON RISPONDE: {e}")
        log.error("Probabile blocco per troppe richieste. Non rilanciare subito: "
                  "ogni tentativo lo allunga. Riprova fra 24 ore.")
        return 1

    if args.ispeziona:
        for nome, t in tab.items():
            descrivi(nome, t)

    base = ("popolazione", "stranieri", "istruzione", "lavoro", "pendolari")
    vuote = [n for n in base if not tab[n]["righe"]]
    if vuote:
        log.error(f"\nTabelle di base senza dati: {', '.join(vuote)}. Non scrivo niente.")
        return 1
    try:
        ind = calcola(tab)
    except Exception as e:  # un codice cambiato da ISTAT non deve passare in silenzio
        log.error(f"\nCalcolo degli indicatori fallito: {type(e).__name__}: {e}")
        return 1
    stampa_indicatori(ind)

    if args.applica:
        ind = {"fonte": "ISTAT, esploradati.istat.it",
               "aggiornato": date.today().isoformat(),
               "aree": AREE, "pieve": PIEVE, "lombardia": LOMBARDIA, **ind}
        DESTINAZIONE.write_text(json.dumps(ind, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        log.info(f"\nScritto {DESTINAZIONE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
