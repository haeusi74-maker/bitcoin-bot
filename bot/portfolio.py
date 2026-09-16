from __future__ import annotations

import time

from . import db
from .config import Config


def execute_decision(cfg: Config, decision: str, score: float, reason: str, price: float) -> None:
    """Fuehrt eine Paper-Trading-Entscheidung aus: passt nur die virtuelle
    Portfolio-Tabelle in der DB an, es wird nie eine echte Order geschickt.
    Enthaelt einfaches Risikomanagement: Cooldown zwischen Trades + Cap auf
    den Anteil des Portfoliowerts pro Trade.
    """
    state = db.get_portfolio_state(cfg.db_path)
    cash, btc = state["cash"], state["btc"]
    last_trade_ts = state["last_trade_ts"]
    now = time.time()

    if decision == "hold":
        return

    if last_trade_ts is not None and (now - last_trade_ts) < cfg.portfolio.min_cooldown_seconds:
        return  # Cooldown aktiv, kein Trade

    portfolio_value = cash + btc * price
    max_trade_value = portfolio_value * cfg.portfolio.max_trade_fraction

    if decision == "buy":
        trade_value = min(cash, max_trade_value)
        if trade_value <= 0:
            return
        amount_btc = trade_value / price
        cash -= trade_value
        btc += amount_btc
        db.record_trade(cfg.db_path, "buy", price, amount_btc, cash, btc, f"{reason} (score={score:.2f})", ts=now)
        db.update_portfolio_state(cfg.db_path, cash, btc, now)

    elif decision == "sell":
        btc_value = btc * price
        trade_value = min(btc_value, max_trade_value)
        if trade_value <= 0:
            return
        amount_btc = trade_value / price
        btc -= amount_btc
        cash += trade_value
        db.record_trade(cfg.db_path, "sell", price, amount_btc, cash, btc, f"{reason} (score={score:.2f})", ts=now)
        db.update_portfolio_state(cfg.db_path, cash, btc, now)
