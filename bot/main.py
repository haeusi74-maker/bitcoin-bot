from __future__ import annotations

import argparse
import logging
import time

from . import db, portfolio, strategy
from .config import load_config
from .price_feed import fetch_price, make_exchange

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bitcoin_bot")


def run_once(cfg, exchange) -> None:
    price = fetch_price(exchange, cfg.symbol)
    db.record_price(cfg.db_path, cfg.symbol, price)
    log.info("Preis %s = %.2f", cfg.symbol, price)

    strategy.generate_momentum_signal(cfg)

    decision, score, reason = strategy.decide(cfg)
    log.info("Entscheidung: %s (score=%.2f) - %s", decision, score, reason)

    portfolio.execute_decision(cfg, decision, score, reason, price)

    state = db.get_portfolio_state(cfg.db_path)
    value = state["cash"] + state["btc"] * price
    log.info(
        "Portfolio: cash=%.2f EUR, btc=%.6f, Gesamtwert=%.2f EUR",
        state["cash"], state["btc"], value,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitcoin Paper-Trading Bot (Simulation)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="nur einen Zyklus ausfuehren, dann beenden")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db.init_db(cfg.db_path, cfg.portfolio.starting_cash)
    exchange = make_exchange(cfg.exchange)

    log.info("Starte Simulation fuer %s auf %s (Poll-Intervall %ss)", cfg.symbol, cfg.exchange, cfg.poll_interval_seconds)

    if args.once:
        run_once(cfg, exchange)
        return

    while True:
        try:
            run_once(cfg, exchange)
        except Exception:
            log.exception("Fehler im Zyklus, mache trotzdem weiter")
        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
