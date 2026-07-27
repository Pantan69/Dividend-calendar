"""
Collecte les donnees de dividendes, cours et resultats pour l'univers de tickers
defini dans tickers.json, via l'API Finnhub (gratuite).

Necessite la variable d'environnement FINNHUB_API_KEY (jamais ecrite en dur ici).

Sorties :
  - data/dividends.json  (ticker, nom, date ex-div, montant, cours, %)
  - data/earnings.json   (ticker, nom, date de resultats)
  - data/last_update.json (horodatage de la derniere collecte, pour affichage sur le site)

Usage :
  FINNHUB_API_KEY=xxxx python collect_data.py
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
if not FINNHUB_KEY:
    raise SystemExit("FINNHUB_API_KEY manquante : definis-la comme secret GitHub Actions.")

BASE = "https://finnhub.io/api/v1"
# 60 appels/minute sur le plan gratuit -> on garde une marge de securite
SECONDS_BETWEEN_CALLS = 1.1

TICKERS_PATH = "tickers.json"
OUT_DIR = "data"


def call_api(endpoint, params, retries=3):
    """Appelle un endpoint Finnhub et renvoie le JSON. Reessaie en cas d'erreur reseau."""
    params = dict(params)
    params["token"] = FINNHUB_KEY
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # 429 = trop de requetes -> on attend plus longtemps et on reessaie
            if e.code == 429:
                time.sleep(5)
                last_err = e
                continue
            last_err = e
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    print(f"  [!] Echec sur {endpoint} params={params.get('symbol', '')}: {last_err}")
    return None


def load_tickers():
    with open(TICKERS_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_dividends(tickers, horizon_days=120):
    """
    Pour chaque ticker, recupere l'historique/les dividendes annonces et garde
    le PROCHAIN dividende dont la date ex-div est dans le futur (jusqu'a horizon_days).

    NOTE : l'endpoint exact et la forme de la reponse Finnhub pour les dividendes
    par symbole peuvent varier legerement selon la version d'API. Si le premier
    run ne remonte rien, verifie la reponse brute (decommente le print ci-dessous)
    et ajuste le nom de champ en consequence -- c'est le seul point non teste en
    direct avant ce premier run.
    """
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=horizon_days)
    results = []
    total = len(tickers)
    for i, t in enumerate(tickers):
        symbol = t["ticker"]
        print(f"[dividendes] {i+1}/{total} {symbol}")
        data = call_api("stock/dividend2", {"symbol": symbol, "from": today.isoformat(), "to": horizon.isoformat()})
        # Debug si besoin :
        # print(json.dumps(data, indent=2))
        if data:
            entries = data if isinstance(data, list) else data.get("data", [])
            for entry in entries or []:
                ex_date_str = entry.get("exDate") or entry.get("ex_date") or entry.get("date")
                amount = entry.get("amount") or entry.get("dividend")
                if not ex_date_str or amount is None:
                    continue
                try:
                    ex_date = datetime.strptime(ex_date_str[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if today <= ex_date <= horizon:
                    results.append({
                        "ticker": symbol,
                        "name": t["name"],
                        "exDate": ex_date.isoformat(),
                        "amount": float(amount),
                    })
        time.sleep(SECONDS_BETWEEN_CALLS)
    return results


def fetch_quotes(tickers):
    """Cours actuel (delai ~20 min sur le plan gratuit) pour chaque ticker."""
    quotes = {}
    total = len(tickers)
    for i, t in enumerate(tickers):
        symbol = t["ticker"]
        print(f"[cours] {i+1}/{total} {symbol}")
        data = call_api("quote", {"symbol": symbol})
        if data and data.get("c"):
            quotes[symbol] = data["c"]  # "c" = current price
        time.sleep(SECONDS_BETWEEN_CALLS)
    return quotes


def fetch_earnings(ticker_set, horizon_days=120):
    """Calendrier des resultats en masse (un seul appel par plage de dates), filtre sur nos tickers."""
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=horizon_days)
    data = call_api("calendar/earnings", {"from": today.isoformat(), "to": horizon.isoformat()})
    results = []
    if data:
        entries = data.get("earningsCalendar", [])
        for entry in entries:
            symbol = entry.get("symbol")
            if symbol in ticker_set:
                results.append({
                    "ticker": symbol,
                    "date": entry.get("date"),
                    "hour": entry.get("hour"),  # "bmo" avant ouverture / "amc" apres cloture
                })
    return results


def refresh_prices_only():
    """
    Mode rapide (a lancer souvent, ex toutes les heures) : relit data/dividends.json
    deja genere par le run complet, et rafraichit juste le cours + % de CES
    tickers-la (pas besoin de rebalayer les 793 -- juste ceux deja retenus).
    """
    path = os.path.join(OUT_DIR, "dividends.json")
    if not os.path.exists(path):
        print("Pas de data/dividends.json existant -- lance d'abord un run complet (--full).")
        return
    with open(path, encoding="utf-8") as f:
        dividends = json.load(f)

    unique_tickers = sorted({d["ticker"] for d in dividends})
    print(f"=== Rafraichissement cours uniquement : {len(unique_tickers)} tickers ===")
    quotes = fetch_quotes([{"ticker": t} for t in unique_tickers])

    for d in dividends:
        price = quotes.get(d["ticker"])
        if price:
            d["price"] = price
            d["pct"] = round((d["amount"] / price) * 100, 4)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(dividends, f, indent=2, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, "last_price_update.json"), "w", encoding="utf-8") as f:
        json.dump({"updatedAt": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    print("=== Cours rafraichis ===")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    tickers = load_tickers()
    ticker_by_symbol = {t["ticker"]: t["name"] for t in tickers}

    print(f"=== Univers : {len(tickers)} tickers ===")

    print("\n--- Resultats (calendrier groupe) ---")
    earnings = fetch_earnings(set(ticker_by_symbol.keys()))
    with open(os.path.join(OUT_DIR, "earnings.json"), "w", encoding="utf-8") as f:
        json.dump(earnings, f, indent=2, ensure_ascii=False)
    print(f"-> {len(earnings)} annonces de resultats trouvees")

    print("\n--- Dividendes (par ticker, ~14 min pour 793 tickers) ---")
    dividends = fetch_dividends(tickers)
    print(f"-> {len(dividends)} dividendes a venir trouves, recuperation des cours...")

    print("\n--- Cours (seulement les tickers avec un dividende trouve) ---")
    needed_tickers = [t for t in tickers if t["ticker"] in {d["ticker"] for d in dividends}]
    quotes = fetch_quotes(needed_tickers)

    for d in dividends:
        price = quotes.get(d["ticker"])
        d["price"] = price
        d["pct"] = round((d["amount"] / price) * 100, 4) if price else None

    with open(os.path.join(OUT_DIR, "dividends.json"), "w", encoding="utf-8") as f:
        json.dump(dividends, f, indent=2, ensure_ascii=False)

    with open(os.path.join(OUT_DIR, "last_update.json"), "w", encoding="utf-8") as f:
        json.dump({"updatedAt": datetime.now(timezone.utc).isoformat()}, f, indent=2)

    print(f"\n=== Termine : {len(dividends)} dividendes, {len(earnings)} resultats ===")


if __name__ == "__main__":
    import sys
    if "--prices-only" in sys.argv:
        refresh_prices_only()
    else:
        main()
