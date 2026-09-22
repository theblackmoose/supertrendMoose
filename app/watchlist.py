"""Default universe and filter profiles.

Profiles come straight out of the backtest: the SMA200 gate helped on
slower, chop-prone names and actively hurt on high-ATR momentum names by
blocking entries at trend inception. So the filter is applied per ticker,
not uniformly.

    full     - close > SMA200 AND ADX > threshold
    adx_only - ADX > threshold (keeps trend-inception entries)
    none     - take every Supertrend flip
"""
from __future__ import annotations

# Tickers added to the default list after the first release. An existing
# install picks up only the ones it has never been offered, so a ticker you
# deleted on purpose stays deleted. Bump the version and add an entry here
# whenever the seed list grows.
SEED_VERSION = 2
SEED_ADDITIONS: dict[int, tuple[str, ...]] = {
    2: ("RTX", "LMT"),
}

# Display names for the seeded universe. Names for tickers added later are
# fetched from Yahoo on demand; edit here if any of these go stale.
NAMES: dict[str, str] = {
    "XOM": "Exxon Mobil Corporation",
    "MPC": "Marathon Petroleum Corporation",
    "COP": "ConocoPhillips",
    "OXY": "Occidental Petroleum Corporation",
    "SLB": "SLB (Schlumberger)",
    "EOG": "EOG Resources, Inc.",
    "PSX": "Phillips 66",
    "SPY": "SPDR S&P 500 ETF Trust",
    "QQQ": "Invesco QQQ Trust",
    "SMH": "VanEck Semiconductor ETF",
    "XLE": "Energy Select Sector SPDR Fund",
    "XLF": "Financial Select Sector SPDR Fund",
    "IWM": "iShares Russell 2000 ETF",
    "GOOGL": "Alphabet Inc.",
    "MSFT": "Microsoft Corporation",
    "AMZN": "Amazon.com, Inc.",
    "META": "Meta Platforms, Inc.",
    "AAPL": "Apple Inc.",
    "NVDA": "NVIDIA Corporation",
    "AVGO": "Broadcom Inc.",
    "AMAT": "Applied Materials, Inc.",
    "MRVL": "Marvell Technology, Inc.",
    "TSM": "Taiwan Semiconductor Manufacturing",
    "MU": "Micron Technology, Inc.",
    "AMD": "Advanced Micro Devices, Inc.",
    "LRCX": "Lam Research Corporation",
    "KLAC": "KLA Corporation",
    "ARM": "Arm Holdings plc",
    "SMCI": "Super Micro Computer, Inc.",
    "COIN": "Coinbase Global, Inc.",
    "PLTR": "Palantir Technologies Inc.",
    "TSLA": "Tesla, Inc.",
    "MSTR": "Strategy (MicroStrategy)",
    "GE": "GE Aerospace",
    "CAT": "Caterpillar Inc.",
    "UBER": "Uber Technologies, Inc.",
    "JPM": "JPMorgan Chase & Co.",
    "GS": "The Goldman Sachs Group, Inc.",
    "LLY": "Eli Lilly and Company",
    "RTX": "RTX Corporation",
    "LMT": "Lockheed Martin Corporation",
}

FULL = "full"
ADX_RISING = "adx_rising"
REGIME = "regime"
ADX_ONLY = "adx_only"
NONE = "none"

DEFAULT_WATCHLIST: list[dict] = [
    # Energy — filter improved expectancy 3.6% -> 8.1%, win rate 40% -> 73%
    {"ticker": "XOM", "sector": "Energy", "profile": FULL},
    {"ticker": "MPC", "sector": "Energy", "profile": FULL},
    {"ticker": "COP", "sector": "Energy", "profile": FULL},
    {"ticker": "OXY", "sector": "Energy", "profile": FULL},
    {"ticker": "SLB", "sector": "Energy", "profile": FULL},
    {"ticker": "EOG", "sector": "Energy", "profile": FULL},
    {"ticker": "PSX", "sector": "Energy", "profile": FULL},
    # ETFs — filter roughly neutral, keeps drawdowns tidier
    {"ticker": "SPY", "sector": "ETF", "profile": FULL},
    {"ticker": "QQQ", "sector": "ETF", "profile": FULL},
    {"ticker": "SMH", "sector": "ETF", "profile": FULL},
    {"ticker": "XLE", "sector": "ETF", "profile": FULL},
    {"ticker": "XLF", "sector": "ETF", "profile": FULL},
    {"ticker": "IWM", "sector": "ETF", "profile": FULL},
    # Mega-cap tech — mixed; GOOGL improved, META/MSFT/AMZN worsened
    {"ticker": "GOOGL", "sector": "Mega-cap tech", "profile": FULL},
    {"ticker": "MSFT", "sector": "Mega-cap tech", "profile": ADX_ONLY},
    {"ticker": "AMZN", "sector": "Mega-cap tech", "profile": ADX_ONLY},
    {"ticker": "META", "sector": "Mega-cap tech", "profile": ADX_ONLY},
    {"ticker": "AAPL", "sector": "Mega-cap tech", "profile": ADX_ONLY},
    # Semis / AI capex — SMA gate cost 2.9% of expectancy on average
    {"ticker": "NVDA", "sector": "Semis / AI", "profile": ADX_ONLY},
    {"ticker": "AVGO", "sector": "Semis / AI", "profile": FULL},
    {"ticker": "AMAT", "sector": "Semis / AI", "profile": FULL},
    {"ticker": "MRVL", "sector": "Semis / AI", "profile": FULL},
    {"ticker": "TSM", "sector": "Semis / AI", "profile": FULL},
    {"ticker": "MU", "sector": "Semis / AI", "profile": ADX_ONLY},
    {"ticker": "AMD", "sector": "Semis / AI", "profile": ADX_ONLY},
    {"ticker": "LRCX", "sector": "Semis / AI", "profile": ADX_ONLY},
    {"ticker": "KLAC", "sector": "Semis / AI", "profile": ADX_ONLY},
    {"ticker": "ARM", "sector": "Semis / AI", "profile": ADX_ONLY},
    # High ATR — the filter destroyed expectancy here (SMCI 20.1% -> -6.1%)
    {"ticker": "SMCI", "sector": "High ATR", "profile": NONE},
    {"ticker": "COIN", "sector": "High ATR", "profile": NONE},
    {"ticker": "PLTR", "sector": "High ATR", "profile": ADX_ONLY},
    {"ticker": "TSLA", "sector": "High ATR", "profile": ADX_ONLY},
    {"ticker": "MSTR", "sector": "High ATR", "profile": NONE},
    # Industrials / other liquid trenders
    {"ticker": "GE", "sector": "Industrials", "profile": FULL},
    {"ticker": "CAT", "sector": "Industrials", "profile": FULL},
    {"ticker": "UBER", "sector": "Industrials", "profile": FULL},
    {"ticker": "JPM", "sector": "Financials", "profile": FULL},
    {"ticker": "GS", "sector": "Financials", "profile": FULL},
    {"ticker": "LLY", "sector": "Healthcare", "profile": FULL},
    # Defence — the one sector here driven by government budgets rather than
    # the tech cycle, so it can trend while the semis are falling. Slower and
    # lower-ATR than the rest, which suits the SMA200 gate.
    {"ticker": "RTX", "sector": "Defence", "profile": FULL},
    {"ticker": "LMT", "sector": "Defence", "profile": FULL},
]
