"""
publisher_telegram.py — Pubblica le nuove spese sul canale Telegram.

Eseguito dopo scraper.py nel workflow giornaliero (NON nel backfill:
centinaia di messaggi storici non interessano a nessuno).

Secrets necessari:
  TELEGRAM_BOT_TOKEN  — token del bot (da @BotFather)
  TELEGRAM_CHANNEL_ID — id numerico del canale (consigliato) oppure @username.
                        L'id numerico non cambia mai; l'username sì, e quando
                        cambia il vecchio smette di funzionare.
"""

import os
import sys
import json
import time
import logging
import requests
from pathlib import Path
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

NUOVE_JSON = Path("data/nuove_spese.json")
MAX_MESSAGGI = 20  # oltre, meglio un solo messaggio riassuntivo

EMOJI_CATEGORIA = {
    "Amministrazione e servizi generali": "🏛️",
    "Polizia locale e sicurezza": "🚓",
    "Istruzione e scuola": "🎒",
    "Cultura": "🎭",
    "Sport e tempo libero": "⚽",
    "Turismo": "🧳",
    "Urbanistica e casa": "🏘️",
    "Ambiente, verde e rifiuti": "🌳",
    "Strade, viabilità e trasporti": "🛣️",
    "Protezione civile": "🚨",
    "Sociale e famiglia": "🤝",
    "Sanità": "🏥",
    "Sviluppo economico e commercio": "🏪",
    "Lavoro": "👷",
    "Debito e anticipazioni": "🏦",
    "Da classificare": "📄",
}


def esc(testo: str) -> str:
    """Escape per Telegram MarkdownV2."""
    speciali = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in speciali else c for c in str(testo))


def eur(v) -> str:
    if v is None:
        return "importo n.d."
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def spiega_importo(s: dict) -> str:
    """
    Una riga che spiega COSA rappresenta la cifra, usando solo i campi
    già estratti. Serve a non far leggere un numero senza contesto.
    """
    if s.get("importo_euro") is None:
        return "Importo non indicato nell'atto: consulta il documento originale."

    note = []
    if s.get("tipo_atto") == "liquidazione":
        note.append("soldi effettivamente pagati")
    else:
        note.append("somma impegnata, il pagamento avverrà dopo")

    if s.get("importo_e_pluriennale") and s.get("durata_anni"):
        anni = s["durata_anni"]
        if s.get("importo_primo_anno"):
            note.append(f"totale per {anni} anni, di cui {eur(s['importo_primo_anno'])} "
                        f"il primo anno")
        else:
            note.append(f"totale per {anni} anni, non di un anno solo")

    n = s.get("n_beneficiari") or 1
    if n > 1:
        note.append(f"somma di più voci verso {n} beneficiari")

    if s.get("iva_inclusa") is True:
        note.append("IVA inclusa")
    elif s.get("iva_inclusa") is False:
        note.append("IVA esclusa")

    if s.get("e_rimodulazione"):
        note.append("rimodulazione di una spesa già approvata, non spesa nuova")

    return "; ".join(note).capitalize() + "."


def formatta_spesa(s: dict) -> str:
    em = EMOJI_CATEGORIA.get(s.get("categoria") or "", "📄")
    tipo = "Liquidazione" if s.get("tipo_atto") == "liquidazione" else "Determinazione"
    parti = [
        f"{em} *{esc(eur(s.get('importo_euro')))}* — {esc(s.get('beneficiario') or 'beneficiario n.d.')}",
        f"_{esc(s.get('descrizione_sintetica') or (s.get('oggetto') or '')[:200])}_",
        f"💡 {esc(spiega_importo(s))}",
        f"{esc(tipo)} n\\. {esc(s.get('numero_raw',''))} · {esc(s.get('categoria') or '')}",
    ]
    if s.get("url_atto"):
        parti.append(f"[Vedi atto originale]({s['url_atto']})")
    return "\n".join(parti)


def canale_raggiungibile(token: str, chat_id: str) -> bool:
    """Verifica il canale PRIMA di pubblicare.

    Serve perché il caso più probabile di rottura è silenzioso: se il secret
    contiene l'username (@nome) e l'username del canale viene cambiato, il
    vecchio non risolve più e ogni invio fallisce con «chat not found». Senza
    questo controllo il workflow resta verde e le spese del giorno, che lo
    scraper sovrascrive a ogni run, non vengono pubblicate mai più.
    """
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/getChat",
                          json={"chat_id": chat_id}, timeout=30)
        if r.status_code == 200:
            nome = r.json().get("result", {}).get("title", "?")
            log.info(f"Canale Telegram raggiunto: «{nome}»")
            return True
        log.error(f"CANALE TELEGRAM NON RAGGIUNGIBILE ({r.status_code}): "
                  f"{r.text[:200]}")
        if str(chat_id).startswith("@"):
            log.error("Il secret TELEGRAM_CHANNEL_ID contiene un username "
                      f"({chat_id}). Se l'username del canale è stato cambiato, "
                      "il vecchio non vale più: aggiorna il secret, meglio "
                      "ancora con l'id numerico del canale, che non cambia mai.")
        return False
    except Exception as e:
        log.error(f"Telegram irraggiungibile: {e}")
        return False


def invia(token: str, chat_id: str, testo: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": testo, "parse_mode": "MarkdownV2",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"Telegram {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram errore: {e}")
        return False


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    if not token or not chat_id:
        log.warning("Secret Telegram mancanti: pubblicazione saltata.")
        return 0

    # Il canale si verifica sempre, anche nei giorni senza spese nuove. Se il
    # controllo stesse dopo, una configurazione rotta resterebbe invisibile
    # finché non arriva un atto da pubblicare: si scoprirebbe il guasto nel
    # momento peggiore, cioè quando c'è qualcosa da dire.
    if not canale_raggiungibile(token, chat_id):
        return 1

    if not NUOVE_JSON.exists():
        log.info("Nessun file nuove_spese.json: niente da pubblicare.")
        return 0
    spese = json.loads(NUOVE_JSON.read_text(encoding="utf-8"))
    if not spese:
        log.info("Nessuna spesa nuova oggi: niente da pubblicare, "
                 "ma il canale risponde.")
        return 0

    oggi = date.today().strftime("%d/%m/%Y")
    totale = sum(s.get("importo_euro") or 0 for s in spese)
    intro = (
        f"💶 *Conti in chiaro — Pieve Emanuele*\n"
        f"📅 {esc(oggi)}\n\n"
        f"{'È stata registrata' if len(spese) == 1 else 'Sono state registrate'} "
        f"*{len(spese)} {'spesa' if len(spese) == 1 else 'spese'}* "
        f"per un totale di *{esc(eur(totale))}*\\."
    )
    inviati = [invia(token, chat_id, intro)]
    time.sleep(1)

    if len(spese) > MAX_MESSAGGI:
        log.info(f"{len(spese)} spese > {MAX_MESSAGGI}: pubblico solo il riepilogo con link al sito.")
        inviati.append(invia(
            token, chat_id,
            esc("Troppe spese per elencarle una a una: le trovi tutte su ") +
            "[conti in chiaro](https://nt0wers84.github.io/bilanciopertutti/)\\."))
    else:
        for s in spese:
            inviati.append(invia(token, chat_id, formatta_spesa(s)))
            time.sleep(1.5)  # rate limit Telegram: max ~20 msg/min per canale

    riusciti = sum(1 for x in inviati if x)
    if riusciti == len(inviati):
        log.info(f"Pubblicate {len(spese)} spese su Telegram.")
        return 0
    # Le spese di oggi non torneranno: nuove_spese.json viene riscritto a ogni
    # run. Meglio un workflow rosso che un silenzio.
    log.error(f"Telegram: {len(inviati) - riusciti} messaggi su {len(inviati)} "
              f"non inviati. Le spese di oggi restano sul sito ma non sono "
              f"state pubblicate sul canale.")
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
