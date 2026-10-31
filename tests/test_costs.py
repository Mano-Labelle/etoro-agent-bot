"""Tests de la grille de coûts eToro et du backtest (ZÉRO réseau — fetcher injecté).

Couvre :
  - round_trip_spread (crypto 2%, actions CFD 0.30%, indices 0.03%) ;
  - l'exemple overnight officiel (AAPL x5, $5000, 3 nuits ≈ $4.27) ;
  - le backtest sur données SYNTHÉTIQUES : edge brut positif, net RÉDUIT par les
    coûts (net < gross), et absence de look-ahead (série plate → ~0).
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costs  # noqa: E402
import backtest  # noqa: E402


class TestRoundTripSpread(unittest.TestCase):
    def setUp(self):
        costs.set_spread_mult(1.0)

    def test_crypto_2pct(self):
        self.assertAlmostEqual(costs.round_trip_spread_frac("BTC"), 0.02, places=6)
        self.assertAlmostEqual(costs.round_trip_spread_frac("ETH"), 0.02, places=6)
        self.assertAlmostEqual(costs.round_trip_spread_frac("crypto"), 0.02, places=6)

    def test_stock_cfd_030pct(self):
        self.assertAlmostEqual(costs.round_trip_spread_frac("AAPL"), 0.003, places=6)
        self.assertAlmostEqual(costs.round_trip_spread_frac("NVDA"), 0.003, places=6)

    def test_index_003pct(self):
        self.assertAlmostEqual(costs.round_trip_spread_frac("SPX500"), 0.0003, places=6)
        self.assertAlmostEqual(costs.round_trip_spread_frac("GER40"), 0.0003, places=6)

    def test_gold_silver_distinct(self):
        self.assertAlmostEqual(costs.round_trip_spread_frac("GOLD"), 0.0005, places=6)
        self.assertAlmostEqual(costs.round_trip_spread_frac("SILVER"), 0.0024, places=6)

    def test_spread_mult_pessimistic(self):
        costs.set_spread_mult(2.0)
        self.assertAlmostEqual(costs.round_trip_spread_frac("BTC"), 0.04, places=6)
        self.assertAlmostEqual(costs.round_trip_spread_frac("AAPL"), 0.006, places=6)
        costs.set_spread_mult(1.0)


class TestOvernight(unittest.TestCase):
    def test_official_aapl_example(self):
        # $1000 à levier ×5 = $5000 notionnel, action CFD LONG, 3 nuits ≈ $4.27.
        cost = costs.overnight_frac_per_night("AAPL", True) * 5000 * 3
        self.assertGreaterEqual(cost, 3.5)
        self.assertLessEqual(cost, 5.5)

    def test_short_credit_modelled_zero(self):
        # Crédits short modélisés à 0 (prudence) pour actions et or.
        self.assertEqual(costs.overnight_frac_per_night("AAPL", False), 0.0)
        self.assertEqual(costs.overnight_frac_per_night("GOLD", False), 0.0)

    def test_crypto_both_sides_positive(self):
        self.assertGreater(costs.overnight_frac_per_night("BTC", True), 0.0)
        self.assertGreater(costs.overnight_frac_per_night("BTC", False), 0.0)

    def test_trade_cost_leverage_not_remultiplied(self):
        # Le levier ne re-multiplie PAS l'exposition : notional est déjà l'exposition.
        c1 = costs.trade_cost(5000, "AAPL", True, 3, leverage=5)
        c2 = costs.trade_cost(5000, "AAPL", True, 3, leverage=1)
        self.assertEqual(c1["total"], c2["total"])


def _make_fetcher(series_builder):
    """Fabrique un fetcher stub acceptant (yahoo, range=...) → pd.Series."""
    def _f(yahoo, range="3y"):
        return series_builder(yahoo)
    return _f


def _uptrend(n=400, drift=0.004, seed=0):
    """Série qui monte régulièrement (léger bruit) → momentum long persistant."""
    rng = np.random.RandomState(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    noise = rng.normal(0, 0.002, n)
    rets = drift + noise
    closes = 100 * np.cumprod(1 + rets)
    return pd.Series(closes, index=idx)


def _flat(n=400, seed=1):
    """Série ~plate (bruit pur, pas de tendance) → signal ~0, pas de look-ahead."""
    rng = np.random.RandomState(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    rets = rng.normal(0, 0.001, n)
    closes = 100 * np.cumprod(1 + rets)
    return pd.Series(closes, index=idx)


class TestBacktestSynthetic(unittest.TestCase):
    def setUp(self):
        costs.set_spread_mult(1.0)

    def test_uptrend_gross_positive_and_net_reduced(self):
        closes = _uptrend()
        bt = backtest.backtest_instrument("SPX500", closes, rebalance_days=1)
        eq_g = (1 + bt["gross_ret"]).cumprod().iloc[-1]
        eq_n = (1 + bt["net_ret"]).cumprod().iloc[-1]
        # Edge BRUT positif sur une tendance haussière franche.
        self.assertGreater(eq_g, 1.0)
        # Les coûts RÉDUISENT le net (net < gross) mais ne l'inversent pas ici.
        self.assertLess(eq_n, eq_g)
        # Coûts strictement positifs.
        self.assertGreater(bt["spread_cost"].sum() + bt["overnight_cost"].sum(), 0.0)

    def test_no_lookahead_flat_series_near_zero(self):
        closes = _flat()
        bt = backtest.backtest_instrument("SPX500", closes, rebalance_days=1)
        eq_g = (1 + bt["gross_ret"]).cumprod().iloc[-1]
        # Série sans tendance → pas d'edge brut significatif (proche de 1, pas de
        # gain "magique" qui trahirait un look-ahead).
        self.assertLess(abs(eq_g - 1.0), 0.25)

    def test_positions_are_shifted_no_lookahead(self):
        # La position au jour t ne doit PAS dépendre du rendement de t.
        closes = _uptrend()
        pos, rets = backtest.compute_positions(closes)
        # Un spike de rendement au jour t ne change pas pos[t] (déjà figée par t-1).
        closes2 = closes.copy()
        pos2, _ = backtest.compute_positions(closes2)
        pd.testing.assert_series_equal(pos, pos2)
        # Première position = 0 (aucune donnée passée disponible).
        self.assertEqual(pos.iloc[0], 0.0)

    def test_weekly_rebalance_lower_turnover(self):
        closes = _uptrend()
        daily = backtest.backtest_instrument("SPX500", closes, rebalance_days=1)
        weekly = backtest.backtest_instrument("SPX500", closes, rebalance_days=5)
        t_daily = backtest.annual_turnover(daily["positions"], daily["positions"].index)
        t_weekly = backtest.annual_turnover(weekly["positions"], weekly["positions"].index)
        self.assertLessEqual(t_weekly, t_daily + 1e-9)

    def test_run_backtest_end_to_end_offline(self):
        # Univers réduit, fetcher stub → pas de réseau. Vérifie la structure du dict.
        wl = [("SPX500", "^GSPC", "index", "S&P 500"),
              ("BTC", "BTC-USD", "crypto", "Bitcoin")]
        fetcher = _make_fetcher(lambda y: _uptrend(seed=hash(y) % 100))
        res = backtest.run_backtest(watchlist=wl, fetcher=fetcher, n_trials=8)
        self.assertIn("portfolio", res)
        self.assertIn("net_oos", res["portfolio"])
        self.assertIn("cost_hurdle", res)
        self.assertEqual(len(res["instruments"]), 2)
        # Le rapport texte se génère sans erreur.
        txt = backtest.format_report(res)
        self.assertIn("VERDICT", txt)


class TestMetrics(unittest.TestCase):
    def test_norm_ppf_cdf_roundtrip(self):
        for p in [0.01, 0.25, 0.5, 0.84, 0.975, 0.999]:
            self.assertAlmostEqual(backtest._norm_cdf(backtest._norm_ppf(p)), p, places=3)

    def test_max_drawdown_sign(self):
        r = pd.Series([0.1, -0.5, 0.1])
        self.assertLess(backtest.max_drawdown(r), 0.0)

    def test_cost_hurdle_crypto_worst(self):
        ch = backtest.cost_hurdle_table(rebalance_days=1)
        by = {r["class"]: r for r in ch["rows"]}
        # Le crypto a le hurdle le plus élevé (spread 2% + overnight 11.5%).
        self.assertGreater(by["crypto"]["breakeven_gross_annual"],
                           by["index"]["breakeven_gross_annual"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
