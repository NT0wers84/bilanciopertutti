"""
ricalcola_importi.py — Ricalcola gli importi dai testi già in archivio.

Serve quando cambia la gerarchia di lettura degli importi in estrattore.py:
invece di riscaricare gli atti dal portale (e di rispendere chiamate a Groq),
rilegge i testi salvati in `docs/testi/` e riapplica le regole correnti.

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
from estrattore import (estrai_importo, e_entrata_pura, impegni_dispositivo,
                        _etichetta_multipla, leggi_prospetto_liquidazione,
                        chiave_beneficiario)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SPESE = Path("data/spese.json")
TESTI = Path("docs/testi")


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


def _aggiorna_creditori(spesa: dict, testo: str, cambi: list) -> None:
    """Il creditore dichiarato dal prospetto di liquidazione.

    Si sostituisce il nome solo quando il prospetto indica un soggetto DIVERSO,
    non quando lo stesso soggetto è scritto in un altro modo: «Leasys Italia
    S.p.A» e «LEASYS SPA» sono la stessa azienda, e il nome già normalizzato si
    legge meglio del maiuscolo del registro contabile.
    """
    _, _, creditori = leggi_prospetto_liquidazione(testo)
    if not creditori:
        return
    nomi = [c["nome"] for c in creditori]
    vecchio = (spesa.get("beneficiario") or "").strip()
    if len(nomi) == 1 and vecchio and \
            chiave_beneficiario(vecchio) == chiave_beneficiario(nomi[0]):
        return
    nuovo = nomi[0] if len(nomi) == 1 else _etichetta_multipla(f"{len(nomi)} fornitori")
    if nuovo == vecchio and (spesa.get("n_beneficiari") or 1) == len(nomi):
        return
    cambi.append((spesa, vecchio, nomi))
    spesa["beneficiario"] = nuovo
    spesa["n_beneficiari"] = len(nomi)
    spesa["beneficiari_dettaglio"] = creditori if len(nomi) > 1 else None


def ricalcola(spese: list[dict]) -> tuple[list, list]:
    """Rilegge gli importi dai testi in archivio.

    Restituisce due liste distinte, perché sono due cose diverse: gli importi
    che CAMBIANO valore, e quelli che restano identici ma smettono di essere
    dubbi perché una regola conclusiva ora li conferma.
    """
    cambi, confermati, beneficiari = [], [], []
    for s in spese:
        testo = leggi_testo(s["id"])
        if not testo:
            continue

        # Quando il dispositivo impegna verso più fornitori, il beneficiario
        # non è uno solo: va sostituito insieme all'importo, altrimenti a un
        # fornitore resta attribuita la spesa di tutti gli altri.
        if s.get("tipo_atto") != "liquidazione":
            voci = impegni_dispositivo(testo)
            if voci and len(voci) != (s.get("n_beneficiari") or 0):
                s["beneficiari_dettaglio"] = voci
                s["n_beneficiari"] = len(voci)
                s["beneficiario"] = _etichetta_multipla(f"{len(voci)} fornitori")
        else:
            _aggiorna_creditori(s, testo, beneficiari)
        nuovo, regola, incerto = estrai_importo(testo, s.get("oggetto", ""),
                                                s.get("tipo_atto", ""))
        if nuovo is None:
            continue
        vecchio, vecchia_regola = s.get("importo_euro"), s.get("regola_importo")
        if nuovo == vecchio:
            # Stesso importo, ma una regola conclusiva dove prima non c'era:
            # l'atto non ha più bisogno dell'avviso «da verificare». Il numero
            # non cambia, cambia quanto ne siamo sicuri.
            if s.get("importo_incerto") and not incerto:
                confermati.append((s, regola))
                s["importo_incerto"] = False
                s["regola_importo"] = regola
            continue
        # Le due regole che leggono l'atto voce per voce (il prospetto di una
        # liquidazione, l'elenco degli impegni di una determina) vincono sempre:
        # è il Comune a fare quel conto. Le altre aggiornano solo ciò che aveva
        # deciso il modello o una regola che ora non vale più.
        autorevole = regola.startswith(("prospetto di liquidazione",
                                        "somma degli impegni"))
        if not autorevole and vecchia_regola and s.get("estrazione") != "regex":
            continue
        cambi.append((s, vecchio, vecchia_regola, nuovo, regola))
        s["importo_euro"] = nuovo
        s["regola_importo"] = regola
        s["importo_incerto"] = incerto
    return cambi, confermati, beneficiari


def main() -> int:
    if not SPESE.exists():
        log.error(f"{SPESE} non trovato")
        return 1
    spese = json.loads(SPESE.read_text(encoding="utf-8"))
    partenza = len(spese)

    if "--importa" in sys.argv:
        importa(spese, Path(sys.argv[sys.argv.index("--importa") + 1]))
        log.info("")

    cambi, confermati, beneficiari = ricalcola(spese)
    log.info(f"IMPORTI CAMBIATI: {len(cambi)} su {len(spese)} atti\n")
    for s, vecchio, vr, nuovo, nr in sorted(cambi, key=lambda c: -(c[3] or 0)):
        v = "vuoto" if vecchio is None else f"{vecchio:,.2f}"
        log.info(f"  n.{s['numero_raw']:10} {v:>16} → {nuovo:>16,.2f}")
        log.info(f"      da [{vr or 'modello'}] a [{nr}]")
        log.info(f"      {s['oggetto'][:74]}")

    if beneficiari:
        log.info(f"\nBENEFICIARI DAL PROSPETTO: {len(beneficiari)} liquidazioni")
        log.info("  Il nome lo dichiara il Comune nel prospetto contabile, non la "
                 "prosa dell'atto.")
        for s, vecchio, nomi in beneficiari:
            fine = (nomi[0] if len(nomi) == 1
                    else f"{len(nomi)} fornitori: " + ", ".join(n[:24] for n in nomi[:4]))
            log.info(f"  n.{s['numero_raw']:10} {(vecchio or '(vuoto)')[:38]:40} → {fine[:62]}")

    if confermati:
        log.info(f"\nIMPORTI CONFERMATI: {len(confermati)} atti perdono l'avviso "
                 f"«da verificare»")
        log.info("  Il valore non cambia: cambia che ora una regola conclusiva lo conferma.")
        regole = {}
        for s, regola in confermati:
            chiave = regola.split(" (")[0]
            regole[chiave] = regole.get(chiave, 0) + 1
        for regola, n in sorted(regole.items(), key=lambda x: -x[1]):
            log.info(f"    {n:>4}  {regola}")
        restano = sum(1 for s in spese if s.get("importo_incerto"))
        log.info(f"  Restano {restano} importi da verificare (erano "
                 f"{restano + len(confermati)}).")

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
