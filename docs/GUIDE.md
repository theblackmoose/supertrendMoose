# SupertrendMoose User Guide

Everything the [README](../README.md) leaves out: what each number on the dashboard means, what the filter profiles do and why, how positions and sizing work, and the API.

The README covers installing and running it. [DEPLOY.md](../DEPLOY.md) covers putting it on a server.

**Contents**

- [Reading the dashboard](#reading-the-dashboard)
- [Filter profiles](#filter-profiles)
- [The controls](#the-controls)
- [Positions](#positions)
- [Earnings tab](#earnings-tab)
- [Backtest](#backtest)
- [Tune](#tune)
- [Alerts](#alerts)
- [Time zone and schedule](#time-zone-and-schedule)
- [Ports](#ports)
- [API](#api)
- [Adding tickers](#adding-tickers)
- [Tests](#tests)
- [Moving to another host](#moving-to-another-host)
- [Performance notes](#performance-notes)

---

## Reading the dashboard

Every ticker you click shows a strip of numbers along the bottom. Here is what each one means and, more usefully, what to do with it.

### Risk to stop

How far the current price sits above the Supertrend stop, as a percentage. If price is 134.26 and the stop is 127.57, that is 5.2%.

This is your position sizing input, and it is the most immediately practical number on the strip. If you risk 1% of your account per trade and the stop is 5% away, the position is about 20% of your account. If the stop is 15% away, the same 1% risk buys you a position a third of the size.

**What to look for:** smaller is better for a new entry. Under about 6% is comfortable. Above 12% means either the trend has run a long way already or the stock is volatile enough that the ATR-based stop had to be set wide — in both cases you are late, or you need a much smaller position than feels natural. Widening your risk to accommodate a distant stop is how accounts get hurt.

### Trend age

How many trading days the current Supertrend direction has held.

A trend three days old and one ninety days old are completely different propositions even if every other number matches. The young one has most of its move potentially ahead of it and a stop close to price. The old one has already delivered much of its gain to whoever bought early, and its stop has trailed up a long way behind.

**What to look for:** for new entries, younger is better. Under about two weeks is where the risk-to-reward is most favourable. Beyond sixty days you are buying a mature trend — not automatically wrong, but you are paying more for less remaining move. For positions you already hold, age is not a reason to exit; the Supertrend flip is.

### Trend strength (ADX)

ADX measures how *directional* recent price action has been, on a scale that runs roughly 0 to 60. It says nothing about direction — a strong downtrend and a strong uptrend both produce a high ADX.

Below 20 generally means chop: price is moving, but not going anywhere. Supertrend performs badly in chop because it flips repeatedly, stopping you out on noise. That is exactly why the filter uses it.

**What to look for:** above your threshold (20 by default) for an entry. Between 20 and 25 is marginal. Above 30 is a genuine trend. Above 45 is very strong but often late — extreme readings tend to mark the end of moves rather than the beginning, so a high ADX paired with an old trend age is a warning, not an endorsement.

### Volume and earnings (price pane)

Volume is drawn along the bottom of the price pane, green on up days and red on down days, on its own scale.

An **E** badge sits at the bottom of the chart on each stored earnings report date; hover it for the date. A report dated on a weekend or holiday is marked on the next trading bar. A dashed **E** at the right edge is the next report, with the date and the number of trading days away on hover. It uses the same date as the *Next earnings* readout.

Badges only appear once earnings dates have been fetched (the header warns when they have not). Like the rest of the chart, they are display only.

Yahoo often posts an estimated report date and corrects it later. Each earnings refresh drops a stored date that Yahoo no longer lists, but only when Yahoo lists a different report within 45 days of it (the estimate moved). A date with no nearby replacement is kept, so a gap in Yahoo's data cannot delete a real report. The same rule clears *Next earnings* when a company reported earlier than its estimate, so the live blackout stops blocking entries for a report that has already happened. The refresh log and the `/api/refresh-earnings` result show how many dates were dropped (`pruned`).

### Momentum (MACD, optional pane)

The **MACD** button beside Position adds a 12/26/9 MACD pane under the ADX pane; click it again to hide it. It is off by default, and its data is only fetched while it is open (`GET /api/macd/{ticker}?timeframe=D|W`).

A **Daily / Weekly** selector appears beside the button while the pane is open, and your choice is kept for the browser session. Weekly is the default.

- **Daily** uses 12, 26 and 9 daily bars.
- **Weekly** matches TradingView's *1 week* timeframe with *Wait for timeframe closes* ticked. It uses weekly closes and completed weeks only. Each week's value appears on that week's last trading day, including a Thursday before Good Friday or a July 4 holiday, and holds until the next week closes. Mid-week you see last week's value, never one that can still change. The pane label shows which week that is. As on a TradingView weekly chart, it plots one point per week: the lines join week to week and the histogram shows one bar per week. A weekly MACD needs about 34 weeks of history before it draws.

The histogram is solid while it is growing and a pale tint while it is shrinking, the same shading TradingView uses.

It is **display only**. No filter profile, score, alert, backtest or tuning run reads it, so turning it on changes nothing the app decides. Treat it as context: a histogram that is shrinking while price still rises means the move is losing momentum, which matters most for late entries and for judging how much a wide Supertrend stop might give back.

### Volatility (ATR)

Average True Range as a percentage of price — the typical daily range. A 2% ATR means the stock moves about 2% on an average day.

This drives everything else indirectly. Your Supertrend stop is placed a multiple of ATR below price, so a high-ATR stock automatically gets a wider stop, a larger risk-to-stop figure, and therefore a smaller position.

**What to look for:** roughly 1.5% to 4% suits this method. Below that, signals lag badly because the stop barely moves. Above 6%, you will be shaken out by ordinary noise and the position size required to keep risk sane becomes very small. When ATR is high, resist the urge to tighten the stop manually — you would just be guaranteeing a stop-out.

### vs 200-day average (SMA)

Whether price is above or below its average close over the last 200 trading days, roughly ten months. The amber line on the chart.

It is the conventional dividing line between a long-term uptrend and a downtrend, and it carries some self-fulfilling weight because a great many funds and algorithms treat it the same way.

**What to look for:** "above" means the broader trend agrees with a long entry. "below" means you would be buying against the longer-term direction.

The caveat, which this project's own backtesting showed: because it averages ten months of data, it lags badly. Price has to rally a long way off a bottom before it climbs back above the line, so this gate blocks entries at exactly the moment the largest moves begin. It is good at keeping you out of chop and bad at letting you in early. That trade-off is why the filter is set per ticker rather than globally.

### Attention score

A 0–100 ranking of how much a ticker deserves a look right now. Three parts:

| Component | Weight | Full marks at | Zero at |
| --- | --- | --- | --- |
| Trend freshness | 40% | day 1 | day 60 |
| Risk to stop | 30% | 2% or tighter | 12% |
| Trend strength | 30% | 25 above threshold | at threshold |

Risk to stop keeps going **negative** beyond 12%, down to −100. Flooring it at zero made a 21% stop score the same as a 12% one, which understated how bad a distant stop is: it forces a tiny position and risks a large loss if hit. The total is still clamped to 0–100.

Only tickers in an uptrend that have cleared their filter are scored. Everything else shows a dash and the reason.

**What to look for:** it is a triage tool, not a forecast. A score of 80 means "young trend, tight stop, strong direction" — a good-looking setup today. It says nothing about whether the trade will work, and nothing about whether this method works on that ticker at all. That second question is what the Backtest tab answers, and the two can disagree sharply: a high-scoring ticker whose backtest loses to buy-and-hold is still a ticker that loses to buy-and-hold.

Use the score to decide what to examine first, then decide with the chart and the backtest.

### Verdict

A plain summary of the other numbers:

- **Buy signal today** — Supertrend flipped up on the most recent bar and the filter passed it. This is the only state that represents a new entry.
- **In trend, filter clear** — already in an uptrend that would still pass the filter. You would be holding, not buying. Entering here means accepting the wider risk-to-stop that comes with a trend already underway.
- **Rejected: \<reason\>** — a signal fired but the filter blocked it. The reason is stated. Worth reading rather than ignoring: over time these tell you what your filter is costing you.
- **Stand aside** — no signal, or the trend is down. Nothing to do.

### Putting it together

A strong candidate looks like: verdict says buy signal, trend age in single digits, risk to stop under 6%, ADX above 25 and rising, price above the 200-day average, ATR between 1.5% and 4%, no earnings within a week, and a backtest on that ticker showing the assigned profile actually has an edge.

You will rarely get all of that. The two worth being strictest about are risk to stop, because it determines how much you can lose, and the earnings date, because a stop cannot protect you against a gap.

---

## Filter profiles

Backtesting showed a blanket SMA200 + ADX filter is expectancy-neutral: it lifts win rate but blocks entries at trend inception, which is where the largest moves start. So each ticker gets its own profile:

| Profile | Rule | Notes |
| --- | --- | --- |
| `full` | close > SMA200 **and** ADX > threshold | Strictest. Blocks most trend-inception entries |
| `adx_only` | ADX > threshold | Demands an established trend at the moment one begins |
| `adx_rising` | ADX higher than 5 bars ago | Asks whether direction is *building* rather than built |
| `regime` | market index above its own 200-day average | Ignores the stock; stands aside in broad downturns |
| `none` | every Supertrend flip | **The default.** The Supertrend exit does the risk management |

The Filter dropdown on the dashboard is a **global** setting, like Range — changing it applies to every ticker at once. Change `DEFAULT_PROFILE` in `.env` to seed new tickers differently, change any individual ticker from the dashboard dropdown, or set them all at once:

```bash
curl -X POST "http://localhost:19080/api/watchlist/profile?value=adx_only" \
  -H "X-Auth-Token: <token>"
```

The per-sector recommendations in `app/watchlist.py` came from backtesting an earlier 25-ticker set: the SMA gate helped on energy and ETFs and actively hurt on high-ATR momentum names by blocking entries at trend inception. Run the watchlist backtest and check the `mismatched` list before trusting one profile everywhere.

### Why unfiltered is the default

Tested out-of-sample across the watchlist (39 tickers at the time of the test), unfiltered had negative expectancy on only **3** of them, against 9–14 for every filtered variant, and the best median total return. Pooled over the whole parameter grid, 82% of its rows were positive versus 65–70% for the others.

The filtered profiles often show a higher *average trade* — they take fewer, more selective entries — but they are far less consistent from ticker to ticker, and the selectivity does not pay for the trades it skips.

The market-regime gate performed worst of the five: lowest expectancy and the largest median drawdown. It removed trades without removing losses.

### Why `adx_rising` exists

ADX measures how *established* a trend is, so it is low by construction at trend inception. Requiring "ADX above 20" on the bar Supertrend flips up asks for an established trend at the exact moment a trend starts — the two conditions work against each other. The rising variant tests the slope instead, which is compatible with entering early.

Whether it actually helps is an empirical question. The Backtest tab runs every profile side by side on the same signals, so compare them per ticker rather than assuming.

### Earnings dates, and how the blackout goes inert

The blackout needs stored report dates. With none, it silently does nothing — no bars to block means no entries blocked, in the scanner and the backtest alike. That is a change of strategy you would not notice.

Report dates are fetched weekly (Sunday 06:00), on a cold start, and whenever the app starts and finds none stored — `docker compose down -v` wipes them, and the weekly job could be six days away.

The header says `no earnings dates — blackout inactive` when there is nothing to act on. To fix it immediately:

```bash
curl -X POST http://localhost:19080/api/refresh-earnings -H "X-Auth-Token: <token>"
```

`GET /api/earnings-coverage` reports how many eligible tickers have dates. ETFs and indices are excluded — they never report.

### Earnings blocks are not filter rejections

A signal can be blocked for two unrelated reasons, and the chart says which. **Filtered out** (grey) means the filter profile rejected it. **Blocked by earnings** (violet) means the entry fell within five days of a report date — a rule that applies whatever the profile is, including `none`, because a trailing stop cannot protect against an overnight gap.

---

## The controls

`Stop width`, `ATR length` and `Min trend strength` apply to everything on screen: the chart lines, the rail ranking, the readout strip, the attention score and the backtest. Changing one recomputes all of them, so the numbers never describe different settings from the lines.

They are a scratchpad, not a saved setting. To keep a value for a ticker, use the **Tune** tab's Apply button; to change the defaults for everything, set `ATR_LEN`, `ATR_MULT` and `ADX_MIN` in `.env`.

---

## Positions

The **Position** button next to the filter records what you actually bought. Enter the date, price, quantity (fractional shares are fine) and the commission your broker charged, and the dashboard then shows:

- live P&L in percent, and in dollars if you gave a quantity
- how long you have held it
- **what is actually at risk** — what you would lose if price fell to the Supertrend stop from here. It is measured from the *current* stop, not your entry, because the stop trails upward as the trade works. Once the stop passes your entry price the trade can no longer lose money, and the panel shows "stop above entry" rather than a dollar figure.

  Example: entry 488.57, quantity 1.56, current stop 451.69. Risk per share is 488.57 − 451.69 = 36.88, so at risk is about $58. That is your live downside on the position, not your unrealised profit.
- a dot beside the ticker in the rail, so held names are visible at a glance

Closing a position records the sale, and the Backtest tab then gains a **Your trades** row alongside the simulated ones — same columns, so you can see directly whether your execution matched the backtest that convinced you.

Sell alerts still fire for every ticker, held or not, but the ones you hold are flagged `← YOU HOLD THIS` in the notification.

Positions are yours to correct: records can be edited by deleting and re-adding, and nothing about them feeds back into the signals.

**Buying more and selling part.** Buying again in a ticker you already hold records a second lot at its own price; the dashboard shows them as one holding, with the shares added up and the entry price averaged by size. The sell form takes a quantity, so you can sell part of a holding and keep the rest. A sale takes from the oldest lot first, splitting it if needed, and the purchase commission follows the shares in proportion.

**Commission** is recorded per order — once on the buy, once on the sell — so the Earnings tab can report what your account really did. Set `DEFAULT_COMMISSION` in `.env` to your broker's flat fee and both fields arrive pre-filled; each trade keeps whatever you actually entered, so changing the default never rewrites past trades. Trades recorded before this existed read as zero commission.

**Size by risk.** The purchase form works out how many shares put no more than a set amount between the price and the Supertrend stop. Enter your account size and the percentage you are willing to risk, and it answers with a share count: at 1% of $20,000 and a stop $7.89 below the price, 25 shares risk $197. **Use** puts that number in the quantity field. If the stop sits above the price (a downtrend), it says so instead of sizing a trade you would not take. Three settings shape the answer, all kept for the browser session only:

| Setting | Does |
| --- | --- |
| **Risk %** | How much of the account a stop-out may cost. |
| **Max %** | The most of the account to put in one position. Whichever limit allows fewer shares wins, so a tight stop is held to the cap and simply risks less than your figure. |
| **Fractional** | On by default, matching brokers that sell part of a share, so the risk figure can be hit exactly. Off rounds down to whole shares. |

It also warns when commission is a meaningful share of the position: a $70 position pays about 2.8% in fees on a $0.99 round trip, which no edge survives. On a small account this is the real constraint, not the risk percentage: prefer positions large enough that fees stay near 1%.

**Stock splits are caught.** Yahoo adjusts its history backwards after a split, but the price you typed stays as it was, so a held position would quietly read wrong by the split factor. When the two disagree by a ratio that looks like a split, the Position panel says so and offers to restate the record: the price per share is divided by the factor and the share count multiplied by it, leaving the money you put in unchanged. A closed trade needs no correction, because both its prices come from before the split.

**Quantity is what makes dollars possible.** Without it a trade still shows its percentage, but it is left out of every dollar figure and out of the Earnings charts rather than being guessed at.

---

## Earnings tab

The **Earnings** tab is your account, not the method: real fills, real quantities, real commission, across every ticker. Positions you still hold count too, valued at the latest stored close, so the tab shows the account moving day by day rather than only when something is sold.

At the top, the totals: account balance, total return, profit banked on closed trades, unrealised profit on what you still hold, gross profit, commission paid, win rate, average trade, best and worst, and average holding time. Hover the commission figure to see what share of gross profit it ate.

Two charts, both daily and both after commission:

| Chart | Shows |
| --- | --- |
| **Return from trading, %** | Two lines: **including open positions**, and **closed trades only**. The gap between them is your unrealised profit. Each day's gain or loss is chained onto the last, so a sale reads the same percentage the table shows for that trade, and money you add never counts as a gain. |
| **Account balance, $** | Cash plus the market value of what you hold, every day. Buys take cash out, sells put it back, commission comes off both. |

**Starting balance.** The account starts at what your **first purchase cost**, so the balance line begins where you began. If a later trade costs more than the account holds, the difference is money you put in: it is added to the balance and to **Money in**, and it never counts as profit. Set `STARTING_CASH` in `.env` to your real opening balance instead, and nothing needs topping up. Note that a large starting balance leaves idle cash in the account, which dilutes the trading return, exactly as it would at a broker.

**Two returns, because they answer different questions**

| Figure | Means |
| --- | --- |
| **Trading return** | How the trades performed. Money added or taken out does not count, so a trade that doubles your money reads +100% whatever the account is worth. This is the line on the chart. |
| **Growth on money in** | The balance against everything you have put in. Lower than the trading return when you added money part-way. |

A sale's figure on the chart matches the table: a trade that turned $463.99 into $922.57 reads **+98.83%** in both. Later trades compound onto it, so two trades of +98.83% and −17.74% leave the line at +63.5%.

**Export CSV** downloads every recorded order as a file: one row per buy and per sell, with the quantity, price, value, commission, days held and the trade's profit, plus the entry and exit of the trade each row belongs to. Open positions are included and marked. The numbers are exactly as recorded, in whatever currency you traded in, with no conversion or tax treatment applied, so it suits any country.

Below them, every buy and sell as its own row: date, ticker, side, quantity, price, order value, commission, days held, and the profit or loss. Positions you still hold come first, tagged **still held**, with their running unrealised P&L on the buy row. Closed trades follow, each trade's buy and sell together, newest first, so the two orders that make a result stay next to each other even when trades overlap.

Worth knowing: `COST_PCT_PER_SIDE` in the Backtest is an estimate applied to simulated trades, while commission here is what you actually paid. They are deliberately separate — one is a model, the other is your account.

---

## Backtest

Every ticker has a **Backtest** tab next to the chart. It runs the same Supertrend signals through every filter profile over the stored history and shows them side by side, with buy-and-hold as a benchmark.

Six numbers per profile, and no more: trades, win rate, expectancy per trade, profit factor, max drawdown, total return. The equity curves are plotted together so you can see where each profile gained or lost ground rather than just reading an end figure.

Two deliberate honesty features:

- **Buy-and-hold is always shown.** If simply owning the stock beat the method, the verdict line says so, including the drawdown you avoided.
- **Weak samples are labelled.** Under 10 trades, the verdict says to treat the result as a hint rather than evidence. Small samples produce spectacular-looking expectancy that does not survive contact with more data.

The ATR and ADX inputs at the top apply to the backtest too, so you can test a setting before you commit it.

`GET /api/backtest?years=5` runs the same test across the whole watchlist and returns a `mismatched` list — tickers where the best-performing profile isn't the one currently assigned. That's your shortlist for review.

Fills are at the next bar's open after a signal, never the signal bar's close.

### Costs

The backtest charges `COST_PCT_PER_SIDE` (default 0.05%, so 0.1% a round trip) on every fill, covering commission and expected slippage together.

This matters more than it looks. Twenty trades is 2% of drag, against per-trade expectancies that are often 1–3%. Buy-and-hold is charged one round trip on the same basis, so the comparison stays honest in both directions.

### The earnings blackout in the backtest

The live scanner blocks entries within five days of a report because a stop cannot protect against an overnight gap. The backtest applies the same rule, using report dates stored during the weekly earnings refresh, so it measures the strategy you actually run. It reports how many bars were blocked and how many report dates it knew about, so you can tell whether the data was there.

---

## Tune

The **Tune** tab searches ATR length and stop width using walk-forward validation.

It splits the window: settings are chosen on the first 60% and scored only on the last 40%, which the search never saw. The table shows both, side by side.

- **Fit per trade** — how the setting did in the window used to choose it
- **Test per trade** — how it did on held-back data. This is the only column worth acting on

A strong fit result with a weak test result means the settings fitted noise. The tab says so in plain words, and the **Apply** button only appears when the chosen setting beat the default out-of-sample *and* produced at least eight out-of-sample trades. When it doesn't appear, that is the honest answer: no setting in the grid earned its place.

The grid is deliberately coarse (4 lengths × 5 multipliers). A finer grid finds better fit numbers and worse test numbers, because there is more noise available to fit.

Applying saves ATR settings against that one ticker; everything else keeps the global defaults.

### Windows

Range maps to trading days, not calendar days: 126, 252, 504 and 1260 bars for 6 months, 1, 2 and 5 years.

**Max** means all *usable* history, not all stored history. The 200-day average needs 200 bars before it exists, so the first ~199 bars of any ticker carry no Supertrend, no SMA and no ADX. The backtest discards them, and the chart window starts at the same point rather than showing 200 bars of bare candles the analysis cannot use. On a recently listed ticker the effect is obvious — COIN listed in April 2021, so its usable history starts in January 2022. A walk-forward split needs enough bars on both sides to mean anything, so Tune uses a minimum of 400 regardless — with 6 months or 1 year selected it says so in the header rather than quietly showing a different window from the one you picked.

### Does tuning work at all?

The header shows the **fit → test correlation** for the ticker and profile in front of you: how well expectancy in the fitting window predicted expectancy in the test window, across the 20 settings.

Measured across the watchlist, this sits at about **r = 0.03** — statistically indistinguishable from zero. The sign matters: a *negative* correlation means the settings that looked best in the fitting window tended to do worse on the test window, which is worse than no information, and the badge says so rather than reporting it as a relationship. The fit-chosen setting beat the default out-of-sample only 37.5% of the time, with a median cost of 0.83% per trade.

So the tab's honest purpose is not to find better parameters. It is to check, cheaply and with evidence, whether a parameter hunch is worth acting on. On this watchlist the answer has consistently been no, and the correlation figure says so every time you open it rather than leaving you to discover it.

The one thing not to do is scan the Test column and pick the best row. That is choosing on the held-back data, which destroys the only property that made it worth measuring.

### Export full grid

The **Export full grid** button writes one CSV row for every combination of ticker, filter profile and ATR setting — 41 × 5 × 20 is about 4,100 rows, and takes roughly twenty seconds.

Each row carries the fitting window and the test window separately, so you can ask across the whole watchlist what the three-ticker view cannot answer: does a stop width that wins in the fitting window tend to win out-of-sample, or is the relationship noise?

Columns: ticker, name, sector, profile, atr_len, atr_mult, adx_min, cost_pct_per_side, fit window and trades and expectancy, test window and trades and expectancy and win rate and return and drawdown, plus flags for which row the fitting window chose (`is_fit_winner`), which row is your current default (`is_default`), whether that ticker's verdict held up, and whether the search was underpowered.

The export uses the window currently selected in Range, and records it in a `window_bars` column, so every row reconciles exactly with what the Tune tab shows for that ticker and profile.

`GET /api/export/grid.csv?tickers=NVDA,AMD` limits it to named tickers, and `?years=2` or `?bars=504` sets the window explicitly.

---

## Alerts

### Alert detail

`NOTIFY_DETAIL=full` (the default) names the tickers, prices and stops:

```
SupertrendMoose — 1 buy, 2 sell
Scanned 41 tickers, prices through 2026-09-19

BUY SIGNALS
  GOOGL: $252.14 - stop: $231.06 - 9.1% risk - ADX: 28 - ATR: 2.1% - above 200MA - score: 78

SELL SIGNALS
  XOM: $110.00 - Supertrend flipped down - ← YOU HOLD THIS - +12.4%, held 34d
  SMH: $285.40 - Supertrend flipped down

BLOCKED, NOT ALERTED
  SPY: outside liquidity/ATR screen
  LMT: earnings in 3d
```

Each value is hyphen-separated and labelled rather than aligned into columns, because mail clients and the ntfy app collapse runs of spaces — aligned columns do not survive the trip.

The scan summary line matters more than it looks: the date the prices run through tells you whether you are reading a fresh scan or a stale one.

**BLOCKED, NOT ALERTED** lists flip-ups that fired and were rejected — by a filter profile, the earnings blackout, or the liquidity/ATR screen. Without it, a ticker's absence from the buy list is indistinguishable from nothing having happened. The 200MA is omitted for a ticker with under 200 bars, since it has no average yet.

The `Dashboard:` link is appended when `BASE_URL` resolves to something — on a loopback-only install there is nothing useful to link to, so it is left out.

`NOTIFY_DETAIL=minimal` sends counts only — "3 buy, 1 sell. Open the scanner for details." None of the fields above appear in it, including the blocked reasons. Useful if alerts might be read off a lock screen. Tapping the notification opens the dashboard either way.

### Other channels

`NOTIFY_CHANNELS` takes any comma-separated combination of `ntfy`, `telegram`, `discord`, `email`. Adding `email` gives you a searchable archive of every signal alongside the push. A failure in one channel can never abort a scan.

[DEPLOY.md](../DEPLOY.md#-email-alerts-optional) covers setting email up through a relay.

### Heartbeat

A quiet day and a broken notification channel look identical from the outside: both produce silence. `NOTIFY_HEARTBEAT=true` makes them distinguishable. On any scan that produces no signals, a short message goes out with the ticker count and the date the prices run through:

```
SupertrendMoose: no signals today
Scan completed. No buy or sell signals.
  41 tickers, prices through 2026-09-18
```

Something arrives every trading day, so silence means the alerting itself has broken.

It is off by default, because on two channels the redundancy already tells you. It earns its place when you run a single channel, where nothing else would reveal a dead relay or an expired credential.

It is suppressed on a degraded scan — stale prices, or most tickers failing to download — because the problem alert below has already gone out, and "no signals" would be a misleading thing to say next to it.

### When a scan does not really work, it says so

Silence used to mean both "nothing to trade" and "nothing ran". An alert goes out when most tickers fail to download, when nothing could be evaluated at all, or when the stored prices are still behind the last finished US session, which means the scan changed nothing. One alert a day at most, so an outage lasting a week does not send seven identical messages, and a single flaky ticker is ignored.

If prices stop arriving, the header says how many sessions the stored data is behind and how many tickers the last scan failed on, in amber. A single missing session is normal on a US holiday. For the cause, open `/api/diagnose`, which probes Yahoo directly, or read the container log.

---

## Time zone and schedule

Set `TZ` in `.env` with its name from the [tz database](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), then `docker compose up -d`. The default is `Australia/Melbourne`. A name that is not a real time zone stops the container with a message listing examples, rather than quietly running on UTC.

`TZ` sets the times in the logs and when the weekly earnings refresh runs (Sundays 06:00). The dashboard shows every time in your browser's own zone, including the next scan in the header; hover it for the rule behind it.

**The scan follows the US market, not your clock.** It runs after every US trading day, 60 minutes after the 16:00 New York close, so it is always after the close wherever you are and whatever daylight saving is doing on either side. You never need to adjust it for your zone:

| TZ | Scan time (local), across a year |
| --- | --- |
| `Australia/Melbourne` | next morning: 07:00 mid-year, 09:00 Nov–Mar, 08:00 in between |
| `Pacific/Auckland` | next morning: 09:00 mid-year, 11:00 Nov–Mar, 10:00 in between |
| `Asia/Singapore` | next morning: 05:00, or 06:00 Nov–Mar |
| `Europe/London` | 22:00, or 21:00 for a few weeks in March and October |
| `America/Los_Angeles` | 14:00 |

The local time moves because the two places change their clocks on different dates; the scan itself stays at 17:00 New York time.

Change the gap with `SCAN_AFTER_CLOSE_MINUTES` (15 to 600). Yahoo's daily bar can still shift for a few minutes after the close, so keep at least 15.

A day that has not finished trading is never stored, so **Scan now** during US market hours uses the last completed session rather than a half-finished one.

**If the machine is asleep or off at scan time**, nothing is lost. Every 15 minutes, and moments after the container starts, SupertrendMoose checks whether the stored prices are behind the last finished US session and scans if they are, alerting as usual. A missed weekly earnings refresh runs on wake too. So a desktop that sleeps overnight catches up when you turn it on, not a day later.

**A fixed local time** is still possible if you want the scan at a set time:

```ini
SCAN_TIME=10:00
SCAN_DAYS=tue-sat
```

The log checks that time against the US close for the coming year and warns if it can run too early in any season. The older `SCAN_HOUR` and `SCAN_MINUTE` settings still work the same way.

The container log shows what was chosen:

```
scan scheduled 60 min after each US close (mon-fri 17:00 New York time);
next Fri 18 Sep 07:00 Australia/Melbourne (Thu 17:00 New York)
```

---

## Ports

| Purpose | Host port | Container port | Bound to |
| --- | --- | --- | --- |
| Dashboard and API | `APP_PORT`, default 19080 | 8000 | `HOST_IP`, default `127.0.0.1` |
| ntfy | `NTFY_PORT`, default 19081 | 80 | `HOST_IP`, default `127.0.0.1` |

Nothing else listens. The database is a file, and the app reaches ntfy over the internal Docker network without touching a host port.

Change either with `APP_PORT` / `NTFY_PORT` in `.env`. The alert links and the ntfy phone address follow automatically, unless you have set `BASE_URL` or `NTFY_PUBLIC_URL` yourself, in which case update those to match.

The container-side ports (8000 and 80) are internal to the Docker network and never exposed, so there is no benefit to changing them.

---

## API

| Route | Purpose |
| --- | --- |
| `GET /api/health` | Status and last scan time (never requires auth) |
| `GET /api/states` | Current state of every watchlist ticker |
| `GET /api/chart/{ticker}` | OHLC plus indicator series; accepts `atr_len`, `atr_mult`, `adx_min` overrides |
| `GET /api/macd/{ticker}` | MACD series for the optional pane; `timeframe=D` or `W` |
| `GET /api/backtest/{ticker}` | Every profile vs buy-and-hold |
| `GET /api/backtest` | Pooled across the watchlist, with a `mismatched` list |
| `GET /api/export/grid.csv` | Full tuning grid as CSV |
| `GET /api/signals?days=60` | Historical signals, passed and rejected |
| `GET /api/watchlist` | Full watchlist |
| `POST /api/watchlist` | Add a ticker and backfill its history |
| `PATCH /api/watchlist/{ticker}` | Change profile, thresholds or enabled state |
| `POST /api/watchlist/profile?value=<profile>` | Set the filter profile on every ticker at once |
| `DELETE /api/watchlist/{ticker}` | Remove a ticker |
| `POST /api/scan` | Run a scan now — `send_alerts=true` to notify on signals not yet alerted, `resend=true` to repeat ones that were, `refresh_prices=false` to skip the download. See [DEPLOY.md](../DEPLOY.md#running-a-missed-scan-by-hand) |
| `POST /api/refresh-earnings` | Fetch report dates now |
| `GET /api/earnings-coverage` | How many eligible tickers have report dates |
| `POST /api/test-notification` | Verify your alert channels |
| `GET /api/diagnose` | Probe the Yahoo price pipeline |
| `GET /api/runs` | Scan history |

Every route except `/api/health` needs an `X-Auth-Token` header.

---

## Adding tickers

```bash
curl -X POST http://localhost:19080/api/watchlist \
  -H 'X-Auth-Token: <token>' -H 'Content-Type: application/json' \
  -d '{"ticker":"ANET","sector":"Semis / AI","profile":"adx_only"}'
```

A liquidity screen rejects signals on anything below `MIN_AVG_VOLUME` or outside the `MIN_ATR_PCT`–`MAX_ATR_PCT` band, so thin or dead-flat names won't generate alerts even if you add them.

---

## Tests

```bash
python tests/smoke_test.py
```

Runs fully offline against synthetic prices — no network, no API keys. It covers the indicator maths, the database round-trip, every filter profile, the earnings blackout, the scan engine, the backtest, every API route, the token gate, secret generation, `entrypoint.sh`, and that a broken notification channel can never abort a scan.

If Docker is installed it also renders `docker-compose.yml` with sample `.env` files. To use a standalone Compose binary instead, set `COMPOSE_BIN=/path/to/docker-compose`.

---

## Moving to another host

Clone the repository on the new host and start it the same way. Prices re-download and the new host generates its own credentials, but **your recorded positions live in the old host's `supertrendmoose_moose-data` volume**. Back it up first (see [DEPLOY.md](../DEPLOY.md#-backups)) and restore it on the new host before the first start if you want to keep them.

The data volumes are always named `supertrendmoose_…`, whatever the cloned folder is called, so renaming or moving the folder on the same host keeps your data. Run `docker compose down` in the old folder first, because the container names are fixed and two copies cannot run at once.

---

## Performance notes

A dashboard refresh used to re-read every ticker's bars from the database as ORM objects, recompute all indicators, and serialise the chart row by row. Four things fixed that:

- **Price frames are read once with pandas and cached**, invalidated whenever prices are written. Loading went from ~20 ms a ticker to nothing.
- **Indicators are memoised** on the data's own fingerprint plus the parameter set, so a new bar invalidates the entry automatically. Bounded by `INDICATOR_CACHE_MAX` (default 200 frames, roughly 25 MB).
- **True range is computed once per call** instead of three times — it was being recalculated separately for ATR, ADX and Supertrend.
- **The chart payload is built column-wise**, not with `iterrows`. That single change took a chart request from ~330 ms to ~12 ms.

The dashboard also no longer blocks on the watchlist sweep: the chart response carries its own recomputed state, so the readout is correct immediately, and the rail ranking catches up in the background. Control changes are debounced, so holding a spinner arrow queues one recompute rather than one per click.
