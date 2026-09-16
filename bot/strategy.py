from __future__ import annotations

import json
import logging
import time
import urllib.request

from . import db
from .config import Config

log = logging.getLogger("bitcoin_bot")

FEARGREED_URL = "https://api.alternative.me/fng/?limit=1"
FUNDING_URL = (
    "https://api.bitget.com/api/v2/mix/market/current-fund-rate"
    "?symbol=BTCUSDT&productType=usdt-futures"
)
# ab dieser Hoehe (pro 8h-Intervall) gilt die Funding Rate als "stark positiv" -> Vorsicht
FUNDING_CAUTION_RATE = 0.0005
DXY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=6mo&interval=1d"


# --- Reine Konfidenz-Formeln -------------------------------------------
# Bewusst getrennt von der Datenbeschaffung (DB/HTTP) darunter, damit
# bot/backtest.py exakt dieselbe Logik auf historischen Daten anwenden kann,
# statt sie ein zweites Mal (potenziell abweichend) nachzubauen.


def momentum_confidence(prices: list[float], short_window: int) -> tuple[float, str]:
    """SMA-Crossover: kurzer Schnitt (`short_window` letzte Werte) vs. Schnitt
    ueber alle uebergebenen `prices`. Auf [-1, 1] geklemmt.
    """
    sma_short = sum(prices[-short_window:]) / short_window
    sma_long = sum(prices) / len(prices)
    if sma_long == 0:
        return 0.0, "SMA_long=0"
    raw = (sma_short - sma_long) / sma_long
    confidence = max(-1.0, min(1.0, raw * 20))
    reason = f"SMA{short_window}={sma_short:.2f} vs SMA{len(prices)}={sma_long:.2f}"
    return confidence, reason


def feargreed_confidence(value: float, classification: str = "") -> tuple[float, str]:
    """Kontra-Indikator: 0 (extreme Angst) -> +1.0, 100 (extreme Gier) -> -1.0."""
    confidence = max(-1.0, min(1.0, (50.0 - value) / 50.0))
    reason = f"Fear&Greed={value:.0f} ({classification})" if classification else f"Fear&Greed={value:.0f}"
    return confidence, reason


def funding_confidence(funding_rate: float) -> tuple[float, str]:
    """Stark positive Funding Rate (viele ueberhebelte Longs) -> Vorsicht."""
    confidence = max(-1.0, min(1.0, -funding_rate / FUNDING_CAUTION_RATE))
    reason = f"Funding Rate={funding_rate * 100:.4f}%/8h"
    return confidence, reason


def trend_confidence(current: float, ma200: float) -> tuple[float, str]:
    """Preis vs. echtem 200-Tage-Durchschnitt auf Tageskerzen."""
    if ma200 == 0:
        return 0.0, "MA200=0"
    raw = (current - ma200) / ma200
    confidence = max(-1.0, min(1.0, raw * 10))
    reason = f"Preis {current:.2f} vs. MA200 {ma200:.2f}"
    return confidence, reason


def dxy_confidence(current: float, sma20: float) -> tuple[float, str]:
    """Dollar-Index vs. eigenem 20-Tage-Schnitt, invertiert (starker Dollar
    -> bearish fuer BTC). Skalierungsfaktor 50 aus historischer Abweichung
    (typisch +-1-2%) kalibriert.
    """
    if sma20 == 0:
        return 0.0, "DXY-SMA20=0"
    raw = (current - sma20) / sma20
    confidence = max(-1.0, min(1.0, -raw * 50))
    reason = f"DXY {current:.2f} vs. SMA20 {sma20:.2f}"
    return confidence, reason


def aggregate_scores(
    source_scores: dict[str, float], buy_threshold: float, sell_threshold: float
) -> tuple[str, float, str]:
    """Gemeinsame Aggregations-/Schwellen-Logik fuer Live-Betrieb und Backtest:
    ein Confidence-Wert pro Quelle rein, eine Handelsentscheidung raus.
    """
    if not source_scores:
        return "hold", 0.0, "keine Signale im Betrachtungszeitraum"

    score = sum(source_scores.values()) / len(source_scores)
    sources = ", ".join(f"{src}={val:+.2f}" for src, val in sorted(source_scores.items()))

    if score >= buy_threshold:
        return "buy", score, f"aggregierter Score {score:.2f} aus [{sources}]"
    if score <= sell_threshold:
        return "sell", score, f"aggregierter Score {score:.2f} aus [{sources}]"
    return "hold", score, f"aggregierter Score {score:.2f} unter Schwelle aus [{sources}]"


def golden_cross_decision(sma_short: float, sma_long: float, holding: bool) -> tuple[str, float, str]:
    """Klassischer SMA-Crossover (z.B. 50/200-Tage): binaer investiert oder in
    Cash, unabhaengig vom 5-Quellen-Score. `holding` = haelt das Portfolio
    aktuell schon eine Position (btc > 0)? Reine Funktion, damit Live-Betrieb
    und bot/backtest.py exakt dieselbe Regel anwenden.
    """
    should_hold = sma_short > sma_long
    if should_hold and not holding:
        return "buy", 1.0, f"Golden Cross: SMA_kurz={sma_short:.2f} > SMA_lang={sma_long:.2f}"
    if not should_hold and holding:
        return "sell", -1.0, f"Death Cross: SMA_kurz={sma_short:.2f} <= SMA_lang={sma_long:.2f}"
    return "hold", 0.0, f"kein Crossover (SMA_kurz={sma_short:.2f}, SMA_lang={sma_long:.2f}, halten={holding})"


# --- Live-Signalquellen: HTTP/DB-Beschaffung + Aufruf der Formeln oben --


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
    confidence, reason = momentum_confidence(prices, cfg.strategy.momentum_short_window)
    direction = "long" if confidence > 0 else ("short" if confidence < 0 else "neutral")

    db.record_signal(
        cfg.db_path,
        source="momentum",
        symbol=cfg.symbol,
        direction=direction,
        confidence=confidence,
        reason=reason,
    )


def generate_feargreed_signal(cfg: Config) -> None:
    """Zweite Signalquelle: Crypto Fear & Greed Index (alternative.me), als
    Kontra-Indikator interpretiert (Angst -> Kauf-Tendenz, Gier -> Vorsicht).

    Der Index aendert sich nur einmal taeglich, daher wird er hoechstens alle
    `feargreed_refresh_seconds` neu abgefragt statt bei jedem Poll-Zyklus.
    """
    last_ts = db.get_latest_signal_ts(cfg.db_path, "sentiment:feargreed", cfg.symbol)
    if last_ts is not None and time.time() - last_ts < cfg.strategy.feargreed_refresh_seconds:
        return

    try:
        with urllib.request.urlopen(FEARGREED_URL, timeout=10) as resp:
            payload = json.load(resp)
        entry = payload["data"][0]
        value = float(entry["value"])
        classification = entry.get("value_classification", "")
    except Exception:
        log.warning("Fear&Greed-Abruf fehlgeschlagen, ueberspringe diesen Zyklus", exc_info=True)
        return

    confidence, reason = feargreed_confidence(value, classification)
    direction = "long" if confidence > 0 else ("short" if confidence < 0 else "neutral")

    db.record_signal(
        cfg.db_path,
        source="sentiment:feargreed",
        symbol=cfg.symbol,
        direction=direction,
        confidence=confidence,
        reason=reason,
    )


def generate_funding_signal(cfg: Config) -> None:
    """Dritte Signalquelle: Perpetual-Funding-Rate von Bitget (BTCUSDT) als
    Positionierungs-Indikator. Stark positive Funding Rate heisst viele
    ueberhebelte Longs -> Vorsicht; negativ/neutral heisst eher unaufgeregt
    bis Short-lastig -> spricht eher fuer Kauf.
    """
    last_ts = db.get_latest_signal_ts(cfg.db_path, "derivatives:funding", cfg.symbol)
    if last_ts is not None and time.time() - last_ts < cfg.strategy.funding_refresh_seconds:
        return

    try:
        with urllib.request.urlopen(FUNDING_URL, timeout=10) as resp:
            payload = json.load(resp)
        funding_rate = float(payload["data"][0]["fundingRate"])
    except Exception:
        log.warning("Funding-Rate-Abruf fehlgeschlagen, ueberspringe diesen Zyklus", exc_info=True)
        return

    confidence, reason = funding_confidence(funding_rate)
    direction = "long" if confidence > 0 else ("short" if confidence < 0 else "neutral")

    db.record_signal(
        cfg.db_path,
        source="derivatives:funding",
        symbol=cfg.symbol,
        direction=direction,
        confidence=confidence,
        reason=reason,
    )


def generate_trend_signal(cfg: Config, exchange) -> None:
    """Vierte Signalquelle: echter 200-Tage-Trend auf Tageskerzen (nicht zu
    verwechseln mit dem kurzfristigen SMA5/SMA20-Momentum oben). Nutzt die
    schon bestehende ccxt-Exchange-Instanz, um keine zweite Verbindung
    aufzubauen.
    """
    last_ts = db.get_latest_signal_ts(cfg.db_path, "trend:ma200", cfg.symbol)
    if last_ts is not None and time.time() - last_ts < cfg.strategy.trend_refresh_seconds:
        return

    try:
        candles = exchange.fetch_ohlcv(cfg.symbol, timeframe="1d", limit=200)
    except Exception:
        log.warning("MA200-Tageskerzen-Abruf fehlgeschlagen, ueberspringe diesen Zyklus", exc_info=True)
        return

    if len(candles) < 200:
        return  # noch nicht genug Handelshistorie fuer ein echtes MA200

    closes = [c[4] for c in candles]
    ma200 = sum(closes) / len(closes)
    current = closes[-1]

    confidence, reason = trend_confidence(current, ma200)
    direction = "long" if confidence > 0 else ("short" if confidence < 0 else "neutral")

    db.record_signal(
        cfg.db_path,
        source="trend:ma200",
        symbol=cfg.symbol,
        direction=direction,
        confidence=confidence,
        reason=reason,
    )


def generate_macro_dxy_signal(cfg: Config) -> None:
    """Fuenfte Signalquelle: US-Dollar-Index (DXY) ueber Yahoo Finance, als
    Makro-Gegenindikator - ein staerker werdender Dollar gilt historisch als
    bearish fuer Bitcoin (Risk-off), ein schwaecher werdender als bullish.
    FRED blockt unsere VM-IP (siehe Recherche), Yahoo Finance ist frei erreichbar.
    """
    last_ts = db.get_latest_signal_ts(cfg.db_path, "macro:dxy", cfg.symbol)
    if last_ts is not None and time.time() - last_ts < cfg.strategy.macro_refresh_seconds:
        return

    try:
        req = urllib.request.Request(DXY_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        result = payload["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    except Exception:
        log.warning("DXY-Abruf fehlgeschlagen, ueberspringe diesen Zyklus", exc_info=True)
        return

    if len(closes) < 20:
        return  # noch nicht genug Handelstage fuer eine 20-Tage-Basislinie

    sma20 = sum(closes[-20:]) / 20
    current = closes[-1]

    confidence, reason = dxy_confidence(current, sma20)
    direction = "long" if confidence > 0 else ("short" if confidence < 0 else "neutral")

    db.record_signal(
        cfg.db_path,
        source="macro:dxy",
        symbol=cfg.symbol,
        direction=direction,
        confidence=confidence,
        reason=reason,
    )


def decide(cfg: Config, lookback_seconds: float = 3600.0) -> tuple[str, float, str]:
    """Aggregiert alle Signale der letzten `lookback_seconds` zu einer
    Handelsentscheidung: ("buy" | "sell" | "hold", score, begruendung).

    Wird zuerst pro Quelle gemittelt und danach ueber die Quellen gemittelt
    (statt ueber alle einzelnen Zeilen), damit eine haeufig schreibende Quelle
    (z.B. momentum alle 60s) nicht automatisch mehr Gewicht bekommt als eine
    seltene (z.B. trend:ma200 alle 6h).
    """
    since = time.time() - lookback_seconds
    signals = db.get_recent_signals(cfg.db_path, cfg.symbol, since)
    if not signals:
        return "hold", 0.0, "keine Signale im Betrachtungszeitraum"

    confidences_by_source: dict[str, list[float]] = {}
    for s in signals:
        confidences_by_source.setdefault(s["source"], []).append(s["confidence"])

    source_scores = {
        source: sum(confs) / len(confs) for source, confs in confidences_by_source.items()
    }
    return aggregate_scores(
        source_scores, cfg.strategy.buy_confidence_threshold, cfg.strategy.sell_confidence_threshold
    )


def decide_golden_cross(cfg: Config, exchange) -> tuple[str, float, str]:
    """Alternative zu decide(): ignoriert die 5 Signalquellen komplett und
    handelt rein nach SMA-Crossover (siehe strategy.mode in config.yaml).
    """
    long_window = cfg.strategy.golden_cross_long_window
    short_window = cfg.strategy.golden_cross_short_window

    try:
        candles = exchange.fetch_ohlcv(cfg.symbol, timeframe="1d", limit=long_window + 5)
    except Exception:
        log.warning("Golden-Cross-Tageskerzen-Abruf fehlgeschlagen, halte diesen Zyklus", exc_info=True)
        return "hold", 0.0, "Tageskerzen-Abruf fehlgeschlagen"

    if len(candles) < long_window:
        return "hold", 0.0, f"noch nicht genug Handelshistorie fuer SMA{long_window}"

    closes = [c[4] for c in candles]
    sma_short = sum(closes[-short_window:]) / short_window
    sma_long = sum(closes[-long_window:]) / long_window
    holding = db.get_portfolio_state(cfg.db_path)["btc"] > 0

    return golden_cross_decision(sma_short, sma_long, holding)
