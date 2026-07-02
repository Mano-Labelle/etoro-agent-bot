"""Garde-fou DÉTERMINISTE — le cerveau (LLM) ne peut pas le contourner.

Chaque action proposée passe ici. Les règles viennent de config.yaml.
État persistant (snapshot quotidien, halte, verrou) dans state/ en JSON.
"""
import datetime as dt
import json
import os
import time

# Classification heuristique des symboles eToro (déterministe, hors LLM).
FX_MAJORS = {"EURUSD", "USDJPY", "GBPUSD", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"}
GOLD = {"GOLD", "XAUUSD"}
INDEX_MAJORS = {"SPX500", "NSDQ100", "DJ30", "GER40", "GER30", "UK100", "FRA40",
                "JPN225", "AUS200", "EUSTX50", "US2000"}
COMMODITIES = {"OIL", "BRENT", "NATGAS", "SILVER", "XAGUSD", "COPPER", "PLATINUM",
               "PALLADIUM", "WHEAT", "CORN", "SUGAR", "COCOA", "COFFEE"}
CRYPTO = {"BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "LTC", "BCH", "DOT", "LINK",
          "AVAX", "POL", "MATIC", "SHIB", "TRX", "BNB", "UNI", "ATOM", "XLM", "ETC"}

DEFAULT_CAPS = {"fx_major": 30, "index_major": 20, "gold": 20,
                "commodity": 10, "stock": 5, "crypto": 2}


def classify_symbol(symbol):
    """Classe d'actif à partir du symbole. Inconnu → 'stock' (levier max 5)."""
    s = (symbol or "").upper().replace("/", "").replace("-", "").strip()
    if s in FX_MAJORS:
        return "fx_major"
    if s in GOLD:
        return "gold"
    if s in INDEX_MAJORS:
        return "index_major"
    if s in COMMODITIES:
        return "commodity"
    if s in CRYPTO or s.removesuffix("USD") in CRYPTO:
        return "crypto"
    return "stock"


class RiskGate:
    def __init__(self, config, state_dir="state"):
        r = (config or {}).get("risk", {})
        self.max_open_positions = int(r.get("max_open_positions", 3))
        self.max_amount_pct = float(r.get("max_amount_pct_of_book_per_trade", 30))
        self.min_cash_reserve_pct = float(r.get("min_cash_reserve_pct", 10))
        self.global_max_leverage = int(r.get("global_max_leverage", 20))
        self.leverage_caps = {**DEFAULT_CAPS, **(r.get("leverage_caps") or {})}
        self.default_sl = float(r.get("default_stop_loss_pct_position", 40))
        self.max_sl = float(r.get("max_stop_loss_pct_position", 50))
        self.daily_max_dd_pct = float(r.get("daily_max_drawdown_pct", 25))
        self.hard_floor_usd = float(r.get("hard_floor_usd", 3500))
        self.lock_max_age_s = float(r.get("lock_max_age_min", 20)) * 60
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    # ---- État fichier ----
    def _path(self, name):
        return os.path.join(self.state_dir, name)

    def _read_json(self, name):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write_json(self, name, obj):
        with open(self._path(name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    # ---- Verrou anti-chevauchement (cron) ----
    def acquire_lock(self, now=None):
        """False si un verrou plus jeune que lock_max_age existe déjà."""
        now = time.time() if now is None else now
        lock = self._read_json("lock.json")
        if lock and now - float(lock.get("timestamp", 0)) < self.lock_max_age_s:
            return False
        self._write_json("lock.json", {"pid": os.getpid(), "timestamp": now})
        return True

    def release_lock(self):
        try:
            os.remove(self._path("lock.json"))
        except OSError:
            pass

    # ---- Halte permanente (plancher) ----
    def is_halted(self):
        return os.path.exists(self._path("halt.json"))

    def set_halt(self, reason):
        self._write_json("halt.json", {
            "reason": reason,
            "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def halt_info(self):
        return self._read_json("halt.json")

    def check_hard_floor(self, total_value):
        """True si le plancher est franchi → tout fermer + halte permanente."""
        return float(total_value) < self.hard_floor_usd

    # ---- Snapshot quotidien + disjoncteur ----
    def daily_snapshot(self, total_value, now_utc=None):
        """Crée/retourne le snapshot d'équité de 00:00 UTC du jour courant."""
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        today = now_utc.date().isoformat()
        snap = self._read_json("daily_snapshot.json")
        if not snap or snap.get("date") != today:
            snap = {"date": today, "equity": float(total_value)}
            self._write_json("daily_snapshot.json", snap)
        return snap

    def circuit_breaker_active(self, total_value, now_utc=None):
        """True si l'équité a perdu > daily_max_dd_pct depuis le snapshot du jour.

        Bloque toute NOUVELLE ouverture jusqu'au prochain jour UTC (ne ferme rien).
        """
        snap = self.daily_snapshot(total_value, now_utc)
        base = float(snap.get("equity", 0))
        if base <= 0:
            return False
        return float(total_value) < base * (1 - self.daily_max_dd_pct / 100.0)

    # ---- Validation d'une action du cerveau ----
    def evaluate(self, action, total_value, cash, open_positions,
                 entry_rate=None, breaker_active=False):
        """Retourne (action_approuvée | None, raison).

        Pour un 'open', l'action approuvée contient leverage/amount plafonnés
        et stop_loss_rate / take_profit_rate en NIVEAUX DE PRIX.
        """
        a_type = (action.get("type") or "hold").lower()
        if a_type == "hold":
            return None, "hold"

        if a_type == "close":
            pid = action.get("position_id")
            match = next((p for p in open_positions if p.get("positionID") == pid), None)
            if pid is None or match is None:
                return None, f"close rejeté: position_id inconnu ({pid})"
            return ({"type": "close", "position_id": pid,
                     "instrument_id": match.get("instrumentID"),
                     "rationale": action.get("rationale", "")},
                    "close approuvé")

        if a_type != "open":
            return None, f"type d'action inconnu: {a_type}"

        # -- Ouverture --
        if breaker_active:
            return None, "disjoncteur quotidien actif: ouvertures bloquées jusqu'au prochain jour UTC"
        if len(open_positions) >= self.max_open_positions:
            return None, f"max positions ouvertes atteint ({self.max_open_positions})"

        symbol = action.get("symbol") or ""
        asset_class = classify_symbol(symbol)
        cap = min(int(self.leverage_caps.get(asset_class, 1)), self.global_max_leverage)
        try:
            leverage = int(action.get("leverage") or 1)
        except (TypeError, ValueError):
            leverage = 1
        leverage = max(1, min(leverage, cap))

        try:
            amount = float(action.get("amount_usd") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        amount = min(amount, total_value * self.max_amount_pct / 100.0)
        # Réserve de cash: ne jamais descendre sous min_cash_reserve_pct du book.
        available = cash - total_value * self.min_cash_reserve_pct / 100.0
        amount = min(amount, available)
        if amount < 1.0:
            return None, "montant nul après plafonds (cash insuffisant ou réserve atteinte)"

        # Stop-loss OBLIGATOIRE: injecté si absent, plafonné, converti en prix.
        sl_pct = action.get("stop_loss_pct_position")
        try:
            sl_pct = float(sl_pct) if sl_pct else 0.0
        except (TypeError, ValueError):
            sl_pct = 0.0
        if sl_pct <= 0:
            sl_pct = self.default_sl
        sl_pct = min(sl_pct, self.max_sl)

        if entry_rate is None or entry_rate <= 0:
            return None, "pas de prix courant disponible → pas de SL possible → trade rejeté"

        is_buy = bool(action.get("is_buy", True))
        # Une perte de P% sur la POSITION = un mouvement de prix de P/levier.
        move = sl_pct / 100.0 / leverage
        sl_rate = entry_rate * (1 - move) if is_buy else entry_rate * (1 + move)

        tp_rate = None
        tp_pct = action.get("take_profit_pct_position")
        try:
            tp_pct = float(tp_pct) if tp_pct else 0.0
        except (TypeError, ValueError):
            tp_pct = 0.0
        if tp_pct > 0:
            tmove = tp_pct / 100.0 / leverage
            tp_rate = entry_rate * (1 + tmove) if is_buy else entry_rate * (1 - tmove)

        return ({"type": "open", "symbol": symbol, "asset_class": asset_class,
                 "is_buy": is_buy, "leverage": leverage,
                 "amount_usd": round(amount, 2),
                 "stop_loss_pct_position": sl_pct,
                 "stop_loss_rate": round(sl_rate, 6),
                 "take_profit_rate": round(tp_rate, 6) if tp_rate else None,
                 "instrument_query": action.get("instrument_query") or symbol,
                 "rationale": action.get("rationale", "")},
                "open approuvé")
