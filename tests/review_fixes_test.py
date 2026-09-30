"""Regression checks for the October 2026 review fixes. Offline, like the smoke test.

    python tests/review_fixes_test.py
"""
import os
import smtplib
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = Path(tempfile.mkdtemp()) / "review.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["SCAN_ON_STARTUP"] = "false"
os.environ["NOTIFY_CHANNELS"] = ""
os.environ["STATIC_DIR"] = str(ROOT / "static")
os.environ["SECRETS_DIR"] = str(Path(tempfile.mkdtemp()))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import data, market, notify, scanner  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Price, get_session, init_db  # noqa: E402
from app.indicators import _market_closed  # noqa: E402
from app.main import _csv_safe, app  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


init_db()

print("\n1. NYSE holidays")
expected = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
            date(2026, 11, 26), date(2026, 12, 25)}
days = [date(2026, 1, 1) + timedelta(i) for i in range(365)]
got = {d for d in days if d.weekday() < 5 and _market_closed(d)}
check("2026 matches the NYSE calendar", got == expected, str(sorted(got ^ expected)))
check("Sunday July 4 2027 is observed on Monday", _market_closed(date(2027, 7, 5)))
labor = datetime(2026, 9, 7, 18, 0, tzinfo=market.NY)
check("Labor Day evening is not a missing session",
      market.last_completed_session(labor) == date(2026, 9, 4))

print("\n2. Split in the stored history")
idx = pd.bdate_range("2025-01-02", periods=300)
close = np.linspace(300, 400, 300)
old = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                    "close": close, "volume": 5e6}, index=idx)
data.store("SPLT", old, replace_all=True)
adjusted = old / 4                                    # Yahoo, after a 4-for-1 split
adjusted["volume"] = 5e6
calls = []


def fake_download(tickers, start, end):
    calls.append(start)
    t = tickers[0]
    return {t: adjusted[adjusted.index >= pd.Timestamp(start)]}


with mock.patch.object(data, "download", side_effect=fake_download), \
        mock.patch.object(data.time, "sleep"):
    data.refresh(["SPLT"])
stored = data._full_frame("SPLT")
check("re-adjusted history triggers a full reload", len(calls) == 2, f"{len(calls)} downloads")
check("no split cliff left in stored closes",
      float(stored["close"].pct_change().abs().max()) < 0.05,
      f"largest daily move {stored['close'].pct_change().abs().max():.1%}")
check("oldest bar kept", stored.index[0] == idx[0])

calls.clear()
with mock.patch.object(data, "download", side_effect=fake_download), \
        mock.patch.object(data.time, "sleep"):
    data.refresh(["SPLT"])
check("matching history is only topped up", len(calls) == 1, f"{len(calls)} downloads")

print("\n3. One scan at a time, flag always cleared")
gate = threading.Event()


def slow(*a, **k):
    gate.wait(5)
    return {"skipped": False, "tickers": 0, "buys": [], "exits": [], "failed": [],
            "notified": {}, "duration_sec": 0}


with mock.patch.object(scanner, "_run_scan", side_effect=slow):
    t = threading.Thread(target=scanner.run_scan)
    t.start()
    second = scanner.run_scan()
    gate.set()
    t.join()
check("a second concurrent scan is skipped", second.get("skipped") is True)

with mock.patch.object(scanner, "_run_scan", side_effect=RuntimeError("boom")):
    scanner._scan_state["running"] = True
    try:
        scanner.run_scan()
    except RuntimeError:
        pass
check("running flag cleared after a crash", scanner.scan_status()["running"] is False)
check("lock released after a crash", scanner._scan_lock.acquire(blocking=False))
scanner._scan_lock.release()

print("\n4. Undelivered alerts stay unsent")
marked = []
st = {"ticker": "SPLT", "date": date.today(), "flip_up": True, "flip_dn": False,
      "passed": True, "reason": "", "close": 100.0, "stop": 95.0, "adx": 25.0,
      "atr_pct": 2.0, "above_sma": True, "position": None}
with mock.patch.object(scanner, "evaluate", return_value=st), \
        mock.patch.object(scanner, "_record"), \
        mock.patch.object(scanner, "_already_notified", return_value=False), \
        mock.patch.object(scanner, "_alert_if_broken", return_value=False), \
        mock.patch.object(scanner, "_mark_notified", side_effect=marked.append), \
        mock.patch.object(scanner.notify, "send", return_value={"email": False}):
    scanner.run_scan(refresh_prices=False, notify_on=True)
check("failed send does not mark the signal alerted", marked == [])

print("\n5. Email TLS verifies the certificate")
seen = {}


class FakeSMTP:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): seen["context"] = context
    def login(self, *a): pass
    def send_message(self, msg): pass


with mock.patch.object(smtplib, "SMTP", FakeSMTP), \
        mock.patch.multiple(settings, smtp_host="smtp.example.com", smtp_port=587,
                            smtp_to=["me@example.com"], smtp_user="u", smtp_tls=True):
    notify._email("t", "b")
ctx = seen.get("context")
check("starttls gets a verifying context",
      ctx is not None and ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname)

print("\n6. API hardening")
client = TestClient(app)
r = client.patch("/api/watchlist/SPY", json={"profile": "fulll"})
check("unknown profile rejected", r.status_code == 422, str(r.status_code))
r = client.patch("/api/watchlist/SPY", json={"enabled": None})
check("null enabled rejected", r.status_code == 422, str(r.status_code))
r = client.get("/api/chart/SPY", params={"atr_len": 100000})
check("chart ATR length bounded", r.status_code == 422, str(r.status_code))
r = client.get("/api/health")
check("security headers present", "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""))
check("CSV formula neutralised", _csv_safe(["=HYPERLINK(1)", "-5", -5.0, "ok"])
      == ["'=HYPERLINK(1)", "'-5", -5.0, "ok"])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL REVIEW CHECKS PASSED")
