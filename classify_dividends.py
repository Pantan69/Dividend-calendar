"""
Classe chaque ticker de tickers.json en "verse un dividende" ou "n'en verse pas",
via l'endpoint Finnhub /stock/metric (gratuit, confirme par test manuel -- contrairement
a /stock/dividend2 qui est payant).

A lancer : une premiere fois pour tout classer, puis periodiquement (le main() ne
re-teste que les tickers dus, voir data/no_div.json -> nextRecheck).

Sorties :
  - data/div_payers.json : liste des tickers consideres "a dividende" (a suivre via Alpha Vantage)
  - data/no_div.json     : liste des tickers "sans dividende" avec la date du prochain re-test
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
    raise SystemExit("FINNHUB_API_KEY manquante.")

BASE = "https://finnhub.io/api/v1"
SECONDS_BETWEEN_CALLS = 1.1
OUT_DIR = "data"
RECHECK_NO_DIV_AFTER_DAYS = 182  # ~6 mois


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


def main():
    with open("tickers.json", encoding="utf-8") as f:
        tickers = json.load(f)

    div_payers = load_json(os.path.join(OUT_DIR, "div_payers.json"), [])
    no_div = load_json(os.path.join(OUT_DIR, "no_div.json"), [])

    already_classified = {t["ticker"] for t in div_payers} | {t["ticker"] for t in no_div}
    today = datetime.now(timezone.utc).date()

    # A classer : jamais vu, OU dans no_div avec un recheck du (ou avant) aujourd'hui
    due_recheck = {t["ticker"] for t in no_div if t.get("nextRecheck") and t["nextRecheck"] <= today.isoformat()}
    to_check = [t for t in tickers if t["ticker"] not in already_classified or t["ticker"] in due_recheck]

    print(f"=== Classification : {len(to_check)} tickers a verifier (sur {len(tickers)}) ===")

    no_div_by_ticker = {t["ticker"]: t for t in no_div if t["ticker"] not in due_recheck}
    div_payers_by_ticker = {t["ticker"]: t for t in div_payers}

    for i, t in enumerate(to_check):
        symbol = t["ticker"]
        print(f"[classification] {i+1}/{len(to_check)} {symbol}")
        data = call_api("stock/metric", {"symbol": symbol, "metric": "all"})
        has_div = False
        if data:
            metric = data.get("metric", {}) or {}
            dps = metric.get("dividendPerShareTTM") or 0
            yld = metric.get("dividendYieldIndicatedAnnual") or 0
            has_div = (dps and dps > 0) or (yld and yld > 0)
        if has_div:
            div_payers_by_ticker[symbol] = {"ticker": symbol, "name": t["name"]}
        else:
            no_div_by_ticker[symbol] = {
                "ticker": symbol,
                "name": t["name"],
                "nextRecheck": (today + timedelta(days=RECHECK_NO_DIV_AFTER_DAYS)).isoformat(),
            }
        time.sleep(SECONDS_BETWEEN_CALLS)

    save_json(os.path.join(OUT_DIR, "div_payers.json"), list(div_payers_by_ticker.values()))
    save_json(os.path.join(OUT_DIR, "no_div.json"), list(no_div_by_ticker.values()))
    print(f"\n=== Termine : {len(div_payers_by_ticker)} a dividende, {len(no_div_by_ticker)} sans ===")


if __name__ == "__main__":
    main()
