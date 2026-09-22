"""
fabbisogni_estrai.py — Fabbisogni standard per SINGOLA FUNZIONE fondamentale.

Il dato aggregato (FC80TOT) dice solo CHE lo scarto fra spesa storica e
fabbisogno standard esiste. I dataset per funzione dicono DOVE si concentra.

I file non sono scaricabili da qui (il portale OpenCivitas è renderizzato in
JavaScript): vanno scaricati a mano da https://www.opencivitas.it/it/open-data
e scompattati in `fabbisogni/`, una sottocartella per funzione, come si fa
con i CSV BDAP in `bilanci/`.

FORMATO DEI FILE (verificato sul rilascio 2022)
  CSV `2022_Ind_FC80<SIGLA>_1.csv`, separatore ';', codifica latin-1, in
  formato LUNGO: una riga per coppia (ente, indicatore).
      USERNAME;Indicatore/Determinante;Valore;Anomalia;Privacy
      AL001SIF11BR;SPESA_STORICA;33318,15;;
  USERNAME NON è il codice ISTAT: la corrispondenza sta in
  `Metadati_Enti_2022.xlsx` (foglio anagrafica_enti_2022).
  `2022_Metadati_Ind_FC80<SIGLA>_1.xlsx` descrive i codici indicatore.

AVVERTENZA SU AMMINISTRAZIONE
  Nel file FC80AMMIN i campi DIFF_OUT_PERC e POSIZIONE_OUTPUT_PERC non si
  riferiscono alla funzione ma all'ente nel complesso: per tutti e sei i
  comuni controllati coincidono con i valori FC80TOT, e manca l'indicatore
  OUT_COMPOSITO_STORICO_X1000AB. Le funzioni generali non hanno un output
  misurabile proprio. Lo script quindi NON attribuisce un livello dei
  servizi all'amministrazione.

Uso:
  python3 scripts/fabbisogni_estrai.py --ispeziona   # struttura dei file
  python3 scripts/fabbisogni_estrai.py               # estrae e aggiorna
"""

import csv
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

CARTELLA = Path("fabbisogni")
CONFRONTI = Path("data/confronti.json")

PIEVE = "015173"
COMUNI = {
    PIEVE: "Pieve Emanuele",
    "015189": "Rozzano",
    "015015": "Basiglio",
    "015115": "Lacchiarella",
    "015125": "Locate di Triulzi",
    "015159": "Opera",
}

# Sigla nel nome del file CSV → etichetta leggibile
FUNZIONI = {
    "AMMIN": "Amministrazione generale",
    "ISTRUZ": "Istruzione",
    "POLIZIA": "Polizia locale",
    "SOCNID": "Sociale e asili nido",
    "TERRVIAB": "Viabilità e territorio",
    "RIFIUTI": "Rifiuti",
    "TOT": "Tutti i servizi",
}

# Funzioni per cui il livello dei servizi nel file NON è della funzione
SENZA_OUTPUT_PROPRIO = {"Amministrazione generale"}

# Codice indicatore → chiave interna (dai metadati ufficiali)
INDICATORI = {
    "SPESA_STORICA": "storica",                      # Spesa storica - Euro
    "FST_RIPROPORZIONATO_BI": "standard",            # Spesa standard - Euro
    "SPESA_STORICA_PROAB": "storica_pro_capite",
    "FST_RIPROPORZIONATO_BI_PROAB": "standard_pro_capite",
    "DIFF_OUT_PERC": "servizi_pct",                  # servizi vs media di fascia
    "POSIZIONE_SPESA_PERC": "livello_spesa",         # 0-10
    "POSIZIONE_OUTPUT_PERC": "livello_servizi",      # 0-10
    "FL_NO_VALUTABILE": "non_valutabile",
}
OBBLIGATORI = ("storica", "standard")

# Dettaglio del sociale: è la funzione che pesa di più sullo scarto, e il
# dato aggregato nasconde la distinzione fra erogare SERVIZI e distribuire
# CONTRIBUTI ECONOMICI. L'indice di output di SOSE pesa soprattutto i primi.
DETTAGLIO_SOCIALE = {
    "SPESA_STORICA_SOC_PROAB": "sociale_pro_capite",
    "SPESA_STORICA_NID_PROAB": "nidi_pro_capite",
    "PERC_COPERTURA_SOC": "copertura_utenti_pct",
    "REDDITO_MEAN": "reddito_imponibile_medio",
    "M40_SOC23_P_X1000AB": "contributi_poverta_x1000ab",
    "M27_SOC23_P_X1000AB": "utenti_poverta_x1000ab",
    "SUPERF_TOT_C": "mq_per_utente_nido",
    "COPERTURA_NID_NEW": "copertura_nido_pct",
    "IND_POP02_SU_POPTOT_PCT": "quota_0_2_anni_pct",
    "INCID_OLTRE_75_MEAN": "quota_over75_pct",
    "DEPRIVAZIONE_MEAN": "deprivazione_vs_italia_pct",
    "SPESA_STORICA_NID": "nidi_totale",
}
ETICHETTE_DETTAGLIO = {
    "sociale_pro_capite": "Spesa sociale al netto dei nidi — € per abitante",
    "nidi_pro_capite": "Spesa per asili nido — € per abitante",
    "copertura_nido_pct": "Bambini 0-2 che frequentano il nido — % dei residenti 0-2",
    "quota_0_2_anni_pct": "Bambini da 0 a 2 anni — % dei residenti",
    "copertura_utenti_pct": "Utenti dei servizi sociali sulla popolazione — %",
    "quota_over75_pct": "Popolazione oltre i 75 anni — % dei residenti",
    "deprivazione_vs_italia_pct": "Indice di deprivazione socio-economica — scostamento % dalla media nazionale",
    "reddito_imponibile_medio": "Reddito imponibile medio ai fini delle addizionali IRPEF — €",
    "contributi_poverta_x1000ab": "Beneficiari di contributi economici per povertà e disagio adulti — per 1.000 abitanti",
    "utenti_poverta_x1000ab": "Utenti di interventi e servizi per povertà e disagio adulti — per 1.000 abitanti",
    "mq_per_utente_nido": "Spazi dell'asilo nido — mq per utente",
}

# Determinanti del modello: le variabili F_* sono il contributo in euro pro
# capite di ciascun fattore al fabbisogno standard, e la loro somma ricostruisce
# esattamente il fabbisogno pubblicato. Servono a rispondere alla domanda
# "perché a questo Comune è stato assegnato QUESTO fabbisogno".
PREFISSO_DETERMINANTE = "F_"
ETICHETTE_DETERMINANTI = {
    "ASILO_NIDO": "Asilo nido",
    "INCID_OLTRE_75_MEAN": "Popolazione oltre i 75 anni",
    "INCID_65_74_MEAN": "Popolazione fra 65 e 74 anni",
    "INCID_15_64_MEAN": "Popolazione in età da lavoro",
    "INCID_POP_STRA_MEAN": "Popolazione straniera",
    "DEPRIVAZIONE_MEAN": "Deprivazione socio-economica",
    "ALUNNI_HANDICAP_MEAN": "Alunni con disabilità",
    "DUMMY_STRUTTURE": "Presenza di strutture sociali",
    "VALORE_BENCHMARK_ORE": "Ore erogate nelle strutture",
    "VALORE_BENCHMARK_UT": "Utenti di interventi e contributi",
    "FASCIA": "Fascia di popolazione",
}


def numero(v):
    """I valori usano la virgola decimale e nessun separatore di migliaia."""
    if v is None:
        return None
    v = str(v).strip().replace(" ", "").replace("€", "")
    if not v:
        return None
    try:
        return float(v.replace(".", "").replace(",", ".") if "," in v else v)
    except ValueError:
        return None


def sigla(percorso: Path) -> str:
    nome = percorso.stem.upper()
    for s in sorted(FUNZIONI, key=len, reverse=True):
        if f"FC80{s}" in nome or f"_{s}_" in nome:
            return s
    return ""


def carica_anagrafica() -> dict:
    """USERNAME → (codice ISTAT, denominazione). Serve openpyxl."""
    try:
        import openpyxl
    except ImportError:
        log.error("Serve openpyxl: pip install openpyxl")
        return {}
    file_xlsx = sorted(CARTELLA.rglob("Metadati_Enti_*.xlsx"))
    if not file_xlsx:
        log.error(f"Manca Metadati_Enti_*.xlsx in {CARTELLA}/: senza l'anagrafica "
                  f"gli USERNAME non sono traducibili in comuni.")
        return {}
    ws = openpyxl.load_workbook(file_xlsx[0], data_only=True).active
    righe = ws.iter_rows(values_only=True)
    h = list(next(righe))
    try:
        iu, ii, ie = h.index("USERNAME"), h.index("COMUNE_ISTAT_COD"), h.index("ENTE")
    except ValueError:
        log.error(f"{file_xlsx[0].name}: intestazione inattesa {h}")
        return {}
    mappa = {r[iu]: (str(r[ii]).zfill(6), str(r[ie]).title())
             for r in righe if r[iu] and r[ii]}
    log.info(f"Anagrafica: {len(mappa)} enti da {file_xlsx[0].name}")
    return mappa


def leggi_funzione(percorso: Path, anagrafica: dict, extra: dict | None = None) -> dict:
    """Pivot del formato lungo: {codice ISTAT: {chiave: valore}} per i comuni cercati."""
    voluti = {u: i for u, (i, _) in anagrafica.items() if i in COMUNI}
    campi = {**INDICATORI, **(extra or {})}
    dati: dict[str, dict] = {}
    with open(percorso, encoding="latin-1", newline="") as fh:
        lettore = csv.reader(fh, delimiter=";")
        next(lettore, None)
        for riga in lettore:
            if len(riga) < 3 or riga[0] not in voluti:
                continue
            chiave = campi.get(riga[1])
            if chiave:
                dati.setdefault(voluti[riga[0]], {})[chiave] = riga[2]
    return dati


def componi(funzione: str, grezzi: dict) -> list[dict]:
    righe = []
    for istat, valori in grezzi.items():
        storica, standard = numero(valori.get("storica")), numero(valori.get("standard"))
        if storica is None or not standard:
            log.warning(f"  {COMUNI[istat]}: {funzione} senza spesa storica o standard, salto")
            continue
        if numero(valori.get("non_valutabile")):
            log.warning(f"  {COMUNI[istat]}: {funzione} marcata non valutabile dalla fonte")
        voce = {
            "nome": COMUNI[istat],
            "istat": istat,
            "storica": round(storica, 2),
            "standard": round(standard, 2),
            "scarto_euro": round(storica - standard, 2),
            "scarto_pct": round((storica - standard) / standard * 100, 2),
            "pro_capite_storica": round(numero(valori.get("storica_pro_capite")) or 0, 2),
            "pro_capite_standard": round(numero(valori.get("standard_pro_capite")) or 0, 2),
            "livello_spesa": numero(valori.get("livello_spesa")),
            "evidenzia": istat == PIEVE,
        }
        if funzione in SENZA_OUTPUT_PROPRIO:
            voce["servizi_pct"] = None
            voce["livello_servizi"] = None
            voce["nota_servizi"] = ("Nella fonte il livello dei servizi di questa funzione "
                                    "riporta il dato complessivo dell'ente, non quello della "
                                    "funzione: qui è omesso.")
        else:
            voce["servizi_pct"] = round(numero(valori.get("servizi_pct")) or 0, 2)
            voce["livello_servizi"] = numero(valori.get("livello_servizi"))
        righe.append(voce)
    return sorted(righe, key=lambda x: -x["scarto_euro"])


def scomponi_fabbisogno(grezzi: dict) -> list[dict]:
    """Perché a Pieve è stato assegnato QUEL fabbisogno: confronta ogni
    determinante con la media dei comuni dell'ambito. La somma dei contributi
    ricostruisce il fabbisogno pro capite pubblicato (controllo in log)."""
    if PIEVE not in grezzi:
        return []
    altri = [i for i in grezzi if i != PIEVE]
    if not altri:
        return []
    voci = []
    for codice, etichetta in ETICHETTE_DETERMINANTI.items():
        chiave = f"det_{codice}"
        p = numero(grezzi[PIEVE].get(chiave))
        if p is None:
            continue
        valori = [numero(grezzi[i].get(chiave)) for i in altri]
        valori = [v for v in valori if v is not None]
        if not valori:
            continue
        media = sum(valori) / len(valori)
        voci.append({
            "determinante": etichetta,
            "pieve": round(p, 2),
            "media_ambito": round(media, 2),
            "scarto": round(p - media, 2),
        })
    voci.sort(key=lambda v: v["scarto"])
    somma = sum(v["pieve"] for v in voci)
    atteso = numero(grezzi[PIEVE].get("standard_pro_capite"))
    if atteso and abs(somma - atteso) > 0.5:
        log.warning(f"  I determinanti sommano {somma:.2f} €/ab ma il fabbisogno "
                    f"pubblicato è {atteso:.2f}: scomposizione incompleta, non pubblicata.")
        return []
    log.info(f"\nSCOMPOSIZIONE DEL FABBISOGNO SOCIALE (somma determinanti {somma:.1f} "
             f"€/ab = fabbisogno pubblicato {atteso:.1f} €/ab)")
    for v in voci:
        if abs(v["scarto"]) >= 0.2:
            log.info(f"  {v['determinante']:32} Pieve {v['pieve']:7.1f} · "
                     f"ambito {v['media_ambito']:7.1f} → {v['scarto']:+7.1f} €/ab")
    return voci


def costo_per_utente_nido(grezzi: dict) -> list[dict]:
    """Stima: spesa per il nido divisa per i bambini effettivamente iscritti,
    ricostruiti da popolazione × quota 0-2 × copertura. È una STIMA derivata da
    tre indicatori, non un dato pubblicato: l'ordine di grandezza è affidabile,
    la cifra esatta no."""
    righe = []
    for istat, v in grezzi.items():
        spesa, pro_ab = numero(v.get("nidi_totale")), numero(v.get("storica_pro_capite"))
        quota, cop = numero(v.get("quota_0_2_anni_pct")), numero(v.get("copertura_nido_pct"))
        totale = numero(v.get("storica"))
        if not all([spesa, pro_ab, quota, cop, totale]):
            continue
        popolazione = totale / pro_ab
        utenti = popolazione * quota / 100 * cop / 100
        if utenti < 5:
            continue
        righe.append({
            "nome": COMUNI[istat],
            "popolazione_stimata": round(popolazione),
            "bambini_0_2": round(popolazione * quota / 100),
            "copertura_pct": round(cop, 1),
            "utenti_stimati": round(utenti),
            "spesa_nido": round(spesa, 2),
            "costo_per_utente": round(spesa / utenti),
            "evidenzia": istat == PIEVE,
        })
    righe.sort(key=lambda r: -r["costo_per_utente"])
    if righe:
        log.info("\nCOSTO STIMATO PER BAMBINO AL NIDO")
        for r in righe:
            log.info(f"  {r['nome']:20} {r['utenti_stimati']:>4} bambini su "
                     f"{r['bambini_0_2']:>4} residenti 0-2 ({r['copertura_pct']:>4.1f}%) · "
                     f"{r['costo_per_utente']:>8,.0f} € ciascuno")
    return righe


def ispeziona() -> int:
    file_csv = sorted(CARTELLA.rglob("*.csv"))
    if not file_csv:
        log.error(f"Nessun CSV in {CARTELLA}/.")
        return 1
    anagrafica = carica_anagrafica()
    for p in file_csv:
        s = sigla(p)
        indicatori, enti = set(), set()
        with open(p, encoding="latin-1", newline="") as fh:
            lettore = csv.reader(fh, delimiter=";")
            intestazione = next(lettore, [])
            for riga in lettore:
                if len(riga) > 1:
                    indicatori.add(riga[1])
                    enti.add(riga[0])
        trovati = {k for k in INDICATORI if k in indicatori}
        mancanti = {INDICATORI[k] for k in INDICATORI} - {INDICATORI[k] for k in trovati}
        log.info(f"── {p.relative_to(CARTELLA)}")
        log.info(f"   funzione: {FUNZIONI.get(s, '??? sigla non riconosciuta')}")
        log.info(f"   intestazione: {intestazione} · {len(enti)} enti · {len(indicatori)} indicatori")
        log.info(f"   indicatori utili trovati: {sorted(trovati)}")
        if mancanti:
            log.info(f"   assenti: {sorted(mancanti)}")
        if anagrafica:
            presenti = [COMUNI[i] for u, (i, _) in anagrafica.items()
                        if i in COMUNI and u in enti]
            log.info(f"   comuni cercati presenti: {len(presenti)}/{len(COMUNI)}")
        log.info("")
    return 0


def estrai() -> int:
    file_csv = sorted(CARTELLA.rglob("*.csv"))
    if not file_csv:
        log.error(f"Nessun CSV in {CARTELLA}/. Vedi --ispeziona.")
        return 1
    anagrafica = carica_anagrafica()
    if not anagrafica:
        return 1

    per_funzione: dict[str, list[dict]] = {}
    dettaglio_sociale: dict = {}
    scomposizione: list = []
    costo_nido: list = []
    for p in file_csv:
        s = sigla(p)
        if not s:
            log.warning(f"{p.name}: sigla di funzione non riconosciuta, salto")
            continue
        funzione = FUNZIONI[s]
        extra = DETTAGLIO_SOCIALE if s == "SOCNID" else None
        determinanti = {f"{PREFISSO_DETERMINANTE}{k}": f"det_{k}"
                        for k in ETICHETTE_DETERMINANTI} if s == "SOCNID" else None
        grezzi = leggi_funzione(p, anagrafica, {**(extra or {}), **(determinanti or {})})
        if extra:
            dettaglio_sociale = {
                COMUNI[istat]: {chiave: numero(v.get(chiave))
                                for chiave in ETICHETTE_DETTAGLIO if chiave in v}
                for istat, v in grezzi.items()
            }
            scomposizione = scomponi_fabbisogno(grezzi)
            costo_nido = costo_per_utente_nido(grezzi)
        righe = componi(funzione, grezzi)
        if not righe:
            log.warning(f"{funzione}: nessun comune trovato, salto")
            continue
        per_funzione[funzione] = righe
        pieve = next((x for x in righe if x["istat"] == PIEVE), None)
        if pieve:
            log.info(f"{funzione:26} spesa {pieve['storica']:>12,.0f} € · "
                     f"fabbisogno {pieve['standard']:>12,.0f} € · "
                     f"scarto {pieve['scarto_pct']:+7.1f}% ({pieve['scarto_euro']:+,.0f} €)")

    if not per_funzione:
        log.error("Nessun dato estratto.")
        return 1

    dati = json.loads(CONFRONTI.read_text(encoding="utf-8")) if CONFRONTI.exists() else {}

    # QUADRATURA: la somma delle funzioni deve ricostruire il totale FC80TOT,
    # che sta già in confronti.json (serie 2022 + rifiuti_2022). Se non torna,
    # il dato per funzione non è utilizzabile per ripartire lo scarto.
    quadratura = None
    fab = dati.get("fabbisogni", {})
    tot = next((x for x in fab.get("serie", []) if x.get("anno") == 2022), None)
    rif = fab.get("rifiuti_2022")
    if tot and rif:
        somma_storica = sum(r["storica"] for f, v in per_funzione.items()
                            if f != "Tutti i servizi"
                            for r in v if r["istat"] == PIEVE) + rif["storica"]
        somma_standard = sum(r["standard"] for f, v in per_funzione.items()
                             if f != "Tutti i servizi"
                             for r in v if r["istat"] == PIEVE) + rif["standard"]
        quadratura = {
            "storica_somma_funzioni": round(somma_storica, 2),
            "storica_totale_fonte": tot["storica"],
            "storica_scarto_pct": round((somma_storica / tot["storica"] - 1) * 100, 3),
            "standard_somma_funzioni": round(somma_standard, 2),
            "standard_totale_fonte": tot["standard"],
            "standard_scarto_pct": round((somma_standard / tot["standard"] - 1) * 100, 3),
        }
        log.info("\nQUADRATURA con il totale FC80TOT (rifiuti inclusi)")
        log.info(f"  spesa storica   somma {somma_storica:>14,.2f} € vs "
                 f"totale {tot['storica']:>14,.2f} € → {quadratura['storica_scarto_pct']:+.3f}%")
        log.info(f"  spesa standard  somma {somma_standard:>14,.2f} € vs "
                 f"totale {tot['standard']:>14,.2f} € → {quadratura['standard_scarto_pct']:+.3f}%")
        if abs(quadratura["storica_scarto_pct"]) > 0.5:
            log.warning("  ATTENZIONE: la spesa storica per funzione non ricostruisce il "
                        "totale. Le quote di ripartizione non sono affidabili.")

        # Quanto ciascuna funzione pesa sullo scarto complessivo
        scarto_tot = tot["storica"] - tot["standard"]
        log.info(f"\nDOVE SI CONCENTRA LO SCARTO ({scarto_tot:+,.0f} €)")
        contributi = [(f, next(r for r in v if r["istat"] == PIEVE))
                      for f, v in per_funzione.items()
                      if f != "Tutti i servizi" and any(r["istat"] == PIEVE for r in v)]
        contributi.append(("Rifiuti", {"scarto_euro": rif["storica"] - rif["standard"]}))
        for f, r in sorted(contributi, key=lambda kv: -kv[1]["scarto_euro"]):
            quota = r["scarto_euro"] / scarto_tot * 100 if scarto_tot else 0
            log.info(f"  {f:26} {r['scarto_euro']:>+12,.0f} €  ({quota:+6.1f}% dello scarto)")
            if f in per_funzione:
                for riga in per_funzione[f]:
                    if riga["istat"] == PIEVE:
                        riga["quota_scarto_pct"] = round(quota, 2)

    dati["funzioni_2022"] = {
        "fonte": "OpenCivitas · SOSE per RGS e Dipartimento delle Finanze — "
                 "indicatori per funzione fondamentale, rilascio 2022 (FC80)",
        "licenza": "CC BY 4.0",
        "url": "https://www.opencivitas.it/it/open-data",
        "avvertenza": "Lo scarto fra spesa storica e fabbisogno standard NON misura lo "
                      "spreco: dice solo quanto il Comune si scosta da ciò che, dati i suoi "
                      "prezzi, la sua morfologia e la sua utenza, spende in media un comune "
                      "comparabile. Il livello dei servizi è calcolato sulla media della "
                      "fascia di popolazione, non su uno standard assoluto.",
        "nota_amministrazione": "Per l'amministrazione generale la fonte non pubblica un "
                                "livello dei servizi della funzione: i campi di output "
                                "riportano il dato complessivo dell'ente. Qui sono omessi.",
        "quadratura": quadratura,
        "funzioni": per_funzione,
        "dettaglio_sociale": {
            "nota": "Il sociale è la funzione che pesa di più sullo scarto. Questi "
                    "indicatori distinguono l'erogazione di SERVIZI dalla distribuzione "
                    "di CONTRIBUTI ECONOMICI: l'indice dei servizi calcolato da SOSE pesa "
                    "soprattutto i primi, quindi un comune che sceglie i trasferimenti "
                    "diretti risulta spendere molto e servire poco.",
            "capofila_ambito": "Rozzano è il Comune capofila dell'ambito distrettuale "
                               "Visconteo Sud Milano: la spesa sociale gestita in forma "
                               "associata transita dal suo bilancio, non da quello di Pieve "
                               "Emanuele. Ciò esclude che il dato di Pieve sia gonfiato "
                               "dalla funzione di capofila.",
            "anomalie_dichiarate": "La fonte marca come anomali, per Pieve Emanuele, gli "
                                   "indicatori sul costo del lavoro e sul numero di "
                                   "dipendenti del sociale: non sono usati qui.",
            "etichette": ETICHETTE_DETTAGLIO,
            "comuni": dettaglio_sociale,
            "scomposizione_fabbisogno": {
                "nota": "Contributo di ciascun determinante del modello al fabbisogno "
                        "standard pro capite del sociale, confrontato con la media dei "
                        "cinque comuni dell'ambito. La somma dei contributi ricostruisce "
                        "il fabbisogno pubblicato. Il reddito NON è fra i determinanti: "
                        "il modello usa l'indice di deprivazione socio-economica.",
                "voci": scomposizione,
            },
            "costo_nido": {
                "nota": "STIMA: spesa per asili nido divisa per i bambini iscritti, "
                        "ricostruiti come popolazione × quota residenti 0-2 × copertura. "
                        "Deriva da tre indicatori pubblicati, non è un dato ufficiale: "
                        "affidabile nell'ordine di grandezza, non nella cifra esatta. "
                        "Basiglio va letto a parte: la spesa comunale per il nido è "
                        "quasi nulla, segno che il servizio non grava sul suo bilancio.",
                "comuni": costo_nido,
            },
        },
    }
    CONFRONTI.write_text(json.dumps(dati, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"\nAggiornato {CONFRONTI} con {len(per_funzione)} funzioni.")
    log.info("Ora rilancia: python3 scripts/genera_sito.py")
    return 0


if __name__ == "__main__":
    sys.exit(ispeziona() if "--ispeziona" in sys.argv else estrai())
