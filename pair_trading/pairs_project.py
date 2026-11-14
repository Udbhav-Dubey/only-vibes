"""
Pairs Trading & Statistical Arbitrage: comparing two class models
=================================================================
Shared pipeline : PCA -> K-Means -> cointegration -> spread -> z-score
Baseline        : hard z-score rule (enter |z|>2, exit |z|<0.5)
Model A (learned)    : MLP written from scratch (perceptron -> backprop -> gradient descent),
                       predicts P(spread reverts within H days); gates entries.
                       Evaluated with a confusion matrix.
Model B (rule-based) : Fuzzy logic controller, z-score + volatility -> position size in [-1, 1].

Run:   python pairs_project.py                # real data (needs internet, yfinance)
       python pairs_project.py --synthetic    # offline test with fake data
Outputs (figures + CSV tables) go to ./results
"""
import argparse
import os
import warnings
from itertools import combinations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from statsmodels.tsa.stattools import coint

warnings.filterwarnings("ignore")

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
TICKERS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA",
    "TITAN", "ULTRACEMCO", "NESTLEIND", "BAJFINANCE", "WIPRO", "HCLTECH", "TECHM",
    "POWERGRID", "NTPC", "ONGC", "COALINDIA", "TATASTEEL", "JSWSTEEL", "HINDALCO",
    "M&M", "TATAMOTORS", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "DRREDDY", "CIPLA",
    "DIVISLAB", "GRASIM", "BPCL", "INDUSINDBK",
]
TICKERS = [t + ".NS" for t in TICKERS]

START = "2015-01-01"
TRAIN_END = "2020-12-31"   # everything up to here: pair selection, hedge ratio, model training
Z_WIN = 60                 # rolling window for z-score
H = 10                     # label horizon (days) for the MLP
ENTRY, EXIT, STOP = 2.0, 0.5, 4.0
COST = 0.0010              # 10 bps per unit of position change
MAX_PAIRS = 8
P_THRESH = 0.60            # MLP probability threshold for allowing an entry
Z_CONTEXT = 1.5            # MLP is trained/evaluated only when |z| > this (a "stretched" spread)
OUT = "results"


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
def synthetic_prices(seed=1):
    """Fake data: stocks in groups share a common stochastic trend + mean-reverting noise."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", "2024-12-31")
    n = len(idx)
    cols = {}
    for g in range(5):
        factor = np.cumsum(rng.normal(0.0003, 0.012, n))
        for i in range(6):
            ou = np.zeros(n)
            for t in range(1, n):
                ou[t] = 0.93 * ou[t - 1] + rng.normal(0, 0.012)
            cols[f"G{g}S{i}"] = 4 + rng.uniform(0.7, 1.3) * factor + ou
    for i in range(4):  # unrelated random walks
        cols[f"RW{i}"] = 4 + np.cumsum(rng.normal(0.0002, 0.015, n))
    return np.exp(pd.DataFrame(cols, index=idx))


def load_prices(synthetic):
    if synthetic:
        return synthetic_prices()
    import yfinance as yf
    px = yf.download(TICKERS, start=START, auto_adjust=True, progress=False)["Close"]
    px = px.dropna(axis=1, thresh=int(0.95 * len(px))).ffill().dropna(axis=1)
    return px


# ----------------------------------------------------------------------------
# Step 1: PCA + K-Means + cointegration  (pair selection, training window only)
# ----------------------------------------------------------------------------
def half_life(s):
    ds = s.diff().dropna()
    lag = s.shift(1).loc[ds.index]
    lam = sm.OLS(ds, sm.add_constant(lag)).fit().params.iloc[1]
    return -np.log(2) / lam if lam < 0 else np.inf


def build_spread(logpx, a, b):
    """Hedge ratio from OLS on the TRAINING window only."""
    tr = logpx.loc[:TRAIN_END]
    alpha, beta = sm.OLS(tr[a], sm.add_constant(tr[b])).fit().params
    s = logpx[a] - beta * logpx[b] - alpha
    z = (s - s.rolling(Z_WIN).mean()) / s.rolling(Z_WIN).std()
    return s, z, beta


def select_pairs(px, logpx):
    rets = np.log(px).diff().dropna().loc[:TRAIN_END]
    rets = (rets - rets.mean()) / rets.std()
    pca = PCA(n_components=5).fit(rets)
    load = pca.components_.T                      # one row of loadings per stock
    ks = list(range(2, min(11, len(px.columns))))
    inertia, sil = [], []
    for k in ks:
        km = KMeans(k, n_init=10, random_state=0).fit(load)
        inertia.append(km.inertia_)
        sil.append(silhouette_score(load, km.labels_))
    k_best = ks[int(np.argmax(sil))]
    labels = KMeans(k_best, n_init=10, random_state=0).fit_predict(load)

    # --- figure: PCA variance, elbow/silhouette, cluster scatter
    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    ax[0].bar(range(1, 6), pca.explained_variance_ratio_)
    ax[0].set_title("PCA: variance explained"); ax[0].set_xlabel("component")
    ax[1].plot(ks, inertia, "o-", label="inertia (elbow)")
    ax[1].set_xlabel("k"); ax[1].set_title(f"K-Means: chosen k={k_best} (best silhouette)")
    a2 = ax[1].twinx(); a2.plot(ks, sil, "s--", color="tab:red"); a2.set_ylabel("silhouette")
    sc = ax[2].scatter(load[:, 0], load[:, 1], c=labels, cmap="tab10")
    for i, name in enumerate(px.columns):
        ax[2].annotate(name.replace(".NS", ""), (load[i, 0], load[i, 1]), fontsize=6)
    ax[2].set_title("Stocks in PCA space, coloured by cluster")
    plt.tight_layout(); plt.savefig(f"{OUT}/fig1_pca_kmeans.png", dpi=150); plt.close()

    # --- cointegration within clusters
    rows = []
    for c in set(labels):
        members = list(px.columns[labels == c])
        for a_, b_ in combinations(members, 2):
            p = coint(logpx.loc[:TRAIN_END, a_], logpx.loc[:TRAIN_END, b_])[1]
            if p < 0.05:
                s, z, beta = build_spread(logpx, a_, b_)
                hl = half_life(s.loc[:TRAIN_END])
                if 2 <= hl <= 40 and beta > 0:
                    p_oos = coint(logpx.loc[TRAIN_END:, a_], logpx.loc[TRAIN_END:, b_])[1]
                    rows.append(dict(A=a_, B=b_, cluster=c, p_train=p, half_life=hl,
                                     beta=beta, p_test_oos=p_oos))
    df = pd.DataFrame(rows).sort_values("p_train").head(MAX_PAIRS).reset_index(drop=True)
    df.to_csv(f"{OUT}/selected_pairs.csv", index=False)
    print(f"\nPCA top-5 variance: {pca.explained_variance_ratio_.sum():.1%} | k={k_best} | "
          f"pairs selected: {len(df)}")
    print(df.round(4).to_string())
    return df


# ----------------------------------------------------------------------------
# Step 2: features / labels for the MLP
# ----------------------------------------------------------------------------
FEATS = ["z", "dz", "z_lag5", "volratio", "mom5"]


def make_features(s, z):
    ds = s.diff()
    return pd.DataFrame({
        "z": z,
        "dz": z.diff(),
        "z_lag5": z.shift(5),
        "volratio": ds.rolling(20).std() / ds.rolling(120).std(),
        "mom5": (s - s.shift(5)) / s.rolling(Z_WIN).std(),
    })


def make_labels(z):
    """1 if |z| drops below EXIT at some point in the next H days (spread reverts)."""
    a = z.abs().values
    n = len(a)
    y = np.full(n, np.nan)
    for t in range(n - H):
        w = a[t + 1: t + H + 1]
        if not np.isnan(a[t]) and not np.isnan(w).any():
            y[t] = float(w.min() < EXIT)
    return pd.Series(y, index=z.index)


# ----------------------------------------------------------------------------
# Model A: MLP from scratch (perceptron -> backprop -> gradient descent)
# ----------------------------------------------------------------------------
class MLP:
    """Fully-connected net, ReLU hidden layers, sigmoid output, binary cross-entropy."""

    def __init__(self, sizes, seed=0):
        rng = np.random.default_rng(seed)
        self.W = [rng.normal(0, np.sqrt(2 / sizes[i]), (sizes[i], sizes[i + 1]))
                  for i in range(len(sizes) - 1)]
        self.b = [np.zeros(sizes[i + 1]) for i in range(len(sizes) - 1)]

    def forward(self, X):
        self.a = [X]
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            zl = self.a[-1] @ W + b
            if i < len(self.W) - 1:
                self.a.append(np.maximum(zl, 0))                 # ReLU
            else:
                self.a.append(1 / (1 + np.exp(-np.clip(zl, -30, 30))))  # sigmoid
        return self.a[-1].ravel()

    def grads(self, X, y, l2=0.0):
        n = len(y)
        p = self.forward(X)
        delta = ((p - y) / n).reshape(-1, 1)      # dL/dz_out for sigmoid + BCE
        gW, gb = [None] * len(self.W), [None] * len(self.W)
        for i in reversed(range(len(self.W))):
            gW[i] = self.a[i].T @ delta + l2 * self.W[i]
            gb[i] = delta.sum(0)
            if i > 0:
                delta = (delta @ self.W[i].T) * (self.a[i] > 0)   # backprop through ReLU
        return gW, gb

    def loss(self, X, y):
        p = np.clip(self.forward(X), 1e-9, 1 - 1e-9)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))

    def fit(self, Xtr, ytr, Xva, yva, lr=0.05, epochs=300, batch=64, momentum=0.9,
            l2=1e-4, patience=40, seed=0):
        rng = np.random.default_rng(seed)
        vW = [np.zeros_like(w) for w in self.W]
        vb = [np.zeros_like(b) for b in self.b]
        hist = {"train": [], "val": []}
        best, best_w, wait = np.inf, None, 0
        for ep in range(epochs):
            order = rng.permutation(len(ytr))
            for i in range(0, len(order), batch):          # mini-batch gradient descent
                idx = order[i:i + batch]
                gW, gb = self.grads(Xtr[idx], ytr[idx], l2)
                for k in range(len(self.W)):
                    vW[k] = momentum * vW[k] - lr * gW[k]
                    vb[k] = momentum * vb[k] - lr * gb[k]
                    self.W[k] += vW[k]
                    self.b[k] += vb[k]
            hist["train"].append(self.loss(Xtr, ytr))
            hist["val"].append(self.loss(Xva, yva))
            if hist["val"][-1] < best - 1e-5:               # early stopping
                best, wait = hist["val"][-1], 0
                best_w = ([w.copy() for w in self.W], [b.copy() for b in self.b])
            else:
                wait += 1
                if wait >= patience:
                    break
        if best_w:
            self.W, self.b = best_w
        return hist

    def predict_proba(self, X):
        return self.forward(X)


def grad_check(seed=0):
    """Compare backprop gradients with numerical gradients (proves backprop is correct)."""
    rng = np.random.default_rng(seed)
    X, y = rng.normal(size=(20, 5)), rng.integers(0, 2, 20).astype(float)
    net = MLP([5, 6, 4, 1], seed=1)
    gW, _ = net.grads(X, y)
    eps, worst = 1e-5, 0
    for k in range(len(net.W)):
        for _ in range(5):
            i, j = rng.integers(net.W[k].shape[0]), rng.integers(net.W[k].shape[1])
            old = net.W[k][i, j]
            net.W[k][i, j] = old + eps; lp = net.loss(X, y)
            net.W[k][i, j] = old - eps; lm = net.loss(X, y)
            net.W[k][i, j] = old
            num = (lp - lm) / (2 * eps)
            worst = max(worst, abs(num - gW[k][i, j]) / (abs(num) + abs(gW[k][i, j]) + 1e-12))
    print(f"Gradient check: worst relative error = {worst:.2e}")
    return worst


# ----------------------------------------------------------------------------
# Model B: Fuzzy logic controller (Mamdani, centroid defuzzification)
# ----------------------------------------------------------------------------
def trimf(x, a, b, c):
    return np.maximum(np.minimum((x - a) / (b - a + 1e-12), (c - x) / (c - b + 1e-12)), 0)


def trapmf(x, a, b, c, d):
    return np.maximum(np.minimum(np.minimum((x - a) / (b - a + 1e-12), 1), (d - x) / (d - c + 1e-12)), 0)


U = np.linspace(-1, 1, 201)                      # output universe: position size
OUT_MF = {
    "SS": trimf(U, -1.5, -1.0, -0.5),            # strong short spread
    "S":  trimf(U, -1.0, -0.5, 0.0),
    "F":  trimf(U, -0.5, 0.0, 0.5),              # flat
    "L":  trimf(U, 0.0, 0.5, 1.0),
    "SL": trimf(U, 0.5, 1.0, 1.5),               # strong long spread
}


def z_memberships(z):
    return {
        "VN": trapmf(z, -5.0, -4.0, -2.5, -1.5),  # very negative (zero beyond -5 => natural stop-loss)
        "N":  trimf(z, -2.5, -1.5, -0.3),
        "Z":  trimf(z, -1.0, 0.0, 1.0),
        "P":  trimf(z, 0.3, 1.5, 2.5),
        "VP": trapmf(z, 1.5, 2.5, 4.0, 5.0),
    }


def v_memberships(v):
    return {
        "Low":  trapmf(v, -1.0, 0.0, 0.8, 1.05),
        "Med":  trimf(v, 0.85, 1.1, 1.5),
        "High": trapmf(v, 1.2, 1.6, 10.0, 11.0),
    }


def fuzzy_position(z, volratio):
    """Rules:
       VN & (Low|Med) -> StrongLong   VN & High -> Long    N & (Low|Med) -> Long   N & High -> Flat
       Z -> Flat
       VP & (Low|Med) -> StrongShort  VP & High -> Short   P & (Low|Med) -> Short  P & High -> Flat
       (High spread volatility = relationship may be breaking, so we shrink the position)."""
    z = np.asarray(z, float)
    v = np.nan_to_num(np.asarray(volratio, float), nan=1.0)
    zm, vm = z_memberships(z), v_memberships(v)
    calm = np.maximum(vm["Low"], vm["Med"])
    high = vm["High"]
    fire = {
        "SL": np.minimum(zm["VN"], calm),
        "L":  np.maximum.reduce([np.minimum(zm["VN"], high), np.minimum(zm["N"], calm)]),
        "F":  np.maximum.reduce([zm["Z"], np.minimum(zm["N"], high), np.minimum(zm["P"], high)]),
        "S":  np.minimum(zm["P"], calm),
        "SS": np.minimum(zm["VP"], calm),
    }
    fire["S"] = np.maximum(fire["S"], np.minimum(zm["VP"], high))
    # Mamdani: min implication, max aggregation, centroid defuzzification
    agg = np.zeros((len(z), len(U)))
    for k, mf in OUT_MF.items():
        agg = np.maximum(agg, np.minimum(fire[k][:, None], mf[None, :]))
    area = agg.sum(1)
    pos = np.where(area > 1e-9, (agg * U).sum(1) / np.maximum(area, 1e-9), 0.0)
    pos[np.isnan(z)] = 0.0
    return pos


# ----------------------------------------------------------------------------
# Strategies, backtest, metrics
# ----------------------------------------------------------------------------
def hard_positions(z, allow=None):
    zv, pos, cur = z.values, np.zeros(len(z)), 0
    for t, zt in enumerate(zv):
        if np.isnan(zt):
            cur = 0
        elif cur == 0:
            if (allow is None or allow[t]) and ENTRY < abs(zt) < STOP:
                cur = -1 if zt > 0 else 1
        elif abs(zt) < EXIT or abs(zt) > STOP:
            cur = 0
        pos[t] = cur
    return pd.Series(pos, index=z.index)


def backtest(pos, s, beta):
    """pos[t] decided at close t earns the spread change on t+1. Costs on every position change."""
    gross = 1 + abs(beta)
    pnl = pos.shift(1) * s.diff() / gross
    cost = COST * pos.diff().abs()
    return (pnl - cost).fillna(0.0), cost.fillna(0.0)


def metrics(port, pos_df, cost_df):
    r = port
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    ann_ret, ann_vol = r.mean() * 252, r.std() * np.sqrt(252)
    down = r[r < 0].std() * np.sqrt(252)
    entries = int(sum(((p.abs() > 0.05) & (p.shift(1).abs() <= 0.05)).sum() for _, p in pos_df.items()))
    turnover = float(pos_df.diff().abs().sum().sum())
    return {
        "Ann. return %": 100 * ann_ret,
        "Ann. vol %": 100 * ann_vol,
        "Sharpe": ann_ret / ann_vol if ann_vol > 0 else np.nan,
        "Sortino": ann_ret / down if down and down > 0 else np.nan,
        "Max drawdown %": 100 * dd,
        "Entries": entries,
        "Turnover": turnover,
        "Total cost %": 100 * cost_df.mean(axis=1).sum(),
    }


def confusion(y, p, thr):
    yh = (p > thr).astype(int)
    tp = int(((yh == 1) & (y == 1)).sum()); fp = int(((yh == 1) & (y == 0)).sum())
    fn = int(((yh == 0) & (y == 1)).sum()); tn = int(((yh == 0) & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else np.nan
    rec = tp / (tp + fn) if tp + fn else np.nan
    f1 = 2 * prec * rec / (prec + rec) if prec == prec and rec == rec and prec + rec > 0 else np.nan
    return dict(thr=thr, TP=tp, FP=fp, FN=fn, TN=tn, accuracy=(tp + tn) / len(y),
                precision=prec, recall=rec, f1=f1)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    OUT = args.out
    os.makedirs(OUT, exist_ok=True)

    px = load_prices(args.synthetic)
    logpx = np.log(px)
    print(f"Data: {px.shape[1]} stocks, {px.index[0].date()} -> {px.index[-1].date()}")

    pairs = select_pairs(px, logpx)
    if pairs.empty:
        print("No cointegrated pairs found. Try more tickers or a looser filter.")
        return

    # ---- build per-pair series
    P = {}
    for _, r in pairs.iterrows():
        s, z, beta = build_spread(logpx, r.A, r.B)
        f = make_features(s, z)
        y = make_labels(z)
        P[(r.A, r.B)] = dict(s=s, z=z, beta=beta, f=f, y=y)

    # ---- training set (pooled across pairs, training window, stretched spreads only)
    tr_dates = px.loc[:TRAIN_END].index
    cut = tr_dates[-H - 1]                                  # labels must not peek past TRAIN_END
    Xs, ys = [], []
    for d in P.values():
        m = (d["f"].index <= cut) & (d["z"].abs() > Z_CONTEXT) & d["f"].notna().all(axis=1) & d["y"].notna()
        Xs.append(d["f"][m][FEATS].values); ys.append(d["y"][m].values)
    X, y = np.vstack(Xs), np.concatenate(ys)
    n_val = int(0.2 * len(y))                              # chronological-ish split (pairs pooled, last 20% rows)
    mu, sd = X[:-n_val].mean(0), X[:-n_val].std(0) + 1e-9
    Xn = (X - mu) / sd
    Xtr, ytr, Xva, yva = Xn[:-n_val], y[:-n_val], Xn[-n_val:], y[-n_val:]
    print(f"\nMLP training rows: {len(ytr)} train / {len(yva)} val | base rate of reversion: {y.mean():.2%}")

    grad_check()

    # ---- gradient descent experiment: different learning rates
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    for lr in [0.5, 0.05, 0.005]:
        h = MLP([len(FEATS), 16, 8, 1], seed=0).fit(Xtr, ytr, Xva, yva, lr=lr, epochs=150, patience=150)
        ax[1].plot(h["val"], label=f"lr={lr}")
    ax[1].set_title("Gradient descent: validation loss vs learning rate")
    ax[1].set_xlabel("epoch"); ax[1].set_ylabel("BCE loss"); ax[1].legend()

    net = MLP([len(FEATS), 16, 8, 1], seed=0)
    hist = net.fit(Xtr, ytr, Xva, yva, lr=0.01, epochs=300, patience=40)
    ax[0].plot(hist["train"], label="train"); ax[0].plot(hist["val"], label="validation")
    ax[0].set_title("Final MLP training curve (early stopping)"); ax[0].set_xlabel("epoch")
    ax[0].set_ylabel("BCE loss"); ax[0].legend()
    plt.tight_layout(); plt.savefig(f"{OUT}/fig2_gradient_descent.png", dpi=150); plt.close()
    print(f"MLP trained for {len(hist['train'])} epochs, best val loss {min(hist['val']):.4f}")

    # ---- predict probabilities for every day of every pair
    for d in P.values():
        ok = d["f"].notna().all(axis=1).values
        prob = np.full(len(d["f"]), np.nan)
        prob[ok] = net.predict_proba((d["f"][FEATS].values[ok] - mu) / sd)
        d["prob"] = pd.Series(prob, index=d["f"].index)

    # ---- confusion matrix on TEST period (stretched spreads only)
    te_p, te_y = [], []
    for d in P.values():
        m = (d["f"].index > TRAIN_END) & (d["z"].abs() > Z_CONTEXT) & d["prob"].notna() & d["y"].notna()
        te_p.append(d["prob"][m].values); te_y.append(d["y"][m].values)
    te_p, te_y = np.concatenate(te_p), np.concatenate(te_y)
    cm_rows = [confusion(te_y, te_p, t) for t in (0.5, 0.6, 0.7)]
    cm = pd.DataFrame(cm_rows)
    cm.to_csv(f"{OUT}/confusion_metrics.csv", index=False)
    print(f"\nTest rows: {len(te_y)} | base rate of reversion: {te_y.mean():.2%}")
    print(cm.round(3).to_string(index=False))

    fig, ax = plt.subplots(1, 3, figsize=(12, 3.6))
    for a, row in zip(ax, cm_rows):
        mat = np.array([[row["TN"], row["FP"]], [row["FN"], row["TP"]]])
        a.imshow(mat, cmap="Blues")
        for i in range(2):
            for j in range(2):
                a.text(j, i, mat[i, j], ha="center", va="center", fontsize=12)
        a.set_xticks([0, 1], ["pred 0", "pred 1"]); a.set_yticks([0, 1], ["true 0", "true 1"])
        a.set_title(f"thr={row['thr']}  prec={row['precision']:.2f}  rec={row['recall']:.2f}")
    plt.tight_layout(); plt.savefig(f"{OUT}/fig3_confusion_matrices.png", dpi=150); plt.close()

    # ---- fuzzy figures
    zz = np.linspace(-5, 5, 500)
    fig, ax = plt.subplots(1, 4, figsize=(18, 3.6))
    for k, v in z_memberships(zz).items():
        ax[0].plot(zz, v, label=k)
    ax[0].set_title("Input 1: z-score"); ax[0].legend()
    vv = np.linspace(0, 3, 300)
    for k, v in v_memberships(vv).items():
        ax[1].plot(vv, v, label=k)
    ax[1].set_title("Input 2: spread volatility ratio"); ax[1].legend()
    for k, v in OUT_MF.items():
        ax[2].plot(U, v, label=k)
    ax[2].set_title("Output: position size"); ax[2].legend()
    ax[3].plot(zz, fuzzy_position(zz, np.full_like(zz, 1.0)), label="vol normal")
    ax[3].plot(zz, fuzzy_position(zz, np.full_like(zz, 1.8)), label="vol high")
    z_h = np.where(np.abs(zz) > ENTRY, -np.sign(zz), 0)
    ax[3].plot(zz, z_h, "k:", label="hard rule (entry side)")
    ax[3].set_title("Control curve: z -> position"); ax[3].set_xlabel("z"); ax[3].legend()
    plt.tight_layout(); plt.savefig(f"{OUT}/fig4_fuzzy_system.png", dpi=150); plt.close()

    # ---- backtest on the TEST period
    strategies = {"Baseline (hard z-score)": {}, "Model A: MLP filter": {}, "Model B: Fuzzy sizing": {}}
    pnl = {k: {} for k in strategies}; cost = {k: {} for k in strategies}; pos = {k: {} for k in strategies}
    for (a, b), d in P.items():
        name = f"{a.replace('.NS','')}/{b.replace('.NS','')}"
        allow = (d["prob"] > P_THRESH).fillna(False).values
        sets = {
            "Baseline (hard z-score)": hard_positions(d["z"]),
            "Model A: MLP filter": hard_positions(d["z"], allow=allow),
            "Model B: Fuzzy sizing": pd.Series(fuzzy_position(d["z"].values, d["f"]["volratio"].values),
                                               index=d["z"].index),
        }
        for k, p in sets.items():
            r, c = backtest(p, d["s"], d["beta"])
            pnl[k][name], cost[k][name], pos[k][name] = r, c, p

    rows, curves = {}, {}
    for k in strategies:
        pnl_df = pd.DataFrame(pnl[k]).loc[TRAIN_END:].iloc[1:]
        cost_df = pd.DataFrame(cost[k]).loc[TRAIN_END:].iloc[1:]
        pos_df = pd.DataFrame(pos[k]).loc[TRAIN_END:].iloc[1:]
        port = pnl_df.mean(axis=1)                       # equal-weight portfolio of pairs
        rows[k] = metrics(port, pos_df, cost_df)
        curves[k] = (1 + port).cumprod()
        tag = "baseline" if k.startswith("Baseline") else ("model_a" if "Model A" in k else "model_b")
        pnl_df.to_csv(f"{OUT}/pnl_by_pair_{tag}.csv")
    res = pd.DataFrame(rows).T
    res.round(3).to_csv(f"{OUT}/strategy_comparison.csv")
    print("\n=== Out-of-sample strategy comparison (test period, 10 bps costs) ===")
    print(res.round(3).to_string())

    plt.figure(figsize=(10, 5))
    for k, c in curves.items():
        plt.plot(c, label=k)
    plt.title("Out-of-sample equity curves (equal-weight portfolio of selected pairs)")
    plt.ylabel("growth of 1"); plt.legend(); plt.tight_layout()
    plt.savefig(f"{OUT}/fig5_equity_curves.png", dpi=150); plt.close()

    # per-pair sharpe table
    sh = {}
    for k in strategies:
        df = pd.DataFrame(pnl[k]).loc[TRAIN_END:].iloc[1:]
        sh[k] = df.mean() / df.std() * np.sqrt(252)
    pd.DataFrame(sh).round(2).to_csv(f"{OUT}/per_pair_sharpe.csv")
    print("\nPer-pair Sharpe:\n", pd.DataFrame(sh).round(2).to_string())
    print(f"\nDone. Figures and tables saved in {OUT}/")


if __name__ == "__main__":
    main()
