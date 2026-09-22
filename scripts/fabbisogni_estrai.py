"""
fabbisogni_estrai.py — Fabbisogni standard per SINGOLA FUNZIONE.

I dati di OpenCivitas disaggregati per funzione fondamentale (istruzione,
sociale, asili nido, viabilità, polizia locale, rifiuti, funzioni generali)
dicono DOVE si concentra lo scarto fra spesa storica e fabbisogno: il dato
aggregato dice solo che lo scarto esiste.

Non sono scaricabili da qui (il portale è renderizzato in JavaScript e gli
archivi ZIP non sono recuperabili dal programma): vanno scaricati a mano da
https://www.opencivitas.it/it/open-data e scompattati nella cartella
`fabbisogni/` del repository, come già si fa per i CSV BDAP in `bilanci/`.

Lo script è ADATTIVO: non conosce in anticipo i nomi esatti delle colonne,
li riconosce dai contenuti dell'intestazione e dichiara cosa ha trovato.

Uso:
  python3 scripts/fabbisogni_estrai.py --ispeziona   # mostra struttura dei file
  python3 scripts/fabbisogni_estrai.py               # estrae e aggiorna i dati
"""

import csv
import glob
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

CARTELLA = Path("fabbisogni")
CONFRONTI = Path("data/confronti.json")

# Comuni da estrarre: Pieve Emanuele e l'ambito Visconteo Sud Milano
COMUNI = {
    "015173": "Pieve Emanuele",
    "015189": "Rozzano",
    "015015": "Basiglio",
    "015115": "Lacchiarella",
    "015125": "Locate di Triulzi",
    "015159": "Opera",
}

# Sigla nel nome del file → etichetta leggibile della funzione
FUNZIONI = {
    "TOT": "Tutti i servizi",
    "RIFIUTI": "Rifiuti",
    "ISTR": "Istruzione",
    "ISTRUZIONE": "Istruzione",
    "SOC": "Servizi sociali",
    "SOCIALI": "Servizi sociali",
    "NIDI": "Asili nido",
    "ASILI": "Asili nido",
    "VIAB": "Viabilità e territorio",
    "VIABILITA": "Viabilità e territorio",
    "TERR": "Viabilità e territorio",
    "POL": "Polizia locale",
    "POLIZIA": "Polizia locale",
    "GEN": "Funzioni generali",
    "GENERALI": "Funzioni generali",
    "AMM": "Funzioni generali",
}

# Riconoscimento colonne: (chiave interna, parole che devono comparire)
COLONNE = [
    ("codice", ["codice", "istat"]),
    ("codice", ["cod", "ente"]),
    ("nome", ["denominazione"]),
    ("nome", ["comune"]),
    ("storica", ["spesa", "storica"]),
    ("standard", ["fabbisogno"]),
    ("standard", ["spesa", "standard"]),
    ("servizi", ["servizi"]),
    ("popolazione", ["popolazione"]),
]


def funzione_da_nome(percorso: Path) -> str:
    """Deduce la funzione dalla sigla nel nome del file (es. FC80ISTR)."""
    nome = percorso.stem.upper()
    m = re.search(r"FC\d{2}([A-Z]+)", nome)
    if m and m.group(1) in FUNZIONI:
        return FUNZIONI[m.group(1)]
    for sigla, etichetta in FUNZIONI.items():
        if sigla in nome:
            return etichetta
    return percorso.stem


def apri(percorso: Path):
    """I rilasci OpenCivitas usano cp1252 o UTF-8, con ; come separatore."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(percorso, encoding=enc, newline="") as f:
                campione = f.read(4096)
            sep = ";" if campione.count(";") >= campione.count(",") else ","
            return open(percorso, encoding=enc, newline=""), sep
        except UnicodeDecodeError:
            continue
    return None, ";"


def mappa_colonne(intestazione: list[str]) -> dict:
    """Associa le colonne del file ai campi che ci servono."""
    trovate = {}
    for i, col in enumerate(intestazione):
        c = col.lower()
        for chiave, parole in COLONNE:
            if chiave in trovate:
                continue
            if all(p in c for p in parole):
                trovate[chiave] = i
    return trovate


def numero(valore: str) -> float | None:
    if valore is None:
        return None
    v = str(valore).strip().replace("€", "").replace(" ", "")
    if not v:
        return None
    # formato italiano (1.234,56) o anglosassone (1234.56)
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def ispeziona() -> int:
    file_csv = sorted(CARTELLA.rglob("*.csv"))
    if not file_csv:
        log.error(f"Nessun CSV in {CARTELLA}/. Scarica i dataset per funzione da "
                  f"https://www.opencivitas.it/it/open-data e scompattali lì.")
        return 1
    log.info(f"{len(file_csv)} file trovati in {CARTELLA}/\n")
    for p in file_csv:
        fh, sep = apri(p)
        if fh is None:
            log.warning(f"{p.name}: codifica non riconosciuta")
            continue
        with fh:
            r = csv.reader(fh, delimiter=sep)
            intestazione = next(r, [])
            prima = next(r, [])
        trovate = mappa_colonne(intestazione)
        log.info(f"── {p.name}")
        log.info(f"   funzione dedotta: {funzione_da_nome(p)}")
        log.info(f"   colonne: {len(intestazione)}, separatore '{sep}'")
        log.info(f"   riconosciute: { {k: intestazione[i] for k, i in trovate.items()} }")
        mancanti = {"codice", "storica", "standard"} - set(trovate)
        if mancanti:
            log.warning(f"   ATTENZIONE: non riconosciute {mancanti}")
            log.info(f"   intestazione completa: {intestazione[:25]}")
        log.info("")
    return 0


def estrai() -> int:
    file_csv = sorted(CARTELLA.rglob("*.csv"))
    if not file_csv:
        log.error(f"Nessun CSV in {CARTELLA}/. Vedi --ispeziona.")
        return 1

    per_funzione: dict[str, list[dict]] = {}
    for p in file_csv:
        funzione = funzione_da_nome(p)
        fh, sep = apri(p)
        if fh is None:
            continue
        with fh:
            r = csv.reader(fh, delimiter=sep)
            intestazione = next(r, [])
            col = mappa_colonne(intestazione)
            if not {"codice", "storica", "standard"} <= set(col):
                log.warning(f"{p.name}: colonne chiave non riconosciute, salto "
                            f"(usa --ispeziona per vedere l'intestazione)")
                continue
            righe = []
            for row in r:
                if len(row) <= max(col.values()):
                    continue
                codice = (row[col["codice"]] or "").strip().zfill(6)
                if codice not in COMUNI:
                    continue
                storica = numero(row[col["storica"]])
                standard = numero(row[col["standard"]])
                if storica is None or standard is None or standard == 0:
                    continue
                righe.append({
                    "nome": COMUNI[codice],
                    "istat": codice,
                    "storica": round(storica, 2),
                    "standard": round(standard, 2),
                    "scarto_euro": round(storica - standard, 2),
                    "scarto_pct": round((storica - standard) / standard * 100, 2),
                    "servizi_pct": numero(row[col["servizi"]]) if "servizi" in col else None,
                    "evidenzia": codice == "015173",
                })
        if righe:
            per_funzione[funzione] = sorted(righe, key=lambda x: -x["scarto_pct"])
            pieve = next((x for x in righe if x["istat"] == "015173"), None)
            if pieve:
                log.info(f"{funzione:26} Pieve: spesa {pieve['storica']:>12,.0f} € · "
                         f"fabbisogno {pieve['standard']:>12,.0f} € · "
                         f"scarto {pieve['scarto_pct']:+7.1f}% "
                         f"({pieve['scarto_euro']:+,.0f} €)")

    if not per_funzione:
        log.error("Nessun dato estratto.")
        return 1

    # Quanto ogni funzione contribuisce allo scarto complessivo
    totale = per_funzione.get("Tutti i servizi", [])
    pieve_tot = next((x for x in totale if x["istat"] == "015173"), None)
    if pieve_tot:
        log.info("\nCONTRIBUTO DI OGNI FUNZIONE ALLO SCARTO TOTALE")
        for funzione, righe in sorted(per_funzione.items(),
                                      key=lambda kv: -(next((x["scarto_euro"] for x in kv[1]
                                                             if x["istat"] == "015173"), 0))):
            if funzione == "Tutti i servizi":
                continue
            p = next((x for x in righe if x["istat"] == "015173"), None)
            if not p:
                continue
            quota = p["scarto_euro"] / pieve_tot["scarto_euro"] * 100 if pieve_tot["scarto_euro"] else 0
            log.info(f"  {funzione:26} {p['scarto_euro']:>+12,.0f} €  ({quota:5.1f}% dello scarto)")

    dati = json.loads(CONFRONTI.read_text(encoding="utf-8")) if CONFRONTI.exists() else {}
    dati["funzioni_2022"] = {
        "fonte": "OpenCivitas · Sogei/SOSE — indicatori per funzione fondamentale, 2022",
        "licenza": "CC BY 4.0",
        "url": "https://www.opencivitas.it/it/open-data",
        "avvertenza": "Lo scarto fra spesa storica e fabbisogno standard non è una misura "
                      "di spreco. Le funzioni non sono sommabili fra loro né con il totale: "
                      "ogni rilevazione ha il proprio perimetro.",
        "funzioni": per_funzione,
    }
    CONFRONTI.write_text(json.dumps(dati, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"\nAggiornato {CONFRONTI} con {len(per_funzione)} funzioni.")
    log.info("Ora rilancia: python3 scripts/genera_sito.py")
    return 0


if __name__ == "__main__":
    sys.exit(ispeziona() if "--ispeziona" in sys.argv else estrai())
