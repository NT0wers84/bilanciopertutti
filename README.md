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
- `scripts/normalizza.py` — manutenzione qualità: toglie gli atti di sola entrata
  finiti fra le spese, ripulisce i nomi dei beneficiari dalle descrizioni della
  fornitura, unifica le grafie, segnala i possibili doppi conteggi. Da rilanciare
  con `--applica` dopo ogni backfill importante
- `data/confronti.json` — dati di confronto (fabbisogni standard, IRPEF, benchmark lombardo) raccolti a mano dal portale dovevannoinostrisoldi.com: **non si aggiornano dal workflow**
- `scripts/fabbisogni_estrai.py` — fabbisogni standard **per singola funzione** dai dataset
  OpenCivitas scaricati a mano in `fabbisogni/` (una sottocartella per funzione, con i
  `Metadati_Enti_*.xlsx` che traducono gli `USERNAME` in codici ISTAT). I CSV sono in
  formato lungo: una riga per coppia ente/indicatore. Lo script quadra la somma delle
  funzioni con il totale FC80TOT e si ferma se non torna
- `docs/` — sito statico servito da GitHub Pages: `index.html` (spese), `bilanci.html`, `confronti.html`

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

- I PDF non vengono conservati, ma il **testo estratto sì**: `data/testi/<id>.txt.gz`
  (compresso, pochi KB per atto). È l'assicurazione contro la sparizione degli
  atti dal portale: una volta letto un atto, le rielaborazioni future non
  dipendono più dalla disponibilità del sito comunale. Resta comunque il link
  all'atto originale.
- **Cosa non è una spesa.** L'archivio esclude le variazioni di bilancio (spostano
  fondi fra capitoli) e gli accertamenti di sola entrata (canoni, multe, vendite:
  sono soldi che entrano). Gli atti misti — accertamento di entrata *e* contestuale
  impegno di spesa — restano.
- **I contratti pluriennali non sono spesa dell'anno.** Un affidamento di quindici
  anni vale da solo l'80% della somma lorda degli atti. Il sito e `meta.json`
  riportano quindi anche il totale su base annua, che usa la quota del primo anno
  dichiarata nell'atto o, in mancanza, la media annua.
- L'estrazione automatica può contenere errori: fa fede l'atto originale.
  Quando il testo dell'atto non è recuperabile, gli importi restano vuoti
  ("da verificare nell'atto"): mai valori inventati dal modello.
- La sezione **Bilanci** copre 2016-2026 con i dati ufficiali OpenBDAP
  (`scripts/bilanci_estrai.py`, da rilanciare quando escono i nuovi anni).
