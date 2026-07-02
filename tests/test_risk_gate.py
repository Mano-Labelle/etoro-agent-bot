"""Tests unitaires purs du garde-fou (aucun réseau, aucun mock d'API).

Lancer: python tests/test_risk_gate.py  ou  python -m pytest tests/
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_gate import RiskGate, classify_symbol  # noqa: E402

CONFIG = {"risk": {
    "max_open_positions": 3,
    "max_amount_pct_of_book_per_trade": 30,
    "min_cash_reserve_pct": 10,
    "global_max_leverage": 20,
    "leverage_caps": {"fx_major": 30, "index_major": 20, "gold": 20,
                      "commodity": 10, "stock": 5, "crypto": 2},
    "default_stop_loss_pct_position": 40,
    "max_stop_loss_pct_position": 50,
    "daily_max_drawdown_pct": 25,
    "hard_floor_usd": 3500,
    "lock_max_age_min": 20,
}}


def open_action(**kw):
    base = {"type": "open", "symbol": "TSLA", "instrument_query": "TSLA",
            "is_buy": True, "leverage": 5, "amount_usd": 1000.0,
            "stop_loss_pct_position": 40, "take_profit_pct_position": None,
            "position_id": None, "rationale": "test"}
    base.update(kw)
    return base


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gate = RiskGate(CONFIG, state_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def evaluate(self, action, total=10000.0, cash=9000.0, positions=(),
                 entry=100.0, breaker=False):
        return self.gate.evaluate(action, total, cash, list(positions),
                                  entry_rate=entry, breaker_active=breaker)


class TestClassification(GateTest):
    def test_classes(self):
        self.assertEqual(classify_symbol("EURUSD"), "fx_major")
        self.assertEqual(classify_symbol("GOLD"), "gold")
        self.assertEqual(classify_symbol("SPX500"), "index_major")
        self.assertEqual(classify_symbol("OIL"), "commodity")
        self.assertEqual(classify_symbol("BTC"), "crypto")
        self.assertEqual(classify_symbol("ETHUSD"), "crypto")
        self.assertEqual(classify_symbol("TSLA"), "stock")
        self.assertEqual(classify_symbol("SYMBOLE_INCONNU"), "stock")  # prudent


class TestLeverageCapping(GateTest):
    def test_stock_capped_at_5(self):
        approved, _ = self.evaluate(open_action(symbol="TSLA", leverage=20))
        self.assertIsNotNone(approved)
        self.assertEqual(approved["leverage"], 5)

    def test_crypto_capped_at_2(self):
        approved, _ = self.evaluate(open_action(symbol="BTC", leverage=10))
        self.assertEqual(approved["leverage"], 2)

    def test_fx_capped_by_global_max(self):
        # fx_major autorise 30 mais le plafond global est 20.
        approved, _ = self.evaluate(open_action(symbol="EURUSD", leverage=30))
        self.assertEqual(approved["leverage"], 20)

    def test_leverage_min_1(self):
        approved, _ = self.evaluate(open_action(leverage=0))
        self.assertEqual(approved["leverage"], 1)


class TestAmountCapping(GateTest):
    def test_capped_at_pct_of_book(self):
        approved, _ = self.evaluate(open_action(amount_usd=5000), total=10000, cash=9000)
        self.assertEqual(approved["amount_usd"], 3000.0)  # 30% de 10 000

    def test_cash_reserve_respected(self):
        # Réserve = 10% de 10 000 = 1000 → disponible = 1200 - 1000 = 200.
        approved, _ = self.evaluate(open_action(amount_usd=3000), total=10000, cash=1200)
        self.assertEqual(approved["amount_usd"], 200.0)

    def test_rejected_when_no_cash(self):
        approved, reason = self.evaluate(open_action(amount_usd=500), total=10000, cash=900)
        self.assertIsNone(approved)
        self.assertIn("montant", reason)


class TestStopLoss(GateTest):
    def test_sl_injected_when_missing(self):
        approved, _ = self.evaluate(open_action(stop_loss_pct_position=None))
        self.assertEqual(approved["stop_loss_pct_position"], 40.0)

    def test_sl_capped_at_50(self):
        approved, _ = self.evaluate(open_action(stop_loss_pct_position=80))
        self.assertEqual(approved["stop_loss_pct_position"], 50.0)

    def test_buy_price_level_math(self):
        # BUY, entrée 100, levier 5, SL 40% position → mouvement 40/100/5 = 8%.
        approved, _ = self.evaluate(
            open_action(is_buy=True, leverage=5, stop_loss_pct_position=40), entry=100.0)
        self.assertAlmostEqual(approved["stop_loss_rate"], 92.0, places=6)

    def test_sell_price_level_math(self):
        # SELL, entrée 100, levier 4, SL 40% → mouvement 10% vers le HAUT.
        approved, _ = self.evaluate(
            open_action(symbol="EURUSD", is_buy=False, leverage=4,
                        stop_loss_pct_position=40), entry=100.0)
        self.assertAlmostEqual(approved["stop_loss_rate"], 110.0, places=6)

    def test_take_profit_math(self):
        approved, _ = self.evaluate(
            open_action(is_buy=True, leverage=5, take_profit_pct_position=100),
            entry=100.0)
        self.assertAlmostEqual(approved["take_profit_rate"], 120.0, places=6)

    def test_rejected_without_entry_rate(self):
        # Pas de prix courant → pas de SL possible → pas de trade.
        approved, reason = self.evaluate(open_action(), entry=None)
        self.assertIsNone(approved)
        self.assertIn("SL", reason)


class TestPositionLimits(GateTest):
    def test_max_open_positions(self):
        positions = [{"positionID": i, "instrumentID": i} for i in range(3)]
        approved, reason = self.evaluate(open_action(), positions=positions)
        self.assertIsNone(approved)
        self.assertIn("max positions", reason)

    def test_close_known_position(self):
        positions = [{"positionID": 42, "instrumentID": 7}]
        approved, _ = self.evaluate({"type": "close", "position_id": 42},
                                    positions=positions)
        self.assertEqual(approved["type"], "close")
        self.assertEqual(approved["instrument_id"], 7)

    def test_close_unknown_position_rejected(self):
        approved, reason = self.evaluate({"type": "close", "position_id": 99})
        self.assertIsNone(approved)
        self.assertIn("inconnu", reason)

    def test_hold_never_approved(self):
        approved, reason = self.evaluate({"type": "hold"})
        self.assertIsNone(approved)
        self.assertEqual(reason, "hold")


class TestFloorAndBreaker(GateTest):
    def test_hard_floor(self):
        self.assertTrue(self.gate.check_hard_floor(3499.99))
        self.assertFalse(self.gate.check_hard_floor(3500.0))
        self.assertFalse(self.gate.check_hard_floor(10000.0))

    def test_halt_flag(self):
        self.assertFalse(self.gate.is_halted())
        self.gate.set_halt("test plancher")
        self.assertTrue(self.gate.is_halted())
        self.assertEqual(self.gate.halt_info()["reason"], "test plancher")

    def test_daily_breaker(self):
        day1 = dt.datetime(2026, 7, 2, 0, 5, tzinfo=dt.timezone.utc)
        self.gate.daily_snapshot(10000.0, now_utc=day1)  # snapshot 00:00 UTC
        # -26% le même jour → disjoncteur actif.
        self.assertTrue(self.gate.circuit_breaker_active(7400.0, now_utc=day1))
        # -24% → pas actif.
        self.assertFalse(self.gate.circuit_breaker_active(7600.0, now_utc=day1))

    def test_breaker_resets_next_utc_day(self):
        day1 = dt.datetime(2026, 7, 2, 12, 0, tzinfo=dt.timezone.utc)
        day2 = dt.datetime(2026, 7, 3, 0, 5, tzinfo=dt.timezone.utc)
        self.gate.daily_snapshot(10000.0, now_utc=day1)
        self.assertTrue(self.gate.circuit_breaker_active(7000.0, now_utc=day1))
        # Nouveau jour UTC → nouveau snapshot à 7000 → plus de blocage.
        self.assertFalse(self.gate.circuit_breaker_active(7000.0, now_utc=day2))

    def test_breaker_blocks_opens_only(self):
        approved, reason = self.evaluate(open_action(), breaker=True)
        self.assertIsNone(approved)
        self.assertIn("disjoncteur", reason)
        # Les fermetures restent autorisées.
        approved, _ = self.evaluate({"type": "close", "position_id": 1},
                                    positions=[{"positionID": 1, "instrumentID": 2}],
                                    breaker=True)
        self.assertIsNotNone(approved)


class TestLock(GateTest):
    def test_lock_lifecycle(self):
        self.assertTrue(self.gate.acquire_lock(now=1000.0))
        # Verrou frais (< 20 min) → refusé.
        self.assertFalse(self.gate.acquire_lock(now=1000.0 + 60))
        # Verrou périmé (> 20 min) → repris.
        self.assertTrue(self.gate.acquire_lock(now=1000.0 + 21 * 60))

    def test_release_allows_reacquire(self):
        self.assertTrue(self.gate.acquire_lock(now=1000.0))
        self.gate.release_lock()
        self.assertTrue(self.gate.acquire_lock(now=1000.0 + 1))

    def test_release_without_lock_is_safe(self):
        self.gate.release_lock()  # ne doit pas lever


if __name__ == "__main__":
    unittest.main(verbosity=2)
