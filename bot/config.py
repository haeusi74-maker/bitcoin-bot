from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class PortfolioConfig:
    starting_cash: float
    max_trade_fraction: float
    min_cooldown_seconds: int


@dataclass
class StrategyConfig:
    momentum_short_window: int
    momentum_long_window: int
    buy_confidence_threshold: float
    sell_confidence_threshold: float


@dataclass
class Config:
    exchange: str
    symbol: str
    poll_interval_seconds: int
    db_path: str
    portfolio: PortfolioConfig
    strategy: StrategyConfig


def load_config(path: str = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Config(
        exchange=raw["exchange"],
        symbol=raw["symbol"],
        poll_interval_seconds=int(raw["poll_interval_seconds"]),
        db_path=raw["database"]["path"],
        portfolio=PortfolioConfig(**raw["portfolio"]),
        strategy=StrategyConfig(**raw["strategy"]),
    )
