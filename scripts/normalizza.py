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
from estrattore import chiave_beneficiario, e_non_beneficiario, e_entrata_pura

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


# Frasi che descrivono la FORNITURA, non il fornitore, rimaste attaccate al
# nome: "HARTEX GROUP SRL la fornitura di carta", "NAVA SERVICE DI FREDA
# NICOLA DEL SERVIZIO DI PULIZIA", "Studio Idini, con sede in Via Marsala".
# Si taglia da lì in poi. Il punto delicato è che "DI"/"DEL" fanno spesso
# parte del nome ("NAVA SERVICE DI FREDA NICOLA", "GE.CO di Rho Laura"):
# per questo si richiede una parola-spia esplicita, non la sola preposizione.
CODA_DESCRITTIVA = re.compile(
    r"(?:\s*,)?\s+(?:"
    r"(?:la|il|lo|le|i|l')\s+(?:fornitur\w+|servizi\w*|stamp\w+|manutenzion\w+|"
    r"noleggi\w+|acquist\w+|lavor\w+|realizzazion\w+|gestion\w+)"
    r"|(?:de[lial]{1,3}|per\s+(?:la|il|lo|le|i|l'))\s+"
    r"(?:fornitur\w+|servizi\w*|stamp\w+|manutenzion\w+|noleggi\w+|acquist\w+|"
    r"lavor\w+|realizzazion\w+|gestion\w+|important\w+)"
    r"|con\s+sede\b|avente\b|titolare\b|rappresentat\w+\b|in\s+persona\b"
    r").*$",
    re.IGNORECASE)

# Lo stesso, ma incollato al punto della forma societaria senza spazio:
# "ALDO&C S.N.C.PER LA STAMPA" → il taglio va fatto dopo "S.N.C."
CODA_ATTACCATA = re.compile(r"(?<=\.)(?:PER|DEL|DELLA|CON)\b.*$", re.IGNORECASE)

# Un taglio che lascia una sola parola comune ha mangiato l'informazione utile:
# "personale del Servizio Tributi" non va ridotto a "personale". Si accetta il
# residuo solo se ha almeno due parole o contiene una maiuscola (una sigla, un
# cognome), cioè se somiglia ancora all'identità di qualcuno.
def _residuo_valido(testo: str) -> bool:
    return len(testo) > 2 and (len(testo.split()) >= 2 or any(c.isupper() for c in testo))


# Il punto finale di una sigla fa parte del nome ("ATM S.P.A."): va tolto solo
# se è punteggiatura di frase, non se chiude un'abbreviazione.
FINE_SIGLA = re.compile(r"(?:\b\w\.){2,}$|\b[A-Za-z]{1,4}\.$")


def _ripulisci_bordi(testo: str) -> str:
    t = testo.strip().strip(",;-").strip()
    if FINE_SIGLA.search(t):
        return t
    return t.strip(" .,;-")

# Il nome vero viene dopo: "elettronici dal fornitore DAY RISTOSERVICE SPA".
# Si tiene ciò che segue, a patto che sia un nome e non un'altra parola vuota.
PREFISSO_INTERNO = re.compile(
    r"^.*?\b(?:dal|dalla|dai|dalle|al|alla|ai|alle)\s+"
    r"(?:fornitor[ei]|ditt[ae]|societ[àa]|impres[ae]|cooperativ[ae])\s+(?=\S)",
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
    for regola in (PREFISSO_SPURIO, PREFISSO_INTERNO):
        nuovo = regola.sub("", t).strip()
        if len(nuovo) > 2 and not RESIDUO_GENERICO.match(nuovo):
            t = nuovo
    return t


def ripulisci_coda(nome: str) -> str:
    """Toglie da un nome la descrizione di ciò che è stato fornito.

    Prima la frase descrittiva ("... la fornitura di carta"), poi le parole di
    servizio rimaste in fondo ("... di complessivi"). Un taglio che lascerebbe
    meno di tre caratteri viene annullato: meglio un nome sporco che un nome
    distrutto, perché qui si sta riscrivendo il dato pubblicato.
    """
    t = _ripulisci_bordi(nome or "")
    # Prima il taglio attaccato alla sigla, poi quello con lo spazio: al
    # contrario, "S.N.C.PER LA STAMPA" perderebbe "LA STAMPA" e resterebbe
    # con un "PER" incollato alla sigla che nessuna regola toglie più.
    for regola in (CODA_ATTACCATA, CODA_DESCRITTIVA):
        nuovo = _ripulisci_bordi(regola.sub("", t))
        if _residuo_valido(nuovo):
            t = nuovo
    precedente = None
    while t != precedente:
        precedente = t
        nuovo = _ripulisci_bordi(CODA_SPURIA.sub("", t))
        if not _residuo_valido(nuovo):
            break
        t = nuovo
    return t or nome


def normalizza_nomi(spese: list[dict]) -> int:
    """Riconduce le grafie dello stesso soggetto alla forma più completa."""
    modificati = 0

    # Prima la pulizia, su OGNI nome. Farla solo dentro i gruppi con più
    # grafie lasciava sporco chi compare una volta sola, e soprattutto teneva
    # separati "HARTEX GROUP SRL" e "HARTEX GROUP SRL la fornitura di carta":
    # con due code diverse le chiavi non coincidono e il gruppo non si forma.
    for s in spese:
        nome = (s.get("beneficiario") or "").strip()
        if not nome or e_non_beneficiario(nome):
            continue
        pulito = ripulisci_coda(ripulisci_prefisso(nome))
        if pulito and pulito != nome:
            log.info(f"  ripulito: {nome[:58]}\n      → {pulito}")
            s["beneficiario"] = pulito
            modificati += 1

    gruppi = defaultdict(list)
    for s in spese:
        nome = (s.get("beneficiario") or "").strip()
        if not nome or e_non_beneficiario(nome):
            continue
        chiave = chiave_beneficiario(nome)
        if chiave:
            gruppi[chiave].append(s)

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


def rimuovi_entrate(spese: list[dict]) -> list[dict]:
    """Toglie dall'archivio gli atti di sola entrata.

    Sono finiti dentro prima che e_spesa() li riconoscesse: canoni di
    locazione, proventi delle multe, vendite di immobili. Sono soldi che
    entrano, e sommarli alle uscite falsa i totali nel verso peggiore.
    Gli atti misti (accertamento di entrata + contestuale impegno di spesa)
    restano: quelli una spesa la fanno davvero.
    """
    tenute, tolte = [], []
    for s in spese:
        (tolte if e_entrata_pura(s.get("oggetto", "")) else tenute).append(s)
    if tolte:
        totale = sum(s.get("importo_euro") or 0 for s in tolte)
        log.info(f"ATTI DI SOLA ENTRATA (non sono spese: {totale:,.2f} €)")
        for s in sorted(tolte, key=lambda x: -(x.get("importo_euro") or 0)):
            log.info(f"  {(s.get('importo_euro') or 0):>12,.2f} €  "
                     f"n.{s['numero_raw']}  {s['oggetto'][:64]}")
        log.info(f"→ {len(tolte)} atti rimossi dall'archivio")
    return tenute


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
    partenza = len(spese)

    spese = rimuovi_entrate(spese)
    log.info("")

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
        log.info(f"Scritto {SPESE}: {len(spese)} atti "
                 f"({partenza - len(spese)} rimossi perché non sono spese)")
    else:
        log.info("Anteprima: rilancia con --applica per salvare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
