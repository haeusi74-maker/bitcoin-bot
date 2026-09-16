from __future__ import annotations

import time

from . import db
from .config import Config


def check_exit_trigger(
    avg_entry_price: float, current_price: float, stop_loss_pct: float, take_profit_pct: float
) -> str | None:
    """Reine Funktion (kein DB-Zugriff), damit bot/backtest.py exakt dieselbe
    Stop-Loss/Take-Profit-Regel auf historischen Daten anwenden kann.
    Liefert "stop_loss", "take_profit" oder None.
    """
    if avg_entry_price <= 0:
        return None
    change = (current_price - avg_entry_price) / avg_entry_price
    if change <= -stop_loss_pct:
        return "stop_loss"
    if change >= take_profit_pct:
        return "take_profit"
    return None


def check_stop_loss_take_profit(cfg: Config, price: float) -> bool:
    """Notausstieg, unabhaengig vom Signal-Score: verkauft die GESAMTE
    Position sofort, wenn Stop-Loss oder Take-Profit erreicht ist. Ignoriert
    bewusst Cooldown und max_trade_fraction - das sind Regeln fuer normale,
    signalgetriebene Trades, kein Risikomanagement-Override.

    Liefert True, wenn ein Exit ausgefuehrt wurde (der Aufrufer sollte dann
    die normale decide()/execute_decision()-Entscheidung fuer diesen Zyklus
    ueberspringen, um kein sofortiges Wieder-Einsteigen im selben Tick).
    """
    state = db.get_portfolio_state(cfg.db_path)
    cash, btc, avg_entry_price = state["cash"], state["btc"], state["avg_entry_price"]
    if btc <= 0:
        return False

    trigger = check_exit_trigger(
        avg_entry_price, price, cfg.portfolio.stop_loss_pct, cfg.portfolio.take_profit_pct
    )
    if trigger is None:
        return False

    now = time.time()
    fee = btc * price * cfg.portfolio.fee_rate
    cash += btc * price - fee
    label = "Stop-Loss" if trigger == "stop_loss" else "Take-Profit"
    reason = f"{label} ausgeloest (Einstieg {avg_entry_price:.2f}, aktuell {price:.2f})"
    db.record_trade(cfg.db_path, "sell", price, btc, cash, 0.0, reason, fee=fee, ts=now)
    db.update_portfolio_state(cfg.db_path, cash, 0.0, now, avg_entry_price=0.0)
    return True


def execute_decision(cfg: Config, decision: str, score: float, reason: str, price: float) -> None:
    """Fuehrt eine Paper-Trading-Entscheidung aus: passt nur die virtuelle
    Portfolio-Tabelle in der DB an, es wird nie eine echte Order geschickt.
    Enthaelt einfaches Risikomanagement: Cooldown zwischen Trades + Cap auf
    den Anteil des Portfoliowerts pro Trade.
    """
    state = db.get_portfolio_state(cfg.db_path)
    cash, btc, avg_entry_price = state["cash"], state["btc"], state["avg_entry_price"]
    last_trade_ts = state["last_trade_ts"]
    now = time.time()

    if decision == "hold":
        return

    if last_trade_ts is not None and (now - last_trade_ts) < cfg.portfolio.min_cooldown_seconds:
        return  # Cooldown aktiv, kein Trade

    portfolio_value = cash + btc * price
    max_trade_value = portfolio_value * cfg.portfolio.max_trade_fraction

    fee_rate = cfg.portfolio.fee_rate

    if decision == "buy":
        trade_value = min(cash, max_trade_value)
        if trade_value <= 0:
            return
        fee = trade_value * fee_rate
        amount_btc = (trade_value - fee) / price
        new_btc = btc + amount_btc
        # mengengewichteter Einstiegspreis ueber alle bisherigen + diesen Kauf
        avg_entry_price = (avg_entry_price * btc + price * amount_btc) / new_btc if new_btc > 0 else 0.0
        cash -= trade_value  # Gebuehr steckt in trade_value, geht nicht in BTC
        btc = new_btc
        db.record_trade(
            cfg.db_path, "buy", price, amount_btc, cash, btc,
            f"{reason} (score={score:.2f})", fee=fee, ts=now,
        )
        db.update_portfolio_state(cfg.db_path, cash, btc, now, avg_entry_price)

    elif decision == "sell":
        btc_value = btc * price
        trade_value = min(btc_value, max_trade_value)
        if trade_value <= 0:
            return
        amount_btc = trade_value / price
        fee = trade_value * fee_rate
        btc -= amount_btc
        cash += trade_value - fee  # Gebuehr wird vom Erloes abgezogen
        if btc < 1e-9:
            btc = 0.0
            avg_entry_price = 0.0  # Position komplett zu, Einstiegspreis verfaellt
        db.record_trade(
            cfg.db_path, "sell", price, amount_btc, cash, btc,
            f"{reason} (score={score:.2f})", fee=fee, ts=now,
        )
        db.update_portfolio_state(cfg.db_path, cash, btc, now, avg_entry_price)
