from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone

from . import portfolio, strategy
from .config import load_config

DAY_SECONDS = 86400

# Kraken/ccxt liefert oeffentlich nur die letzten ~720 Tageskerzen (siehe Recherche),
# das reicht nicht fuer Mehrjahres-Backtests. Yahoo Finance fuehrt BTC-EUR als
# regulaeres Ticker-Symbol mit >5 Jahren Tageshistorie - wird deshalb nur fuer den
# Backtest genutzt (der Live-Bot bezieht Preise weiterhin per ccxt von Kraken).
PRICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_}&interval=1d"
FEARGREED_URL = "https://api.alternative.me/fng/?limit={limit}&format=json"
DXY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range={range_}&interval=1d"
FUNDING_HISTORY_URL = (
    "https://api.bitget.com/api/v2/mix/market/history-fund-rate"
    "?symbol=BTCUSDT&productType=usdt-futures&pageSize=100&pageNo={page}"
)


def _day(ts: float) -> int:
    return int(ts) // DAY_SECONDS * DAY_SECONDS


def _yahoo_range_for(days_needed: int) -> str:
    years = days_needed / 365.25
    for threshold, label in [(1, "1y"), (2, "2y"), (5, "5y"), (10, "10y")]:
        if years <= threshold:
            return label
    return "max"


def fetch_price_history(symbol: str, days_needed: int) -> list[tuple[int, float]]:
    """Taegliche Schlusskurse von Yahoo Finance (z.B. "BTC/EUR" -> Ticker "BTC-EUR")."""
    ticker = symbol.replace("/", "-")
    range_ = _yahoo_range_for(days_needed)
    req = urllib.request.Request(
        PRICE_URL.format(symbol=ticker, range_=range_), headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    result = payload["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    pairs = [(_day(ts), c) for ts, c in zip(timestamps, closes) if c is not None]
    return pairs[-days_needed:] if len(pairs) > days_needed else pairs


def fetch_feargreed_history(limit: int) -> dict[int, float]:
    with urllib.request.urlopen(FEARGREED_URL.format(limit=limit), timeout=15) as resp:
        payload = json.load(resp)
    return {_day(int(e["timestamp"])): float(e["value"]) for e in payload["data"]}


def fetch_dxy_history(range_: str = "2y") -> dict[int, float]:
    req = urllib.request.Request(DXY_URL.format(range_=range_), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.load(resp)
    result = payload["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    return {_day(ts): close for ts, close in zip(timestamps, closes) if close is not None}


def fetch_funding_history(days_needed: int, max_pages: int = 10) -> dict[int, float]:
    """Bitget liefert ~3 Werte/Tag (alle 8h) in Seiten a 100, absteigend sortiert."""
    by_day: dict[int, list[float]] = {}
    pages_needed = min(max_pages, max(1, (days_needed * 3) // 100 + 1))
    for page in range(1, pages_needed + 1):
        with urllib.request.urlopen(FUNDING_HISTORY_URL.format(page=page), timeout=15) as resp:
            payload = json.load(resp)
        rows = payload.get("data", [])
        if not rows:
            break
        for row in rows:
            day = _day(int(row["fundingTime"]) / 1000)
            by_day.setdefault(day, []).append(float(row["fundingRate"]))
        time.sleep(0.2)
    return {day: sum(rates) / len(rates) for day, rates in by_day.items()}


def _value_as_of(sorted_days: list[int], series: dict[int, float], target_day: int) -> float | None:
    """Letzter bekannter Wert an oder vor target_day (Forward-Fill fuer Wochenenden etc.)."""
    latest = None
    for d in sorted_days:
        if d > target_day:
            break
        latest = d
    return series[latest] if latest is not None else None


def run_backtest(cfg, days: int, dca_interval_days: int = 7) -> dict:
    warmup = 200  # Tage fuer echtes MA200 (und ausreichend fuer SMA50 des Golden-Cross-Vergleichs)
    total_days = days + warmup + 5

    print(f"Lade {total_days} Tage {cfg.symbol}-Kerzen von Yahoo Finance...")
    candles = fetch_price_history(cfg.symbol, total_days)
    if len(candles) < warmup + days:
        raise RuntimeError(
            f"Nur {len(candles)} Tageskerzen verfuegbar, brauche {warmup + days} "
            f"({warmup} Tage MA200-Warmup + {days} Tage Backtest)."
        )

    print("Lade Fear&Greed-Historie (alternative.me)...")
    feargreed = fetch_feargreed_history(days + warmup)

    print("Lade DXY-Historie (Yahoo Finance)...")
    dxy = fetch_dxy_history(_yahoo_range_for(days + warmup))
    dxy_days = sorted(dxy)

    print("Lade Funding-Rate-Historie (Bitget, nur begrenzt verfuegbar)...")
    funding = fetch_funding_history(days + 10)
    funding_days = sorted(funding)

    day_ts_list = [c[0] for c in candles]
    prices = [c[1] for c in candles]

    start_index = len(candles) - days
    start_price = prices[start_index]
    baseline_btc = cfg.portfolio.starting_cash / start_price

    funding_coverage_days = 0
    if funding_days:
        funding_coverage_days = min(days, max(0, (day_ts_list[-1] - funding_days[0]) // DAY_SECONDS))

    cash = cfg.portfolio.starting_cash
    btc = 0.0
    avg_entry_price = 0.0
    fee_rate = cfg.portfolio.fee_rate
    max_trade_fraction = cfg.portfolio.max_trade_fraction
    stop_loss_pct = cfg.portfolio.stop_loss_pct
    take_profit_pct = cfg.portfolio.take_profit_pct

    # --- DCA-Vergleich: dasselbe Startkapital, aber in gleichen Portionen
    # ueber den Zeitraum verteilt investiert statt als Einmalanlage. ---
    num_intervals = max(1, (days + dca_interval_days - 1) // dca_interval_days)
    dca_amount_per_interval = cfg.portfolio.starting_cash / num_intervals
    dca_cash = cfg.portfolio.starting_cash
    dca_btc = 0.0
    dca_fees = 0.0

    # --- Golden-Cross-Vergleich: SMA50 kreuzt SMA200, binaer voll investiert
    # oder komplett in Cash - unabhaengig vom 5-Signal-Score des Bots. ---
    gc_cash = cfg.portfolio.starting_cash
    gc_btc = 0.0
    gc_fees = 0.0
    gc_trades = 0

    equity_curve = []
    buy_hold_curve = []
    dca_curve = []
    golden_cross_curve = []
    trade_log = []
    n_buys = n_sells = n_exits = 0
    total_fees = 0.0
    peak = cfg.portfolio.starting_cash
    max_drawdown = 0.0

    for i in range(start_index, len(candles)):
        day_ts = day_ts_list[i]
        price = prices[i]

        ma200_window = prices[i - 199 : i + 1]
        ma200 = sum(ma200_window) / len(ma200_window)
        sma50 = sum(prices[i - 49 : i + 1]) / 50

        # --- DCA: alle `dca_interval_days` einen festen Betrag investieren ---
        if (i - start_index) % dca_interval_days == 0 and dca_cash > 0:
            invest = min(dca_amount_per_interval, dca_cash)
            fee = invest * fee_rate
            dca_btc += (invest - fee) / price
            dca_cash -= invest
            dca_fees += fee

        # --- Golden Cross / Death Cross: dieselbe Regel wie im Live-Bot (strategy.mode=golden_cross) ---
        gc_decision, _, _ = strategy.golden_cross_decision(sma50, ma200, holding=gc_btc > 0)
        if gc_decision == "buy" and gc_cash > 0:
            fee = gc_cash * fee_rate
            gc_btc = (gc_cash - fee) / price
            gc_cash = 0.0
            gc_fees += fee
            gc_trades += 1
        elif gc_decision == "sell" and gc_btc > 0:
            proceeds = gc_btc * price
            fee = proceeds * fee_rate
            gc_cash = proceeds - fee
            gc_btc = 0.0
            gc_fees += fee
            gc_trades += 1

        # --- Haupt-Bot: Notausstieg hat Vorrang vor der normalen Entscheidung ---
        bot_handled_by_exit = False
        if btc > 0:
            trigger = portfolio.check_exit_trigger(avg_entry_price, price, stop_loss_pct, take_profit_pct)
            if trigger is not None:
                fee = btc * price * fee_rate
                cash += btc * price - fee
                total_fees += fee
                n_exits += 1
                label = "sell(stop_loss)" if trigger == "stop_loss" else "sell(take_profit)"
                trade_log.append((day_ts, label, price, btc, fee, None))
                btc = 0.0
                avg_entry_price = 0.0
                bot_handled_by_exit = True

        if not bot_handled_by_exit:
            window = prices[i - cfg.strategy.momentum_long_window + 1 : i + 1]
            momentum_conf, _ = strategy.momentum_confidence(window, cfg.strategy.momentum_short_window)
            trend_conf, _ = strategy.trend_confidence(price, ma200)

            fg_value = feargreed.get(day_ts)
            if fg_value is None:
                fg_value = _value_as_of(sorted(feargreed), feargreed, day_ts)
            fg_conf, _ = strategy.feargreed_confidence(fg_value) if fg_value is not None else (0.0, "")

            dxy_current = _value_as_of(dxy_days, dxy, day_ts)
            dxy_history_upto = [dxy[d] for d in dxy_days if d <= day_ts]
            if dxy_current is not None and len(dxy_history_upto) >= 20:
                dxy_sma20 = sum(dxy_history_upto[-20:]) / 20
                dxy_conf, _ = strategy.dxy_confidence(dxy_current, dxy_sma20)
            else:
                dxy_conf = 0.0

            funding_rate = _value_as_of(funding_days, funding, day_ts)
            funding_conf, _ = strategy.funding_confidence(funding_rate) if funding_rate is not None else (0.0, "")

            source_scores = {
                "momentum": momentum_conf,
                "trend:ma200": trend_conf,
                "sentiment:feargreed": fg_conf,
                "macro:dxy": dxy_conf,
                "derivatives:funding": funding_conf,
            }
            decision, score, _ = strategy.aggregate_scores(
                source_scores, cfg.strategy.buy_confidence_threshold, cfg.strategy.sell_confidence_threshold
            )

            portfolio_value = cash + btc * price
            max_trade_value = portfolio_value * max_trade_fraction

            if decision == "buy":
                trade_value = min(cash, max_trade_value)
                if trade_value > 0:
                    fee = trade_value * fee_rate
                    amount_btc = (trade_value - fee) / price
                    new_btc = btc + amount_btc
                    avg_entry_price = (
                        (avg_entry_price * btc + price * amount_btc) / new_btc if new_btc > 0 else 0.0
                    )
                    cash -= trade_value
                    btc = new_btc
                    total_fees += fee
                    n_buys += 1
                    trade_log.append((day_ts, "buy", price, amount_btc, fee, score))
            elif decision == "sell":
                btc_value = btc * price
                trade_value = min(btc_value, max_trade_value)
                if trade_value > 0:
                    fee = trade_value * fee_rate
                    amount_btc = trade_value / price
                    btc -= amount_btc
                    cash += trade_value - fee
                    if btc < 1e-9:
                        btc = 0.0
                        avg_entry_price = 0.0
                    total_fees += fee
                    n_sells += 1
                    trade_log.append((day_ts, "sell", price, amount_btc, fee, score))

        value = cash + btc * price
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak if peak > 0 else 0.0)

        equity_curve.append((day_ts, value))
        buy_hold_curve.append((day_ts, baseline_btc * price))
        dca_curve.append((day_ts, dca_cash + dca_btc * price))
        golden_cross_curve.append((day_ts, gc_cash + gc_btc * price))

    final_value = equity_curve[-1][1]
    final_buy_hold = buy_hold_curve[-1][1]
    final_dca = dca_curve[-1][1]
    final_golden_cross = golden_cross_curve[-1][1]

    return {
        "start_date": datetime.fromtimestamp(day_ts_list[start_index], tz=timezone.utc).date(),
        "end_date": datetime.fromtimestamp(day_ts_list[-1], tz=timezone.utc).date(),
        "days": days,
        "dca_interval_days": dca_interval_days,
        "starting_cash": cfg.portfolio.starting_cash,
        "final_value": final_value,
        "final_buy_hold": final_buy_hold,
        "final_dca": final_dca,
        "final_golden_cross": final_golden_cross,
        "return_pct": (final_value / cfg.portfolio.starting_cash - 1) * 100,
        "buy_hold_return_pct": (final_buy_hold / cfg.portfolio.starting_cash - 1) * 100,
        "dca_return_pct": (final_dca / cfg.portfolio.starting_cash - 1) * 100,
        "golden_cross_return_pct": (final_golden_cross / cfg.portfolio.starting_cash - 1) * 100,
        "dca_fees": dca_fees,
        "golden_cross_fees": gc_fees,
        "golden_cross_trades": gc_trades,
        "funding_coverage_days": funding_coverage_days,
        "max_drawdown_pct": max_drawdown * 100,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "n_exits": n_exits,
        "total_fees": total_fees,
        "trade_log": trade_log,
        "equity_curve": equity_curve,
        "buy_hold_curve": buy_hold_curve,
        "dca_curve": dca_curve,
        "golden_cross_curve": golden_cross_curve,
    }


def print_report(result: dict) -> None:
    print()
    print(f"Zeitraum: {result['start_date']} bis {result['end_date']} ({result['days']} Tage)")
    print(f"Startkapital: {result['starting_cash']:.2f} EUR")
    print()
    print(f"{'Bot (Simulation)':<20} Endwert {result['final_value']:>10.2f} EUR  "
          f"Rendite {result['return_pct']:>+7.2f}%")
    print(f"{'Buy & Hold':<20} Endwert {result['final_buy_hold']:>10.2f} EUR  "
          f"Rendite {result['buy_hold_return_pct']:>+7.2f}%")
    dca_label = f"DCA (alle {result['dca_interval_days']}d)"
    print(f"{dca_label:<20} Endwert {result['final_dca']:>10.2f} EUR  "
          f"Rendite {result['dca_return_pct']:>+7.2f}%")
    print(f"{'Golden Cross 50/200':<20} Endwert {result['final_golden_cross']:>10.2f} EUR  "
          f"Rendite {result['golden_cross_return_pct']:>+7.2f}%  "
          f"({result['golden_cross_trades']} Trades, Gebuehren {result['golden_cross_fees']:.2f} EUR)")
    print()
    print(f"Max Drawdown (Bot): {result['max_drawdown_pct']:.2f}%")
    print(f"Bot-Trades: {result['n_buys']} Buys, {result['n_sells']} Sells, "
          f"{result['n_exits']} Stop-Loss/Take-Profit-Exits, "
          f"Gebuehren gesamt: {result['total_fees']:.2f} EUR")
    if result["days"] > result["funding_coverage_days"]:
        print(
            f"Hinweis: Bitgets Funding-Rate-Historie deckt nur die letzten "
            f"{result['funding_coverage_days']} von {result['days']} Tagen ab (API-Limit). "
            f"Fuer den Rest zaehlt 'derivatives:funding' als neutral (0) im Score."
        )
    print()
    if result["trade_log"]:
        print("Trade-Log:")
        for day_ts, side, price, amount, fee, score in result["trade_log"]:
            date = datetime.fromtimestamp(day_ts, tz=timezone.utc).date()
            score_str = f"score={score:+.2f}" if score is not None else "Exit-Regel"
            print(f"  {date} {side:<16} {amount:.6f} BTC @ {price:>9.2f} EUR "
                  f"(fee={fee:.2f}, {score_str})")
    else:
        print("Keine Trades ausgeloest in diesem Zeitraum (Score hat nie eine Schwelle ueberschritten).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest der Signal-Strategie auf historischen Daten")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=180, help="Laenge des Backtest-Zeitraums in Tagen")
    parser.add_argument(
        "--buy-threshold", type=float, default=None,
        help="ueberschreibt buy_confidence_threshold aus der Config nur fuer diesen Lauf",
    )
    parser.add_argument(
        "--sell-threshold", type=float, default=None,
        help="ueberschreibt sell_confidence_threshold aus der Config nur fuer diesen Lauf",
    )
    parser.add_argument(
        "--dca-interval-days", type=int, default=7,
        help="Investitionsintervall fuer den DCA-Vergleich in Tagen (Standard: 7 = woechentlich)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.buy_threshold is not None:
        cfg.strategy.buy_confidence_threshold = args.buy_threshold
    if args.sell_threshold is not None:
        cfg.strategy.sell_confidence_threshold = args.sell_threshold

    print(
        f"Schwellen fuer diesen Lauf: buy>={cfg.strategy.buy_confidence_threshold:+.2f}  "
        f"sell<={cfg.strategy.sell_confidence_threshold:+.2f}"
    )
    result = run_backtest(cfg, args.days, dca_interval_days=args.dca_interval_days)
    print_report(result)


if __name__ == "__main__":
    main()
