#!/usr/bin/env python3
"""
siope_estrai.py — I pagamenti di cassa del Comune, da SIOPE.

PERCHÉ SERVE
L'albo pretorio tiene le liquidazioni quindici giorni, poi le cancella: per
questo la serie dei pagamenti sul sito parte da luglio 2026 e prima non c'è
niente. SIOPE è l'altra strada: la Ragioneria dello Stato, tramite Banca
d'Italia, pubblica ogni mese i pagamenti di cassa di TUTTI i Comuni, per
codice gestionale, e li tiene per anni. Non dipende dall'albo e non si
cancella.

I due dati non sono la stessa cosa e non vanno sommati:
  · l'albo dice QUALE atto autorizza una spesa, con oggetto e beneficiario
  · SIOPE dice QUANTO è uscito davvero dalla cassa in quel mese, senza dire
    a chi né perché
Messi accanto, il secondo misura quanto ci sfugge del primo.

COME FUNZIONA
Il file nazionale è grosso (milioni di righe: tutti i Comuni italiani), e non
si tiene nel repository. Si scarica, si filtra sul codice fiscale del Comune,
si salva solo l'estratto. Come per i CSV di BDAP in `bilanci/`.

Prima di estrarre bisogna sapere com'è fatto il file. La modalità --ispeziona
lo scarica e dichiara cosa ci trova, senza scrivere niente:

    python3 scripts/siope_estrai.py --ispeziona --anno 2025
    python3 scripts/siope_estrai.py --anno 2025 --applica

Da GitHub Actions si lancia con il workflow «Conti in chiaro — Pagamenti
SIOPE», che ha la rete per scaricare.
"""

import argparse
import csv
import io
import itertools
import json
import logging
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# Il Comune di Pieve Emanuele nell'anagrafica SIOPE
CODICE_FISCALE = "80104290152"
NOME_ENTE = "Comune di Pieve Emanuele"

BASE = "https://www.siope.it/documenti/siope2/open/last"
USCITE = f"{BASE}/SIOPE_USCITE.{{anno}}.zip"
ANAGRAFICHE = f"{BASE}/SIOPE_ANAGRAFICHE.zip"

DESTINAZIONE = Path("data/siope.json")

# Il tracciato non è documentato in modo stabile: invece di fissare le
# posizioni si riconoscono le colonne dai nomi, con i sinonimi che SIOPE ha
# usato nel tempo. Se un nome non viene riconosciuto, --ispeziona lo dice.
COLONNE = {
    "codice_fiscale": ["codice_fiscale", "cod_fiscale", "cf_ente", "codicefiscale"],
    "ente": ["denominazione", "des_ente", "descrizione_ente", "ente"],
    "anno": ["anno", "esercizio"],
    "mese": ["mese", "periodo"],
    # Attenzione a non mettere qui "codice" da solo: corrisponderebbe anche a
    # CODICE_FISCALE, e tutti i pagamenti finirebbero raggruppati sotto il
    # codice fiscale dell'ente invece che sulla voce di spesa.
    "codice_gestionale": ["codice_gestionale", "cod_gestionale",
                          "cod_voce", "codice_siope", "cod_siope"],
    "descrizione": ["descrizione_gestionale", "des_gestionale", "descrizione",
                    "des_voce"],
    "importo": ["importo", "importo_pagamenti", "pagamenti", "valore"],
}


def scarica(url: str) -> bytes:
    """Scarica un file annunciando quanto pesa: sono decine di megabyte."""
    log.info(f"Scarico {url}")
    richiesta = urllib.request.Request(
        url, headers={"User-Agent": "civic-tech; conti-in-chiaro Pieve Emanuele"})
    with urllib.request.urlopen(richiesta, timeout=600) as risposta:
        dati = risposta.read()
    log.info(f"  {len(dati) / 1e6:.1f} MB scaricati")
    return dati


def apri_csv(contenuto: bytes):
    """I file dentro lo zip, uno per uno, come righe già decodificate."""
    with zipfile.ZipFile(io.BytesIO(contenuto)) as zf:
        for nome in zf.namelist():
            if not nome.lower().endswith((".csv", ".txt")):
                continue
            with zf.open(nome) as f:
                testo = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
                yield nome, testo


def separatore(riga: str) -> str:
    return max([";", ",", "\t", "|"], key=riga.count)


def mappa_colonne(intestazione: list[str]) -> dict:
    """Nome della colonna → posizione, riconosciuta dai sinonimi noti.

    Due passaggi: prima i nomi che combaciano esattamente, poi quelli che
    cominciano per un sinonimo. Una posizione già assegnata non viene mai
    riusata, altrimenti una colonna finisce a rappresentare due cose diverse.
    """
    normalizza = lambda s: re.sub(r"[^a-z0-9]", "_", (s or "").strip().lower())
    colonne = [normalizza(c) for c in intestazione]
    trovate, occupate = {}, set()

    for esatto in (True, False):
        for i, col in enumerate(colonne):
            if i in occupate:
                continue
            for chiave, sinonimi in COLONNE.items():
                if chiave in trovate:
                    continue
                combacia = (col in sinonimi if esatto
                            else any(col.startswith(s) for s in sinonimi))
                if combacia:
                    trovate[chiave] = i
                    occupate.add(i)
                    break
    return trovate


def numero(valore: str) -> float | None:
    v = (valore or "").strip().replace(" ", "")
    if not v:
        return None
    # SIOPE usa il punto decimale, ma non si può escludere il formato italiano
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


# Il file dei pagamenti NON ha intestazione (verificato sul 2021: la prima
# riga è «000053540,2021,07,1.03.02.15.006,6217139»). Le colonne sono, per
# posizione: codice ente SIOPE, anno, mese, codice gestionale, importo. Il
# codice fiscale non c'è: si ricava dal codice ente con l'anagrafica.
POSIZIONI = {"ente": 0, "anno": 1, "mese": 2, "codice_gestionale": 3, "importo": 4}


def codice_ente() -> tuple[str | None, list[str]]:
    """Il codice ente SIOPE del Comune, cercato per codice fiscale nei file
    dell'anagrafica. Restituisce anche le righe trovate, per il log."""
    contenuto = scarica(ANAGRAFICHE)
    trovate, codice = [], None
    for nome, testo in apri_csv(contenuto):
        log.info(f"\n── anagrafica {nome}")
        for n, r in enumerate(testo):
            if n < 3:
                log.info(f"   {r.rstrip()[:160]}")
            if CODICE_FISCALE in r:
                trovate.append(f"{nome}: {r.rstrip()[:200]}")
                campi = next(csv.reader([r], delimiter=separatore(r)))
                # il codice ente è il campo di nove cifre che non è il CF
                for c in campi:
                    c = c.strip().strip('"')
                    if re.fullmatch(r"\d{9}", c) and codice is None:
                        codice = c
    return codice, trovate


def ispeziona(anno: int) -> int:
    """Dichiara com'è fatto il file, senza scrivere niente.

    Domande, in ordine: che codice ha il Comune nell'anagrafica SIOPE, che
    colonne ha il file dei pagamenti, quante righe sono del Comune, e in che
    unità sono gli importi (euro o centesimi).
    """
    codice, trovate = codice_ente()
    log.info(f"\nRighe dell'anagrafica con il CF {CODICE_FISCALE}:")
    for t in trovate[:10]:
        log.info(f"   {t}")
    log.info(f"Codice ente SIOPE ricavato: {codice}")

    contenuto = scarica(USCITE.format(anno=anno))
    for nome, testo in apri_csv(contenuto):
        prima = testo.readline()
        sep = separatore(prima)
        campi = next(csv.reader([prima], delimiter=sep))
        senza_intestazione = bool(re.fullmatch(r"\d{6,}", campi[0].strip()))
        log.info(f"\n── {nome}: separatore '{sep}', {len(campi)} colonne, "
                 f"{'SENZA' if senza_intestazione else 'con'} intestazione")
        log.info(f"   prima riga: {campi}")
        if not senza_intestazione or not codice:
            continue
        mensili, voci, righe = defaultdict(float), defaultdict(float), 0
        esempi = []
        # una riga alla volta: il file ha milioni di righe
        for c in itertools.chain([campi], csv.reader(testo, delimiter=sep)):
            if len(c) < 5 or c[POSIZIONI["ente"]].strip() != codice:
                continue
            v = numero(c[POSIZIONI["importo"]])
            if v is None:
                continue
            righe += 1
            mensili[c[POSIZIONI["mese"]].strip()] += v
            voci[c[POSIZIONI["codice_gestionale"]].strip()] += v
            if len(esempi) < 5:
                esempi.append(c)
        totale = sum(mensili.values())
        log.info(f"   righe del Comune (codice {codice}): {righe:,}")
        for e in esempi:
            log.info(f"     {e}")
        log.info(f"   totale dell'anno se gli importi sono in euro:      {totale:,.2f} €")
        log.info(f"   totale dell'anno se gli importi sono in centesimi: {totale / 100:,.2f} €")
        log.info("   mesi: " + ", ".join(f"{m}: {v / 100:,.0f}" for m, v in sorted(mensili.items()))
                 + "  (in euro, se centesimi)")
        log.info("   voci più grandi (in euro, se centesimi):")
        for voce, v in sorted(voci.items(), key=lambda x: -x[1])[:8]:
            log.info(f"     {voce:18} {v / 100:>16,.2f}")
    return 0


def ispeziona_vecchio(anno: int) -> int:
    """Versione con intestazione: tenuta per il caso in cui SIOPE la rimetta."""
    contenuto = scarica(USCITE.format(anno=anno))
    for nome, testo in apri_csv(contenuto):
        log.info(f"\n── {nome}")
        lettore = None
        righe_nostre, righe_totali, con_importo = [], 0, 0
        for numero_riga, riga in enumerate(testo):
            if numero_riga == 0:
                sep = separatore(riga)
                intestazione = next(csv.reader([riga], delimiter=sep))
                col = mappa_colonne(intestazione)
                log.info(f"   separatore '{sep}' · {len(intestazione)} colonne")
                log.info(f"   intestazione: {intestazione[:14]}")
                log.info(f"   colonne riconosciute: { {k: intestazione[i] for k, i in col.items()} }")
                mancanti = set(COLONNE) - set(col)
                if mancanti:
                    log.warning(f"   NON riconosciute: {sorted(mancanti)}")
                lettore = csv.reader(testo, delimiter=sep)
                continue
            break
        if lettore is None:
            continue
        for campi in lettore:
            righe_totali += 1
            if "codice_fiscale" in col and len(campi) > col["codice_fiscale"]:
                if campi[col["codice_fiscale"]].strip() == CODICE_FISCALE:
                    righe_nostre.append(campi)
                    if "importo" in col and numero(campi[col["importo"]]):
                        con_importo += 1
            if righe_totali >= 4_000_000:
                break
        log.info(f"   {righe_totali:,} righe lette")
        log.info(f"   {len(righe_nostre)} righe di {NOME_ENTE} (CF {CODICE_FISCALE})")
        log.info(f"   di cui con un importo leggibile: {con_importo}")
        for campi in righe_nostre[:5]:
            log.info(f"     {campi[:10]}")
        if righe_nostre and not con_importo:
            log.warning("   ATTENZIONE: il Comune c'è ma gli importi non sono "
                        "leggibili. Per i Comuni SIOPE potrebbe pubblicare solo "
                        "il censimento dei movimenti, non i valori.")
    return 0


# Il primo numero del codice gestionale è il titolo della spesa. I titoli 5
# (chiusura delle anticipazioni di tesoreria) e 7 (partite di giro: ritenute,
# IVA in split payment) escono dalla cassa ma non sono spesa del Comune: sono
# restituzione di prestiti di cassa e soldi girati per conto di altri.
TITOLI = {"1": "Spese correnti", "2": "Spese in conto capitale",
          "3": "Spese per incremento di attività finanziarie",
          "4": "Rimborso di prestiti",
          "5": "Chiusura anticipazioni di tesoreria",
          "7": "Uscite per conto terzi e partite di giro"}
TITOLI_SPESA = ("1", "2", "3", "4")


def descrizioni_voci() -> dict:
    """Codice gestionale → descrizione, dall'anagrafica SIOPE delle uscite.
    Lo stesso codice compare una volta per comparto, con la stessa descrizione:
    se ne tiene la prima."""
    contenuto = scarica(ANAGRAFICHE)
    voci = {}
    for nome, testo in apri_csv(contenuto):
        if "CODGEST_USCITE" not in nome.upper():
            continue
        for campi in csv.reader(testo, delimiter=","):
            if len(campi) >= 3:
                voci.setdefault(campi[0].strip(), campi[2].strip())
    return voci


def estrai(anni: list[int]) -> dict:
    """Pagamenti di cassa mensili del Comune, per titolo e per voce.

    Il file non ha intestazione: le colonne si leggono per posizione
    (POSIZIONI). Gli importi sono in centesimi: verificato sul 2021, dove la
    somma dei pagamenti del Comune è 23,6 milioni in centesimi e sarebbe 2,36
    miliardi in euro."""
    codice, _ = codice_ente()
    if not codice:
        log.error(f"Codice ente SIOPE non trovato per il CF {CODICE_FISCALE}")
        return {}
    descrizioni = descrizioni_voci()
    log.info(f"Codice ente SIOPE: {codice} · {len(descrizioni):,} voci descritte")
    risultato = {}
    for anno in anni:
        try:
            contenuto = scarica(USCITE.format(anno=anno))
        except Exception as e:
            log.warning(f"{anno}: file non disponibile ({e})")
            continue
        mensili, mensili_spesa = defaultdict(float), defaultdict(float)
        per_titolo, per_voce = defaultdict(float), defaultdict(float)
        righe = 0
        for nome, testo in apri_csv(contenuto):
            for campi in csv.reader(testo, delimiter=","):
                if len(campi) < 5 or campi[POSIZIONI["ente"]].strip() != codice:
                    continue
                v = numero(campi[POSIZIONI["importo"]])
                if v is None:
                    continue
                euro = v / 100
                voce = campi[POSIZIONI["codice_gestionale"]].strip()
                titolo = voce[:1]
                mese = campi[POSIZIONI["mese"]].strip().zfill(2)
                righe += 1
                mensili[mese] += euro
                per_titolo[titolo] += euro
                per_voce[voce] += euro
                if titolo in TITOLI_SPESA:
                    mensili_spesa[mese] += euro
        if not righe:
            log.warning(f"{anno}: nessuna riga per {NOME_ENTE}")
            continue
        totale = round(sum(mensili.values()), 2)
        spesa = round(sum(mensili_spesa.values()), 2)
        log.info(f"{anno}: {righe:,} movimenti · cassa {totale:,.2f} € · "
                 f"spesa effettiva (titoli 1-4) {spesa:,.2f} € · mesi {len(mensili)}")
        risultato[str(anno)] = {
            "totale_cassa": totale,
            "spesa_effettiva": spesa,
            "mesi_presenti": len(mensili),
            "mensili_cassa": {m: round(x, 2) for m, x in sorted(mensili.items())},
            "mensili_spesa": {m: round(x, 2) for m, x in sorted(mensili_spesa.items())},
            "per_titolo": [{"titolo": t, "descrizione": TITOLI.get(t, ""),
                            "importo": round(x, 2)}
                           for t, x in sorted(per_titolo.items())],
            "per_voce": [{"codice": c, "descrizione": descrizioni.get(c, ""),
                          "importo": round(x, 2)}
                         for c, x in sorted(per_voce.items(), key=lambda x: -x[1])[:60]],
        }
    return risultato


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anno", type=int, action="append",
                        help="Anno da elaborare (ripetibile). Senza, 2021-2026")
    parser.add_argument("--ispeziona", action="store_true",
                        help="Mostra com'è fatto il file senza estrarre niente")
    parser.add_argument("--applica", action="store_true",
                        help="Scrive data/siope.json")
    args = parser.parse_args()
    anni = args.anno or list(range(2021, 2027))

    if args.ispeziona:
        return ispeziona(anni[0])

    dati = estrai(anni)
    if not dati:
        log.error("Nessun dato estratto: controlla con --ispeziona.")
        return 1

    uscita = {
        "ente": NOME_ENTE,
        "codice_fiscale": CODICE_FISCALE,
        "fonte": "SIOPE — Ragioneria Generale dello Stato, banca dati gestita "
                 "da Banca d'Italia",
        "url": BASE,
        "avvertenza": "Sono pagamenti di CASSA: quanto è materialmente uscito "
                      "dal conto del Comune in quel mese. Non sono confrontabili "
                      "con gli impegni del bilancio né sommabili con le "
                      "liquidazioni dell'albo, che descrivono gli stessi soldi "
                      "da un altro punto di vista. La «spesa effettiva» esclude "
                      "la restituzione delle anticipazioni di tesoreria (titolo 5) "
                      "e le partite di giro (titolo 7), che escono dalla cassa "
                      "ma non sono spesa del Comune.",
        "anni": dati,
    }
    if args.applica:
        DESTINAZIONE.parent.mkdir(parents=True, exist_ok=True)
        DESTINAZIONE.write_text(json.dumps(uscita, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        log.info(f"Scritto {DESTINAZIONE}")
    else:
        log.info("Anteprima: rilancia con --applica per salvare.")
        log.info(json.dumps(uscita, ensure_ascii=False, indent=1)[:900])
    return 0


if __name__ == "__main__":
    sys.exit(main())
