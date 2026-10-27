# Psx-Lens

# PSX Lens — automatic pyPSX research workspace

A local, single-user PSX decision-support app with an actual-data watchlist, beginner guidance, risk planning, paper trades, and historical backtesting. The strategy remains unvalidated. No broker orders are sent.

Start with [GET_STARTED.md](GET_STARTED.md), or open **Beginner guide** in the app.

## Run in VS Code

Python 3.10+; no third-party packages or build step required.

```sh
cd /Users/hannan/Documents/Codex/2026-10-11/we-x20/outputs/psx-assistant
python3 server.py
```

Open http://localhost:8765. Keep the Python process running for automatic updates. Ctrl+C stops it. The macOS launcher `Start PSX Lens.command` starts the same server. If another instance already owns the port, use it or stop it first. `--port 8766` selects another port.

This is a loopback-only local app, not a hosted service. It does not keep updating while your computer or Python process is off. Runtime data is stored in `data/workspace.sqlite3`. `PSX_DB` can select a different workspace database.

## pyPSX credentials

Copy `.env.example` to `.env` beside `server.py`, and enter the key ID and secret locally. Use `chmod 600 .env` to restrict access. Environment variables override this file.

- `PYPSX_ENVIRONMENT=paper` uses https://paper-api.pypsx.com.
- `PYPSX_API_KEY_ID` and `PYPSX_API_SECRET_KEY` stay server-side.
- The live host is supported only for read-only data requests when explicitly configured with an appropriate key; no account/order endpoint is implemented.

Existing credentials were configured by the owner. Do not paste keys into chat or put them in source packages. `.env` and runtime data are excluded from the ZIP.

Provider reference: https://docs.pypsx.com/api-reference/ and https://docs.pypsx.com/market-hours/. Confirm any account-specific entitlements, costs, and usage rights with pyPSX. A successful request is not independent verification of market accuracy.

## Automatic ingestion

`market_service.py` starts a background thread when `server.py` starts. It requests `/market/status` and reads each active watchlist symbol's finalized candles through `/api/v1/market/historical-intraday` (or `/historical/{symbol}` for daily data).

- About 60 seconds between open-market cycles; 300 seconds when closed, missing credentials, or failing. Network work adds time to each cycle.
- Requests run outside the database lock; each symbol applies atomically so the UI remains responsive during network waits.
- Manual refresh requests coalesce. A 30-second minimum between cycles and a five-minute rate-limit cooldown limit repeated requests.
- Up to 20 stocks. Defaults: BOP, MEBL, HUBC, OGDC, LUCK, ENGROH; 20 calendar days of five-minute candles.
- Default ENGROH was verified against pyPSX; the earlier ENGRO ticker returned insufficient data. No silent symbol fallback is performed for added stocks.
- Synthetic prototype datasets are not seeded in normal mode and are excluded from normal watchlist prices, current guidance, backtest selection, and alerts. Old records remain stored so earlier paper history is not destroyed. `init(demo=True)` is only used in isolated tests.
- No synthetic fallback on API failures. Unsupported symbols show unavailable. Errors preserve prior provider data, explicitly pause current guidance, and appear under Data connections.
- Missing/invalid OHLCV, duplicate timestamps, provider-reported gaps, insufficient warmup, backwards history, and unsupported schemas are rejected. Unfinished candles are conservatively omitted.
- Source/interval changes are blocked with an open paper position. Same-source updates process unseen bars in timestamp order, once each, checking paper stops/targets and recording category transitions. Initial history is a baseline, not replayed trades.
- Revisions with an open position and histories that no longer overlap its period are blocked for review. With no position, revised history establishes a fresh baseline. A long offline period with no overlap also establishes a fresh warmup baseline.

## Freshness and market-state gates

`availability()` determines whether a current signal may be shown. These checks apply server-side to paper entries, as well as to the UI.

- Data must be labeled by the pyPSX adapter; CSV/synthetic prices cannot become current watchlist data.
- Provider status must explicitly report `demo_mode=false` and `override_mode=AUTO`. Unknown or forced/demo status disables guidance.
- Open-market status expires after three minutes. Closed-market status expires after ten minutes; it only supports historical planning, never current entries.
- During trading, the latest candle must be at most max(15 minutes, 3× its interval) old, and successful download at most three minutes old. Future timestamps are rejected for current use.
- Closed-market candles remain labeled as last-session snapshots. Candles more than four days old are marked outdated. Holidays and unexpected closures rely on provider status; this app does not claim its own authoritative exchange calendar.
- Daily data supports historical research only; intraday VWAP and order-book factors are excluded. Daily date labels are not executable trade times.
- Loss of browser-to-server connectivity visibly pauses entry guidance. Data timestamps and provider errors are shown per stock.

Thresholds are conservative heuristics. Freshness checks do not certify that a quote is executable, that coverage is complete, or that a provider has no delay. Thinly traded stocks can remain unavailable because no recent candle exists.

## Beginner UI

**Stock plans** shows the next step, last candle time, rule score, plain-language reasons, and a reference risk plan. Current actions are Review entry setup, Wait, and Avoid new entry. Stale/closed/missing data replaces these with a wait state. The interface separately explains what a new buyer and an existing holder should review.

**Beginner guide** explains startup, freshness, score meanings, entry zones, stops, targets, position sizing, and paper trading. A glossary explains EMA, RSI, volume, estimated VWAP, ATR, and support/resistance. It does not know real broker holdings, personal finances, or an existing real stop.

**Data connections** shows background updater state and individual source errors. **Paper portfolio** records local modeled fills. **Strategy lab** evaluates historical candles. **Signal alerts** are timestamped historical transitions, not current trade instructions.

## Signal and risk methodology

`engine.py`: EMA 9/21; Wilder RSI 14; MACD 12/26/9; candle-based session VWAP estimate; Wilder ATR 14; latest volume relative to prior 20 bars; prior 20-bar support/resistance. Available weights: trend 25, volume 15, VWAP 15, momentum 20, breakout 10, depth 15. This integration fetches no depth; its factor is excluded, with the remaining score normalized.

Thresholds: ≥80 strong setup, ≥65 buy watch, ≥45 wait, ≥30 exit watch, otherwise bearish. These remain unvalidated research rules, not probabilities of profit. Historical scores remain visibly dated when entry guidance is paused.

Stop distance: max(1.5×ATR, 0.5% of reference price). Targets: 2R and 3R. Share sizing is capped by cash and risk inclusive of configured costs. Paper sizing includes modeled exit slippage. Gaps can exceed the planned loss. Portfolio correlation, aggregate loss caps, lot sizes, exchange price limits, liquidity, spread, corporate actions, market impact, and actual broker tariffs are not fully modeled.

For someone who already owns a stock, newly calculated model levels must not replace their existing risk plan. This app does not read real holdings.

## Paper trading and backtests

Paper opens use the last stored candle close plus slippage and require fresh provider data during an open market. They do not submit any pyPSX order. Newly downloaded bars check stops/targets; checks are at candle resolution, not continuous. Older bars and duplicate refreshes do not re-fill positions. Manual paper exits use the stored close and remain explicitly modeled.

Backtests use close-based signals executed at next-bar open, per-side fees/slippage, conservative stop-first handling when both stop and target touch, gap-aware stops, one long position, no leverage, and end-of-sample liquidation. Warmup is 35 candles; features use up to 301 bars. At least 70 candles are required. Drawdown uses modeled liquidation equity at candle closes.

There is no corporate-action verification, benchmark comparison, holdout validation, walk-forward testing, regime analysis, or calibrated probability model. Historical results do not establish future profitability.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

57 tests cover calculation/accounting, imports, causal backtests, provider response normalization, secret redaction, source and freshness gates, demo/override rejection, failed-sync preservation, hidden synthetic records, incremental paper stop processing, duplicate refreshes, corrections, and recovery after long downtime. Network responses are mocked in unit tests; separate actual-provider checks verified all six defaults on 19 October 2026.

The actual provider reports the market closed during verification; all visible watchlist datasets came from pyPSX, with candles ending 16 October. An open-market streaming session was not observed end-to-end during this run; open-market freshness/entry behavior was tested with controlled fixtures. This app uses polling, not WebSocket tick streaming.

A pre-upgrade database backup was saved in the task's `work/` folder. The source archive excludes credentials, runtime databases, and Python caches.

## Still outside scope

Tick-by-tick feed and live order-book analysis, independently verified data quality/entitlements, validated strategy performance, real holdings and broker execution, LLM chat/news summaries, external notifications, production accounts/authentication and hosted deployment. Never expose this loopback server through a public tunnel.

## Interactive stock chart

The stock plan uses locally bundled TradingView Lightweight Charts 5.0.9 (Apache 2.0, with attribution). It displays the 120 most recent loaded OHLCV candles, volume, and EMA 9/21 calculated from that displayed history. The model uses longer history, so initial chart averages can differ. Times display in Asia/Karachi. Recent/All loaded, zoom/pan, optional model levels/markers/averages, and expanded view are available. Zoom is retained across background refreshes.

Blue lines bound the latest entry review zone; red marks the model stop; green marks the 2R/3R targets. Shading projects from the latest candle, not historical trades. All levels are references when freshness checks fail or the market closes. Targets and model levels update with candles, not a user’s fixed trade plan.

Setup arrows come only from recorded threshold transitions within the loaded candles and matching source. Paper buy/sell arrows are recorded paper fills, not broker executions. Current review-entry markers require verified actionable data and a score of at least 65. Older events may be outside the 100 saved alerts or 120 displayed candles. No hindsight-generated historical trade calls are added.

Chart annotation checks: `node --test tests/test_chart_model.cjs`.

## Multi-timeframe analysis

Open **Timeframe analysis**. 3M / 1M / 5D are lookback windows of 90 / 30 / 5 calendar days. Candle interval is independent: Auto chooses daily / hourly / 15m respectively; 4H and 15 min overrides are available. Data downloads asynchronously and refreshes while that view is open. Its bounded in-memory cache and workers never replace the SQLite execution feed or modify paper positions.

4H is built from provider 15-minute bars, anchored 09:30 Mon–Thu and 09:15 / 14:30 Friday, Pakistan time. These are maximum 4-hour session buckets: closing bars and Friday sessions are shorter, lunch is never bridged, and unfinished buckets are excluded. A calendar schedule change such as Ramadan would require updating this aggregation convention.

Known provider history gaps reset the indicator warmup after the latest gap. A visible notice identifies excluded ranges and shortened coverage. Fewer than 35 completed candles still permit chart review but suppress model scores and risk levels. Daily and 4H views are research context; they do not assert a current scalp entry. The existing heuristic strategy is not separately validated for each horizon and does not forecast a price three months ahead.

Volume context shows volume-weighted candle close location, average shares and approximate daily value across the last 20 daily sessions. It is an explicitly labeled OHLCV proxy, not actual buy/sell delta, a liquidity guarantee or an institutional-flow detector. Order-flow depth and trade-side tape remain unconnected. pyPSX documents market-depth access, but standard SDK access has not been verified for this account; the documented broker routes returned HTTP 404 on the paper host. No broker order endpoints are called.

Additional checks: `python3 -m unittest tests/test_analysis_service.py` (or unittest discovery from the app directory).

EC2 deployment instructions are in [deploy/README.md](deploy/README.md).
