"""
publisher_telegram.py — Pubblica le nuove spese sul canale Telegram.

Eseguito dopo scraper.py nel workflow giornaliero (NON nel backfill:
centinaia di messaggi storici non interessano a nessuno).

Secrets necessari:
  TELEGRAM_BOT_TOKEN  — token del bot (da @BotFather)
  TELEGRAM_CHANNEL_ID — username canale con @ oppure id numerico
"""

import os
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


def formatta_spesa(s: dict) -> str:
    em = EMOJI_CATEGORIA.get(s.get("categoria") or "", "📄")
    tipo = "Liquidazione" if s.get("tipo_atto") == "liquidazione" else "Determinazione"
    parti = [
        f"{em} *{esc(eur(s.get('importo_euro')))}* — {esc(s.get('beneficiario') or 'beneficiario n.d.')}",
        f"_{esc(s.get('descrizione_sintetica') or (s.get('oggetto') or '')[:200])}_",
        f"{esc(tipo)} n\\. {esc(s.get('numero_raw',''))} · {esc(s.get('categoria') or 'Da classificare')}",
    ]
    if s.get("url_atto"):
        parti.append(f"[Vedi atto originale]({s['url_atto']})")
    return "\n".join(parti)


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


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    if not token or not chat_id:
        log.warning("Secret Telegram mancanti: pubblicazione saltata.")
        return

    if not NUOVE_JSON.exists():
        log.info("Nessun file nuove_spese.json: niente da pubblicare.")
        return
    spese = json.loads(NUOVE_JSON.read_text(encoding="utf-8"))
    if not spese:
        log.info("Nessuna spesa nuova: niente da pubblicare.")
        return

    oggi = date.today().strftime("%d/%m/%Y")
    totale = sum(s.get("importo_euro") or 0 for s in spese)
    intro = (
        f"💶 *OpenSpese — Pieve Emanuele*\n"
        f"📅 {esc(oggi)}\n\n"
        f"{'È stata registrata' if len(spese) == 1 else 'Sono state registrate'} "
        f"*{len(spese)} {'spesa' if len(spese) == 1 else 'spese'}* "
        f"per un totale di *{esc(eur(totale))}*\\."
    )
    invia(token, chat_id, intro)
    time.sleep(1)

    if len(spese) > MAX_MESSAGGI:
        log.info(f"{len(spese)} spese > {MAX_MESSAGGI}: pubblico solo il riepilogo con link al sito.")
        invia(token, chat_id,
              esc("Troppe spese per elencarle una a una: le trovi tutte su ") +
              "[openspese](https://nt0wers84.github.io/bilanciopertutti/)\\.")
        return

    for s in spese:
        invia(token, chat_id, formatta_spesa(s))
        time.sleep(1.5)  # rate limit Telegram: max ~20 msg/min per canale

    log.info(f"Pubblicate {len(spese)} spese su Telegram.")


if __name__ == "__main__":
    main()
