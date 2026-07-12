# OpenSpese Pieve Emanuele

Monitoraggio civico indipendente della spesa del Comune di Pieve Emanuele (MI).

Ogni giorno un automatismo legge l'Albo Pretorio comunale, scarica le
**determinazioni contabili** e le **liquidazioni**, ne estrae con l'AI
(Groq / Llama) beneficiario, importo, CIG e ambito di spesa, e pubblica
tutto su un sito statico con grafici e un feed consultabile.

Sito: https://nt0wers84.github.io/bilanciopertutti/
Progetto gemello: https://nt0wers84.github.io/albo-pretorio/

## Architettura

- `scripts/portale.py` — accesso al portale JCityGov/Liferay (griglie, dettaglio, PDF, OCR)
- `scripts/estrattore.py` — estrazione JSON strutturata via Groq, con fallback regex; categorie allineate alle Missioni BDAP
- `scripts/scraper.py` — run giornaliero (albo corrente)
- `scripts/backfill.py` — recupero storico dalla sezione archivio provvedimenti, a blocchi con stato di avanzamento
- `scripts/genera_sito.py` — prepara `docs/data/` per il sito
- `scripts/publisher_telegram.py` — pubblica le nuove spese sul canale Telegram
- `data/spese.json` — database flat (unica fonte di verità)
- `docs/` — sito statico servito da GitHub Pages

## Setup (una tantum)

1. **Secret** (Settings → Secrets and variables → Actions):
   - `GROQ_API_KEY` — da https://console.groq.com (gratuita)
   - `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHANNEL_ID` — opzionali
2. **GitHub Pages**: Settings → Pages → Source: `Deploy from a branch`, branch `main`, cartella `/docs`
3. **Backfill**: tab Actions → "OpenSpese — Backfill Storico" → Run workflow.
   Primo giro consigliato con "solo censimento" = true per scoprire la
   profondità dell'archivio; poi rilanciarlo (senza censimento) più volte
   finché il log non dice "Archivio esaurito".

Il run giornaliero parte da solo (cron 15:30 UTC).

## Note

- I PDF non vengono conservati nel repository: resta il link all'atto
  originale sul portale comunale.
- L'estrazione automatica può contenere errori: fa fede l'atto originale.
- Fase 2 (pianificata): riconciliazione con il bilancio di previsione e
  il consuntivo da OpenBDAP (Missioni/Programmi).
