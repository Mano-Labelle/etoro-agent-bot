"""Cerveau de recherche: OpenAI (Responses API) + outil `web_search` natif.

Chaque cycle fait de la VRAIE recherche web (tendance, catalyseurs, momentum)
puis rend une décision JSON. Parsing robuste: en cas d'échec → hold. La
CONSTRUCTION du cerveau peut échouer (clé absente, paquet manquant): main.py
l'enveloppe et dégrade en SAFE_HOLD — jamais de crash de cycle.

Fournisseur: OpenAI. Modèle par défaut gpt-5.4-mini (bon rapport capacité/coût,
modèle de raisonnement), recherche web via l'outil hébergé `web_search`. La clé
vient de OPENAI_API_KEY. Coût typique par cycle: quelques centimes (tokens
négligeables + 0,01 $/recherche web).
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

# DOCTRINE — distillée d'une revue de littérature vérifiée (votes adversariaux 3-0),
# révisée pour le régime ACTIFS RÉELS SANS CFD (le backtest a prouvé que le CFD à
# levier était un piège à frais : spread + financement overnight ~10 %/an).
# Chaque règle est sourcée ; les garde-fous durs de risk_gate.py restent souverains.
DOCTRINE = """DOCTRINE DE TRADING — ACTIFS RÉELS, NON-LEVIÉRÉS, LONG-ONLY (respecte-la) :

RÉGIME. Tu tradées des ACTIFS RÉELS que tu POSSÈDES (crypto en spot, actions au
comptant), JAMAIS des CFD. Conséquences dures :
  • LEVIER TOUJOURS 1. Le champ `leverage` sera de toute façon forcé à 1 par le
    garde-fou — ne le gaspille pas, mets 1. La perte est bornée à la mise engagée.
  • LONG-ONLY. Impossible de shorter sans CFD. En marché BAISSIER, la seule réponse
    est le CASH (ne rien détenir), JAMAIS un pari à la baisse. Un `is_buy=false` sera
    rejeté par le garde-fou.

1) DIRECTION = MOMENTUM (ÉVITE-KRACH). Ne détiens un actif que si son momentum récent
   est HAUSSIER (mom_1w/1m/3m > 0). N'attrape pas un couteau qui tombe
   [Moskowitz-Ooi-Pedersen 2012]. Le momentum sert d'abord de FILTRE ANTI-KRACH : en
   tendance baissière d'un actif, on n'y touche pas (cash). C'est VALIDÉ — le momentum
   a esquivé les krachs crypto de -47 % / -61 % en restant à l'écart.

2) TAILLE = VOLATILITÉ INVERSE + DEMI-KELLY. Position d'autant PLUS PETITE que la
   volatilité récente de l'actif est haute [Moreira-Muir 2017]. Sans levier, la
   volatilité intrinsèque de l'actif fait déjà tout le travail : ces cryptos/actions
   bougent assez pour multiplier une mise à 1x. Ne risque jamais plus de ~0,5x ta
   fraction de Kelly estimée, et DIVISE par 2 ta confiance dans ton edge (estimations
   bruitées, biaisées à la hausse) [Thorp ; Chopra-Ziemba 20:2:1].

3) FILTRE DE RÉGIME. Marché en baisse + volatilité élevée = panique → n'ouvre RIEN,
   reste en cash [Daniel-Moskowitz 2016]. Si tes derniers trades sont perdants,
   réduis les tailles. Pas de signal clair = pas de position (le CASH est une position
   légitime, souvent la meilleure).

4) ANTI-RUINE. Un drawdown de -30/-40 % est une trajectoire NORMALE à ce profil, pas
   une urgence. JAMAIS de martingale, JAMAIS d'augmentation de taille après une perte
   pour "se refaire" [MacLean-Thorp-Ziemba]. Chaque position a un stop AVANT l'entrée.
   En cas de doute sur l'edge : taille zéro.

5) COÛTS = SPREAD UNIQUEMENT. Plus de financement overnight (on possède l'actif). Le
   SEUL coût est le spread à l'aller-retour : crypto ~2 %, actions ~0. Donc TIENS tes
   positions plusieurs JOURS / SEMAINES et tradés RAREMENT. Le sur-trading crypto à
   2 % par aller-retour est FATAL — le backtest l'a prouvé. "Ne rien faire" est
   l'action PAR DÉFAUT : justifie chaque trade, ne justifie JAMAIS l'attente. Ne
   réouvre pas un symbole que tu viens de fermer.

RÈGLES RÉFUTÉES (n'y crois PAS) : les croisements de moyennes mobiles et breakouts
naïfs n'ont PAS d'edge fiable [Brock et al. réfuté] — n'ouvre pas une position sur ce
seul motif."""


def build_system_prompt(config, tactics=None):
    prompt = (
        "Tu es le directeur d'investissement d'un livre VIRTUEL de 10 000 $ sur eToro, "
        "investi en ACTIFS RÉELS que tu POSSÈDES (crypto en spot, actions au comptant) — "
        "PAS de CFD, PAS de levier, PAS de short. Mandat : ambitieux, paris concentrés à "
        "forte conviction pour multiplier le capital, mais discipliné par la doctrine "
        "ci-dessous. C'est de l'argent de jeu assumé, mais la perte est bornée à la mise.\n\n"
        + DOCTRINE + "\n\n"
        "GARDE-FOU. Un garde-fou déterministe FORCE le levier à 1 sur toute ouverture, "
        "REJETTE tout `is_buy=false` (long-only), plafonne le montant et le stop-loss, et "
        "limite le nombre d'ouvertures par jour — inutile de le tester. Mets toujours "
        "`leverage`: 1 et `is_buy`: true.\n"
        "UNIVERS : crypto majors liquides (BTC, ETH, SOL, XRP, DOGE) + actions high-beta / "
        "thématiques (NVDA, TSLA, AMD, PLTR, MSTR). Ces actifs sont assez VOLATILS pour "
        "multiplier une mise SANS levier — leur volatilité intrinsèque fait le travail.\n"
        "  • ACTIONS INDIVIDUELLES = riches en catalyseurs datés (résultats, guidance, "
        "upgrades, lancements). Quand la bourse est ouverte et qu'un catalyseur clair pousse "
        "un titre, c'est souvent le pari le plus LISIBLE. CAVEAT : une action peut GAPPER "
        "(profit warning -20 % overnight) par-dessus le stop → ne JAMAIS garder une action à "
        "travers sa publication de résultats sans réduire fortement la taille.\n"
        "  • CRYPTO = ouverte 24/7, très volatile, MAIS spread ~2 % à l'aller-retour → à "
        "TENIR longtemps, jamais à sur-trader.\n"
        "Si aucun actif n'a un momentum haussier ET un catalyseur à conviction VRAIMENT "
        "forte, ATTENDS : le CASH est une position, souvent la meilleure.\n"
        "DONNÉES CHIFFRÉES (vérité) : le champ `market_data` de l'état contient, pour la "
        "watchlist réelle, le momentum CALCULÉ (mom_1w/1m/3m en %), la volatilité "
        "JOURNALIÈRE réelle (vol_daily_%), le drawdown depuis le plus-haut et l'écart à la "
        "MM20 — tirés de vraies séries de prix. UTILISE CES CHIFFRES comme source de "
        "vérité : (1) DIRECTION = signe du momentum passé — n'ouvre QUE si mom_1m/mom_3m > 0 "
        "(sinon cash, jamais de short) ; (2) TAILLE = inverse de la volatilité (plus "
        "vol_daily_% est haute, PLUS PETITE la position). Ne te fie PAS aux pourcentages lus "
        "dans les articles pour ça — la recherche web sert à expliquer le POURQUOI et à "
        "confirmer le catalyseur, pas à mesurer le momentum.\n"
        "PROCESSUS D'ANALYSE (obligatoire avant toute ouverture) : (a) lis `market_data` "
        "pour repérer les 2-3 actifs au meilleur momentum HAUSSIER ; (b) recherche web le "
        "CATALYSEUR et l'actualité du jour pour ces candidats ; (c) pour le meilleur, "
        "formule une thèse HAUSSIÈRE *et* le risque baissier (qu'est-ce qui invaliderait le "
        "trade ? → dans ce cas, reste en cash) ; (d) ne l'ouvre que si le momentum chiffré "
        "haussier ET le catalyseur convergent. Cite les chiffres de market_data + la source "
        "news dans le rationale.\n"
        "stop_loss_pct_position / take_profit_pct_position sont des % de la POSITION "
        "(ex. 40 = perte max de 40 % de la mise). position_id ne sert que pour 'close'.\n"
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
    import os
    try:
        with open(os.path.join(state_dir, "doctrine_tactics.md"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


class Brain:
    def __init__(self, config, state_dir="state"):
        self.config = config or {}
        self.model = self.config.get("model", "gpt-5.4-mini")
        # Recherche web native via l'outil hébergé `web_search` de la Responses API.
        self.web_search = bool(self.config.get("web_search_enabled", True))
        # gpt-5.x sont des modèles de RAISONNEMENT: effort low/medium/high. Mettre
        # une chaîne vide pour un modèle non-raisonnant (ex. gpt-4.1).
        self.reasoning_effort = self.config.get("reasoning_effort", "low")
        self.max_output_tokens = int(self.config.get("max_output_tokens", 8000))
        self.max_web_searches = int(self.config.get("max_web_searches", 3))
        self.tactics = load_tactics(state_dir)  # self-learning: leçons auto-écrites
        # Import paresseux: l'absence du paquet/de la clé dégrade en SAFE_HOLD
        # via le try/except de main.py au lieu de casser l'import du module.
        import openai
        # timeout court + 1 seul retry SDK: la Responses API + recherche web peut
        # boucler côté serveur; on reste bien sous le timeout-minutes: 15 du job.
        self.client = openai.OpenAI(timeout=120.0, max_retries=1)

    def decide(self, portfolio_state):
        """portfolio_state: dict (cash, valeur, positions+pnL, halte, breaker...)."""
        now = dt.datetime.now(dt.timezone.utc)
        user_msg = (
            f"Heure UTC: {now.isoformat(timespec='minutes')} — marchés: {markets_open_note(now)}\n"
            f"Fais au plus {self.max_web_searches} recherches web CIBLÉES (tendance de "
            "marché, catalyseurs, momentum, actualité macro) puis décide.\n"
            "État actuel du portefeuille (JSON brut):\n"
            + json.dumps(portfolio_state, ensure_ascii=False, default=str)
            + "\nRéponds UNIQUEMENT avec le JSON de décision, sans texte autour."
        )
        try:
            kwargs = dict(
                model=self.model,
                instructions=build_system_prompt(self.config, tactics=self.tactics),
                input=user_msg,
                max_output_tokens=self.max_output_tokens,
            )
            if self.web_search:
                kwargs["tools"] = [{"type": "web_search"}]
            if self.reasoning_effort:
                kwargs["reasoning"] = {"effort": self.reasoning_effort}
            resp = self.client.responses.create(**kwargs)
            text = resp.output_text or ""
        except Exception as exc:  # panne API, clé absente… → hold, jamais de crash
            hold = json.loads(json.dumps(SAFE_HOLD))
            hold["market_note"] = f"erreur API cerveau (OpenAI): {exc}"
            return hold

        decision = extract_first_json(text)
        if not isinstance(decision, dict) or not isinstance(decision.get("actions"), list):
            return json.loads(json.dumps(SAFE_HOLD))
        decision.setdefault("market_note", "")
        return decision
