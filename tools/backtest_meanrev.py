#!/usr/bin/env python3
"""
backtest_meanrev.py
===================
Prueba la HIPOTESIS de reversion en extremos descubierta en el backtest base:
  - cuando el sesgo es muy BAJISTA, el US100 tiende a rebotar (comprar la caida)
  - cuando el sesgo es muy ALCISTA, tiende a agotarse

Estrategia evaluada (reproducible solo desde precio; sin gamma, que no tiene
historico de opciones):

  LONG  cuando sesgo <= L  (flat->long), salida tras H dias.
  SHORT cuando sesgo >= S  (flat->short), salida tras H dias.

Prueba una rejilla de umbrales/horizontes, separa lado largo y corto (porque un
indice alcista estructural castiga a los cortos), y reporta estadisticas de
trade: nº, win rate, retorno medio, expectativa, total, y curva combinada.

Veredicto honesto. Genera data/meanrev.json. Sin dependencias (urllib).
"""

import sys
import json
import math
import datetime as dt
import urllib.request

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10y&interval=1d"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MEGADATA-MR/1.0)"}

RAW_W = {"trend": 0.24, "momentum": 0.16, "volatility": 0.16, "rates": 0.12, "dollar": 0.10}
_S = sum(RAW_W.values())
WEIGHTS = {k: v / _S for k, v in RAW_W.items()}


def clamp(x, lo=-100.0, hi=100.0):
    return max(lo, min(hi, x))


def sma(v, p):
    return sum(v[-p:]) / p if len(v) >= p else None


def rsi(v, p=14):
    if len(v) < p + 1:
        return None
    g = l = 0.0
    for i in range(-p, 0):
        d = v[i] - v[i - 1]
        g += d if d >= 0 else 0
        l += -d if d < 0 else 0
    ag, al = g / p, l / p
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def roc(v, p=20):
    if len(v) < p + 1 or not v[-p - 1]:
        return None
    return (v[-1] - v[-p - 1]) / v[-p - 1] * 100.0


def fetch(sym):
    req = urllib.request.Request(CHART.format(sym=sym), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        js = json.loads(r.read().decode("utf-8"))
    res = js["chart"]["result"][0]
    out = {}
    for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[dt.datetime.utcfromtimestamp(t).date().isoformat()] = c
    return out


def f_trend(c):
    price = c[-1]
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    score = parts = 0
    for s in (s20, s50, s200):
        if s is not None:
            parts += 1
            score += 1 if price > s else -1
    if not parts:
        return 0.0
    base = score / parts * 70.0
    if s20 and s50 and s200:
        base += 30 if s20 > s50 > s200 else (-30 if s20 < s50 < s200 else 0)
    return clamp(base)


def f_mom(c):
    r = rsi(c, 14)
    mom = roc(c, 20)
    return 0.0 if r is None else clamp(0.6 * (r - 50) * 2 + 0.4 * clamp((mom or 0) * 6))


def f_vol(v):
    if len(v) < 2:
        return 0.0
    chg = (v[-1] - v[-2]) / v[-2] * 100 if v[-2] else 0
    return clamp(0.7 * clamp((20 - v[-1]) * 8) + 0.3 * clamp(-chg * 3))


def f_rates(t):
    c = roc(t, 5)
    return 0.0 if c is None else clamp(-c * 12)


def f_usd(d):
    c = roc(d, 5)
    return 0.0 if c is None else clamp(-c * 25)


def comp(n, v, t, d):
    return clamp(f_trend(n) * WEIGHTS["trend"] + f_mom(n) * WEIGHTS["momentum"]
                + f_vol(v) * WEIGHTS["volatility"] + f_rates(t) * WEIGHTS["rates"]
                + f_usd(d) * WEIGHTS["dollar"])


def stats_from_trades(trades):
    if not trades:
        return {"n": 0}
    n = len(trades)
    wins = [r for r in trades if r > 0]
    losses = [r for r in trades if r <= 0]
    avg = sum(trades) / n
    wr = len(wins) / n * 100
    aw = sum(wins) / len(wins) if wins else 0
    al = sum(losses) / len(losses) if losses else 0
    # expectativa por trade (%)
    return {
        "n": n,
        "win_rate": round(wr, 1),
        "avg_trade_pct": round(avg, 3),
        "avg_win_pct": round(aw, 3),
        "avg_loss_pct": round(al, 3),
        "total_pct": round(sum(trades), 1),
        "profit_factor": round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) != 0 else None,
    }


def simulate(side, thr, H, scores, closes):
    """Una posicion a la vez; entra al cierre de la señal, sale H dias despues."""
    trades = []
    i = 0
    n = len(scores)
    while i < n - H:
        sc = scores[i]
        sig = (side == "long" and sc <= thr) or (side == "short" and sc >= thr)
        if sig:
            entry = closes[i]
            exit_ = closes[i + H]
            ret = (exit_ - entry) / entry * 100.0
            if side == "short":
                ret = -ret
            trades.append(ret)
            i += H  # no solapar
        else:
            i += 1
    return trades


def build():
    ndx = fetch("%5ENDX")
    vix = fetch("%5EVIX")
    tnx = fetch("%5ETNX")
    dxy = fetch("DX-Y.NYB")
    dates = sorted(d for d in ndx if d in vix and d in tnx and d in dxy)
    if len(dates) < 400:
        raise RuntimeError(f"historico insuficiente ({len(dates)})")

    ndx_s = [ndx[d] for d in dates]
    vix_s = [vix[d] for d in dates]
    tnx_s = [tnx[d] for d in dates]
    dxy_s = [dxy[d] for d in dates]

    start = 200
    scores = [None] * start + [comp(ndx_s[:i + 1], vix_s[:i + 1], tnx_s[:i + 1], dxy_s[:i + 1])
                               for i in range(start, len(dates))]
    sc_eff = scores[start:]
    cl_eff = ndx_s[start:]

    # rejilla
    grid = {"long": [], "short": []}
    for H in (5, 10, 20):
        for L in (-40, -50, -60):
            tr = simulate("long", L, H, sc_eff, cl_eff)
            st = stats_from_trades(tr)
            st.update({"threshold": L, "hold_days": H})
            grid["long"].append(st)
        for Sx in (50, 60, 70):
            tr = simulate("short", Sx, H, sc_eff, cl_eff)
            st = stats_from_trades(tr)
            st.update({"threshold": Sx, "hold_days": H})
            grid["short"].append(st)

    def best(side):
        cand = [g for g in grid[side] if g.get("n", 0) >= 8]
        if not cand:
            return None
        return max(cand, key=lambda g: g["avg_trade_pct"])

    best_long = best("long")
    best_short = best("short")

    # benchmark: retorno medio a H dias de un dia cualquiera (base rate del indice)
    def base_rate(H):
        rr = [(cl_eff[i + H] - cl_eff[i]) / cl_eff[i] * 100 for i in range(len(cl_eff) - H)]
        return round(sum(rr) / len(rr), 3)

    edge = []
    long_edge = (best_long and best_long["avg_trade_pct"] > base_rate(best_long["hold_days"])
                 and best_long["win_rate"] >= 55 and (best_long.get("profit_factor") or 0) >= 1.5)
    if long_edge:
        edge.append(f"LONG-the-dip: sesgo<= {best_long['threshold']}, {best_long['hold_days']}d -> "
                    f"{best_long['avg_trade_pct']:+.2f}%/trade, {best_long['win_rate']}% aciertos, PF "
                    f"{best_long['profit_factor']}, {best_long['n']} trades (base del indice "
                    f"{base_rate(best_long['hold_days']):+.2f}%)")
    # el corto debe BATIR la base del indice y tener PF claro, si no es ruido
    short_edge = (best_short and best_short["avg_trade_pct"] > base_rate(best_short["hold_days"])
                  and best_short["win_rate"] >= 55 and (best_short.get("profit_factor") or 0) >= 1.5)
    if short_edge:
        edge.append(f"SHORT extremos alcistas: sesgo>= {best_short['threshold']}, "
                    f"{best_short['hold_days']}d -> {best_short['avg_trade_pct']:+.2f}%/trade, {best_short['win_rate']}%")

    thin = best_long and best_long["n"] < 20
    caution = " ADVERTENCIA: muestra pequeña (pocos eventos extremos en el periodo); efecto grande pero usar con gestion de riesgo, no como sistema de alta frecuencia." if thin else ""

    if long_edge and not short_edge:
        verdict = ("CONFIRMADO en el lado LARGO: comprar la caida en sesgo extremo bajista tiene ventaja clara. "
                   "El lado CORTO NO bate al indice (tiende a subir): operar SOLO largos de reversion. "
                   + edge[0] + caution)
        rating = "long_only"
    elif edge:
        verdict = "Ventaja confirmada: " + " | ".join(edge) + caution
        rating = "ok"
    else:
        verdict = ("La reversion en extremos NO mostro ventaja robusta en este periodo. "
                   "Tratar como contexto, no como sistema.")
        rating = "weak"

    # nota sobre gamma (no backtesteable historicamente)
    gamma_note = ("La confluencia con gamma (entrar largo cerca del PUT WALL, corto cerca del CALL WALL) "
                  "no se puede backtestear sin historico de opciones, pero se aplica EN VIVO como filtro "
                  "de mayor conviccion en el detector de setups del dashboard.")

    return {
        "ok": True,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "period": {"from": dates[start], "to": dates[-1], "years": round(len(sc_eff) / 252.0, 1)},
        "grid": grid,
        "best_long": best_long,
        "best_short": best_short,
        "base_rate_5d": base_rate(5),
        "base_rate_20d": base_rate(20),
        "rating": rating,
        "verdict": verdict,
        "gamma_note": gamma_note,
    }


def main():
    try:
        out = build()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "error": str(e)}
        print(f"[error] {e}", file=sys.stderr)
    with open("data/meanrev.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: out[k] for k in out if k != "grid"}, indent=2, ensure_ascii=False)[:1600])


if __name__ == "__main__":
    main()
