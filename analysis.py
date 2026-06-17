"""
analysis.py
===========
Motor de sesgo (bias) multifactor para el US100 (Nasdaq-100).

Combina varias dimensiones independientes, cada una normalizada al rango
[-100, +100] (negativo = bajista, positivo = alcista), y produce:

  * un sesgo compuesto ponderado (el "medidor ultrapreciso"),
  * una confianza/conviccion (acuerdo entre factores - riesgo de noticias),
  * una conclusion razonada (el "por que"),
  * una lista de escenarios adversos (siempre presente).

Factores:
  1. Tendencia       (precio vs SMA20/50/200 y alineacion)
  2. Momentum        (RSI 14 + tasa de cambio 20d)
  3. Volatilidad     (VIX nivel y cambio - inverso: miedo = bajista)
  4. Dolar (DXY)     (inverso: dolar fuerte = viento en contra)
  5. Tasas (10Y)     (inverso: rendimientos al alza = viento en contra para tech)
  6. Amplitud        (% de constituyentes al alza, ponderado)
  7. Sectores        (rendimiento sectorial ponderado del NDX)
  8. Riesgo noticias (eventos de alto impacto -> modula la conviccion)
"""

import datetime as dt
from statistics import pstdev

# Pesos de cada factor direccional (suman 1.0)
WEIGHTS = {
    "trend":      0.24,
    "momentum":   0.16,
    "volatility": 0.16,
    "rates":      0.12,
    "breadth":    0.12,
    "dollar":     0.10,
    "sectors":    0.10,
}

USD_LIKE = {"USD"}  # eventos que afectan directo al US100


# ---------------------------------------------------------------------------
# Indicadores tecnicos
# ---------------------------------------------------------------------------
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def roc(values, period=20):
    """Rate of change porcentual."""
    if len(values) < period + 1:
        return None
    past = values[-period - 1]
    if not past:
        return None
    return (values[-1] - past) / past * 100.0


def clamp(x, lo=-100.0, hi=100.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Factores
# ---------------------------------------------------------------------------
def factor_trend(ndx):
    closes = ndx.get("closes") or []
    price = ndx.get("price")
    if price is None and closes:
        price = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    detail_bits = []
    score = 0.0
    parts = 0
    for label, s in (("SMA20", s20), ("SMA50", s50), ("SMA200", s200)):
        if s is not None and price is not None:
            parts += 1
            above = price > s
            score += 1 if above else -1
            detail_bits.append(f"{'>' if above else '<'} {label}")
    if parts == 0:
        return {"key": "trend", "score": 0.0, "detail": "Sin historico suficiente"}
    base = score / parts * 70.0  # -70..+70 segun cuantas MAs respeta
    # bonus por alineacion perfecta (estructura de tendencia)
    if s20 and s50 and s200:
        if s20 > s50 > s200:
            base += 30
        elif s20 < s50 < s200:
            base -= 30
    return {
        "key": "trend",
        "score": clamp(base),
        "detail": "Precio " + ", ".join(detail_bits),
    }


def factor_momentum(ndx):
    closes = ndx.get("closes") or []
    r = rsi(closes, 14)
    mom = roc(closes, 20)
    if r is None:
        return {"key": "momentum", "score": 0.0, "detail": "Sin RSI"}
    # RSI 50 -> 0 ; RSI 80 -> +60 ; RSI 20 -> -60
    rsi_score = (r - 50) * 2.0
    roc_score = clamp((mom or 0) * 6.0)  # ~+/-16% => saturado
    score = clamp(0.6 * rsi_score + 0.4 * roc_score)
    flag = ""
    if r >= 70:
        flag = " (SOBRECOMPRA)"
    elif r <= 30:
        flag = " (SOBREVENTA)"
    detail = f"RSI14 {r:.0f}{flag}, ROC20 {mom:+.1f}%" if mom is not None else f"RSI14 {r:.0f}{flag}"
    return {"key": "momentum", "score": score, "detail": detail, "rsi": r}


def factor_volatility(vix):
    price = vix.get("price")
    chg = vix.get("change_pct") or 0.0
    if price is None:
        return {"key": "volatility", "score": 0.0, "detail": "VIX no disponible"}
    # VIX 12 -> +70 ; 20 -> 0 ; 30 -> -70 ; 40 -> -100  (inverso)
    level_score = clamp((20.0 - price) * 8.0)
    change_score = clamp(-chg * 3.0)  # VIX subiendo => risk-off
    score = clamp(0.7 * level_score + 0.3 * change_score)
    regime = "baja (risk-on)" if price < 15 else "moderada" if price < 22 else "elevada (risk-off)" if price < 30 else "extrema"
    return {"key": "volatility", "score": score,
            "detail": f"VIX {price:.1f} {chg:+.1f}% — volatilidad {regime}"}


def factor_dollar(dxy):
    closes = dxy.get("closes") or []
    chg5 = roc(closes, 5)
    chg = dxy.get("change_pct") or 0.0
    if chg5 is None:
        chg5 = chg
    # Dolar al alza = viento en contra (inverso). 2% en 5d => fuerte
    score = clamp(-chg5 * 25.0)
    trend = "fortaleciendose (presion)" if chg5 > 0.2 else "debilitandose (apoyo)" if chg5 < -0.2 else "estable"
    px = dxy.get("price")
    return {"key": "dollar", "score": score,
            "detail": f"DXY {px:.2f} ({chg5:+.1f}% 5d) — {trend}" if px else f"DXY {chg5:+.1f}% 5d"}


def factor_rates(tnx):
    closes = tnx.get("closes") or []
    chg5 = roc(closes, 5)
    px = tnx.get("price")
    if chg5 is None:
        chg5 = tnx.get("change_pct") or 0.0
    # Rendimientos al alza = viento en contra para tech (inverso)
    score = clamp(-chg5 * 12.0)
    yld = px if px else None  # ^TNX ya cotiza en % (ej 4.43)
    trend = "subiendo (presion)" if chg5 > 0.5 else "bajando (apoyo)" if chg5 < -0.5 else "estable"
    detail = f"10Y {yld:.2f}% ({chg5:+.1f}% 5d) — {trend}" if yld else f"10Y {chg5:+.1f}% 5d — {trend}"
    return {"key": "rates", "score": score, "detail": detail}


def factor_breadth(constituents):
    if not constituents:
        return {"key": "breadth", "score": 0.0, "detail": "Sin datos de amplitud"}
    up_w, total_w, up_n = 0.0, 0.0, 0
    for c in constituents:
        w = c.get("weight") or 0
        total_w += w
        chg = c.get("change_pct")
        if chg is not None and chg > 0:
            up_w += w
            up_n += 1
    if total_w == 0:
        return {"key": "breadth", "score": 0.0, "detail": "Sin pesos"}
    pct_up_w = up_w / total_w * 100.0
    score = clamp((pct_up_w - 50.0) * 3.0)
    return {"key": "breadth", "score": score,
            "detail": f"{up_n}/{len(constituents)} al alza — {pct_up_w:.0f}% del peso en verde"}


def factor_sectors(constituents):
    if not constituents:
        return {"key": "sectors", "score": 0.0, "detail": "Sin datos sectoriales", "sectors": []}
    agg = {}
    for c in constituents:
        sec = c.get("sector") or "Otros"
        w = c.get("weight") or 0
        chg = c.get("change_pct")
        if chg is None:
            continue
        d = agg.setdefault(sec, {"w": 0.0, "wchg": 0.0})
        d["w"] += w
        d["wchg"] += w * chg
    sectors = []
    tot_w, tot_wchg = 0.0, 0.0
    for sec, d in agg.items():
        if d["w"] == 0:
            continue
        avg = d["wchg"] / d["w"]
        sectors.append({"sector": sec, "weight": round(d["w"], 1), "change_pct": round(avg, 2)})
        tot_w += d["w"]
        tot_wchg += d["wchg"]
    sectors.sort(key=lambda x: x["change_pct"], reverse=True)
    market_chg = (tot_wchg / tot_w) if tot_w else 0.0
    score = clamp(market_chg * 35.0)
    return {"key": "sectors", "score": score,
            "detail": f"Rendimiento sectorial ponderado {market_chg:+.2f}%",
            "sectors": sectors}


# ---------------------------------------------------------------------------
# Riesgo de noticias (modula conviccion)
# ---------------------------------------------------------------------------
def analyze_news(events):
    now = dt.datetime.now(dt.timezone.utc)
    high_upcoming = []
    today_high = []
    for e in events or []:
        impact = (e.get("impact") or "").lower()
        country = e.get("country") or ""
        try:
            when = dt.datetime.fromisoformat(e["date"])
        except Exception:  # noqa: BLE001
            continue
        when_utc = when.astimezone(dt.timezone.utc)
        is_high = impact == "high"
        relevant = country in USD_LIKE or is_high
        if not relevant:
            continue
        delta_h = (when_utc - now).total_seconds() / 3600.0
        item = {
            "title": e.get("title"),
            "country": country,
            "impact": e.get("impact"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
            "date": e.get("date"),
            "hours_away": round(delta_h, 1),
        }
        if is_high and 0 <= delta_h <= 72:
            high_upcoming.append(item)
        if is_high and -12 <= delta_h < 0 and country in USD_LIKE:
            today_high.append(item)
    high_upcoming.sort(key=lambda x: x["hours_away"])
    # 0 eventos -> sin riesgo ; 4+ -> riesgo alto
    n = len(high_upcoming)
    risk = min(100, n * 22)
    return {
        "high_upcoming": high_upcoming,
        "today_high": today_high,
        "risk_score": risk,
        "count_high_upcoming": n,
    }


# ---------------------------------------------------------------------------
# Composicion final
# ---------------------------------------------------------------------------
def classify(score):
    if score >= 55:
        return "ALCISTA FUERTE", "strong-bull"
    if score >= 22:
        return "ALCISTA", "bull"
    if score > -22:
        return "NEUTRAL", "neutral"
    if score > -55:
        return "BAJISTA", "bear"
    return "BAJISTA FUERTE", "strong-bear"


FACTOR_LABELS = {
    "trend": "Tendencia (medias)",
    "momentum": "Momentum (RSI/ROC)",
    "volatility": "Volatilidad (VIX)",
    "rates": "Tasas 10Y",
    "breadth": "Amplitud (breadth)",
    "dollar": "Dolar (DXY)",
    "sectors": "Sectores NDX",
}


def compose(market):
    """
    market = {
      'ndx', 'vix', 'dxy', 'tnx', 'constituents', 'news'
    }
    """
    factors = {
        "trend": factor_trend(market["ndx"]),
        "momentum": factor_momentum(market["ndx"]),
        "volatility": factor_volatility(market["vix"]),
        "dollar": factor_dollar(market["dxy"]),
        "rates": factor_rates(market["tnx"]),
        "breadth": factor_breadth(market["constituents"]),
        "sectors": factor_sectors(market["constituents"]),
    }
    news = market["news"]

    # sesgo compuesto ponderado
    composite = 0.0
    for k, w in WEIGHTS.items():
        composite += factors[k]["score"] * w
    composite = clamp(composite)

    # conviccion: acuerdo entre factores (baja dispersion = alta) menos riesgo noticias
    scores = [factors[k]["score"] for k in WEIGHTS]
    dispersion = pstdev(scores) if len(scores) > 1 else 0.0
    agreement = clamp(100 - dispersion, 0, 100)
    confidence = max(0.0, min(100.0, agreement * (1 - news["risk_score"] / 250.0)))

    label, css = classify(composite)

    # contribuciones para narrativa
    contribs = []
    for k, w in WEIGHTS.items():
        contribs.append({
            "key": k,
            "label": FACTOR_LABELS[k],
            "score": round(factors[k]["score"], 1),
            "weight": w,
            "contribution": round(factors[k]["score"] * w, 1),
            "detail": factors[k].get("detail", ""),
        })
    contribs.sort(key=lambda x: x["contribution"], reverse=True)

    conclusion, adverse = build_narrative(composite, label, factors, news, contribs, market)

    return {
        "composite": round(composite, 1),
        "label": label,
        "label_css": css,
        "confidence": round(confidence, 0),
        "factors": factors,
        "contributions": contribs,
        "news": news,
        "conclusion": conclusion,
        "adverse": adverse,
    }


def build_narrative(composite, label, factors, news, contribs, market):
    direction = "alcista" if composite > 0 else "bajista" if composite < 0 else "neutral"
    bullish = [c for c in contribs if c["contribution"] > 2]
    bearish = [c for c in contribs if c["contribution"] < -2]

    parts = []
    parts.append(
        f"El medidor compuesto del US100 marca {composite:+.0f}/100 ({label}). "
        f"El sesgo neto es {direction}."
    )
    if bullish:
        top = "; ".join(f"{c['label']} ({c['detail']})" for c in bullish[:3])
        parts.append(f"A favor: {top}.")
    if bearish:
        top = "; ".join(f"{c['label']} ({c['detail']})" for c in bearish[:3])
        parts.append(f"En contra: {top}.")

    # postura swing sugerida
    if composite >= 22 and news["risk_score"] < 50:
        stance = ("Postura swing: sesgo a favor de largos en retrocesos hacia soporte/medias, "
                  "siempre con stop bajo el ultimo swing low.")
    elif composite <= -22 and news["risk_score"] < 50:
        stance = ("Postura swing: sesgo a favor de cortos en rebotes hacia resistencia/medias, "
                  "con stop sobre el ultimo swing high.")
    else:
        stance = ("Postura swing: mercado sin ventaja clara o con alto ruido de noticias. "
                  "Prudencia, tamano reducido o esperar confirmacion.")
    parts.append(stance)

    if news["count_high_upcoming"] > 0:
        ev = news["high_upcoming"][0]
        parts.append(
            f"Atencion: {news['count_high_upcoming']} evento(s) de alto impacto en 72h; "
            f"el mas proximo es '{ev['title']}' ({ev['country']}) en ~{ev['hours_away']:.0f}h."
        )

    conclusion = " ".join(parts)

    # ---- escenarios adversos (SIEMPRE) ----
    adverse = []
    rsi = factors["momentum"].get("rsi")
    if rsi is not None and rsi >= 70:
        adverse.append("RSI en sobrecompra (>70): riesgo de correccion / agotamiento alcista.")
    if rsi is not None and rsi <= 30:
        adverse.append("RSI en sobreventa (<30): posible rebote tecnico contra cortos.")
    vix = market["vix"].get("price")
    if vix is not None and vix >= 20:
        adverse.append(f"VIX elevado ({vix:.1f}): mayor probabilidad de movimientos bruscos.")
    if factors["dollar"]["score"] < -15:
        adverse.append("Dolar fortaleciendose: viento en contra para el Nasdaq.")
    if factors["rates"]["score"] < -15:
        adverse.append("Rendimientos del 10Y al alza: presion sobre valoraciones tech.")
    if factors["breadth"]["score"] < -10:
        adverse.append("Amplitud debil: la subida (si la hay) no esta respaldada por la mayoria.")
    for c in bearish[:3]:
        adverse.append(f"Factor en contra del sesgo: {c['label']} — {c['detail']}.")
    # contrarios al sesgo principal
    if composite > 0:
        for c in bearish:
            txt = f"Riesgo para el sesgo alcista: {c['label']} ({c['detail']})."
            if txt not in adverse:
                adverse.append(txt)
    elif composite < 0:
        for c in bullish:
            adverse.append(f"Riesgo para el sesgo bajista: {c['label']} ({c['detail']}).")
    if news["count_high_upcoming"] > 0:
        adverse.append(
            f"{news['count_high_upcoming']} dato(s) macro de alto impacto pueden invalidar el escenario."
        )
    if not adverse:
        adverse.append("Sin alertas tecnicas mayores, pero todo escenario puede fallar: gestiona el riesgo.")

    # dedup preservando orden
    seen, dedup = set(), []
    for a in adverse:
        if a not in seen:
            seen.add(a)
            dedup.append(a)
    return conclusion, dedup
