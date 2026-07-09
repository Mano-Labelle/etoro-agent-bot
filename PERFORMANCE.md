# 📈 Expérience eToro — 200 € Bold Bets

_Régénéré automatiquement à chaque cycle par `tracker.py` — ne pas éditer à la main._

- **T0** : 2026-07-02T16:59Z — mise réelle **200 €** (book virtuel de 10,000 $ répliqué à ~2 %)
- **Valeur actuelle** : **199.95 €** (-0.03 %) — _book 9,997 $_
- **Drawdown max** : 0.03 %
- **Jours écoulés** : 7.0
- **Dernier point** : 2026-07-09T16:18:59+00:00

## Courbe d'équité

```
██████████████▄▂▂▃▁
```
min 9,997 $ — max 10,000 $

## Métriques

| Métrique | Valeur |
|---|---|
| Trades (open + close, exécutés ou dry-run) | 2 |
| Fermetures avec PnL connu | 0 |
| Taux de réussite | — |
| Gain moyen | — |
| Perte moyenne | — |
| Ratio gain/perte | — |
| Espérance par trade | — |
| Coûts bruts estimés (proxy spread 0,1 %) | 0.90 $ |

## 10 derniers trades

| Date (UTC) | Type | Symbole | Sens | Montant | Levier | Statut | PnL | Rationale |
|---|---|---|---|---|---|---|---|---|
| 2026-07-09T12:24 | close | TSLA | achat | — | — | executed | — | Flatten TSLA. The trade no longer passes the doctrine's momentum filter: 1W mome |
| 2026-07-09T10:04 | open | TSLA | achat | 900 $ | 1 | executed | — | Opening TSLA only. The momentum filter is valid: mom_1m +0.78% and mom_3m +13.68 |
| 2026-07-09T09:56 | open | TSLA | achat | — | — | rejected | — | TSLA is one of the only names in the provided state with positive 1m and 3m mome |
| 2026-07-09T08:58 | open | AMD | achat | — | — | rejected | — | AMD is the best long-only setup right now: mom_1m +10.94% and mom_3m +133.56% cl |
| 2026-07-09T01:24 | open | AMD | achat | — | — | rejected | — | Long only, leverage 1. AMD is the cleanest momentum/catalyst match in the watchl |

## Objectifs (en euros réels)

- 💀 Plancher dur : **70 €** (_book 3,500 $_) — halte permanente en dessous, confirmée 2 cycles
- 🎯 Objectif x3,5 (P90 realiste) : **700 €** (_book 35,000 $_)
- 🎯 Stretch x5 (queue droite) : **1,000 €** (_book 50,000 $_)
- 🔒 Cliquet : atteindre 300 € verrouille un plancher à 220 €
- 🔒 Cliquet : atteindre 400 € verrouille un plancher à 260 €
- 🔒 Cliquet : atteindre 600 € verrouille un plancher à 400 €

---

Suivi web : `dashboard/index.html` (GitHub Pages) — données brutes : `data/equity.jsonl`, `data/trades.jsonl`. Source temps réel : l'app eToro.
