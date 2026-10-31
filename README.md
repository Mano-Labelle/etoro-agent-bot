# etoro-agent-bot

**Expérience arrêtée le 14/08/2026.** Code seul : aucune clé n'est passée par ce dépôt, les données de trading ont été retirées.

Bot de trading autonome pour l'**Agent Portfolio eToro** : un sous-compte
isolé avec un livre **virtuel de 10 000 $**. Toutes les 2 heures, un cerveau
(OpenAI `gpt-5.4-mini` + recherche web native) propose des trades CFD agressifs ;
un
garde-fou **déterministe** (que le LLM ne peut pas contourner) plafonne
levier, montant et stop-loss avant toute exécution.

## Doctrine du cerveau (fondée sur la recherche)

Le cerveau ne trade pas au feeling : son prompt encode une **doctrine distillée
d'une revue de littérature vérifiée** (`brain.py` → `DOCTRINE`) — momentum pour
la direction, taille inverse à la volatilité + demi-Kelly, filtre de régime
anti-crash (pas de shorts pendant un rebond), anti-martingale strict. Les règles
techniques naïves (croisements de moyennes mobiles) sont explicitement écartées.

## Ancrage sur données réelles (`marketdata.py`)

Le momentum et la volatilité ne sont pas *lus dans des articles* (approximatif,
hallucinable) mais **calculés sur de vraies séries de prix** (Yahoo Finance, EOD,
sans clé). Chaque cycle, pour une **watchlist** couvrant toutes les classes
(indices, FX, or/argent, crypto, actions liquides — éditable en tête de
`marketdata.py`), le bot calcule : momentum 1 sem/1 mois/3 mois, volatilité
journalière, drawdown depuis le plus-haut, écart à la MM20. Cette table est
injectée dans le contexte du cerveau, qui l'utilise comme **vérité** pour la
DIRECTION (signe du momentum) et la TAILLE (inverse de la volatilité) ; la
recherche web sert alors uniquement au **pourquoi** (catalyseur). Fetch
best-effort (jamais bloquant) et mis en cache une fois par jour UTC dans `state/`.

## Boucle de self-learning

- **Mémoire courte** (chaque cycle) : le cerveau reçoit ses derniers trades
  fermés (thèse d'entrée vs résultat) et apprend in-context.
- **Rétrospective hebdomadaire** (`retro.py`, workflow `retro`, dimanche 20:00
  UTC) : le bot analyse sa semaine, écrit `data/retro-AAAA-MM-JJ.md` (versionné,
  lisible) et amende son playbook **tactique** `state/doctrine_tactics.md`,
  réinjecté dans son prompt aux cycles suivants.
- **Asymétrie de sécurité** : le self-learning ne touche QUE la tactique. La
  **cage de risque** (leviers, tailles, stop, plancher, disjoncteur) vit dans
  `config.yaml`/`risk_gate.py`, hors de portée du LLM. La rétro a interdiction
  explicite de conclure « augmenter la taille après une perte ».

## Garde-fous (config.yaml)

- 3 positions ouvertes max, 30 % du book max par trade, 10 % de cash en réserve
- Levier plafonné par classe d'actif (ESMA) + plafond global x20
- **Stop-loss obligatoire** sur chaque ouverture (40 % de la position par
  défaut, 50 % max) — pas de prix courant = pas de trade
- **Disjoncteur quotidien** : -25 % depuis 00:00 UTC → plus aucune ouverture
  jusqu'au lendemain
- **Anti-churn** : 6 ouvertures max par jour UTC, détention minimale de 2 h
  avant de refermer une position ouverte par le bot ou de rouvrir le même
  symbole (le spread ne doit pas manger le book)
- **Plancher dur** : book < 3 500 $ **confirmé sur 2 cycles consécutifs**
  (une lecture d'API transitoirement fausse ne liquide jamais) → tout est
  fermé + halte permanente (fichier `state/halt.json` ; seul son effacement
  manuel relance le bot). En halte, le bot continue chaque cycle de tenter de
  fermer les positions restantes — il n'ouvre plus jamais rien.
- **Écritures jamais rejouées à l'aveugle** : un timeout ou un 5xx sur un
  ordre lève une erreur « ambiguë » (l'ordre a peut-être été exécuté) ; le
  cycle suivant réconcilie via `/pnl` au lieu de risquer un ordre dupliqué.

## Installation en 5 étapes

1. **Créer un dépôt GitHub privé** et pousser ce dossier
   (`git remote add origin … && git push -u origin main`).
2. **Ajouter 3 secrets** (Settings → Secrets and variables → Actions →
   Secrets) : `ETORO_PUBLIC_KEY`, `ETORO_PRIVATE_KEY`, `OPENAI_API_KEY`.
3. **Ajouter la variable** `DRY_RUN` = `true` (Settings → … → Variables).
4. **Observer les premiers runs à blanc** dans l'onglet Actions (workflow
   `trade`, toutes les 2 h ou lancement manuel). En dry-run, le bot lit le
   portefeuille et logge ce qu'il *aurait* fait, sans rien exécuter.
5. Quand les décisions vous conviennent : **passer `DRY_RUN` à `false`**.

En local : `cp .env.example .env`, remplir les clés, puis `python main.py`.

## Tout arrêter

- **Immédiat** : onglet Actions → workflow `trade` → « Disable workflow ».
- **Définitif** : supprimer l'Agent Portfolio dans eToro (les clés API
  deviennent inertes).
- Le bot s'arrête aussi tout seul si le plancher de 3 500 $ est franchi.

## 💶 Coût du cerveau

Le cerveau tourne sur **OpenAI `gpt-5.4-mini`** via la Responses API, recherche
web hébergée incluse. À la cadence de 2 h (12 cycles/jour, ≤3 recherches/cycle) :
recherche web ~1 080 appels/mois × 0,01 $ ≈ **11 $**, tokens négligeables
(quelques euros) → **~15 €/mois** au total. `gpt-5.4-nano` (config `model`)
divise encore les tokens ; espacer la cadence (`cron`) réduit tout
proportionnellement.

## Lancer les tests

```bash
python3 tests/run_all.py              # toute la suite (aucun réseau)
python3 tests/test_risk_gate.py       # ou un fichier seul / python -m pytest tests/
```

## Avertissement honnête

Ce bot applique une stratégie **volontairement agressive et hautement
spéculative** (CFD à fort levier, paris concentrés, décisions par IA).
L'espérance de gain n'est pas démontrée ; la **perte totale** de la mise
répliquée est un scénario réaliste et accepté. Les garde-fous ralentissent la
perte, ils ne l'empêchent pas. N'augmentez jamais le montant miroir au-delà
de ce que vous acceptez de perdre intégralement.
