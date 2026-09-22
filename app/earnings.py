"""Profit and loss on the trades you actually took.

The backtest answers "does the method work?". This answers "what did my
account do?", which is a different question: it uses the prices you were
filled at, the quantities you held, and the commission you paid on each order.

Only closed trades count. A trade needs a buy and a sell before it has a
result, so an open position appears here once it is closed.

Money needs a quantity. A position recorded without one still shows in the
table, with its percentage, but is left out of the dollar figures rather than
being guessed at.
"""
from __future__ import annotations

from datetime import date


def _trade(p) -> dict:
    """One closed position, with fees taken out.

    net = what the account actually gained: the sale proceeds after the sell
    commission, less the purchase cost including the buy commission.
    """
    qty = p.quantity
    fees = round((p.entry_fee or 0.0) + (p.exit_fee or 0.0), 2)
    gross_pct = (p.exit_price / p.entry_price - 1) * 100

    cost = proceeds = gross = net = net_pct = None
    if qty:
        cost = qty * p.entry_price + (p.entry_fee or 0.0)
        proceeds = qty * p.exit_price - (p.exit_fee or 0.0)
        gross = qty * (p.exit_price - p.entry_price)
        net = proceeds - cost
        net_pct = net / cost * 100 if cost else None

    return {
        "id": p.id,
        "ticker": p.ticker,
        "entry_date": p.entry_date.isoformat(),
        "exit_date": p.exit_date.isoformat(),
        "hold_days": (p.exit_date - p.entry_date).days,
        "quantity": qty,
        "entry_price": p.entry_price,
        "exit_price": p.exit_price,
        "entry_fee": round(p.entry_fee or 0.0, 2),
        "exit_fee": round(p.exit_fee or 0.0, 2),
        "fees": fees,
        "cost": round(cost, 2) if cost is not None else None,
        "proceeds": round(proceeds, 2) if proceeds is not None else None,
        "gross": round(gross, 2) if gross is not None else None,
        "net": round(net, 2) if net is not None else None,
        "gross_pct": round(gross_pct, 2),
        "net_pct": round(net_pct, 2) if net_pct is not None else None,
        "note": p.note,
    }


def trades(positions) -> list[dict]:
    """Closed positions as trades, oldest sale first."""
    closed = [p for p in positions if p.exit_date and p.exit_price]
    closed.sort(key=lambda p: (p.exit_date, p.entry_date, p.id or 0))
    return [_trade(p) for p in closed]


def open_rows(positions, marks: dict) -> list[dict]:
    """Positions still held, valued at the latest stored close.

    Shown next to the closed trades so the table is the whole account, not
    only the part that has been sold. The result is what the trade is worth
    today, not what it made: it can still change.
    """
    out = []
    for p in positions:
        if p.exit_date:
            continue
        mark = marks.get(p.ticker, {}).get("close")
        as_of = marks.get(p.ticker, {}).get("date")
        qty = p.quantity
        fee = p.entry_fee or 0.0
        cost = qty * p.entry_price + fee if qty else None
        value = qty * mark if (qty and mark) else None
        # Only the buy commission is spent so far; the sell one is not.
        net = value - cost if (value is not None and cost is not None) else None
        out.append({
            "id": p.id, "ticker": p.ticker, "open": True,
            "entry_date": p.entry_date.isoformat(),
            "exit_date": None,
            # Never negative: a purchase dated after the latest stored bar
            # has been held for no days, not minus some.
            "hold_days": max((as_of - p.entry_date).days, 0) if as_of else None,
            "quantity": qty, "entry_price": p.entry_price, "exit_price": None,
            "entry_fee": round(fee, 2), "exit_fee": 0.0, "fees": round(fee, 2),
            "cost": round(cost, 2) if cost is not None else None,
            "mark": mark, "mark_date": as_of.isoformat() if as_of else None,
            "value": round(value, 2) if value is not None else None,
            "net": round(net, 2) if net is not None else None,
            "net_pct": round(net / cost * 100, 2) if (net is not None and cost) else None,
            "gross_pct": round((mark / p.entry_price - 1) * 100, 2) if mark else None,
            "note": p.note,
        })
    out.sort(key=lambda t: t["entry_date"], reverse=True)
    return out


def orders(trade_rows: list[dict], open_trades: list[dict] | None = None) -> list[dict]:
    """Every buy and sell as its own row, grouped by trade, newest sale first.

    Grouped rather than strictly chronological: when two trades overlap, a
    purely date-ordered list separates a buy from the sell that closed it, and
    the profit on the row stops making sense. The result of a trade is shown
    on its sell, because that is the order that realised it.
    """
    out: list[dict] = []
    # Still-open trades first: they are the live part of the account.
    for t in (open_trades or []):
        value = t["quantity"] * t["entry_price"] if t["quantity"] else None
        out.append({
            "trade_id": t["id"], "date": t["entry_date"], "ticker": t["ticker"],
            "side": "BUY", "quantity": t["quantity"], "price": t["entry_price"],
            "value": round(value, 2) if value is not None else None,
            "commission": t["entry_fee"], "net": t["net"], "net_pct": t["net_pct"],
            "hold_days": t["hold_days"], "open": True, "mark": t["mark"],
        })
    for t in sorted(trade_rows, key=lambda x: (x["exit_date"], x["id"] or 0), reverse=True):
        value = t["quantity"] * t["entry_price"] if t["quantity"] else None
        out.append({
            "trade_id": t["id"], "date": t["entry_date"], "ticker": t["ticker"],
            "side": "BUY", "quantity": t["quantity"], "price": t["entry_price"],
            "value": round(value, 2) if value is not None else None,
            "commission": t["entry_fee"], "net": None, "net_pct": None,
            "hold_days": None, "open": False, "mark": None,
        })
        value = t["quantity"] * t["exit_price"] if t["quantity"] else None
        out.append({
            "trade_id": t["id"], "date": t["exit_date"], "ticker": t["ticker"],
            "side": "SELL", "quantity": t["quantity"], "price": t["exit_price"],
            "value": round(value, 2) if value is not None else None,
            "commission": t["exit_fee"], "net": t["net"], "net_pct": t["net_pct"],
            "hold_days": t["hold_days"], "open": False, "mark": None,
        })
    return out


def curves(trade_rows: list[dict]) -> dict:
    """Running totals by sale date, for the two charts.

    dollars  cumulative net profit, after commission
    percent  net profit as a share of everything put in so far, so a $50 gain
             on $1,000 and another on $10,000 do not count the same
    Trades sharing a sale date collapse into one point, the last of that day.
    """
    dollars: list[dict] = []
    percent: list[dict] = []
    net_sum = cost_sum = 0.0
    for t in trade_rows:
        if t["net"] is None or not t["cost"]:
            continue                       # no quantity: not a dollar figure
        net_sum += t["net"]
        cost_sum += t["cost"]
        point_d = {"time": t["exit_date"], "value": round(net_sum, 2)}
        point_p = {"time": t["exit_date"], "value": round(net_sum / cost_sum * 100, 2)}
        if dollars and dollars[-1]["time"] == t["exit_date"]:
            dollars[-1], percent[-1] = point_d, point_p
        else:
            dollars.append(point_d)
            percent.append(point_p)
    return {"dollars": dollars, "percent": percent}


def _events(positions) -> list[dict]:
    """Cash movements, one per order.

    Each carries the position it belongs to, so a second trade in a ticker you
    already hold is tracked as its own lot at its own purchase price rather
    than being confused with the first one.
    """
    ev = []
    for p in positions:
        qty = p.quantity or 0.0
        ev.append({"date": p.entry_date, "cash": -(qty * p.entry_price) - (p.entry_fee or 0.0),
                   "buy": True, "id": p.id, "ticker": p.ticker, "qty": qty,
                   "price": p.entry_price})
        if p.exit_date and p.exit_price:
            ev.append({"date": p.exit_date, "cash": qty * p.exit_price - (p.exit_fee or 0.0),
                       "buy": False, "id": p.id, "ticker": p.ticker, "qty": qty,
                       "price": p.entry_price})
    # A sale settles before a purchase on the same day: the cash it frees is
    # what usually pays for the purchase.
    ev.sort(key=lambda e: (e["date"], e["buy"]))
    return ev


def required_cash(positions) -> float:
    """The most cash the account would ever need if nothing were added."""
    cash = low = 0.0
    for e in _events(positions):
        cash += e["cash"]
        low = min(low, cash)
    return round(-low, 2)


def first_cost(positions) -> float:
    """What the earliest purchase cost, commission included.

    The default starting balance: the account begins with exactly the money
    the first trade needed, so the curve starts where you started and the
    first sale's percentage matches that trade's own return.
    """
    buys = [e for e in _events(positions) if e["buy"]]
    return round(-buys[0]["cash"], 2) if buys else 0.0


def daily(positions, closes: dict, starting_cash: float | None = None) -> dict:
    """Day-by-day account balance and return, open positions included.

    closes maps a ticker to {date: close}. Each day the account is worth the
    cash it holds plus the market value of the shares it holds, so an open
    position moves the line every day rather than only when it is sold.

    Two return lines share the axis, both measuring the trading itself: each
    day's gain or loss is chained onto the last, so money you put in to afford
    a bigger trade changes the balance but never the return. A trade that
    doubles your money reads +100% whatever the account is worth.
      total   the account including what is still held
      closed  the same, with open positions worth exactly what was paid for
              them, so only sold trades move it
    A day with no stored price for a held ticker carries the previous close.
    """
    held = [p for p in positions if p.quantity]
    if not held:
        return {"balance": [], "capital": [], "pct_total": [], "pct_closed": [],
                "trading_return": None, "starting_cash": 0.0,
                "auto_cash": starting_cash is None,
                "capital_in": 0.0, "capital_added": 0.0, "days": 0}

    start = min(p.entry_date for p in held)
    dates = sorted({d for p in held for d in closes.get(p.ticker, {})
                    if d >= start})
    last_event = max(max((p.exit_date or p.entry_date) for p in held), start)
    if not dates or dates[-1] < last_event:
        dates = sorted(set(dates) | {last_event})
    if dates[0] > start:
        dates.insert(0, start)

    cash0 = first_cost(held) if starting_cash is None else float(starting_cash)
    events = _events(held)

    balance, pct_total, pct_closed, capital_line = [], [], [], []
    cash = cash0
    capital = cash0                  # everything you have put in, so far
    added = 0.0                      # of which, topped up after the start
    lots: dict[int, dict] = {}       # what is held right now, lot by lot
    idx = 0
    marks: dict[str, float] = {}
    growth_total = growth_closed = 1.0    # running growth factors
    # The chain starts at the money committed, not at the first day's share
    # value, so the opening commission shows up as the small loss it is.
    prev_total = prev_closed = cash0
    added_today = 0.0
    for d in dates:
        while idx < len(events) and events[idx]["date"] <= d:
            e = events[idx]
            cash += e["cash"]
            if e["buy"]:
                lots[e["id"]] = e
            else:
                lots.pop(e["id"], None)
            # A purchase larger than the account holds was paid for with money
            # you put in. Counting it as capital keeps it out of the return:
            # a deposit is not a profit.
            if cash < -1e-9:
                capital += -cash
                added += -cash
                added_today += -cash
                cash = 0.0
            idx += 1
        value = closed_value = 0.0
        for lot in lots.values():
            # A missing bar carries the last close, or what was paid if there
            # has never been one, so a gap never invents a gain or a loss.
            close = closes.get(lot["ticker"], {}).get(d, marks.get(lot["ticker"], lot["price"]))
            marks[lot["ticker"]] = close
            value += lot["qty"] * close
            # Closed-only: each lot is worth exactly what it cost.
            closed_value += lot["qty"] * lot["price"]
        iso = d.isoformat()
        total_value = cash + value
        closed_only = cash + closed_value
        balance.append({"time": iso, "value": round(total_value, 2)})
        capital_line.append({"time": iso, "value": round(capital, 2)})
        # Chain today's move onto the running figure, with the day's top-up
        # taken out first so it cannot look like a gain.
        if prev_total and prev_total > 0:
            growth_total *= (total_value - added_today) / prev_total
        if prev_closed and prev_closed > 0:
            growth_closed *= (closed_only - added_today) / prev_closed
        prev_total, prev_closed = total_value, closed_only
        added_today = 0.0
        pct_total.append({"time": iso, "value": round(growth_total * 100 - 100, 2)})
        pct_closed.append({"time": iso, "value": round(growth_closed * 100 - 100, 2)})
    return {"balance": balance, "capital": capital_line,
            "pct_total": pct_total, "pct_closed": pct_closed,
            "trading_return": round(growth_total * 100 - 100, 2),
            "starting_cash": round(cash0, 2), "auto_cash": starting_cash is None,
            "capital_in": round(capital, 2), "capital_added": round(added, 2),
            "days": len(balance)}


def summary(trade_rows: list[dict]) -> dict:
    """Totals across the closed trades."""
    priced = [t for t in trade_rows if t["net"] is not None]
    nets = [t["net"] for t in priced]
    pcts = [t["net_pct"] for t in trade_rows if t["net_pct"] is not None]
    wins = [n for n in nets if n > 0]
    cost = sum(t["cost"] for t in priced)
    net = sum(nets)
    fees = sum(t["fees"] for t in trade_rows)
    gross = sum(t["gross"] for t in priced)
    return {
        "trades": len(trade_rows),
        "priced_trades": len(priced),
        "without_quantity": len(trade_rows) - len(priced),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(nets) * 100, 1) if nets else None,
        "gross": round(gross, 2) if priced else None,
        "fees": round(fees, 2),
        "net": round(net, 2) if priced else None,
        "net_pct": round(net / cost * 100, 2) if cost else None,
        "invested": round(cost, 2) if priced else None,
        "best": round(max(nets), 2) if nets else None,
        "worst": round(min(nets), 2) if nets else None,
        "avg_net": round(net / len(nets), 2) if nets else None,
        "avg_net_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
        "avg_hold_days": round(sum(t["hold_days"] for t in trade_rows) / len(trade_rows))
        if trade_rows else None,
        # Commission as a share of the profit it was paid out of, which is
        # what tells you whether the costs are eating the edge.
        "fees_vs_gross_pct": round(fees / gross * 100, 1) if priced and gross > 0 else None,
        "first_sale": trade_rows[0]["exit_date"] if trade_rows else None,
        "last_sale": trade_rows[-1]["exit_date"] if trade_rows else None,
    }


def report(positions, closes: dict | None = None,
           starting_cash: float | None = None) -> dict:
    """Everything the Earnings tab needs, in one payload.

    closes maps a ticker to {date: close}; without it the daily account
    curves are empty and only the closed trades are reported.
    """
    closes = closes or {}
    rows = trades(positions)
    marks = {t: {"date": max(v), "close": v[max(v)]} for t, v in closes.items() if v}
    opens = open_rows(positions, marks)
    money = daily(positions, closes, starting_cash)
    totals = summary(rows)
    totals.update(_open_totals(opens, money))
    # Commission is money already spent, so the purchase commission on what
    # you still hold counts too, not only the fees on trades you have closed.
    open_fees = round(sum(t["entry_fee"] for t in opens), 2)
    totals["fees_closed"] = totals["fees"]
    totals["fees_open"] = open_fees
    totals["fees"] = round(totals["fees"] + open_fees, 2)
    if totals["gross"] and totals["gross"] > 0:
        totals["fees_vs_gross_pct"] = round(totals["fees"] / totals["gross"] * 100, 1)
    return {
        "trades": rows,
        "open_trades": opens,
        "orders": orders(rows, opens),
        "curves": curves(rows),
        "money": money,
        "summary": totals,
        "generated": date.today().isoformat(),
    }


def _open_totals(open_trades: list[dict], money: dict) -> dict:
    """What the open side of the account is worth right now."""
    priced = [t for t in open_trades if t["net"] is not None]
    balance = money["balance"][-1]["value"] if money["balance"] else None
    capital = money["capital_in"]
    return {
        "open_trades": len(open_trades),
        "open_value": round(sum(t["value"] for t in priced), 2) if priced else None,
        "unrealised": round(sum(t["net"] for t in priced), 2) if priced else None,
        "starting_cash": money["starting_cash"],
        "auto_cash": money["auto_cash"],
        "capital_in": capital,
        "capital_added": money["capital_added"],
        "balance": balance,
        # Two different questions, both worth answering:
        #   trading_return  how the trades performed, deposits excluded
        #   balance_pct     how much the money you put in has grown
        "trading_return": money["trading_return"],
        "balance_pct": round((balance - capital) / capital * 100, 2)
        if balance is not None and capital else None,
    }
