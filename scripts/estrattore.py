"""
estrattore.py — Estrazione dati strutturati dalle determine/liquidazioni.

Usa Groq (Llama) in modalità JSON per trasformare il testo burocratico in:
  beneficiario, importo_euro, cig, categoria, descrizione_sintetica,
  capitolo_bilancio.

Le categorie sono allineate alle Missioni del bilancio armonizzato (BDAP),
così la fase 2 (riconciliazione preventivo/consuntivo) mappa 1:1.

Se GROQ_API_KEY manca o l'API fallisce, ripiega su euristiche regex
(meno precise, marcate con estrazione="regex").
"""

import os
import re
import json
import time
import random
import logging

log = logging.getLogger(__name__)

# L'8B è il default: sul free tier il 70B ha TPM così bassi che ogni chiamata
# finisce in 429 (verificato nel backfill del 2026-07-16); l'8B risponde
# stabilmente e per un'estrazione JSON strutturata è più che sufficiente.
MODELLO_DEFAULT = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
MODELLO_RISERVA = "llama-3.3-70b-versatile"

# Pausa tra chiamate, ADATTIVA: cresce a ogni 429, si riassesta sui successi.
# Il vero collo di bottiglia del free tier è il TPM (token/minuto).
PAUSA_TRA_CHIAMATE = float(os.environ.get("GROQ_PAUSA", "2.5"))
_pausa_corrente = PAUSA_TRA_CHIAMATE
TESTO_MAX_CHARS = 7_000  # ~2.000 token/chiamata: raddoppia la resa sul TPM


RE_HA_IMPORTO = re.compile(
    r"(€|euro\b|importo|totale|iva|imponibile|impegn|liquidaz|fattura|cig)",
    re.IGNORECASE)


def _riduci_testo(testo: str, max_chars: int = TESTO_MAX_CHARS) -> str:
    """
    Riduzione che PRESERVA le righe con gli importi.
    Negli atti gli importi stanno spesso in tabelle a metà documento
    (pdfplumber le rende come righe "a | b | c"): tagliare testa+coda le
    perdeva. Qui teniamo: intestazione + tutte le righe che contengono
    importi/parole chiave + coda (dispositivo).
    """
    if len(testo) <= max_chars:
        return testo

    righe = testo.splitlines()
    quota_testa = int(max_chars * 0.3)
    testa, usato = [], 0
    for r in righe:
        if usato + len(r) > quota_testa:
            break
        testa.append(r)
        usato += len(r) + 1
    n_testa = len(testa)

    quota_coda = int(max_chars * 0.25)
    coda, usato = [], 0
    for r in reversed(righe[n_testa:]):
        if usato + len(r) > quota_coda:
            break
        coda.insert(0, r)
        usato += len(r) + 1
    n_coda = len(coda)

    centro = righe[n_testa: len(righe) - n_coda] if n_coda else righe[n_testa:]
    rilevanti, usato = [], 0
    disponibile = max_chars - quota_testa - quota_coda
    for r in centro:
        if not RE_HA_IMPORTO.search(r):
            continue
        if usato + len(r) > disponibile:
            break
        rilevanti.append(r)
        usato += len(r) + 1

    parti = ["\n".join(testa)]
    if rilevanti:
        parti.append("[... righe rilevanti dal corpo dell'atto ...]")
        parti.append("\n".join(rilevanti))
    if coda:
        parti.append("[... ...]")
        parti.append("\n".join(coda))
    return "\n".join(parti)


def importo_italiano(valore) -> float | None:
    """
    Converte un importo scritto all'italiana ("8.540,00", "121.530.402",
    "1.234,56 €") in float. Regole:
      - se c'è una virgola, è il separatore decimale e i punti sono migliaia
      - se ci sono solo punti: sono migliaia se raggruppano cifre a 3
        ("8.540" → 8540; "1.234.567" → 1234567), decimali solo se il gruppo
        finale non ha 3 cifre ("8.5" → 8.5)
    È QUESTA funzione a decidere il valore, non il modello: i LLM sbagliano
    sistematicamente il formato italiano (8.540,00 letto come 8.54).
    """
    if valore is None:
        return None
    if isinstance(valore, (int, float)):
        v = float(valore)
        return round(v, 2) if v > 0 else None

    s = str(valore).strip()
    s = re.sub(r"(?i)(€|euro|eur|iva|inclusa|esclusa|compresa)", " ", s)
    s = re.sub(r"[^\d.,\-]", "", s).strip()
    if not s or s in ("-", ".", ","):
        return None

    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        gruppi = s.split(".")
        # migliaia se tutti i gruppi dopo il primo hanno esattamente 3 cifre
        if all(len(g) == 3 for g in gruppi[1:]):
            s = "".join(gruppi)
        # altrimenti resta un decimale anglosassone
    try:
        v = float(s)
    except ValueError:
        return None
    return round(v, 2) if v > 0 else None

# Modelli disattivati per il resto del run (3 fallimenti consecutivi)
_MODELLI_SALTATI: set[str] = set()
_FALLIMENTI_CONSECUTIVI: dict[str, int] = {}

# Categorie ammesse (chiave = etichetta mostrata sul sito, valore = Missione BDAP)
CATEGORIE = {
    "Amministrazione e servizi generali": 1,
    "Polizia locale e sicurezza": 3,
    "Istruzione e scuola": 4,
    "Cultura": 5,
    "Sport e tempo libero": 6,
    "Turismo": 7,
    "Urbanistica e casa": 8,
    "Ambiente, verde e rifiuti": 9,
    "Strade, viabilità e trasporti": 10,
    "Protezione civile": 11,
    "Sociale e famiglia": 12,
    "Sanità": 13,
    "Sviluppo economico e commercio": 14,
    "Lavoro": 15,
    "Debito e anticipazioni": 50,
    "Da classificare": 99,
}

PROMPT_SISTEMA = """Sei un estrattore di dati da atti amministrativi comunali italiani (determinazioni contabili e liquidazioni).
Rispondi SOLO con un oggetto JSON valido, senza testo aggiuntivo, con questo schema:

{
  "tipo_atto": "determinazione" oppure "liquidazione",
  "beneficiario": "chi riceve i soldi (string, mai null: vedi REGOLE BENEFICIARIO)",
  "n_beneficiari": numero di soggetti che ricevono i soldi (number, 1 se uno solo),
  "beneficiari_dettaglio": [ {"nome": "...", "importo_testuale": "1.234,56"} ],
  "importo_testuale": "l'importo TOTALE copiato ESATTAMENTE come appare nell'atto, es. \\"8.540,00\\" (string, null se assente)",
  "importo_e_pluriennale": true/false,
  "durata_anni": numero di anni coperti dalla spesa (number, null se non pluriennale),
  "importo_primo_anno_testuale": "importo del primo anno copiato esattamente (string, null se non indicato)",
  "iva_inclusa": true/false/null,
  "cig": "Codice Identificativo Gara (string, null se assente)",
  "capitolo_bilancio": "capitolo/i di bilancio citati (string, null se assenti)",
  "descrizione_sintetica": "una frase semplice, max 25 parole, che spiega a un cittadino cosa paga il Comune e perché",
  "categoria": una tra le categorie elencate sotto (string, esattamente come scritta)
}

REGOLE IMPORTI (le più importanti):
- COPIA l'importo come stringa ESATTAMENTE come scritto nell'atto, con i suoi punti e virgole: "8.540,00", "121.530.402,00", "1.300,00". NON convertirlo, NON arrotondarlo, NON toglierne i separatori. In italiano il PUNTO separa le migliaia e la VIRGOLA i decimali.
- Gli importi spesso stanno in una TABELLA (colonne come "Importo", "Importo Iva comp.", "Totale"): leggila e usala. Se la tabella elenca più righe/fatture, SOMMA gli importi positivi delle righe e scrivi la somma in importo_testuale in formato italiano; elenca ogni riga in beneficiari_dettaglio.
- Ignora le righe di sola IVA a favore dell'erario ("ESATTORIA - IVA", "scissione dei pagamenti", "split payment") e gli importi negativi di storno: non sono spesa aggiuntiva.
- Se l'atto impegna una spesa per PIÙ ANNI (es. "durata 15 anni", "triennio"), metti importo_e_pluriennale=true, indica durata_anni, e se l'atto specifica quanto vale il primo anno mettilo in importo_primo_anno_testuale. In importo_testuale metti comunque il TOTALE dell'affidamento.
- Non confondere importi citati come riferimento (impegni precedenti, quadri economici, importi di gara) con quanto questo atto effettivamente impegna o liquida.

REGOLE BENEFICIARIO:
- Se c'è un fornitore o una ditta, usa la sua ragione sociale.
- Se i beneficiari sono più di uno, in "beneficiario" scrivi una sintesi leggibile (es. "7 fornitori", "12 dipendenti comunali") e metti l'elenco completo in beneficiari_dettaglio.
- Se il beneficiario è una persona fisica, NON scrivere il nome: usa una descrizione della categoria (es. "un cittadino con disabilità", "3 famiglie in difficoltà", "un dipendente comunale").
- Se sono dipendenti o amministratori, scrivi la categoria (es. "personale amministrativo", "personale della polizia locale", "amministratori comunali").
- Se davvero non si capisce chi riceve i soldi, scrivi una descrizione dello scopo (es. "rimborsi tributi ai contribuenti"). Non lasciare mai il campo vuoto o null.

ALTRE REGOLE:
- Non inventare: se un dato non c'è, usa null (tranne beneficiario, vedi sopra).
- categoria: scegli quella che meglio descrive l'AMBITO della spesa (rifiuti → Ambiente; scuola → Istruzione; strade e manutenzione stradale → Strade; edilizia e patrimonio → Urbanistica).

CATEGORIE AMMESSE:
""" + "\n".join(f"- {c}" for c in CATEGORIE)


# Sotto questa soglia il testo dell'atto è inutilizzabile: senza dati il
# modello INVENTA importi plausibili, e con temperature=0 inventa sempre lo
# stesso (nel run del 2026-07 ha prodotto 121.530.402,00 su 10 atti diversi).
TESTO_MIN_UTILE = 300


def estrai_dati(testo: str, oggetto: str, tipo_portale: str) -> dict:
    """
    Estrae i dati strutturati della spesa. Prova Groq, poi regex.
    Restituisce sempre un dict con le chiavi dello schema + "estrazione".
    Se il testo dell'atto non è disponibile, gli importi restano null:
    mai inventati dal modello.
    """
    testo_grezzo = testo or ""
    testo = _riduci_testo(testo_grezzo)
    testo_utile = len(testo_grezzo.strip()) >= TESTO_MIN_UTILE
    risultato = None

    if os.environ.get("GROQ_API_KEY"):
        risultato = _estrai_con_groq(testo, oggetto)

    if risultato is None:
        risultato = _estrai_con_regex(testo, oggetto)
        risultato["estrazione"] = "regex"
    else:
        risultato["estrazione"] = "groq"

    # ── Normalizzazioni difensive ────────────────────────────────────────
    risultato["tipo_atto"] = _normalizza_tipo(risultato.get("tipo_atto"), tipo_portale)

    # SENZA TESTO NON CI SONO IMPORTI. Il modello, interrogato sul solo
    # oggetto, produce cifre verosimili e sempre identiche fra loro:
    # meglio "importo n.d." che un numero inventato.
    risultato["testo_disponibile"] = testo_utile
    if not testo_utile:
        for campo in ("importo_testuale", "importo_euro", "importo_primo_anno_testuale",
                      "beneficiari_dettaglio", "cig", "capitolo_bilancio"):
            risultato[campo] = None
        risultato["importo_e_pluriennale"] = False
        risultato["durata_anni"] = None
        risultato["iva_inclusa"] = None
        log.warning("  Testo dell'atto non disponibile: importi azzerati "
                    "(niente valori inventati)")

    # L'importo lo decidiamo NOI dalla stringa testuale (i LLM sbagliano il
    # formato italiano); il numero del modello è solo un ripiego.
    imp = importo_italiano(risultato.get("importo_testuale"))
    if imp is None:
        imp = importo_italiano(risultato.get("importo_euro"))
    risultato["importo_euro"] = imp
    risultato["importo_primo_anno"] = importo_italiano(
        risultato.get("importo_primo_anno_testuale"))

    # Beneficiari multipli: normalizza la lista e, se manca il totale,
    # ricavalo dalla somma delle voci.
    dettaglio = risultato.get("beneficiari_dettaglio")
    voci = []
    if isinstance(dettaglio, list):
        for v in dettaglio[:60]:
            if not isinstance(v, dict):
                continue
            nome = (v.get("nome") or "").strip()
            valore = importo_italiano(v.get("importo_testuale") or v.get("importo"))
            if nome or valore:
                voci.append({"nome": nome or "—", "importo": valore})
    risultato["beneficiari_dettaglio"] = voci or None
    if risultato["importo_euro"] is None and voci:
        somma = sum(v["importo"] for v in voci if v["importo"])
        if somma > 0:
            risultato["importo_euro"] = round(somma, 2)

    # Conta i soggetti DISTINTI (le tabelle ripetono lo stesso fornitore su
    # più righe/fatture: non sono beneficiari diversi)
    nomi_distinti = {v["nome"].strip().lower() for v in voci if v["nome"] != "—"}
    try:
        n_ben = int(risultato.get("n_beneficiari"))
    except (TypeError, ValueError):
        n_ben = 0
    risultato["n_beneficiari"] = max(n_ben, len(nomi_distinti), 1)

    # Pluriennale
    risultato["importo_e_pluriennale"] = bool(risultato.get("importo_e_pluriennale"))
    try:
        durata = int(risultato.get("durata_anni"))
        risultato["durata_anni"] = durata if 1 < durata <= 50 else None
    except (TypeError, ValueError):
        risultato["durata_anni"] = None
    if risultato["durata_anni"]:
        risultato["importo_e_pluriennale"] = True
    if not risultato["importo_e_pluriennale"]:
        risultato["importo_primo_anno"] = None

    if risultato.get("categoria") not in CATEGORIE:
        risultato["categoria"] = "Da classificare"
    risultato["missione_bdap"] = CATEGORIE[risultato["categoria"]]
    for k in ("beneficiario", "cig", "capitolo_bilancio", "descrizione_sintetica",
              "importo_testuale"):
        v = risultato.get(k)
        risultato[k] = v.strip() if isinstance(v, str) and v.strip() else None
    risultato["iva_inclusa"] = (risultato.get("iva_inclusa")
                                if isinstance(risultato.get("iva_inclusa"), bool) else None)
    return risultato


# ─────────────────────────────────────────────────────────────────────────────
# GROQ
# ─────────────────────────────────────────────────────────────────────────────

def _chiama_modello(client, modello: str, testo: str, oggetto: str) -> dict | None:
    """
    Una estrazione con un singolo modello. Gestione errori differenziata:
      - 413 (payload troppo grande): deterministico → dimezza il testo e
        ritenta subito, mai backoff
      - 429 (rate limit): backoff esponenziale con jitter
      - altro: non recuperabile, esci subito
    """
    global _pausa_corrente
    testo_corrente = testo
    for tentativo in range(3):
        prompt_utente = (f"Oggetto dell'atto: {oggetto}\n\nTesto dell'atto:\n"
                         f"{testo_corrente if testo_corrente else '(testo non disponibile: deduci il possibile dal solo oggetto)'}")
        try:
            risposta = client.chat.completions.create(
                model=modello,
                max_tokens=500,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": PROMPT_SISTEMA},
                    {"role": "user", "content": prompt_utente},
                ],
            )
            # Successo: la pausa adattiva si riassesta lentamente verso la base
            _pausa_corrente = max(_pausa_corrente * 0.9, PAUSA_TRA_CHIAMATE)
            time.sleep(_pausa_corrente)
            dati = json.loads(risposta.choices[0].message.content)
            if isinstance(dati, dict):
                return dati
            log.warning(f"  {modello}: JSON non-dict, riprovo")
        except json.JSONDecodeError as e:
            log.warning(f"  {modello}: JSON malformato ({e}), tentativo {tentativo+1}")
        except Exception as e:
            messaggio = str(e)
            if "413" in messaggio or "too large" in messaggio.lower():
                testo_corrente = _riduci_testo(testo_corrente, len(testo_corrente) // 2)
                log.info(f"  {modello}: payload troppo grande, riduco il testo "
                         f"a {len(testo_corrente)} char")
                if len(testo_corrente) < 500:
                    return None
                continue  # ritenta subito: niente attesa
            if "429" in messaggio or "rate" in messaggio.lower():
                # Il TPM è saturo: alza la pausa di regime per le prossime chiamate
                _pausa_corrente = min(_pausa_corrente * 1.5, 30.0)
                attesa = (2 ** tentativo) * 5 + random.uniform(0, 3)
                log.warning(f"  {modello}: rate limit, attendo {attesa:.0f}s "
                            f"(tentativo {tentativo+1}, pausa di regime → "
                            f"{_pausa_corrente:.1f}s)")
                time.sleep(attesa)
                continue
            log.error(f"  {modello}: errore non recuperabile: {e}")
            return None
    return None


def _estrai_con_groq(testo: str, oggetto: str) -> dict | None:
    from groq import Groq
    # max_retries=0: i retry li gestiamo noi (l'SDK ritenterebbe anche i 413,
    # che sono deterministici e non vanno mai ritentati uguali)
    client = Groq(api_key=os.environ["GROQ_API_KEY"], max_retries=0)

    for modello in (MODELLO_DEFAULT, MODELLO_RISERVA):
        if modello in _MODELLI_SALTATI:
            continue
        risultato = _chiama_modello(client, modello, testo, oggetto)
        if risultato is not None:
            _FALLIMENTI_CONSECUTIVI[modello] = 0
            return risultato
        _FALLIMENTI_CONSECUTIVI[modello] = _FALLIMENTI_CONSECUTIVI.get(modello, 0) + 1
        if _FALLIMENTI_CONSECUTIVI[modello] >= 3:
            _MODELLI_SALTATI.add(modello)
            log.warning(f"  Modello {modello} disattivato per il resto del run "
                        f"(3 fallimenti consecutivi)")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK REGEX
# ─────────────────────────────────────────────────────────────────────────────

RE_IMPORTO = re.compile(
    r"(?:€|euro|eur)\s*\.?\s*([\d.]{1,12},\d{2})|([\d.]{1,12},\d{2})\s*(?:€|euro|eur)",
    re.IGNORECASE,
)
RE_CIG = re.compile(r"\bCIG[:\s.]*([A-Z0-9]{10})\b", re.IGNORECASE)
RE_BENEFICIARIO = re.compile(
    r"(?:a favore (?:di|della|del)|ditta|società|societa'?)\s+([A-Z][A-Za-z0-9&.'\s]{3,60}?)(?:[,;\n]|con sede|P\.?\s?IVA|C\.?F\.?)",
)


# Soglie normative citate nel boilerplate degli atti (Codice dei contratti):
# NON sono importi di spesa. Il fallback regex le prendeva per buone,
# attribuendo 140.000,00 € a 26 atti diversi.
SOGLIE_NORMATIVE = {40_000.0, 139_000.0, 140_000.0, 143_000.0, 150_000.0,
                    200_000.0, 215_000.0, 221_000.0, 750_000.0, 1_000_000.0,
                    5_382_000.0, 5_538_000.0}

RE_CONTESTO_SPESA = re.compile(
    r"(impegn\w*|liquid\w*|affid\w*|spesa complessiva|importo complessivo|"
    r"per un totale|corrispettivo)", re.IGNORECASE)


def _estrai_con_regex(testo: str, oggetto: str) -> dict:
    completo = f"{oggetto}\n{testo}"

    # Cerca gli importi preferendo quelli in un contesto di spesa effettiva
    # (impegno/liquidazione/affidamento) entro i 120 caratteri precedenti.
    candidati_contesto, candidati_tutti = [], []
    for m in RE_IMPORTO.finditer(completo):
        raw = m.group(1) or m.group(2)
        v = importo_italiano(raw)
        if not v or v in SOGLIE_NORMATIVE:
            continue
        candidati_tutti.append(v)
        if RE_CONTESTO_SPESA.search(completo[max(0, m.start() - 120): m.start()]):
            candidati_contesto.append(v)
    importi = candidati_contesto or candidati_tutti
    importo = max(importi) if importi else None

    cig = None
    m = RE_CIG.search(completo)
    if m:
        cig = m.group(1).upper()

    beneficiario = None
    m = RE_BENEFICIARIO.search(completo)
    if m:
        beneficiario = m.group(1).strip()

    return {
        "tipo_atto": None,
        "beneficiario": beneficiario,
        "n_beneficiari": 1,
        "beneficiari_dettaglio": None,
        "importo_testuale": None,
        "importo_euro": importo,
        "importo_e_pluriennale": False,
        "durata_anni": None,
        "importo_primo_anno_testuale": None,
        "iva_inclusa": None,
        "cig": cig,
        "capitolo_bilancio": None,
        "descrizione_sintetica": oggetto[:180] if oggetto else None,
        "categoria": "Da classificare",
    }


# ─────────────────────────────────────────────────────────────────────────────
# NORMALIZZAZIONI
# ─────────────────────────────────────────────────────────────────────────────

def _normalizza_tipo(tipo_ai, tipo_portale: str) -> str:
    """Il tipo dal portale (sottocategoria) è più affidabile dell'AI."""
    tp = (tipo_portale or "").lower()
    if "liquidazione" in tp:
        return "liquidazione"
    if "determinazione" in tp:
        return "determinazione"
    t = (tipo_ai or "").lower()
    return "liquidazione" if "liquid" in t else "determinazione"


def _normalizza_importo(valore) -> float | None:
    if valore is None:
        return None
    if isinstance(valore, (int, float)):
        return round(float(valore), 2) if valore > 0 else None
    if isinstance(valore, str):
        pulito = valore.replace("€", "").replace("euro", "").strip()
        # "1.234,56" (italiano) vs "1234.56" (anglosassone)
        if "," in pulito:
            pulito = pulito.replace(".", "").replace(",", ".")
        try:
            v = float(pulito)
            return round(v, 2) if v > 0 else None
        except ValueError:
            return None
    return None
