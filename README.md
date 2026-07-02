# etoro-agent-bot

Bot de trading autonome pour l'**Agent Portfolio eToro** : un sous-compte
isolé avec un livre **virtuel de 10 000 $**. Toutes les 30 minutes, un cerveau
(Claude + recherche web en direct) propose des trades CFD agressifs ; un
garde-fou **déterministe** (que le LLM ne peut pas contourner) plafonne
levier, montant et stop-loss avant toute exécution.

## Le miroir : 200 € → ~2 %

Vos ~200 € réels répliquent le livre virtuel à ~2 %. Concrètement :
**1 $ de gain/perte sur le book virtuel ≈ 2 centimes réels.** Un trade de
3 000 $ virtuels engage ~60 € réels d'exposition (avec levier). Si le book
virtuel tombe sous 3 500 $ (≈ -65 %), le bot ferme tout et s'arrête
définitivement.

## Garde-fous (config.yaml)

- 3 positions ouvertes max, 30 % du book max par trade, 10 % de cash en réserve
- Levier plafonné par classe d'actif (ESMA) + plafond global x20
- **Stop-loss obligatoire** sur chaque ouverture (40 % de la position par
  défaut, 50 % max) — pas de prix courant = pas de trade
- **Disjoncteur quotidien** : -25 % depuis 00:00 UTC → plus aucune ouverture
  jusqu'au lendemain
- **Plancher dur** : book < 3 500 $ → tout est fermé + halte permanente
  (fichier `state/halt.json` ; seul son effacement manuel relance le bot)

## Installation en 5 étapes

1. **Créer un dépôt GitHub privé** et pousser ce dossier
   (`git remote add origin … && git push -u origin main`).
2. **Ajouter 3 secrets** (Settings → Secrets and variables → Actions →
   Secrets) : `ETORO_PUBLIC_KEY`, `ETORO_PRIVATE_KEY`, `ANTHROPIC_API_KEY`.
3. **Ajouter la variable** `DRY_RUN` = `true` (Settings → … → Variables).
4. **Observer les premiers runs à blanc** dans l'onglet Actions (workflow
   `trade`, toutes les 30 min ou lancement manuel). En dry-run, le bot lit le
   portefeuille et logge ce qu'il *aurait* fait, sans rien exécuter.
5. Quand les décisions vous conviennent : **passer `DRY_RUN` à `false`**.

En local : `cp .env.example .env`, remplir les clés, puis `python main.py`.

## Tout arrêter

- **Immédiat** : onglet Actions → workflow `trade` → « Disable workflow ».
- **Définitif** : supprimer l'Agent Portfolio dans eToro (les clés API
  deviennent inertes).
- Le bot s'arrête aussi tout seul si le plancher de 3 500 $ est franchi.

## Logs et état

- `logs/trades.jsonl` : une ligne JSON par décision/action + résumé de cycle.
  Sur GitHub, téléchargeable en artefact de chaque run (rétention 7 jours).
- `state/` : snapshot quotidien, verrou, drapeau de halte. Persisté entre les
  runs via `actions/cache`. **Caveat** : ce cache est à cohérence
  différée (« eventually consistent ») — dans de rares cas un run peut
  repartir d'un état légèrement ancien (snapshot du jour recréé, verrou
  perdu). Les garde-fous critiques (plancher, SL) sont recalculés à chaque
  cycle à partir de l'API eToro et ne dépendent donc pas du cache.

## Lancer les tests

```bash
python tests/test_risk_gate.py        # ou: python -m pytest tests/
```

## Avertissement honnête

Ce bot applique une stratégie **volontairement agressive et hautement
spéculative** (CFD à fort levier, paris concentrés, décisions par IA).
L'espérance de gain n'est pas démontrée ; la **perte totale** de la mise
répliquée est un scénario réaliste et accepté. Les garde-fous ralentissent la
perte, ils ne l'empêchent pas. N'augmentez jamais le montant miroir au-delà
de ce que vous acceptez de perdre intégralement.
