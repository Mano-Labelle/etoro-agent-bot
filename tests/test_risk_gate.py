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
    # Défaut du nouveau régime ACTIFS RÉELS: levier forcé à 1, long-only.
    "force_unleveraged": True,
    "long_only": True,
}}

# Config "machinerie": drapeaux du nouveau régime DÉSACTIVÉS, pour prouver que la
# mécanique de plafonnement du levier et de conversion SL/TP reste INTACTE (elle
# n'est plus qu'inerte par défaut, pas retirée).
MACHINERY_CONFIG = {"risk": {**CONFIG["risk"],
                             "force_unleveraged": False, "long_only": False}}


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
                 entry=100.0, breaker=False, instrument="__auto__"):
        # Comme main.py: un 'open' est jugé sur l'instrument RÉSOLU, jamais sur
        # le symbole déclaré seul. On fabrique un instrument minimal qui matche
        # le symbole de l'action (classification par repli sur les listes).
        if instrument == "__auto__":
            sym = str(action.get("symbol") or "").strip()
            instrument = {"symbol": sym} if sym else None
        return self.gate.evaluate(action, total, cash, list(positions),
                                  entry_rate=entry, breaker_active=breaker,
                                  instrument=instrument)


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
    # La MÉCANIQUE de plafonnement du levier reste intacte : on la teste avec le
    # forçage désactivé (MACHINERY_CONFIG). Par défaut, le levier est forcé à 1
    # (voir TestForceUnleveraged).
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gate = RiskGate(MACHINERY_CONFIG, state_dir=self.tmp)

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
    # La conversion SL/TP % position → niveau de prix dépend du levier : on la teste
    # avec MACHINERY_CONFIG (levier passant + shorts permis). À levier 1 (défaut),
    # la conversion reste correcte — voir TestForceUnleveraged.test_sl_math_at_leverage_1.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gate = RiskGate(MACHINERY_CONFIG, state_dir=self.tmp)

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


class TestUntrustedLLMOutput(GateTest):
    """Le gate doit DIGÉRER du JSON arbitraire du cerveau sans jamais crasher."""

    def test_is_buy_string_false_rejected(self):
        # "false" (str) est truthy en Python: sans garde strict, un SHORT
        # deviendrait un LONG à levier. Doit être rejeté.
        approved, reason = self.evaluate(open_action(is_buy="false"))
        self.assertIsNone(approved)
        self.assertIn("booléen", reason)

    def test_non_string_type_no_crash(self):
        approved, reason = self.evaluate({"type": 5, "symbol": "TSLA"})
        self.assertIsNone(approved)  # coerce en "5" → type inconnu, pas de crash

    def test_numeric_symbol_no_crash(self):
        approved, _ = self.evaluate(open_action(symbol=123, instrument_query=123),
                                    instrument={"symbol": "123"})
        # 123 → "123" (coercition), classé stock, ne lève pas.
        self.assertTrue(approved is None or approved["leverage"] <= 5)

    def test_nan_amount_rejected(self):
        approved, reason = self.evaluate(open_action(amount_usd=float("nan")))
        self.assertIsNone(approved)
        self.assertIn("non fini", reason)

    def test_inf_amount_rejected(self):
        approved, _ = self.evaluate(open_action(amount_usd=float("inf")))
        # inf → plafonné par min(inf, 30% book)=3000, donc APPROUVÉ à 3000:
        # le cap absorbe l'infini. On vérifie juste l'absence de crash + borne.
        self.assertTrue(approved is None or approved["amount_usd"] <= 3000.0)

    def test_string_amount_rejected(self):
        approved, reason = self.evaluate(open_action(amount_usd="abc"))
        self.assertIsNone(approved)

    def test_nan_stop_loss_falls_back_to_default(self):
        approved, _ = self.evaluate(open_action(stop_loss_pct_position=float("nan")))
        self.assertIsNotNone(approved)
        self.assertEqual(approved["stop_loss_pct_position"], 40.0)  # défaut réinjecté


class TestInstrumentIdentity(GateTest):
    """Un 'open' n'est jamais exécuté sur un instrument non vérifié."""

    def test_open_rejected_without_instrument(self):
        approved, reason = self.evaluate(open_action(), instrument=None)
        self.assertIsNone(approved)
        self.assertIn("instrument", reason)

    def test_mismatch_rejected(self):
        # Le cerveau dit EURUSD mais la recherche a résolu Tesla → rejet.
        approved, reason = self.evaluate(
            open_action(symbol="EURUSD", instrument_query="Tesla"),
            instrument={"symbol": "TSLA"})
        self.assertIsNone(approved)
        self.assertIn("mismatch", reason)

    def test_class_from_resolved_metadata(self):
        # Métadonnées: type crypto → cap 2, même si le symbole ressemble à autre chose.
        # Testé avec le forçage désactivé pour vérifier la classification (cap 2).
        gate = RiskGate(MACHINERY_CONFIG, state_dir=self.tmp)
        approved, _ = gate.evaluate(
            open_action(symbol="BTC", leverage=10), 10000.0, 9000.0, [],
            entry_rate=100.0, instrument={"symbol": "BTC", "instrumentTypeId": 10})
        self.assertEqual(approved["leverage"], 2)


class TestForceUnleveraged(GateTest):
    """Régime ACTIFS RÉELS: le levier est FORCÉ à 1 quoi que demande le cerveau."""

    def test_leverage_20_forced_to_1(self):
        approved, _ = self.evaluate(open_action(symbol="TSLA", leverage=20))
        self.assertIsNotNone(approved)
        self.assertEqual(approved["leverage"], 1)  # écrasé, pas seulement plafonné

    def test_crypto_leverage_forced_to_1(self):
        # Même la crypto (cap 2) sort à 1 en mode force_unleveraged.
        approved, _ = self.evaluate(open_action(symbol="BTC", leverage=10))
        self.assertEqual(approved["leverage"], 1)

    def test_sl_math_at_leverage_1(self):
        # BUY, entrée 100, SL 40% position, levier forcé 1 → mouvement 40/100/1 = 40%.
        approved, _ = self.evaluate(
            open_action(is_buy=True, leverage=20, stop_loss_pct_position=40),
            entry=100.0)
        self.assertEqual(approved["leverage"], 1)
        self.assertAlmostEqual(approved["stop_loss_rate"], 60.0, places=6)


class TestLongOnly(GateTest):
    """Régime ACTIFS RÉELS: shorts interdits (CFD), fermetures toujours permises."""

    def test_short_open_rejected(self):
        approved, reason = self.evaluate(open_action(symbol="BTC", is_buy=False))
        self.assertIsNone(approved)
        self.assertIn("long-only", reason)
        self.assertIn("short", reason.lower())

    def test_long_open_still_allowed(self):
        approved, _ = self.evaluate(open_action(symbol="BTC", is_buy=True))
        self.assertIsNotNone(approved)
        self.assertTrue(approved["is_buy"])

    def test_close_allowed_in_long_only(self):
        # Un 'close' n'est JAMAIS bloqué par long_only (traité par _evaluate_close).
        positions = [{"positionID": 7, "instrumentID": 3}]
        approved, _ = self.evaluate({"type": "close", "position_id": 7},
                                    positions=positions)
        self.assertIsNotNone(approved)
        self.assertEqual(approved["type"], "close")

    def test_is_buy_string_false_still_booleen_reject(self):
        # "false" (str) est rejeté par le garde strict AVANT long_only ("booléen"),
        # pas par la règle long-only — l'ordre des vérifications est préservé.
        approved, reason = self.evaluate(open_action(is_buy="false"))
        self.assertIsNone(approved)
        self.assertIn("booléen", reason)


class TestChurn(GateTest):
    def test_max_opens_per_day(self):
        now = dt.datetime(2026, 7, 3, 12, 0, tzinfo=dt.timezone.utc)
        for i in range(6):
            self.gate.record_open(f"SYM{i}", instrument_id=i, now_utc=now)
        approved, reason = self.gate.evaluate(
            open_action(symbol="TSLA"), 10000.0, 9000.0, [],
            entry_rate=100.0, instrument={"symbol": "TSLA"}, now_utc=now)
        self.assertIsNone(approved)
        self.assertIn("churn", reason)

    def test_min_hold_blocks_reopen_same_symbol(self):
        now = dt.datetime(2026, 7, 3, 12, 0, tzinfo=dt.timezone.utc)
        self.gate.record_open("TSLA", instrument_id=1, now_utc=now)
        soon = now + dt.timedelta(minutes=30)  # < 120 min min-hold
        approved, reason = self.gate.evaluate(
            open_action(symbol="TSLA"), 10000.0, 9000.0, [],
            entry_rate=100.0, instrument={"symbol": "TSLA"}, now_utc=soon)
        self.assertIsNone(approved)
        self.assertIn("churn", reason)

    def test_opens_budget_resets_next_utc_day(self):
        d1 = dt.datetime(2026, 7, 3, 23, 0, tzinfo=dt.timezone.utc)
        for i in range(6):
            self.gate.record_open(f"S{i}", instrument_id=i, now_utc=d1)
        d2 = dt.datetime(2026, 7, 4, 1, 0, tzinfo=dt.timezone.utc)
        approved, _ = self.gate.evaluate(
            open_action(symbol="AAPL"), 10000.0, 9000.0, [],
            entry_rate=100.0, instrument={"symbol": "AAPL"}, now_utc=d2)
        self.assertIsNotNone(approved)  # budget d'ouvertures reparti à zéro


class TestCloseCoercion(GateTest):
    def test_string_position_id_still_closes(self):
        # position_id arrive en str via le round-trip JSON du LLM → doit matcher.
        positions = [{"positionID": 42, "instrumentID": 7}]
        approved, _ = self.evaluate({"type": "close", "position_id": "42"},
                                    positions=positions)
        self.assertIsNotNone(approved)
        self.assertEqual(approved["position_id"], 42)


class TestEquityValidity(GateTest):
    def test_non_finite_equity_rejected(self):
        approved, reason = self.gate.evaluate(
            open_action(), float("nan"), 9000.0, [],
            entry_rate=100.0, instrument={"symbol": "TSLA"})
        self.assertIsNone(approved)

    def test_zero_equity_rejected(self):
        approved, _ = self.gate.evaluate(
            open_action(), 0.0, 0.0, [],
            entry_rate=100.0, instrument={"symbol": "TSLA"})
        self.assertIsNone(approved)


class TestBrainParsing(unittest.TestCase):
    """extract_first_json: robuste aux fences/tronqué/vide, refuse NaN/Infinity."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def test_plain_json(self):
        from brain import extract_first_json
        self.assertEqual(extract_first_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        from brain import extract_first_json
        txt = 'Voici ma décision:\n```json\n{"actions": [], "market_note": "x"}\n```'
        self.assertEqual(extract_first_json(txt), {"actions": [], "market_note": "x"})

    def test_empty_and_garbage(self):
        from brain import extract_first_json
        self.assertIsNone(extract_first_json(""))
        self.assertIsNone(extract_first_json("aucun json ici"))

    def test_truncated_json_returns_none(self):
        from brain import extract_first_json
        self.assertIsNone(extract_first_json('{"actions": [{"type": "op'))

    def test_nan_rejected(self):
        # json.loads accepte NaN par défaut — extract_first_json doit le REFUSER.
        from brain import extract_first_json
        self.assertIsNone(extract_first_json('{"amount_usd": NaN}'))
        self.assertIsNone(extract_first_json('{"x": Infinity}'))


class TestTrackerSmoke(unittest.TestCase):
    """tracker: écrit les JSONL + PERFORMANCE.md sans réseau, métriques saines."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_writes_files(self):
        import tracker
        eq = tracker.equity_record(equity=10500.0, cash=8000.0, n_positions=1,
                                    dry_run=True)
        tr = [tracker.trade_record(cycle_id="c1", a_type="open", symbol="TSLA",
                                   is_buy=True, leverage=5, amount_usd=1000.0,
                                   status="dry_run", rationale="momentum")]
        tracker.update(self.tmp, equity_record=eq, trade_records=tr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "data", "equity.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "PERFORMANCE.md")))
        with open(os.path.join(self.tmp, "PERFORMANCE.md"), encoding="utf-8") as f:
            self.assertIn("Expérience eToro", f.read())

    def test_metrics_hit_rate(self):
        import tracker
        trades = [
            tracker.trade_record("c", "close", status="dry_run", pnl_usd=100.0),
            tracker.trade_record("c", "close", status="dry_run", pnl_usd=-50.0),
        ]
        m = tracker.compute_metrics([{"equity": 10050.0}], trades)
        self.assertEqual(m["n_closes_pnl"], 2)
        self.assertAlmostEqual(m["hit_rate_pct"], 50.0)
        self.assertAlmostEqual(m["expectancy_usd"], 25.0)  # (100-50)/2


if __name__ == "__main__":
    unittest.main(verbosity=2)
