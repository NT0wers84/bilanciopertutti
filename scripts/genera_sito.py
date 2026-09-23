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
    "e_rimodulazione", "beneficiario_generico", "atti_gemelli",
    "importo_incerto", "regola_importo",
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


def _quota_annua(spesa: dict) -> float:
    """Quanto pesa un atto su un singolo anno.

    Un affidamento pluriennale non è spesa dell'anno: sommarlo alle liquidazioni
    di singole fatture produce un totale che non significa niente (nell'archivio
    un solo contratto quindicennale vale l'80% della somma lorda). Si usa la
    quota del primo anno quando l'atto la dichiara, altrimenti la media annua.
    Stessa regola applicata dal sito in docs/index.html: se cambia una, va
    cambiata anche l'altra.
    """
    importo = spesa.get("importo_euro")
    if not importo:
        return 0.0
    durata = spesa.get("durata_anni")
    if spesa.get("importo_e_pluriennale") and durata:
        primo = spesa.get("importo_primo_anno")
        return float(primo) if primo is not None else importo / durata
    return float(importo)


def main():
    spese = []
    if SPESE_JSON.exists():
        spese = json.loads(SPESE_JSON.read_text(encoding="utf-8"))

    DOCS_DATA.mkdir(parents=True, exist_ok=True)

    ridotte = [{k: s.get(k) for k in CAMPI_SITO} for s in spese]
    _scrivi(DOCS_DATA / "spese.json",
            json.dumps(ridotte, ensure_ascii=False, separators=(",", ":")))

    anni = sorted({s.get("anno") for s in spese if s.get("anno")}, reverse=True)
    lordo = round(sum(s.get("importo_euro") or 0 for s in spese), 2)
    annuo = round(sum(_quota_annua(s) for s in spese), 2)
    meta = {
        "aggiornato": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "n_spese": len(spese),
        "anni": anni,
        # Il totale lordo somma contratti pluriennali e liquidazioni di singole
        # fatture: un affidamento di quindici anni lo domina da solo. È il dato
        # grezzo, tenuto per compatibilità; per capire quanto pesa un anno serve
        # totale_annuo, che è anche quello mostrato dal sito.
        "totale_euro": lordo,
        "totale_annuo_euro": annuo,
        "n_pluriennali": sum(1 for s in spese
                             if s.get("importo_e_pluriennale") and s.get("durata_anni")),
    }
    _scrivi(DOCS_DATA / "meta.json", json.dumps(meta, ensure_ascii=False))

    # Dati di confronto (fabbisogni standard, IRPEF, benchmark lombardo):
    # raccolti a mano dal portale dovevannoinostrisoldi e dai CSV BDAP,
    # non aggiornabili dal workflow. Qui vengono solo ricopiati nel sito.
    confronti = Path("data/confronti.json")
    if confronti.exists():
        _scrivi(DOCS_DATA / "confronti.json", confronti.read_text(encoding="utf-8"))
        log.info("Dati di confronto copiati nel sito")
    log.info(f"Sito aggiornato: {len(spese)} spese · su base annua "
             f"€ {meta['totale_annuo_euro']:,.2f} · valore lordo degli atti "
             f"€ {meta['totale_euro']:,.2f} ({meta['n_pluriennali']} pluriennali)")


if __name__ == "__main__":
    main()
