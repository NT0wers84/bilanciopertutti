"""
normalizza.py — Manutenzione della qualità dei dati raccolti.

Due interventi, entrambi conservativi (non cancellano mai una spesa):

1. NOMI DEI BENEFICIARI: lo stesso soggetto compare con grafie diverse
   ("TEKNO GREEN SRL" e "Tekno Green Srl di Melito di Napoli"), il che
   spezza le classifiche dei maggiori destinatari. Le grafie vengono
   ricondotte alla forma più completa. Le etichette che non indicano un
   destinatario reale (erario, "Fornitori diversi") vengono marcate.

2. POSSIBILI DOPPI CONTEGGI: atti diversi con stesso importo e stesso
   oggetto possono essere la stessa spesa contata due volte (impegno +
   rimodulazione) oppure pratiche distinte a tariffa fissa. La differenza
   non è decidibile dal testo: gli atti vengono SEGNALATI, mai rimossi.

Uso:  python3 scripts/normalizza.py [--applica]
Senza --applica mostra solo cosa cambierebbe.
"""

import re
import sys
import json
import logging
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from estrattore import chiave_beneficiario, e_non_beneficiario

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SPESE = Path("data/spese.json")

# Parole di servizio rimaste in coda all'estrazione: non fanno parte del
# nome ("ITALIANA PETROLI S.P.A. di complessivi").
CODA_SPURIA = re.compile(
    r"\s+(?:di|del|della|dei|delle|per|con|in|su|a|ad|e|ed|oltre|pari|"
    r"complessiv\w+|import\w+|iva|esclusa|inclusa|compresa|totale)"
    r"(?:\s+(?:di|del|della|complessiv\w+|import\w+|iva|esclusa|inclusa))*\s*$",
    re.IGNORECASE)


# "fornitore ADF GROUP s.r.l" contiene il nome vero: va tolto il prefisso,
# non scartato il record. Resta generico solo ciò che non nomina nessuno.
PREFISSO_SPURIO = re.compile(
    r"^(?:il\s+|la\s+)?(?:fornitor[ei]|fornitrice|ditta|impresa|societ[àa]|"
    r"operatore(?:\s+economico)?|azienda)\s+(?=\S)", re.IGNORECASE)


# Se dopo il prefisso resta solo questo, l'etichetta era già generica:
# "Fornitori diversi" non va ridotto a "diversi".
RESIDUO_GENERICO = re.compile(
    r"^(divers[ei]|vari[ei]?|altri|altre|misti|generici|"
    r"di servizi|economici?)$", re.IGNORECASE)


def ripulisci_prefisso(nome: str) -> str:
    """Toglie 'fornitore ', 'ditta ' ecc. solo se resta un nome vero."""
    t = (nome or "").strip()
    nuovo = PREFISSO_SPURIO.sub("", t).strip()
    if len(nuovo) <= 2 or RESIDUO_GENERICO.match(nuovo):
        return t
    return nuovo


def ripulisci_coda(nome: str) -> str:
    """Toglie le parole di servizio finali, ripetutamente."""
    precedente = None
    t = (nome or "").strip(" .,;-")
    while t != precedente:
        precedente = t
        t = CODA_SPURIA.sub("", t).strip(" .,;-")
    return t or nome


def normalizza_nomi(spese: list[dict]) -> int:
    """Riconduce le grafie dello stesso soggetto alla forma più completa."""
    gruppi = defaultdict(list)
    for s in spese:
        nome = (s.get("beneficiario") or "").strip()
        if not nome or e_non_beneficiario(nome):
            continue
        chiave = chiave_beneficiario(nome)
        if chiave:
            gruppi[chiave].append(s)

    modificati = 0
    for chiave, righe in gruppi.items():
        varianti = {(r.get("beneficiario") or "").strip() for r in righe}
        if len(varianti) < 2:
            continue
        # forma canonica: la più lunga (di norma la più informativa),
        # a parità di lunghezza quella che ricorre più spesso
        frequenze = defaultdict(int)
        for r in righe:
            frequenze[(r.get("beneficiario") or "").strip()] += 1
        pulite = {ripulisci_coda(v) for v in varianti}
        canonico = sorted(pulite, key=lambda v: (len(v), frequenze.get(v, 0)),
                          reverse=True)[0]
        log.info(f"  {canonico}")
        for v in sorted(varianti - {canonico}):
            log.info(f"      ← {v}")
        for r in righe:
            if (r.get("beneficiario") or "").strip() != canonico:
                r["beneficiario"] = canonico
                modificati += 1
    return modificati


def marca_non_beneficiari(spese: list[dict]) -> int:
    """Segnala le etichette che non indicano un destinatario reale."""
    n = 0
    for s in spese:
        # prima togli il prefisso: "fornitore ACME Srl" → "ACME Srl"
        nome = ripulisci_prefisso((s.get("beneficiario") or "").strip())
        if nome != (s.get("beneficiario") or "").strip():
            s["beneficiario"] = nome
        era = s.get("beneficiario_generico")
        s["beneficiario_generico"] = bool(nome) and e_non_beneficiario(nome)
        if s["beneficiario_generico"] and not era:
            n += 1
    return n


def _chiave_oggetto(testo: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (testo or "").lower())[:70].strip()


def marca_possibili_duplicati(spese: list[dict]) -> int:
    """
    Marca gli atti che condividono importo e oggetto con altri atti.
    NON li rimuove: possono essere la stessa spesa reimpegnata oppure
    pratiche distinte con la stessa tariffa.
    """
    gruppi = defaultdict(list)
    for s in spese:
        if s.get("importo_euro") is None:
            continue
        gruppi[(round(s["importo_euro"], 2), _chiave_oggetto(s.get("oggetto")))].append(s)

    n = 0
    for (importo, _), righe in gruppi.items():
        gemelli = [r.get("numero_raw") for r in righe]
        for r in righe:
            r["atti_gemelli"] = ([g for g in gemelli if g != r.get("numero_raw")]
                                 if len(righe) > 1 else None)
        if len(righe) > 1:
            n += len(righe)
            log.info(f"  {importo:>12,.2f} € × {len(righe)} → {', '.join(gemelli)}")
    return n


def main() -> int:
    applica = "--applica" in sys.argv
    if not SPESE.exists():
        log.error(f"{SPESE} non trovato")
        return 1
    spese = json.loads(SPESE.read_text(encoding="utf-8"))

    log.info("GRAFIE UNIFICATE")
    n_nomi = normalizza_nomi(spese)
    log.info(f"→ {n_nomi} record aggiornati\n")

    log.info("ETICHETTE NON-BENEFICIARIO")
    n_gen = marca_non_beneficiari(spese)
    generici = sorted({s["beneficiario"] for s in spese if s.get("beneficiario_generico")})
    for g in generici:
        log.info(f"  {g}")
    log.info(f"→ {n_gen} record marcati\n")

    log.info("POSSIBILI DOPPI CONTEGGI (stesso importo e oggetto)")
    n_dup = marca_possibili_duplicati(spese)
    log.info(f"→ {n_dup} atti coinvolti, nessuno rimosso\n")

    if applica:
        SPESE.write_text(json.dumps(spese, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info(f"Scritto {SPESE}")
    else:
        log.info("Anteprima: rilancia con --applica per salvare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
