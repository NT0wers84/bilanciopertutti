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

MODELLO_DEFAULT = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MODELLO_FALLBACK = "llama-3.1-8b-instant"

# Pausa tra chiamate: free tier Groq = 30 RPM → 2.5s è prudente
PAUSA_TRA_CHIAMATE = float(os.environ.get("GROQ_PAUSA", "2.5"))
TESTO_MAX_CHARS = 15_000  # ~4.000 token: sta nel TPM free tier

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

def _estrai_con_groq(testo: str, oggetto: str) -> dict | None:
    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    prompt_utente = f"Oggetto dell'atto: {oggetto}\n\nTesto dell'atto:\n{testo if testo else '(testo non disponibile: deduci il possibile dal solo oggetto)'}"

    for modello in (MODELLO_DEFAULT, MODELLO_FALLBACK):
        for tentativo in range(4):
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
                log.warning(f"  Groq: JSON non-dict ({type(dati)}), riprovo")
            except json.JSONDecodeError as e:
                log.warning(f"  Groq: JSON malformato ({e}), tentativo {tentativo+1}")
            except Exception as e:
                messaggio = str(e)
                if "429" in messaggio or "rate" in messaggio.lower():
                    attesa = (2 ** tentativo) * 5 + random.uniform(0, 3)
                    log.warning(f"  Groq rate limit, attendo {attesa:.0f}s (tentativo {tentativo+1})")
                    time.sleep(attesa)
                else:
                    log.error(f"  Groq errore ({modello}): {e}")
                    break  # errore non recuperabile con questo modello → prova fallback
        log.info(f"  Passo al modello di riserva dopo {modello}")
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
