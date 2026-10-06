#!/usr/bin/env python3
"""
ripristina_azzerati.py — Rimette gli importi che il backfill ha azzerato per sbaglio.

COSA È SUCCESSO
Il 6 ottobre 2026 il backfill ha rielaborato gli atti estratti con le regex.
Per 38 liquidazioni (dal 2026/1320 al 2026/1407) il portale non serviva più
il testo, perché le liquidazioni restano sull'albo solo 15 giorni, e il
backfill ha azzerato l'importo come se fosse stato inventato. Non lo era:
quegli importi erano stati letti dal testo quando il testo c'era.
Il backfill ora non lo fa più; questo script ripara il danno già fatto.

COME FUNZIONA
Cerca gli atti oggi senza importo e senza testo, e per ognuno risale le
versioni precedenti di data/spese.json nella storia git, fino alla più
recente in cui l'importo c'era. Da quella rimette i campi che il backfill
aveva azzerato. Non tocca nessun altro atto e nessun altro campo.

Serve la storia git completa: in GitHub Actions il checkout va fatto con
fetch-depth: 0 (lo fa il workflow «Ricalcola importi»).

    python3 scripts/ripristina_azzerati.py            # anteprima
    python3 scripts/ripristina_azzerati.py --applica  # scrive
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SPESE = Path("data/spese.json")
VERSIONI_DA_ESAMINARE = 60
CAMPI = ("importo_euro", "importo_testuale", "beneficiari_dettaglio",
         "importo_primo_anno", "cig", "capitolo_bilancio",
         "importo_e_pluriennale", "durata_anni", "caratteri_testo",
         "testo_disponibile", "regola_importo", "importo_incerto")


def azzerato(s: dict) -> bool:
    """La firma del danno: importo vuoto, testo dichiarato non disponibile."""
    return (s.get("importo_euro") is None and s.get("testo_disponibile") is False
            and (s.get("caratteri_testo") or 0) < 300)


def versioni() -> list[str]:
    esito = subprocess.run(
        ["git", "log", f"-{VERSIONI_DA_ESAMINARE}", "--format=%H", "--", str(SPESE)],
        capture_output=True, text=True, check=True)
    return esito.stdout.split()


def leggi_versione(commit: str) -> dict | None:
    """Gli atti di una versione passata, o None se quella versione è
    illeggibile: nella storia ci sono file rovinati dai merge di settembre
    (marcatori di conflitto dentro il JSON), e vanno saltati, non letti."""
    esito = subprocess.run(["git", "show", f"{commit}:{SPESE}"],
                           capture_output=True, text=True)
    if esito.returncode != 0:
        return None
    try:
        return {s["id"]: s for s in json.loads(esito.stdout)}
    except (json.JSONDecodeError, KeyError, TypeError):
        log.info(f"  (versione {commit[:7]} illeggibile, la salto)")
        return None


def main() -> int:
    spese = json.loads(SPESE.read_text(encoding="utf-8"))
    da_fare = {s["id"]: s for s in spese if azzerato(s)}
    log.info(f"Atti senza importo e senza testo: {len(da_fare)}")
    if not da_fare:
        return 0

    commit = versioni()
    if len(commit) < 2:
        log.error("Storia git non disponibile: serve il checkout con fetch-depth: 0")
        return 1

    ripristinati = 0
    for c in commit[1:]:                       # la prima è quella attuale
        if not da_fare:
            break
        vecchia = leggi_versione(c)
        if vecchia is None:
            continue
        for id_atto in list(da_fare):
            prima = vecchia.get(id_atto)
            # Solo importi letti da un testo vero: quelli che il modello aveva
            # dedotto dal solo oggetto, senza testo, erano stati azzerati a
            # ragione e non vanno rimessi
            if (prima and prima.get("importo_euro") is not None
                    and (prima.get("caratteri_testo") or 0) >= 300):
                s = da_fare.pop(id_atto)
                log.info(f"  {s['numero_raw']:10} {prima['importo_euro']:>14,.2f} €  "
                         f"(versione {c[:7]})  {s['oggetto'][:58]}")
                for campo in CAMPI:
                    if campo in prima:
                        s[campo] = prima[campo]
                # Ripristinare vuol dire tornare allo stato di prima, errori
                # compresi: molti valori venivano dalle regex vecchie. Il
                # testo non c'è più e non si possono ricontrollare, quindi gli
                # importi implausibili tornano con l'avviso «da verificare»
                # (1,76 € per una liquidazione di fatture; 1.010.014,38 €, che è
                # il finanziamento totale di un progetto PNRR, non una liquidazione)
                v = s["importo_euro"]
                if (not (s.get("regola_importo") or "").startswith("prospetto")
                        and (v < 10 or v > 100_000)):
                    s["importo_incerto"] = True
                    log.info(f"             ↳ marcato «da verificare»")
                ripristinati += 1

    log.info(f"\nRipristinati: {ripristinati}. Senza una versione precedente con "
             f"importo: {len(da_fare)}")
    for s in da_fare.values():
        log.info(f"  {s['numero_raw']:10} {s['oggetto'][:70]}")

    if "--applica" in sys.argv:
        SPESE.write_text(json.dumps(spese, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info(f"Scritto {SPESE}")
    else:
        log.info("Anteprima: rilancia con --applica per salvare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
