"""
backfill.py — OpenSpese Pieve Emanuele: recupero dello storico (v3).

Ricetta verificata sul portale reale (browser, 2026-07): l'albo espone
l'INTERO archivio storico (7.954 atti dal maggio 2021) tramite
POST eseguiOrdinamentoLista + paginazione eseguiPaginazione.
Il filtro annoRegistrazioneDa (">= anno") restringe la scansione;
il filtro per data è rotto lato server e non va mai usato.

Flusso per run:
  1. scansione archivio (albo + provvedimenti) da anno_min in poi (~5-10 min)
  2. censimento per anno/tipo nel log
  3. elaborazione di max --max-atti spese nuove (dalle più recenti),
     con salvataggio incrementale: rilanciare finché il log non dice
     "ARCHIVIO ESAURITO".

Uso:
  python scripts/backfill.py --max-atti 300 [--anno-min 2021] [--solo-censimento]
"""

import time
import logging
import argparse

import portale
from scraper import carica_archivio, chiavi_note, elabora_spesa, salva

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# (nome, pagina con i form di ricerca/paginazione)
SORGENTI = [
    ("albo", portale.ALBO_URL),
    ("provvedimenti", f"{portale.BASE_URL}/web/trasparenza/papca-g"),
]


def censimento_righe(nome: str, righe: list[dict]) -> None:
    per_anno: dict[int, dict] = {}
    tipi: dict[str, int] = {}
    for r in righe:
        anno = portale.estrai_anno(r)
        per_anno.setdefault(anno, {"totale": 0, "spese": 0})
        per_anno[anno]["totale"] += 1
        if portale.e_spesa(r["tipo"]):
            per_anno[anno]["spese"] += 1
        tipi[r["tipo"]] = tipi.get(r["tipo"], 0) + 1
    log.info(f"CENSIMENTO {nome} — {len(righe)} atti:")
    for anno, c in sorted(per_anno.items()):
        log.info(f"  {anno}: {c['totale']} atti, di cui {c['spese']} spese")
    log.info(f"Tipi più frequenti in {nome}:")
    for t, c in sorted(tipi.items(), key=lambda x: -x[1])[:10]:
        marcatore = " ← SPESA" if portale.e_spesa(t) else ""
        log.info(f"  {c:5} × {t!r}{marcatore}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-atti", type=int, default=300,
                        help="Massimo numero di spese da elaborare in questo run")
    parser.add_argument("--anno-min", type=int, default=2021,
                        help="Non risalire oltre quest'anno (archivio del "
                             "portale: da maggio 2021)")
    parser.add_argument("--solo-censimento", action="store_true",
                        help="Solo scansione e censimento, nessuna elaborazione")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("OPENSPESE — BACKFILL STORICO v3 (archivio completo albo)")
    log.info(f"max-atti={args.max_atti}  anno-min={args.anno_min}  "
             f"solo-censimento={args.solo_censimento}")
    log.info("=" * 60)

    portale.init_sessione()
    archivio = carica_archivio()
    note = chiavi_note(archivio)

    # 1. Scansione archivio di tutte le sorgenti
    candidate: list[dict] = []
    viste: set[tuple] = set()
    for nome, url_pagina in SORGENTI:
        log.info(f"=== Scansione archivio {nome!r} ===")
        righe = portale.ricerca_archivio(url_pagina, anno_da=args.anno_min)
        censimento_righe(nome, righe)
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

    candidate.sort(key=lambda r: (r.get("data_inizio") or "", r.get("numero_raw") or ""),
                   reverse=True)
    log.info(f"Spese nuove da elaborare (tutte le sorgenti): {len(candidate)}")

    if args.solo_censimento:
        log.info("Solo censimento: nessuna elaborazione. Fine.")
        return

    # 2. Elaborazione a blocchi
    nuove: list[dict] = []
    elaborate = 0
    for atto in candidate[:args.max_atti]:
        elaborate += 1
        log.info(f"[{elaborate}/{min(args.max_atti, len(candidate))}] "
                 f"{atto['tipo']} {atto['numero_raw']} — {atto['oggetto'][:60]}")
        try:
            spesa = elabora_spesa(atto)
            nuove.append(spesa)
        except Exception as e:
            log.error(f"  Elaborazione fallita, salto: {e}")
        # Salvataggio incrementale ogni 25 atti: nulla si perde
        if len(nuove) and len(nuove) % 25 == 0:
            salva(nuove + archivio, nuove)
        time.sleep(1)

    if nuove:
        salva(nuove + archivio, nuove)

    log.info("=" * 60)
    log.info(f"Backfill: {elaborate} spese elaborate in questo run.")
    if len(candidate) > args.max_atti:
        log.info(f"ARCHIVIO NON ESAURITO: restano {len(candidate) - args.max_atti} "
                 f"spese. Rilancia il workflow.")
    else:
        log.info("ARCHIVIO ESAURITO fino ad anno-min. Backfill completo.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
