"""Rétrospective hebdomadaire — étage 2 de la boucle de self-learning.

Lancé le dimanche (marchés fermés) par un workflow séparé. Le bot analyse ses
propres trades de la semaine + sa courbe d'équité, écrit une rétro horodatée
(data/retro-AAAA-MM-JJ.md, versionnée) et AMENDE son playbook TACTIQUE
(state/doctrine_tactics.md), réinjecté dans son prompt à chaque cycle suivant.

GARDE-FOU D'APPRENTISSAGE (asymétrie) : la rétro ne touche QUE la tactique
(quels setups marchent, quoi éviter). Elle ne peut PAS modifier la cage de
risque (leviers, tailles, stop, plancher, disjoncteur) — celle-ci vit dans
config.yaml/risk_gate.py, hors de portée du LLM. Interdiction explicite de
conclure "augmenter la taille après des pertes" (anti-martingale).
Humilité statistique : un motif ne devient une règle que s'il a n>=5 occurrences ;
sinon il est noté "à surveiller".
"""
import datetime as dt
import json
import os

import yaml

import tracker

HERE = os.path.dirname(os.path.abspath(__file__))

RETRO_SYSTEM = """Tu es l'analyste-risque qui fait la rétrospective HEBDOMADAIRE d'un
bot de trading CFD que tu pilotes (livre virtuel de 10 000 $). On te donne tes trades
fermés de la période et ta courbe d'équité. Objectif : apprendre de tes résultats et
amender ta TACTIQUE.

RÈGLES STRICTES DE LA RÉTRO :
- Tu n'as le droit d'amender QUE la tactique (quels setups/symboles/conditions marchent
  ou non, horizon de détention, filtres d'entrée). Tu ne touches JAMAIS aux limites de
  risque (levier, taille, stop, plancher) — elles sont gravées dans le code.
- INTERDIT de recommander d'augmenter la taille ou le levier après des pertes
  (anti-martingale : le sur-dimensionnement mène à la ruine certaine).
- HUMILITÉ STATISTIQUE : ton échantillon est petit et bruité. Un enseignement ne
  devient une règle ("À APPLIQUER") que si le motif a au moins 5 occurrences.
  En dessous, classe-le "À SURVEILLER". 3 pertes d'affilée ne prouvent rien.
- Reste fidèle à la doctrine de base (momentum, vol-targeting, demi-Kelly, éviter le
  sur-trading, cash = position).

Réponds en deux parties séparées par la ligne exacte `===TACTICS===` :
1) AVANT le séparateur : une rétro en français lisible (ce qui a marché, ce qui n'a
   pas marché, chiffres clés, hypothèses).
2) APRÈS le séparateur : la NOUVELLE version COMPLÈTE de tes leçons tactiques
   (liste puce concise, < 1500 caractères, cumulant les leçons encore valides des
   semaines précédentes qu'on te fournit). C'est ce texte qui sera réinjecté tel quel
   dans ton prompt de trading."""


def _period_trades(trades, since):
    out = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            when = dt.datetime.fromisoformat(str(t.get("ts")))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if when >= since and t.get("type") in ("open", "close"):
            out.append(t)
    return out


def run_retro(repo_dir=HERE, now=None, brain_factory=None):
    """Génère la rétro + met à jour state/doctrine_tactics.md. Best-effort."""
    now = now or dt.datetime.now(dt.timezone.utc)
    data_dir = os.path.join(repo_dir, "data")
    state_dir = os.path.join(repo_dir, "state")
    os.makedirs(state_dir, exist_ok=True)

    trades = tracker._read_jsonl(os.path.join(data_dir, "trades.jsonl"))
    equity_rows = tracker._read_jsonl(os.path.join(data_dir, "equity.jsonl"))
    week = _period_trades(trades, now - dt.timedelta(days=7))
    metrics = tracker.compute_metrics(equity_rows, trades, now=now)

    prev_tactics = ""
    tpath = os.path.join(state_dir, "doctrine_tactics.md")
    if os.path.exists(tpath):
        with open(tpath, encoding="utf-8") as f:
            prev_tactics = f.read()

    if not week:
        # Rien à apprendre cette semaine : on ne réécrit pas les tactiques.
        _write_retro(repo_dir, now, "Aucun trade fermé cette semaine — pas d'amendement "
                     "des tactiques. Le cash reste une position légitime.")
        return "no-trades"

    with open(os.path.join(repo_dir, "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    user_msg = (
        f"Période : 7 derniers jours au {now.date().isoformat()}.\n"
        f"Métriques cumulées : {json.dumps(metrics, ensure_ascii=False, default=str)}\n"
        f"Trades de la semaine (JSON) :\n{json.dumps(week, ensure_ascii=False, default=str)}\n\n"
        f"Tes leçons tactiques ACTUELLES (à réviser, garder ce qui tient, corriger le reste) :\n"
        f"{prev_tactics or '(aucune encore)'}\n\n"
        "Produis la rétro puis, après ===TACTICS===, la version complète mise à jour."
    )

    try:
        if brain_factory is not None:
            client = brain_factory()
        else:
            import anthropic
            client = anthropic.Anthropic(timeout=180.0, max_retries=1)
        resp = client.messages.create(
            model=config.get("model", "claude-sonnet-5"),
            max_tokens=4000, system=RETRO_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as exc:
        _write_retro(repo_dir, now, f"Rétro indisponible (erreur API) : {exc}")
        return "error"

    if "===TACTICS===" in text:
        retro_md, tactics = text.split("===TACTICS===", 1)
    else:
        retro_md, tactics = text, prev_tactics
    _write_retro(repo_dir, now, retro_md.strip())
    tactics = tactics.strip()
    if tactics:
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(tactics)
    return "ok"


def _write_retro(repo_dir, now, body):
    data_dir = os.path.join(repo_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"retro-{now.date().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Rétrospective — semaine du {now.date().isoformat()}\n\n{body}\n")


if __name__ == "__main__":
    print("Rétro:", run_retro())
