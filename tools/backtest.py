#!/usr/bin/env python3
"""
backtest.py
===========
Valida si el MEDIDOR DE SESGO del US100 realmente anticipa el movimiento.

Reconstruye historicamente los factores tecnicos que SI son reproducibles desde
precio (tendencia, momentum, volatilidad VIX, tasas 10Y, dolar DXY), recalcula
el sesgo compuesto con las MISMAS formulas del dashboard, y mide:

  * retorno forward medio del US100 por bucket de sesgo (1/5/10/20 dias)
  * hit rate (acierto direccional del sesgo)
  * correlacion sesgo vs retorno forward
  * curva de equity de una estrategia simple (posicion = sesgo/100) vs buy&hold
  * Sharpe, max drawdown, win rate

NOTA HONESTA: amplitud (breadth) y sectores quedan FUERA del backtest porque
necesitarian historico de los 100 constituyentes; los pesos se renormalizan
entre los 5 factores reproducibles. Es una aproximacion fiel del nucleo tecnico.

Sin dependencias externas (urllib). Genera data/backtest.json.
"""

import sys
import json
import math
import datetime as dt
import urllib.request

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10y&interval=1d"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MEGADATA-Backtest/1.0)"}

# pesos originales del dashboard, renormalizados sin breadth/sectors
RAW_W = {"trend": 0.24, "momentum": 0.16, "volatility": 0.16, "rates": 0.12, "dollar": 0.10}
_S = sum(RAW_W.values())
WEIGHTS = {k: v / _S for k, v in RAW_W.items()}


def clamp(x, lo=-100.0, hi=100.0):
    return max(lo, min(hi, x))


def sma(v, p):
    if len(v) < p:
        return None
    return sum(v[-p:]) / p


def rsi(v, p=14):
    if len(v) < p + 1:
        return None
    g = l = 0.0
    for i in range(-p, 0):
        d = v[i] - v[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    ag, al = g / p, l / p
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def roc(v, p=20):
    if len(v) < p + 1:
        return None
    past = v[-p - 1]
    if not past:
        return None
    return (v[-1] - past) / past * 100.0


def fetch(sym):
    url = CHART.format(sym=sym)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        js = json.loads(r.read().decode("utf-8"))
    res = js["chart"]["result"][0]
    ts = res["timestamp"]
    cl = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        d = dt.datetime.utcfromtimestamp(t).date().isoformat()
        out[d] = c
    return out


# ---- factores (replican analysis.py) ----
def f_trend(closes):
    price = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    score, parts = 0.0, 0
    for s in (s20, s50, s200):
        if s is not None:
            parts += 1
            score += 1 if price > s else -1
    if parts == 0:
        return 0.0
    base = score / parts * 70.0
    if s20 and s50 and s200:
        if s20 > s50 > s200:
            base += 30
        elif s20 < s50 < s200:
            base -= 30
    return clamp(base)


def f_momentum(closes):
    r = rsi(closes, 14)
    mom = roc(closes, 20)
    if r is None:
        return 0.0
    return clamp(0.6 * (r - 50) * 2.0 + 0.4 * clamp((mom or 0) * 6.0))


def f_volatility(vseries):
    if len(vseries) < 2:
        return 0.0
    price = vseries[-1]
    chg = (vseries[-1] - vseries[-2]) / vseries[-2] * 100 if vseries[-2] else 0
    lvl = clamp((20.0 - price) * 8.0)
    return clamp(0.7 * lvl + 0.3 * clamp(-chg * 3.0))


def f_rates(tseries):
    c = roc(tseries, 5)
    if c is None:
        return 0.0
    return clamp(-c * 12.0)


def f_dollar(dseries):
    c = roc(dseries, 5)
    if c is None:
        return 0.0
    return clamp(-c * 25.0)


def composite(ndx_h, vix_h, tnx_h, dxy_h):
    s = (f_trend(ndx_h) * WEIGHTS["trend"]
         + f_momentum(ndx_h) * WEIGHTS["momentum"]
         + f_volatility(vix_h) * WEIGHTS["volatility"]
         + f_rates(tnx_h) * WEIGHTS["rates"]
         + f_dollar(dxy_h) * WEIGHTS["dollar"])
    return clamp(s)


def bucket(score):
    if score >= 55:
        return "Alcista fuerte (>=55)"
    if score >= 22:
        return "Alcista (22..55)"
    if score > -22:
        return "Neutral (-22..22)"
    if score > -55:
        return "Bajista (-55..-22)"
    return "Bajista fuerte (<=-55)"


def pstdev(a):
    n = len(a)
    if n < 2:
        return 0.0
    m = sum(a) / n
    return math.sqrt(sum((x - m) ** 2 for x in a) / n)


def pearson(x, y):
    n = min(len(x), len(y))
    if n < 10:
        return None
    x, y = x[:n], y[:n]
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = sum((v - mx) ** 2 for v in x)
    sy = sum((v - my) ** 2 for v in y)
    if sx == 0 or sy == 0:
        return None
    return sxy / math.sqrt(sx * sy)


def build():
    ndx = fetch("%5ENDX")
    vix = fetch("%5EVIX")
    tnx = fetch("%5ETNX")
    dxy = fetch("DX-Y.NYB")

    dates = sorted(d for d in ndx if d in vix and d in tnx and d in dxy)
    if len(dates) < 300:
        raise RuntimeError(f"historico insuficiente ({len(dates)} dias alineados)")

    ndx_s = [ndx[d] for d in dates]
    vix_s = [vix[d] for d in dates]
    tnx_s = [tnx[d] for d in dates]
    dxy_s = [dxy[d] for d in dates]

    horizons = [1, 5, 10, 20]
    rows = []  # (i, score)
    start = 200  # necesita SMA200
    for i in range(start, len(dates) - max(horizons)):
        sc = composite(ndx_s[:i + 1], vix_s[:i + 1], tnx_s[:i + 1], dxy_s[:i + 1])
        rows.append((i, sc))

    # --- forward returns por bucket ---
    buckets = {}
    scores_for_corr, fwd5_for_corr = [], []
    for i, sc in rows:
        b = buckets.setdefault(bucket(sc), {h: [] for h in horizons})
        for h in horizons:
            fwd = (ndx_s[i + h] - ndx_s[i]) / ndx_s[i] * 100.0
            b[h].append(fwd)
        scores_for_corr.append(sc)
        fwd5_for_corr.append((ndx_s[i + 5] - ndx_s[i]) / ndx_s[i] * 100.0)

    order = ["Bajista fuerte (<=-55)", "Bajista (-55..-22)", "Neutral (-22..22)",
             "Alcista (22..55)", "Alcista fuerte (>=55)"]
    bucket_stats = []
    for name in order:
        if name not in buckets:
            continue
        b = buckets[name]
        n = len(b[5])
        row = {"bucket": name, "n": n}
        for h in horizons:
            arr = b[h]
            avg = sum(arr) / len(arr) if arr else 0
            row[f"avg_fwd_{h}d"] = round(avg, 3)
        # hit rate a 5d: signo del retorno coincide con signo del sesgo
        directional = [r for r in b[5]]
        if "Alcista" in name:
            hit = sum(1 for r in directional if r > 0) / len(directional) * 100
        elif "Bajista" in name:
            hit = sum(1 for r in directional if r < 0) / len(directional) * 100
        else:
            hit = None
        row["hit_rate_5d"] = round(hit, 1) if hit is not None else None
        bucket_stats.append(row)

    corr5 = pearson(scores_for_corr, fwd5_for_corr)

    # --- estrategia: posicion = sesgo/100 (largo/corto escalado), 1 dia de retraso ---
    eq, eq_bh = [1.0], [1.0]
    daily_strat = []
    for k in range(len(rows) - 1):
        i, sc = rows[k]
        pos = max(-1.0, min(1.0, sc / 100.0))
        ret = (ndx_s[i + 1] - ndx_s[i]) / ndx_s[i]
        pnl = pos * ret
        daily_strat.append(pnl)
        eq.append(eq[-1] * (1 + pnl))
        eq_bh.append(eq_bh[-1] * (1 + ret))

    def maxdd(curve):
        peak, dd = curve[0], 0.0
        for v in curve:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1)
        return dd * 100

    yrs = len(daily_strat) / 252.0
    strat_total = (eq[-1] - 1) * 100
    bh_total = (eq_bh[-1] - 1) * 100
    sharpe = (sum(daily_strat) / len(daily_strat)) / (pstdev(daily_strat) or 1e-9) * math.sqrt(252)
    winrate = sum(1 for p in daily_strat if p > 0) / len(daily_strat) * 100

    # --- veredicto ---
    edge = []
    if corr5 is not None and corr5 > 0.05:
        edge.append(f"correlacion sesgo/retorno-5d positiva ({corr5:.2f})")
    bull = next((r for r in bucket_stats if r["bucket"].startswith("Alcista (")), None)
    bear = next((r for r in bucket_stats if r["bucket"].startswith("Bajista (")), None)
    monotonic = False
    avgs = [r.get("avg_fwd_5d", 0) for r in bucket_stats]
    if len(avgs) >= 3 and all(avgs[i] <= avgs[i + 1] for i in range(len(avgs) - 1)):
        monotonic = True
        edge.append("los retornos forward suben de bajista->alcista (monotonia)")
    if strat_total > bh_total:
        edge.append("la estrategia supera a buy&hold en retorno total")

    if len(edge) >= 2:
        verdict = "El medidor MUESTRA ventaja estadistica en este periodo: " + "; ".join(edge) + "."
    elif edge:
        verdict = "Ventaja DEBIL/parcial: " + "; ".join(edge) + ". Usar con prudencia."
    else:
        verdict = ("El medidor NO muestra ventaja clara en este periodo: tratalo como "
                   "herramienta de contexto, no como senal automatica.")

    return {
        "ok": True,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "period": {"from": dates[start], "to": dates[-1], "trading_days": len(rows), "years": round(yrs, 1)},
        "note": "Backtest del nucleo tecnico (5 de 7 factores; sin breadth/sectores). Pesos renormalizados.",
        "buckets": bucket_stats,
        "corr_bias_vs_fwd5d": round(corr5, 3) if corr5 is not None else None,
        "strategy": {
            "total_return_pct": round(strat_total, 1),
            "buyhold_return_pct": round(bh_total, 1),
            "annualized_pct": round(((eq[-1]) ** (1 / yrs) - 1) * 100, 1) if yrs > 0 else None,
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(maxdd(eq), 1),
            "buyhold_max_drawdown_pct": round(maxdd(eq_bh), 1),
            "win_rate_pct": round(winrate, 1),
        },
        "verdict": verdict,
    }


def main():
    try:
        out = build()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "error": str(e)}
        print(f"[error] {e}", file=sys.stderr)
    with open("data/backtest.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(json.dumps(out, indent=2, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    main()
