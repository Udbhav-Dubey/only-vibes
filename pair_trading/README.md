# ML-Powered Pairs Trading & Statistical Arbitrage

A class ML project that builds a pairs-trading pipeline on Nifty 50 stocks and compares **two models from the course** against a simple rule-based baseline:

| | Model | Course topics used |
|---|---|---|
| Baseline | Hard z-score thresholds (enter at \|z\| > 2, exit at \|z\| < 0.5) | none |
| **Model A** | MLP written from scratch in NumPy; predicts whether a stretched spread will revert and gates entries | Perceptron, backpropagation, gradient descent, confusion matrix |
| **Model B** | Mamdani fuzzy logic controller; z-score + volatility → position size in [-1, +1] | Fuzzy sets, rule base, centroid defuzzification |

Both models share the same first stage: **PCA → K-Means → cointegration test** to choose the pairs.

## Quick start

```bash
pip install yfinance statsmodels scikit-learn matplotlib pandas numpy
python pairs_project.py                  # real data (needs internet)
python pairs_project.py --synthetic      # offline test with generated data
python pairs_project.py --out my_results # custom output folder (default: results/)
```

Runs in about a minute. Python 3.9+ recommended.

## How it works

1. **Data:** daily adjusted closes from Yahoo Finance (`yfinance`) for 40 Nifty 50 stocks, 2015 onward.
2. **Split:** training window up to 2020-12-31; out-of-sample test from 2021-01-01. Pair selection, hedge ratios, feature scaling and MLP training use the training window only.
3. **Pair selection:** PCA (5 components) on standardised returns → K-Means with k chosen by silhouette score → Engle-Granger cointegration test within each cluster (p < 0.05) → half-life filter (2 to 40 days) → top 8 pairs.
4. **Spread:** `S = ln A − β ln B − α`, with β from OLS on the training window; z-score uses a 60-day rolling window.
5. **Model A (MLP):** network 5-16-8-1 (ReLU, sigmoid output, cross-entropy loss), mini-batch gradient descent with momentum, L2 penalty and early stopping. Label: spread reverts (|z| < 0.5) within 10 days. A trade is allowed only if P(revert) > 0.6. Includes a numerical gradient check of the backprop code.
6. **Model B (fuzzy):** 5 z-score sets × 3 volatility sets, 9 rules, min/max inference, centroid defuzzification.
7. **Backtest:** position set at close of day *t* earns the spread change on *t+1*; 10 bps cost per unit of position change; equal-weight portfolio of the selected pairs.

## Output files (in `results/`)

| File | Contents |
|---|---|
| `fig1_pca_kmeans.png` | PCA variance, K-Means elbow/silhouette, stocks in PCA space |
| `fig2_gradient_descent.png` | MLP training curve and learning-rate comparison |
| `fig3_confusion_matrices.png` | MLP confusion matrices at thresholds 0.5 / 0.6 / 0.7 |
| `fig4_fuzzy_system.png` | Membership functions and the fuzzy control curve |
| `fig5_equity_curves.png` | Out-of-sample equity curves of the three strategies |
| `selected_pairs.csv` | Chosen pairs with training and out-of-sample cointegration p-values |
| `confusion_metrics.csv` | TP/FP/FN/TN, accuracy, precision, recall, F1 for Model A |
| `strategy_comparison.csv` | Sharpe, Sortino, drawdown, entries, turnover, costs per strategy |
| `per_pair_sharpe.csv` | Sharpe ratio per pair and strategy |
| `pnl_by_pair_baseline.csv`, `pnl_by_pair_model_a.csv`, `pnl_by_pair_model_b.csv` | Daily P&L per pair |

> Note: an earlier version of the script wrote Model A and Model B P&L to the same file name (`pnl_by_pair_model.csv`), so one overwrote the other. This is fixed; re-run the script to get separate files.

## Results (out-of-sample, Jan 2021 to Nov 2026, 10 bps costs)

| Strategy | Ann. return | Sharpe | Max drawdown | Entries | Turnover |
|---|---|---|---|---|---|
| Baseline (hard z-score) | 2.26% | 0.67 | −6.65% | 216 | 431 |
| Model A: MLP filter | 0.11% | 0.18 | −1.32% | 7 | 14 |
| Model B: Fuzzy sizing | 0.39% | 0.17 | −5.33% | 835 | 1071 |

**Neither model beat the baseline.** The MLP showed no predictive skill (recall under 4%, accuracy no better than always predicting "no reversion") and almost never traded. The fuzzy controller was competitive before costs but lost most of its return to turnover. Only 2 of the 8 selected pairs stayed cointegrated out-of-sample. See the report for the full analysis.

## Configuration

All settings are constants at the top of `pairs_project.py`:

| Constant | Default | Meaning |
|---|---|---|
| `TRAIN_END` | `2020-12-31` | End of training window |
| `Z_WIN` | 60 | Rolling window for the z-score |
| `H` | 10 | MLP label horizon (days) |
| `ENTRY`, `EXIT`, `STOP` | 2.0, 0.5, 4.0 | Baseline thresholds |
| `COST` | 0.001 | Cost per unit of position change (10 bps) |
| `MAX_PAIRS` | 8 | Number of pairs traded |
| `P_THRESH` | 0.60 | MLP probability threshold to allow an entry |
| `Z_CONTEXT` | 1.5 | MLP trained and evaluated only when \|z\| exceeds this |

## Limitations

- One train/test split; pairs are chosen once and never re-selected.
- No multiple-testing correction on the cointegration p-values.
- Uses today's Nifty constituents (survivorship bias).
- Short selling in Indian cash equities is restricted, and futures costs and margins are not modelled.
- The single-layer perceptron is the building block of the MLP but is not tested as a separate model.

## Project files

```
pairs_project.py              main script (everything)
results/                      generated figures and tables
Pairs_Trading_Report.docx     project report
README.md                     this file
```
