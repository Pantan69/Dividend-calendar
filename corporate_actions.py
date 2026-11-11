"""
Operations sur titres : splits, regroupements d'actions, dividendes speciaux, scissions.

Sert a jouer un gros changement de cours, meme quand rien ne change factuellement.
Sources :
  - Massive (ex-Polygon.io), API officielle, cle gratuite dans MASSIVE_API_KEY : splits et regroupements
    a venir, dividendes classes « speciaux » (dividend_type = SC), pour tout le marche US.
    Sans cle, cette partie est simplement sautee.
  - Yahoo (chart events=div) : historique pour reperer un dividende special dans l'univers
    (montant >= 2,2 x la mediane des 4 versements precedents, versements precedents stables).
  - data/corporate_actions_manual.json : scissions, dividendes speciaux declares, splits releves a la main
    (aucune source gratuite structuree n'existe pour les scissions).

Sortie : data/corporate_actions.json. Chaque ligne :
  ticker, name, type (split | reverse_split | stock_dividend | special_dividend | spinoff),
  date (date d'effet / de detachement, None si pas encore fixee), detail, amount/pct, inUniverse, src.

Usage : python corporate_actions.py [--horizon 60]
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

OUT_DIR = "data"
UA = "DividendCalendar/1.0 (github.com/Pantan69/Dividend-calendar)"
MASSIVE = "https://api.massive.com"
BIG_ONE_SHOT_PCT = 3.0      # hors univers : dividende special >= 3 % du cours pour alerter
SPECIAL_RATIO = 2.2         # dans l'univers : montant >= 2,2 x la mediane des 4 derniers (versements stables) = special
EXCLUDE_NAME = re.compile(r"\b(ETF|ETN|Fund|Portfolio|iShares|SPDR|ProShares|Direxion|Preferred|Notes?|Series|Depositary|Warrants?|Rights?)\b", re.I)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def http_json(url, retries=3):
    """None si la source refuse ou ne repond pas (on n'insiste pas sur un refus 401/403/429)."""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


# ---------------------------------------------------------------- Massive
def massive_pages(path, params, key, max_pages=6):
    """Parcourt les pages d'un endpoint Massive (la gratuite limite le debit : pause entre les appels)."""
    params = dict(params, apiKey=key, limit=1000)
    url = f"{MASSIVE}{path}?{urllib.parse.urlencode(params)}"
    out = []
    for _ in range(max_pages):
        j = http_json(url)
        if not j:
            break
        out += j.get("results") or []
        nxt = j.get("next_url")
        if not nxt:
            break
        url = nxt + ("&" if "?" in nxt else "?") + "apiKey=" + key
        time.sleep(13)  # plan gratuit : 5 appels par minute
    return out


def massive_splits(key, universe, today_iso):
    rows = massive_pages("/v3/reference/splits", {"execution_date.gte": today_iso, "sort": "execution_date", "order": "asc"}, key)
    out = []
    for r in rows:
        a, b = r.get("split_to"), r.get("split_from")  # split_to pour split_from : 2-for-1 -> to=2, from=1
        if not a or not b or a == b:
            continue
        fmt = lambda x: str(int(x)) if x == int(x) else str(x)
        typ = "split" if a > b else "reverse_split"
        word = "Split" if a > b else "Regroupement"
        out.append({"ticker": r["ticker"], "name": "", "type": typ, "date": r["execution_date"],
                    "detail": f"{word} {fmt(a)} pour {fmt(b)}", "inUniverse": r["ticker"] in universe,
                    "src": "massive"})
    return out


def massive_specials(key, universe, today_iso):
    rows = massive_pages("/v3/reference/dividends",
                         {"ex_dividend_date.gte": today_iso, "dividend_type": "SC", "sort": "ex_dividend_date", "order": "asc"}, key)
    out = []
    for r in rows:
        amt = r.get("cash_amount")
        if not amt:
            continue
        out.append({"ticker": r["ticker"], "name": "", "type": "special_dividend", "date": r["ex_dividend_date"],
                    "amount": amt, "pct": None,
                    "detail": f"Dividende spécial de {amt} $ (paiement {r.get('pay_date') or '?'})",
                    "inUniverse": r["ticker"] in universe, "src": "massive"})
    return out


# ------------------------------------------------------------------ Yahoo
def yahoo_chart(symbol, rng="2y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol.replace('.', '-'))}?range={rng}&interval=1d&events=div"
    j = http_json(url)
    try:
        res = j["chart"]["result"][0]
        ev = (res.get("events") or {}).get("dividends", {})
        h = sorted((datetime.fromtimestamp(e["date"], timezone.utc).date().isoformat(), e["amount"]) for e in ev.values())
        return h, res["meta"].get("regularMarketPrice"), res["meta"].get("longName")
    except (TypeError, KeyError, IndexError):
        return [], None, None


def detect_specials(dividends, today_iso):
    """Dans l'univers : un dividende a venir nettement superieur aux versements habituels."""
    out, seen = [], set()
    for d in dividends:
        if d.get("exDate", "") < today_iso or d["ticker"] in seen:
            continue
        seen.add(d["ticker"])
        hist, _, _ = yahoo_chart(d["ticker"])
        prev = [a for dt, a in hist if dt < d["exDate"]][-4:]
        time.sleep(0.15)
        if len(prev) < 2:
            continue
        med = statistics.median(prev)
        if min(prev) <= 0 or max(prev) / min(prev) > 1.6:
            continue  # payeur variable : un gros montant n'y est pas forcement un special
        if d["amount"] >= SPECIAL_RATIO * med:
            out.append({"ticker": d["ticker"], "name": d.get("name", ""), "type": "special_dividend",
                        "date": d["exDate"], "amount": d["amount"], "pct": d.get("pct"),
                        "detail": f"Dividende nettement supérieur à l'habituel ({d['amount']} $ contre environ {round(med, 3)} $)",
                        "inUniverse": True, "src": "auto"})
    return out


def fill_names_and_pct(actions, universe_names):
    """Complete le nom et le % (cours Yahoo) des lignes hors univers venant de Massive."""
    for a in actions:
        if a["name"]:
            continue
        a["name"] = universe_names.get(a["ticker"], "")
        if a["type"] == "special_dividend" and a.get("pct") is None and not a["inUniverse"]:
            _, px, nm = yahoo_chart(a["ticker"], "5d")
            time.sleep(0.15)
            if nm and not a["name"]:
                a["name"] = nm
            if px:
                a["pct"] = round(a["amount"] / px * 100, 2)
        elif not a["name"]:
            _, _, nm = yahoo_chart(a["ticker"], "5d")
            time.sleep(0.15)
            a["name"] = nm or ""
        a["etf"] = bool(EXCLUDE_NAME.search(a["name"]))
    # hors univers : un dividende special < 3 % n'est pas une alerte
    return [a for a in actions if a["inUniverse"] or a["type"] != "special_dividend" or (a.get("pct") or 0) >= BIG_ONE_SHOT_PCT]


# ------------------------------------------------------------------ main
def main():
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()

    tickers = load_json("tickers.json", [])
    universe = {t["ticker"] for t in tickers}
    names = {t["ticker"]: t["name"] for t in tickers}
    dividends = load_json(os.path.join(OUT_DIR, "dividends.json"), [])
    manual = load_json(os.path.join(OUT_DIR, "corporate_actions_manual.json"), [])
    previous = load_json(os.path.join(OUT_DIR, "corporate_actions.json"), {}).get("actions", [])

    actions = []
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if key:
        auto = massive_splits(key, universe, today_iso) + massive_specials(key, universe, today_iso)
        auto = fill_names_and_pct(auto, names)
        print(f"Massive : {len(auto)} operations (splits et dividendes speciaux)")
        actions += auto
    else:
        print("MASSIVE_API_KEY absente : splits et speciaux hors univers non collectes (cle gratuite sur massive.com).")
        # on conserve ce que la derniere collecte avec cle avait trouve et qui est encore a venir
        actions += [a for a in previous if a.get("src") == "massive" and a.get("date") and a["date"] >= today_iso]

    specials = detect_specials([d for d in dividends if d.get("price")], today_iso)
    print(f"Dividendes speciaux detectes dans l'univers (historique Yahoo) : {len(specials)}")
    actions += specials

    for m in manual:
        m = dict(m)
        m["inUniverse"] = m["ticker"] in universe
        m["src"] = m.get("src", "manual")
        m.setdefault("etf", bool(EXCLUDE_NAME.search(m.get("name", ""))))
        actions.append(m)

    # % d'un dividende special saisi a la main : repris de la ligne de dividende correspondante
    pct_by = {(d["ticker"], d["exDate"]): d.get("pct") for d in dividends}
    for a in actions:
        if a["type"] == "special_dividend" and a.get("pct") is None:
            a["pct"] = pct_by.get((a["ticker"], a.get("date")))

    # 14 jours d'historique : une operation passee reste visible
    keep_from = (today - timedelta(days=14)).isoformat()
    keys = {(a["ticker"], a["type"], a.get("date")) for a in actions}
    for a in previous:
        k = (a["ticker"], a["type"], a.get("date"))
        if a.get("date") and keep_from <= a["date"] < today_iso and k not in keys:
            actions.append(a)
            keys.add(k)

    # dedoublonnage : le manuel l'emporte sur l'auto pour une meme (ticker, type, date)
    best = {}
    for a in actions:
        k = (a["ticker"], a["type"], a.get("date"))
        if k not in best or a.get("src") == "manual":
            best[k] = a
    actions = sorted(best.values(), key=lambda a: (a.get("date") or "9999", a["ticker"]))
    save_json(os.path.join(OUT_DIR, "corporate_actions.json"),
              {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actions": actions})
    print(f"Ecrit data/corporate_actions.json : {len(actions)} operations")


if __name__ == "__main__":
    main()
