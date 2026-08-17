"""
Bootstrap ponctuel -- PAS dans le workflow GitHub Actions, a lancer manuellement en local
(via Claude Code) quand on veut recharger l'historique de dividendes de tout l'univers
d'un coup (backlog initial, ou nouvelle liste de tickers copiee-collee sur un autre site).

Utilise l'API JSON non officielle de Yahoo Finance (celle qu'utilise `yfinance` en coulisses)
pour recuperer en quelques minutes l'historique de dividendes (jusqu'a 10 ans) de tous les
tickers a dividende (data/div_payers.json), sans plafond quotidien contrairement a Alpha
Vantage (25/jour gratuit). Volontairement tenu a l'ecart du workflow automatique : c'est un
endpoint non officiel, sans garantie de disponibilite, et les IP partagees de GitHub Actions
sont parfois bloquees par Yahoo -- en local via Claude Code, ce risque ne s'applique pas.

Remplit UNIQUEMENT data/dividend_state.json (derniere date/montant connus, frequence,
cadence). Ne touche JAMAIS data/dividends.json (le calendrier public affiche sur le site) :
Yahoo ne renvoie que du PASSE, jamais de date future deja annoncee (verifie manuellement --
sur INSW par ex., Yahoo s'arrete a la derniere date passee alors qu'Alpha Vantage avait deja
la prochaine confirmee). Les dates a venir restent donc la responsabilite exclusive d'Alpha
Vantage (update_dividends.py), qui beneficie ensuite d'une cadence deja connue pour tout
l'univers et peut cibler ses 25 requetes/jour sur les tickers reellement proches de leur
echeance au lieu de decouvrir la cadence au fil de l'eau sur plusieurs semaines.

Ne degrade jamais une donnee deja confirmee par Alpha Vantage : si l'etat existant a deja
une lastExDate future (>= aujourd'hui), elle est conservee telle quelle -- seule la
frequence/cadence est rafraichie a partir de l'historique Yahoo (plus profond que ce
qu'Alpha Vantage renvoie habituellement).

Usage prevu : relancer a la main environ 1x/mois pour un "gros checkup", ou a chaque
nouvelle liste de tickers.

Usage : python bootstrap_yahoo.py
"""
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

OUT_DIR = "data"
SECONDS_BETWEEN_CALLS = 0.15
SAFETY_BUFFER_DAYS = 7
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_yahoo_dividends(symbol, retries=3):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=10y&interval=1mo&events=div"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            result = (data.get("chart") or {}).get("result") or []
            if not result:
                return []
            events = (result[0].get("events") or {}).get("dividends") or {}
            parsed = []
            for ev in events.values():
                ts = ev.get("date")
                amt = ev.get("amount")
                if ts is None or amt is None:
                    continue
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                parsed.append((d, float(amt)))
            parsed.sort(key=lambda x: x[0], reverse=True)
            return parsed
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            print(f"  [!] {symbol}: HTTP {e.code}")
            return None
        except Exception as e:
            print(f"  [!] {symbol}: {e}")
            time.sleep(1)
    return None


def classify_frequency(ex_dates_sorted_desc):
    """Identique a update_dividends.py -- memes seuils, meme logique, pour que
    dividend_state.json reste dans un format que le reste du pipeline comprend telle quelle."""
    if len(ex_dates_sorted_desc) < 2:
        return "inconnu", 91
    gaps = [(ex_dates_sorted_desc[i] - ex_dates_sorted_desc[i + 1]).days
            for i in range(min(5, len(ex_dates_sorted_desc) - 1))]
    median_gap = sorted(gaps)[len(gaps) // 2]
    if median_gap <= 45:
        return "mensuel", median_gap
    elif median_gap <= 135:
        return "trimestriel", median_gap
    elif median_gap <= 270:
        return "semestriel", median_gap
    elif median_gap <= 400:
        return "annuel", median_gap
    return "irregulier", median_gap


def main():
    div_payers = load_json(os.path.join(OUT_DIR, "div_payers.json"), [])
    if not div_payers:
        print("Aucun div_payers.json -- rien a faire.")
        return

    state = load_json(os.path.join(OUT_DIR, "dividend_state.json"), {})
    today = datetime.now(timezone.utc).date()

    updated, kept_future, no_data, errors = 0, 0, 0, 0

    for i, p in enumerate(div_payers):
        symbol = p["ticker"]
        print(f"[{i + 1}/{len(div_payers)}] {symbol}")
        parsed = fetch_yahoo_dividends(symbol)

        if parsed is None:
            errors += 1
            time.sleep(SECONDS_BETWEEN_CALLS)
            continue
        if not parsed:
            no_data += 1
            time.sleep(SECONDS_BETWEEN_CALLS)
            continue

        st = state.setdefault(symbol, {})
        existing_last = None
        if st.get("lastExDate"):
            try:
                existing_last = datetime.strptime(st["lastExDate"], "%Y-%m-%d").date()
            except ValueError:
                existing_last = None

        yahoo_last_date, yahoo_last_amount = parsed[0]
        freq_class, gap = classify_frequency([d for d, _ in parsed])

        if existing_last and existing_last >= today:
            # Deja confirme et futur par Alpha Vantage -- ne jamais degrader.
            # On rafraichit seulement la frequence/cadence (historique Yahoo plus profond).
            kept_future += 1
            st["frequencyClass"] = freq_class
            st["typicalGapDays"] = gap
        else:
            final_last_date, final_last_amount = yahoo_last_date, yahoo_last_amount
            if existing_last and existing_last > final_last_date:
                final_last_date = existing_last
                final_last_amount = st.get("lastAmount", yahoo_last_amount)

            st["frequencyClass"] = freq_class
            st["typicalGapDays"] = gap
            st["lastExDate"] = final_last_date.isoformat()
            st["lastAmount"] = final_last_amount
            st["nextCheckNotBefore"] = (
                final_last_date + timedelta(days=max(gap - SAFETY_BUFFER_DAYS, 1))
            ).isoformat()
            updated += 1

        st["yahooHistoryAt"] = today.isoformat()
        st["yahooHistoryDepth"] = len(parsed)
        time.sleep(SECONDS_BETWEEN_CALLS)

    save_json(os.path.join(OUT_DIR, "dividend_state.json"), state)
    print(f"\n=== Termine : {updated} mis a jour, {kept_future} deja optimaux "
          f"(date future Alpha Vantage conservee), {no_data} sans historique Yahoo, "
          f"{errors} erreurs, sur {len(div_payers)} tickers ===")


if __name__ == "__main__":
    main()
