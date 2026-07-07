"""Tests de tracker.write_positions (aucun réseau).

Vérifie: JSON valide, noms lisibles + rationale résolus, PnL %/jours détenus,
tolérance des positions vides et des champs manquants.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker  # noqa: E402


class TestWritePositions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.now = dt.datetime(2026, 7, 7, 12, 0, tzinfo=dt.timezone.utc)

    def _read(self):
        with open(os.path.join(self.tmp, "data", "positions.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_full_position_shapes_row(self):
        positions = [{
            "positionID": 1, "instrumentID": 100, "symbol": "BTC",
            "amount": 500.0, "openRate": 60000.0, "netProfit": 50.0,
            "openDateTime": "2026-07-05T12:00:00Z",
        }]
        out = tracker.write_positions(
            self.tmp, positions,
            name_map={"BTC": "Bitcoin"},
            rationale_map={"BTC": "momentum haussier + halving"},
            now=self.now)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["symbol"], "BTC")
        self.assertEqual(row["name"], "Bitcoin")
        self.assertEqual(row["amount_usd"], 500.0)
        self.assertEqual(row["entry_rate"], 60000.0)
        self.assertEqual(row["pnl_usd"], 50.0)
        self.assertAlmostEqual(row["pnl_pct"], 10.0)  # 50/500
        self.assertEqual(row["days_held"], 2.0)        # 05→07 juillet
        self.assertEqual(row["rationale"], "momentum haussier + halving")
        # Fichier bien écrit et relisible.
        self.assertEqual(self._read(), out)

    def test_empty_positions_writes_empty_list(self):
        out = tracker.write_positions(self.tmp, [], now=self.now)
        self.assertEqual(out, [])
        self.assertEqual(self._read(), [])

    def test_missing_fields_tolerated(self):
        # Position quasi vide + une entrée non-dict → jamais de crash.
        positions = [{"symbol": "ETH"}, "je-ne-suis-pas-un-dict", {}]
        out = tracker.write_positions(
            self.tmp, positions, name_map={"ETH": "Ethereum"}, now=self.now)
        self.assertEqual(len(out), 2)  # la str est ignorée, les 2 dicts restent
        eth = out[0]
        self.assertEqual(eth["symbol"], "ETH")
        self.assertEqual(eth["name"], "Ethereum")
        self.assertIsNone(eth["amount_usd"])
        self.assertIsNone(eth["pnl_pct"])   # pas de montant → pas de %
        self.assertIsNone(eth["days_held"])
        self.assertIsNone(eth["rationale"])

    def test_name_falls_back_to_symbol(self):
        out = tracker.write_positions(
            self.tmp, [{"symbol": "AMD", "instrumentID": 9}], now=self.now)
        self.assertEqual(out[0]["name"], "AMD")  # pas de name_map → symbole brut

    def test_name_resolved_by_instrument_id(self):
        # Aucun symbole, mais l'instrumentID est mappé.
        out = tracker.write_positions(
            self.tmp, [{"instrumentID": 42}],
            name_map={"42": "Solana"}, now=self.now)
        self.assertEqual(out[0]["name"], "Solana")

    def test_negative_pnl_percent(self):
        out = tracker.write_positions(
            self.tmp, [{"symbol": "TSLA", "amount": 200.0, "netProfit": -40.0}],
            now=self.now)
        self.assertAlmostEqual(out[0]["pnl_pct"], -20.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
