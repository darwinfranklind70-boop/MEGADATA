#!/usr/bin/env python3
"""
build_news.py
=============
Motor de contexto geopolitico / macro para el US100 (Nasdaq-100).

Lee titulares en (casi) tiempo real desde Google News RSS (gratis, sin login)
para temas que mueven al US100: anuncios de Trump y funcionarios, Israel/Iran/
Estrecho de Ormuz, Reserva Federal, tasas, inflacion, aranceles/guerra comercial,
petroleo y tono de mercado.

Clasifica cada titular por su IMPACTO direccional sobre el US100, lo pondera por
relevancia y por lo reciente que es, y produce:
  * "news_pulse"  -> sesgo de contexto -100..+100 (negativo = presion bajista)
  * titulares destacados con su categoria, direccion y el porque
  * una conclusion que relaciona el contexto mundial con el US100

Pensado para correr en GitHub Actions junto a build_gex.py. Nunca lanza
excepcion hacia el runner; ante fallo escribe news.json con ok=false.
"""

import re
import sys
import json
import html
import datetime as dt
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (compatible; MEGADATA-News/1.0)"
GN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# Consultas dirigidas (cada una en la ultima ~24-48h)
QUERIES = [
    "Trump announcement when:2d",
    "Israel OR Iran OR Hormuz OR Gaza OR Lebanon conflict when:2d",
    "oil price OR crude OR Brent OR OPEC when:2d",
    "Federal Reserve OR interest rates OR Powell OR FOMC when:2d",
    "inflation OR CPI OR jobs report OR recession US when:2d",
    "tariffs OR trade war OR China chips OR export ban when:2d",
    "Nasdaq OR Wall Street OR stock market OR tech stocks when:1d",
]

# Reglas de impacto: (regex, categoria, direccion[-1..+1], peso_base, explicacion)
# direccion: +1 alcista para US100, -1 bajista.
# IMPORTANTE: cada titular suma TODAS las reglas que matchean (no solo una), para
# que el contexto se compense (ej. "oil tumbles as Hormuz reopens" = alcista).
RULES = [
    # --- Geopolitica: escalada (bajista) - requiere verbos de conflicto ---
    (r"\b(war breaks|missile|airstrike|air strike|attack|invasion|invade|bombard|nuclear|deploy troops|warship|seiz|blockade|escalat)\b",
     "Geopolitica/escalada", -1, 3.0, "Escalada geopolitica = risk-off, salida de activos de riesgo"),
    (r"(hormuz.{0,30}(clos|block|seiz|attack|mine|threat|disrupt|tension|halt|shut|strike))|((clos|block|seiz|attack|mine|threat|disrupt|halt|shut).{0,30}hormuz)",
     "Ormuz cerrado/amenazado", -1, 3.5, "Disrupcion en Ormuz dispara el petroleo -> inflacion y risk-off"),
    (r"\b(no deal|talks collapse|breaks down|rejected|walks away|ultimatum|new sanctions|imposes sanctions)\b",
     "Diplomacia rota", -1, 2.5, "Ruptura de negociaciones aumenta la incertidumbre"),
    # --- Geopolitica: desescalada (alcista) ---
    (r"\b(ceasefire|cease-fire|truce|peace deal|de-?escalat|agreement reached|deal reached|resolution|reopen|reopens|reopening)\b",
     "Desescalada / acuerdo", +1, 3.0, "Desescalada reduce la prima de riesgo -> apoyo a activos de riesgo"),
    # --- Petroleo ---
    (r"\b(oil|crude|brent)\b.{0,25}\b(surg|jump|spike|soar|rally|rise|climb|jumps|rises)\b",
     "Petroleo al alza", -1, 2.0, "Petroleo al alza alimenta inflacion y frena al Nasdaq"),
    (r"\b(oil|crude|brent)\b.{0,25}\b(tumbl|fall|drop|slump|slide|ease|plunge|retreat|falls|drops)\b",
     "Petroleo a la baja", +1, 1.8, "Petroleo a la baja alivia presion inflacionaria -> apoyo al US100"),
    # --- Reserva Federal / tasas ---
    (r"\b(rate cut|cut rates|cuts rates|dovish|easing|lower rates|rate pause|holds rates)\b",
     "Fed dovish/pausa", +1, 2.3, "Sesgo dovish/pausa de la Fed favorece a las tecnologicas/US100"),
    (r"\b(rate hike|hikes rates|raise rates|hawkish|higher for longer|tighten)\b",
     "Fed hawkish", -1, 2.5, "Sesgo hawkish presiona valoraciones tech del US100"),
    # --- Inflacion / recesion ---
    (r"\b(inflation (hot|surg|jump|accelerat|rises|rise)|cpi (hot|jump|surg|rises)|prices? surg)\b",
     "Inflacion caliente", -1, 2.5, "Inflacion alta -> Fed mas dura -> presion al US100"),
    (r"\b(inflation (cool|eas|slow|fall)|cpi (cool|miss|slow|eas)|disinflation)\b",
     "Inflacion enfriando", +1, 2.0, "Inflacion cediendo abre la puerta a recortes"),
    (r"\b(recession|hard landing|layoffs surge|jobless surg|credit crunch|mass layoffs)\b",
     "Recesion / debilidad", -1, 2.5, "Temor de recesion pesa sobre las acciones"),
    # --- Comercio / aranceles ---
    (r"\b(tariff|trade war|export ban|chip ban|export restrictions|sanctions on chips)\b",
     "Aranceles / guerra comercial", -1, 2.5, "Aranceles/limites a chips danan a las tecnologicas del US100"),
    (r"\b(trade deal|tariff (cut|reliev|pause|exempt|roll)|trade agreement)\b",
     "Acuerdo comercial", +1, 2.0, "Distension comercial favorece a las tecnologicas"),
    # --- Tech / AI ---
    (r"\b(ai (boom|demand|spend|capex)|record (earnings|profit)|chip demand|datacenter|blowout earnings)\b",
     "Tech/IA fuerte", +1, 2.0, "Demanda de IA/chips impulsa al Nasdaq"),
    (r"\b(tech (selloff|sell-off|rout|plunge|slump)|chip (glut|slump)|earnings miss|guidance cut)\b",
     "Tech debil", -1, 2.0, "Debilidad tech pesa directamente sobre el US100"),
    # --- Tono de mercado ---
    (r"\b(stocks? (jump|rally|surg|soar|rise|climb)|equity indexes? jump|wall street (rally|jump|rises)|nasdaq (jump|surg|rally|rises))\b",
     "Mercado risk-on", +1, 1.5, "Tono de mercado positivo (risk-on)"),
    (r"\b(stocks? (fall|drop|tumbl|slump|sink|plunge)|wall street (fall|drop|tumbl)|nasdaq (fall|drop|tumbl|slump)|selloff)\b",
     "Mercado risk-off", -1, 1.5, "Tono de mercado negativo (risk-off)"),
]


def fetch_rss(query):
    url = GN.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_items(xml):
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(.*?)</title>", block, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        s = re.search(r"<source[^>]*>(.*?)</source>", block, re.S)
        if not t:
            continue
        title = html.unescape(re.sub(r"<.*?>", "", t.group(1))).strip()
        when = None
        if d:
            try:
                when = parsedate_to_datetime(d.group(1).strip())
            except Exception:  # noqa: BLE001
                when = None
        items.append({"title": title,
                      "source": html.unescape(s.group(1)).strip() if s else "",
                      "when": when})
    return items


def classify(title):
    """Devuelve lista de (categoria, direccion, peso, explicacion) que matchean."""
    hits = []
    low = title.lower()
    for rx, cat, direction, w, why in RULES:
        if re.search(rx, low):
            hits.append((cat, direction, w, why))
    return hits


def build():
    now = dt.datetime.now(dt.timezone.utc)
    seen = set()
    scored = []          # titulares con impacto
    raw_count = 0

    for q in QUERIES:
        try:
            xml = fetch_rss(q)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] query fallo: {q}: {e}", file=sys.stderr)
            continue
        for it in parse_items(xml):
            key = it["title"].lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            raw_count += 1
            hits = classify(it["title"])
            if not hits:
                continue
            # recencia: peso x1 si <2h, decae hasta x0.3 a 60h
            hours = 24.0
            if it["when"]:
                hours = max(0.0, (now - it["when"]).total_seconds() / 3600.0)
            recency = max(0.3, 1.0 - hours / 60.0)
            # NET del titular: suma de todas las reglas (el contexto se compensa)
            net_dir = sum(d * w for (_c, d, w, _y) in hits)
            if abs(net_dir) < 0.4:
                continue  # senal mixta/neutral
            # categoria dominante = la que mas aporta en el sentido del neto
            sign = 1 if net_dir > 0 else -1
            same = [h for h in hits if h[1] == sign] or hits
            cat, direction, w, why = max(same, key=lambda h: h[2])
            contribution = net_dir * recency
            scored.append({
                "title": it["title"],
                "source": it["source"],
                "when": it["when"].isoformat() if it["when"] else None,
                "hours_ago": round(hours, 1),
                "category": cat,
                "direction": "alcista" if net_dir > 0 else "bajista",
                "why": why,
                "score": round(contribution, 2),
            })

    if not scored:
        return {"ok": True, "generated_at": now.isoformat(),
                "news_pulse": 0, "label": "sin senales",
                "headlines": [], "drivers": [],
                "conclusion": "Sin titulares de alto impacto para el US100 en las ultimas horas.",
                "raw_count": raw_count}

    # pulso = balance neto direccional ponderado (-100..+100), no satura por volumen
    net = sum(s["score"] for s in scored)
    abs_total = sum(abs(s["score"]) for s in scored) or 1.0
    tilt = net / abs_total                     # -1..+1 (fraccion neta a favor/contra)
    intensity = min(1.0, abs_total / 25.0)     # cuanta "energia" de noticias hay
    pulse = max(-100, min(100, tilt * (60 + 40 * intensity)))

    # ordenar por impacto absoluto y recencia
    scored.sort(key=lambda s: (abs(s["score"]), -s["hours_ago"]), reverse=True)
    top = scored[:12]

    # agrupar drivers por categoria
    by_cat = {}
    for s in scored:
        c = by_cat.setdefault(s["category"], {"category": s["category"], "score": 0.0,
                                              "direction": s["direction"], "why": s["why"], "n": 0})
        c["score"] += s["score"]
        c["n"] += 1
    drivers = sorted(by_cat.values(), key=lambda c: abs(c["score"]), reverse=True)
    for d in drivers:
        d["score"] = round(d["score"], 2)
        d["direction"] = "alcista" if d["score"] >= 0 else "bajista"

    bias = "bajista" if pulse < -8 else "alcista" if pulse > 8 else "neutral"
    pos = [d for d in drivers if d["score"] > 0][:3]
    neg = [d for d in drivers if d["score"] < 0][:3]
    parts = [f"El contexto de noticias inclina al US100 en sentido {bias} (pulso {pulse:+.0f}/100)."]
    if neg:
        parts.append("Presion bajista por: " + "; ".join(f"{d['category']} ({d['why']})" for d in neg) + ".")
    if pos:
        parts.append("Apoyo alcista por: " + "; ".join(f"{d['category']} ({d['why']})" for d in pos) + ".")
    parts.append("Recuerda: los titulares geopoliticos pueden revertir el sesgo tecnico en minutos; ajusta riesgo ante eventos vivos.")
    conclusion = " ".join(parts)

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "source": "Google News RSS (gratis)",
        "news_pulse": round(pulse, 1),
        "label": bias,
        "drivers": drivers[:6],
        "headlines": top,
        "conclusion": conclusion,
        "raw_count": raw_count,
    }


def main():
    try:
        out = build()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "error": str(e)}
        print(f"[error] {e}", file=sys.stderr)
    with open("data/news.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: out[k] for k in out if k != "headlines"}, indent=2, ensure_ascii=False)[:900])


if __name__ == "__main__":
    main()
