"""Garde-fou DÉTERMINISTE — le cerveau (LLM) ne peut pas le contourner.

Chaque action proposée passe ici. Les règles viennent de config.yaml.
État persistant (snapshot quotidien, halte, churn, verrou) dans state/ en JSON.

Le gate est conçu pour digérer du JSON ARBITRAIRE produit par un LLM :
coercitions str()/int()/float() systématiques, rejet des non-finis (NaN/inf),
is_buy strictement booléen, identité d'instrument vérifiée contre le résultat
de recherche RÉSOLU (jamais confiance au symbole déclaré seul).
"""
import datetime as dt
import json
import math
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

# Champs candidats pour l'identité et le type d'un instrument RÉSOLU
# (résultat de /market-data/search). L'identité prime sur le symbole du LLM.
_SYMBOL_FIELDS = ("internalsymbolfull", "symbolfull", "internalsymbol", "symbol", "ticker")
_TYPE_FIELDS = ("instrumenttypeid", "instrumenttype", "assetclassid", "assetclass",
                "typeid", "type")
# Mapping type eToro → classe d'actif. Une classe inconnue → cap levier 1 (prudent).
_TYPE_CLASS = {
    "1": "fx", "currencies": "fx", "currency": "fx", "forex": "fx", "fx": "fx",
    "2": "commodity", "commodities": "commodity", "commodity": "commodity",
    "4": "index_major", "indices": "index_major", "index": "index_major",
    "5": "stock", "stocks": "stock", "equities": "stock", "stock": "stock",
    "6": "stock", "etf": "stock",
    "10": "crypto", "cryptocurrencies": "crypto", "crypto": "crypto",
}


def _clean_symbol(symbol):
    """Normalise un symbole pour comparaison (coercition str incluse)."""
    return (str(symbol if symbol is not None else "")
            .upper().replace("/", "").replace("-", "").replace(" ", "").strip())


def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _lower_keys(d):
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def classify_symbol(symbol):
    """Classe d'actif à partir du symbole. Inconnu → 'stock' (levier max 5)."""
    s = _clean_symbol(symbol)
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


def instrument_symbols(instrument):
    """Tous les symboles candidats d'un instrument résolu (str, nettoyés)."""
    low = _lower_keys(instrument)
    out = []
    for f in _SYMBOL_FIELDS:
        v = low.get(f)
        if isinstance(v, str) and v.strip():
            out.append(_clean_symbol(v))
    return out


def classify_instrument(instrument, symbol=None):
    """Classe d'actif depuis les MÉTADONNÉES de l'instrument résolu.

    Repli sur les listes de symboles si aucun champ de type reconnu.
    FX non-majeure → 'fx_minor' (absente des caps → levier 1, prudent).
    """
    sym = _clean_symbol(symbol)
    if isinstance(instrument, dict):
        low = _lower_keys(instrument)
        for f in _TYPE_FIELDS:
            v = low.get(f)
            if v is None or not str(v).strip():
                continue
            cls = _TYPE_CLASS.get(str(v).strip().lower())
            if cls == "fx":
                return "fx_major" if sym in FX_MAJORS else "fx_minor"
            if cls == "commodity":
                return "gold" if sym in GOLD else "commodity"
            if cls:
                return cls
    return classify_symbol(sym)


class RiskGate:
    def __init__(self, config, state_dir="state"):
        r = (config or {}).get("risk", {})
        self.max_open_positions = int(r.get("max_open_positions", 3))
        self.max_amount_pct = float(r.get("max_amount_pct_of_book_per_trade", 30))
        self.min_cash_reserve_pct = float(r.get("min_cash_reserve_pct", 10))
        self.global_max_leverage = int(r.get("global_max_leverage", 20))
        self.leverage_caps = {**DEFAULT_CAPS, **(r.get("leverage_caps") or {})}
        # Mode ACTIFS RÉELS SANS CFD (défaut). Posséder la crypto/l'action = pas de
        # levier possible, pas de portage overnight, impossible de shorter. Ces deux
        # drapeaux rendent les caps de levier ci-dessus INERTES (levier sortant = 1).
        self.force_unleveraged = bool(r.get("force_unleveraged", True))
        self.long_only = bool(r.get("long_only", True))
        self.default_sl = float(r.get("default_stop_loss_pct_position", 40))
        self.max_sl = float(r.get("max_stop_loss_pct_position", 50))
        self.daily_max_dd_pct = float(r.get("daily_max_drawdown_pct", 25))
        self.hard_floor_usd = float(r.get("hard_floor_usd", 3500))
        self.lock_max_age_s = float(r.get("lock_max_age_min", 20)) * 60
        # Anti-churn: budget d'ouvertures + durée de détention minimale.
        self.max_opens_per_day = int(r.get("max_opens_per_day", 6))
        self.min_hold_minutes = float(r.get("min_hold_minutes", 120))
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

    # ---- Verrou anti-chevauchement ----
    # NOTE: ce verrou ne protège que des runs LOCAUX concurrents sur la même
    # machine. En CI, c'est le groupe `concurrency` du workflow qui sérialise
    # les runs (les runners éphémères ne partagent pas ce fichier).
    def acquire_lock(self, now=None):
        """False si un verrou plus jeune que lock_max_age existe déjà.

        Création ATOMIQUE (O_CREAT|O_EXCL) — pas de fenêtre lecture/écriture.
        """
        now = time.time() if now is None else now
        path = self._path("lock.json")
        payload = json.dumps({"timestamp": now}).encode("utf-8")
        for _ in range(2):  # 2e passe si on vient de purger un verrou périmé
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                lock = self._read_json("lock.json")
                ts = float(lock.get("timestamp", 0)) if isinstance(lock, dict) else 0.0
                if now - ts < self.lock_max_age_s:
                    return False
                try:  # verrou périmé (> TTL) → purge puis nouvelle tentative atomique
                    os.remove(path)
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            return True
        return False

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
        """True si le plancher est franchi. La liquidation exige en plus une
        CONFIRMATION sur 2 cycles consécutifs (voir floor_breach_pending)."""
        try:
            v = float(total_value)
        except (TypeError, ValueError):
            return False  # équité illisible ≠ plancher franchi (main la rejette avant)
        if not math.isfinite(v):
            return False
        return v < self.hard_floor_usd

    # ---- Confirmation du plancher sur 2 cycles consécutifs ----
    # Une lecture d'équité transitoirement fausse ne doit jamais suffire à
    # liquider: le 1er cycle sous le plancher ARME, le 2e consécutif DÉCLENCHE.
    def floor_breach_pending(self):
        return self._read_json("floor_breach_pending.json") is not None

    def set_floor_breach_pending(self, total_value):
        self._write_json("floor_breach_pending.json", {
            "total_value": float(total_value),
            "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def clear_floor_breach_pending(self):
        try:
            os.remove(self._path("floor_breach_pending.json"))
        except OSError:
            pass

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
        if base <= 0 or not math.isfinite(base):
            return False
        return float(total_value) < base * (1 - self.daily_max_dd_pct / 100.0)

    # ---- Anti-churn (state/churn.json) ----
    @staticmethod
    def _parse_ts(s):
        try:
            t = dt.datetime.fromisoformat(str(s))
        except (TypeError, ValueError):
            return None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

    def _load_churn(self, now_utc=None):
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        today = now.date().isoformat()
        churn = self._read_json("churn.json")
        if not isinstance(churn, dict):
            churn = {}
        if churn.get("date") != today:
            # Nouveau jour UTC: le budget d'ouvertures repart à zéro, mais les
            # horodatages (min-hold) survivent — une position ouverte à 23h50
            # reste protégée à 00h10.
            churn = {"date": today, "opens_today": 0,
                     "last_open_by_symbol": churn.get("last_open_by_symbol") or {},
                     "opened_instruments": churn.get("opened_instruments") or {}}
        churn.setdefault("opens_today", 0)
        churn.setdefault("last_open_by_symbol", {})
        churn.setdefault("opened_instruments", {})
        cutoff = now - dt.timedelta(hours=48)  # élagage: inutile au-delà de 48 h
        for key in ("last_open_by_symbol", "opened_instruments"):
            kept = {}
            for k, v in dict(churn[key]).items():
                ts = self._parse_ts(v)
                if ts is not None and ts > cutoff:
                    kept[k] = v
            churn[key] = kept
        return churn

    def record_open(self, symbol, instrument_id=None, now_utc=None):
        """À appeler pour CHAQUE ouverture approuvée (même en dry-run:
        le budget anti-churn doit être réaliste en simulation)."""
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        churn = self._load_churn(now)
        churn["opens_today"] = int(churn.get("opens_today", 0)) + 1
        churn["last_open_by_symbol"][_clean_symbol(symbol)] = now.isoformat()
        iid = _coerce_int(instrument_id)
        if iid is not None:
            churn["opened_instruments"][str(iid)] = now.isoformat()
        self._write_json("churn.json", churn)

    def _within_min_hold(self, iso_ts, now):
        ts = self._parse_ts(iso_ts)
        return ts is not None and (now - ts) < dt.timedelta(minutes=self.min_hold_minutes)

    # ---- Validation d'une action du cerveau ----
    def evaluate(self, action, total_value, cash, open_positions,
                 entry_rate=None, breaker_active=False, instrument=None, now_utc=None):
        """Retourne (action_approuvée | None, raison).

        `instrument` = résultat RÉSOLU de /market-data/search : obligatoire pour
        un 'open' (identité vérifiée + classification depuis ses métadonnées),
        optionnel pour un 'close' (repli fermeture-par-symbole).
        Pour un 'open', l'action approuvée contient leverage/amount plafonnés
        et stop_loss_rate / take_profit_rate en NIVEAUX DE PRIX (pleine précision).
        """
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        if not isinstance(action, dict):
            return None, "action non-dict"
        a_type = str(action.get("type") or "hold").strip().lower()
        if a_type == "hold":
            return None, "hold"

        # Équité/cash non finis → on ne peut rien approuver de fiable.
        try:
            total_value = float(total_value)
            cash = float(cash)
        except (TypeError, ValueError):
            return None, "équité/cash illisibles"
        if not math.isfinite(total_value) or not math.isfinite(cash) or total_value <= 0:
            return None, "équité/cash non finis ou nuls"

        open_positions = [p for p in (open_positions or []) if isinstance(p, dict)]

        if a_type == "close":
            return self._evaluate_close(action, open_positions, instrument, now)

        if a_type != "open":
            return None, f"type d'action inconnu: {a_type}"

        # -- Ouverture --
        if breaker_active:
            return None, "disjoncteur quotidien actif: ouvertures bloquées jusqu'au prochain jour UTC"
        if len(open_positions) >= self.max_open_positions:
            return None, f"max positions ouvertes atteint ({self.max_open_positions})"

        symbol = str(action.get("symbol") or "").strip()
        if not symbol:
            return None, "symbole vide"

        # Identité d'instrument: le gate ne juge JAMAIS un symbole déclaré seul —
        # il exige l'instrument résolu et la correspondance des symboles.
        if not isinstance(instrument, dict) or not instrument:
            return None, "instrument non résolu (recherche vide ou requête vide) → rejet"
        candidates = instrument_symbols(instrument)
        if not candidates or _clean_symbol(symbol) not in candidates:
            return None, (f"instrument mismatch: '{symbol}' ≠ résultat de recherche "
                          f"{candidates or '(sans symbole)'}")

        # Classe d'actif depuis l'instrument RÉSOLU; le cap final est le min des
        # deux classifications (métadonnées + listes) — jamais moins prudent.
        asset_class = classify_instrument(instrument, symbol)
        cap = min(int(self.leverage_caps.get(asset_class, 1)),
                  int(self.leverage_caps.get(classify_symbol(symbol), 1)),
                  self.global_max_leverage)

        # Anti-churn.
        churn = self._load_churn(now)
        if int(churn.get("opens_today", 0)) >= self.max_opens_per_day:
            return None, (f"anti-churn: {self.max_opens_per_day} ouvertures max "
                          "par jour UTC atteintes")
        last = churn["last_open_by_symbol"].get(_clean_symbol(symbol))
        if last is not None and self._within_min_hold(last, now):
            return None, (f"anti-churn: {symbol} déjà ouvert il y a moins de "
                          f"{self.min_hold_minutes:.0f} min")

        try:
            leverage = int(action.get("leverage") or 1)
        except (TypeError, ValueError):
            leverage = 1
        leverage = max(1, min(leverage, cap))
        # ACTIFS RÉELS : le levier sortant est FORCÉ à 1, quoi que demande le cerveau
        # et quel que soit le cap de classe (on possède l'actif → aucun CFD). On
        # ÉCRASE la valeur, on ne se contente pas de la plafonner.
        if self.force_unleveraged:
            leverage = 1

        try:
            amount = float(action.get("amount_usd") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if not math.isfinite(amount) or amount <= 0:
            return None, "amount_usd non fini ou nul → rejet"
        amount = min(amount, total_value * self.max_amount_pct / 100.0)
        # Réserve de cash: ne jamais descendre sous min_cash_reserve_pct du book.
        available = cash - total_value * self.min_cash_reserve_pct / 100.0
        amount = min(amount, available)
        if not math.isfinite(amount) or amount < 1.0:
            return None, "montant nul après plafonds (cash insuffisant ou réserve atteinte)"

        # Stop-loss OBLIGATOIRE: injecté si absent, plafonné, converti en prix.
        sl_pct = action.get("stop_loss_pct_position")
        try:
            sl_pct = float(sl_pct) if sl_pct else 0.0
        except (TypeError, ValueError):
            sl_pct = 0.0
        if not math.isfinite(sl_pct) or sl_pct <= 0:
            sl_pct = self.default_sl
        sl_pct = min(sl_pct, self.max_sl)

        try:
            entry_rate = float(entry_rate) if entry_rate is not None else None
        except (TypeError, ValueError):
            entry_rate = None
        if entry_rate is None or not math.isfinite(entry_rate) or entry_rate <= 0:
            return None, "pas de prix courant disponible → pas de SL possible → trade rejeté"

        # is_buy: booléen JSON STRICT — "false" (str) inverserait le sens du pari.
        is_buy = action.get("is_buy", True)
        if not isinstance(is_buy, bool):
            return None, f"is_buy non booléen ({is_buy!r}) → rejet"
        # LONG-ONLY : shorter exigerait un CFD (impossible en actif réel). En marché
        # baissier on passe en CASH, on ne parie jamais à la baisse. Les 'close'
        # restent toujours permis (traités par _evaluate_close, jamais ici).
        if self.long_only and is_buy is not True:
            return None, "long-only: shorts interdits (nécessiteraient un CFD)"

        # Une perte de P% sur la POSITION = un mouvement de prix de P/levier.
        move = sl_pct / 100.0 / leverage
        sl_rate = entry_rate * (1 - move) if is_buy else entry_rate * (1 + move)

        tp_rate = None
        tp_pct = action.get("take_profit_pct_position")
        try:
            tp_pct = float(tp_pct) if tp_pct else 0.0
        except (TypeError, ValueError):
            tp_pct = 0.0
        if math.isfinite(tp_pct) and tp_pct > 0:
            tmove = tp_pct / 100.0 / leverage
            tp_rate = entry_rate * (1 + tmove) if is_buy else entry_rate * (1 - tmove)

        if not math.isfinite(sl_rate) or (tp_rate is not None and not math.isfinite(tp_rate)):
            return None, "niveaux SL/TP non finis → rejet"

        # Niveaux de prix envoyés en PLEINE PRÉCISION (pas de round à N décimales).
        return ({"type": "open", "symbol": symbol, "asset_class": asset_class,
                 "is_buy": is_buy, "leverage": leverage,
                 "amount_usd": round(amount, 2),
                 "stop_loss_pct_position": sl_pct,
                 "stop_loss_rate": sl_rate,
                 "take_profit_rate": tp_rate,
                 "instrument_query": str(action.get("instrument_query") or symbol),
                 "rationale": str(action.get("rationale") or "")},
                "open approuvé")

    def _evaluate_close(self, action, open_positions, instrument, now):
        """Fermetures volontairement PERMISSIVES: coercition int des deux côtés,
        repli fermeture-par-symbole si l'ID est mutilé. Seul frein: le min-hold
        anti-churn sur les positions ouvertes par le bot."""
        pid = _coerce_int(action.get("position_id"))
        closes = []
        if pid is not None:
            for p in open_positions:
                if _coerce_int(p.get("positionID")) == pid:
                    closes = [{"position_id": pid,
                               "instrument_id": p.get("instrumentID")}]
                    break

        matched_by = "position_id"
        if not closes:
            # Repli 1: symbole porté par la position elle-même (si présent).
            sym = _clean_symbol(action.get("symbol"))
            if sym:
                for p in open_positions:
                    psym = _clean_symbol(p.get("symbol") or p.get("symbolFull")
                                         or p.get("instrumentName") or "")
                    if psym and psym == sym:
                        closes.append({"position_id": _coerce_int(p.get("positionID")),
                                       "instrument_id": p.get("instrumentID")})
                matched_by = "symbol"
            # Repli 2: instrument résolu par main.py (fermer tout l'instrument).
            if not closes and isinstance(instrument, dict):
                low = _lower_keys(instrument)
                iid = None
                for f in ("instrumentid", "instrument_id", "id"):
                    iid = _coerce_int(low.get(f))
                    if iid is not None:
                        break
                if iid is not None:
                    for p in open_positions:
                        if _coerce_int(p.get("instrumentID")) == iid:
                            closes.append({"position_id": _coerce_int(p.get("positionID")),
                                           "instrument_id": iid})
                    matched_by = "instrument résolu"

        closes = [c for c in closes if c["position_id"] is not None
                  and c["instrument_id"] is not None]
        if not closes:
            return None, f"close rejeté: position_id inconnu ({action.get('position_id')})"

        # Min-hold: une position ouverte par le bot il y a < min_hold ne se
        # referme pas (le SL/TP la protège; ceci coupe le churn payé en spread).
        churn = self._load_churn(now)
        for c in closes:
            ts = churn["opened_instruments"].get(str(_coerce_int(c["instrument_id"])))
            if ts is not None and self._within_min_hold(ts, now):
                return None, (f"anti-churn: min-hold {self.min_hold_minutes:.0f} min "
                              "avant de fermer une position ouverte par le bot")

        return ({"type": "close",
                 "position_id": closes[0]["position_id"],
                 "instrument_id": closes[0]["instrument_id"],
                 "closes": closes,
                 "rationale": str(action.get("rationale") or "")},
                f"close approuvé (par {matched_by})")
