from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import db  # noqa: E402
from bot.config import load_config  # noqa: E402

st.set_page_config(page_title="Bitcoin Paper-Trading", layout="wide")

CONFIG_PATH = "config.yaml"
cfg = load_config(CONFIG_PATH)

st.title("Bitcoin Paper-Trading Dashboard")
st.caption(f"Symbol: {cfg.symbol} | Exchange: {cfg.exchange} | Simulation (kein echtes Geld)")

price_rows = db.get_price_history(cfg.db_path, cfg.symbol, limit=5000)
trade_rows = db.get_trades(cfg.db_path, limit=500)
state = db.get_portfolio_state(cfg.db_path)

if not price_rows:
    st.warning("Noch keine Preisdaten. Starte zuerst den Bot mit `python -m bot.main`.")
    st.stop()

price_df = pd.DataFrame(price_rows, columns=["ts", "price"])
price_df["time"] = pd.to_datetime(price_df["ts"], unit="s")

current_price = price_df["price"].iloc[-1]
portfolio_value = state["cash"] + state["btc"] * current_price

# Buy-and-hold Vergleich: was waere das Startkapital wert, haette man am
# Anfang alles in BTC gesteckt und nie wieder angefasst.
start_price = price_df["price"].iloc[0]
baseline_btc = cfg.portfolio.starting_cash / start_price
price_df["buy_and_hold_value"] = baseline_btc * price_df["price"]

# Portfolio-Wert ueber Zeit rekonstruieren: fuer jeden Preis-Zeitpunkt den
# zu dem Zeitpunkt gueltigen cash/btc-Stand aus den Trades ableiten.
trades_df = pd.DataFrame(
    [dict(r) for r in trade_rows],
    columns=["ts", "side", "price", "amount_btc", "cash_after", "btc_after", "reason"],
).sort_values("ts")

cash = cfg.portfolio.starting_cash
btc = 0.0
portfolio_values = []
trade_idx = 0
trades_list = trades_df.to_dict("records")
for _, row in price_df.iterrows():
    while trade_idx < len(trades_list) and trades_list[trade_idx]["ts"] <= row["ts"]:
        cash = trades_list[trade_idx]["cash_after"]
        btc = trades_list[trade_idx]["btc_after"]
        trade_idx += 1
    portfolio_values.append(cash + btc * row["price"])
price_df["portfolio_value"] = portfolio_values

col1, col2, col3, col4 = st.columns(4)
col1.metric("Portfolio-Wert", f"{portfolio_value:,.2f} EUR")
col2.metric("Cash", f"{state['cash']:,.2f} EUR")
col3.metric("BTC-Bestand", f"{state['btc']:.6f} BTC")
col4.metric("Aktueller Preis", f"{current_price:,.2f} EUR")

fig = go.Figure()
fig.add_trace(go.Scatter(x=price_df["time"], y=price_df["portfolio_value"], name="Bot (Simulation)"))
fig.add_trace(go.Scatter(x=price_df["time"], y=price_df["buy_and_hold_value"], name="Buy & Hold"))
fig.update_layout(title="Portfolio-Wert: Bot vs. Buy & Hold", yaxis_title="EUR", height=450)
st.plotly_chart(fig, width="stretch")

price_fig = go.Figure()
price_fig.add_trace(go.Scatter(x=price_df["time"], y=price_df["price"], name=cfg.symbol))
price_fig.update_layout(title=f"{cfg.symbol} Preisverlauf", yaxis_title="EUR", height=300)
st.plotly_chart(price_fig, width="stretch")

st.subheader("Letzte Trades")
if trade_rows:
    show = pd.DataFrame([dict(r) for r in trade_rows])
    show["time"] = pd.to_datetime(show["ts"], unit="s")
    st.dataframe(
        show[["time", "side", "price", "amount_btc", "fee", "cash_after", "btc_after", "reason"]],
        width="stretch",
    )
else:
    st.info("Noch keine Trades ausgefuehrt.")

st.subheader("Letzte Signale")
signals = db.get_recent_signals(cfg.db_path, cfg.symbol, since_ts=0)[:200]
if signals:
    sig_df = pd.DataFrame([dict(r) for r in signals])
    sig_df["time"] = pd.to_datetime(sig_df["ts"], unit="s")
    st.dataframe(
        sig_df[["time", "source", "direction", "confidence", "reason"]],
        width="stretch",
    )
else:
    st.info("Noch keine Signale.")
