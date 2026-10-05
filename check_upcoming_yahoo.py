"""
Filet de securite : interroge Yahoo Finance (quoteSummary : prochaine date ex-div declaree) pour
TOUT l'univers (tickers.json) et complete data/dividends.json avec les dividendes a venir que
Alpha Vantage n'a pas (encore) remontes.

Pourquoi : Alpha Vantage (25 requetes/jour) publie les dividendes declares tard, et ne repasse sur un
titre qu'~80 jours apres une confirmation -- un decalage de calendrier (KVUE) ou une annonce recente
(EIX) passe donc a travers. Yahoo, lui, expose la prochaine ex-date des qu'elle est declaree, pour
tous les tickers, en ~3 minutes.

Acces : cookie + "crumb" (comme yfinance). Endpoint non officiel.

Usage : python check_upcoming_yahoo.py [--dry-run] [--horizon 75]
  --dry-run : n'ecrit rien, affiche seulement ce qui changerait.
"""
import json
import os
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
    """Renvoie (ex_date, amount, pay_date) ou None. ex_date peut etre passee."""
    mods = "calendarEvents,summaryDetail,defaultKeyStatistics"
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
            return ex, amt, pay
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
    for i, t in enumerate(tickers):
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
        ex, amt, pay = got
        if ex < today or ex > horizon:
            continue
        cur = by_ticker.get(tk)
        st = state.get(tk, {})
        amount = amt if amt else st.get("lastAmount")
        # Yahoo arrondit parfois (0.878 au lieu de 0.8775) : si notre dernier montant connu est
        # quasi identique (< 2 %), on garde le notre, plus precis.
        last_amt = st.get("lastAmount")
        if amount and last_amt and abs(amount - last_amt) / last_amt < 0.02:
            amount = last_amt
        if not amount:
            continue
        if cur and cur.get("exDate") == ex.isoformat():
            same += 1
            continue
        entry = {
            "ticker": tk, "name": names.get(tk, tk), "exDate": ex.isoformat(),
            "amount": round(float(amount), 6),
            "price": (cur or {}).get("price"), "pct": (cur or {}).get("pct"),
            "src": "yahoo",
        }
        if cur and cur.get("exDate", "") >= today.isoformat():
            changed.append((tk, cur["exDate"], cur["amount"], ex.isoformat(), entry["amount"]))
        else:
            new.append((tk, ex.isoformat(), entry["amount"]))
        if not dry:
            by_ticker[tk] = entry
            gap = st.get("typicalGapDays") or 91
            st = state.setdefault(tk, {})
            st["lastExDate"] = ex.isoformat()
            st["lastAmount"] = entry["amount"]
            st.setdefault("frequencyClass", "trimestriel")
            st.setdefault("typicalGapDays", gap)
            st["nextCheckNotBefore"] = (ex + timedelta(days=max(gap - SAFETY_BUFFER_DAYS, 1))).isoformat()
            st["yahooUpcomingAt"] = today.isoformat()

    print(f"\n=== Yahoo : {same} deja identiques, {len(new)} NOUVEAUX, {len(changed)} date/montant differents ===")
    for tk, ex, amt in sorted(new, key=lambda x: x[1]):
        print(f"  NOUVEAU  {ex}  {tk:7} ${amt}")
    for tk, oex, oamt, ex, amt in sorted(changed, key=lambda x: x[3]):
        print(f"  DIFF     {tk:7} calendrier: {oex} ${oamt}  ->  yahoo: {ex} ${amt}")

    if not dry:
        save_json(os.path.join(OUT_DIR, "dividends.json"), list(by_ticker.values()))
        save_json(os.path.join(OUT_DIR, "dividend_state.json"), state)
        print("\nEcrit : dividends.json + dividend_state.json")


if __name__ == "__main__":
    main()
