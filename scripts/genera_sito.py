"""
genera_sito.py — Prepara i dati per il sito statico in docs/.

Il sito (docs/index.html) è un'app statica che legge docs/data/spese.json
e docs/data/meta.json via fetch. Questo script:
  1. copia data/spese.json in docs/data/spese.json (versione compatta)
  2. genera docs/data/meta.json con timestamp e contatori

Eseguito dal workflow dopo scraper.py / backfill.py.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SPESE_JSON = Path("data/spese.json")
DOCS_DATA = Path("docs/data")

CAMPI_SITO = [
    "id", "numero_raw", "anno", "tipo_atto", "data_pubblicazione", "oggetto",
    "beneficiario", "n_beneficiari", "beneficiari_dettaglio",
    "importo_euro", "importo_e_pluriennale", "durata_anni", "importo_primo_anno",
    "iva_inclusa", "cig", "categoria", "capitolo_bilancio",
    "descrizione_sintetica", "url_atto", "estrazione", "testo_disponibile",
    "e_rimodulazione",
]


def _scrivi(percorso: Path, contenuto: str) -> None:
    """
    Scrittura via file temporaneo + rename atomico: evita di lasciare file
    a metà se il processo muore e aggira i lock del filesystem quando la
    cartella è condivisa con altri programmi.
    """
    tmp = percorso.with_name(f".{percorso.name}.tmp")
    tmp.write_text(contenuto, encoding="utf-8")
    os.replace(tmp, percorso)


def main():
    spese = []
    if SPESE_JSON.exists():
        spese = json.loads(SPESE_JSON.read_text(encoding="utf-8"))

    DOCS_DATA.mkdir(parents=True, exist_ok=True)

    ridotte = [{k: s.get(k) for k in CAMPI_SITO} for s in spese]
    _scrivi(DOCS_DATA / "spese.json",
            json.dumps(ridotte, ensure_ascii=False, separators=(",", ":")))

    anni = sorted({s.get("anno") for s in spese if s.get("anno")}, reverse=True)
    meta = {
        "aggiornato": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "n_spese": len(spese),
        "anni": anni,
        "totale_euro": round(sum(s.get("importo_euro") or 0 for s in spese), 2),
    }
    _scrivi(DOCS_DATA / "meta.json", json.dumps(meta, ensure_ascii=False))
    log.info(f"Sito aggiornato: {len(spese)} spese, totale € {meta['totale_euro']:,.2f}")


if __name__ == "__main__":
    main()
