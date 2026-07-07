"""Backtest HONNÊTE de la stratégie momentum + vol-targeting du bot eToro.

Question UNIQUE à laquelle ce fichier répond :
    « La stratégie time-series momentum / vol-targeting a-t-elle un edge NET des
      coûts eToro, sur CET univers ? »

Ce backtest est conçu pour se RÉFUTER lui-même, pas pour flatter :
  - anti look-ahead strict (les signaux sont .shift(1) — info ≤ t-1 seulement) ;
  - coûts eToro modélisés de façon CONSERVATRICE (cf. costs.py + note ci-dessous) ;
  - split OUT-OF-SAMPLE 70/30 — seul l'OOS compte pour le verdict ;
  - Deflated Sharpe Ratio (Bailey & López de Prado) qui pénalise le Sharpe pour le
    nombre d'essais (data-mining) ;
  - biais de sélection ASSUMÉ et signalé : l'univers de 16 instruments a été choisi
    a posteriori (survivants connus), ce qui gonfle mécaniquement les résultats.

Dépendances : numpy + pandas UNIQUEMENT (aucune autre). Toutes les métriques sont
recalculées à la main (pas d'empyrical, pas de vectorbt).

MODÈLE DE COÛT DE SPREAD (choix explicite et conservateur) :
    On applique `round_trip_spread_frac` sur le notionnel TRADÉ à CHAQUE
    changement de position (turnover |Δpoids|). C'est un MAJORANT : un aller-retour
    parfaitement synchronisé ne paierait qu'un demi round-trip par côté ; ici, en
    comptant un round-trip complet par variation, on surestime plutôt qu'on ne
    sous-estime les coûts. Cohérent avec un backtest qui cherche à se réfuter.
"""

import datetime as dt
import json
import math
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

import costs

# Univers = watchlist du bot (symbole eToro, Yahoo, classe, requête).
from marketdata import WATCHLIST

ANN = 252  # jours de bourse / an (annualisation Sharpe/Sortino/vol)
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


# ═════════════════════════════════════════════════════════════════════════════
# 1) DONNÉES — fetcher réseau best-effort, INJECTABLE pour les tests.
# ═════════════════════════════════════════════════════════════════════════════
def fetch_history(yahoo_symbol, range="3y", timeout=10):
    """Clôtures journalières → pd.Series indexée par date (best-effort).

    Utilise l'API chart Yahoo avec period1/period2 (le paramètre `range` de Yahoo
    ne connaît pas '3y' ; on convertit '<n>y' en une fenêtre de n années). Renvoie
    une Series VIDE en cas d'échec (jamais bloquant), même pattern que marketdata.py.
    """
    try:
        years = float(str(range).lower().rstrip("y")) if str(range).endswith("y") else 3.0
    except ValueError:
        years = 3.0
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    p1 = now - int(years * 366 * 24 * 3600)
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(yahoo_symbol)
           + "?period1=%d&period2=%d&interval=1d" % (p1, now))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        res = data["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        idx = pd.to_datetime([t for t, c in zip(ts, closes) if c is not None], unit="s")
        vals = [float(c) for c in closes if c is not None]
        s = pd.Series(vals, index=pd.DatetimeIndex(idx).normalize(), name=yahoo_symbol)
        return s[~s.index.duplicated(keep="last")].sort_index()
    except Exception:
        return pd.Series(dtype=float, name=yahoo_symbol)


# ═════════════════════════════════════════════════════════════════════════════
# 2) STRATÉGIE — time-series momentum + vol-targeting + filtre de régime.
#    ANTI LOOK-AHEAD : tout signal est .shift(1) → n'utilise QUE des clôtures ≤ t-1.
# ═════════════════════════════════════════════════════════════════════════════
def compute_positions(closes, mom_lookback=63, vol_lookback=63, target_vol=0.15,
                      lev_cap=1.5, regime_k=0.5, blend=False):
    """Série de poids de position (signés, en fraction du capital du sleeve).

    - Direction = signe du momentum trailing (rendement sur `mom_lookback` jours).
      `blend=True` mélange 21 j et 63 j (moyenne des deux rendements trailing).
    - FILTRE DE RÉGIME : on ne prend position que si le momentum NORMALISÉ PAR LA VOL
      dépasse `regime_k` : |ret_lookback| > regime_k × vol_annualisée. Sinon → flat
      (cash). Défaut regime_k=0.5 : il faut un momentum d'au moins 0.5× la vol
      annualisée pour justifier le risque. Sous ce seuil, le signe est trop bruité.
    - SIZING vol-target : |poids| = min(target_vol / vol_réalisée_annualisée, lev_cap).
      Levier effectif CAPÉ à lev_cap (défaut 1.5). Le risque par position est borné.
    - .shift(1) FINAL : le poids appliqué au jour t est calculé sur l'information
      disponible à la clôture t-1. C'est le garde-fou anti look-ahead central.

    Retour : (positions: pd.Series, daily_returns: pd.Series) alignées sur l'index.
    """
    closes = closes.astype(float)
    rets = closes.pct_change()

    if blend:
        mom = 0.5 * (closes / closes.shift(21) - 1.0) + 0.5 * (closes / closes.shift(63) - 1.0)
    else:
        mom = closes / closes.shift(mom_lookback) - 1.0

    vol_daily = rets.rolling(vol_lookback).std()
    vol_ann = vol_daily * math.sqrt(ANN)

    direction = np.sign(mom)
    # Momentum normalisé par la vol (sans division par zéro).
    strength = mom.abs() / vol_ann.replace(0.0, np.nan)
    in_regime = strength > regime_k

    size = (target_vol / vol_ann.replace(0.0, np.nan)).clip(upper=lev_cap)
    raw_pos = direction * size
    raw_pos = raw_pos.where(in_regime, 0.0).fillna(0.0)

    # ANTI LOOK-AHEAD : décalage d'un jour. Le poids de t vient des données de t-1.
    positions = raw_pos.shift(1).fillna(0.0)
    return positions, rets


def _calendar_nights(index):
    """Nombre de nuits CALENDAIRES entre bougies consécutives (série alignée sur index).

    Capture naturellement le week-end : Ven→Lun = 3 nuits pour indices/actions
    (Yahoo saute le week-end) ; crypto = 7j/7 → 1 nuit. Reproduit le ×3 officiel.
    """
    days = pd.Series(index, index=index).diff().dt.days
    return days.fillna(0.0).clip(lower=0.0)


def backtest_instrument(symbol, closes, rebalance_days=1, mom_lookback=63,
                        vol_lookback=63, target_vol=0.15, lev_cap=1.5,
                        regime_k=0.5, blend=False):
    """Backtest d'un instrument : rendements GROSS/NET quotidiens + coûts détaillés.

    `rebalance_days` : 1 = quotidien ; 5 = « hold hebdo » (on ne met à jour le poids
    que tous les 5 jours de bourse ; entre-temps le poids est maintenu). Montre la
    sensibilité au turnover/coûts.

    COÛTS (fractions du capital du sleeve, agrégés ensuite au portefeuille) :
      spread_t   = round_trip_spread_frac(symbol) × |Δpoids_t|          (à chaque trade)
      overnight_t= overnight_frac_per_night(symbol, long?) × |poids_t| × nuits_t
    """
    positions, rets = compute_positions(
        closes, mom_lookback=mom_lookback, vol_lookback=vol_lookback,
        target_vol=target_vol, lev_cap=lev_cap, regime_k=regime_k, blend=blend)

    # Hold hebdo : on ne rafraîchit le poids que tous les `rebalance_days` jours.
    if rebalance_days > 1:
        held = positions.copy()
        vals = positions.values.copy()
        last = 0.0
        for i in range(len(vals)):
            if i % rebalance_days == 0:
                last = vals[i]
            vals[i] = last
        held = pd.Series(vals, index=positions.index)
        positions = held

    nights = _calendar_nights(closes.index)

    # Coût de spread : round-trip sur le notionnel tradé (turnover), à chaque change.
    turnover = positions.diff().abs().fillna(positions.abs())  # 1re ligne = ouverture
    spread_frac = costs.round_trip_spread_frac(symbol)
    spread_cost = spread_frac * turnover

    # Coût overnight : dépend du sens (long/short → taux différents, short = 0).
    ovn_long = costs.overnight_frac_per_night(symbol, True)
    ovn_short = costs.overnight_frac_per_night(symbol, False)
    per_night = pd.Series(np.where(positions > 0, ovn_long,
                          np.where(positions < 0, ovn_short, 0.0)), index=positions.index)
    overnight_cost = per_night * positions.abs() * nights

    gross_ret = (positions * rets).fillna(0.0)
    net_ret = (gross_ret - spread_cost - overnight_cost).fillna(0.0)

    return {
        "symbol": symbol,
        "positions": positions,
        "gross_ret": gross_ret,
        "net_ret": net_ret,
        "spread_cost": spread_cost.fillna(0.0),
        "overnight_cost": overnight_cost.fillna(0.0),
        "turnover": turnover,
    }


# ═════════════════════════════════════════════════════════════════════════════
# 3) MÉTRIQUES — recalculées à la main (ni empyrical ni scipy).
# ═════════════════════════════════════════════════════════════════════════════
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p):
    """Inverse de la CDF normale (algorithme d'Acklam, sans scipy)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def max_drawdown(returns):
    """Max drawdown (négatif) d'une série de rendements."""
    r = returns.fillna(0.0)
    equity = (1.0 + r).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if len(dd) else 0.0


def compute_metrics(returns, index=None):
    """Dict de métriques annualisées pour une série de rendements journaliers.

    Sharpe/Sortino annualisés (×√252, taux sans risque = 0 — hypothèse notée).
    CAGR sur le temps CALENDAIRE réel. Calmar = CAGR / |maxDD|.
    """
    r = returns.dropna()
    n = len(r)
    if index is None:
        index = r.index
    out = {"n_obs": n}
    if n < 5:
        return {**out, "sharpe": float("nan"), "sortino": float("nan"),
                "cagr": float("nan"), "max_dd": float("nan"), "calmar": float("nan"),
                "vol_ann": float("nan"), "total_return": float("nan")}
    mean = r.mean()
    std = r.std(ddof=1)
    sharpe = (mean / std * math.sqrt(ANN)) if std > 0 else float("nan")
    downside = r.copy()
    downside[downside > 0] = 0.0
    dd_dev = math.sqrt((downside ** 2).mean())
    sortino = (mean / dd_dev * math.sqrt(ANN)) if dd_dev > 0 else float("nan")
    equity = (1.0 + r).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    try:
        span_days = (r.index[-1] - r.index[0]).days
        years = span_days / 365.25 if span_days > 0 else n / ANN
    except Exception:
        years = n / ANN
    cagr = (equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else float("nan")
    mdd = max_drawdown(r)
    calmar = (cagr / abs(mdd)) if mdd < 0 and not math.isnan(cagr) else float("nan")
    return {**out, "sharpe": sharpe, "sortino": sortino, "cagr": cagr,
            "max_dd": mdd, "calmar": calmar,
            "vol_ann": std * math.sqrt(ANN), "total_return": total_return}


def trade_stats(positions, net_ret):
    """Hit-rate NET par trade + nb de trades. Un trade = run de signe constant ≠ 0."""
    pos = positions.values
    nr = net_ret.values
    trades = []
    cursign = 0
    comp = 1.0
    is_open = False
    for i in range(len(pos)):
        s = 0 if pos[i] == 0 else (1 if pos[i] > 0 else -1)
        if s != cursign:
            if is_open:
                trades.append(comp - 1.0)
            is_open = s != 0
            comp = 1.0
            cursign = s
        if is_open:
            comp *= (1.0 + nr[i])
    if is_open:
        trades.append(comp - 1.0)
    n_tr = len(trades)
    hit = float(np.mean([1.0 if t > 0 else 0.0 for t in trades])) if n_tr else float("nan")
    return {"n_trades": n_tr, "hit_rate": hit}


def annual_turnover(positions, index):
    """Turnover annualisé = somme des |Δpoids| ramenée à l'année calendaire."""
    tot = positions.diff().abs().fillna(positions.abs()).sum()
    try:
        years = (index[-1] - index[0]).days / 365.25
    except Exception:
        years = len(index) / ANN
    return float(tot / years) if years > 0 else float("nan")


def deflated_sharpe(net_returns, n_trials, sr_variance):
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Pénalise le Sharpe observé pour le nombre d'essais `n_trials` (data-mining) et
    pour la non-normalité (skew/kurtosis). Renvoie la probabilité que le vrai Sharpe
    soit > 0 compte tenu de la sélection. Un DSR < 0.95 = pas significatif après
    correction. Tout est en Sharpe PAR PÉRIODE (cohérent avec `sr_variance`).

    sr_variance : variance des Sharpe (par période) À TRAVERS les essais.
    """
    r = pd.Series(net_returns).dropna().values
    n = len(r)
    if n < 20:
        return None
    s0 = r.std(ddof=0)
    if s0 <= 0:
        return None
    sr = r.mean() / r.std(ddof=1)  # Sharpe par période
    m = r.mean()
    skew = float(((r - m) ** 3).mean() / s0 ** 3)
    kurt = float(((r - m) ** 4).mean() / s0 ** 4)  # NON-excess (normale = 3)
    gamma = 0.5772156649015329  # Euler-Mascheroni
    if sr_variance <= 0 or n_trials < 2:
        return None
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    sr0 = math.sqrt(sr_variance) * ((1.0 - gamma) * z1 + gamma * z2)  # Sharpe max attendu sous H0
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr))
    dsr = _norm_cdf((sr - sr0) * math.sqrt(n - 1) / denom)
    return {"sr_per_period": sr, "sr0_expected_max": sr0, "dsr": dsr,
            "skew": skew, "kurt_non_excess": kurt, "n_trials": n_trials}


# ═════════════════════════════════════════════════════════════════════════════
# 4) HURDLE DE COÛT — combien de rendement brut/an juste pour couvrir les coûts.
# ═════════════════════════════════════════════════════════════════════════════
def cost_hurdle_table(rebalance_days=1, assumed_full_roundtrips_per_year=None):
    """Coût annuel minimal (rendement brut requis) par classe, avant tout profit.

    Modèle : détenir une exposition |poids|≈1 pendant un an coûte
        overnight_long_annuel  +  spread_round_trip × (nb d'allers-retours complets/an).
    Le nb d'A/R dépend de l'agressivité du signal ; on tabule un CAS de RÉFÉRENCE :
    un flip complet à chaque rebalance (majorant), soit ~252/rebalance_days A/R.
    """
    rebals_per_year = ANN / max(1, rebalance_days)
    trips = assumed_full_roundtrips_per_year if assumed_full_roundtrips_per_year else rebals_per_year
    rows = []
    for cls in ["index", "fx", "commodity", "crypto", "stock"]:
        spread_rt = costs.round_trip_spread_frac(cls)
        ovn = costs.overnight_annual_rate(cls, True)
        spread_annual = spread_rt * trips
        rows.append({
            "class": cls,
            "spread_round_trip": spread_rt,
            "overnight_annual_long": ovn,
            "spread_cost_annual_@freq": spread_annual,
            "breakeven_gross_annual": ovn + spread_annual,
        })
    return {"rebalance_days": rebalance_days, "roundtrips_per_year": trips, "rows": rows}


# ═════════════════════════════════════════════════════════════════════════════
# 5) ORCHESTRATION — univers complet, portefeuille, split OOS, rapport.
# ═════════════════════════════════════════════════════════════════════════════
def _split_is_oos(series, oos_frac=0.30):
    """Coupe temporelle : (in-sample 70%, out-of-sample 30%). OOS = fin de période."""
    n = len(series)
    cut = int(round(n * (1.0 - oos_frac)))
    return series.iloc[:cut], series.iloc[cut:]


def run_backtest(watchlist=None, fetcher=fetch_history, range="3y",
                 rebalance_days=1, mom_lookback=63, vol_lookback=63,
                 target_vol=0.15, lev_cap=1.5, regime_k=0.5, blend=False,
                 spread_mult=1.0, oos_frac=0.30, n_trials=64):
    """Lance le backtest sur tout l'univers et renvoie un dict de résultats.

    `n_trials` : nb d'essais pour le Deflated Sharpe (data-mining). Défaut 64 ≈
    16 instruments × 4 configurations explorées (lookback 63 / blend / daily /
    weekly). C'est une estimation HONNÊTE de l'espace de recherche.
    """
    costs.set_spread_mult(spread_mult)
    watchlist = watchlist or WATCHLIST

    per_instrument = {}
    gross_cols, net_cols = {}, {}
    for etoro_sym, yahoo, cls, _query in watchlist:
        closes = fetcher(yahoo, range=range) if _accepts_range(fetcher) else fetcher(yahoo)
        if closes is None or len(closes) < max(mom_lookback, vol_lookback) + 30:
            continue
        bt = backtest_instrument(
            etoro_sym, closes, rebalance_days=rebalance_days,
            mom_lookback=mom_lookback, vol_lookback=vol_lookback,
            target_vol=target_vol, lev_cap=lev_cap, regime_k=regime_k, blend=blend)
        bt["class"] = cls
        per_instrument[etoro_sym] = bt
        gross_cols[etoro_sym] = bt["gross_ret"]
        net_cols[etoro_sym] = bt["net_ret"]

    if not per_instrument:
        return {"error": "aucune donnée exploitable", "instruments": {}}

    # Portefeuille équipondéré : moyenne des sleeves disponibles chaque jour.
    gross_df = pd.DataFrame(gross_cols).sort_index()
    net_df = pd.DataFrame(net_cols).sort_index()
    port_gross = gross_df.mean(axis=1)
    port_net = net_df.mean(axis=1)

    # Coût total (drag) au niveau portefeuille = gross - net cumulés.
    eq_g = (1 + port_gross.fillna(0)).cumprod()
    eq_n = (1 + port_net.fillna(0)).cumprod()
    cost_drag_total = float(eq_g.iloc[-1] - eq_n.iloc[-1])  # en fraction d'equity

    # Split OOS sur le portefeuille.
    g_is, g_oos = _split_is_oos(port_gross, oos_frac)
    n_is, n_oos = _split_is_oos(port_net, oos_frac)

    portfolio = {
        "gross_full": compute_metrics(port_gross),
        "net_full": compute_metrics(port_net),
        "gross_is": compute_metrics(g_is),
        "net_is": compute_metrics(n_is),
        "gross_oos": compute_metrics(g_oos),
        "net_oos": compute_metrics(n_oos),
        "cost_drag_frac_of_equity": cost_drag_total,
        "equity_gross_final": float(eq_g.iloc[-1]),
        "equity_net_final": float(eq_n.iloc[-1]),
    }

    # Métriques par instrument (OOS net = ce qui compte) + survie aux coûts.
    inst_metrics = {}
    oos_net_sr_perperiod = []  # pour la variance des essais (Deflated Sharpe)
    for sym, bt in per_instrument.items():
        _, net_oos = _split_is_oos(bt["net_ret"], oos_frac)
        _, gross_oos = _split_is_oos(bt["gross_ret"], oos_frac)
        m_net_oos = compute_metrics(net_oos)
        m_gross_oos = compute_metrics(gross_oos)
        ts = trade_stats(bt["positions"], bt["net_ret"])
        turn = annual_turnover(bt["positions"], bt["positions"].index)
        drag = float((1 + bt["gross_ret"]).cumprod().iloc[-1]
                     - (1 + bt["net_ret"]).cumprod().iloc[-1])
        inst_metrics[sym] = {
            "class": bt["class"],
            "net_oos": m_net_oos,
            "gross_oos": m_gross_oos,
            "hit_rate": ts["hit_rate"],
            "n_trades": ts["n_trades"],
            "annual_turnover": turn,
            "cost_drag_frac": drag,
            "survives_costs_oos": (not math.isnan(m_net_oos["cagr"])) and m_net_oos["cagr"] > 0,
        }
        rr = net_oos.dropna()
        if len(rr) > 20 and rr.std(ddof=1) > 0:
            oos_net_sr_perperiod.append(rr.mean() / rr.std(ddof=1))

    sr_var = float(np.var(oos_net_sr_perperiod, ddof=1)) if len(oos_net_sr_perperiod) > 1 else 0.0
    dsr = deflated_sharpe(n_oos, n_trials=n_trials, sr_variance=sr_var)

    return {
        "params": {"range": range, "rebalance_days": rebalance_days,
                   "mom_lookback": mom_lookback, "vol_lookback": vol_lookback,
                   "target_vol": target_vol, "lev_cap": lev_cap,
                   "regime_k": regime_k, "blend": blend, "spread_mult": spread_mult,
                   "oos_frac": oos_frac, "n_trials": n_trials},
        "portfolio": portfolio,
        "instruments": inst_metrics,
        "deflated_sharpe": dsr,
        "cost_hurdle": cost_hurdle_table(rebalance_days),
        "_series": {"port_gross": port_gross, "port_net": port_net},  # usage interne/tests
    }


def _accepts_range(fetcher):
    """Le fetcher accepte-t-il un kwarg `range` ? (compat stubs de test minimalistes)."""
    try:
        import inspect
        return "range" in inspect.signature(fetcher).parameters
    except (TypeError, ValueError):
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 6) RAPPORT TEXTE — verdict honnête.
# ═════════════════════════════════════════════════════════════════════════════
def _fmt(x, pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "   n/a"
    return ("%+7.2f%%" % (x * 100)) if pct else ("%7.3f" % x)


def format_report(res):
    L = []
    P = res["portfolio"]
    p = res["params"]
    L.append("=" * 78)
    L.append("BACKTEST HONNÊTE — momentum + vol-targeting sur univers eToro")
    L.append("=" * 78)
    L.append("Params: lookback=%s blend=%s rebal=%dj target_vol=%.0f%% lev_cap=%.1f "
             "regime_k=%.2f spread_mult=%.1f"
             % (p["mom_lookback"], p["blend"], p["rebalance_days"],
                p["target_vol"] * 100, p["lev_cap"], p["regime_k"], p["spread_mult"]))
    L.append("")

    # Verdict = signe du CAGR NET OOS du portefeuille.
    net_oos_cagr = P["net_oos"]["cagr"]
    net_oos_sharpe = P["net_oos"]["sharpe"]
    edge = (not math.isnan(net_oos_cagr)) and net_oos_cagr > 0 and net_oos_sharpe > 0
    dsr = res.get("deflated_sharpe")
    dsr_ok = dsr is not None and dsr["dsr"] > 0.95
    L.append("VERDICT — edge NET positif en OUT-OF-SAMPLE ? : %s" % ("OUI" if edge else "NON"))
    L.append("  CAGR net OOS = %s | Sharpe net OOS = %s"
             % (_fmt(net_oos_cagr, pct=True).strip(), _fmt(net_oos_sharpe).strip()))
    if dsr is not None:
        L.append("  Deflated Sharpe (n_trials=%d) = %.3f  → significatif après data-mining ? %s"
                 % (dsr["n_trials"], dsr["dsr"], "OUI" if dsr_ok else "NON"))
    else:
        L.append("  Deflated Sharpe : n/a (échantillon OOS trop court)")
    L.append("")

    # Tableau gross vs net, IS vs OOS.
    L.append("PORTEFEUILLE — GROSS vs NET (in-sample 70% | out-of-sample 30%)")
    hdr = "  %-14s %10s %10s %10s %10s" % ("métrique", "GROSS IS", "NET IS", "GROSS OOS", "NET OOS")
    L.append(hdr)
    L.append("  " + "-" * (len(hdr) - 2))

    def row(label, key, pct=False):
        L.append("  %-14s %10s %10s %10s %10s" % (
            label, _fmt(P["gross_is"][key], pct), _fmt(P["net_is"][key], pct),
            _fmt(P["gross_oos"][key], pct), _fmt(P["net_oos"][key], pct)))
    row("CAGR", "cagr", pct=True)
    row("Sharpe", "sharpe")
    row("Sortino", "sortino")
    row("MaxDD", "max_dd", pct=True)
    row("Calmar", "calmar")
    row("Vol ann", "vol_ann", pct=True)
    L.append("")
    L.append("  Drag de coût total (portefeuille) = %.2f%% de l'equity finale"
             % (P["cost_drag_frac_of_equity"] * 100))
    L.append("  Equity finale : GROSS ×%.3f  vs  NET ×%.3f"
             % (P["equity_gross_final"], P["equity_net_final"]))
    L.append("")

    # Survie par classe.
    L.append("SURVIE AUX COÛTS — par instrument (métriques NET OOS)")
    L.append("  %-8s %-9s %9s %9s %9s %8s %8s %6s"
             % ("symbol", "classe", "CAGRnet", "Sharpe", "MaxDD", "hit%", "turn/an", "surv"))
    L.append("  " + "-" * 74)
    by_class = {}
    for sym, m in sorted(res["instruments"].items(), key=lambda kv: kv[1]["class"]):
        no = m["net_oos"]
        L.append("  %-8s %-9s %9s %9s %9s %8s %8.1f %6s" % (
            sym, m["class"], _fmt(no["cagr"], pct=True).strip(),
            _fmt(no["sharpe"]).strip(), _fmt(no["max_dd"], pct=True).strip(),
            ("%.0f" % (m["hit_rate"] * 100)) if not math.isnan(m["hit_rate"]) else "n/a",
            m["annual_turnover"], "OK" if m["survives_costs_oos"] else "—"))
        by_class.setdefault(m["class"], [0, 0])
        by_class[m["class"]][1] += 1
        if m["survives_costs_oos"]:
            by_class[m["class"]][0] += 1
    L.append("")
    L.append("  Récapitulatif survie par classe (net OOS CAGR>0) :")
    for cls, (surv, tot) in sorted(by_class.items()):
        L.append("    %-10s : %d/%d instruments survivent" % (cls, surv, tot))
    L.append("")

    # Hurdle de coût.
    ch = res["cost_hurdle"]
    L.append("HURDLE DE COÛT — rendement brut annuel requis juste pour couvrir les coûts")
    L.append("  (cas de référence : flip complet à chaque rebalance = %.0f A/R/an)"
             % ch["roundtrips_per_year"])
    L.append("  %-10s %14s %16s %16s %18s" % (
        "classe", "spread A/R", "overnight/an", "spread coût/an", "BREAKEVEN brut/an"))
    L.append("  " + "-" * 76)
    for r in ch["rows"]:
        L.append("  %-10s %13.3f%% %15.2f%% %15.2f%% %17.2f%%" % (
            r["class"], r["spread_round_trip"] * 100, r["overnight_annual_long"] * 100,
            r["spread_cost_annual_@freq"] * 100, r["breakeven_gross_annual"] * 100))
    L.append("")
    L.append("LIMITES ASSUMÉES :")
    L.append("  - Biais de SÉLECTION : univers 16 instruments choisi a posteriori (survivants).")
    L.append("  - Modèle de spread CONSERVATEUR (round-trip par changement = majorant).")
    L.append("  - Rendements close-to-close ; slippage/impact réels ≥ modèle. Rf=0.")
    L.append("  - Conversion EUR→USD (~3% A/R sur le capital) HORS backtest — coût fixe séparé.")
    L.append("=" * 78)
    return "\n".join(L)


def main(fetcher=fetch_history, **kwargs):
    """Lance le backtest (defaults), imprime le rapport, renvoie le dict de résultats.

    NB : par défaut fait un appel RÉSEAU (Yahoo). Passer un `fetcher` injecté pour
    des tests hors-ligne.
    """
    res = run_backtest(fetcher=fetcher, **kwargs)
    if "error" in res:
        print("ERREUR:", res["error"])
        return res
    print(format_report(res))
    return res


if __name__ == "__main__":
    main()
