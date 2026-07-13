"""
bdap_scopri.py — Sonda del catalogo Open Data BDAP (fase 2: bilanci).

Obiettivo: trovare i dataset "Previsione - Schemi di Bilancio" e
"Rendiconto - Schemi di Bilancio" (spese per missione/programma) e
loggare gli URL delle risorse scaricabili (CSV/ZIP), così da costruire
lo script di sincronizzazione sul formato reale e non su ipotesi.

La ricerca del catalogo è solo-JS, ma la paginazione statica funziona:
/catalog?page=N restituisce 10 dataset a pagina in HTML statico.

Uso:  python scripts/bdap_scopri.py [--max-pagine 361]
Output: data/bdap/catalogo.json + log dettagliato.
"""

import re
import json
import time
import logging
import argparse
import requests
from pathlib import Path
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE = "https://bdap-opendata.rgs.mef.gov.it"
OUT = Path("data/bdap/catalogo.json")

# Un dataset ci interessa se il titolo parla di schemi di bilancio
# previsione/rendiconto (spese), in ogni variante di titolo usata da RGS
PAROLE_TITOLO = re.compile(
    r"(previsione|rendiconto|consuntivo)", re.IGNORECASE)
PAROLE_CONTESTO = re.compile(
    r"(spes[ae]|missio|bilanci|entrat)", re.IGNORECASE)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (civic-tech; OpenSpese Pieve Emanuele)"})


def lista_dataset(max_pagine: int) -> list[dict]:
    trovati = []
    errori_consecutivi = 0
    for pagina in range(max_pagine):
        url = f"{BASE}/catalog?page={pagina}"
        try:
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
            errori_consecutivi = 0
        except requests.RequestException as e:
            errori_consecutivi += 1
            log.warning(f"Pagina {pagina} non raggiungibile: {e}")
            if errori_consecutivi >= 3:
                log.error("3 errori consecutivi: il sito RGS è irraggiungibile da "
                          "questa rete (blocca gli IP dei datacenter, GitHub Actions "
                          "incluso). Lancia questo script dalla tua rete di casa: "
                          "pip install requests beautifulsoup4 && "
                          "python scripts/bdap_scopri.py")
                break
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        schede = soup.select("a[href*='/content/']")
        if not schede:
            log.info(f"Pagina {pagina}: nessun dataset, fine catalogo.")
            break

        # Il catalogo è renderizzato in JS: se il parametro page è ignorato,
        # ogni pagina restituisce gli stessi dataset. Rilevalo e fermati.
        firma = schede[0].get("href", "")
        if pagina > 0 and firma == getattr(lista_dataset, "_firma_p0", None):
            log.error("Il parametro ?page= è ignorato dal server (catalogo solo-JS): "
                      "il crawl HTML non può funzionare. Usa l'API CKAN o scarica "
                      "i dataset manualmente dal catalogo.")
            break
        if pagina == 0:
            lista_dataset._firma_p0 = firma

        for a in schede:
            titolo = a.get_text(strip=True)
            href = a["href"]
            url_ds = href if href.startswith("http") else BASE + href
            if PAROLE_TITOLO.search(titolo) and PAROLE_CONTESTO.search(titolo):
                trovati.append({"titolo": titolo, "url": url_ds})
                log.info(f"  CANDIDATO: {titolo}  →  {url_ds}")

        if pagina % 25 == 0:
            log.info(f"Scansione catalogo: pagina {pagina}, candidati finora: {len(trovati)}")
        time.sleep(0.3)
    return trovati


def risorse_dataset(url_ds: str) -> list[dict]:
    """Apre la scheda del dataset (tab Scarica) e raccoglie i link alle risorse."""
    risorse = []
    for variante in (f"{url_ds}?t=Scarica", url_ds):
        try:
            r = SESSION.get(variante, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"  Scheda non raggiungibile ({variante}): {e}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.(csv|zip|json|xlsx?)(\?|$)", href, re.IGNORECASE) \
               or "/download/" in href.lower() or "getfile" in href.lower():
                u = href if href.startswith("http") else BASE + href
                if u not in [x["url"] for x in risorse]:
                    risorse.append({"testo": a.get_text(strip=True)[:80], "url": u})
        if risorse:
            break
    return risorse


# Parole che identificano i dataset dei bilanci armonizzati degli enti
# territoriali nei nomi macchina dei package (es. rendiconto, previsione)
RE_NOME_INTERESSANTE = re.compile(
    r"(rendicont|prevision|consuntiv|bilanc|schemi|missio|_fet_|enti[_\-]territorial)",
    re.IGNORECASE)


def prova_api_ckan() -> list[dict]:
    """
    API CKAN documentate dal portale (sottoinsieme):
      /SpodCkanApi/api/3/action/package_list  → elenco nomi dataset
      /SpodCkanApi/api/{1,2}/rest/dataset/<n> → dettaglio con risorse
    Niente package_search: non è nel sottoinsieme esposto.
    """
    base = f"{BASE}/SpodCkanApi/api"

    # 1. Elenco completo dei nomi
    try:
        r = SESSION.get(f"{base}/3/action/package_list", timeout=30)
        r.raise_for_status()
        dati = r.json()
        nomi = dati.get("result") or dati  # v3 action → {result: [...]}; tolleranza
        if isinstance(nomi, dict):
            nomi = nomi.get("results", [])
    except Exception as e:
        log.info(f"package_list non risponde: {e}")
        return []
    if not isinstance(nomi, list) or not nomi:
        log.info(f"package_list: risposta inattesa ({str(dati)[:200]})")
        return []
    log.info(f"API CKAN OK: {len(nomi)} dataset nel catalogo")

    # 2. Filtra i nomi pertinenti
    candidati = [n for n in nomi if RE_NOME_INTERESSANTE.search(str(n))]
    log.info(f"Dataset con nomi pertinenti (rendiconto/previsione/bilancio/...): {len(candidati)}")
    for n in candidati:
        log.info(f"  candidato: {n}")

    # 3. Dettaglio + risorse per ogni candidato
    trovati = []
    for nome in candidati:
        pkg = None
        for endpoint in (f"{base}/3/action/package_show?id={nome}",
                         f"{base}/2/rest/dataset/{nome}",
                         f"{base}/1/rest/dataset/{nome}"):
            try:
                r = SESSION.get(endpoint, timeout=30)
                if r.status_code != 200:
                    continue
                pkg = r.json()
                if isinstance(pkg, dict) and "result" in pkg:
                    pkg = pkg["result"]
                break
            except Exception:
                continue
        if not isinstance(pkg, dict):
            log.warning(f"  {nome}: dettaglio non recuperabile")
            continue
        risorse = [{"testo": (ris.get("name") or ris.get("description") or ris.get("format", ""))[:80],
                    "formato": ris.get("format", ""),
                    "url": ris.get("url", ""),
                    "id_risorsa": ris.get("id", "")}
                   for ris in pkg.get("resources", [])]
        trovati.append({"titolo": pkg.get("title", nome), "nome": nome,
                        "note": (pkg.get("notes") or "")[:200],
                        "risorse": risorse, "fonte": "ckan"})
        log.info(f"--- {pkg.get('title', nome)}")
        for ris in risorse[:8]:
            log.info(f"    [{ris['formato']}] {ris['testo']!r} → {ris['url'][:140]}")
        time.sleep(0.3)
    return trovati


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pagine", type=int, default=361)
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("SONDA CATALOGO OPEN DATA BDAP")
    log.info("=" * 60)

    # Via maestra: API CKAN dichiarata dal portale
    dataset = prova_api_ckan()
    if dataset:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(dataset, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info(f"Catalogo salvato via API CKAN in {OUT} ({len(dataset)} dataset)")
        return

    log.info("API CKAN non disponibile: ripiego sul crawl del catalogo HTML.")
    dataset = lista_dataset(args.max_pagine)
    log.info(f"Dataset candidati totali: {len(dataset)}")

    for ds in dataset:
        ds["risorse"] = risorse_dataset(ds["url"])
        log.info(f"--- {ds['titolo']}")
        for ris in ds["risorse"][:10]:
            log.info(f"    {ris['testo']!r} → {ris['url'][:160]}")
        if not ds["risorse"]:
            log.info("    (nessuna risorsa trovata nell'HTML statico)")
        time.sleep(0.3)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dataset, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(f"Catalogo salvato in {OUT} ({len(dataset)} dataset)")


if __name__ == "__main__":
    main()
