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

# Pausa tra chiamate: free tier Groq = 30 RPM → 2.5s è prudente
PAUSA_TRA_CHIAMATE = float(os.environ.get("GROQ_PAUSA", "2.5"))
TESTO_MAX_CHARS = 10_000  # ~2.800 token: sotto i limiti per-richiesta free tier

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
  "beneficiario": "ragione sociale o nome del fornitore/beneficiario principale (string, null se assente)",
  "importo_euro": importo totale della spesa in euro (number, null se assente; usa il punto come separatore decimale),
  "cig": "Codice Identificativo Gara (string, null se assente)",
  "capitolo_bilancio": "capitolo/i di bilancio citati nel testo (string, null se assenti)",
  "descrizione_sintetica": "una frase semplice, max 25 parole, che spiega a un cittadino cosa paga il Comune e perché",
  "categoria": una tra le categorie elencate sotto (string, esattamente come scritta)
}

REGOLE:
- importo_euro: usa l'importo TOTALE impegnato o liquidato dall'atto (IVA inclusa se indicata). Se ci sono più importi, somma solo quelli effettivamente impegnati/liquidati da questo atto, non quelli citati come riferimento.
- Non inventare: se un dato non c'è, usa null.
- categoria: scegli quella che meglio descrive l'ambito della spesa.

CATEGORIE AMMESSE:
""" + "\n".join(f"- {c}" for c in CATEGORIE)


def estrai_dati(testo: str, oggetto: str, tipo_portale: str) -> dict:
    """
    Estrae i dati strutturati della spesa. Prova Groq, poi regex.
    Restituisce sempre un dict con le chiavi dello schema + "estrazione".
    """
    testo = (testo or "")[:TESTO_MAX_CHARS]
    risultato = None

    if os.environ.get("GROQ_API_KEY"):
        risultato = _estrai_con_groq(testo, oggetto)

    if risultato is None:
        risultato = _estrai_con_regex(testo, oggetto)
        risultato["estrazione"] = "regex"
    else:
        risultato["estrazione"] = "groq"

    # Normalizzazioni difensive
    risultato["tipo_atto"] = _normalizza_tipo(risultato.get("tipo_atto"), tipo_portale)
    risultato["importo_euro"] = _normalizza_importo(risultato.get("importo_euro"))
    if risultato.get("categoria") not in CATEGORIE:
        risultato["categoria"] = "Da classificare"
    risultato["missione_bdap"] = CATEGORIE[risultato["categoria"]]
    for k in ("beneficiario", "cig", "capitolo_bilancio", "descrizione_sintetica"):
        v = risultato.get(k)
        risultato[k] = v.strip() if isinstance(v, str) and v.strip() else None
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
            time.sleep(PAUSA_TRA_CHIAMATE)
            dati = json.loads(risposta.choices[0].message.content)
            if isinstance(dati, dict):
                return dati
            log.warning(f"  {modello}: JSON non-dict, riprovo")
        except json.JSONDecodeError as e:
            log.warning(f"  {modello}: JSON malformato ({e}), tentativo {tentativo+1}")
        except Exception as e:
            messaggio = str(e)
            if "413" in messaggio or "too large" in messaggio.lower():
                testo_corrente = testo_corrente[: len(testo_corrente) // 2]
                log.info(f"  {modello}: payload troppo grande, dimezzo il testo "
                         f"a {len(testo_corrente)} char")
                if len(testo_corrente) < 500:
                    return None
                continue  # ritenta subito: niente attesa
            if "429" in messaggio or "rate" in messaggio.lower():
                attesa = (2 ** tentativo) * 5 + random.uniform(0, 3)
                log.warning(f"  {modello}: rate limit, attendo {attesa:.0f}s "
                            f"(tentativo {tentativo+1})")
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


def _estrai_con_regex(testo: str, oggetto: str) -> dict:
    completo = f"{oggetto}\n{testo}"

    importi = []
    for m in RE_IMPORTO.finditer(completo):
        raw = m.group(1) or m.group(2)
        try:
            importi.append(float(raw.replace(".", "").replace(",", ".")))
        except ValueError:
            pass
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
        "importo_euro": importo,
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
