from __future__ import annotations

import time

from . import db
from .config import Config


def generate_momentum_signal(cfg: Config) -> None:
    """Platzhalter-Signalquelle: SMA-Crossover auf der bisherigen Preis-Historie.

    Dient nur dazu, die Pipeline (Signal -> Entscheidung -> Trade) end-to-end
    lauffaehig zu machen. Spaeter schreiben weitere Quellen (Discord-Collector,
    Twitter-Listener, ...) einfach zusaetzliche Zeilen in dieselbe "signals"-
    Tabelle mit source="discord:<name>" usw. - die Decision-Engine unten
    muss dafuer nicht angepasst werden.
    """
    long_window = cfg.strategy.momentum_long_window
    history = db.get_price_history(cfg.db_path, cfg.symbol, limit=long_window)
    if len(history) < long_window:
        return  # noch nicht genug Daten gesammelt

    prices = [p for _, p in history]
    short_window = cfg.strategy.momentum_short_window
    sma_short = sum(prices[-short_window:]) / short_window
    sma_long = sum(prices) / len(prices)

    if sma_long == 0:
        return

    # normalisierte Abweichung als grobes Konfidenzmass, auf [-1, 1] geklemmt
    raw = (sma_short - sma_long) / sma_long
    confidence = max(-1.0, min(1.0, raw * 20))
    direction = "long" if confidence > 0 else ("short" if confidence < 0 else "neutral")

    db.record_signal(
        cfg.db_path,
        source="momentum",
        symbol=cfg.symbol,
        direction=direction,
        confidence=confidence,
        reason=f"SMA{short_window}={sma_short:.2f} vs SMA{len(prices)}={sma_long:.2f}",
    )


def decide(cfg: Config, lookback_seconds: float = 3600.0) -> tuple[str, float, str]:
    """Aggregiert alle Signale der letzten `lookback_seconds` (konfidenz-gewichtet)
    und liefert eine Handelsentscheidung: ("buy" | "sell" | "hold", score, begruendung).
    """
    since = time.time() - lookback_seconds
    signals = db.get_recent_signals(cfg.db_path, cfg.symbol, since)
    if not signals:
        return "hold", 0.0, "keine Signale im Betrachtungszeitraum"

    total_confidence_weight = sum(abs(s["confidence"]) for s in signals)
    if total_confidence_weight == 0:
        return "hold", 0.0, "alle Signale neutral"

    score = sum(s["confidence"] for s in signals) / len(signals)
    sources = ", ".join(sorted({s["source"] for s in signals}))

    if score >= cfg.strategy.buy_confidence_threshold:
        return "buy", score, f"aggregierter Score {score:.2f} aus [{sources}]"
    if score <= cfg.strategy.sell_confidence_threshold:
        return "sell", score, f"aggregierter Score {score:.2f} aus [{sources}]"
    return "hold", score, f"aggregierter Score {score:.2f} unter Schwelle aus [{sources}]"
