# Your first five minutes with PSX Lens

1. **Start it.** Open this folder in VS Code. Choose Terminal → New Terminal and run `python3 server.py`. Open http://localhost:8765. Keep the terminal running; the updater stops when the Python process or computer stops.
2. **Check the status.** The banner says whether pyPSX reports the market open. Each stock shows its latest candle time. Recent candles during market hours enable current guidance. Closed-market prices are last-session snapshots. A failed or stale feed means wait.
3. **Choose a stock.** Start with one of BOP, MEBL, HUBC, OGDC, LUCK, or ENGROH. Click its row or Review plan. Add other valid PSX tickers using Watch another stock. The app monitors your watchlist, not the entire exchange.
4. **Read the next step.** Review entry setup means inspect bullish evidence; Wait means mixed evidence; Avoid new entry means weak conditions for a new long trade. None is an instruction to place an automatic order. Already-owning guidance is separate because your real entry and stop are unknown to this app.
5. **Calculate risk and practise.** Read the entry zone, model stop, and targets. Set planning capital and risk, then calculate shares. When freshness checks pass, try a paper position. Fees and slippage apply; gaps can exceed the planned loss. Confirm any real quote and liquidity with your broker.

Use **Beginner guide** for plain-language definitions. Use **Data connections** to check source, errors, or request a refresh. Use **Strategy lab** to inspect historical results. A high setup score is not a win probability, and the rules have not demonstrated profitable PSX performance.

## What is automatic?

The local server requests pyPSX market status and candles about every minute while the provider reports open, and about every five minutes while closed. The browser rereads local state every ten seconds. This is completed-candle polling, not tick streaming. A paper API key is used only to read data; no pyPSX orders, broker accounts, or real funds are accessed.

## What if something looks wrong?

- **Market closed:** prepare a plan; wait for current candles after opening.
- **Delayed / stale:** don't use the old setup as a current entry. Check Data connections.
- **Update failed:** the last successful candles remain labeled, but current guidance and paper entries pause.
- **Unavailable:** check the ticker. The old ENGRO symbol did not return usable history in testing; ENGROH is the default Engro holding-company ticker here.
- **Local connection lost:** restart the Python server, then reload the browser.
- **Port already in use:** use the existing running app, or stop that instance before restarting. For another port run `python3 server.py --port 8766` and visit http://localhost:8766.

Your pyPSX credentials belong in the local `.env` file, never in screenshots or chat.

## Compare timeframes

Open **Timeframe analysis** in the sidebar. Choose a watched stock, then 3M / 1M / 5D for how much history to show. Leave candle interval on Auto, or select 4H / 15 min independently. Use longer windows for context, then shorter candles to review timing. Daily and 4H views are research context, not current scalp instructions.

Read coverage warnings first. Known provider gaps shorten history and reset indicators. When fewer than 35 candles remain, the chart is available but the model withholds scores and levels. The volume balance is a candle-based estimate, not actual buying/selling volume. Order flow and market depth are currently unconnected.
