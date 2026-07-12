"""
scraper.py — OpenSpese Pieve Emanuele: aggiornamento giornaliero.

Flusso:
  1. Scarica l'albo pretorio corrente (papca-ap) e filtra le spese
     (DETERMINAZIONE CONTABILE, LIQUIDAZIONE)
  2. Per ogni atto nuovo: dettaglio → PDF → testo → estrazione Groq
  3. Aggiorna data/spese.json e scrive data/nuove_spese.json

Eseguito da GitHub Actions (workflow aggiornamento.yml).
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime

import portale
from estrattore import estrai_dati

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
SPESE_JSON = DATA_DIR / "spese.json"
NUOVE_JSON = DATA_DIR / "nuove_spese.json"


def carica_archivio() -> list[dict]:
    if SPESE_JSON.exists():
        try:
            return json.loads(SPESE_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("spese.json corrotto: riparto da archivio vuoto (il file resta su git)")
    return []


def chiavi_note(archivio: list[dict]) -> set[tuple]:
    return {(s.get("numero_raw", ""), s.get("oggetto", "")) for s in archivio}


def elabora_spesa(atto: dict) -> dict:
    """Atto grezzo dal portale → record spesa completo."""
    testo = portale.estrai_testo_atto(atto)
    dati = estrai_dati(testo, atto.get("oggetto", ""), atto.get("tipo", ""))

    return {
        "id": portale.genera_id(atto),
        "numero_raw": atto.get("numero_raw", ""),
        "numero": portale.estrai_numero(atto.get("numero_raw", "")),
        "anno": portale.estrai_anno(atto),
        "tipo_atto": dati["tipo_atto"],
        "data_pubblicazione": atto.get("data_inizio"),
        "oggetto": atto.get("oggetto", ""),
        "beneficiario": dati["beneficiario"],
        "importo_euro": dati["importo_euro"],
        "cig": dati["cig"],
        "categoria": dati["categoria"],
        "missione_bdap": dati["missione_bdap"],
        "capitolo_bilancio": dati["capitolo_bilancio"],
        "descrizione_sintetica": dati["descrizione_sintetica"],
        "url_atto": portale.url_display_stabile(atto.get("url_dettaglio", "")),
        "estrazione": dati["estrazione"],
        "caratteri_testo": len(testo),
        "fonte": atto.get("fonte", "albo"),
        "data_elaborazione": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def salva(archivio: list[dict], nuove: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    archivio_ordinato = sorted(
        archivio,
        key=lambda s: (s.get("data_pubblicazione") or "", s.get("numero_raw") or ""),
        reverse=True,
    )
    SPESE_JSON.write_text(
        json.dumps(archivio_ordinato, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    NUOVE_JSON.write_text(
        json.dumps(nuove, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log.info(f"Archivio: {len(archivio)} spese totali — nuove oggi: {len(nuove)}")


def main():
    log.info("=" * 60)
    log.info("OPENSPESE — Comune di Pieve Emanuele — run giornaliero")
    log.info("=" * 60)

    portale.init_sessione()

    archivio = carica_archivio()
    note = chiavi_note(archivio)

    # 1. Albo corrente
    righe = portale.scrape_griglia(portale.ALBO_URL, atti_noti=note)

    # 2. Solo spese, solo nuove, dedup interno (il portale pubblica doppioni)
    viste: set[tuple] = set()
    nuove_righe = []
    for r in righe:
        chiave = (r["numero_raw"], r["oggetto"])
        if not portale.e_spesa(r["tipo"]):
            continue
        if chiave in note or chiave in viste:
            continue
        viste.add(chiave)
        nuove_righe.append(r)

    log.info(f"Spese nuove da elaborare: {len(nuove_righe)}")
    if not nuove_righe:
        salva(archivio, [])
        return

    # 3. Elaborazione completa
    nuove_spese = []
    for i, atto in enumerate(nuove_righe, 1):
        log.info(f"[{i}/{len(nuove_righe)}] {atto['tipo']} {atto['numero_raw']} — {atto['oggetto'][:60]}")
        try:
            spesa = elabora_spesa(atto)
            nuove_spese.append(spesa)
            log.info(f"  → {spesa['beneficiario'] or '?'} | "
                     f"{spesa['importo_euro'] if spesa['importo_euro'] is not None else '?'} € | "
                     f"{spesa['categoria']}")
        except Exception as e:
            log.error(f"  Elaborazione fallita, salto l'atto: {e}")
        time.sleep(1)

    salva(nuove_spese + archivio, nuove_spese)
    log.info("Fine run giornaliero.")


if __name__ == "__main__":
    main()
