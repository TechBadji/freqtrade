# ZeroLag Trading Plan — $1,000 Binance 2026

## Stratégie: ZeroLagTrend

### Paramètres clés
| Param | Valeur | Raison |
|-------|--------|--------|
| Budget | $1,000 USDT | Capital initial |
| Stake/trade | $190 | 5 trades × $190 = $950 (5% de réserve) |
| Max trades ouverts | 5 | Diversification sans sur-exposition |
| Stop loss | -5% | Max perte par trade: ~$9.50 |
| Trailing stop | 2% (offset 3.5%) | Protège les gains sur les runners |
| Timeframe | 1h | Signal fiable, moins de bruit |
| Frais estimés | 0.1% | Binance maker/taker |

### Risque par trade
- Perte max par trade: $190 × 5% = **$9.50**
- Perte simultanée max (5 trades): **$47.50** (4.75% du capital)
- ROI cible par trade: 2-4%

---

## Phase 1 — Setup & Download Data

```bash
# Activer l'environnement
source .venv/bin/activate

# Télécharger 6 mois de données 1h pour top pairs Binance
freqtrade download-data \
  --exchange binance \
  --timeframe 1h 4h \
  --days 365 \
  --pairs BTC/USDT ETH/USDT SOL/USDT BNB/USDT XRP/USDT \
         ADA/USDT AVAX/USDT DOT/USDT MATIC/USDT LINK/USDT \
         UNI/USDT ATOM/USDT LTC/USDT NEAR/USDT FIL/USDT \
         APT/USDT ARB/USDT OP/USDT INJ/USDT SUI/USDT \
  --data-format-ohlcv json
```

---

## Phase 2 — Backtesting

```bash
# Backtest simple (6 mois)
freqtrade backtesting \
  --config user_data/config_paper.json \
  --strategy ZeroLagTrend \
  --timerange 20250901-20260301 \
  --breakdown month \
  --export trades \
  --export-filename user_data/backtest_results/zerolag_v1.0.json

# Analyser les résultats
freqtrade backtesting-analysis \
  --config user_data/config_paper.json \
  --export-filename user_data/backtest_results/zerolag_v1.0.json

# Visualiser (optionnel)
freqtrade plot-dataframe \
  --config user_data/config_paper.json \
  --strategy ZeroLagTrend \
  --pair BTC/USDT \
  --timerange 20260101-20260301
```

### Métriques cibles pour valider la stratégie
| Métrique | Minimum acceptable | Cible |
|----------|-------------------|-------|
| Profit total | > 10% | > 30% |
| Win rate | > 45% | > 55% |
| Profit factor | > 1.2 | > 1.5 |
| Max drawdown | < 20% | < 10% |
| Sharpe ratio | > 0.5 | > 1.0 |
| Trades/mois | > 5 | 10-20 |

---

## Phase 3 — Hyperopt (Optimisation)

```bash
# Optimiser les paramètres d'entrée (buy signals)
freqtrade hyperopt \
  --config user_data/config_paper.json \
  --strategy ZeroLagTrend \
  --hyperopt-loss SharpeHyperOptLoss \
  --spaces buy \
  --epochs 200 \
  --timerange 20250901-20260101 \
  --jobs -1

# Optimiser ROI + stoploss + trailing
freqtrade hyperopt \
  --config user_data/config_paper.json \
  --strategy ZeroLagTrend \
  --hyperopt-loss SharpeHyperOptLoss \
  --spaces roi stoploss trailing \
  --epochs 200 \
  --timerange 20250901-20260101 \
  --jobs -1

# Optimiser les signaux de sortie
freqtrade hyperopt \
  --config user_data/config_paper.json \
  --strategy ZeroLagTrend \
  --hyperopt-loss SharpeHyperOptLoss \
  --spaces sell \
  --epochs 100 \
  --timerange 20250901-20260101 \
  --jobs -1

# Voir les meilleurs résultats hyperopt
freqtrade hyperopt-show --best --print-json
```

### Fonctions de loss à tester
- `SharpeHyperOptLoss` — équilibre rendement/risque (recommandé)
- `CalmarHyperOptLoss` — optimise vs max drawdown (conservative)
- `ProfitDrawDownHyperOptLoss` — maximise profit relatif au drawdown

---

## Phase 4 — Paper Trading (Validation)

```bash
# Lancer le bot en paper trading (local)
freqtrade trade \
  --config user_data/config_paper.json \
  --strategy ZeroLagTrend

# Accéder à l'API (FreqUI)
# http://localhost:8080

# Sur Hetzner (déploiement)
# 1. Copier les fichiers
scp -r user_data/ user@hetzner-ip:/home/user/freqtrade/

# 2. Lancer avec docker-compose
docker-compose up -d
```

### Durée minimum de paper trading: 4 semaines
- Comparer avec les résultats du backtest
- Vérifier que le live ratio (trades réels vs attendus) est > 80%
- Ajuster si nécessaire

---

## Phase 5 — Live Trading

### Checklist avant de passer en live
- [ ] Paper trading > 4 semaines validé
- [ ] Win rate live proche du backtest (±10%)
- [ ] Backtest sur données OOS (out-of-sample) validé
- [ ] Clés API Binance configurées (spot only, withdrawal disabled)
- [ ] Telegram notifications actives
- [ ] Stoploss on exchange activé
- [ ] API Freqtrade protégée par mot de passe fort

```bash
# Passer en live (modifier dry_run: false dans config_live.json)
freqtrade trade \
  --config user_data/config_live.json \
  --strategy ZeroLagTrend
```

---

## Gestion du capital — Règles d'or

### Scale-up progressif
| Mois | Action | Condition |
|------|--------|-----------|
| M1 | Paper trading | Toujours |
| M2 | Live avec $300 (3 trades × $90) | Paper validé |
| M3 | Augmenter à $600 si +5% | Pas de drawdown > 10% |
| M4+ | Full $1,000 | +10% sur $600 |

### Règles de risque
1. **Ne jamais** ajouter plus de capital après une perte > 15%
2. **Retirer** 25% des profits chaque mois
3. **Stopper** le bot si drawdown > 20% et ré-analyser
4. **Surveiller** les frais — avec 0.1% maker/taker: breakeven = 0.2% minimum

### Estimation de rendement (conservateur)
- 10-15 trades/mois × 2% avg profit = 20-30% brut
- Moins frais (10-15%): ~5-15% net/mois
- **Cible annuelle réaliste: 50-100%** (éviter l'optimisme excessif)

---

## Commandes utiles

```bash
# Status du bot
freqtrade status --config user_data/config_paper.json

# Voir les trades ouverts
freqtrade show-trades --config user_data/config_paper.json

# Analyser les performances
freqtrade backtesting-analysis --config user_data/config_paper.json

# Lister les paires disponibles
freqtrade list-pairs --exchange binance --quote USDT

# Valider la stratégie (syntaxe)
freqtrade check-freqaimodels --config user_data/config_paper.json

# Tester la stratégie (sans trade)
freqtrade test-pairlist --config user_data/config_paper.json
```

---

## Architecture sur Hetzner

```
Instance Hetzner (CX21 minimum — 2 CPU, 4GB RAM)
├── bot1/  ← Ton bot existant #1
├── bot2/  ← Ton bot existant #2
└── bot3/  ← ZeroLagTrend (nouveau)
    ├── config_paper.json  (Phase 1-4)
    └── config_live.json   (Phase 5)

Ports:
- bot1: 8080
- bot2: 8081
- bot3: 8082 (ZeroLagTrend)

FreqUI: Nginx reverse proxy → HTTPS
```
