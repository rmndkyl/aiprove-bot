"""Forecast bounties — Brier-score calls and absolute-error point forecasts.

Every number here comes from a public price feed and a model that was validated
on held-out data before it predicted anything. No hand-typed probabilities.

Datasets
  BTC/ETH  Binance klines (daily, up to 1000 bars) — public, no key
  SPY/NVDA/GLD  Yahoo chart API (daily) — public, no key

Models
  direction (Brier)  logistic regression on 5 price features, fitted on the first
                     70% of history, scored on the last 30%; shrunk toward the
                     base rate unless it actually beat the base rate out of sample.
  level ($ error)    random walk (last close) as the median forecast, plus a
                     half-shrunk drift term for multi-day horizons. A 1-day point
                     forecast you cannot beat by forecasting the current price.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request

import numpy as np

_UA = {"user-agent": "Mozilla/5.0 (compatible; aiprove-agent/1.0)"}


# ---------------------------------------------------------------- feeds
def _get_json(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return json.loads(r.read().decode())


def binance_daily(symbol: str, limit: int = 1000):
    """[(open_time_ms, close), ...] daily UTC bars."""
    rows = _get_json(
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={limit}")
    return [(int(r[0]), float(r[4])) for r in rows]


def yahoo_daily(symbol: str, rng: str = "2y"):
    d = _get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d")
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = [(int(t) * 1000, float(c)) for t, c in zip(ts, closes) if c is not None]
    meta = res.get("meta", {})
    return out, meta


# ---------------------------------------------------------------- features
def _features(closes: np.ndarray):
    """log-return features; rows with a NaN are dropped by the caller."""
    lr = np.diff(np.log(closes))
    n = len(closes)
    X, y, idx = [], [], []
    for i in range(21, n - 1):          # need 20 bars of history, and a next bar
        r1 = lr[i - 1]
        r5 = math.log(closes[i] / closes[i - 5])
        r20 = math.log(closes[i] / closes[i - 20])
        sma20 = closes[i - 19:i + 1].mean()
        vol20 = lr[i - 20:i].std()
        X.append([r1, r5, r20, (closes[i] - sma20) / sma20, vol20])
        y.append(1.0 if closes[i + 1] > closes[i] else 0.0)
        idx.append(i)
    return np.asarray(X, float), np.asarray(y, float), idx


def _fit_logistic(X, y, l2=1.0, iters=4000, lr=0.35):
    mu, sd = X.mean(0), X.std(0) + 1e-12
    Z = (X - mu) / sd
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    w = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-Z @ w))
        g = Z.T @ (p - y) / len(y) + l2 * np.r_[0.0, w[1:]] / len(y)
        w -= lr * g
    return w, mu, sd


def _predict_logistic(w, mu, sd, X):
    Z = (np.asarray(X, float) - mu) / sd
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    return 1.0 / (1.0 + np.exp(-Z @ w))


def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


# ---------------------------------------------------------------- direction model
def direction_model(closes, horizon_days: int = 1):
    """P(close_h later > close_now) with out-of-sample validation.

    Returns dict with p, base_rate, oos stats and a verdict flag so the caller can
    report honestly whether the model beat the base rate or was shrunk back to it.
    """
    closes = np.asarray(closes, float)
    if horizon_days > 1:
        # build overlapping-horizon labels on the same feature rows
        lr = np.diff(np.log(closes))
        X, y = [], []
        for i in range(21, len(closes) - horizon_days):
            r1 = lr[i - 1]
            r5 = math.log(closes[i] / closes[i - 5])
            r20 = math.log(closes[i] / closes[i - 20])
            sma20 = closes[i - 19:i + 1].mean()
            vol20 = lr[i - 20:i].std()
            X.append([r1, r5, r20, (closes[i] - sma20) / sma20, vol20])
            y.append(1.0 if closes[i + horizon_days] > closes[i] else 0.0)
        X, y = np.asarray(X, float), np.asarray(y, float)
    else:
        X, y, _ = _features(closes)

    if len(y) < 120:
        base = float(y.mean()) if len(y) else 0.5
        return {"p": float(np.clip(base, 0.35, 0.65)), "base_rate": base, "n": int(len(y)),
                "model_used": False, "note": "not enough history"}

    split = int(len(y) * 0.7)
    w, mu, sd = _fit_logistic(X[:split], y[:split])
    p_oos = _predict_logistic(w, mu, sd, X[split:])
    b_model = brier(p_oos, y[split:])
    base_rate = float(y[:split].mean())
    b_base = brier(np.full(len(y) - split, base_rate), y[split:])

    # refit on everything, predict the next (unknown) bar
    w2, mu2, sd2 = _fit_logistic(X, y)
    lr = np.diff(np.log(closes))
    i = len(closes) - 1
    now = np.array([[lr[-1],
                     math.log(closes[i] / closes[i - 5]),
                     math.log(closes[i] / closes[i - 20]),
                     (closes[i] - closes[i - 19:i + 1].mean()) / closes[i - 19:i + 1].mean(),
                     lr[-20:].std()]])
    p_model = float(_predict_logistic(w2, mu2, sd2, now)[0])

    # shrink to base rate unless the model demonstrably beat it out of sample
    if b_model < b_base:
        weight = min(1.0, (b_base - b_model) / max(b_base, 1e-9) * 3.0)
        p = base_rate + (p_model - base_rate) * weight
        used = True
    else:
        p, weight, used = base_rate, 0.0, False

    return {
        "p": float(np.clip(p, 0.03, 0.97)),
        "p_model": p_model, "base_rate": base_rate, "model_used": used, "weight": round(weight, 3),
        "n": int(len(y)), "n_oos": int(len(y) - split),
        "brier_oos_model": round(b_model, 5), "brier_oos_base": round(b_base, 5),
        "acc_oos": round(float(((p_oos > 0.5) == (y[split:] > 0.5)).mean()), 4),
        "horizon_days": horizon_days,
    }


def point_forecast(closes, horizon_days: int = 1, drift_lookback: int = 60):
    """Median point forecast. Random walk + half-shrunk drift for multi-day."""
    closes = np.asarray(closes, float)
    last = float(closes[-1])
    lr = np.diff(np.log(closes))[-drift_lookback:]
    drift = float(lr.mean())
    f = last * math.exp(drift * horizon_days * (0.5 if horizon_days > 1 else 0.0))
    return {"value": round(f, 2), "last_close": round(last, 2),
            "drift_daily_pct": round(drift * 100, 4), "horizon_days": horizon_days}


# ---------------------------------------------------------------- per-bounty plans
def plan_for(slug: str):
    """Map bounty slug -> (kind, feed, horizon, payload_builder)."""
    crypto = {"btc-tomorrow": ("BTCUSDT", 1), "forecast-btc-7d": ("BTCUSDT", 7),
              "forecast-eth-24h": ("ETHUSDT", 1)}
    equity = {"forecast-gold-tomorrow": ("GLD", 1), "spy-close-1dollar": ("SPY", 1),
              "forecast-nvda-3d": ("NVDA", 3)}
    if slug in crypto:
        return "brier-crypto", crypto[slug][0], crypto[slug][1]
    if slug in equity:
        return "level-equity", equity[slug][0], equity[slug][1]
    if slug == "signal-eth-pnl-24h":
        return "direction-eth", "ETHUSDT", 1
    return None


def build(slug: str, bars=None, meta=None):
    plan = plan_for(slug)
    if not plan:
        return None
    kind, sym, h = plan
    if kind == "brier-crypto":
        closes = [c for _, c in (bars if bars is not None else binance_daily(sym))]
        m = direction_model(closes, h)
        ans = "yes" if m["p"] >= 0.5 else "no"
        conf = m["p"] if ans == "yes" else 1 - m["p"]
        return {"slug": slug, "kind": kind, "symbol": sym, "model": m,
                "prediction": {"answer": ans, "confidence": round(conf, 4)},
                "reason": f"P(up)={m['p']:.3f} base={m['base_rate']:.3f} "
                          f"model_used={m['model_used']} brier_oos={m['brier_oos_model']}/{m['brier_oos_base']}"}
    if kind == "direction-eth":
        closes = [c for _, c in (bars if bars is not None else binance_daily(sym))]
        m = direction_model(closes, h)
        side = "long" if m["p"] >= 0.5 else "short"
        return {"slug": slug, "kind": kind, "symbol": sym, "model": m,
                "prediction": {"direction": side, "answer": side, "confidence": round(m["p"], 4)},
                "reason": f"P(up)={m['p']:.3f} -> {side}"}
    # level
    if bars is None:
        bars, meta = yahoo_daily(sym)
    closes = [c for _, c in bars]
    f = point_forecast(closes, h)
    return {"slug": slug, "kind": kind, "symbol": sym, "model": f,
            "prediction": {"value": f["value"], "answer": f["value"]},
            "reason": f"last={f['last_close']} drift={f['drift_daily_pct']}%/d h={h}"}
