"""Client eToro minimaliste — Agent Portfolio (livre virtuel de 10 000 $).

Chaque requête porte 3 en-têtes d'authentification + un x-request-id (UUID4)
frais. Tous les montants sont en USD contre le livre virtuel.

Politique de retry (revue adversariale) :
- LECTURES : jusqu'à MAX_TRIES essais sur erreur réseau, 5xx et 429.
- ÉCRITURES : JAMAIS de rejeu après un ReadTimeout ou un 5xx — l'ordre a
  peut-être déjà été exécuté côté serveur, et le x-request-id frais ferait du
  rejeu un ORDRE DUPLIQUÉ. On lève AmbiguousWriteError et le /pnl du cycle
  suivant sert de réconciliation. Seules exceptions rejouables en écriture :
  échec de connexion strictement AVANT envoi (ConnectionError sans réponse,
  ConnectTimeout) et 429 (l'ordre a été rejeté par le rate limiter, jamais
  exécuté).
"""
import math
import os
import time
import uuid

import requests

BASE_URL = "https://public-api.etoro.com/api/v1"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Champs candidats pour extraire un prix courant d'un résultat de recherche.
# 'close' en DERNIER: c'est potentiellement un prix de clôture de la veille.
RATE_FIELDS = ("rate", "currentRate", "lastRate", "lastPrice", "price", "ask", "bid", "close")
ID_FIELDS = ("instrumentid", "instrument_id", "id")
# Champs candidats pour l'identité (symbole) d'un instrument résolu.
SYMBOL_FIELDS = ("internalsymbolfull", "symbolfull", "internalsymbol", "symbol", "ticker")

MAX_RETRY_AFTER_S = 30.0  # un serveur qui demande 30 min ne doit pas bloquer le job


class EtoroError(Exception):
    """Erreur renvoyée par l'API eToro ({errorCode, errorMessage})."""

    def __init__(self, error_code, error_message):
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"{error_code}: {error_message}")


class AmbiguousWriteError(EtoroError):
    """Échec AMBIGU d'une écriture: l'ordre a PEUT-ÊTRE été exécuté côté serveur.

    Ne jamais rejouer. Le /pnl du cycle suivant est la réconciliation.
    """


def _lower_keys(d):
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def extract_rate(instrument, is_buy=None):
    """Meilleur effort: prix courant d'un instrument, ou None.

    Si is_buy est un booléen, préfère le prix vivant du bon côté du carnet
    (achat → ask, vente → bid) avant les champs génériques.
    """
    low = _lower_keys(instrument)
    fields = list(RATE_FIELDS)
    if is_buy is True:
        fields = ["ask"] + [f for f in fields if f != "ask"]
    elif is_buy is False:
        fields = ["bid"] + [f for f in fields if f != "bid"]
    for field in fields:
        v = low.get(field.lower())
        if isinstance(v, (int, float)) and math.isfinite(float(v)) and v > 0:
            return float(v)
    return None


def extract_instrument_id(instrument):
    low = _lower_keys(instrument)
    for field in ID_FIELDS:
        v = low.get(field)
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            return int(v)
    return None


def extract_symbol(instrument):
    """Symbole du résultat de recherche (internalSymbolFull/symbol...), ou None."""
    low = _lower_keys(instrument)
    for field in SYMBOL_FIELDS:
        v = low.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
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

    @staticmethod
    def _retryable_write_exc(exc):
        """True seulement si l'échec est STRICTEMENT antérieur à l'envoi.

        ConnectTimeout et ConnectionError sans réponse: la connexion n'a pas
        abouti, l'ordre n'a pas pu atteindre le serveur → rejouable.
        ReadTimeout (et tout le reste): l'ordre a pu partir → ambigu.
        """
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
        return (isinstance(exc, requests.exceptions.ConnectionError)
                and getattr(exc, "response", None) is None)

    def _request(self, method, path, params=None, body=None, is_write=False):
        if is_write:  # espacement des ordres
            wait = self.WRITE_SPACING_S - (time.monotonic() - self._last_write)
            if wait > 0:
                time.sleep(wait)
        url = BASE_URL + path
        last_err = None
        for attempt in range(self.MAX_TRIES):
            last_attempt = attempt == self.MAX_TRIES - 1
            try:
                resp = requests.request(method, url, headers=self._headers(),
                                        params=params, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                if is_write and not self._retryable_write_exc(exc):
                    # L'ordre a pu être exécuté — rejouer = dupliquer. On s'arrête.
                    self._last_write = time.monotonic()
                    raise AmbiguousWriteError(
                        "AMBIGUOUS_WRITE",
                        f"{method} {path}: {type(exc).__name__} après envoi possible "
                        f"— ordre peut-être exécuté, pas de rejeu ({exc})")
                last_err = exc
                if not last_attempt:
                    time.sleep(2 ** attempt)
                continue
            if is_write:
                self._last_write = time.monotonic()
            if resp.status_code == 429:
                # 429 = rejeté par le rate limiter, jamais exécuté → rejouable
                # même en écriture. Retry-After plafonné (jamais > 30 s).
                last_err = None
                if last_attempt:
                    break
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    retry_after = float(2 ** (attempt + 1))
                time.sleep(min(max(retry_after, 0.0), MAX_RETRY_AFTER_S))
                continue
            if resp.status_code >= 500:
                if is_write:
                    # Un 502/504 de passerelle peut arriver APRÈS exécution.
                    raise AmbiguousWriteError(
                        "AMBIGUOUS_WRITE",
                        f"{method} {path}: HTTP {resp.status_code} sur écriture "
                        f"— ordre peut-être exécuté, pas de rejeu")
                last_err = None
                if not last_attempt:
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
        """Premier instrument correspondant à la recherche (dict), ou None.

        Une requête vide est refusée (jamais trader items[0] de « tout »).
        """
        query = str(query or "").strip()
        if not query:
            return None
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
            # NIVEAU de prix, pleine précision (pas d'arrondi à N décimales:
            # sur un actif micro-coté ça déformerait le stop).
            body["StopLossRate"] = float(stop_loss_rate)
        if take_profit_rate is not None:
            body["TakeProfitRate"] = float(take_profit_rate)
        return self._request("POST", "/trading/execution/market-open-orders/by-amount",
                             body=body, is_write=True)

    def close_position(self, position_id, instrument_id):
        body = {"InstrumentId": int(instrument_id), "UnitsToDeduct": None}  # null = fermer tout
        return self._request(
            "POST", f"/trading/execution/market-close-orders/positions/{int(position_id)}",
            body=body, is_write=True)
