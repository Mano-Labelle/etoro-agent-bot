"""Client eToro minimaliste — Agent Portfolio (livre virtuel de 10 000 $).

Chaque requête porte 3 en-têtes d'authentification + un x-request-id (UUID4)
frais. Tous les montants sont en USD contre le livre virtuel.
"""
import os
import time
import uuid

import requests

BASE_URL = "https://public-api.etoro.com/api/v1"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Champs candidats pour extraire un prix courant d'un résultat de recherche.
RATE_FIELDS = ("rate", "currentRate", "lastRate", "lastPrice", "price", "ask", "bid", "close")
ID_FIELDS = ("instrumentid", "instrument_id", "id")


class EtoroError(Exception):
    """Erreur renvoyée par l'API eToro ({errorCode, errorMessage})."""

    def __init__(self, error_code, error_message):
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"{error_code}: {error_message}")


def _lower_keys(d):
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def extract_rate(instrument):
    """Meilleur effort: prix courant d'un instrument, ou None."""
    low = _lower_keys(instrument)
    for field in RATE_FIELDS:
        v = low.get(field.lower())
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def extract_instrument_id(instrument):
    low = _lower_keys(instrument)
    for field in ID_FIELDS:
        v = low.get(field)
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            return int(v)
    return None


class EtoroClient:
    WRITE_SPACING_S = 3.0  # groupe écriture: 20 req/min → >= 3 s entre trades
    MAX_TRIES = 3

    def __init__(self, public_key=None, private_key=None, timeout=30):
        self.public_key = public_key or os.environ.get("ETORO_PUBLIC_KEY", "")
        self.private_key = private_key or os.environ.get("ETORO_PRIVATE_KEY", "")
        if not self.public_key or not self.private_key:
            raise EtoroError("MISSING_KEYS",
                             "ETORO_PUBLIC_KEY / ETORO_PRIVATE_KEY absents de l'environnement")
        self.timeout = timeout
        self._last_write = 0.0

    def _headers(self):
        return {
            "x-api-key": self.public_key,
            "x-user-key": self.private_key,
            "x-request-id": str(uuid.uuid4()),  # frais à chaque appel
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _request(self, method, path, params=None, body=None, is_write=False):
        if is_write:  # espacement des ordres
            wait = self.WRITE_SPACING_S - (time.monotonic() - self._last_write)
            if wait > 0:
                time.sleep(wait)
        url = BASE_URL + path
        last_err = None
        for attempt in range(self.MAX_TRIES):
            try:
                resp = requests.request(method, url, headers=self._headers(),
                                        params=params, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = exc
                time.sleep(2 ** attempt)
                continue
            if is_write:
                self._last_write = time.monotonic()
            if resp.status_code == 429:  # honorer Retry-After
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = float(2 ** (attempt + 1))
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                data = None
            # Payload d'erreur eToro (marché fermé, ordre rejeté, etc.)
            if isinstance(data, dict) and data.get("errorCode"):
                raise EtoroError(data.get("errorCode"), data.get("errorMessage", ""))
            if resp.status_code >= 400:
                raise EtoroError(str(resp.status_code), resp.text[:300])
            return data
        if last_err is not None:
            raise EtoroError("NETWORK", str(last_err))
        raise EtoroError("RETRIES_EXHAUSTED",
                         f"{method} {path}: échec après {self.MAX_TRIES} tentatives")

    # ---- Lecture ----
    def get_portfolio(self):
        return self._request("GET", "/trading/info/aggregate-portfolio",
                             params={"conversionMode": "eToroApp"})

    def get_pnl(self):
        return self._request("GET", "/trading/info/real/pnl")

    def search_instrument(self, query):
        """Premier instrument correspondant à la recherche (dict), ou None."""
        data = self._request("GET", "/market-data/search", params={"query": query})
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("instruments") or data.get("results") or []
        else:
            items = []
        return items[0] if items else None

    # ---- Écriture ----
    def open_position(self, instrument_id, is_buy, leverage, amount_usd,
                      stop_loss_rate=None, take_profit_rate=None):
        body = {
            "InstrumentID": int(instrument_id),
            "IsBuy": bool(is_buy),
            "Leverage": int(leverage),
            "Amount": float(amount_usd),
        }
        if stop_loss_rate is not None:
            body["StopLossRate"] = float(stop_loss_rate)  # NIVEAU de prix, pas un %
        if take_profit_rate is not None:
            body["TakeProfitRate"] = float(take_profit_rate)
        return self._request("POST", "/trading/execution/market-open-orders/by-amount",
                             body=body, is_write=True)

    def close_position(self, position_id, instrument_id):
        body = {"InstrumentId": int(instrument_id), "UnitsToDeduct": None}  # null = fermer tout
        return self._request(
            "POST", f"/trading/execution/market-close-orders/positions/{position_id}",
            body=body, is_write=True)
