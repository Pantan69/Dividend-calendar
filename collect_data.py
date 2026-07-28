"""
Version allegee : les dividendes sont geres par classify_dividends.py (classification
Finnhub /metric) et update_dividends.py (planification + Alpha Vantage). Ce script-ci
ne gere plus que :

  --earnings  : calendrier des resultats en masse (Finnhub, un seul appel groupe) -> data/earnings.json
  --prices    : rafraichit le cours (Finnhub /quote) uniquement pour les tickers deja
                presents dans data/dividends.json (ceux qui ont une date ex-div connue) -> recalcule le %

Usage :
  FINNHUB_API_KEY=xxxx python collect_data.py --earnings
  FINNHUB_API_KEY=xxxx python collect_data.py --prices
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
if not FINNHUB_KEY:
    raise SystemExit("FINNHUB_API_KEY manquante.")

BASE = "https://finnhub.io/api/v1"
SECONDS_BETWEEN_CALLS = 1.1
OUT_DIR = "data"


def call_api(endpoint, params, retries=3):
    params = dict(params)
    params["token"] = FINNHUB_KEY
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                last_err = e
                continue
            last_err = e
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    print(f"  [!] Echec {endpoint} {params.get('symbol','')}: {last_err}")
    return None


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run_earnings(horizon_days=120, chunk_days=14):
    """
    Finnhub semble plafonner le nombre de lignes renvoyees par calendar/earnings
    quand la plage de dates est large (confirme empiriquement : sur 120 jours,
    seule la toute fin de la fenetre etait couverte). On decoupe donc en petits
    blocs de chunk_days et on recolle les resultats.
    """
    with open("tickers.json", encoding="utf-8") as f:
        tickers = {t["ticker"] for t in json.load(f)}
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=horizon_days)

    results_by_key = {}
    chunk_start = today
    while chunk_start < horizon:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), horizon)
        print(f"[resultats] {chunk_start.isoformat()} -> {chunk_end.isoformat()}")
        data = call_api("calendar/earnings", {"from": chunk_start.isoformat(), "to": chunk_end.isoformat()})
        if data:
            for entry in data.get("earningsCalendar", []):
                symbol = entry.get("symbol")
                if symbol in tickers:
                    key = (symbol, entry.get("date"))
                    results_by_key[key] = {"ticker": symbol, "date": entry.get("date"), "hour": entry.get("hour")}
        chunk_start = chunk_end
        time.sleep(SECONDS_BETWEEN_CALLS)

    results = list(results_by_key.values())
    save_json(os.path.join(OUT_DIR, "earnings.json"), results)
    save_json(os.path.join(OUT_DIR, "last_earnings_update.json"), {"updatedAt": datetime.now(timezone.utc).isoformat()})
    print(f"=== {len(results)} annonces de resultats ===")


def run_prices():
    dividends = load_json(os.path.join(OUT_DIR, "dividends.json"), [])
    tickers = sorted({d["ticker"] for d in dividends})
    print(f"=== Rafraichissement cours : {len(tickers)} tickers ===")
    for i, symbol in enumerate(tickers):
        print(f"[cours] {i+1}/{len(tickers)} {symbol}")
        data = call_api("quote", {"symbol": symbol})
        price = data.get("c") if data else None
        if price:
            for d in dividends:
                if d["ticker"] == symbol:
                    d["price"] = price
                    d["pct"] = round((d["amount"] / price) * 100, 4)
        time.sleep(SECONDS_BETWEEN_CALLS)
    save_json(os.path.join(OUT_DIR, "dividends.json"), dividends)
    save_json(os.path.join(OUT_DIR, "last_price_update.json"), {"updatedAt": datetime.now(timezone.utc).isoformat()})
    print("=== Cours rafraichis ===")


if __name__ == "__main__":
    if "--earnings" in sys.argv:
        run_earnings()
    elif "--prices" in sys.argv:
        run_prices()
    else:
        print("Usage: python collect_data.py --earnings | --prices")
