"""Grille de coûts eToro (vérifiée 07/2026) — fonctions PURES et testables.

But : encoder UNE FOIS les règles de frais eToro pour que le backtest puisse
répondre honnêtement à « la stratégie a-t-elle un edge NET des coûts ? ».
Aucune dépendance externe : ni numpy, ni pandas ici (arithmétique pure Python).

DEUX familles de coûts, avec des ASSIETTES DIFFÉRENTES — c'est la source d'erreur
classique, donc on est explicite :

1) SPREAD (coût de transaction). Fraction du NOTIONNEL TRADÉ, appliquée à CHAQUE
   changement de position. On modélise le round-trip (aller-retour = 2 côtés) car
   toute position ouverte finit par être fermée. `round_trip_spread_frac`.

2) OVERNIGHT / carry (coût de financement). Taux ANNUALISÉ appliqué au NOTIONNEL
   D'EXPOSITION (= units × price), divisé par 365, multiplié par le nombre de
   NUITS CALENDAIRES de détention. Le week-end compte naturellement (3 nuits
   calendaires vendredi→lundi ≈ le ×3 officiel), crypto = 7j/7 même formule.
   `overnight_frac_per_night`.

CLARIFICATION LEVIER (piège eToro) : le « notionnel » = exposition = units × price.
C'est DÉJÀ la valeur exposée au marché. Le levier sert à réduire la MARGE
immobilisée, PAS à re-multiplier l'exposition. Donc l'overnight s'applique sur le
notionnel tel quel, on NE re-multiplie PAS par le levier. Le paramètre `leverage`
de `trade_cost` est purement informatif/documentaire (l'appelant a déjà calculé
le notionnel comme units×price = capital×levier).

Tous les spreads « from » d'eToro sont des PLANCHERS : le réel peut être pire.
D'où `SPREAD_MULT` (défaut 1.0 ; scénario pessimiste 2.0) qui multiplie TOUS les
spreads pour tester la robustesse.
"""

import math

# ─────────────────────────────────────────────────────────────────────────────
# Multiplicateur global de spread. 1.0 = grille officielle (plancher).
# 2.0 = scénario pessimiste (le "from" n'est jamais tenu). Modifiable par le
# backtest pour lancer les deux scénarios.
# ─────────────────────────────────────────────────────────────────────────────
SPREAD_MULT = 1.0


def set_spread_mult(mult):
    """Fixe le multiplicateur global de spread (1.0 officiel, 2.0 pessimiste)."""
    global SPREAD_MULT
    SPREAD_MULT = float(mult)


# ─────────────────────────────────────────────────────────────────────────────
# Mapping symbole → classe d'actif. Univers FIXE et connu (attention : univers
# SÉLECTIONNÉ a posteriori → biais de sélection, cf. rapport backtest).
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL_CLASS = {
    "SPX500": "index", "NSDQ100": "index", "GER40": "index",
    "EURUSD": "fx", "GBPUSD": "fx", "USDJPY": "fx",
    "GOLD": "commodity", "SILVER": "commodity",
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto",
    "AAPL": "stock", "NVDA": "stock", "MSFT": "stock",
    "TSLA": "stock", "AMZN": "stock",
}

_CLASSES = {"index", "fx", "commodity", "crypto", "stock"}


class SymbolClassifier:
    """Helper pur pour mapper un symbole eToro → sa classe d'actif.

    Accepte aussi directement un nom de classe (idempotent), ce qui permet aux
    fonctions de coût d'accepter indifféremment un symbole ou une classe.
    """

    @staticmethod
    def classify(symbol_or_class):
        key = str(symbol_or_class).upper()
        if key in SYMBOL_CLASS:
            return SYMBOL_CLASS[key]
        low = str(symbol_or_class).lower()
        if low in _CLASSES:
            return low
        raise KeyError(
            "Symbole/classe inconnu : %r (attendus : %s ou une classe %s)"
            % (symbol_or_class, sorted(SYMBOL_CLASS), sorted(_CLASSES))
        )


# ─────────────────────────────────────────────────────────────────────────────
# SPREADS round-trip (fraction du notionnel tradé, 2 côtés).
# Dérivés des "from" one-side officiels × 2 (aller-retour).
#   indices        : 0.015% one-side → 0.03%  round-trip
#   EURUSD/USDJPY  : ~0.01% one-side → 0.02%  round-trip (1 pip)
#   GBPUSD         : ~0.015% one-side → 0.03% round-trip (2 pips)
#   or (GOLD)      : 0.025% one-side → 0.05%  round-trip
#   argent (SILVER): 0.12%  one-side → 0.24%  round-trip
#   actions CFD    : 0.15%  PAR CÔTÉ → 0.30%  round-trip
#   crypto         : 1%     PAR CÔTÉ → 2.00%  round-trip (explicite eToro, Bronze)
# Table PAR SYMBOLE (précise), avec repli PAR CLASSE (représentatif, plus grossier).
# ─────────────────────────────────────────────────────────────────────────────
_ROUND_TRIP_SPREAD_BY_SYMBOL = {
    "SPX500": 0.0003, "NSDQ100": 0.0003, "GER40": 0.0003,
    "EURUSD": 0.0002, "USDJPY": 0.0002, "GBPUSD": 0.0003,
    "GOLD": 0.0005, "SILVER": 0.0024,
    "BTC": 0.02, "ETH": 0.02, "SOL": 0.02,
    "AAPL": 0.003, "NVDA": 0.003, "MSFT": 0.003, "TSLA": 0.003, "AMZN": 0.003,
}

# Repli par classe : on prend la valeur la PLUS CONSERVATRICE (pire) de la classe
# pour ne jamais sous-estimer les coûts quand on interroge par classe.
#   fx        → GBPUSD (0.03%)      ; commodity → SILVER (0.24%)
_ROUND_TRIP_SPREAD_BY_CLASS = {
    "index": 0.0003,
    "fx": 0.0003,
    "commodity": 0.0024,
    "crypto": 0.02,
    "stock": 0.003,
}


def round_trip_spread_frac(symbol_or_class):
    """Fraction de spread ALLER-RETOUR sur le notionnel tradé (× SPREAD_MULT).

    Ex. round_trip_spread_frac('BTC')    -> 0.02  (2 %, à SPREAD_MULT=1)
        round_trip_spread_frac('AAPL')   -> 0.003 (0.30 %)
        round_trip_spread_frac('SPX500') -> 0.0003 (0.03 %)

    Appliqué à CHAQUE changement de position (le notionnel effectivement tradé).
    """
    key = str(symbol_or_class).upper()
    if key in _ROUND_TRIP_SPREAD_BY_SYMBOL:
        base = _ROUND_TRIP_SPREAD_BY_SYMBOL[key]
    else:
        cls = SymbolClassifier.classify(symbol_or_class)
        base = _ROUND_TRIP_SPREAD_BY_CLASS[cls]
    return base * SPREAD_MULT


# ─────────────────────────────────────────────────────────────────────────────
# OVERNIGHT — taux ANNUALISÉS (fraction du notionnel d'exposition / an).
# Les CRÉDITS short sont modélisés à 0 (prudence : on ne compte jamais un frais
# comme un gain). Long / short séparés.
#   indices  : LONG 9.0%/an, SHORT 1.5%/an
#   fx       : 3%/an des DEUX côtés (par prudence ; short a parfois un petit crédit)
#   commodity: LONG 9.5%/an (or), SHORT 0 (léger crédit → 0). SILVER traité comme
#              l'or faute de taux publié distinct (hypothèse notée).
#   stock CFD: LONG 10.4%/an (6.4% eToro + ~4% benchmark), SHORT 0 (easy-to-borrow)
#   crypto   : 11.5%/an des DEUX côtés, 7j/7
# ─────────────────────────────────────────────────────────────────────────────
_OVERNIGHT_ANNUAL = {
    "index":     {"long": 0.09,  "short": 0.015},
    "fx":        {"long": 0.03,  "short": 0.03},
    "commodity": {"long": 0.095, "short": 0.0},
    "stock":     {"long": 0.104, "short": 0.0},
    "crypto":    {"long": 0.115, "short": 0.115},
}

_DAYS_PER_YEAR = 365.0  # eToro divise le taux annuel par 365 (nuits calendaires)


def overnight_annual_rate(symbol_or_class, is_buy):
    """Taux overnight ANNUALISÉ (fraction/an) pour ce symbole et ce sens.

    is_buy=True -> position LONG ; is_buy=False -> SHORT. Crédits short = 0.
    """
    cls = SymbolClassifier.classify(symbol_or_class)
    side = "long" if is_buy else "short"
    return _OVERNIGHT_ANNUAL[cls][side]


def overnight_frac_per_night(symbol_or_class, is_buy):
    """Fraction du notionnel d'EXPOSITION prélevée PAR NUIT CALENDAIRE.

    = taux_annuel / 365. À multiplier ensuite par (notionnel × nb_nuits).
    N'inclut PAS le levier (le notionnel EST déjà units×price = l'exposition).

    Ex. overnight_frac_per_night('AAPL', True) = 0.104/365 ≈ 0.000285.
    """
    return overnight_annual_rate(symbol_or_class, is_buy) / _DAYS_PER_YEAR


def trade_cost(notional, symbol, is_buy, nights_held, leverage=1.0):
    """Coût total en $ d'un aller-retour détenu `nights_held` nuits.

    Paramètres
    ----------
    notional : float
        Exposition = units × price (DÉJÀ le montant exposé au marché, levier inclus
        dans le sens où units a été dimensionné avec le levier). C'est l'assiette
        des DEUX coûts.
    symbol : str
        Symbole eToro (ou classe).
    is_buy : bool
        True = long (overnight long), False = short (overnight short, souvent 0).
    nights_held : int
        Nombre de NUITS CALENDAIRES de détention (week-ends comptés).
    leverage : float
        INFORMATIF UNIQUEMENT — n'entre PAS dans le calcul (le notionnel est déjà
        l'exposition). Présent pour documenter l'intention de l'appelant.

    Retour
    ------
    dict {spread, overnight, total} en $.

    Détail :
      spread   = round_trip_spread_frac(symbol) × notional
      overnight= overnight_frac_per_night(symbol, is_buy) × notional × nights_held
    """
    _ = leverage  # explicitement ignoré (voir docstring)
    spread = round_trip_spread_frac(symbol) * notional
    overnight = overnight_frac_per_night(symbol, is_buy) * notional * max(0, nights_held)
    return {"spread": spread, "overnight": overnight, "total": spread + overnight}


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSION EUR→USD (NOTÉE SÉPARÉMENT — hors backtest de stratégie).
# eToro convertit 1.5% à l'ENTRÉE et 1.5% à la SORTIE du capital réel. C'est un
# coût FIXE sur les 200 € (aller-retour ≈ 3% du capital), PAS un coût par trade.
# ─────────────────────────────────────────────────────────────────────────────
FX_CONVERSION_ONE_WAY = 0.015


def eur_conversion_cost(capital_eur):
    """Coût aller-retour de conversion EUR→USD→EUR sur le capital (≈3%)."""
    one_way = FX_CONVERSION_ONE_WAY * capital_eur
    return {"in": one_way, "out": one_way, "round_trip": 2 * one_way}


if __name__ == "__main__":
    # Démo rapide (aucun réseau).
    print("Spread round-trip BTC   :", round_trip_spread_frac("BTC"))
    print("Spread round-trip AAPL  :", round_trip_spread_frac("AAPL"))
    print("Spread round-trip SPX500:", round_trip_spread_frac("SPX500"))
    print("Exemple officiel AAPL x5, 3 nuits :",
          trade_cost(5000, "AAPL", True, 3, leverage=5)["overnight"])
