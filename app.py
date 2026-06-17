"""
app.py
======
Backend Flask del dashboard MEGADATA US100.

  GET /            -> dashboard HTML
  GET /api/data    -> JSON con todo el analisis (cacheado)
  GET /api/health  -> estado simple

El backend hace todas las peticiones externas (ForexFactory, Yahoo) para
evitar CORS y para cachear/normalizar. El front solo consume /api/data.
"""

import time
import logging
import threading
import datetime as dt

from flask import Flask, jsonify, render_template

import data_sources as ds
import analysis as an

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("megadata")

app = Flask(__name__)

CACHE_TTL = 180  # segundos
_cache = {"ts": 0, "payload": None}
_lock = threading.Lock()


def build_payload():
    """Adquiere datos en vivo y compone el analisis completo."""
    errors = []

    def safe(fn, default, label):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.warning("%s fallo: %s", label, e)
            errors.append(f"{label}: {e}")
            return default

    ndx = safe(lambda: ds.fetch_yahoo_chart("^NDX", rng="2y", interval="1d"),
               {"closes": [], "price": None}, "NDX")
    vix = safe(lambda: ds.fetch_yahoo_chart("^VIX", rng="1mo", interval="1d"),
               {"closes": [], "price": None}, "VIX")
    dxy = safe(lambda: ds.fetch_yahoo_chart("DX-Y.NYB", rng="1mo", interval="1d"),
               {"closes": [], "price": None}, "DXY")
    tnx = safe(lambda: ds.fetch_yahoo_chart("^TNX", rng="1mo", interval="1d"),
               {"closes": [], "price": None}, "TNX")
    constituents = safe(ds.fetch_constituents_performance, [], "Constituyentes")
    events = safe(ds.fetch_forexfactory_calendar, [], "ForexFactory")

    news = an.analyze_news(events)
    market = {
        "ndx": ndx, "vix": vix, "dxy": dxy, "tnx": tnx,
        "constituents": constituents, "news": news,
    }
    result = an.compose(market)

    # resumen de macro para el front
    def yld(t):
        p = t.get("price")
        return round(p, 2) if p else None  # ^TNX ya cotiza en % (ej 4.43)

    macro = {
        "ndx": {"price": ndx.get("price"), "change_pct": ndx.get("change_pct"),
                "high52": ndx.get("high52"), "low52": ndx.get("low52")},
        "vix": {"price": vix.get("price"), "change_pct": vix.get("change_pct")},
        "dxy": {"price": dxy.get("price"), "change_pct": dxy.get("change_pct")},
        "tnx": {"yield": yld(tnx), "change_pct": tnx.get("change_pct")},
    }

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "macro": macro,
        "analysis": result,
        "constituents": sorted(
            [c for c in constituents if c.get("change_pct") is not None],
            key=lambda x: x["change_pct"], reverse=True),
        "calendar_high": news["high_upcoming"],
        "errors": errors,
        "ttl": CACHE_TTL,
    }


def get_payload(force=False):
    with _lock:
        now = time.time()
        if force or _cache["payload"] is None or now - _cache["ts"] > CACHE_TTL:
            log.info("Reconstruyendo payload (force=%s)...", force)
            _cache["payload"] = build_payload()
            _cache["ts"] = now
        return _cache["payload"]


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    return jsonify(get_payload())


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "cached": _cache["payload"] is not None,
                    "age": round(time.time() - _cache["ts"], 1)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
