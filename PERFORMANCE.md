# 📈 Expérience eToro — 200 € Bold Bets

_Régénéré automatiquement à chaque cycle par `tracker.py` — ne pas éditer à la main._

- **T0** : 2026-07-02T16:59Z — mise réelle **200 €** (book virtuel de 10,000 $ répliqué à ~2 %)
- **Valeur actuelle** : **195.18 €** (-2.41 %) — _book 9,759 $_
- **Drawdown max** : 2.74 %
- **Jours écoulés** : 18.2
- **Dernier point** : 2026-07-20T20:57:05+00:00

## Courbe d'équité

```
▇▇▇▇▇▇▇▇▆▆▇█▇▇▇▇▇▇▇▇▆▅▅▅▆▇▇▇▄▅▄▄▃▃▂▂▂▂▁▁▁▁▁▁▁▁▁▁
```
min 9,759 $ — max 10,034 $

## Métriques

| Métrique | Valeur |
|---|---|
| Trades (open + close, exécutés ou dry-run) | 38 |
| Fermetures avec PnL connu | 0 |
| Taux de réussite | — |
| Gain moyen | — |
| Perte moyenne | — |
| Ratio gain/perte | — |
| Espérance par trade | — |
| Coûts bruts estimés (proxy spread 0,1 %) | 18.00 $ |

## 10 derniers trades

| Date (UTC) | Type | Symbole | Sens | Montant | Levier | Statut | PnL | Rationale |
|---|---|---|---|---|---|---|---|---|
| 2026-07-20T01:18 | close | ETH | achat | — | — | executed | — | Close ETH. The state feed still shows weak multi-horizon alignment for a long-on |
| 2026-07-19T22:37 | open | ETH | achat | 1500 $ | 1 | executed | — | Open a small ETH starter, not a full-sized swing. In the state feed, ETH has the |
| 2026-07-18T22:35 | close | BTC | achat | — | — | executed | — | Flatten the residual BTC starter. The state feed still shows positive 1W/1M mome |
| 2026-07-18T20:35 | close | BTC | vente | — | — | rejected | — | BTC's 1W/1M momentum is positive, but its 3M momentum is still deeply negative ( |
| 2026-07-18T18:41 | open | BTC | achat | 600 $ | 1 | executed | — | BTC is the best available long-only starter, but only at small size: the state f |
| 2026-07-18T04:35 | close | ETH | achat | — | — | executed | — | ETH is the only live position, but it does not clear the book's strict multi-hor |
| 2026-07-17T22:36 | open | ETH | achat | 400 $ | 1 | executed | — | ETH has the cleanest near-term setup in the watchlist: the state feed shows +4.2 |
| 2026-07-17T04:50 | close | AMD | achat | — | — | executed | — | This long also fails the momentum screen because both 1W and 1M momentum are neg |
| 2026-07-17T04:50 | close | NVDA | achat | — | — | executed | — | This long no longer clears the book's momentum screen because 1M momentum is neg |
| 2026-07-16T22:44 | open | AMD | achat | 900 $ | 1 | executed | — | Starter long on AMD. The state feed shows the best multi-horizon momentum among  |

## Objectifs (en euros réels)

- 💀 Plancher dur : **70 €** (_book 3,500 $_) — halte permanente en dessous, confirmée 2 cycles
- 🎯 Objectif x3,5 (P90 realiste) : **700 €** (_book 35,000 $_)
- 🎯 Stretch x5 (queue droite) : **1,000 €** (_book 50,000 $_)
- 🔒 Cliquet : atteindre 300 € verrouille un plancher à 220 €
- 🔒 Cliquet : atteindre 400 € verrouille un plancher à 260 €
- 🔒 Cliquet : atteindre 600 € verrouille un plancher à 400 €

---

Suivi web : `dashboard/index.html` (GitHub Pages) — données brutes : `data/equity.jsonl`, `data/trades.jsonl`. Source temps réel : l'app eToro.
