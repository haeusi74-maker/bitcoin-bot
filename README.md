# Bitcoin Paper-Trading Bot (Simulation)

MVP-Grundgeruest: holt echte Live-Preise, generiert Handelssignale und fuehrt
darauf basierend **virtuelle** (Paper-)Trades aus. Es wird niemals eine echte
Order verschickt - alles laeuft nur gegen eine lokale SQLite-Datenbank.

## Architektur

```
bot/price_feed.py   -> holt Preise via ccxt (oeffentliche API, kein Key noetig)
bot/strategy.py     -> generiert Signale (Platzhalter: SMA-Crossover) und
                        aggregiert alle Signale zu einer Kauf/Verkauf-Entscheidung
bot/portfolio.py     -> fuehrt die Entscheidung als Paper-Trade aus (Risiko-
                        Limits: max. Anteil pro Trade, Cooldown)
bot/db.py            -> SQLite-Speicher fuer Preise, Signale, Trades, Portfolio
dashboard/app.py     -> Streamlit-Dashboard: Portfolio-Wert vs. Buy&Hold
```

Die `signals`-Tabelle ist bewusst der Erweiterungspunkt: jede zukuenftige
Quelle (Discord-Kanal, Twitter/X-Account, News-Feed, ...) schreibt einfach
weitere Zeilen mit einem eigenen `source`-Namen (z.B. `discord:mo`) hinein.
`strategy.decide()` aggregiert automatisch alle Quellen konfidenz-gewichtet -
an der Entscheidungslogik muss dafuer nichts geaendert werden.

## Setup

```bash
cd bitcoin_bot
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

## Bot starten (Simulation)

```bash
python -m bot.main
```

Laeuft dauerhaft im konfigurierten Intervall (`config.yaml`,
`poll_interval_seconds`). Mit `--once` nur ein einzelner Zyklus zum Testen.

## Dashboard starten

In einem zweiten Terminal (bei laufendem Bot):

```bash
streamlit run dashboard/app.py
```

Zeigt Portfolio-Wert vs. Buy&Hold, Preisverlauf, Trade-Log und Signal-Log.

## Konfiguration

Alles in `config.yaml`: Exchange, Handelspaar, Poll-Intervall, Startkapital,
Risiko-Limits (`max_trade_fraction`, `min_cooldown_seconds`), Strategie-
Parameter. Keine API-Keys noetig, solange nur simuliert wird (Preisdaten
sind oeffentlich).

## Naechste Schritte (noch nicht umgesetzt)

1. **Laenger laufen lassen** (Wochen/Monate, verschiedene Marktphasen), um zu
   sehen ob die Platzhalter-Strategie ueberhaupt einen Edge hat, bevor
   weitere Quellen dazukommen.
2. **Discord-Collector**: eigener Bot-Account (kein Self-Bot!) in Servern, in
   denen du Mitglied/Admin bist, liest Nachrichten aus bestimmten Kanaelen,
   extrahiert Signale (z.B. per LLM) und schreibt sie in `signals` mit
   `source="discord:<kanalname>"`.
3. **Weitere Quellen** nach demselben Muster: Twitter/X, Telegram, News-RSS.
4. **Deployment auf Proxmox**: LXC-Container, Bot + Dashboard als systemd-
   Services oder Docker-Compose-Stack, SQLite reicht fuer den Anfang, spaeter
   ggf. auf Postgres/TimescaleDB wechseln wenn mehrere Prozesse parallel
   schreiben.
5. **Live-Trading** (erst nach ausfuehrlicher Simulation!): `bot/portfolio.py`
   um einen echten Executor via `ccxt` ergaenzen, der mit API-Keys ohne
   Withdrawal-Recht auf einer Exchange (z.B. Kraken) tatsaechliche Orders
   platziert. Simulation und Live sollten dieselbe `strategy.decide()`-Logik
   nutzen, nur der letzte Schritt unterscheidet sich.
