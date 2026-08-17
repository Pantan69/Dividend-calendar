"""
Tourne une fois par jour. Choisit jusqu'a 25 tickers (limite gratuite Alpha Vantage)
parmi data/div_payers.json a interroger aujourd'hui, par ordre de priorite :

  1. Reportes de la veille (deja en retard, geres via data/av_queue_overflow.json)
  2. Tickers trimestriels/semestriels/annuels dont les resultats sont tombes hier
     (data/earnings.json) -- l'annonce du dividende sort generalement a ce moment-la
  3. Tickers mensuels/irreguliers (ou pas encore assez d'historique pour classer la
     frequence) dont la cadence calculee (derniere date + ecart type - 10j) est atteinte
  4. S'il reste de la place : les plus proches de leur seuil, pour ne rien gaspiller
  5. Si ca deborde au-dela de 25 : le surplus part dans av_queue_overflow.json pour demain

Met a jour :
  - data/dividend_state.json : etat par ticker (derniere date/montant, frequence, seuils)
  - data/dividends.json      : donnees publiques que le site lit (ex-date a venir + %)
  - data/av_queue_overflow.json
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timedelta, timezone

AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
if not AV_KEY:
    raise SystemExit("ALPHAVANTAGE_API_KEY manquante.")

OUT_DIR = "data"
DAILY_CAP = 25
# 10j de marge : avec ~500 tickers a dividende repartis sur des cycles ~91j (trimestriel),
# en moyenne ~5-6 tickers/jour entrent dans la fenetre -- tres large marge sous les 25/jour
# disponibles. Objectif utilisateur : confirmer chaque dividende ~10j avant son echeance.
SAFETY_BUFFER_DAYS = 10
EARNINGS_LOOKBACK_DAYS = 2  # on considere "resultats d'hier" avec un peu de marge


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def call_alpha_vantage_dividends(symbol, retries=2):
    params = {"function": "DIVIDENDS", "symbol": symbol, "apikey": AV_KEY}
    url = f"https://www.alphavantage.co/query?{urllib.parse.urlencode(params)}"
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"  [!] Echec Alpha Vantage {symbol}: {e}")
            time.sleep(3)
    return None


def classify_frequency(ex_dates_sorted_desc):
    """ex_dates_sorted_desc: liste de date() du plus recent au plus ancien."""
    if len(ex_dates_sorted_desc) < 2:
        return "inconnu", 91  # par defaut on suppose trimestriel tant qu'on n'a pas 2 points
    gaps = [(ex_dates_sorted_desc[i] - ex_dates_sorted_desc[i+1]).days for i in range(min(3, len(ex_dates_sorted_desc)-1))]
    median_gap = sorted(gaps)[len(gaps)//2]
    if median_gap <= 45:
        return "mensuel", median_gap
    elif median_gap <= 135:
        return "trimestriel", median_gap
    elif median_gap <= 270:
        return "semestriel", median_gap
    elif median_gap <= 400:
        return "annuel", median_gap
    return "irregulier", median_gap


def build_today_queue(state, div_payers, earnings, overflow, today):
    """Renvoie la liste ordonnee des tickers a interroger aujourd'hui (avant plafonnement a 25)."""
    tier1_overflow = [t for t in overflow if t in {p["ticker"] for p in div_payers}]

    earnings_yesterday_tickers = set()
    for e in earnings:
        if not e.get("date"):
            continue
        try:
            edate = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if 0 <= (today - edate).days <= EARNINGS_LOOKBACK_DAYS:
            earnings_yesterday_tickers.add(e["ticker"])

    tier2, tier3, later = [], [], []
    for p in div_payers:
        symbol = p["ticker"]
        if symbol in tier1_overflow:
            continue
        st = state.get(symbol, {})
        freq = st.get("frequencyClass", "inconnu")
        next_check = st.get("nextCheckNotBefore")
        next_check_date = datetime.strptime(next_check, "%Y-%m-%d").date() if next_check else today

        if freq in ("trimestriel", "semestriel", "annuel", "inconnu") and symbol in earnings_yesterday_tickers:
            tier2.append(symbol)
        elif next_check_date <= today:
            tier3.append(symbol)
        else:
            later.append((symbol, next_check_date))

    later.sort(key=lambda x: x[1])
    tier4 = [s for s, _ in later]

    return tier1_overflow + tier2 + tier3 + tier4


def main():
    with open("tickers.json", encoding="utf-8") as f:
        all_tickers = {t["ticker"]: t["name"] for t in json.load(f)}

    div_payers = load_json(os.path.join(OUT_DIR, "div_payers.json"), [])
    if not div_payers:
        print("Aucun div_payers.json -- lance d'abord classify_dividends.py")
        return

    state = load_json(os.path.join(OUT_DIR, "dividend_state.json"), {})
    earnings = load_json(os.path.join(OUT_DIR, "earnings.json"), [])
    overflow = load_json(os.path.join(OUT_DIR, "av_queue_overflow.json"), [])
    dividends_public = load_json(os.path.join(OUT_DIR, "dividends.json"), [])

    today = datetime.now(timezone.utc).date()

    queue = build_today_queue(state, div_payers, earnings, overflow, today)
    todays_batch = queue[:DAILY_CAP]
    tomorrows_overflow = queue[DAILY_CAP:2*DAILY_CAP]  # on ne fait deborder que ce qui etait deja du/en retard

    print(f"=== {len(todays_batch)} tickers interroges aujourd'hui (sur {len(queue)} candidats) ===")

    dividends_by_ticker = {d["ticker"]: d for d in dividends_public}

    for i, symbol in enumerate(todays_batch):
        print(f"[Alpha Vantage] {i+1}/{len(todays_batch)} {symbol}")
        data = call_alpha_vantage_dividends(symbol)
        st = state.setdefault(symbol, {})
        st["lastChecked"] = today.isoformat()

        entries = (data or {}).get("data", [])
        parsed_dates = []
        for e in entries:
            try:
                d = datetime.strptime(e["ex_dividend_date"], "%Y-%m-%d").date()
                amt = float(e["amount"])
                parsed_dates.append((d, amt))
            except (ValueError, KeyError, TypeError):
                continue
        parsed_dates.sort(key=lambda x: x[0], reverse=True)

        if parsed_dates:
            most_recent_date, most_recent_amount = parsed_dates[0]
            freq_class, gap = classify_frequency([d for d, _ in parsed_dates])
            st["frequencyClass"] = freq_class
            st["typicalGapDays"] = gap
            st["lastExDate"] = most_recent_date.isoformat()
            st["lastAmount"] = most_recent_amount
            st["nextCheckNotBefore"] = (most_recent_date + timedelta(days=max(gap - SAFETY_BUFFER_DAYS, 1))).isoformat()

            # Si la date la plus recente est encore a venir (pas depassee), on la publie
            if most_recent_date >= today:
                dividends_by_ticker[symbol] = {
                    "ticker": symbol,
                    "name": all_tickers.get(symbol, symbol),
                    "exDate": most_recent_date.isoformat(),
                    "amount": most_recent_amount,
                    "price": (dividends_by_ticker.get(symbol) or {}).get("price"),
                    "pct": (dividends_by_ticker.get(symbol) or {}).get("pct"),
                }
        time.sleep(1)  # marge de securite, Alpha Vantage free = 5 appels/min max

    # Nettoyage : on retire du fichier public les entrees dont la date ex-div est desormais passee
    dividends_by_ticker = {
        t: d for t, d in dividends_by_ticker.items()
        if d.get("exDate") and datetime.strptime(d["exDate"], "%Y-%m-%d").date() >= today
    }

    save_json(os.path.join(OUT_DIR, "dividend_state.json"), state)
    save_json(os.path.join(OUT_DIR, "dividends.json"), list(dividends_by_ticker.values()))
    save_json(os.path.join(OUT_DIR, "av_queue_overflow.json"), tomorrows_overflow)

    print(f"\n=== Termine : {len(dividends_by_ticker)} dividendes a venir publies, {len(tomorrows_overflow)} reportes a demain ===")


if __name__ == "__main__":
    main()
