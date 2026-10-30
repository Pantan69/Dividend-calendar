"""
Filet de securite : interroge Yahoo Finance (quoteSummary : prochaine date ex-div declaree) pour
TOUT l'univers (tickers.json) et complete data/dividends.json avec les dividendes a venir que
Alpha Vantage n'a pas (encore) remontes.

Pourquoi : Alpha Vantage (25 requetes/jour) publie les dividendes declares tard, et ne repasse sur un
titre qu'~80 jours apres une confirmation -- un decalage de calendrier (KVUE) ou une annonce recente
(EIX) passe donc a travers. Yahoo, lui, expose la prochaine ex-date des qu'elle est declaree, pour
tous les tickers, en ~3 minutes.

Acces : cookie + "crumb" (comme yfinance). Endpoint non officiel.

FIABILITE DU MONTANT (champ amountSrc) -- la date ex-div vient de Yahoo ; le montant, lui, doit etre
traite avec prudence (bug DMLP du 30/10 : on affichait le dividende PRECEDENT, 1.27 au lieu de 0.88) :
  - "declared" : montant declare, confirme par Nasdaq (date + montant), Alpha Vantage ou a la main.
  - "estimate" : taux annuel Yahoo (dividendRate) / frequence -- tombe juste pour les dividendes
                 reguliers, y compris apres une hausse (21 titres sur 22 verifies contre Nasdaq).
  - "variable" : historique des derniers versements instable (royalties, MLP, supplementaires...) :
                 le montant n'est PAS fiable, a verifier a la main avant d'agir.
Le site affiche "estimate" et "variable" avec un "≈" (et "[!]" pour variable).

Usage : python check_upcoming_yahoo.py [--dry-run] [--horizon 75]
  --dry-run : n'ecrit rien, affiche seulement ce qui changerait.
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta, timezone

OUT_DIR = "data"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SECONDS_BETWEEN_CALLS = 0.12
SAFETY_BUFFER_DAYS = 10  # aligne avec update_dividends.py
FREQ_PER_YEAR = {"mensuel": 12, "trimestriel": 4, "semestriel": 2, "annuel": 1}
VARIABLE_CV = 0.12  # ecart-type / moyenne des derniers versements au-dela duquel le montant est "variable"
# Payeurs dont le montant change a chaque versement (royalties/minerals, MLP, armateurs, ADR chinois a
# dividende annuel/semestriel, etc.) : jamais fiable sans confirmation, donc toujours "variable" tant
# que ni Nasdaq ni une verification manuelle n'a confirme le montant.
KNOWN_VARIABLE = {
    "BSM", "DMLP", "ARLP", "SBR", "KRP", "VNOM", "TPL", "INSW", "FRO", "TRMD", "ZIM", "DHT", "STNG", "TNK", "NAT",
    "EDU", "JD", "BABA", "VIPS", "BEKE", "NTES", "TME", "FUTU", "PDD", "BIDU", "YUMC", "ZTO", "TAL", "ATHM",
    "CRK", "SD", "PBF", "AESI", "OXM", "CALM", "RGR", "SWBI", "TDG", "GOLF", "HVT", "MED", "SIRI",
}
UP_TOLERANCE = 1.15    # hausse au-dela de +15 % vs montant precedent : suspect -> variable
DOWN_TOLERANCE = 0.95  # baisse de plus de 5 % : suspect (CCL : Yahoo disait -25 %) -> variable
NASDAQ_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                  "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
    try:
        opener.open("https://fc.yahoo.com", timeout=15).read()
    except urllib.error.HTTPError:
        pass  # 404 attendu, seul le cookie nous interesse
    crumb = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
    if not crumb or "<" in crumb:
        raise RuntimeError(f"crumb invalide: {crumb[:80]!r}")
    return opener, crumb


def yahoo_symbol(t):
    return t.replace(".", "-")


def fetch_next_exdiv(opener, crumb, ticker):
    """Renvoie (ex_date, last_amount, pay_date, cours, taux_annuel) ou None. ex_date peut etre passee."""
    mods = "calendarEvents,summaryDetail,defaultKeyStatistics,price"
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(yahoo_symbol(ticker))}"
           f"?modules={mods}&crumb={urllib.parse.quote(crumb)}")
    for attempt in range(3):
        try:
            data = json.loads(opener.open(url, timeout=15).read().decode())
            res = (data.get("quoteSummary") or {}).get("result") or []
            if not res:
                return None
            r = res[0]
            ce, sd, ks = r.get("calendarEvents") or {}, r.get("summaryDetail") or {}, r.get("defaultKeyStatistics") or {}
            ts = (ce.get("exDividendDate") or {}).get("raw") or (sd.get("exDividendDate") or {}).get("raw")
            if not ts:
                return None
            ex = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            amt = (ks.get("lastDividendValue") or {}).get("raw")
            pay_ts = (ce.get("dividendDate") or {}).get("raw")
            pay = datetime.fromtimestamp(pay_ts, tz=timezone.utc).date() if pay_ts else None
            px = ((r.get("price") or {}).get("regularMarketPrice") or {}).get("raw")
            rate = (sd.get("dividendRate") or {}).get("raw")
            return ex, amt, pay, px, rate
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4)
                continue
            if e.code in (401, 403):
                raise
            return None
        except Exception:
            time.sleep(1)
    return None


def nasdaq_declared(ticker, ex_iso):
    """Montant declare par Nasdaq pour cette ex-date (surtout les titres cotes Nasdaq), sinon None."""
    url = f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(ticker)}/dividends?assetclass=stocks"
    try:
        req = urllib.request.Request(url, headers=NASDAQ_HEADERS)
        j = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        rows = (((j or {}).get("data") or {}).get("dividends") or {}).get("rows") or []
        for r in rows[:6]:
            m, d, y = r["exOrEffDate"].split("/")
            if f"{y}-{m}-{d}" == ex_iso:
                return float(r["amount"].replace("$", "").replace(",", ""))
    except Exception:
        pass
    return None


def recent_amounts(opener, ticker, n=4):
    """Les n derniers montants verses (historique Yahoo), du plus recent au plus ancien."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yahoo_symbol(ticker))}"
           f"?range=2y&interval=1mo&events=div")
    try:
        j = json.loads(opener.open(url, timeout=15).read().decode())
        ev = ((j["chart"]["result"][0].get("events") or {}).get("dividends") or {})
        vals = sorted(ev.values(), key=lambda e: e["date"], reverse=True)
        return [float(e["amount"]) for e in vals[:n]]
    except Exception:
        return []


def main():
    dry = "--dry-run" in sys.argv
    horizon_days = 75
    if "--horizon" in sys.argv:
        horizon_days = int(sys.argv[sys.argv.index("--horizon") + 1])

    tickers = load_json("tickers.json", [])
    names = {t["ticker"]: t["name"] for t in tickers}
    state = load_json(os.path.join(OUT_DIR, "dividend_state.json"), {})
    divs = load_json(os.path.join(OUT_DIR, "dividends.json"), [])
    by_ticker = {d["ticker"]: d for d in divs}

    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=horizon_days)

    opener, crumb = make_session()
    print(f"Session Yahoo OK. {len(tickers)} tickers a controler (horizon {horizon_days}j).")

    new, changed, same, errors = [], [], 0, 0
    for t in tickers:
        tk = t["ticker"]
        try:
            got = fetch_next_exdiv(opener, crumb, tk)
        except urllib.error.HTTPError as e:
            print(f"  [!] {tk}: HTTP {e.code} -- arret (acces refuse).")
            errors += 1
            break
        time.sleep(SECONDS_BETWEEN_CALLS)
        if not got:
            continue
        ex, amt, pay, px, rate = got
        if ex < today or ex > horizon:
            continue
        cur = by_ticker.get(tk)
        st = state.get(tk, {})
        last = amt or st.get("lastAmount")
        n = FREQ_PER_YEAR.get(st.get("frequencyClass"), 4)
        same_date = bool(cur) and cur.get("exDate") == ex.isoformat()

        # Entree deja confirmee (Alpha Vantage / a la main) pour cette meme date : on n'y touche pas.
        if same_date and cur.get("src") != "yahoo":
            same += 1
            if px and not cur.get("price") and not dry:
                cur["price"] = round(px, 2)
                cur["pct"] = round(cur["amount"] / px * 100, 4)
            continue
        # Entree Yahoo dont le montant est deja "declared" (verifie Nasdaq) : idem.
        if same_date and cur.get("amountSrc") == "declared":
            same += 1
            continue

        # 1) montant declare par Nasdaq : le plus fiable
        amount, amount_src = nasdaq_declared(tk, ex.isoformat()), "declared"
        time.sleep(0.2)
        if amount is None:
            # 2) estimation : taux annuel Yahoo / frequence (juste pour les dividendes reguliers)
            est = None
            if rate:
                # La frequence enregistree peut etre fausse (SO etait classe "mensuel") : on prend celle
                # (1, 2, 4 ou 12 versements/an) qui donne un montant le plus proche du dernier connu.
                ref0 = st.get("lastAmount") or last
                if ref0:
                    n = min((1, 2, 4, 12), key=lambda k: abs(rate / k - ref0))
                est = rate / n
            if est:
                # meme montant a 2 % pres : on garde la valeur la plus precise (Yahoo arrondit a 3 decimales)
                for ref in (st.get("lastAmount"), last):
                    if ref and abs(est - ref) / ref < 0.02:
                        est = ref
                        break
            amount = est or st.get("lastAmount") or last
            amount_src = "estimate"
            # 3) suspect -> "variable" : a verifier a la main
            prev = st.get("lastAmount") or last
            if tk in KNOWN_VARIABLE:
                amount_src = "variable"
            elif prev and amount and not (prev * DOWN_TOLERANCE <= amount <= prev * UP_TOLERANCE):
                amount_src = "variable"
            # ... ou historique des derniers versements instable
            hist = recent_amounts(opener, tk)
            time.sleep(SECONDS_BETWEEN_CALLS)
            if len(hist) >= 3 and statistics.mean(hist) > 0:
                if statistics.pstdev(hist) / statistics.mean(hist) > VARIABLE_CV:
                    amount_src = "variable"
        if not amount:
            continue
        amount = round(float(amount), 6)

        entry = {
            "ticker": tk, "name": names.get(tk, tk), "exDate": ex.isoformat(), "amount": amount,
            "price": (cur or {}).get("price"), "pct": (cur or {}).get("pct"),
            "src": "yahoo", "amountSrc": amount_src,
        }
        if px:  # cours Yahoo : visible tout de suite sur le site (qui masque les valeurs sans cours)
            entry["price"] = round(px, 2)
            entry["pct"] = round(amount / px * 100, 4)

        if same_date:
            same += 1
            if cur.get("amount") != amount or cur.get("amountSrc") != amount_src:
                changed.append((tk, cur["exDate"], cur["amount"], ex.isoformat(), amount, amount_src))
        elif cur and cur.get("exDate", "") >= today.isoformat():
            changed.append((tk, cur["exDate"], cur["amount"], ex.isoformat(), amount, amount_src))
        else:
            new.append((tk, ex.isoformat(), amount, amount_src))

        if not dry:
            by_ticker[tk] = entry
            gap = st.get("typicalGapDays") or 91
            st = state.setdefault(tk, {})
            st["lastExDate"] = ex.isoformat()
            st["lastAmount"] = amount
            st.setdefault("frequencyClass", "trimestriel")
            st.setdefault("typicalGapDays", gap)
            st["nextCheckNotBefore"] = (ex + timedelta(days=max(gap - SAFETY_BUFFER_DAYS, 1))).isoformat()
            st["yahooUpcomingAt"] = today.isoformat()

    print(f"\n=== Yahoo : {same} deja identiques, {len(new)} NOUVEAUX, {len(changed)} modifies ===")
    for tk, ex, amt, src in sorted(new, key=lambda x: x[1]):
        print(f"  NOUVEAU  {ex}  {tk:7} ${amt}  [{src}]")
    for tk, oex, oamt, ex, amt, src in sorted(changed, key=lambda x: x[3]):
        print(f"  MODIFIE  {tk:7} {oex} ${oamt}  ->  {ex} ${amt}  [{src}]")
    nvar = [d["ticker"] for d in by_ticker.values()
            if d.get("amountSrc") == "variable" and d["exDate"] >= today.isoformat()]
    if nvar:
        print(f"\n  [!] Montant VARIABLE (a verifier a la main) : {', '.join(sorted(nvar))}")

    if not dry:
        save_json(os.path.join(OUT_DIR, "dividends.json"), list(by_ticker.values()))
        save_json(os.path.join(OUT_DIR, "dividend_state.json"), state)
        print("\nEcrit : dividends.json + dividend_state.json")


if __name__ == "__main__":
    main()
