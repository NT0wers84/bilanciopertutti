"""
backfill.py — OpenSpese Pieve Emanuele: recupero dello storico.

Strategia (v2, guidata dalla ricerca): il portlet pubblicazioni espone un
form di ricerca con intervallo di date di pubblicazione. Il filtro resta
nella sessione Liferay, quindi: POST del form per una finestra mensile →
paginazione normale della lista filtrata → elaborazione delle spese.
Le liste di default (griglie) mostrano solo un sottoinsieme curato: la
ricerca è l'unica via all'archivio completo, se esiste.

Modalità censimento (--solo-censimento): interroga finestre-sonda su più
anni (2026, 2024, 2022, 2020, 2018, 2016) e logga quante righe restituisce
il portale: verdetto immediato sulla profondità reale dell'archivio.

A blocchi: --max-atti per run, stato in data/backfill_state.json
(finestre completate per sorgente), rilanciabile fino a esaurimento.

Uso:
  python scripts/backfill.py --max-atti 300 [--anno-min 2016] [--solo-censimento]
"""

import json
import time
import logging
import argparse
import calendar
from pathlib import Path
from datetime import date

import portale
from scraper import carica_archivio, chiavi_note, elabora_spesa, salva

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_JSON = Path("data/backfill_state.json")

# Finestre-sonda per il censimento: un mese campione ogni due anni
FINESTRE_SONDA = ["2026-06", "2024-06", "2022-06", "2020-06", "2018-06", "2016-06"]


def carica_stato() -> dict:
    if STATE_JSON.exists():
        try:
            stato = json.loads(STATE_JSON.read_text(encoding="utf-8"))
            stato.setdefault("finestre_completate", [])
            stato.setdefault("censimento_ricerca", {})
            return stato
        except json.JSONDecodeError:
            pass
    return {"finestre_completate": [], "censimento_ricerca": {}}


def salva_stato(stato: dict) -> None:
    STATE_JSON.parent.mkdir(exist_ok=True)
    STATE_JSON.write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")


def confini_mese(etichetta: str) -> tuple[str, str]:
    """'2024-06' → ('2024-06-01', '2024-06-30')"""
    anno, mese = int(etichetta[:4]), int(etichetta[5:7])
    ultimo = calendar.monthrange(anno, mese)[1]
    return f"{anno}-{mese:02d}-01", f"{anno}-{mese:02d}-{ultimo:02d}"


def finestre_mensili(anno_min: int) -> list[str]:
    """Etichette 'YYYY-MM' dal mese corrente a ritroso fino a gennaio anno_min."""
    oggi = date.today()
    finestre = []
    anno, mese = oggi.year, oggi.month
    while anno >= anno_min:
        finestre.append(f"{anno}-{mese:02d}")
        mese -= 1
        if mese == 0:
            mese, anno = 12, anno - 1
    return finestre


def raccogli_finestra(slug: str, url_form: str, etichetta: str) -> list[dict] | None:
    """
    POST del filtro data per una finestra e lettura della lista filtrata.
    Le righe vengono validate contro l'intervallo: se il portale ha ignorato
    il filtro (righe fuori finestra), vengono scartate e il log lo segnala.
    """
    da, a = confini_mese(etichetta)
    html1 = portale.imposta_filtro_ricerca(url_form, da, a)
    if html1 is None:
        return None
    url_lista = portale._url_mostra_lista(slug)
    righe = portale.scrape_griglia(url_lista, stop_se_tutti_noti=False,
                                   html_prima_pagina=html1)
    in_finestra = [r for r in righe
                   if r.get("data_inizio") and da <= r["data_inizio"] <= a]
    fuori = len(righe) - len(in_finestra)
    if fuori:
        log.warning(f"  {etichetta}: {fuori} righe FUORI finestra scartate "
                    f"(il filtro data non è stato applicato dal portale?)")
    return in_finestra


def censimento(stato: dict) -> None:
    """Sonda l'archivio con finestre campione e logga il verdetto."""
    for nome, url_form, slug in portale.SORGENTI_RICERCA:
        log.info(f"=== SONDA RICERCA sorgente {nome!r} ===")
        portale.dump_moduli_ricerca(url_form)
        for etichetta in FINESTRE_SONDA:
            righe = raccogli_finestra(slug, url_form, etichetta)
            if righe is None:
                log.warning(f"  {etichetta}: form di ricerca non disponibile su {nome}")
                break
            spese = sum(1 for r in righe if portale.e_spesa(r["tipo"]))
            log.info(f"  SONDA {nome} {etichetta}: {len(righe)} atti, "
                     f"di cui {spese} spese")
            stato["censimento_ricerca"][f"{nome}|{etichetta}"] = {
                "totale": len(righe), "spese": spese}
            salva_stato(stato)
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-atti", type=int, default=300,
                        help="Massimo numero di atti da elaborare in questo run")
    parser.add_argument("--anno-min", type=int, default=2016,
                        help="Non risalire oltre quest'anno")
    parser.add_argument("--solo-censimento", action="store_true",
                        help="Solo finestre-sonda, nessuna elaborazione")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("OPENSPESE — BACKFILL STORICO (ricerca per finestre)")
    log.info(f"max-atti={args.max_atti}  anno-min={args.anno_min}  "
             f"solo-censimento={args.solo_censimento}")
    log.info("=" * 60)

    portale.init_sessione()
    stato = carica_stato()

    if args.solo_censimento:
        censimento(stato)
        log.info("Censimento completato. Leggi le righe 'SONDA' qui sopra.")
        return

    archivio = carica_archivio()
    note = chiavi_note(archivio)
    elaborate = 0

    for nome, url_form, slug in portale.SORGENTI_RICERCA:
        if elaborate >= args.max_atti:
            break
        for etichetta in finestre_mensili(args.anno_min):
            if elaborate >= args.max_atti:
                break
            chiave_finestra = f"{nome}|{etichetta}"
            if chiave_finestra in stato["finestre_completate"]:
                continue

            log.info(f"--- Finestra {chiave_finestra}")
            righe = raccogli_finestra(slug, url_form, etichetta)
            if righe is None:
                log.warning(f"Form di ricerca non disponibile su {nome}: "
                            f"salto la sorgente.")
                break

            candidate, viste = [], set()
            for r in righe:
                chiave = (r["numero_raw"], r["oggetto"])
                if not portale.e_spesa(r["tipo"]):
                    continue
                if chiave in note or chiave in viste:
                    continue
                viste.add(chiave)
                r["fonte"] = "storico"
                candidate.append(r)
            log.info(f"  {len(righe)} atti nella finestra, "
                     f"{len(candidate)} spese nuove da elaborare")

            nuove = []
            finestra_esaurita = True
            for atto in candidate:
                if elaborate >= args.max_atti:
                    finestra_esaurita = False
                    log.info(f"Limite {args.max_atti} raggiunto: rilancia il "
                             f"workflow per continuare da {chiave_finestra}.")
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
                salva(archivio, nuove)  # incrementale: nulla si perde

            if finestra_esaurita:
                stato["finestre_completate"].append(chiave_finestra)
            salva_stato(stato)

    log.info("=" * 60)
    log.info(f"Backfill: {elaborate} atti elaborati in questo run.")
    if elaborate >= args.max_atti:
        log.info("ARCHIVIO NON ESAURITO: rilancia il workflow per il blocco successivo.")
    else:
        log.info("Tutte le finestre elaborate (fino ad anno-min).")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
