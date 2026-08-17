"""
Construit une liste de veille des tickers les plus remunerateurs, a partir de
CE QU'ON SAIT DEJA (data/dividend_state.json -- ne consomme aucun nouvel appel
Alpha Vantage). Pour chaque ticker deja verifie au moins une fois, on prend son
DERNIER montant connu (lastAmount) et on le divise par le cours ACTUEL (Finnhub
/quote, gratuit, rapide) -- exactement la meme metrique "%" que partout ailleurs
sur le site (montant d'UN versement / cours), PAS un rendement annualise.

Sert a prioriser "quels tickers valent la peine d'etre suivis de pres" en
attendant que la rotation Alpha Vantage leur trouve une prochaine date confirmee.

Sortie : data/watchlist_highyield.json -- tickers a >=2% (ou >=1.5% si moins de
20 resultats a 2%), tries decroissant, avec la derniere date/montant connus
pour reference (pas une date a venir garantie).
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
if not FINNHUB_KEY:
    raise SystemExit("FINNHUB_API_KEY manquante.")

BASE = "https://finnhub.io/api/v1"
SECONDS_BETWEEN_CALLS = 1.1
OUT_DIR = "data"
HIGH_THRESHOLD = 2.0
LOW_THRESHOLD = 1.5
MIN_RESULTS_BEFORE_LOWERING = 20


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
                time.sleep(5); last_err = e; continue
            last_err = e; break
        except Exception as e:
            last_err = e; time.sleep(2)
    print(f"  [!] Echec quote {params.get('symbol','')}: {last_err}")
    return None


def main():
    with open("tickers.json", encoding="utf-8") as f:
        names = {t["ticker"]: t["name"] for t in json.load(f)}
    with open(os.path.join(OUT_DIR, "dividend_state.json"), encoding="utf-8") as f:
        state = json.load(f)

    candidates = [(t, v) for t, v in state.items() if v.get("lastAmount") and v.get("lastExDate")]
    print(f"=== {len(candidates)} tickers deja verifies au moins une fois, calcul du rendement dernier-verse ===")

    results = []
    for i, (ticker, v) in enumerate(candidates):
        print(f"[{i+1}/{len(candidates)}] {ticker}")
        data = call_api("quote", {"symbol": ticker})
        price = data.get("c") if data else None
        if price:
            pct = round(v["lastAmount"] / price * 100, 4)
            results.append({
                "ticker": ticker,
                "name": names.get(ticker, ticker),
                "price": price,
                "lastAmount": v["lastAmount"],
                "lastExDate": v["lastExDate"],
                "frequencyClass": v.get("frequencyClass", "inconnu"),
                "pct": pct,
            })
        time.sleep(SECONDS_BETWEEN_CALLS)

    results.sort(key=lambda x: -x["pct"])

    high = [r for r in results if r["pct"] >= HIGH_THRESHOLD]
    if len(high) < MIN_RESULTS_BEFORE_LOWERING:
        high = [r for r in results if r["pct"] >= LOW_THRESHOLD]
        threshold_used = LOW_THRESHOLD
    else:
        threshold_used = HIGH_THRESHOLD

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "thresholdUsed": threshold_used,
        "note": "pct = dernier montant verse / cours actuel (pas une date future garantie, juste un historique recent -- sert a prioriser la veille manuelle)",
        "watchlist": high,
    }
    with open(os.path.join(OUT_DIR, "watchlist_highyield.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\n=== {len(high)} tickers dans la watchlist (seuil {threshold_used}%) sur {len(results)} evalues ===")


if __name__ == "__main__":
    main()
