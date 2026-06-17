"""
data_sources.py
================
Capa de adquisicion de datos en vivo para el dashboard del US100 (Nasdaq-100).

Fuentes:
  * ForexFactory (feed semanal JSON via faireconomy.media) -> calendario economico / noticias.
  * Yahoo Finance (chart API publica) -> precio e historico de:
        ^NDX  (US100 / Nasdaq-100)
        ^VIX  (volatilidad / miedo)
        DX-Y.NYB (indice dolar / DXY)
        ^TNX  (rendimiento del bono 10Y)
  * Constituyentes del Nasdaq-100 (via Yahoo) -> breadth (amplitud) y heatmap por sector.

Todas las peticiones salen del backend (no del navegador) para evitar CORS y
para poder cachear y normalizar los datos.
"""

import time
import logging
import requests

log = logging.getLogger("megadata.data")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FF_CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ---------------------------------------------------------------------------
# Constituyentes representativos del Nasdaq-100 con su peso aproximado (%) y
# sector. Sirven para calcular amplitud (breadth) y el mapa por sector de forma
# ponderada. Los pesos son aproximados y suman ~la mayor parte del indice.
# ---------------------------------------------------------------------------
NDX_CONSTITUENTS = [
    # symbol, nombre, sector, peso aprox %
    ("AAPL",  "Apple",            "Technology",            8.8),
    ("MSFT",  "Microsoft",        "Technology",            8.2),
    ("NVDA",  "NVIDIA",           "Technology",            8.0),
    ("AMZN",  "Amazon",           "Consumer Cyclical",     5.5),
    ("AVGO",  "Broadcom",         "Technology",            4.8),
    ("META",  "Meta Platforms",   "Communication Svcs",    4.6),
    ("GOOGL", "Alphabet A",       "Communication Svcs",    2.6),
    ("GOOG",  "Alphabet C",       "Communication Svcs",    2.5),
    ("TSLA",  "Tesla",            "Consumer Cyclical",     3.1),
    ("COST",  "Costco",           "Consumer Defensive",    2.7),
    ("NFLX",  "Netflix",          "Communication Svcs",    2.6),
    ("AMD",   "AMD",              "Technology",            1.5),
    ("PEP",   "PepsiCo",          "Consumer Defensive",    1.4),
    ("ADBE",  "Adobe",            "Technology",            1.3),
    ("CSCO",  "Cisco",            "Technology",            1.3),
    ("LIN",   "Linde",            "Basic Materials",       1.2),
    ("TMUS",  "T-Mobile",         "Communication Svcs",    1.4),
    ("INTU",  "Intuit",           "Technology",            1.2),
    ("QCOM",  "Qualcomm",         "Technology",            1.1),
    ("TXN",   "Texas Instruments","Technology",            1.1),
    ("AMGN",  "Amgen",            "Healthcare",            1.1),
    ("ISRG",  "Intuitive Surg.",  "Healthcare",            1.0),
    ("HON",   "Honeywell",        "Industrials",           0.9),
    ("BKNG",  "Booking",          "Consumer Cyclical",     1.1),
    ("AMAT",  "Applied Materials","Technology",            0.9),
    ("CMCSA", "Comcast",          "Communication Svcs",    0.9),
    ("VRTX",  "Vertex Pharma",    "Healthcare",            0.8),
    ("ADP",   "ADP",              "Industrials",           0.8),
    ("MU",    "Micron",           "Technology",            0.8),
    ("PANW",  "Palo Alto Nets",   "Technology",            0.8),
    ("GILD",  "Gilead",           "Healthcare",            0.7),
    ("LRCX",  "Lam Research",     "Technology",            0.7),
    ("REGN",  "Regeneron",        "Healthcare",            0.6),
    ("PDD",   "PDD Holdings",     "Consumer Cyclical",     0.7),
    ("KLAC",  "KLA Corp",         "Technology",            0.7),
    ("SBUX",  "Starbucks",        "Consumer Cyclical",     0.6),
    ("MDLZ",  "Mondelez",         "Consumer Defensive",    0.6),
    ("INTC",  "Intel",            "Technology",            0.6),
    ("CDNS",  "Cadence",          "Technology",            0.6),
    ("SNPS",  "Synopsys",         "Technology",            0.6),
]


def _get_json(url, params=None, timeout=20, retries=2):
    """GET con reintentos que devuelve JSON o lanza excepcion."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("GET fallo (%s) intento %d: %s", url, attempt + 1, e)
            time.sleep(0.6 * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------
def fetch_yahoo_chart(symbol, rng="1y", interval="1d"):
    """
    Devuelve dict con:
      price, prev_close, change_pct, currency, name,
      closes (lista de cierres historicos), high52, low52, market_time
    """
    url = YF_CHART.format(symbol=requests.utils.quote(symbol, safe=""))
    data = _get_json(url, params={"range": rng, "interval": interval})
    result = data["chart"]["result"][0]
    meta = result["meta"]

    closes = []
    try:
        quote = result["indicators"]["quote"][0]
        raw = quote.get("close") or []
        closes = [c for c in raw if c is not None]
    except (KeyError, IndexError):
        closes = []

    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    # cierre previo mas exacto = penultimo cierre historico si disponible
    if len(closes) >= 2:
        prev = closes[-2]
        if price is None:
            price = closes[-1]

    change_pct = None
    if price is not None and prev:
        change_pct = (price - prev) / prev * 100.0

    return {
        "symbol": symbol,
        "name": meta.get("shortName") or symbol,
        "price": price,
        "prev_close": prev,
        "change_pct": change_pct,
        "currency": meta.get("currency"),
        "high52": meta.get("fiftyTwoWeekHigh"),
        "low52": meta.get("fiftyTwoWeekLow"),
        "closes": closes,
        "market_time": meta.get("regularMarketTime"),
    }


def fetch_quote_simple(symbol, rng="5d"):
    """Version ligera: solo precio y cambio %."""
    d = fetch_yahoo_chart(symbol, rng=rng, interval="1d")
    return {
        "symbol": symbol,
        "price": d["price"],
        "change_pct": d["change_pct"],
    }


# ---------------------------------------------------------------------------
# ForexFactory (calendario economico)
# ---------------------------------------------------------------------------
def fetch_forexfactory_calendar():
    """
    Devuelve lista de eventos de la semana:
      title, country, date(ISO), impact(High/Medium/Low), forecast, previous
    """
    data = _get_json(FF_CALENDAR)
    events = []
    for e in data:
        events.append({
            "title": e.get("title"),
            "country": e.get("country"),
            "date": e.get("date"),
            "impact": e.get("impact"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
        })
    return events


# ---------------------------------------------------------------------------
# Constituyentes -> breadth + sectores
# ---------------------------------------------------------------------------
def fetch_constituents_performance():
    """
    Recorre los constituyentes principales y devuelve su rendimiento intradia.
    Tolerante a fallos: si un simbolo falla se omite.
    """
    out = []
    for symbol, name, sector, weight in NDX_CONSTITUENTS:
        try:
            d = fetch_yahoo_chart(symbol, rng="5d", interval="1d")
            out.append({
                "symbol": symbol,
                "name": name,
                "sector": sector,
                "weight": weight,
                "price": d["price"],
                "change_pct": d["change_pct"],
            })
        except Exception as e:  # noqa: BLE001
            log.warning("constituyente %s fallo: %s", symbol, e)
        time.sleep(0.05)
    return out
