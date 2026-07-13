"""
bilanci_estrai.py — Estrae i dati di bilancio di Pieve Emanuele dai CSV
regionali BDAP scaricati a mano dall'area FET di OpenBDAP (cartella /bilanci).

Input:  bilanci/previsione/<anno>_*/ e bilanci/rendiconto/<anno>_*/
        → file "*Spese Riepilogo Missioni*_LOMBARDIA.csv" (tutta la regione)
Output: data/bilanci_pieve.json + docs/data/bilanci.json (solo Pieve Emanuele)

Colonne usate:
  Previsione:  "Previsioni in CC 1 Anno" (stanziamento di competenza)
  Rendiconto:  "Previsioni Definitive di Competenza", "Impegni",
               "Totale Pagamenti"

Da rilanciare quando si aggiungono nuovi anni in /bilanci (una volta l'anno).
"""

import csv
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BILANCI_DIR = Path("bilanci")
OUT_DATA = Path("data/bilanci_pieve.json")
OUT_DOCS = Path("docs/data/bilanci.json")

ENTE = "PIEVE EMANUELE"


def numero(v) -> float:
    if v is None:
        return 0.0
    v = str(v).strip().replace('"', "")
    if not v:
        return 0.0
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return 0.0


def leggi_csv(percorso: Path) -> list[dict]:
    """Legge un CSV BDAP (; come separatore, encoding latin-1) e restituisce
    solo le righe di Pieve Emanuele."""
    righe = []
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(percorso, encoding=encoding, newline="") as f:
                lettore = csv.DictReader(f, delimiter=";")
                righe = [r for r in lettore
                         if ENTE in (r.get("Denominazione Soggetto") or "").upper()]
            break
        except UnicodeDecodeError:
            continue
    return righe


def trova_file_missioni(cartella: Path) -> list[Path]:
    """Trova i CSV 'Spese Riepilogo Missioni' (esclusa la variante Voce di
    Riepilogo, che è un'aggregazione diversa)."""
    out = []
    for p in cartella.rglob("*.csv"):
        nome = p.name.lower()
        if "riepilogo missioni" in nome and "voce di riepilogo" not in nome:
            out.append(p)
    return sorted(out)


def main():
    record: dict[tuple, dict] = {}  # (anno, codice_missione) → dati

    # ── Previsione ────────────────────────────────────────────────────────
    for percorso in trova_file_missioni(BILANCI_DIR / "previsione"):
        righe = leggi_csv(percorso)
        log.info(f"{percorso.name}: {len(righe)} righe Pieve Emanuele")
        for r in righe:
            anno = int(r["Esercizio Finanziario"])
            codice = (r.get("Codice Missione Arconet") or r.get("Codice Missione") or "").strip()
            descr = (r.get("Descrizione Missione Arconet") or r.get("Descrizione Missione") or "").strip()
            if not codice:
                continue
            chiave = (anno, codice)
            rec = record.setdefault(chiave, {
                "anno": anno, "codice_missione": codice, "missione": descr,
                "previsione_competenza": 0.0, "previsione_definitiva": 0.0,
                "impegni": 0.0, "pagamenti": 0.0,
            })
            rec["missione"] = rec["missione"] or descr
            rec["previsione_competenza"] += numero(r.get("Previsioni in CC 1 Anno"))

    # ── Rendiconto ────────────────────────────────────────────────────────
    for percorso in trova_file_missioni(BILANCI_DIR / "rendiconto"):
        righe = leggi_csv(percorso)
        log.info(f"{percorso.name}: {len(righe)} righe Pieve Emanuele")
        for r in righe:
            anno = int(r["Esercizio Finanziario"])
            codice = (r.get("Codice Missione") or r.get("Codice Missione Arconet") or "").strip()
            descr = (r.get("Descrizione Missione") or r.get("Descrizione Missione Arconet") or "").strip()
            if not codice:
                continue
            chiave = (anno, codice)
            rec = record.setdefault(chiave, {
                "anno": anno, "codice_missione": codice, "missione": descr,
                "previsione_competenza": 0.0, "previsione_definitiva": 0.0,
                "impegni": 0.0, "pagamenti": 0.0,
            })
            rec["missione"] = rec["missione"] or descr
            rec["previsione_definitiva"] += numero(r.get("Previsioni Definitive di Competenza"))
            rec["impegni"] += numero(r.get("Impegni"))
            rec["pagamenti"] += numero(r.get("Totale Pagamenti"))

    dati = sorted(record.values(), key=lambda x: (x["anno"], x["codice_missione"]))
    for d in dati:
        for k in ("previsione_competenza", "previsione_definitiva", "impegni", "pagamenti"):
            d[k] = round(d[k], 2)

    anni = sorted({d["anno"] for d in dati})
    log.info(f"Record totali: {len(dati)} — anni coperti: {anni}")
    for anno in anni:
        del_anno = [d for d in dati if d["anno"] == anno]
        prev = sum(d["previsione_competenza"] for d in del_anno)
        imp = sum(d["impegni"] for d in del_anno)
        log.info(f"  {anno}: {len(del_anno)} missioni, "
                 f"previsione € {prev:,.0f}, impegni € {imp:,.0f}")

    OUT_DATA.parent.mkdir(exist_ok=True)
    OUT_DATA.write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_DOCS.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOCS.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    log.info(f"Scritti {OUT_DATA} e {OUT_DOCS}")


if __name__ == "__main__":
    main()
