"""
verifica_dati.py — Controllo di integrità prima della pubblicazione.

Impedisce che finiscano online file JSON rotti (tipicamente con i
marcatori di conflitto Git lasciati da un merge non risolto: il sito
smette di caricare i dati e continua a mostrare la versione in cache).

Esce con codice 1 se qualcosa non va: il workflow si ferma.
"""

import json
import sys
from pathlib import Path

FILE_DATI = [
    Path("data/spese.json"),
    Path("docs/data/spese.json"),
    Path("docs/data/meta.json"),
    Path("docs/data/bilanci.json"),
]
MARCATORI = ("<<<<<<< ", "=======\n", ">>>>>>> ")


def main() -> int:
    errori = []
    for percorso in FILE_DATI:
        if not percorso.exists():
            continue
        testo = percorso.read_text(encoding="utf-8")
        if testo.lstrip().startswith("<<<<<<<") or "\n<<<<<<< " in testo:
            errori.append(f"{percorso}: contiene marcatori di conflitto Git")
            continue
        try:
            dati = json.loads(testo)
        except json.JSONDecodeError as e:
            errori.append(f"{percorso}: JSON non valido ({e})")
            continue
        if isinstance(dati, list) and not dati:
            errori.append(f"{percorso}: elenco vuoto (dati persi?)")
        else:
            n = len(dati) if isinstance(dati, list) else 1
            print(f"OK  {percorso} — {n} record")

    if errori:
        print("\nINTEGRITÀ DATI COMPROMESSA:")
        for e in errori:
            print(f"  ✗ {e}")
        print("\nPubblicazione interrotta: risolvi il conflitto e rilancia.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
