#!/usr/bin/env python3
"""
test_estrattore.py — Rete di sicurezza sulla lettura degli atti.

Ogni test qui dentro nasce da un errore vero, trovato guardando il sito. Non
sono esempi inventati: sono gli atti che hanno mostrato un numero sbagliato,
con accanto la cifra corretta letta sul documento. Finché questi test passano,
quegli errori non possono tornare.

Si lancia senza installare niente:

    python3 scripts/test_estrattore.py

Esce con codice 1 se anche un solo caso fallisce, così può stare in un
workflow e fermare la pubblicazione prima che i dati sbagliati vadano online.

I casi che richiedono il testo integrale di un atto usano l'archivio in
data/testi/: se manca, il test viene saltato e dichiarato, non dato per buono.
"""

import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from estrattore import (                                    # noqa: E402
    importo_italiano, estrai_importo, impegni_dispositivo,
    somma_prospetto_liquidazione, e_entrata_pura, e_variazione_bilancio,
    categoria_da_settore,
)
from portale import metadati_scheda                          # noqa: E402
from normalizza import ripulisci_coda, ripulisci_prefisso   # noqa: E402
from portale import e_spesa                                 # noqa: E402
from genera_sito import _quota_annua                        # noqa: E402

TESTI = Path("data/testi")
falliti, passati, saltati = [], 0, []


def verifica(descrizione: str, ottenuto, atteso) -> None:
    global passati
    if ottenuto == atteso:
        passati += 1
    else:
        falliti.append(f"{descrizione}\n      atteso:  {atteso!r}\n      ottenuto: {ottenuto!r}")
        print(f"  FALLITO  {descrizione}")
        print(f"           atteso {atteso!r}, ottenuto {ottenuto!r}")


def testo_atto(id_atto: str):
    """Il testo archiviato di un atto, o None se non è nell'archivio."""
    p = TESTI / f"{id_atto}.txt.gz"
    if not p.exists():
        saltati.append(id_atto)
        return None
    with gzip.open(p, "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


def sezione(titolo: str) -> None:
    print(f"\n── {titolo}")


# ─────────────────────────────────────────────────────────────────────────────
def test_numeri_italiani():
    """La virgola è il decimale. Un modello che legge 8.540,00 come 8,54
    sbaglia di tre ordini di grandezza: è successo con la fattura Maggioli."""
    sezione("Numeri in formato italiano")
    for grezzo, atteso in [
        ("1.234,56", 1234.56),
        ("8.540,00", 8540.00),      # Maggioli: il sito mostrava 8,54
        ("254,44", 254.44),
        ("2,54", 2.54),
        ("140.000", 140000.0),
        ("1.000.000", 1000000.0),
        ("601.334,66", 601334.66),
        ("1976,40", 1976.40),       # senza separatore di migliaia
        ("0,01", 0.01),
    ]:
        verifica(f"importo_italiano({grezzo!r})", importo_italiano(grezzo), atteso)
    for grezzo in ("abc", "", None):
        verifica(f"importo_italiano({grezzo!r}) non è un numero",
                 importo_italiano(grezzo), None)


def test_soglie_di_legge():
    """«affidamenti sotto i 140.000 euro» è una citazione di legge, non una
    spesa: 26 atti mostravano quella cifra come importo."""
    sezione("Soglie di legge scambiate per importi")
    boilerplate = ("Considerato che ai sensi dell'art. 50 comma 1 lett. b) del "
                   "D.Lgs. 36/2023 per affidamenti di importo inferiore a "
                   "140.000,00 euro si procede ad affidamento diretto;")
    importo, _, _ = estrai_importo(boilerplate, "fornitura di cancelleria",
                                   "determinazione contabile")
    verifica("la soglia di legge non diventa l'importo dell'atto", importo, None)


def test_liquidazione_imet():
    """Il caso segnalato: il sito mostrava 1.671.543,18, cioè il valore
    dell'intero appalto dopo la modifica contrattuale. L'atto liquida il IV SAL
    per 601.334,66 IVA compresa, come dice il suo stesso prospetto contabile
    (601.334,66 meno lo storno IVA 54.666,79 più la riga esattoria 54.666,79)."""
    sezione("Liquidazione I.M.E.T. — IV SAL scuola M.L. King")
    t = testo_atto("liquidazione-1893-2026")
    if t is None:
        return
    importo, regola, _ = estrai_importo(t, "", "liquidazione")
    verifica("importo liquidato", importo, 601334.66)
    verifica("letto dal prospetto contabile",
             regola.startswith("prospetto di liquidazione"), True)
    somma, righe = somma_prospetto_liquidazione(t)
    verifica("il prospetto ha tre righe (spesa, storno IVA, esattoria)", righe, 3)
    verifica("lo storno negativo annulla la riga esattoria", somma, 601334.66)


def test_liquidazione_due_fatture():
    """SIVIS: l'atto liquida DUE fatture da 6.032,29. Il sito mostrava
    41.557,00, che è l'impegno della proroga citato in premessa."""
    sezione("Liquidazione SIVIS — due fatture nello stesso atto")
    t = testo_atto("liquidazione-1873-2026")
    if t is None:
        return
    importo, _, _ = estrai_importo(t, "", "liquidazione")
    verifica("le due fatture vengono sommate", importo, 12064.58)


def test_liquidazione_iva_non_doppia():
    """Qui il vecchio calcolo dava 300,00 sommando 250,00 + l'IVA 50,00 che
    nel prospetto compare due volte, una in negativo e una in positivo."""
    sezione("Liquidazione — l'IVA non va contata due volte")
    t = testo_atto("liquidazione-1457-2026")
    if t is None:
        return
    importo, _, _ = estrai_importo(t, "", "liquidazione")
    verifica("importo al netto del giroconto IVA", importo, 250.00)


def test_liquidazione_prende_il_lordo():
    """METALMAC: 916,30 è l'imponibile, quello che esce dalle casse è il lordo
    (916,30 + IVA 22% = 1.117,89), che è anche la cifra del prospetto."""
    sezione("Liquidazione — conta il lordo, non l'imponibile")
    t = testo_atto("liquidazione-1865-2026")
    if t is None:
        return
    importo, _, _ = estrai_importo(t, "", "liquidazione")
    verifica("importo IVA compresa", importo, 1117.89)


def test_determina_magna_grecia():
    """Il caso segnalato: il sito mostrava 4.479,84 (l'importo di AD Security)
    attribuito ad Antincendio Master. L'atto impegna verso otto fornitori."""
    sezione("Determina Magna Grecia — otto fornitori in un atto solo")
    t = testo_atto("determinazione-1897-2026")
    if t is None:
        return
    voci = impegni_dispositivo(t)
    verifica("fornitori riconosciuti", len(voci), 8)
    verifica("totale impegnato", round(sum(v["importo"] for v in voci), 2), 19330.24)
    importo, regola, _ = estrai_importo(t, "", "determinazione contabile")
    verifica("l'importo dell'atto è la somma degli impegni", importo, 19330.24)
    verifica("regola dichiarata", regola.startswith("somma degli impegni"), True)
    nomi = {v["nome"][:20] for v in voci}
    verifica("Sud Allestimenti senza la coda della frase",
             any(n.startswith("Sud Allestimenti") for n in nomi), True)


def test_determina_dodici_impegni():
    """Festa: dodici impegni, scritti in quattro modi diversi nello stesso
    atto («per un importo complessivo», «per un impegno complessivo», il refuso
    «compleassivo», e il nome dopo la cifra). Con un pattern unico se ne
    leggevano otto e il totale era 15.732,00 invece di 16.396,70."""
    sezione("Determina Festa — dodici impegni scritti in modi diversi")
    t = testo_atto("determinazione-1869-2026")
    if t is None:
        return
    voci = impegni_dispositivo(t)
    verifica("impegni riconosciuti", len(voci), 12)
    verifica("totale impegnato", round(sum(v["importo"] for v in voci), 2), 16396.70)


def test_salvaguardie_impegni():
    """Le quattro trappole in cui la somma degli impegni farebbe danni. Sono
    state trovate dal test di regressione, non da un lettore: senza questi
    vincoli il sito avrebbe mostrato 163.000 al posto di 99.430 e 4.523 al
    posto di 488.400."""
    sezione("Determine in cui NON si deve sommare")
    for id_atto, motivo in [
        ("determinazione-1284-2026", "stesso impegno citato col nome e senza"),
        ("determinazione-43-2026", "cinque righe senza nome del beneficiario"),
        ("determinazione-1597-2026", "spese tecniche accessorie, non fornitori"),
        ("determinazione-1103-2026", "quattro righe dello stesso fornitore"),
    ]:
        t = testo_atto(id_atto)
        if t is None:
            continue
        verifica(f"{id_atto}: {motivo}", impegni_dispositivo(t), [])


def test_entrate_non_sono_spese():
    """Canoni, multe e vendite immobiliari sono soldi che ENTRANO: 15 atti per
    899.434 euro stavano fra le uscite. Ma un atto che accerta un'entrata e
    contestualmente impegna una spesa resta una spesa."""
    sezione("Entrate e spese")
    for oggetto, atteso in [
        ("ACCERTAMENTO DI ENTRATA DERIVANTE DA CANONI DI LOCAZIONE", True),
        ("ACCERTAMENTI DI ENTRATA QUALI PROVENTI DELLE SANZIONI AMMINISTRATIVE", True),
        ("ASSUNZIONE E ACCERTAMENTO DI ENTRATA – TRASFORMAZIONE DIRITTO DI SUPERFICIE", True),
        ("ACCERTAMENTO DI ENTRATA E CORRISPONDENTE IMPEGNO DI SPESA PER MISURA B1", False),
        ("PROGETTO PADIS – ACCERTAMENTO ENTRATA E ASSUNZIONE IMPEGNI DI SPESA", False),
        ("IMPEGNO DI SPESA E CONTESTUALE ACCERTAMENTO DI ENTRATA PER CONTRATTI SAP", False),
        ("DETERMINA A CONTRARRE PER AFFIDAMENTO DEL SERVIZIO DI RISCOSSIONE", False),
        ("LIQUIDAZIONE FATTURA MAGGIOLI SPA", False),
    ]:
        verifica(f"e_entrata_pura: {oggetto[:52]}", e_entrata_pura(oggetto), atteso)

    sezione("Cosa entra nell'archivio delle spese")
    for tipo, oggetto, atteso in [
        ("liquidazione", "LIQUIDAZIONE FATTURA MAGGIOLI SPA", True),
        ("determinazione contabile", "AFFIDAMENTO DIRETTO PULIZIE", True),
        ("determinazione contabile", "VARIAZIONE AL BILANCIO DI PREVISIONE", False),
        ("determinazione contabile", "ACCERTAMENTO DI ENTRATA DA CANONI", False),
        ("delibera", "ACCERTAMENTO DI ENTRATA", False),
        ("ordinanza", "DIVIETO DI BALNEAZIONE", False),
    ]:
        verifica(f"e_spesa({tipo}, {oggetto[:40]})", e_spesa(tipo, oggetto), atteso)

    verifica("una variazione di bilancio non è spesa nuova",
             e_variazione_bilancio("VARIAZIONE AL BILANCIO DI PREVISIONE 2026"), True)


def test_nomi_beneficiari():
    """I nomi arrivano sporchi dagli atti. Le prime due righe sono i danni che
    la pulizia stava per fare: il punto delle sigle mangiato su trenta nomi, e
    «personale del Servizio Tributi» ridotto a «personale»."""
    sezione("Pulizia dei nomi dei beneficiari")
    pulisci = lambda n: ripulisci_coda(ripulisci_prefisso(n))  # noqa: E731
    for grezzo, atteso in [
        ("ATM S.P.A.", "ATM S.P.A."),                       # il punto resta
        ("personale del Servizio Tributi", "personale del Servizio Tributi"),
        ("HARTEX GROUP SRL la fornitura di carta e materiali vari", "HARTEX GROUP SRL"),
        ("NAVA SERVICE DI FREDA NICOLA DEL SERVIZIO DI PULIZIA", "NAVA SERVICE DI FREDA NICOLA"),
        ("elettronici dal fornitore DAY RISTOSERVICE SPA", "DAY RISTOSERVICE SPA"),
        ("F.A.F. srl con", "F.A.F. srl"),
        ("ITALIANA PETROLI S.P.A. di complessivi", "ITALIANA PETROLI S.P.A."),
        ("Fornitori diversi", "Fornitori diversi"),         # etichetta generica valida
        ("Comune di Rozzano", "Comune di Rozzano"),
    ]:
        verifica(f"nome: {grezzo[:48]}", pulisci(grezzo), atteso)


def test_quota_annua():
    """Un affidamento di quindici anni non è spesa dell'anno: da solo valeva
    l'80% della somma di tutti gli atti."""
    sezione("Contratti pluriennali")
    rifiuti = {"importo_euro": 121530402.20, "importo_e_pluriennale": True,
               "durata_anni": 15, "importo_primo_anno": 3120036.00}
    verifica("si usa la quota del primo anno dichiarata nell'atto",
             _quota_annua(rifiuti), 3120036.00)
    senza_quota = {"importo_euro": 300000.0, "importo_e_pluriennale": True,
                   "durata_anni": 3, "importo_primo_anno": None}
    verifica("senza quota dichiarata, la media annua",
             _quota_annua(senza_quota), 100000.0)
    normale = {"importo_euro": 1037.0, "importo_e_pluriennale": False,
               "durata_anni": None, "importo_primo_anno": None}
    verifica("una spesa normale vale per intero", _quota_annua(normale), 1037.0)
    verifica("un atto senza importo non pesa",
             _quota_annua({"importo_euro": None}), 0)


def test_metadati_scheda():
    """La scheda dell'albo è una tabella con una classe per riga. L'HTML qui
    sotto è copiato dalla pagina vera di un atto del Comune."""
    sezione("Metadati della scheda dell'albo")
    from bs4 import BeautifulSoup
    html = """<table class="table dettaglio-table">
      <tr class="ap-categoria"><td><span class="label label-info">Categoria</span></td>
        <td><span>ATTI AMMINISTRATIVI</span></td></tr>
      <tr class="ap-sottocategoria"><td>Sottocategoria</td><td>DETERMINAZIONE CONTABILE</td></tr>
      <tr class="ap-proponente"><td>Proponente</td><td>AMBIENTE, ECOLOGIA  E SVILUPPO ECONOMICO</td></tr>
      <tr class="ap-dirigente"><td>Dirigente/Firmatario</td><td>Dott. Walter Luigi Vignati</td></tr>
      <tr class="ap-classifica"><td>Classifica</td><td>AMBIENTE: AUTORIZZAZIONI, MONITORAGGIO E CONTROLLO</td></tr>
      <tr class="ap-dataInizioPubblicazione ap-dataPubblicazione"><td>Periodo Pubblicazione</td>
        <td>13/01/2026\n\t\t - \n\t\t31/12/2031</td></tr>
      <tr class="ap-numeroAllegati"><td>Numero allegati</td><td>3</td></tr>
    </table>"""
    m = metadati_scheda(BeautifulSoup(html, "html.parser"))
    verifica("settore proponente", m.get("proponente"),
             "AMBIENTE, ECOLOGIA E SVILUPPO ECONOMICO")
    verifica("classifica tematica", m.get("classifica"),
             "AMBIENTE: AUTORIZZAZIONI, MONITORAGGIO E CONTROLLO")
    verifica("chi firma", m.get("dirigente"), "Dott. Walter Luigi Vignati")
    verifica("tipo di atto dichiarato dal Comune", m.get("sottocategoria"),
             "DETERMINAZIONE CONTABILE")
    verifica("quando scade la pubblicazione", m.get("pubblicato_fino_al"), "31/12/2031")
    verifica("da quando è pubblicato", m.get("pubblicato_dal"), "13/01/2026")
    verifica("una scheda senza tabella non rompe nulla",
             metadati_scheda(BeautifulSoup("<p>niente</p>", "html.parser")), {})


def test_categoria_dal_settore():
    """Il settore che emette l'atto è una classificazione ufficiale, ma solo
    quando è inequivocabile: un'area che tiene insieme ambiente e commercio
    non può decidere, e il testo dell'atto resta più specifico."""
    sezione("Categoria dedotta dal settore proponente")
    for settore, atteso in [
        ("POLIZIA LOCALE", "Polizia locale e sicurezza"),
        ("SETTORE III AREA SERVIZI SOCIALI", "Sociale e famiglia"),
        ("AREA CULTURA, EVENTI E BIBLIOTECA", "Cultura"),
        ("SERVIZIO INFORMATICO COMUNALE", "Amministrazione e servizi generali"),
        ("RAGIONERIA E TRIBUTI", "Amministrazione e servizi generali"),
        # ambigui: due ambiti nello stesso nome, non decide il settore
        ("AMBIENTE, ECOLOGIA  E SVILUPPO ECONOMICO", None),
        ("SETTORE VI AREA COMUNICAZIONE E RELAZIONI ESTERNE,EVENTI,"
         "SERVIZI CULTURALI E SPORTIVI", None),
        # generici: non dicono nulla di utile
        ("AREA TECNICA", None),
        ("SEGRETERIA GENERALE", "Amministrazione e servizi generali"),
        ("", None),
        (None, None),
    ]:
        verifica(f"settore: {str(settore)[:46]}", categoria_da_settore(settore), atteso)


def main() -> int:
    print("Rete di sicurezza sulla lettura degli atti")
    print("Ogni caso qui sotto è un errore vero, trovato guardando il sito.")

    test_numeri_italiani()
    test_soglie_di_legge()
    test_liquidazione_imet()
    test_liquidazione_due_fatture()
    test_liquidazione_iva_non_doppia()
    test_liquidazione_prende_il_lordo()
    test_determina_magna_grecia()
    test_determina_dodici_impegni()
    test_salvaguardie_impegni()
    test_entrate_non_sono_spese()
    test_nomi_beneficiari()
    test_quota_annua()
    test_metadati_scheda()
    test_categoria_dal_settore()

    print()
    if saltati:
        print(f"SALTATI {len(saltati)} casi: manca il testo di {', '.join(sorted(set(saltati)))}")
        print("  (l'archivio data/testi/ non è completo: i casi non sono stati verificati)")
    if falliti:
        print(f"FALLITI {len(falliti)} casi su {passati + len(falliti)}.")
        print("Il comportamento è cambiato: o è una regressione, o il test va aggiornato")
        print("perché la nuova lettura è più giusta. Va deciso, non ignorato.")
        return 1
    print(f"Tutti i {passati} casi passati.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
