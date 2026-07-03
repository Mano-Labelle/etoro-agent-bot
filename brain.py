"""Cerveau de recherche: Claude + outil web_search côté serveur.

Chaque cycle fait de la vraie recherche web (tendance, catalyseurs, momentum)
puis rend une décision JSON. Le parsing est robuste: en cas d'échec → hold.
La CONSTRUCTION même du cerveau peut échouer (clé absente, paquet manquant):
main.py l'enveloppe et dégrade en SAFE_HOLD — jamais de crash de cycle.
"""
import datetime as dt
import json
from zoneinfo import ZoneInfo

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

TZ_NY = ZoneInfo("America/New_York")
TZ_PARIS = ZoneInfo("Europe/Paris")


# DOCTRINE — distillée d'une revue de littérature vérifiée (votes adversariaux 3-0).
# Chaque règle est sourcée ; les garde-fous durs de risk_gate.py restent souverains.
DOCTRINE = """DOCTRINE DE TRADING (fondée sur l'évidence académique vérifiée — respecte-la) :

1) DIRECTION = MOMENTUM. Ne prends une position que dans le SENS du rendement passé
   récent de l'instrument lui-même (long si tendance haussière 1 sem.–3 mois, short si
   baissière). N'invente pas de retournement ("catch a falling knife" interdit).
   [Moskowitz-Ooi-Pedersen 2012, 58 futures]. En intraday, n'agis sur un signal
   d'ouverture que les jours à FORTE volatilité/volume/actualité [Gao et al. 2018].

2) TAILLE = VOLATILITÉ INVERSE + DEMI-KELLY. Position d'autant PLUS PETITE que la
   volatilité récente de l'actif est haute. Levier EFFECTIF cible ~1,5x (le levier
   eToro élevé sert à immobiliser peu de marge, PAS à s'exposer 20x) [Moreira-Muir
   2017]. Ne risque jamais plus de ~0,5x ta fraction de Kelly estimée, et DIVISE
   toujours par 2 ta confiance dans ton edge (tes estimations sont des guesses
   bruitées, biaisées à la hausse) [Thorp ; Chopra-Ziemba 20:2:1].

3) FILTRE DE RÉGIME. Marché en baisse + volatilité élevée = état de panique →
   suspends le momentum et N'OUVRE PAS de nouveaux SHORTS pendant un rebond (les
   crashs de momentum viennent de là) [Daniel-Moskowitz 2016]. Si tes propres
   derniers trades sont très volatils/perdants, réduis les tailles. Pas de signal
   clair = pas de position (le CASH est une position légitime).

4) ANTI-RUINE. Un drawdown de -30/-40% est une trajectoire NORMALE à ce profil, pas
   une urgence. JAMAIS de martingale, JAMAIS d'augmentation de taille après une perte
   pour "se refaire" (sur-parier = croissance négative garantie) [MacLean-Thorp-Ziemba].
   Chaque position a un stop AVANT l'entrée. En cas de doute sur l'edge : taille zéro.

5) COÛTS. À cette cadence, spreads + financement overnight peuvent manger tout l'edge.
   Évite le sur-trading : privilégie peu de paris à forte conviction tenus plusieurs
   heures/jours plutôt que beaucoup d'aller-retours. Ne réouvre pas un symbole que tu
   viens de fermer.

RÈGLES RÉFUTÉES (n'y crois PAS) : les croisements de moyennes mobiles et breakouts
naïfs n'ont PAS d'edge fiable [Brock et al. réfuté] — n'ouvre pas une position sur ce
seul motif."""


def build_system_prompt(config, tactics=None):
    caps = json.dumps((config.get("risk") or {}).get("leverage_caps") or {})
    prompt = (
        "Tu es le directeur d'investissement d'un livre VIRTUEL de 10 000 $ en CFD sur "
        "eToro. Mandat : AGRESSIF, paris concentrés à forte conviction, objectif de "
        "multiplier le capital — mais discipliné par la doctrine ci-dessous. C'est de "
        "l'argent de jeu assumé.\n\n"
        + DOCTRINE + "\n\n"
        f"Classes autorisées et levier max par classe : {caps}. Un garde-fou "
        "déterministe plafonnera de toute façon levier, montant et stop-loss, et "
        "limitera le nombre d'ouvertures par jour — inutile de le tester.\n"
        "PROCESSUS : fais d'abord des recherches web (tendance de marché, catalyseurs, "
        "momentum, actualité macro) PUIS décide : ouvrir, fermer, ou ne rien faire.\n"
        "stop_loss_pct_position / take_profit_pct_position sont des % de la POSITION "
        "(ex. 40 = perte max de 40% de la mise). position_id ne sert que pour 'close'.\n"
    )
    if tactics:  # amendements tactiques auto-appris (rétro hebdo) — voir retro.py
        prompt += ("\nLEÇONS DE TES RÉTROSPECTIVES (tu les as écrites toi-même en "
                   "analysant tes trades passés — applique-les) :\n" + str(tactics)[:2000] + "\n")
    prompt += ("\nTu réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, "
               "au format : " + DECISION_SCHEMA)
    return prompt


def markets_open_note(now_utc):
    """Quels marchés sont ouverts — calculé dans les fuseaux RÉELS (zoneinfo),
    donc correct été comme hiver (DST)."""
    ny = now_utc.astimezone(TZ_NY)
    paris = now_utc.astimezone(TZ_PARIS)
    # FX: fermé du vendredi 17:00 au dimanche 17:00, heure de New York.
    fx_open = not (ny.weekday() == 5
                   or (ny.weekday() == 4 and ny.time() >= dt.time(17, 0))
                   or (ny.weekday() == 6 and ny.time() < dt.time(17, 0)))
    # NYSE/Nasdaq: 09:30–16:00 America/New_York.
    us_open = ny.weekday() < 5 and dt.time(9, 30) <= ny.time() < dt.time(16, 0)
    # Europe (Paris/Francfort): 09:00–17:30 Europe/Paris.
    eu_open = paris.weekday() < 5 and dt.time(9, 0) <= paris.time() < dt.time(17, 30)
    return ("crypto: 24/7; FX: " + ("ouvert" if fx_open else "fermé (week-end)")
            + "; actions/indices US: " + ("ouvert" if us_open else "fermé")
            + "; actions/indices Europe: " + ("ouvert" if eu_open else "fermé"))


def _reject_json_constant(name):
    # NaN/Infinity ne sont pas du JSON standard: json.loads les accepte par
    # défaut, mais un NaN traverserait ensuite les plafonds du gate.
    raise ValueError(f"constante JSON non autorisée: {name}")


def extract_first_json(text):
    """Extrait le premier objet JSON équilibré du texte, ou None.

    NaN/Infinity/-Infinity sont REFUSÉS (parse_constant) — jamais de non-finis.
    """
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
                        return json.loads(text[start:i + 1],
                                          parse_constant=_reject_json_constant)
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def load_tactics(state_dir="state"):
    """Amendements tactiques que le bot s'est écrits lui-même (rétro hebdo).

    Fichier optionnel `state/doctrine_tactics.md` — mémoire de long terme (étage 2
    de la boucle de self-learning). Absent au démarrage = doctrine de base seule.
    """
    try:
        with open(os.path.join(state_dir, "doctrine_tactics.md"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


class Brain:
    def __init__(self, config, state_dir="state"):
        self.config = config or {}
        self.model = self.config.get("model", "claude-sonnet-5")
        self.max_web_searches = int(self.config.get("max_web_searches", 6))
        # Variante plus récente disponible: "web_search_20260209" (filtrage dynamique).
        self.web_search_tool_type = self.config.get("web_search_tool_type",
                                                    "web_search_20250305")
        self.tactics = load_tactics(state_dir)  # self-learning: leçons auto-écrites
        # Import paresseux: l'absence du paquet/de la clé dégrade en SAFE_HOLD
        # via le try/except de main.py au lieu de casser l'import du module.
        import anthropic
        # timeout court + 1 seul retry SDK: pire cas ≈ 4 min × tours pause_turn,
        # confortablement sous le timeout-minutes: 15 du job (défauts SDK: 600 s ×3).
        self.client = anthropic.Anthropic(timeout=120.0, max_retries=1)

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
                    system=build_system_prompt(self.config, tactics=self.tactics),
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
