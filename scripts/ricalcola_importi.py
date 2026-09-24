"""
ricalcola_importi.py — Ricalcola gli importi dai testi già in archivio.

Serve quando cambia la gerarchia di lettura degli importi in estrattore.py:
invece di riscaricare gli atti dal portale (e di rispendere chiamate a Groq),
rilegge i testi salvati in `data/testi/` e riapplica le regole correnti.

Aggiorna un record SOLO quando la nuova regola è più affidabile di quella che
aveva prodotto il valore attuale. La scala di affidabilità, dalla più alta:

  1. prospetto di liquidazione — è l'atto stesso a fare il conto, riga per
     riga, e comprende IVA, storni e fatture multiple
  2. regole sulla prosa dell'atto (totale generale, importo contrattuale…)
  3. qualunque cosa avesse deciso il modello

Uso:
  python3 scripts/ricalcola_importi.py              # anteprima
  python3 scripts/ricalcola_importi.py --applica    # scrive
  python3 scripts/ricalcola_importi.py --importa FILE.json --applica
"""

import gzip
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from estrattore import estrai_importo, e_entrata_pura

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SPESE = Path("data/spese.json")
TESTI = Path("data/testi")


def leggi_testo(id_atto: str) -> str | None:
    p = TESTI / f"{id_atto}.txt.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


def importa(spese: list[dict], percorso: Path) -> int:
    """Reintegra atti che mancano dall'archivio (es. persi in un merge)."""
    presenti = {s["id"] for s in spese}
    nuovi = [r for r in json.loads(percorso.read_text(encoding="utf-8"))
             if r["id"] not in presenti and not e_entrata_pura(r.get("oggetto", ""))]
    if nuovi:
        log.info(f"REINTEGRATI {len(nuovi)} atti da {percorso.name}")
        for r in nuovi:
            log.info(f"  n.{r['numero_raw']}  {r['oggetto'][:66]}")
        spese.extend(nuovi)
        spese.sort(key=lambda s: (s.get("data_pubblicazione") or "",
                                  s.get("numero_raw") or ""), reverse=True)
    return len(nuovi)


def ricalcola(spese: list[dict]) -> list[tuple]:
    cambi = []
    for s in spese:
        testo = leggi_testo(s["id"])
        if not testo:
            continue
        nuovo, regola, incerto = estrai_importo(testo, s.get("oggetto", ""),
                                                s.get("tipo_atto", ""))
        if nuovo is None:
            continue
        vecchio, vecchia_regola = s.get("importo_euro"), s.get("regola_importo")
        if nuovo == vecchio:
            continue
        # Il prospetto vince sempre; altrimenti si aggiorna solo ciò che era
        # stato deciso dal modello o da una regola che ora non vale più.
        dal_prospetto = regola.startswith("prospetto di liquidazione")
        if not dal_prospetto and vecchia_regola and s.get("estrazione") != "regex":
            continue
        cambi.append((s, vecchio, vecchia_regola, nuovo, regola))
        s["importo_euro"] = nuovo
        s["regola_importo"] = regola
        s["importo_incerto"] = incerto
    return cambi


def main() -> int:
    if not SPESE.exists():
        log.error(f"{SPESE} non trovato")
        return 1
    spese = json.loads(SPESE.read_text(encoding="utf-8"))
    partenza = len(spese)

    if "--importa" in sys.argv:
        importa(spese, Path(sys.argv[sys.argv.index("--importa") + 1]))
        log.info("")

    cambi = ricalcola(spese)
    log.info(f"IMPORTI RICALCOLATI: {len(cambi)} su {len(spese)} atti\n")
    for s, vecchio, vr, nuovo, nr in sorted(cambi, key=lambda c: -(c[3] or 0)):
        v = "vuoto" if vecchio is None else f"{vecchio:,.2f}"
        log.info(f"  n.{s['numero_raw']:10} {v:>16} → {nuovo:>16,.2f}")
        log.info(f"      da [{vr or 'modello'}] a [{nr}]")
        log.info(f"      {s['oggetto'][:74]}")

    if "--applica" in sys.argv:
        SPESE.write_text(json.dumps(spese, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        log.info(f"\nScritto {SPESE}: {len(spese)} atti "
                 f"({len(spese) - partenza:+d} rispetto a prima)")
    else:
        log.info("\nAnteprima: rilancia con --applica per salvare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
