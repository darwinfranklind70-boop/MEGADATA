#!/usr/bin/env python3
"""
build_gex.py
============
Motor tipo "SpotGamma" (gratis) para el US100 / Nasdaq-100.

Descarga la cadena de opciones del indice NDX (y QQQ como respaldo) desde el
feed publico y diferido de CBOE, y calcula los niveles de posicionamiento de
dealers que mueven el mercado:

  * GEX total (Gamma Exposure) y regimen (gamma positiva vs negativa)
  * Gamma flip / zero-gamma (nivel donde el regimen cambia) -> recalculado por Black-Scholes
  * Call wall  (mayor pared de gamma de calls -> resistencia / iman)
  * Put wall   (mayor pared de gamma de puts  -> soporte / iman)
  * Max pain   (strike de maximo dolor para compradores de opciones)
  * Expected move (movimiento esperado por el straddle ATM del vencimiento mas cercano)
  * Perfil de gamma por strike alrededor del spot

Genera un JSON pequeno en data/gex.json que el dashboard consume.

Pensado para correr en GitHub Actions cada ~30 min (datos CBOE diferidos ~15 min).
Nunca lanza excepcion hacia el runner: ante fallo escribe un JSON con 'ok': false.
"""

import sys
import re
import json
import math
import gzip
import datetime as dt
import urllib.request

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
OCC_RE = re.compile(r"(\d{6})([CP])(\d{8})$")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MEGADATA-GEX/1.0)"}
CONTRACT = 100          # multiplicador
DTE_MAX = 45            # vencimientos relevantes para swing (dias)
STRIKE_BAND = 0.18      # +/-18% del spot para el calculo de gamma flip


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_gamma(S, K, T, sigma, r=0.04):
    """Gamma de Black-Scholes (igual para call y put)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_pdf(d1) / (S * sigma * math.sqrt(T))


def parse_symbol(sym, root):
    """
    Formato OCC al final del simbolo: YYMMDD + (C|P) + strike(8 digitos, /1000).
    Robusto a cualquier raiz/prefijo (NDX, QQQ, etc.).
    """
    m = OCC_RE.search(sym or "")
    if not m:
        return None
    ymd, cp, strike_s = m.group(1), m.group(2), m.group(3)
    strike = int(strike_s) / 1000.0
    try:
        expiry = dt.date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    return expiry, cp, strike


def fetch_chain(sym):
    url = CBOE.format(sym=sym)
    req = urllib.request.Request(url, headers={**HEADERS, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    js = json.loads(raw.decode("utf-8"))
    data = js.get("data", js)
    opts = data.get("options", [])
    spot = data.get("close") or data.get("current_price") or data.get("last")
    if not spot:
        spot = data.get("prev_day_close")
    return float(spot), opts


def build(sym, root):
    spot, opts = fetch_chain(sym)
    today = dt.date.today()

    rows = []
    for o in opts:
        p = parse_symbol(o.get("option", ""), root)
        if not p:
            continue
        expiry, cp, strike = p
        dte = (expiry - today).days
        if dte < 0 or dte > DTE_MAX:
            continue
        oi = o.get("open_interest") or 0
        if oi <= 0:
            continue
        rows.append({
            "expiry": expiry, "dte": dte, "cp": cp, "strike": strike,
            "oi": oi, "gamma": o.get("gamma") or 0.0,
            "iv": o.get("iv") or 0.0, "vol": o.get("volume") or 0,
            "bid": o.get("bid") or 0.0, "ask": o.get("ask") or 0.0,
        })

    if not rows:
        raise RuntimeError("sin opciones validas en rango DTE")

    # ---- GEX por strike (gamma del feed al spot actual) ----
    # convencion naive: calls suman gamma (+), puts restan (-) para el dealer.
    by_strike = {}
    call_gamma = {}
    put_gamma = {}
    for r in rows:
        notional = r["gamma"] * r["oi"] * CONTRACT * spot * spot * 0.01
        sign = 1.0 if r["cp"] == "C" else -1.0
        by_strike[r["strike"]] = by_strike.get(r["strike"], 0.0) + sign * notional
        if r["cp"] == "C":
            call_gamma[r["strike"]] = call_gamma.get(r["strike"], 0.0) + r["gamma"] * r["oi"]
        else:
            put_gamma[r["strike"]] = put_gamma.get(r["strike"], 0.0) + r["gamma"] * r["oi"]

    total_gex = sum(by_strike.values())

    # call wall: strike con mayor gamma de calls por encima del spot
    calls_above = {k: v for k, v in call_gamma.items() if k >= spot}
    puts_below = {k: v for k, v in put_gamma.items() if k <= spot}
    call_wall = max(calls_above, key=calls_above.get) if calls_above else max(call_gamma, key=call_gamma.get)
    put_wall = max(puts_below, key=puts_below.get) if puts_below else max(put_gamma, key=put_gamma.get)

    # ---- gamma flip por Black-Scholes (recalcula gamma a cada spot candidato) ----
    band_rows = [r for r in rows if abs(r["strike"] - spot) <= spot * STRIKE_BAND and r["iv"] > 0]
    lo, hi = spot * (1 - STRIKE_BAND), spot * (1 + STRIKE_BAND)
    steps = 140
    prev_s, prev_g = None, None
    flip = None
    profile_flip = []
    for i in range(steps + 1):
        S = lo + (hi - lo) * i / steps
        g = 0.0
        for r in band_rows:
            T = max(r["dte"], 0.5) / 365.0
            gm = bs_gamma(S, r["strike"], T, r["iv"])
            sign = 1.0 if r["cp"] == "C" else -1.0
            g += sign * gm * r["oi"] * CONTRACT * S * S * 0.01
        profile_flip.append((S, g))
        if prev_g is not None and prev_g <= 0 < g or (prev_g is not None and prev_g >= 0 > g):
            # interpolar cruce por cero mas cercano al spot
            frac = abs(prev_g) / (abs(prev_g) + abs(g)) if (abs(prev_g) + abs(g)) else 0.5
            cross = prev_s + (S - prev_s) * frac
            if flip is None or abs(cross - spot) < abs(flip - spot):
                flip = cross
        prev_s, prev_g = S, g

    # ---- max pain (vencimiento mas cercano) ----
    nearest_exp = min(set(r["expiry"] for r in rows))
    exp_rows = [r for r in rows if r["expiry"] == nearest_exp]
    strikes = sorted(set(r["strike"] for r in exp_rows))
    best_pain, max_pain = None, None
    for S in strikes:
        pain = 0.0
        for r in exp_rows:
            if r["cp"] == "C":
                pain += max(0.0, S - r["strike"]) * r["oi"]
            else:
                pain += max(0.0, r["strike"] - S) * r["oi"]
        if best_pain is None or pain < best_pain:
            best_pain, max_pain = pain, S

    # ---- expected move (straddle ATM del vencimiento mas cercano) ----
    atm = min(exp_rows, key=lambda r: abs(r["strike"] - spot))["strike"]
    call_mid = next((( r["bid"] + r["ask"]) / 2 for r in exp_rows if r["cp"] == "C" and r["strike"] == atm), 0)
    put_mid = next(((r["bid"] + r["ask"]) / 2 for r in exp_rows if r["cp"] == "P" and r["strike"] == atm), 0)
    straddle = call_mid + put_mid
    exp_move = straddle * 0.85  # aproximacion 1 sigma
    exp_move_pct = exp_move / spot * 100 if spot else None

    # ---- perfil de gamma por strike alrededor del spot ----
    near = sorted([k for k in by_strike if abs(k - spot) <= spot * 0.06])
    profile = [{"strike": round(k, 1), "gex": round(by_strike[k] / 1e9, 3)} for k in near]

    scale = 1e9  # mostrar en "miles de millones $ / 1%"
    return {
        "ok": True,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "CBOE delayed (~15min)",
        "underlying": root,
        "spot": round(spot, 2),
        "dte_max": DTE_MAX,
        "nearest_expiry": nearest_exp.isoformat(),
        "total_gex_bn": round(total_gex / scale, 2),
        "regime": "positiva" if total_gex >= 0 else "negativa",
        "gamma_flip": round(flip, 1) if flip else None,
        "call_wall": round(call_wall, 1),
        "put_wall": round(put_wall, 1),
        "max_pain": round(max_pain, 1) if max_pain else None,
        "expected_move_pct": round(exp_move_pct, 2) if exp_move_pct else None,
        "expected_move_pts": round(exp_move, 1) if exp_move else None,
        "profile": profile,
    }


def main():
    out = {"ok": False, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
           "error": None}
    # intenta NDX (indice, niveles institucionales directos); si falla, QQQ.
    for sym, root in (("_NDX", "NDX"), ("QQQ", "QQQ")):
        try:
            out = build(sym, root)
            out["underlying_symbol"] = sym
            break
        except Exception as e:  # noqa: BLE001
            out = {"ok": False,
                   "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "error": f"{sym}: {e}"}
            print(f"[warn] {sym} fallo: {e}", file=sys.stderr)

    with open("data/gex.json", "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2)[:1200])


if __name__ == "__main__":
    main()
