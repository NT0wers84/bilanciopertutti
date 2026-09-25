# Conti in chiaro — Pieve Emanuele

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
- `scripts/siope_estrai.py` — i pagamenti di **cassa** del Comune da SIOPE
  (Ragioneria dello Stato, banca dati Banca d'Italia). Serve a colmare il buco
  che l'albo non può coprire: le liquidazioni restano pubblicate quindici
  giorni, SIOPE pubblica ogni mese i pagamenti di tutti i Comuni e li tiene per
  anni. Il file nazionale è grosso e resta fuori dal repository: si scarica, si
  filtra sul codice fiscale del Comune e si salva solo l'estratto in
  `data/siope.json`. Si lancia dal workflow «Pagamenti SIOPE», **la prima volta
  con `solo_ispezione` attivo**, che dichiara com'è fatto il file senza scrivere
  niente. I due dati non vanno sommati: l'albo dice quale atto autorizza una
  spesa, SIOPE quanto è uscito davvero di cassa
- `scripts/test_estrattore.py` — rete di sicurezza sulla lettura degli atti. Ogni
  caso è un errore vero trovato guardando il sito, con accanto la cifra corretta
  letta sul documento: la liquidazione I.M.E.T. che mostrava 1,6 milioni invece di
  601.334,66, la determina con otto fornitori, il prospetto con lo storno IVA.
  Si lancia con `python3 scripts/test_estrattore.py`, senza installare nulla, e
  gira da solo prima di ogni scraping e di ogni backfill: se una regola smette di
  funzionare il workflow si ferma **prima** di scrivere dati sbagliati
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
   - `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHANNEL_ID` — opzionali.
     Nel secret conviene mettere l'**id numerico** del canale (del tipo
     `-1001234567890`), non `@username`: l'username si può cambiare e da quel
     momento il vecchio non risolve più, mentre l'id resta lo stesso per sempre.
     Per leggerlo: inoltra un messaggio del canale a `@userinfobot`, oppure apri
     `https://api.telegram.org/bot<TOKEN>/getUpdates` dopo aver pubblicato un post.
     Se il canale non risponde il workflow diventa rosso: la pubblicazione è
     l'ultimo passo, quindi i dati del sito sono già stati salvati.
2. **GitHub Pages**: Settings → Pages → Source: `Deploy from a branch`, branch `main`, cartella `/docs`
3. **Backfill**: tab Actions → "Conti in chiaro — Backfill Storico" → Run workflow.
   Primo giro consigliato con "solo censimento" = true per scoprire la
   profondità dell'archivio; poi rilanciarlo (senza censimento) più volte
   finché il log non dice "Archivio esaurito".

Il run giornaliero parte da solo (cron 15:30 UTC).

## Note

- **Le liquidazioni spariscono dal portale dopo quindici giorni.** Verificato:
  un link a una liquidazione di luglio risponde «Atto non disponibile o non più
  in pubblicazione», mentre le determinazioni restano online per anni (quelle
  PNRR fino al 2031). Per questo la serie mensile dei pagamenti parte da luglio
  2026: prima di allora nessuno li aveva archiviati, e non sono più recuperabili
  da fonti pubbliche.
- I PDF non vengono conservati, ma il **testo estratto sì**: `docs/testi/<id>.txt.gz`
  (compresso, pochi KB per atto). `genera_sito.py` lo copia in `docs/testi/`, il
  sito lo mostra con il pulsante «testo archiviato» e il browser lo decomprime da
  solo (`DecompressionStream`). È ciò che rende verificabile una spesa anche
  quando il Comune ha già ritirato l'atto.
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
