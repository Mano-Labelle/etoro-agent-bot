"""Cerveau de recherche: Claude + outil web_search côté serveur.

Chaque cycle fait de la vraie recherche web (tendance, catalyseurs, momentum)
puis rend une décision JSON. Le parsing est robuste: en cas d'échec → hold.
"""
import datetime as dt
import json
import os

import anthropic

SAFE_HOLD = {
    "actions": [{"type": "hold", "symbol": "", "instrument_query": "", "is_buy": True,
                 "leverage": 1, "amount_usd": 0.0, "stop_loss_pct_position": 40.0,
                 "take_profit_pct_position": None, "position_id": None,
                 "rationale": "réponse du cerveau illisible ou indisponible → on ne fait rien"}],
    "market_note": "fallback: hold",
}

DECISION_SCHEMA = (
    '{"actions": [{"type": "open"|"close"|"hold", "symbol": str, "instrument_query": str, '
    '"is_buy": bool, "leverage": int, "amount_usd": float, '
    '"stop_loss_pct_position": float, "take_profit_pct_position": float|null, '
    '"position_id": int|null, "rationale": str}], "market_note": str}'
)


def build_system_prompt(config):
    caps = json.dumps((config.get("risk") or {}).get("leverage_caps") or {})
    return (
        "Tu es un trader momentum/catalyseurs AGRESSIF qui gère un livre VIRTUEL de "
        "10 000 $ en CFD sur eToro. Style: paris concentrés à forte conviction, levier "
        "élevé, objectif de multiplier le capital. C'est de l'argent de jeu assumé.\n"
        f"Classes d'actifs autorisées et levier max par classe: {caps}. "
        "Un garde-fou déterministe plafonnera de toute façon levier, montant et stop-loss.\n"
        "Fais d'abord des recherches web (actualités, catalyseurs, momentum, tendance de "
        "marché) puis décide: ouvrir, fermer ou ne rien faire.\n"
        "stop_loss_pct_position et take_profit_pct_position sont des % de la POSITION "
        "(ex. 40 = perte max de 40% de la mise). position_id ne sert que pour 'close'.\n"
        "Tu réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, au format: "
        + DECISION_SCHEMA
    )


def markets_open_note(now_utc):
    """Quels marchés sont probablement ouverts (approximatif, en UTC)."""
    wd = now_utc.weekday()  # 0 = lundi
    h = now_utc.hour + now_utc.minute / 60.0
    fx_open = not (wd == 5 or (wd == 4 and h >= 21) or (wd == 6 and h < 21))
    us_open = wd < 5 and 13.5 <= h < 20
    eu_open = wd < 5 and 7 <= h < 15.5
    return ("crypto: 24/7; FX: " + ("ouvert" if fx_open else "fermé (week-end)")
            + "; actions/indices US: " + ("ouvert" if us_open else "fermé")
            + "; actions/indices Europe: " + ("ouvert" if eu_open else "fermé"))


def extract_first_json(text):
    """Extrait le premier objet JSON équilibré du texte, ou None."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


class Brain:
    def __init__(self, config):
        self.config = config or {}
        self.model = self.config.get("model", "claude-sonnet-5")
        self.max_web_searches = int(self.config.get("max_web_searches", 6))
        # Variante plus récente disponible: "web_search_20260209" (filtrage dynamique).
        self.web_search_tool_type = self.config.get("web_search_tool_type",
                                                    "web_search_20250305")
        self.client = anthropic.Anthropic()  # lit ANTHROPIC_API_KEY

    def decide(self, portfolio_state):
        """portfolio_state: dict (cash, valeur, positions+pnL, halte, breaker...)."""
        now = dt.datetime.now(dt.timezone.utc)
        user_msg = (
            f"Heure UTC: {now.isoformat(timespec='minutes')} — marchés: {markets_open_note(now)}\n"
            "État actuel du portefeuille (JSON brut):\n"
            + json.dumps(portfolio_state, ensure_ascii=False, default=str)
            + "\nFais tes recherches web puis réponds UNIQUEMENT avec le JSON de décision."
        )
        try:
            messages = [{"role": "user", "content": user_msg}]
            for _ in range(4):  # pause_turn: l'outil serveur peut demander à continuer
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=build_system_prompt(self.config),
                    messages=messages,
                    tools=[{"type": self.web_search_tool_type, "name": "web_search",
                            "max_uses": self.max_web_searches}],
                )
                if resp.stop_reason != "pause_turn":
                    break
                messages = [{"role": "user", "content": user_msg},
                            {"role": "assistant", "content": resp.content}]
            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text")
        except Exception as exc:  # panne API, clé absente… → hold, jamais de crash
            hold = json.loads(json.dumps(SAFE_HOLD))
            hold["market_note"] = f"erreur API cerveau: {exc}"
            return hold

        decision = extract_first_json(text)
        if not isinstance(decision, dict) or not isinstance(decision.get("actions"), list):
            return json.loads(json.dumps(SAFE_HOLD))
        decision.setdefault("market_note", "")
        return decision
