"""Tests du calcul de stats de marché (aucun réseau — fetcher injecté)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from marketdata import compute_stats, market_snapshot  # noqa: E402


class TestComputeStats(unittest.TestCase):
    def test_uptrend_positive_momentum(self):
        closes = [100 * (1.01 ** i) for i in range(70)]  # +1 %/jour, monotone
        s = compute_stats(closes)
        self.assertIsNotNone(s)
        self.assertGreater(s["mom_1w_%"], 0)
        self.assertGreater(s["mom_1m_%"], 0)
        self.assertGreater(s["mom_3m_%"], 0)
        self.assertGreater(s["vs_ma20_%"], 0)
        # série monotone croissante → dernier = plus-haut → drawdown nul
        self.assertAlmostEqual(s["drawdown_from_high_%"], 0.0, places=6)

    def test_downtrend_negative(self):
        closes = [100 * (0.99 ** i) for i in range(70)]
        s = compute_stats(closes)
        self.assertLess(s["mom_1m_%"], 0)
        self.assertLess(s["vs_ma20_%"], 0)
        self.assertLess(s["drawdown_from_high_%"], 0)  # sous le plus-haut

    def test_flat_series_zero_vol(self):
        s = compute_stats([100.0] * 70)
        self.assertEqual(s["vol_daily_%"], 0.0)
        self.assertEqual(s["mom_1m_%"], 0.0)

    def test_too_short_returns_none(self):
        self.assertIsNone(compute_stats([100, 101, 102]))

    def test_higher_vol_detected(self):
        calm = compute_stats([100 + (i % 2) * 0.1 for i in range(70)])
        wild = compute_stats([100 + (i % 2) * 10 for i in range(70)])
        self.assertLess(calm["vol_daily_%"], wild["vol_daily_%"])


class TestSnapshot(unittest.TestCase):
    def test_snapshot_skips_failures_and_shapes_rows(self):
        # fetcher factice: une série exploitable pour BTC, vide pour le reste.
        def fake_fetch(sym):
            return [100 * (1.005 ** i) for i in range(70)] if sym == "BTC-USD" else []
        with tempfile.TemporaryDirectory() as tmp:
            rows = market_snapshot(
                watchlist=[("BTC", "BTC-USD", "crypto", "Bitcoin"),
                           ("AAPL", "AAPL", "stock", "Apple")],
                state_dir=tmp, fetcher=fake_fetch)
            self.assertEqual(len(rows), 1)  # AAPL (vide) est ignoré
            self.assertEqual(rows[0]["symbol"], "BTC")
            self.assertIn("mom_1m_%", rows[0])
            self.assertGreater(rows[0]["mom_1m_%"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
