"""Données de marché historiques (Yahoo Finance, sans clé) → momentum & volatilité
CALCULÉS, injectés dans le contexte du cerveau.

But : ancrer les deux piliers de la doctrine dans des CHIFFRES RÉELS plutôt que
dans des pourcentages lus dans des articles — direction = signe du rendement passé,
taille = 1/variance récente [Moskowitz-Ooi-Pedersen ; Moreira-Muir]. La recherche
web garde le rôle du "pourquoi" (catalyseurs).

Best-effort : toute panne réseau/instrument est ignorée (jamais bloquant). Bougies
journalières (EOD), mises en cache une fois par jour UTC dans state/ (donc un seul
lot de requêtes Yahoo par jour, quel que soit le nombre de cycles).
"""
import datetime as dt
import json
import math
import os
import urllib.parse
import urllib.request

# Watchlist par défaut, couvrant TOUTES les classes autorisées. Éditable ici.
# (symbole eToro, symbole Yahoo, classe, requête pour la recherche d'instrument eToro)
WATCHLIST = [
    ("SPX500",  "^GSPC",    "index",     "S&P 500"),
    ("NSDQ100", "^NDX",     "index",     "Nasdaq 100"),
    ("GER40",   "^GDAXI",   "index",     "DAX 40"),
    ("EURUSD",  "EURUSD=X", "fx",        "EUR/USD"),
    ("GBPUSD",  "GBPUSD=X", "fx",        "GBP/USD"),
    ("USDJPY",  "JPY=X",    "fx",        "USD/JPY"),
    ("GOLD",    "GC=F",     "commodity", "Gold"),
    ("SILVER",  "SI=F",     "commodity", "Silver"),
    ("BTC",     "BTC-USD",  "crypto",    "Bitcoin"),
    ("ETH",     "ETH-USD",  "crypto",    "Ethereum"),
    ("SOL",     "SOL-USD",  "crypto",    "Solana"),
    ("AAPL",    "AAPL",     "stock",     "Apple"),
    ("NVDA",    "NVDA",     "stock",     "Nvidia"),
    ("MSFT",    "MSFT",     "stock",     "Microsoft"),
    ("TSLA",    "TSLA",     "stock",     "Tesla"),
    ("AMZN",    "AMZN",     "stock",     "Amazon"),
]

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def fetch_closes(yahoo_symbol, timeout=8):
    """Liste des clôtures journalières (ancien→récent), ou [] en cas d'échec."""
    # 6 mois de bougies: le momentum 3 mois (mom_3m) a besoin de >63 séances —
    # avec seulement 3mo il était TOUJOURS null. La fenêtre drawdown reste 3 mois.
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(yahoo_symbol) + "?range=6mo&interval=1d")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        q = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return [float(c) for c in q if c is not None]
    except Exception:
        return []


def _pct(a, b):
    return round((a / b - 1.0) * 100.0, 2) if b else None


def compute_stats(closes):
    """Momentum multi-horizon + volatilité journalière + drawdown + écart MM20.

    closes: clôtures chronologiques (ancien→récent). Renvoie None si trop court.
    """
    n = len(closes)
    if n < 6:
        return None
    last = closes[-1]

    def mom(days):
        return _pct(last, closes[-1 - days]) if n > days else None

    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, n) if closes[i - 1]]
    vol = None
    if len(rets) >= 5:
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        vol = round(math.sqrt(var) * 100.0, 2)  # volatilité JOURNALIÈRE en %
    window = closes[-63:]
    drawdown = _pct(last, max(window)) if window else None  # écart au plus-haut (négatif)
    ma20 = sum(closes[-20:]) / min(20, n)
    return {
        "last": round(last, 4),
        "mom_1w_%": mom(5), "mom_1m_%": mom(21), "mom_3m_%": mom(63),
        "vol_daily_%": vol,
        "drawdown_from_high_%": drawdown,
        "vs_ma20_%": _pct(last, ma20),
    }


def _today_utc():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def market_snapshot(watchlist=None, state_dir="state", fetcher=fetch_closes):
    """Table de stats par instrument (best-effort). Cachée 1×/jour UTC dans state/.

    Retourne une liste de dicts prête à injecter dans le contexte du cerveau.
    `fetcher` est injectable pour les tests (aucun réseau).
    """
    watchlist = watchlist or WATCHLIST
    cache_path = os.path.join(state_dir, "marketdata_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("date") == _today_utc() and cached.get("rows"):
            return cached["rows"]
    except (OSError, ValueError):
        pass

    rows = []
    for etoro_sym, yahoo, cls, query in watchlist:
        stats = compute_stats(fetcher(yahoo))
        if stats:
            rows.append({"symbol": etoro_sym, "class": cls,
                         "instrument_query": query, **stats})
    if rows:  # ne cache que des données non vides
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"date": _today_utc(), "rows": rows}, f)
        except OSError:
            pass
    return rows
