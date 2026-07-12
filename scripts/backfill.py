"""
backfill.py — OpenSpese Pieve Emanuele: recupero dello storico.

Scandaglia la sezione archivio provvedimenti (papca-g) del portale,
che conserva gli atti oltre i 15 giorni di pubblicazione dell'albo.
La profondità reale dell'archivio (5 anni? 10?) viene scoperta e
loggata a runtime.

Eseguito manualmente da GitHub Actions (workflow backfill.yml).
A blocchi: --max-atti per run, con stato di avanzamento in
data/backfill_state.json, così può essere rilanciato più volte
fino a esaurire lo storico senza sforare i limiti di GitHub Actions
(6h/job) e del free tier Groq.

Uso:
  python scripts/backfill.py --max-atti 300 [--anno-min 2016] [--solo-censimento]
"""

import json
import time
import logging
import argparse
from pathlib import Path

import portale
from scraper import carica_archivio, chiavi_note, elabora_spesa, salva

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_JSON = Path("data/backfill_state.json")


def carica_stato() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"griglie_completate": [], "censimento": {}}


def salva_stato(stato: dict) -> None:
    STATE_JSON.parent.mkdir(exist_ok=True)
    STATE_JSON.write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")


def censisci(righe: list[dict]) -> dict:
    """Statistiche per anno/tipo: dice quanto è profondo l'archivio."""
    censimento: dict = {}
    for r in righe:
        anno = str(portale.estrai_anno(r))
        censimento.setdefault(anno, {"totale": 0, "spese": 0})
        censimento[anno]["totale"] += 1
        if portale.e_spesa(r["tipo"]):
            censimento[anno]["spese"] += 1
    return dict(sorted(censimento.items()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-atti", type=int, default=300,
                        help="Massimo numero di atti da elaborare in questo run")
    parser.add_argument("--anno-min", type=int, default=2016,
                        help="Non elaborare atti antecedenti a quest'anno")
    parser.add_argument("--solo-censimento", action="store_true",
                        help="Scansiona le griglie e logga la profondità "
                             "dell'archivio senza elaborare nulla")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("OPENSPESE — BACKFILL STORICO")
    log.info(f"max-atti={args.max_atti}  anno-min={args.anno_min}  "
             f"solo-censimento={args.solo_censimento}")
    log.info("=" * 60)

    portale.init_sessione()
    stato = carica_stato()

    # 1. Scopri le griglie dell'archivio storico
    griglie = portale.scopri_griglie_storico()
    # L'albo corrente è comunque una sorgente (utile al primo avvio)
    griglie.append(portale.ALBO_URL)

    archivio = carica_archivio()
    note = chiavi_note(archivio)

    elaborate = 0
    for griglia in griglie:
        if elaborate >= args.max_atti:
            break
        if griglia in stato["griglie_completate"]:
            log.info(f"Griglia già completata, salto: {griglia[:80]}")
            continue

        log.info(f"--- Scansione griglia: {griglia[:100]}")
        righe = portale.scrape_griglia(griglia, stop_se_tutti_noti=False)

        # 2. Censimento: quanto è profondo l'archivio?
        censimento = censisci(righe)
        stato["censimento"][griglia] = censimento
        log.info("CENSIMENTO ARCHIVIO (anno: totale atti / spese):")
        for anno, c in censimento.items():
            log.info(f"  {anno}: {c['totale']} atti, di cui {c['spese']} spese")
        if args.solo_censimento:
            salva_stato(stato)
            continue

        # 3. Elabora le spese non ancora in archivio (dalle più recenti)
        candidate = []
        viste: set[tuple] = set()
        for r in righe:
            chiave = (r["numero_raw"], r["oggetto"])
            if not portale.e_spesa(r["tipo"]):
                continue
            if portale.estrai_anno(r) < args.anno_min:
                continue
            if chiave in note or chiave in viste:
                continue
            viste.add(chiave)
            r["fonte"] = "storico"
            candidate.append(r)

        candidate.sort(key=lambda r: r.get("data_inizio") or "", reverse=True)
        log.info(f"Spese da elaborare in questa griglia: {len(candidate)}")

        nuove = []
        griglia_esaurita = True
        for atto in candidate:
            if elaborate >= args.max_atti:
                griglia_esaurita = False
                log.info(f"Raggiunto il limite di {args.max_atti} atti per questo run. "
                         f"Rilancia il workflow per continuare.")
                break
            elaborate += 1
            log.info(f"[{elaborate}/{args.max_atti}] {atto['tipo']} "
                     f"{atto['numero_raw']} — {atto['oggetto'][:60]}")
            try:
                spesa = elabora_spesa(atto)
                nuove.append(spesa)
                note.add((atto["numero_raw"], atto["oggetto"]))
            except Exception as e:
                log.error(f"  Elaborazione fallita, salto: {e}")
            time.sleep(1)

        if nuove:
            archivio = nuove + archivio
            salva(archivio, nuove)  # salvataggio incrementale: run interrotti non perdono nulla

        if griglia_esaurita:
            stato["griglie_completate"].append(griglia)
        salva_stato(stato)

    log.info("=" * 60)
    log.info(f"Backfill: {elaborate} atti elaborati in questo run.")
    if elaborate >= args.max_atti:
        log.info("ARCHIVIO NON ESAURITO: rilancia il workflow per il blocco successivo.")
    else:
        log.info("Archivio esaurito (o nessuna spesa nuova trovata).")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
