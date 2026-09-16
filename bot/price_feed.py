from __future__ import annotations

import ccxt


def make_exchange(exchange_id: str):
    """Oeffentliche ccxt-Exchange-Instanz, kein API-Key noetig fuer Preisdaten."""
    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({"enableRateLimit": True})


def fetch_price(exchange, symbol: str) -> float:
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker["last"])
