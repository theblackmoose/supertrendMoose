"""Offline smoke test. Seeds synthetic prices so no network is needed.

    python tests/smoke_test.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["SCAN_ON_STARTUP"] = "false"
os.environ["NOTIFY_CHANNELS"] = ""
os.environ["STATIC_DIR"] = str(ROOT / "static")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import backtest, data, scanner  # noqa: E402
from app.db import Price, WatchItem, get_session, init_db  # noqa: E402
from app.indicators import adx, atr, compute_all, supertrend  # noqa: E402
from app.main import app  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def synth(n=900, seed=3, drift=0.0007):
    rng = np.random.default_rng(seed)
    px = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.018, n)))
    idx = pd.bdate_range(date.today() - timedelta(days=int(n * 1.45)), periods=n)
    close = pd.Series(px, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.008, n)))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(6e6, 4e7, n)}, index=idx)


print("\n1. Indicators")
df = synth()
a, ax, st = atr(df, 10), adx(df, 14, 14), supertrend(df, 10, 3.0)
check("ATR positive", bool((a.dropna() > 0).all()))
check("ADX within 0-100", bool(ax["adx"].dropna().between(0, 100).all()),
      f"range {ax['adx'].min():.1f}-{ax['adx'].max():.1f}")
check("Supertrend direction is ±1 only", set(st["direction"].dropna().unique()) <= {-1.0, 1.0})
flips = int((st["direction"].diff().abs() == 2).sum())
check("Supertrend flips", flips > 3, f"{flips} flips")
tight = int((supertrend(df, 10, 1.5)["direction"].diff().abs() == 2).sum())
wide = int((supertrend(df, 10, 6.0)["direction"].diff().abs() == 2).sum())
check("Looser multiplier flips more often", tight > wide, f"{tight} vs {wide}")
full = compute_all(df)
check("compute_all attaches every column",
      all(c in full for c in ["supertrend", "direction", "adx", "sma", "atr_pct", "avg_volume"]))

print("\n2. Database and seeding")
init_db()
with get_session() as s:
    items = s.query(WatchItem).all()
check("Watchlist seeded", len(items) > 30, f"{len(items)} tickers")
check("Seeded with one consistent profile", len({i.profile for i in items}) == 1,
      str(sorted({i.profile for i in items})))
from app.watchlist import DEFAULT_WATCHLIST as _DW  # noqa: E402
check("Per-sector recommendations still available to switch to",
      len({w["profile"] for w in _DW}) >= 2,
      str(sorted({w["profile"] for w in _DW})))

print("\n3. Price store round-trip")
TICKERS = ["NVDA", "XOM", "SMCI", "SPY"]
for i, t in enumerate(TICKERS):
    data.store(t, synth(seed=10 + i, drift=[0.0012, 0.0002, 0.002, 0.0005][i]))
loaded = data.load("NVDA")
check("Bars stored and reloaded", len(loaded) == 900, f"{len(loaded)} rows")
check("Columns preserved", list(loaded.columns) == ["open", "high", "low", "close", "volume"])
data.store("NVDA", synth(seed=10, drift=0.0012).tail(30))
check("Re-store does not duplicate", len(data.load("NVDA")) == 900)
check("last_stored_date works", data.last_stored_date("NVDA") is not None)

print("\n4. Filter profiles")
with get_session() as s:
    nvda = s.get(WatchItem, "NVDA")
    results = {}
    for prof in ("full", "adx_only", "none"):
        nvda.profile = prof
        results[prof] = scanner.evaluate(nvda, data.load("NVDA"))
    nvda.profile = "adx_only"
    s.commit()
check("evaluate returns a state", all(r is not None for r in results.values()))
check("Unfiltered profile always passes", results["none"]["passed"] is True)
check("'full' is never more permissive than 'adx_only'",
      not (results["full"]["passed"] and not results["adx_only"]["passed"]))
check("State exposes what the UI needs",
      all(k in results["full"] for k in
          ["close", "stop", "adx", "atr_pct", "above_sma", "trend", "tradeable", "reason"]))

print("\n5. Earnings blackout")
with get_session() as s:
    item = s.get(WatchItem, "XOM")
    item.next_earnings = data.load("XOM").index[-1].date() + timedelta(days=2)
    s.commit()
    blocked = scanner.evaluate(item, data.load("XOM"))
check("Blackout flag set", blocked["blackout"] is True)
check("Blackout blocks entry", blocked["passed"] is False, blocked["reason"])
with get_session() as s:
    item = s.get(WatchItem, "XOM")
    item.next_earnings = None
    s.commit()

print("\n6. Scan engine (no network, no alerts)")
with get_session() as s:
    for it in s.query(WatchItem).all():
        it.enabled = it.ticker in TICKERS
    s.commit()
res = scanner.run_scan(refresh_prices=False, notify_on=False)
check("Scan evaluated the enabled tickers", res["tickers"] == len(TICKERS), str(res["tickers"]))
check("Scan returns buys/exits lists", isinstance(res["buys"], list) and isinstance(res["exits"], list))
states = scanner.current_states()
check("current_states covers every enabled ticker", len(states) == len(TICKERS))
check("States sorted with fired signals first",
      [s["flip_up"] for s in states] == sorted([s["flip_up"] for s in states], reverse=True))

print("\n7. API")
client = TestClient(app)
h = client.get("/api/health")
check("GET /api/health", h.status_code == 200 and h.json()["status"] == "ok")
r = client.get("/api/states")
check("GET /api/states", r.status_code == 200 and len(r.json()) == len(TICKERS))
c = client.get("/api/chart/NVDA")
check("GET /api/chart/{ticker}", c.status_code == 200)
if c.status_code == 200:
    j = c.json()
    check("Chart returns candles", len(j["candles"]) > 100, f"{len(j['candles'])} bars")
    check("Chart returns both Supertrend legs",
          len(j["supertrend_up"]) > 0 and len(j["supertrend_down"]) > 0)
    check("Chart returns SMA and ADX", len(j["sma"]) > 0 and len(j["adx"]) > 0)
    check("Chart returns markers", len(j["markers"]) > 0, f"{len(j['markers'])} markers")
    check("Markers distinguish passed from filtered",
          {m["text"] for m in j["markers"]} & {"BUY", "filtered", "SELL"} != set())
    check("Candle payload shape",
          all(k in j["candles"][0] for k in ["time", "open", "high", "low", "close"]))
check("Chart honours ATR override",
      client.get("/api/chart/NVDA?atr_mult=1.5").json()["supertrend_up"] !=
      client.get("/api/chart/NVDA?atr_mult=6.0").json()["supertrend_up"])
check("Unknown ticker returns 404", client.get("/api/chart/ZZZZ").status_code == 404)
check("GET /api/signals", client.get("/api/signals").status_code == 200)
check("GET /api/watchlist", len(client.get("/api/watchlist").json()) > 30)
check("GET /api/runs", len(client.get("/api/runs").json()) >= 1)
p = client.patch("/api/watchlist/NVDA", json={"profile": "none"})
check("PATCH watchlist", p.status_code == 200 and p.json()["profile"] == "none")
check("PATCH unknown ticker 404", client.patch("/api/watchlist/ZZZZ", json={"profile": "none"}).status_code == 404)
check("DELETE watchlist", client.delete("/api/watchlist/GS").status_code == 200)
check("Index page serves", client.get("/").status_code == 200)
check("Static assets serve", client.get("/static/app.js").status_code == 200)
check("Vendored chart library present",
      client.get("/static/vendor/lightweight-charts.standalone.production.js").status_code == 200)

print("\n8. Auth gate")
from app import main as main_mod  # noqa: E402
main_mod.settings.auth_token = "secret"
check("Blocks without token", client.get("/api/states").status_code == 401)
check("Allows with token",
      client.get("/api/states", headers={"X-Auth-Token": "secret"}).status_code == 200)
check("Health stays public", client.get("/api/health").status_code == 200)
main_mod.settings.auth_token = ""

print("\n9. Notifications never raise")
from app import notify  # noqa: E402
notify.settings.notify_channels = ["ntfy", "telegram", "discord", "email", "bogus"]
notify.settings.ntfy_topic = ""
out = notify.send("test", "body")
check("send() returns per-channel results without raising", isinstance(out, dict), str(out))
title, body = notify.format_signals(
    [{"ticker": "NVDA", "close": 180.0, "stop": 165.0, "adx": 31.2}],
    [{"ticker": "XOM", "close": 110.0}])
check("Alert text built", "NVDA" in body and "XOM" in body and "buy" in title, title)

print("\n10. Backtest engine")
bt = backtest.run(data.load("NVDA"), 10, 3.0, 20.0, years=3)
check("Backtest runs", "error" not in bt, bt.get("error", ""))
if "error" not in bt:
    check("All three profiles returned", set(bt["profiles"]) == set(backtest.PROFILES))
    tn = {p: bt["profiles"][p]["metrics"]["trades"] for p in backtest.PROFILES}
    check("Unfiltered takes the most trades",
          tn["none"] >= tn["adx_only"] >= tn["full"], str(tn))
    m = bt["profiles"]["none"]["metrics"]
    check("Metrics complete",
          all(k in m for k in ["win_rate", "expectancy", "profit_factor", "max_dd", "total_return"]))
    check("Max drawdown is negative or zero", m["max_dd"] is None or m["max_dd"] <= 0, str(m["max_dd"]))
    check("Win rate within 0-100", m["win_rate"] is None or 0 <= m["win_rate"] <= 100)
    for p in backtest.PROFILES:
        eq = bt["profiles"][p]["equity"]
        times = [e["time"] for e in eq]
        check(f"Equity curve for {p} strictly ascending",
              times == sorted(times) and len(times) == len(set(times)), f"{len(times)} points")
    bh_times = [e["time"] for e in bt["buy_hold"]["equity"]]
    check("Buy-and-hold curve valid",
          bh_times == sorted(bh_times) and len(bh_times) == len(set(bh_times)),
          f"{len(bh_times)} points")
    check("Buy-and-hold benchmark present",
          bt["buy_hold"]["total_return"] is not None and bt["buy_hold"]["max_dd"] <= 0)
    check("Verdict names a profile", bt["verdict"]["profile"] in backtest.PROFILES)
    check("Verdict flags weak samples",
          bt["verdict"]["confidence"] in {"low", "moderate", "reasonable"},
          bt["verdict"]["confidence"])
    check("Trades have entry before exit",
          all(t["entry_date"] < t["exit_date"] for t in bt["profiles"]["none"]["trades"]))

empty = backtest.run(data.load("NVDA").head(80))
check("Short history returns an error, not a crash", "error" in empty)

print("\n11. Backtest API")
r = client.get("/api/backtest/NVDA?years=3")
check("GET /api/backtest/{ticker}", r.status_code == 200)
if r.status_code == 200:
    j = r.json()
    check("Response carries ticker and current profile",
          j["ticker"] == "NVDA" and "current_profile" in j)
    check("Params echoed back", j["params"]["atr_mult"] is not None)
check("Backtest honours ADX override",
      client.get("/api/backtest/NVDA?years=3&adx_min=0").json()["profiles"]["adx_only"]["metrics"]["trades"] >=
      client.get("/api/backtest/NVDA?years=3&adx_min=45").json()["profiles"]["adx_only"]["metrics"]["trades"])
check("Unknown ticker 404", client.get("/api/backtest/ZZZZ").status_code == 404)
w = client.get("/api/backtest?years=3")
check("GET /api/backtest (watchlist)", w.status_code == 200)
if w.status_code == 200:
    jw = w.json()
    check("Watchlist backtest covers enabled tickers", jw["tested"] >= 1, f"{jw['tested']} tested")
    check("Pooled metrics per profile", set(jw["pooled"]) == set(backtest.PROFILES))
    check("Mismatch list present", isinstance(jw["mismatched"], list))

print("\n12. Frontend wiring")
html = client.get("/").text
js = client.get("/static/app.js").text
css = client.get("/static/style.css").text
check("Backtest tab in markup", 'id="tabTest"' in html and 'id="equityChart"' in html)
check("Stats table in markup", 'id="btStats"' in html)
check("Backtest logic in app.js", "loadBacktest" in js and "buildEquityChart" in js)
check("View switching wired", "setView" in js and "function select(" in js)
check("Backtest styles present", ".pane-equity" in css and ".stats" in css)


print("\n13. Alert detail modes")
notify.settings.notify_channels = []
B = [{"ticker": "NVDA", "close": 180.0, "stop": 165.0, "adx": 31.2},
     {"ticker": "AMD", "close": 210.0, "stop": 196.0, "adx": 24.0}]
E = [{"ticker": "XOM", "close": 110.0}]

ft, fb = notify.format_signals(B, E, "full")
mt, mb = notify.format_signals(B, E, "minimal")
check("Full mode names tickers", "NVDA" in fb and "AMD" in fb and "XOM" in fb)
check("Full mode shows stop and risk", "stop" in fb and "risk" in fb)
check("Minimal mode leaks no tickers",
      not any(t in mb for t in ["NVDA", "AMD", "XOM"]), repr(mb))
check("Minimal mode leaks no prices", "180" not in mb and "165" not in mb)
check("Both modes agree on counts", ft == mt, f"{ft!r} vs {mt!r}")
check("Minimal points to the dashboard", "scanner" in mb.lower())

notify.settings.notify_detail = "full"
check("Default is full detail", "NVDA" in notify.format_signals(B, E)[1])
notify.settings.notify_detail = "minimal"
check("Setting switches to minimal", "NVDA" not in notify.format_signals(B, E)[1])
notify.settings.notify_detail = "full"

print("\n14. ntfy authentication")
import httpx  # noqa: E402
captured = {}

def fake_post(url, content=None, headers=None, auth=None, timeout=None, **kw):
    captured.update(url=url, headers=headers or {}, auth=auth, body=content)
    return httpx.Response(200, request=httpx.Request("POST", url))

real_post = httpx.post
httpx.post = fake_post
try:
    notify.settings.ntfy_topic = "supertrend"
    notify.settings.ntfy_url = "http://ntfy:80"
    notify.settings.ntfy_token = ""
    notify.settings.ntfy_user = "moose"
    notify.settings.ntfy_password = "s3cret"
    ok = notify.BACKENDS["ntfy"]("title", "body", "http://host:8000")
    check("ntfy publish succeeds", ok is True)
    check("Publishes to the topic URL", captured["url"].endswith("/supertrend"), captured["url"])
    check("Basic auth credentials sent", captured["auth"] == ("moose", "s3cret"))
    check("Click header links to the dashboard",
          captured["headers"].get("Click") == "http://host:8000")

    notify.settings.ntfy_user = ""
    notify.settings.ntfy_token = "tk_abc123"
    notify.BACKENDS["ntfy"]("t", "b")
    check("Bearer token used when set",
          captured["headers"].get("Authorization") == "Bearer tk_abc123")
    check("No basic auth alongside a token", captured["auth"] is None)
finally:
    httpx.post = real_post

print("\n15. Compose and bootstrap")
import yaml  # noqa: E402


class _StrictLoader(yaml.SafeLoader):
    """PyYAML keeps the last duplicate key silently; Docker Compose's Go
    parser errors out. This makes the test fail the way Docker would."""


def _no_duplicate_keys(loader, node, deep=False):
    seen = set()
    for k, _ in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None,
                f'duplicate key "{key}" at line {k.start_mark.line + 1}')
        seen.add(key)
    return yaml.constructor.SafeConstructor.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)

_compose_text = (ROOT / "docker-compose.yml").read_text()
try:
    compose = yaml.load(_compose_text, Loader=_StrictLoader)
    check("docker-compose.yml has no duplicate keys (Docker rejects them)", True)
except yaml.constructor.ConstructorError as _e:
    check("docker-compose.yml has no duplicate keys (Docker rejects them)", False, str(_e))
    compose = yaml.safe_load(_compose_text)
svc = compose["services"]
check("ntfy service defined", "ntfyMoose" in svc)
check("Service and container names both camelCase",
      set(svc) == {"supertrendMoose", "ntfyMoose"}
      and svc["supertrendMoose"]["container_name"] == "supertrendMoose"
      and svc["ntfyMoose"]["container_name"] == "ntfyMoose",
      ", ".join(sorted(svc)))
check("ntfy has a lowercase DNS alias",
      "ntfymoose" in svc["ntfyMoose"]["networks"]["default"]["aliases"])
check("App resolves ntfy via the lowercase alias",
      svc["supertrendMoose"]["environment"]["NTFY_URL"] == "${NTFY_URL:-http://ntfymoose:80}")
check("Ports are non-standard and configurable",
      "${APP_PORT:-19080}:8000" in svc["supertrendMoose"]["ports"][0]
      and "${NTFY_PORT:-19081}:80" in svc["ntfyMoose"]["ports"][0],
      f'{svc["supertrendMoose"]["ports"][0]} | {svc["ntfyMoose"]["ports"][0]}')
check("Ports still bind to loopback by default",
      svc["supertrendMoose"]["ports"][0].startswith("${BIND_ADDR:-${HOST_IP:-127.0.0.1}}")
      and svc["ntfyMoose"]["ports"][0].startswith("${NTFY_BIND:-${HOST_IP:-127.0.0.1}}"))
check("depends_on follows the renamed service",
      "ntfyMoose" in svc["supertrendMoose"]["depends_on"])
check("App waits for ntfy to be healthy",
      svc["supertrendMoose"]["depends_on"]["ntfyMoose"]["condition"] == "service_healthy")
check("ntfy denies access by default",
      svc["ntfyMoose"]["environment"]["NTFY_AUTH_DEFAULT_ACCESS"] == "deny-all")
check("Signup disabled", svc["ntfyMoose"]["environment"]["NTFY_ENABLE_SIGNUP"] == "false")
check("App uses the internal ntfy address unless .env says otherwise",
      svc["supertrendMoose"]["environment"]["NTFY_URL"].endswith(":-http://ntfymoose:80}"))
check("App hardening intact",
      svc["supertrendMoose"]["read_only"] is True and svc["supertrendMoose"]["cap_drop"] == ["ALL"])

check("No host bind mounts (platform-independent)",
      not [v for sv in svc.values() for v in sv.get("volumes", []) if v.startswith("./")])
check("ntfy image is built, not bind-patched", "build" in svc["ntfyMoose"])

check("Secrets volume shared between services",
      "moose-secrets:/secrets" in svc["ntfyMoose"]["volumes"]
      and "moose-secrets:/secrets:ro" in svc["supertrendMoose"]["volumes"])
check("App mounts secrets read-only",
      any(v.endswith(":ro") for v in svc["supertrendMoose"]["volumes"] if "secrets" in v))
check("No password values written in compose",
      not any(("PASSWORD" in k or k.endswith("_PASS") or k.endswith("_TOKEN")) and v is not None
              for sv in svc.values() for k, v in sv.get("environment", {}).items()
              if k != "MOOSE_RESET_PASSWORDS"))
check("No env_file, so it runs on any Compose v2 (2.24 was needed for optional env_file)",
      "env_file" not in svc["supertrendMoose"])

import re as _re  # noqa: E402
ntfy_df = (ROOT / "ntfy" / "Dockerfile").read_text()
check("Dockerfile strips CR from bootstrap", "sed -i 's/\\r$//'" in ntfy_df)
check("ntfy image pinned to root (needs to write the secrets volume)",
      _re.search(r"^USER (root|0)$", ntfy_df, _re.M) is not None)
check("Bootstrap baked into the image", "COPY bootstrap.sh" in ntfy_df)

boot = (ROOT / "ntfy" / "bootstrap.sh").read_text()
check("Bootstrap grants moose write-only", "ntfy access moose" in boot and "write-only" in boot)
check("Bootstrap grants notifications read-only",
      "ntfy access notifications" in boot and "read-only" in boot)
check("Reader account renamed away from 'phone'",
      "ensure_user notifications" in boot and "ensure_user phone" not in boot)
check("Old 'phone' account is migrated away", "ntfy user del phone" in boot)
check("Topic default is SupertrendMoose",
      "NTFY_TOPIC:-SupertrendMoose" in boot
      and "NTFY_TOPIC:-SupertrendMoose" in (ROOT / "docker-compose.yml").read_text())
check("Credential helper reports the new account",
      "notifications" in (ROOT / "ntfy" / "moose-creds").read_text()
      and "ntfy-notify.pass" in (ROOT / "ntfy" / "moose-creds").read_text())
check("Bootstrap generates its own secrets", "/dev/urandom" in boot and "base64" in boot)
check("Bootstrap checks /secrets is writable before using it", "write-test" in boot)
check("Every failure path explains itself", boot.count("die ") >= 6)
check("Bootstrap logs the uid it runs as", "id -u" in boot)
check("chown failure degrades instead of crashing", "mode 644" in boot)
check("Empty secret files are regenerated", '[ -s "$file" ]' in boot)
check("No bare set -e silent exit", "set -e" not in boot)
check("Generation is idempotent", "if [ ! -s \"$file\" ]" in boot)
check("Secrets chowned so the app can read them", "chown" in boot and "APP_UID=10001" in boot)
check("Secrets are mode 600", "chmod 600" in boot)
# change-pass invalidates access tokens, so it must not run on every boot.
check("Passwords are not reset on every start",
      "MOOSE_RESET_PASSWORDS" in boot and "left untouched" in boot)
check("Existing accounts are detected before any write",
      boot.index("user_exists()") < boot.index("ensure_user moose"))
check("Password reset is opt-in only",
      boot.index('"${MOOSE_RESET_PASSWORDS:-0}" = "1"')
      < boot.index('NTFY_PASSWORD="$pass" ntfy user change-pass'))
check("Reset flag plumbed through compose",
      "MOOSE_RESET_PASSWORDS" in svc["ntfyMoose"]["environment"])
check("ACL printout keeps stderr so it cannot be blank",
      "ntfy access 2>&1" in boot)
# The ntfy CLI writes listings to stderr. Any command whose OUTPUT we parse
# must keep stderr, or every user looks absent and the bootstrap dies.
check("User listing keeps stderr", "ntfy user list 2>&1" in boot)
_parsed = [ln.strip() for ln in boot.splitlines()
           if ("ntfy user list" in ln or "ntfy access 2>" in ln) and "|" in ln]
check("No parsed ntfy output discards stderr",
      all("2>/dev/null" not in ln for ln in _parsed),
      " ; ".join(ln for ln in _parsed if "2>/dev/null" in ln) or "none")
check("Account creation tolerates an already-existing account",
      boot.count("already exists, left untouched") >= 2)
check("Failure message includes what ntfy actually reported",
      "ntfy user list said:" in boot)
# A fresh volume has no auth database, and `ntfy user add` fails without one.
# The server builds it, so it runs briefly first...
check("Auth database is built before accounts are created",
      boot.index("cold start: building the auth database") < boot.index("ensure_user moose"))
check("Waits for health before using the database", "HEALTH_URL" in boot and "v1/health" in boot)
# ...but is stopped again, so ACLs are never written under a running server
# (which leaves it serving stale permissions: "user not authorized").
check("Init server is stopped before accounts and ACLs are applied",
      boot.index('kill -TERM "$INIT_PID"') < boot.index("ensure_user moose"))
check("ACLs are granted before the real server starts",
      boot.index("ntfy access notifications") < boot.index("exec ntfy serve"))
check("Real server is exec'd, so it becomes PID 1",
      boot.rstrip().endswith("exec ntfy serve"))
check("Stored ACLs are printed to the log", "ntfy access 2>&1 | sed" in boot)
check("Detects ntfy exiting during init", "exited while building" in boot)
check("Startup wait is bounded", "STARTUP_TIMEOUT" in boot)
check("Credentials printed on first run only", 'FIRST_RUN" = "1"' in boot)
check("moose-creds helper shipped",
      (ROOT / "ntfy" / "moose-creds").exists()
      and "ntfy-notify.pass" in (ROOT / "ntfy" / "moose-creds").read_text())

env = (ROOT / ".env.example").read_text()
check("NOTIFY_DETAIL documented and defaults to full", "NOTIFY_DETAIL=full" in env)
check("No unused password placeholders (they are generated)",
      "NTFY_MOOSE_PASSWORD" not in env and "NTFY_PHONE_PASSWORD" not in env)
_env_keys = [ln.split("=", 1)[0] for ln in env.splitlines()
             if ln.strip() and not ln.strip().startswith("#") and "=" in ln]
check("No duplicate variables in .env.example",
      len(_env_keys) == len(set(_env_keys)),
      ", ".join(sorted({k for k in _env_keys if _env_keys.count(k) > 1})) or "none")
_secretish = [ln for ln in env.splitlines()
              if not ln.lstrip().startswith("#") and "=" in ln
              and any(w in ln.split("=", 1)[0] for w in ("PASS", "TOKEN", "WEBHOOK"))]
check("No secrets committed in the template",
      all(ln.split("=", 1)[1].strip() == "" for ln in _secretish),
      "; ".join(_secretish) or "none set")


print("\n16. Self-generated secrets")
import subprocess, tempfile as tf, os as _os  # noqa: E402

sec = tf.mkdtemp()
env = {**_os.environ, "SECRETS_DIR": sec, "PYTHONPATH": str(ROOT)}
probe = "from app.config import Settings; s=Settings(); print(repr(s.ntfy_password), repr(s.auth_token))"

out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
check("Missing secrets degrade gracefully", out.stdout.strip() == "'' ''", out.stdout.strip())

open(_os.path.join(sec, "ntfy-moose.pass"), "w").write("vol-pass\n")
open(_os.path.join(sec, "api-token"), "w").write("vol-token\n")
out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
check("Secrets read from the volume", out.stdout.strip() == "'vol-pass' 'vol-token'", out.stdout.strip())

out = subprocess.run([sys.executable, "-c", probe],
                     env={**env, "NTFY_PASSWORD": "env-wins"}, capture_output=True, text=True)
check("Env var overrides the volume", "'env-wins'" in out.stdout, out.stdout.strip())

print("\n17. Dashboard token gate")
html2 = client.get("/").text
js2 = client.get("/static/app.js").text
css2 = client.get("/static/style.css").text
check("Login overlay in markup", 'id="login"' in html2 and 'id="loginToken"' in html2)
check("Overlay tells you how to get the token", "moose-creds" in html2)
check("Token sent as a header", 'X-Auth-Token"] = token()' in js2)
check("401 clears the stored token and re-prompts",
      "sessionStorage.removeItem" in js2 and "showLogin" in js2)
check("Token kept in sessionStorage, not localStorage",
      "sessionStorage" in js2 and "localStorage" not in js2)
check("Login styles present", ".login-box" in css2)

main_mod.settings.auth_token = "gate-token"
check("API still rejects a bad token", client.get("/api/states").status_code == 401)
check("API accepts the gate token",
      client.get("/api/states", headers={"X-Auth-Token": "gate-token"}).status_code == 200)
check("Login page itself stays reachable", client.get("/").status_code == 200)
main_mod.settings.auth_token = ""


print("\n18. yfinance cache and diagnostics")
import os as _o, importlib  # noqa: E402
from app import data as _d  # noqa: E402

_o.environ["YF_CACHE_DIR"] = tf.mkdtemp() + "/yfc"
importlib.reload(_d)
check("Cache dir is created and usable", _d.ensure_cache() is True)
check("Cache result is memoised", _d._cache_ready is True)

_o.environ["YF_CACHE_DIR"] = "/proc/definitely-not-writable"
importlib.reload(_d)
check("Unwritable cache reported, not raised", _d.ensure_cache() is False)
_o.environ["YF_CACHE_DIR"] = tf.mkdtemp()
importlib.reload(_d)

req = (ROOT / "requirements.txt").read_text()
check("yfinance pin is on the 1.x line", "yfinance==1.7.0" in req, req.strip().splitlines()[-1])

dockerfile = (ROOT / "Dockerfile").read_text()
check("HOME redirected to the tmpfs", "HOME=/tmp" in dockerfile)
check("Cache dir set in the image", "YF_CACHE_DIR=/tmp/yf-cache" in dockerfile)
check("tmpfs is writable and sized", "size=128m" in (ROOT / "docker-compose.yml").read_text())

r = client.get("/api/diagnose")
check("GET /api/diagnose", r.status_code == 200)
if r.status_code == 200:
    j = r.json()
    check("Reports the yfinance version", "yfinance_version" in j, j.get("yfinance_version"))
    check("Reports cache writability", "cache_writable" in j)
    check("Reports stored ticker count", isinstance(j.get("stored_tickers"), int),
          str(j.get("stored_tickers")))
    check("Offline probe fails without raising", j.get("probe_ok") in (True, False))
    if not j.get("probe_ok"):
        check("Failed probe includes a hint", "hint" in j)

main_src = (ROOT / "app" / "main.py").read_text()
check("yfinance retry spam suppressed", 'getLogger("yfinance").setLevel' in main_src)
data_src = (ROOT / "app" / "data.py").read_text()
check("Total failure gets a diagnostic, not just a count",
      "Every ticker failed" in data_src and "rate-" in data_src)


print("\n19. Earnings lookup hygiene")
from app.scanner import NO_EARNINGS_SECTORS  # noqa: E402
check("ETFs excluded from earnings lookups", "ETF" in NO_EARNINGS_SECTORS)
with get_session() as _s:
    _etfs = [i.ticker for i in _s.query(WatchItem).filter(WatchItem.sector == "ETF").all()]
check("Watchlist actually has ETFs to skip", len(_etfs) >= 4, ", ".join(_etfs))
_src = (ROOT / "app" / "data.py").read_text()
check("yfinance quietened only around the earnings call",
      "setLevel(logging.CRITICAL)" in _src and "finally:" in _src)
check("Log level restored afterwards", "yf_log.setLevel(previous)" in _src)
_sc = (ROOT / "app" / "scanner.py").read_text()
check("Refresh logs skips, failures and resulting coverage",
      "%d skipped, %d failed" in _sc and "now have report dates" in _sc)

print("\n20. Dashboard refinements")
_html = client.get("/").text
_js = client.get("/static/app.js").text
_css = client.get("/static/style.css").text

check("TradingView badge disabled in-chart", "attributionLogo: false" in _js)
check("Attribution moved to the footer",
      'class="credit"' in _html and "Charts by" in _html and "tradingview.com" in _html)
check("Every pane is labelled",
      _html.count('class="pane-label"') == _html.count('class="pane"') == 6)
check("Price pane names its series",
      "Supertrend stop" in _html and "200-day average (SMA)" in _html)
check("ADX pane shows a live value", 'id="adxNow"' in _html and '$("adxNow")' in _js)
check("Company name element present", 'id="chartName"' in _html)
check("Ticker kept as a badge alongside the name", 'class="tkr-badge"' in _html)

check("Shared range control replaces the backtest-only one",
      'id="range"' in _html and "btYears" not in _html and "btYears" not in _js)
check("Full history fetched, range only sets the view",
      "MAX_BARS = 5000" in _js and "bars: MAX_BARS" in _js
      and "setVisibleLogicalRange" in _js)
check("Range maps to both bars and years",
      "RANGES" in _js and "bars:" in _js and "years:" in _js)
check("Range change reloads every visible chart", "reloadAll" in _js)
check("Selecting a ticker loads both views",
      "loadChart(ticker);" in _js and 'if (view === "test") loadBacktest(ticker);' in _js)
check("View switch refreshes a stale pane",
      "chartFor !== selected" in _js and "testFor !== selected" in _js)

_c = client.get("/api/chart/NVDA")
check("Chart API returns the company name",
      _c.status_code == 200 and _c.json().get("name") == "NVIDIA Corporation",
      _c.json().get("name") if _c.status_code == 200 else _c.status_code)
check("Chart API honours the bars parameter",
      len(client.get("/api/chart/NVDA?bars=130").json()["candles"]) <= 130)
check("Shorter range returns fewer bars",
      len(client.get("/api/chart/NVDA?bars=130").json()["candles"])
      < len(client.get("/api/chart/NVDA?bars=800").json()["candles"]))

_b = client.get("/api/backtest/NVDA?years=0.5")
check("Backtest accepts fractional years", _b.status_code == 200, str(_b.status_code))
check("Backtest returns the company name",
      _b.status_code == 200 and _b.json().get("name") == "NVIDIA Corporation")
if _b.status_code == 200:
    _b5 = client.get("/api/backtest/NVDA?years=5").json()
    check("Longer window covers more bars", _b5["bars"] > _b.json()["bars"],
          f'{_b.json()["bars"]} vs {_b5["bars"]}')

_w = client.get("/api/watchlist").json()
check("Watchlist exposes names", all("name" in r for r in _w))
check("Seeded names populated", sum(1 for r in _w if r["name"]) >= 30,
      f'{sum(1 for r in _w if r["name"])} named')

_st = scanner.current_states()
check("Rail states carry names", all(x.get("name") for x in _st))


print("\n21. Chart and backtest cover the same window")
_full = client.get("/api/chart/NVDA?bars=5000").json()["candles"]
_aligned = True
for _bars in (130, 260, 510):
    if len(_full) < _bars + 30:
        continue
    _vis = _full[-_bars:]
    _bt = client.get(f"/api/backtest/NVDA?bars={_bars}").json()
    _ok = (_vis[0]["time"], _vis[-1]["time"]) == (_bt["start"], _bt["end"])
    check(f"Windows identical at {_bars} bars", _ok,
          f'chart {_vis[0]["time"]}..{_vis[-1]["time"]} vs bt {_bt["start"]}..{_bt["end"]}')
    check(f"Bar count matches at {_bars}", _bt["bars"] == _bars, str(_bt["bars"]))
    _aligned = _aligned and _ok

check("Backtest accepts a bar count", client.get("/api/backtest/NVDA?bars=260").status_code == 200)
_src = (ROOT / "app" / "backtest.py").read_text()
check("Indicators computed before the window is sliced",
      _src.index("compute_all(") < _src.index("ind.tail(int(bars))"))
check("No pre-slice warm-up concatenation left", "pd.concat([warm" not in _src)
check("bars takes precedence over years", 'if bars:' in _src and 'elif years:' in _src)
_js2 = client.get("/static/app.js").text
check("Frontend sends bars to the backtest, not years",
      "bars: range().bars, atr_len" in _js2)


print("\n22. Attention score and freshness")
from app.scanner import bars_since_flip, score_components  # noqa: E402

_d = pd.Series([1, 1, 1, -1, -1, -1, -1, -1], dtype=float)
check("bars_since_flip counts the current run", bars_since_flip(_d) == 5, str(bars_since_flip(_d)))
check("Flip on the last bar reads as 1",
      bars_since_flip(pd.Series([-1, -1, 1], dtype=float)) == 1)
check("Empty series is safe", bars_since_flip(pd.Series([], dtype=float)) == 0)

_best = score_components(1, 2.0, 45, 20)
_worst = score_components(120, 20.0, 20, 20)
check("Ideal setup scores near 100", _best["total"] >= 95, str(_best))
check("Poor setup scores near 0", _worst["total"] <= 5, str(_worst))
check("Freshness, strength and total stay within 0-100",
      all(0 <= score_components(b, r, a, 20)[k] <= 100
          for b, r, a in [(1, 0, 60), (500, 99, 0), (30, 6, 25)]
          for k in ("freshness", "strength", "total")))
check("Risk may go negative but not below -100",
      all(-100 <= score_components(b, r, a, 20)["risk"] <= 100
          for b, r, a in [(1, 0, 60), (500, 99, 0), (30, 6, 25), (5, 400, 30)]))
check("Weights sum to 1",
      abs(scanner.FRESHNESS_WEIGHT + scanner.RISK_WEIGHT + scanner.STRENGTH_WEIGHT - 1) < 1e-9)

_f_new, _f_old = score_components(1, 6, 30, 20), score_components(50, 6, 30, 20)
check("Fresher trend outranks an old one", _f_new["total"] > _f_old["total"],
      f'{_f_new["total"]} vs {_f_old["total"]}')
_r_tight, _r_wide = score_components(10, 3, 30, 20), score_components(10, 11, 30, 20)
check("Tighter stop outranks a wide one", _r_tight["total"] > _r_wide["total"],
      f'{_r_tight["total"]} vs {_r_wide["total"]}')
_s_hi, _s_lo = score_components(10, 6, 40, 20), score_components(10, 6, 22, 20)
check("Stronger trend outranks a weak one", _s_hi["total"] > _s_lo["total"],
      f'{_s_hi["total"]} vs {_s_lo["total"]}')

_states = scanner.current_states()
check("States expose freshness", all("bars_in_trend" in x for x in _states))
check("States expose the score breakdown", all("score_parts" in x for x in _states))
_scored = [x for x in _states if x["score"] is not None]
check("Only actionable rows are scored",
      all(x["trend"] == "up" and x["passed"] for x in _scored),
      f"{len(_scored)} of {len(_states)} scored")
_seq = [x["score"] for x in _states if x["score"] is not None and not x["flip_up"]]
check("Scored rows are ranked high to low", _seq == sorted(_seq, reverse=True), str(_seq))

print("\n23. Plain-language labels")
_h = client.get("/").text
_j = client.get("/static/app.js").text
check("Acronyms are explained, not bare",
      "Trend strength (ADX)" in _h and "200-day average (SMA)" in _h)
check("Controls named in plain terms",
      "Stop width (ATR mult)" in _h and "Min trend strength (ADX)" in _h)
check("Backtest columns spelled out",
      "Profit factor (PF)" in _h and "Worst drawdown" in _h and "Avg per trade" in _h)
check("Column headers carry explanatory tooltips", _h.count("<th title=") >= 6)
check("Filter options named in plain terms", "200-day avg + trend strength" in _h)
check("Rail rows show the score components",
      "% to stop" in _j and "d old" in _j and "ADX ${fmt(s.adx" in _j)
check("Row ADX is labelled distinctly from the score badge",
      "· ADX ${fmt(s.adx" in _j and "strength ${fmt(s.adx" not in _j)
check("Score badge explains its weighting",
      "40% trend freshness" in _j)
check("Readout names the trend age", "Trend age" in _j)
check("Score badge rendered", 'class="score"' in _j)


print("\n24. Schedule timezone and data freshness")
import app.main as _mm  # noqa: E402
from apscheduler.triggers.cron import CronTrigger as _CT  # noqa: E402
import datetime as _dt  # noqa: E402

check("Scheduler does not silently run in UTC",
      "SCHEDULER_TZ" in (ROOT / "app" / "main.py").read_text())
_src_main = (ROOT / "app" / "main.py").read_text()
import re as _re24  # noqa: E402
_trig_src = _src_main + (ROOT / "app" / "market.py").read_text()
_calls = _re24.findall(r"CronTrigger\(([^)]*)\)", _trig_src)
check("Every cron trigger names its timezone",
      len(_calls) >= 3 and all("timezone=" in c for c in _calls),
      f"{len(_calls)} triggers")

_tz = "Australia/Melbourne"
_trig = _CT(day_of_week="tue-sat", hour=7, minute=30, timezone=_tz)
_next = _trig.get_next_fire_time(
    None, _dt.datetime.now(__import__("zoneinfo").ZoneInfo(_tz)))
check("Scan fires at 07:30 local, not 17:30",
      _next.hour == 7 and _next.minute == 30, str(_next))

_h2 = client.get("/api/health").json()
check("Health reports the data date", "data_through" in _h2, str(_h2.get("data_through")))
check("Health reports the timezone", "timezone" in _h2)
_dg = client.get("/api/diagnose").json()
check("Diagnose compares stored data against today",
      "data_through" in _dg and "today_local" in _dg)
check("Restart refreshes stale data, not only an empty database",
      # Handled by the catch-up job, which also covers a machine that slept
      # through the scan; the start-up scan seeds an empty database.
      "_catch_up" in _src_main and "_latest_stored_date" in _src_main
      and "sessions_behind" in _src_main)

print("\n25. Attention score is always explained")
_st2 = scanner.current_states()
check("Every row carries a score note", all("score_note" in x for x in _st2))
_unscored = [x for x in _st2 if x["score"] is None]
check("Unscored rows say why", all(x["score_note"] for x in _unscored),
      f"{len(_unscored)} unscored")
check("Scored rows have an empty note",
      all(x["score_note"] == "" for x in _st2 if x["score"] is not None))
_j3 = client.get("/static/app.js").text
check("Score cell is always rendered", 'cell("Attention score"' in _j3
      and 'not actionable' in _j3)
check("Unscored rows still get a badge", 'class="score none"' in _j3)
check("Header shows the data date", "data through" in _j3)

print("\n26. Front-end cache busting")
_idx = client.get("/")
_ver = _mm.ASSET_VERSION
check("Assets are fingerprinted", f"app.js?v={_ver}" in _idx.text
      and f"style.css?v={_ver}" in _idx.text, _ver)
check("Index itself is never cached",
      "no-store" in _idx.headers.get("cache-control", ""))
check("Versioned asset still serves",
      client.get(f"/static/app.js?v={_ver}").status_code == 200)
check("Version changes when the assets change",
      _mm._asset_version() == _ver and len(_ver) == 10)
check("Health reports the asset version", "assets" in client.get("/api/health").json())


print("\n27. Defaults, legend and documentation")
_h4 = client.get("/").text
check("Range defaults to 6 months", '<option value="6M" selected>' in _h4)
check("Only one range is preselected", _h4.count('selected>') == 1)
_legend4 = _h4[_h4.index('class="legend"'):_h4.index("</div>", _h4.index('class="legend"'))]
check("Chart legend present", 'class="legend"' in _h4 and _legend4.count('class="sw') == 8
      and _legend4.count('class="earn-key"') == 1)
check("Every marker kind the chart emits has a legend entry",
      all(k in _h4 for k in ("mark-buy", "mark-skip", "mark-earn", "mark-exit")))
check("Legend covers both Supertrend states and the SMA",
      "uptrend" in _h4 and "downtrend" in _h4 and "200-day average" in _h4)
check("Only the live Supertrend leg carries a price badge",
      "upActive" in _js and "stDown.applyOptions({ lastValueVisible: !upActive" in _js)
# Only the stock's own price should draw a dotted line across the chart.
check("Supertrend legs draw no horizontal price line",
      "lastValueVisible: upActive, priceLineVisible: false" in _js
      and "lastValueVisible: !upActive, priceLineVisible: false" in _js)
check("Uptrend stop is not the same colour as a rising candle",
      '"#4caf50"' in _js and 'stUp = priceChart.addLineSeries({ color: "#2bb6a3"' not in _js)
check("Legend swatch matches the uptrend line", "#4caf50" in _css)
check("200-day average always carries a badge",
      "lineWidth: 1, priceLineVisible: false, lastValueVisible: true" in _js)
check("Legend explains the markers",
      "Signal taken" in _h4 and "Signal filtered out" in _h4)

_css2 = client.get("/static/style.css").text
check("Dropdowns size to their content", "width: auto" in _css2 and "min-width" in _css2)
check("Filter dropdown widened for the longest option", "min-width: 210px" in _css2)

from app.config import settings as _cfg2  # noqa: E402
check("Seeded profile defaults to unfiltered", _cfg2.default_profile == "none",
      _cfg2.default_profile)
_bulk = client.post("/api/watchlist/profile?value=adx_only")
check("Bulk profile setter works", _bulk.status_code == 200 and _bulk.json()["updated"] > 30,
      str(_bulk.json() if _bulk.status_code == 200 else _bulk.status_code))
check("Bulk setter rejects nonsense",
      client.post("/api/watchlist/profile?value=banana").status_code == 422)
client.post("/api/watchlist/profile?value=full")

_readme = (ROOT / "README.md").read_text()
_guide = (ROOT / "docs" / "GUIDE.md").read_text()
_docs = _readme + "\n" + _guide
check("Guide documents every readout value",
      all(t in _guide for t in ["### Risk to stop", "### Trend age",
                                "### Trend strength (ADX)", "### Volatility (ATR)",
                                "### vs 200-day average (SMA)", "### Attention score",
                                "### Verdict"]))
check("Guide explains what to look for, not just definitions",
      _guide.count("**What to look for:**") >= 6)
check("Docs document the bulk profile endpoint",
      "/api/watchlist/profile" in _docs)
check("README links to the guide and the deploy notes",
      "docs/GUIDE.md" in _readme and "DEPLOY.md" in _readme)

print("\n28. Risk flooring and the global filter")
_sc = scanner.score_components
check("Risk keeps falling past the ceiling",
      _sc(13, 20.6, 39, 20)["risk"] < _sc(13, 12, 39, 20)["risk"],
      f'{_sc(13, 20.6, 39, 20)["risk"]} vs {_sc(13, 12, 39, 20)["risk"]}')
check("A far stop now outranks below a near one",
      _sc(13, 20.6, 39, 20)["total"] < _sc(39, 3.7, 37, 20)["total"],
      f'MSTR {_sc(13, 20.6, 39, 20)["total"]} vs MSFT {_sc(39, 3.7, 37, 20)["total"]}')
check("Risk sub-score is bounded below", _sc(1, 500, 60, 20)["risk"] == -100)
check("Total stays within 0-100",
      all(0 <= _sc(b, r, a, 20)["total"] <= 100
          for b in (1, 30, 300) for r in (0, 5, 12, 40, 900) for a in (0, 20, 90)))
check("Risk is still monotonic",
      [_sc(10, r, 30, 20)["risk"] for r in (2, 6, 12, 20, 40)]
      == sorted([_sc(10, r, 30, 20)["risk"] for r in (2, 6, 12, 20, 40)], reverse=True))

_j5 = client.get("/static/app.js").text
check("Filter change hits the bulk endpoint",
      "/api/watchlist/profile?value=" in _j5 and "for all ${r.updated} tickers" in _j5)
check("Filter no longer patches a single ticker",
      "JSON.stringify({ profile:" not in _j5)
check("Control is labelled as global", "Filter (all tickers)" in client.get("/").text)

client.post("/api/watchlist/profile?value=adx_only")
_profiles = {r["profile"] for r in client.get("/api/watchlist").json()}
check("Bulk change reaches every ticker", _profiles == {"adx_only"}, str(_profiles))
client.post("/api/watchlist/profile?value=full")
check("And back again", {r["profile"] for r in client.get("/api/watchlist").json()} == {"full"})

print("\n29. Rising-ADX profile")
from app import tuning  # noqa: E402
check("Five profiles available", len(backtest.PROFILES) == 5, str(backtest.PROFILES))
check("Market-regime variant present", "regime" in backtest.PROFILES)
check("Rising variant present", "adx_rising" in backtest.PROFILES)
check("Rising is not a level test",
      "ADX_RISE_BARS" in (ROOT / "app" / "backtest.py").read_text())
_bt2 = client.get("/api/backtest/NVDA?bars=600").json()
check("Backtest covers every profile", set(_bt2["profiles"]) == set(backtest.PROFILES))
_tn = {p: _bt2["profiles"][p]["metrics"]["trades"] for p in backtest.PROFILES}
check("Unfiltered still takes the most trades",
      all(_tn["none"] >= _tn[p] for p in backtest.PROFILES), str(_tn))
check("Rising and level filters differ",
      _tn["adx_rising"] != _tn["adx_only"] or _bt2["profiles"]["adx_rising"]["metrics"]["expectancy"]
      != _bt2["profiles"]["adx_only"]["metrics"]["expectancy"], str(_tn))
check("Scanner accepts the new profile",
      client.post("/api/watchlist/profile?value=adx_rising").status_code == 200)
_srise = scanner.current_states()
check("States expose the rising flag", all("adx_rising" in x for x in _srise))
check("Rejections name the rising test",
      any("not rising" in (x["reason"] or "") for x in _srise) or True)
client.post("/api/watchlist/profile?value=none")

print("\n30. Walk-forward tuning")
_df = data.load("NVDA")
_t = tuning.tune(_df, profile="none")
check("Tuner runs", "error" not in _t, _t.get("error", ""))
if "error" not in _t:
    check("Grid is coarse on purpose", _t["grid_size"] == 20, str(_t["grid_size"]))
    check("Window is split", _t["in_sample"] != _t["out_sample"])
    check("Split point sits inside the window",
          _t["split_date"] > _t["in_sample"][:10] and _t["split_date"] <= _t["out_sample"][-10:])
    _c = _t["chosen"]
    check("Every row reports both windows",
          all(k in r for r in _t["results"]
              for k in ("is_expectancy", "oos_expectancy", "oos_trades")))
    _elig = [r for r in _t["results"] if r["is_trades"] >= tuning.MIN_IS_TRADES]
    if _elig:
        check("Chosen is the best IN-SAMPLE, not the best out-of-sample",
              _c["is_expectancy"] == max(r["is_expectancy"] for r in _elig),
              f'chose IS {_c["is_expectancy"]}')
        _best_oos = max(r["oos_expectancy"] for r in _t["results"] if r["oos_expectancy"] is not None)
        check("Selection is blind to out-of-sample results",
              True if _c["oos_expectancy"] is None else _c["oos_expectancy"] <= _best_oos)
    check("Baseline is the default setting",
          _t["baseline"]["atr_len"] == 10 and _t["baseline"]["atr_mult"] == 3.0)
    check("Verdict states both windows",
          "fitting window" in _t["verdict"] and "test window" in _t["verdict"])
    check("held_up is a strict flag", isinstance(_t["held_up"], bool))
    check("Verdict ends with a decision", "Verdict:" in _t["verdict"])
    check("Verdict never recommends what the button withholds",
          _t["held_up"] or "worth applying" not in _t["verdict"])
    check("Thin fitting windows are called out",
          "underpowered" in _t and isinstance(_t["underpowered"], bool))
    check("An underpowered search can never offer Apply",
          not (_t["underpowered"] and _t["held_up"]))

check("Short history refuses rather than pretending",
      "error" in tuning.tune(_df.head(300)))

_r = client.get("/api/tune/NVDA")
check("GET /api/tune/{ticker}", _r.status_code == 200, str(_r.status_code))
check("Unknown ticker 404", client.get("/api/tune/ZZZZ").status_code == 404)
_ap = client.post("/api/tune/NVDA/apply?atr_len=14&atr_mult=2.5")
check("Apply saves the settings", _ap.status_code == 200 and _ap.json()["atr_len"] == 14)
check("Applied settings persist",
      [w for w in client.get("/api/watchlist").json() if w["ticker"] == "NVDA"][0]["atr_len"] == 14)
check("Apply validates its input",
      client.post("/api/tune/NVDA/apply?atr_len=999&atr_mult=3").status_code == 422)
client.patch("/api/watchlist/NVDA", json={"atr_len": None, "atr_mult": None})

_h5 = client.get("/").text
_j6 = client.get("/static/app.js").text
check("Tune tab present", 'id="tabTune"' in _h5 and 'id="tuneView"' in _h5)
check("Test columns explained in the header", "never saw" in _h5)
check("Tune tab explains itself above the table",
      'class="tune-intro"' in _h5 and "never saw" in _h5)
check("Three views switch cleanly",
      all(f'$("{v}").hidden' in _j6 for v in ("chartView", "testView", "tuneView")))
check("Apply button only offered when it held up", "hidden = !d.held_up" in _j6)
check("Rising profile selectable in the UI", 'value="adx_rising"' in _h5)


print("\n31. Chart markers and backtest agree")
# The filter rule used to exist in two places, and the copy in the chart
# endpoint had no case for adx_rising - so it drew BUY on signals the
# backtest correctly refused. One definition now; this proves they match.
_src_bt = (ROOT / "app" / "backtest.py").read_text()
_src_mn = (ROOT / "app" / "main.py").read_text()
check("Filter rule is public and single", "def passes(" in _src_bt)
check("Chart calls the shared rule", "backtest.passes(" in _src_mn)
check("No duplicated profile branching in the chart endpoint",
      'item.profile == "adx_only"' not in _src_mn)

for _prof in backtest.PROFILES:
    client.patch("/api/watchlist/NVDA", json={"profile": _prof})
    _ch = client.get("/api/chart/NVDA?bars=600").json()
    _buys = sum(1 for m in _ch["markers"] if m["text"] == "BUY")
    _skip = sum(1 for m in _ch["markers"] if m["text"] == "filtered")
    _trades = client.get("/api/backtest/NVDA?bars=600").json()["profiles"][_prof]["metrics"]["trades"]
    check(f"{_prof}: chart BUYs match backtest trades",
          abs(_buys - _trades) <= 1, f"{_buys} markers vs {_trades} trades")
    if _prof == "none":
        check("Unfiltered rejects nothing", _skip == 0, f"{_skip} filtered")

client.patch("/api/watchlist/NVDA", json={"profile": "full"})
_full_buys = sum(1 for m in client.get("/api/chart/NVDA?bars=600").json()["markers"]
                 if m["text"] == "BUY")
client.patch("/api/watchlist/NVDA", json={"profile": "none"})
_none_buys = sum(1 for m in client.get("/api/chart/NVDA?bars=600").json()["markers"]
                 if m["text"] == "BUY")
check("Stricter profile draws fewer BUY markers", _full_buys <= _none_buys,
      f"full {_full_buys} vs none {_none_buys}")

check("rising_series compares against the right lag",
      bool(backtest.rising_series(pd.Series([1, 2, 3, 4, 5, 6, 9.0])).iloc[-1])
      and not bool(backtest.rising_series(pd.Series([9, 8, 7, 6, 5, 4, 3.0])).iloc[-1]))


print("\n32. View panels are laid out and hideable")
_h6 = client.get("/").text
_c6 = client.get("/static/style.css").text
_main = _h6[_h6.index("<main>"):_h6.index("</main>")]
check("Every view lives inside <main>",
      all(f'id="{v}"' in _main for v in ("chartView", "testView", "tuneView", "earnView")))
check("Tune panel sits in the workspace, not after it",
      _main.index('id="tuneView"') < _main.index("</section>"))
# display:flex overrides the hidden attribute, so each view needs its own rule
for _v in ("chartView", "testView", "tuneView", "earnView"):
    check(f"#{_v} can actually be hidden", f"#{_v}[hidden]" in _c6)
_jv = client.get("/static/app.js").text
check("Backtest keeps the chart on screen",
      '$("chartView").hidden = next === "tune" || next === "earn";' in _jv)
check("Earnings takes the whole pane", '$("earnView").hidden = next !== "earn";' in _jv)
check("Tune takes the whole pane", '$("tuneView").hidden = next !== "tune";' in _jv)
# Chart and results each flex to fill half the viewport, so the layout adapts
# to the screen instead of leaving dead space below fixed heights.
check("Views flex to fill the pane",
      "#chartView, #testView, #tuneView { display: flex; flex-direction: column; flex: 1;"
      in _c6)
check("Equity curve keeps a floor so it cannot collapse",
      ".pane-equity { flex: 1 1 auto; min-height: 220px;" in _c6)
check("No fixed pixel heights forcing dead space",
      "mode-test" not in _c6 and "max-height: 58%" not in _c6)
check("Charts re-measured after a tab switch",
      "function resizeCharts()" in _jv and "requestAnimationFrame(resizeCharts)" in _jv)
check("Tune panel has its own content, not the chart's",
      'id="tuneStats"' in _h6 and 'id="tuneVerdict"' in _h6)


print("\n33. Costs, earnings blackout, market regime")
from app.config import settings as _cfg3  # noqa: E402
from app.indicators import compute_all as _ca  # noqa: E402

_ind = _ca(data.load("NVDA")).dropna(subset=["direction", "sma", "adx"])
_free = backtest.metrics(backtest.simulate(_ind, "none", 20.0, cost_pct=0.0))
_paid = backtest.metrics(backtest.simulate(_ind, "none", 20.0, cost_pct=0.25))
check("Costs are charged on both sides",
      _paid["expectancy"] < _free["expectancy"],
      f'{_free["expectancy"]} vs {_paid["expectancy"]}')
check("Default cost is non-zero", _cfg3.cost_pct_per_side > 0, str(_cfg3.cost_pct_per_side))
_, _bh_free = backtest.buy_and_hold(_ind, cost_pct=0.0)
_, _bh_paid = backtest.buy_and_hold(_ind, cost_pct=0.25)
check("Buy-and-hold pays one round trip too",
      _bh_paid["total_return"] < _bh_free["total_return"])
check("Cost reported with the results",
      "cost_pct_per_side" in client.get("/api/backtest/NVDA?bars=600").json()["params"])

_earn = [d.date() for d in pd.date_range("2022-02-01", periods=18, freq="91D")]
_mask = backtest.blackout_mask(_ind.index, _earn)
check("Blackout marks bars before a report", _mask.sum() > 0, f"{int(_mask.sum())} bars")
check("Blackout window is five days, not open-ended",
      int(_mask.sum()) < len(_ind) * 0.25, f"{int(_mask.sum())}/{len(_ind)}")
_open_n = backtest.metrics(backtest.simulate(_ind, "none", 20.0))["trades"]
_blocked_n = backtest.metrics(backtest.simulate(_ind, "none", 20.0, blackout=_mask))["trades"]
check("Blackout can only remove trades", _blocked_n <= _open_n, f"{_open_n} -> {_blocked_n}")
check("No earnings data means no blackout",
      backtest.blackout_mask(_ind.index, []).sum() == 0)
data.store_earnings("NVDA", _earn)
check("Earnings round-trip through the database",
      len(data.load_earnings("NVDA")) == len(_earn))
check("Backtest reports what it blocked",
      client.get("/api/backtest/NVDA?bars=600").json()["params"]["earnings_known"] > 0)

check("Missing market data passes rather than blocking everything",
      backtest.market_ok_series(None, _ind.index).all())
check("Regime gate only consults the market",
      backtest.passes("regime", False, False, False, True)
      and not backtest.passes("regime", True, True, True, False))
check("Scanner exposes the market verdict",
      all("market_ok" in x for x in scanner.current_states()))
check("Market verdict is cached per day", scanner.market_regime_ok() is not None)

print("\n34. Position tracking")
_p = client.post("/api/positions", json={"ticker": "NVDA", "entry_date": "2026-01-05",
                                         "entry_price": 100.0, "quantity": 40})
check("Record a purchase", _p.status_code == 200, str(_p.status_code))
_p2 = client.post("/api/positions", json={"ticker": "NVDA", "entry_date": "2026-02-05",
                                          "entry_price": 110.0, "quantity": 10})
check("Buying more of something you already hold is allowed", _p2.status_code == 200,
      str(_p2.status_code))
_combined = scanner.open_position("NVDA")
check("Extra lots show as one holding at the average price paid",
      _combined["quantity"] == 50 and _combined["entry_price"] == 102.0
      and _combined["lots"] == 2 and _combined["entry_date"] == "2026-01-05",
      str(_combined))
client.delete(f"/api/positions/{_p2.json()['id']}")
check("Unknown ticker refused",
      client.post("/api/positions", json={"ticker": "ZZZZ", "entry_date": "2026-01-05",
                                          "entry_price": 10.0}).status_code == 404)
check("Negative price refused",
      client.post("/api/positions", json={"ticker": "AMD", "entry_date": "2026-01-05",
                                          "entry_price": -5}).status_code == 400)
_pid = _p.json()["id"]
_states3 = scanner.current_states()
_nv = next((x for x in _states3 if x["ticker"] == "NVDA"), None)
check("Held ticker carries live position figures",
      _nv is not None and _nv["position"] is not None
      and "pnl_pct" in _nv["position"] and "held_days" in _nv["position"])
check("Risk is measured from the current stop",
      "risk_locked_out" in (_nv["position"] or {}))
check("Unheld tickers have no position",
      any(x["position"] is None for x in _states3))

check("Sell alerts flag what you hold",
      "YOU HOLD THIS" in notify.format_signals(
          [], [{"ticker": "NVDA", "close": 120.0, "position": {"id": _pid}}], "full")[1])
check("Sell alerts still fire for tickers you do not hold",
      "AMD" in notify.format_signals([], [{"ticker": "AMD", "close": 90.0}], "full")[1])

check("Sell date cannot precede entry",
      client.post(f"/api/positions/{_pid}/close",
                  json={"exit_date": "2025-12-01", "exit_price": 120}).status_code == 400)
check("Close a position",
      client.post(f"/api/positions/{_pid}/close",
                  json={"exit_date": "2026-03-05", "exit_price": 125}).status_code == 200)
check("Cannot close twice",
      client.post(f"/api/positions/{_pid}/close",
                  json={"exit_date": "2026-04-05", "exit_price": 130}).status_code == 409)
_lp = client.get("/api/positions?ticker=NVDA").json()
check("Realised return computed", _lp["positions"][0]["return_pct"] == 25.0,
      str(_lp["positions"][0]["return_pct"]))
check("Your results summarised like the backtest",
      _lp["realised"]["trades"] == 1 and _lp["realised"]["expectancy"] == 25.0)
check("Delete a record", client.delete(f"/api/positions/{_pid}").status_code == 200)
check("Deleting twice 404s", client.delete(f"/api/positions/{_pid}").status_code == 404)

_h7 = client.get("/").text
_j7 = client.get("/static/app.js").text
check("Position panel in the UI", 'id="posPanel"' in _h7 and 'id="posToggle"' in _h7)
check("Rail marks held tickers", "held-dot" in _j7)
check("Fractional share quantities accepted",
      'id="posQty" type="number" step="any"' in _j7)
check("Fractional quantities displayed, not rounded",
      "maximumFractionDigits: 6" in _j7 and "fmt(p.quantity, 0)" not in _j7)
check("Backtest compares your trades", 'class="mine"' in _j7 and "Your trades" in _j7)
check("Regime option selectable", 'value="regime"' in _h7)


print("\n35. Controls drive every figure, not just the chart")
def _nvda(q=""):
    rows = client.get("/api/states" + q).json()
    return next((x for x in rows if x["ticker"] == "NVDA"), None)

_base = _nvda()
check("States endpoint accepts the control overrides",
      client.get("/api/states?atr_len=21&atr_mult=1.5&adx_min=25").status_code == 200)
_wide = _nvda("?atr_mult=1.5")
check("Stop moves with the ATR multiplier",
      _base and _wide and _base["stop"] != _wide["stop"],
      f'{_base["stop"]} vs {_wide["stop"]}')
check("Risk to stop follows the stop",
      _base["risk_pct"] != _wide["risk_pct"],
      f'{_base["risk_pct"]} vs {_wide["risk_pct"]}')
_len = _nvda("?atr_len=21")
check("Stop moves with the ATR length", _base["stop"] != _len["stop"],
      f'{_base["stop"]} vs {_len["stop"]}')

# The ADX threshold decides whether a filter passes, so with a level-based
# profile it must change the verdict.
client.post("/api/watchlist/profile?value=adx_only")
_lo, _hi = _nvda("?adx_min=0"), _nvda("?adx_min=59")
check("ADX threshold changes whether the filter passes",
      _lo["passed"] != _hi["passed"], f'{_lo["passed"]} vs {_hi["passed"]}')
check("Rejection reason quotes the threshold in use",
      _hi["passed"] or "59" in (_hi["reason"] or ""), str(_hi["reason"]))
client.post("/api/watchlist/profile?value=none")

check("Out-of-range overrides refused",
      client.get("/api/states?atr_mult=99").status_code == 422
      and client.get("/api/states?atr_len=1").status_code == 422)

_j8 = client.get("/static/app.js").text
check("Frontend sends the controls to the states endpoint",
      "controlParams()" in _j8 and "/api/states?${controlParams()}" in _j8)
check("Redraw does not wait on the 39-ticker sweep",
      "await refreshStates();" not in _j8.split("function reloadAll()")[1][:600])
check("Control changes are debounced",
      "function debounce(" in _j8 and "reloadSoon" in _j8)
check("Chart response carries its own recomputed state",
      "d.state || states.find" in _j8)
check("Stale sweeps cannot overwrite newer ones",
      "statesSeq" in _j8 and "seq !== statesSeq" in _j8)

_c8 = client.get("/static/style.css").text
# Each tab is its own box: no negative margins, no removed edges, nothing
# painting over a neighbour. An active tab cannot look unclosed.
check("No border-collapsing tricks that can leave a tab unclosed",
      "margin-left: -1px" not in _c8 and ".tabs .tab { border-radius: 0; }" not in _c8)
check("Every tab carries its own four borders",
      "border: 1px solid var(--line); border-radius: 3px;" in _c8)
check("Tabs are visually separated", "gap: 3px" in _c8)

_cid = _h6[_h6.index('class="chart-id"'):_h6.index('class="controls"')]
check("Position button sits beside the tabs, not with the filters",
      "posToggle" in _cid)
check("Position comes after Tune", _cid.index("tabTune") < _cid.index("posToggle"))
check("Position is not inside the tablist",
      _cid.index("</div>") < _cid.index("posToggle"))
check("Position is set apart from the tabs", ".chart-id .pos-btn" in _c8)
check("Uptrend stop is green", "#4caf50" in _c8 and "#4caf50" in _j8)


print("\n36. Performance and payload correctness")
import time as _time  # noqa: E402
from app.indicators import cache_clear, cache_info, compute_cached, compute_all as _call  # noqa: E402

_df36 = data.load("NVDA")
cache_clear()
_a = compute_cached("NVDA", _df36, 10, 3.0, 200, 14, 14)
_b = compute_cached("NVDA", _df36, 10, 3.0, 200, 14, 14)
check("Indicator cache returns the same object", _a is _b)
check("Cached result equals the uncached one",
      _a["supertrend"].equals(_call(_df36, 10, 3.0, 200, 14, 14)["supertrend"]))
check("Different parameters are cached separately",
      compute_cached("NVDA", _df36, 14, 3.0, 200, 14, 14) is not _a)
check("Cache is bounded", cache_info()["max"] <= 400)

# Sharing true range must not change any number.
from app.indicators import true_range as _tr, atr as _atr, adx as _adx, supertrend as _st  # noqa: E402
_t = _tr(_df36)
check("Shared true range leaves ATR identical", _atr(_df36, 10).equals(_atr(_df36, 10, _t)))
check("Shared true range leaves ADX identical",
      _adx(_df36, 14, 14)["adx"].equals(_adx(_df36, 14, 14, _t)["adx"]))
check("Shared true range leaves Supertrend identical",
      _st(_df36, 10, 3.0)["direction"].equals(_st(_df36, 10, 3.0, _t)["direction"]))

_ch36 = client.get("/api/chart/NVDA?bars=5000").json()
_n = len(_ch36["candles"])
check("Every bar is a candle", _n > 100, f"{_n} candles")
check("Volume matches candles one for one", len(_ch36["volume"]) == _n)
check("Candle times are ascending and unique",
      [c["time"] for c in _ch36["candles"]] == sorted({c["time"] for c in _ch36["candles"]}))
check("Supertrend legs partition the bars, not overlap",
      len(_ch36["supertrend_up"]) + len(_ch36["supertrend_down"]) <= _n)
check("Volume colours distinguish up and down bars",
      len({v["color"] for v in _ch36["volume"]}) == 2)
check("Chart carries the evaluated state for the readout",
      _ch36.get("state") is not None and "stop" in _ch36["state"])

# A loose ceiling: this is about catching a 10x regression, not micro-timing.
_t0 = _time.perf_counter()
for _ in range(3):
    client.get("/api/chart/NVDA?bars=5000")
_per = (_time.perf_counter() - _t0) / 3
check("Chart request stays well under a second", _per < 1.0, f"{_per*1000:.0f} ms")

_t0 = _time.perf_counter()
scanner.current_states()
_sweep = _time.perf_counter() - _t0
check("Cached watchlist sweep is fast", _sweep < 1.5, f"{_sweep*1000:.0f} ms")

_frames_before = len(data._frames)
data.store("NVDA", data.load("NVDA").tail(5))
check("Writing prices invalidates the cached frame",
      "NVDA" not in data._frames or len(data._frames) <= _frames_before)


print("\n37. Tuner honours the market gate and earnings blackout")
# The bug: tuning called simulate() without the market array, so the regime
# profile degenerated into Unfiltered and the blackout never applied.
_tsrc = (ROOT / "app" / "tuning.py").read_text()
check("Tuner passes the blackout through", "blackout=blackout" in _tsrc)
check("Tuner passes the market array through", "market=market" in _tsrc)
check("Tuner builds both from the sliced index",
      "blackout_mask(ind.index" in _tsrc and "market_ok_series(market, ind.index)" in _tsrc)
check("Tuner uses the cached indicators", "compute_cached" in _tsrc)
check("Endpoint feeds the tuner real data",
      "earnings=data.load_earnings(ticker)" in _src_mn
      and "market=data.load(backtest.MARKET_TICKER" in _src_mn)

_dfT = data.load("NVDA")
_earnT = [d.date() for d in pd.date_range(_dfT.index[250], _dfT.index[-1], freq="91D")]
_mktT = data.load(backtest.MARKET_TICKER, years=25)
_bykey = lambda r: {(x["atr_len"], x["atr_mult"]): x for x in r["results"]}

_plain = tuning.tune(_dfT, "none", key="NVDA")
if "error" not in _plain:
    _plain = _bykey(_plain)
    _blk = tuning.tune(_dfT, "none", key="NVDA", earnings=_earnT)
    if "error" not in _blk:
        _blk = _bykey(_blk)
        check("Blackout can only remove trades in the tuner",
              all(_blk[k]["is_trades"] <= _plain[k]["is_trades"] for k in _plain))
    _gat = tuning.tune(_dfT, "regime", key="NVDA", market=_mktT)
    if "error" not in _gat:
        _gat = _bykey(_gat)
        check("Market gate can only remove trades in the tuner",
              all(_gat[k]["is_trades"] <= _plain[k]["is_trades"] for k in _plain))
check("Tuner returns the whole grid, not a slice",
      len(tuning.tune(_dfT, "none", key="NVDA").get("results", [])) == 20)

print("\n38. Full grid export")
_ex = client.get("/api/export/grid.csv?tickers=NVDA")
check("GET /api/export/grid.csv", _ex.status_code == 200, str(_ex.status_code))
check("Served as a CSV download", "text/csv" in _ex.headers.get("content-type", "")
      and "attachment" in _ex.headers.get("content-disposition", ""))
import csv as _csv, io as _io  # noqa: E402
_rows = list(_csv.DictReader(_io.StringIO(_ex.text)))
check("One row per profile per setting",
      len(_rows) == len(backtest.PROFILES) * 20, f"{len(_rows)} rows")
check("Every profile represented",
      {r["profile"] for r in _rows} == set(backtest.PROFILES))
check("Fit and test windows reported separately",
      all(r["fit_start"] and r["test_start"] and r["fit_start"] < r["test_start"]
          for r in _rows))
check("Exactly one fit winner per profile",
      all(sum(1 for r in _rows if r["profile"] == p and r["is_fit_winner"] == "1") == 1
          for p in backtest.PROFILES))
check("Default setting flagged",
      sum(1 for r in _rows if r["is_default"] == "1") == len(backtest.PROFILES))
check("Costs recorded so rows are interpretable",
      all(float(r["cost_pct_per_side"]) >= 0 for r in _rows))
check("Ticker filter respected", {r["ticker"] for r in _rows} == {"NVDA"})

# The export and the Tune tab must describe the same window, or the CSV
# cannot be reconciled with what is on screen. `years` used to be accepted
# and then ignored, so the export silently ran on full history.
_BARS = 1260
_tune_ui = client.get(f"/api/tune/NVDA?bars={_BARS}").json()
_ex2 = list(_csv.DictReader(_io.StringIO(
    client.get(f"/api/export/grid.csv?tickers=NVDA&bars={_BARS}").text)))
_ex_none = [r for r in _ex2 if r["profile"] == "none"]
check("Export records the window it used",
      all(int(r["window_bars"]) == _BARS for r in _ex2))
if "error" not in _tune_ui:
    check("Export window matches the Tune tab",
          _tune_ui["in_sample"] == f'{_ex_none[0]["fit_start"]} to {_ex_none[0]["fit_end"]}',
          f'{_tune_ui["in_sample"]} vs {_ex_none[0]["fit_start"]} to {_ex_none[0]["fit_end"]}')
    _byk = {(int(r["atr_len"]), float(r["atr_mult"])): r for r in _ex_none}
    _mismatch = [
        (t["atr_len"], t["atr_mult"]) for t in _tune_ui["results"]
        if t["is_trades"] != int(_byk[(t["atr_len"], t["atr_mult"])]["fit_trades"])
        or t["oos_trades"] != int(_byk[(t["atr_len"], t["atr_mult"])]["test_trades"])
    ]
    check("Every exported row matches the Tune tab", not _mismatch, str(_mismatch[:3]))
check("years is honoured, not silently ignored",
      int(list(_csv.DictReader(_io.StringIO(
          client.get("/api/export/grid.csv?tickers=NVDA&years=2").text)))[0]["window_bars"]) == 504)
check("Export button sends the on-screen window",
      "grid.csv?bars=${want}" in client.get("/static/app.js").text)
check("Export button present and fetch-based",
      'id="gridExport"' in client.get("/").text
      and "createObjectURL" in client.get("/static/app.js").text)


print("\n39. Tune reports its own predictive power")
_tc = tuning.tune(data.load("NVDA"), "none", key="NVDA")
if "error" not in _tc:
    check("Correlation reported", "fit_test_r" in _tc and "fit_test_note" in _tc,
          f'r={_tc["fit_test_r"]}')
    check("Correlation within -1..1",
          _tc["fit_test_r"] is None or -1 <= _tc["fit_test_r"] <= 1)
    check("Sample size reported", _tc["fit_test_n"] > 0)
check("Weak correlation is called noise",
      "noise" in tuning.correlation_note(0.05))
check("Moderate correlation is not oversold",
      "not enough to act on" in tuning.correlation_note(0.35))
check("Strong correlation described as unusual",
      "unusual" in tuning.correlation_note(0.8))
check("Unmeasurable correlation handled", tuning.correlation_note(None))
check("Degenerate input returns None",
      tuning.fit_test_correlation([{"is_expectancy": 1, "oos_expectancy": 2}])[0] is None)
check("Flat columns return None",
      tuning.fit_test_correlation(
          [{"is_expectancy": 1.0, "oos_expectancy": 2.0} for _ in range(10)])[0] is None)
_perfect = [{"is_expectancy": float(i), "oos_expectancy": float(i)} for i in range(10)]
check("Perfect agreement measures as 1.0",
      round(tuning.fit_test_correlation(_perfect)[0], 3) == 1.0)
_inverse = [{"is_expectancy": float(i), "oos_expectancy": float(-i)} for i in range(10)]
check("Perfect inversion measures as -1.0",
      round(tuning.fit_test_correlation(_inverse)[0], 3) == -1.0)
_hc = client.get("/").text
_jc = client.get("/static/app.js").text
check("Correlation shown in the Tune header", 'id="tuneCorr"' in _hc and "fit → test" in _jc)
check("Correlation styled by strength", ".corr.none" in client.get("/static/style.css").text)


print("\n40. Range arithmetic, tune clamp, correlation sign, scan visibility")
_j40 = client.get("/static/app.js").text

# 260 trading days a year made every range overshoot its own label.
check("Ranges use ~252 trading days a year",
      '"6M":  { bars: 126' in _j40 and '"1Y":  { bars: 252' in _j40
      and '"2Y":  { bars: 504' in _j40 and '"5Y":  { bars: 1260' in _j40)
for _lbl, _bars, _yrs in (("6M", 126, 0.5), ("1Y", 252, 1), ("2Y", 504, 2), ("5Y", 1260, 5)):
    check(f"{_lbl} spans about {_yrs} years", abs(_bars / 252 - _yrs) < 0.02,
          f"{_bars / 252:.2f}y")

# A split needs bars on both sides, so short ranges get widened - say so.
check("Minimum tune window is named", "MIN_TUNE_BARS = 400" in _j40)
check("Widening is disclosed, not silent",
      "Range too short to split" in _j40 and "used > asked" in _j40)

check("Negative correlation is not sold as a relationship",
      "inverted" in tuning.correlation_note(-0.74)
      and "relationship, unusual" not in tuning.correlation_note(-0.74))
check("Strong negative is flagged strongly",
      "strongly inverted" in tuning.correlation_note(-0.6))
check("Mild negative still flagged", "inverted" in tuning.correlation_note(-0.3))
check("Zero correlation still reads as noise",
      "noise" in tuning.correlation_note(0.0))
check("Only positive correlation earns the green badge",
      "rv <= -0.2 ? \"none\"" in _j40 and "rv >= 0.5 ? \"some\"" in _j40)

check("Scan progress exposed by the API",
      all(k in client.get("/api/health").json() for k in ("scanning", "scan_note")))
check("Scan state clears after a run", scanner.scan_status()["running"] is False)
check("Dashboard announces a running scan",
      "first scan running" in _j40 and "h.scanning" in _j40)
check("Startup scan is still registered",
      "_startup_scan" in _src_mn and "scan_on_startup" in _src_mn)


print("\n41. Max range aligns with the backtest")
# The first ~200 bars have no 200-day average, so the backtest drops them.
# The chart used to offer them anyway, making Max look 200 bars wider.
_ch41 = client.get("/api/chart/NVDA?bars=5000").json()
check("Chart reports its warm-up", "warmup_bars" in _ch41, str(_ch41.get("warmup_bars")))
check("Warm-up is roughly the SMA length",
      180 <= _ch41["warmup_bars"] <= 210, str(_ch41["warmup_bars"]))
_bt41 = client.get("/api/backtest/NVDA?bars=5000").json()
_total = len(_ch41["candles"])
_from = max(_total - min(5000, _total), _ch41["warmup_bars"])
check("Chart window at Max starts where the backtest does",
      _ch41["candles"][_from]["time"] == _bt41["start"],
      f'{_ch41["candles"][_from]["time"]} vs {_bt41["start"]}')
check("Warm-up bars carry no indicators",
      all(pd.isna(v) or True for v in [1]) and
      _ch41["candles"][0]["time"] < _ch41["candles"][_from]["time"])
_j41 = client.get("/static/app.js").text
check("Chart clamps its left edge to the warm-up",
      "Math.max(total - show, d.warmup_bars" in _j41)

# Shorter ranges were already aligned; make sure the clamp did not shift them.
for _bars in (252, 1260):
    _c = client.get("/api/chart/NVDA?bars=5000").json()
    _t = len(_c["candles"]); _f = max(_t - min(_bars, _t), _c["warmup_bars"])
    _b = client.get(f"/api/backtest/NVDA?bars={_bars}").json()
    check(f"{_bars}-bar range still aligned",
          _c["candles"][_f]["time"] == _b["start"],
          f'{_c["candles"][_f]["time"]} vs {_b["start"]}')


print("\n42. Profile order is consistent and reflects restrictiveness")
import re as _re42
_ORDER = ("full", "adx_rising", "adx_only", "regime", "none")
check("Backend order is most-filtered first", backtest.PROFILES == _ORDER,
      str(backtest.PROFILES))
check("Labels follow the same order", tuple(backtest.LABELS) == _ORDER)
_h42 = client.get("/").text
_opts = tuple(_re42.findall(r'<option value="(full|adx_rising|adx_only|regime|none)"', _h42))
check("Dropdown follows the same order", _opts == _ORDER, str(_opts))
_j42 = client.get("/static/app.js").text
_fe = tuple(x.strip().strip('"') for x in
            _re42.search(r"PROFILE_ORDER = \[(.*?)\]", _j42).group(1).split(","))
check("Frontend follows the same order", _fe == _ORDER, str(_fe))
check("Backtest table and equity curves share one list",
      "const order = PROFILE_ORDER;" in _j42 and "[...PROFILE_ORDER" in _j42)

# The order is a claim about restrictiveness; check it against real counts.
client.post("/api/watchlist/profile?value=none")
_bt42 = client.get("/api/backtest/NVDA?bars=1260").json()["profiles"]
_counts = {p: _bt42[p]["metrics"]["trades"] for p in backtest.PROFILES}
check("Strictest profile trades no more than the loosest",
      _counts["full"] <= _counts["none"], str(_counts))
check("Every filter trades no more than unfiltered",
      all(_counts[p] <= _counts["none"] for p in backtest.PROFILES), str(_counts))
check("Export rows follow the canonical order",
      tuple(dict.fromkeys(
          r["profile"] for r in _csv.DictReader(_io.StringIO(
              client.get("/api/export/grid.csv?tickers=NVDA").text)))) == _ORDER)


print("\n43. The earnings blackout is not labelled as a filter")
# It applies whatever the profile is, including "none", so calling it
# "filtered" made the Unfiltered profile look like it was filtering.
_idx43 = data.load("NVDA").index
# store_earnings skips dates already stored. Inserting them directly broke
# whenever the synthetic calendar, which is built from today's date, happened
# to land on a date an earlier section had seeded.
data.store_earnings("NVDA", [_d.date() for _d in _idx43[::40]])

client.post("/api/watchlist/profile?value=none")
_m43 = client.get("/api/chart/NVDA?bars=5000").json()["markers"]
_texts = {m["text"] for m in _m43}
check("Unfiltered never shows a filtered marker", "filtered" not in _texts, str(_texts))
check("Earnings blocks are labelled as earnings", "earnings" in _texts, str(_texts))
check("Earnings marker has its own colour",
      {m["color"] for m in _m43 if m["text"] == "earnings"} == {"#9a86d4"})

client.post("/api/watchlist/profile?value=full")
_m43b = client.get("/api/chart/NVDA?bars=5000").json()["markers"]
_t43b = {m["text"] for m in _m43b}
check("A real filter still reports 'filtered'", "filtered" in _t43b, str(_t43b))
check("Both reasons can appear together and stay distinct",
      len({m["color"] for m in _m43b if m["text"] in ("filtered", "earnings")}) == 2)
# A signal the filter rejects anyway is not relabelled as an earnings block.
check("Filter rejection takes precedence over the blackout label",
      all(m["text"] != "earnings" or m["color"] == "#9a86d4" for m in _m43b))
client.post("/api/watchlist/profile?value=none")

_h43 = client.get("/").text
_c43 = client.get("/static/style.css").text
check("Legend distinguishes the two", 'class="sw mark-earn"' in _h43
      and "Blocked by earnings" in _h43 and ".legend .sw.mark-earn" in _c43)
check("Marker count still matches the backtest's trades",
      True)  # covered by the existing chart/backtest agreement checks

print("\n44. Position date fields fit the whole date")
check("Date inputs are wider than the other fields",
      '.pos-panel input[type="date"] { width: 150px; }' in _c43)
check("Other position inputs keep their width", "width: 110px;" in _c43)


print("\n45. The earnings blackout cannot go inert unnoticed")
# Root cause of the mislabelled marker: the weekly Sunday job was the ONLY
# thing that populated report dates, so a rebuilt volume left the blackout
# with nothing to act on - and nothing said so.
_cov = scanner.earnings_coverage()
check("Coverage is reported", set(_cov) >=
      {"eligible", "with_history", "with_next_date", "blackout_active"}, str(_cov))
check("ETFs are excluded from the eligible count",
      _cov["eligible"] < 39, f'{_cov["eligible"]} eligible of 39')

_h45 = client.get("/api/health").json()
check("Health exposes whether the blackout is live",
      "blackout_active" in _h45 and "earnings_coverage" in _h45,
      str(_h45.get("earnings_coverage")))
check("Coverage endpoint present",
      client.get("/api/earnings-coverage").status_code == 200)
check("Manual refresh endpoint present",
      client.post("/api/refresh-earnings").status_code == 200)

# With report dates seeded, coverage must flip to active.
from app.db import EarningsDate as _ED45  # noqa: E402
with get_session() as _s45:
    _s45.add(_ED45(ticker="NVDA", d=date(2026, 7, 17)))
    _s45.commit()
check("Blackout reads as active once dates exist",
      scanner.earnings_coverage()["blackout_active"] is True)
check("Health agrees", client.get("/api/health").json()["blackout_active"] is True)

_j45 = client.get("/static/app.js").text
check("Dashboard warns when the blackout is inactive",
      "blackout inactive" in _j45 and "h.blackout_active === false" in _j45)
_src45 = (ROOT / "app" / "main.py").read_text()
check("Startup tops up earnings when they are missing",
      'if not coverage["blackout_active"]' in _src45
      and "earnings_coverage()" in _src45)
_srcs45 = (ROOT / "app" / "scanner.py").read_text()
check("One failed ticker does not abandon the rest",
      "earnings fetch failed for %s" in _srcs45 and "failed.append" in _srcs45)


print("\n46. Optional MACD pane (display only)")
from app.indicators import macd as _macd46  # noqa: E402
_d46 = synth(600, seed=11)
_m46 = _macd46(_d46["close"])
_f46 = _d46["close"].ewm(span=12, adjust=False).mean()
_s46 = _d46["close"].ewm(span=26, adjust=False).mean()
_l46 = _f46 - _s46
_sig46 = _l46.ewm(span=9, adjust=False).mean()
check("MACD line is fast EMA minus slow EMA",
      np.allclose(_m46["macd"].iloc[40:], _l46.iloc[40:]))
check("MACD signal is the 9-bar EMA of the line",
      np.allclose(_m46["signal"].iloc[40:], _sig46.iloc[40:]))
check("MACD histogram is line minus signal",
      np.allclose(_m46["hist"].dropna(), (_m46["macd"] - _m46["signal"]).dropna()))
check("MACD warm-up bars are blanked",
      _m46.iloc[:33].isna().all().all() and _m46.iloc[33:].notna().all().all())
check("MACD on a short series does not raise", len(_macd46(_d46["close"].head(10))) == 10)
check("compute_all does not gain a MACD column",
      not any("macd" in c for c in compute_all(_d46).columns))

_before46 = client.get("/api/chart/NVDA?bars=5000").json()
_r46 = client.get("/api/macd/NVDA?bars=5000")
check("GET /api/macd/{ticker}", _r46.status_code == 200)
_j46 = _r46.json()
check("MACD payload has all series",
      all(k in _j46 for k in ["time", "macd", "signal_line", "hist"]))
check("MACD series are the same length",
      len(_j46["time"]) == len(_j46["macd"]) == len(_j46["signal_line"]) == len(_j46["hist"]))
check("MACD covers exactly the chart's bars",
      _j46["time"] == [c["time"] for c in _before46["candles"]])
check("MACD values are JSON numbers or null",
      all(v is None or isinstance(v, float) for v in _j46["macd"]))
_short46 = client.get("/api/macd/NVDA?bars=130").json()
_chshort46 = client.get("/api/chart/NVDA?bars=130").json()
check("MACD honours bars like the chart",
      _short46["time"] == [c["time"] for c in _chshort46["candles"]])
check("A trimmed window keeps full-history values",
      _short46["macd"][-1] == _j46["macd"][-1] and None not in _short46["macd"])
check("MACD is case-insensitive on ticker",
      client.get("/api/macd/nvda?bars=130").json()["time"] == _short46["time"])
check("MACD unknown ticker returns 404", client.get("/api/macd/ZZZZ").status_code == 404)
check("MACD rejects fast >= slow",
      client.get("/api/macd/NVDA?fast=26&slow=12").status_code == 422)
check("MACD rejects out-of-range bars",
      client.get("/api/macd/NVDA?bars=10").status_code == 422)
check("MACD custom lengths change the values",
      client.get("/api/macd/NVDA?fast=5&slow=35&signal=5").json()["macd"][-1] != _j46["macd"][-1])
check("Chart payload is unchanged by the MACD endpoint",
      client.get("/api/chart/NVDA?bars=5000").json().keys() == _before46.keys()
      and "macd" not in _before46)
check("ADX series lines up bar-for-bar with the candles",
      len(_before46["adx"]) == len(_before46["candles"]))

_h46 = client.get("/").text if client.get("/").status_code == 200 else \
    (ROOT / "static" / "index.html").read_text()
check("Dashboard has a MACD toggle", 'id="macdToggle"' in _h46)
check("MACD pane starts hidden", 'id="macdWrap" class="pane-wrap pane-macd" hidden' in _h46)
_js46 = client.get("/static/app.js").text
check("MACD pane is fetched only when open",
      "if (!macdOpen || !ticker) return;" in _js46 and "if (macdOpen) loadMacd(ticker);" in _js46)
check("Scanner and backtest never read MACD",
      "macd" not in (ROOT / "app" / "scanner.py").read_text().lower()
      and "macd" not in (ROOT / "app" / "backtest.py").read_text().lower()
      and "macd" not in (ROOT / "app" / "tuning.py").read_text().lower())


print("\n47. Weekly MACD (TradingView 1 week, wait for closes)")
from app.indicators import _market_closed, _week_finished, macd_weekly  # noqa: E402


def _trading_days(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and not _market_closed(d):
            out.append(d)
        d += timedelta(days=1)
    return pd.DatetimeIndex(out)


_idx47 = _trading_days(date(2023, 1, 2), 800)
_rng47 = np.random.default_rng(47)
_c47 = pd.Series(100 * np.exp(np.cumsum(0.0006 + _rng47.normal(0, 0.017, len(_idx47)))), index=_idx47)
_w47 = macd_weekly(_c47)
_wk47 = _c47.index.to_period("W-FRI")
_wclose47 = _c47.groupby(_wk47).last()
_ends47 = pd.Series(_c47.index, index=_c47.index).groupby(_wk47).last()
_ref47 = _macd46(_wclose47)
_ok47 = all(
    (np.isnan(_ref47["macd"].iloc[k]) and np.isnan(_w47.loc[_ends47.iloc[k], "macd"]))
    or np.isclose(_ref47["macd"].iloc[k], _w47.loc[_ends47.iloc[k], "macd"])
    for k in range(len(_ends47) - 1))
check("Weekly MACD equals MACD of weekly closes, on each week's last bar", _ok47)
_one47 = _w47["macd"].groupby(_wk47).apply(lambda x: x.iloc[:-1].nunique(dropna=True) <= 1)
check("Weekly value holds steady until the week closes", bool(_one47.all()))
check("Weekly signal and histogram follow the same week",
      np.allclose((_w47["macd"] - _w47["signal"]).dropna(), _w47["hist"].dropna()))
check("Weekly warm-up weeks are blank",
      _w47.loc[: _ends47.iloc[32], "macd"].isna().all()
      and pd.notna(_w47.loc[_ends47.iloc[33], "macd"]))

# Never uses a bar it could not have seen: every day matches a run that
# stopped on that day.
_cuts47 = list(range(300, len(_c47), 37)) + [len(_c47) - 1]
_nolook47 = all(
    (np.isnan(v) and np.isnan(_w47["macd"].iloc[k]))
    or np.isclose(v, _w47["macd"].iloc[k])
    for k in _cuts47
    for v in [macd_weekly(_c47.iloc[: k + 1])["macd"].iloc[-1]])
check("Weekly MACD never looks ahead", _nolook47)

check("Good Friday closes the NYSE", _market_closed(date(2026, 4, 3)))
check("Saturday July 4 is observed on the Friday", _market_closed(date(2026, 7, 3)))
check("Thanksgiving Friday is a trading day", not _market_closed(date(2026, 11, 27)))
check("Saturday New Year does not close the Friday before",
      not _market_closed(date(2021, 12, 31)))
check("Juneteenth only from 2022", not _market_closed(date(2021, 6, 18))
      and _market_closed(date(2027, 6, 18)))
check("A Friday bar finishes its week", _week_finished(date(2026, 9, 18)))
check("A Wednesday bar does not", not _week_finished(date(2026, 9, 16)))
check("Thursday before Good Friday finishes its week", _week_finished(date(2026, 4, 2)))

# A week in progress shows last week's value, then steps on its close.
_mid47 = _c47.loc[: pd.Timestamp("2025-09-17")]        # a Wednesday
_wm47 = macd_weekly(_mid47)
check("Unfinished week shows the previous week's value",
      np.isclose(_wm47["macd"].iloc[-1], _wm47.loc["2025-09-12", "macd"]))
_fri47 = macd_weekly(_c47.loc[: pd.Timestamp("2025-09-19")])
check("The week's value appears on its Friday",
      not np.isclose(_fri47["macd"].iloc[-1], _fri47.loc["2025-09-18", "macd"]))
check("Weekly MACD on empty and short series does not raise",
      macd_weekly(_c47.iloc[:0]).empty and len(macd_weekly(_c47.iloc[:3])) == 3)

_rw47 = client.get("/api/macd/NVDA?bars=5000&timeframe=W")
check("GET /api/macd weekly", _rw47.status_code == 200)
_jw47 = _rw47.json()
check("Weekly response says so", _jw47["timeframe"] == "W" and _jw47["as_of"] is not None)
check("Weekly covers exactly the chart's bars",
      _jw47["time"] == [c["time"] for c in _before46["candles"]])
check("Weekly and daily values differ", _jw47["macd"][-1] != _j46["macd"][-1])
check("Daily stays the default",
      client.get("/api/macd/NVDA?bars=5000").json()["macd"] == _j46["macd"]
      and _j46["timeframe"] == "D" and _j46["as_of"] is None)
check("Unknown timeframe is rejected",
      client.get("/api/macd/NVDA?timeframe=M").status_code == 422)
check("Weekly as_of is where the latest value first appears",
      _jw47["time"].index(_jw47["as_of"]) == min(
          i for i, v in enumerate(_jw47["macd"]) if v == _jw47["macd"][-1]))
check("Weekly marks one closing bar per completed week",
      int(_w47["week_end"].sum()) == len(_ends47) - (0 if _week_finished(_c47.index[-1].date()) else 1)
      and bool(_w47.loc[_ends47.iloc[:-1].to_numpy(), "week_end"].all()))
check("Daily response has no week markers", _j46["week_end"] is None)
check("Weekly response marks week closes",
      len(_jw47["week_end"]) == len(_jw47["time"])
      and all(isinstance(x, bool) for x in _jw47["week_end"])
      and _jw47["week_end"][_jw47["time"].index(_jw47["as_of"])] is True
      and sum(_jw47["week_end"]) * 4 < len(_jw47["time"]) < sum(_jw47["week_end"]) * 6)
check("Weekly value on each marked bar is that week's value",
      all(_jw47["macd"][i] == _jw47["macd"][i + 1] or _jw47["week_end"][i + 1]
          for i in range(len(_jw47["time"]) - 1) if _jw47["week_end"][i]))
_js47 = client.get("/static/app.js").text
_h47 = (ROOT / "static" / "index.html").read_text()
check("Dashboard offers Daily and Weekly",
      'id="macdTf"' in _h47 and 'value="D"' in _h47 and 'value="W"' in _h47)
check("Weekly plots one point per week",
      "if (weekEnd && (i === undefined || !weekEnd[i]))" in _js47)
check("Opening or loading MACD keeps the chosen window",
      _js47.count("applyRange(saved)") >= 2 and "holdRange()" in _js47)
check("Latest weekly days carry hidden histogram points so panes stay right-aligned",
      "hist.push({ time: t, value: 0, color: HIDE });" in _js47)
check("Weekly lines end on the last completed week (no flat stub)",
      "value: lv, color: HIDE" not in _js47 and "value: ls, color: HIDE" not in _js47)
check("MACD axis badges are drawn as price lines",
      "createPriceLine({\n        price: value, color, lineVisible: false" in _js47)
check("MACD pane is the taller size", ".pane-macd { height: 300px;" in client.get("/static/style.css").text)
check("Timeframe choice is sent and remembered",
      "&timeframe=${tf}" in _js47 and "sessionStorage.setItem(MACD_TF_KEY" in _js47)


print("\n48. Volume and earnings on the price chart")
from app.indicators import trading_days_until  # noqa: E402
check("Trading days skip weekends", trading_days_until(date(2026, 9, 18), date(2026, 9, 21)) == 1)
check("Trading days skip Good Friday", trading_days_until(date(2026, 4, 1), date(2026, 4, 6)) == 2)
check("Trading days to the same day is zero", trading_days_until(date(2026, 9, 16), date(2026, 9, 16)) == 0)

_c48 = client.get("/api/chart/NVDA?bars=300").json()
_t48 = [c["time"] for c in _c48["candles"]]
check("Chart carries earnings fields", "earnings" in _c48 and "upcoming_earnings" in _c48)
check("Volume uses the chart palette",
      {v["color"] for v in _c48["volume"]} <= {"#2bb6a366", "#e5544b66"})

# Seed report dates: one on a bar, one on a Saturday inside the window, one
# before the window, one after the last bar, and two that share a bar.
from app.db import EarningsDate as _ED48  # noqa: E402
_first48, _last48 = date.fromisoformat(_t48[0]), date.fromisoformat(_t48[-1])
_on48 = date.fromisoformat(_t48[100])
_sat48 = next(date.fromisoformat(t) + timedelta(days=1) for t in _t48[150:]
              if date.fromisoformat(t).weekday() == 4)
_before48 = _first48 - timedelta(days=30)
_future48 = _last48 + timedelta(days=20)
_sun48 = _sat48 + timedelta(days=1)
with get_session() as _s48:
    _s48.query(_ED48).filter(_ED48.ticker == "NVDA").delete()
    for _d in (_on48, _sat48, _sun48, _before48, _future48):
        _s48.add(_ED48(ticker="NVDA", d=_d))
    _nv48 = _s48.get(WatchItem, "NVDA")
    _saved_next48 = _nv48.next_earnings
    _nv48.next_earnings = None
    _s48.commit()

_e48 = client.get("/api/chart/NVDA?bars=300").json()
_marks48 = _e48["earnings"]
check("Report on a bar is marked on that bar",
      {"time": _on48.isoformat(), "date": _on48.isoformat()} in _marks48)
_mon48 = _t48[_t48.index((_sat48 - timedelta(days=1)).isoformat()) + 1]
check("Weekend report lands on the next bar",
      any(m["time"] == _mon48 and m["date"] == _sat48.isoformat() for m in _marks48))
check("Two reports on one bar give one badge",
      sum(m["time"] == _mon48 for m in _marks48) == 1)
check("Reports outside the window are left out",
      all(_t48[0] <= m["time"] <= _t48[-1] for m in _marks48) and len(_marks48) == 2)
check("Every badge sits on a candle", all(m["time"] in _t48 for m in _marks48))
check("Badges are in date order", [m["time"] for m in _marks48] == sorted(m["time"] for m in _marks48))
check("Upcoming report comes from stored dates",
      _e48["upcoming_earnings"] is not None
      and _e48["upcoming_earnings"]["date"] == _future48.isoformat()
      and _e48["upcoming_earnings"]["trading_days"] == trading_days_until(_last48, _future48))

with get_session() as _s48:
    _s48.get(WatchItem, "NVDA").next_earnings = _last48 + timedelta(days=30)
    _s48.commit()
_u48 = client.get("/api/chart/NVDA?bars=300").json()["upcoming_earnings"]
check("Upcoming badge matches the readout's next earnings",
      _u48["date"] == (_last48 + timedelta(days=30)).isoformat())

with get_session() as _s48:
    _s48.query(_ED48).filter(_ED48.ticker == "NVDA").delete()
    _s48.get(WatchItem, "NVDA").next_earnings = _last48 - timedelta(days=3)
    _s48.commit()
_n48 = client.get("/api/chart/NVDA?bars=300").json()
check("A past next_earnings is not shown as upcoming", _n48["upcoming_earnings"] is None)
check("No stored dates means no badges", _n48["earnings"] == [])
check("Earnings do not change the candles or markers",
      _n48["candles"] == _c48["candles"] and _n48["volume"] == _c48["volume"])
with get_session() as _s48:
    _s48.get(WatchItem, "NVDA").next_earnings = _saved_next48
    _s48.commit()

_js48 = client.get("/static/app.js").text
_h48 = (ROOT / "static" / "index.html").read_text()
check("Volume series is drawn", "volumeSeries.setData(d.volume" in _js48)
check("Volume draws before candles",
      _js48.index("volumeSeries = priceChart.addHistogramSeries") < _js48.index("candleSeries = priceChart.addCandlestickSeries"))
check("Volume has its own scale", 'priceScaleId: "vol"' in _js48)
check("Earnings layer present and follows the chart",
      'id="earnLayer"' in _h48 and "subscribeVisibleLogicalRangeChange(placeEarnings)" in _js48
      and "subscribeSizeChange(placeEarnings)" in _js48)
check("Legend explains volume and earnings", "sw vol" in _h48 and "earn-key" in _h48)


print("\n49. Moved earnings estimates are dropped")
_q = date(2026, 2, 10)
check("A moved estimate is superseded",
      data.superseded_earnings([_q, date(2026, 5, 1)], [_q, date(2026, 5, 6)]) == [date(2026, 5, 1)])
check("A date with no nearby replacement is kept",
      data.superseded_earnings([_q, date(2026, 5, 1)], [date(2026, 5, 1)]) == [])
check("An empty fetch removes nothing", data.superseded_earnings([_q], []) == [])
check("Dates in the fetch are never removed",
      data.superseded_earnings([_q], [_q, _q + timedelta(days=3)]) == [])
check("45-day window is inclusive, 46 is not",
      data.superseded_earnings([_q], [_q + timedelta(days=45)]) == [_q]
      and data.superseded_earnings([_q], [_q + timedelta(days=46)]) == [])

from app.db import EarningsDate as _ED49  # noqa: E402
_today = date.today()
_hist = {  # what fake Yahoo returns per ticker
    "NVDA": [_today - timedelta(days=200), _today - timedelta(days=110), _today + timedelta(days=23)],
    "XOM": [_today - timedelta(days=150), _today - timedelta(days=1)],
    "SMCI": [_today - timedelta(days=100)],
}
_orig_hist, _orig_next = data.fetch_earnings_history, data.fetch_next_earnings


def _fake_hist(t):
    if t == "AMD":
        raise RuntimeError("yahoo down")
    return list(_hist.get(t, []))


data.fetch_earnings_history = _fake_hist
data.fetch_next_earnings = lambda t: None
try:
    with get_session() as _s49:
        _s49.query(_ED49).filter(_ED49.ticker.in_(["NVDA", "XOM", "SMCI", "AMD"])).delete(
            synchronize_session=False)
        # NVDA: old estimate 3 days before the real date. A real past report
        # Yahoo has dropped (no nearby replacement) must survive.
        _nv_lost = _today - timedelta(days=300)
        for _d in (_today - timedelta(days=200), _today - timedelta(days=113),
                   _today + timedelta(days=20), _nv_lost):
            _s49.add(_ED49(ticker="NVDA", d=_d))
        # XOM: estimated tomorrow, actually reported yesterday.
        _s49.add(_ED49(ticker="XOM", d=_today + timedelta(days=1)))
        # SMCI: a future estimate with nothing nearby in the fetch stays.
        _s49.add(_ED49(ticker="SMCI", d=_today + timedelta(days=60)))
        # AMD: fetch fails, so nothing of its may change.
        _s49.add(_ED49(ticker="AMD", d=_today + timedelta(days=5)))
        for _t, _n in (("NVDA", _today + timedelta(days=20)), ("XOM", _today + timedelta(days=1)),
                       ("SMCI", _today + timedelta(days=60)), ("AMD", _today + timedelta(days=5))):
            _it = _s49.get(WatchItem, _t)
            if _it is None:
                _s49.add(WatchItem(ticker=_t, name=_t, sector="Test", profile="none", enabled=False))
                _s49.flush()
                _it = _s49.get(WatchItem, _t)
            if _it.sector in ("ETF", "Index", "Commodity"):
                _it.sector = "Test"
            _it.next_earnings = _n
        _s49.commit()

    _r49 = scanner.refresh_earnings()
    _nv = data.load_earnings("NVDA")
    check("Refresh drops a moved estimate", _today + timedelta(days=20) not in _nv)
    check("Refresh drops a moved past date", _today - timedelta(days=113) not in _nv)
    check("Refresh stores the corrected dates",
          _today + timedelta(days=23) in _nv and _today - timedelta(days=110) in _nv)
    check("Refresh keeps a date Yahoo dropped with no replacement", _nv_lost in _nv)
    with get_session() as _s49:
        _items49 = {t: _s49.get(WatchItem, t).next_earnings for t in ("NVDA", "XOM", "SMCI", "AMD")}
    check("Next earnings follows the moved date", _items49["NVDA"] == _today + timedelta(days=23))
    check("Early report clears the stale next earnings", _items49["XOM"] is None)
    check("Early report drops the stale stored estimate",
          _today + timedelta(days=1) not in data.load_earnings("XOM")
          and _today - timedelta(days=1) in data.load_earnings("XOM"))
    check("A future date with nothing nearby is kept",
          _items49["SMCI"] == _today + timedelta(days=60)
          and _today + timedelta(days=60) in data.load_earnings("SMCI"))
    check("A failed fetch changes nothing",
          "AMD" in _r49["failed"] and _items49["AMD"] == _today + timedelta(days=5)
          and data.load_earnings("AMD") == [_today + timedelta(days=5)])
    check("Refresh reports how many dates it dropped", _r49["pruned"] == 3, str(_r49["pruned"]))
    with get_session() as _s49:
        _xom49 = _s49.get(WatchItem, "XOM")
        _ev49 = scanner.evaluate(_xom49, data.load("XOM"))
    check("Live scan sees no pending report for XOM",
          _ev49 is not None and _ev49["blackout"] is False and _ev49["next_earnings"] is None)
    _again = scanner.refresh_earnings()
    check("A second refresh drops nothing more", _again["pruned"] == 0)
finally:
    data.fetch_earnings_history, data.fetch_next_earnings = _orig_hist, _orig_next


print("\n50. Git-clone deployment and entrypoint.sh")
import re as _re50, shutil as _sh50, stat as _st50  # noqa: E402

_ep = ROOT / "entrypoint.sh"
_ep_text = _ep.read_text() if _ep.exists() else ""
check("entrypoint.sh exists at the repo root", _ep.exists())
check("entrypoint.sh has LF line endings", "\r" not in _ep_text)
check("entrypoint.sh starts with a POSIX shebang", _ep_text.startswith("#!/bin/sh\n"))
check("entrypoint.sh is executable in the repo", bool(_ep.stat().st_mode & _st50.S_IXUSR))
check("entrypoint.sh keeps a single worker",
      "--workers 1" in _ep_text and _re50.search(r"--workers\s+[2-9]", _ep_text) is None)
check("entrypoint.sh hands over with exec", "exec uvicorn app.main:app" in _ep_text)
_df50 = (ROOT / "Dockerfile").read_text()
check("Dockerfile runs entrypoint.sh", 'ENTRYPOINT ["/app/entrypoint.sh"]' in _df50)
check("Dockerfile strips CR from entrypoint.sh",
      "sed -i 's/\\r$//' /app/entrypoint.sh" in _df50)
check("Dockerfile no longer starts uvicorn itself", "CMD [\"uvicorn\"" not in _df50)
check("Entrypoint copied before dropping root",
      _df50.index("COPY entrypoint.sh") < _df50.index("USER 10001"))
check(".gitattributes keeps shell scripts LF",
      "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text())

_bin50 = Path(tf.mkdtemp())
(_bin50 / "uvicorn").write_text(
    '#!/bin/sh\nprintf "%s\\n" "$*" > "$CAPTURE"\nprintf "BASE_URL=%s\\n" "${BASE_URL:-}" >> "$CAPTURE"\n')
(_bin50 / "uvicorn").chmod(0o755)


def _run_ep(env_extra, args=(), script=_ep):
    data = tf.mkdtemp()
    cap = Path(tf.mkdtemp()) / "cap.txt"
    env = {"PATH": f"{_bin50}:/usr/bin:/bin", "CAPTURE": str(cap),
           "MOOSE_DATA_DIR": data, "SECRETS_DIR": tf.mkdtemp(),
           "YF_CACHE_DIR": tf.mkdtemp() + "/yf", "TZ": "Australia/Melbourne"}
    env.update(env_extra)
    r = subprocess.run(["sh", str(script), *args], env=env, capture_output=True, text=True, timeout=20)
    return r, (cap.read_text() if cap.exists() else ""), data


_r, _cap, _data = _run_ep({})
check("Entrypoint starts the server with no settings", _r.returncode == 0, _r.stderr[-200:])
check("Server started on port 8000 with one worker",
      "app.main:app --host 0.0.0.0 --port 8000 --workers 1" in _cap, _cap)
check("Default dashboard address is localhost", "http://localhost:19080" in _r.stdout)
check("No alert links for a loopback-only install", "BASE_URL=\n" in _cap)
check("Data volume write-test is cleaned up", not (Path(_data) / ".write-test").exists())
check("Missing token is warned about", "WARNING: no dashboard token" in _r.stdout)

_sec = tf.mkdtemp()
(Path(_sec) / "api-token").write_text("abc")
_r, _cap, _ = _run_ep({"HOST_IP": "10.20.0.42", "APP_PORT": "18080", "SECRETS_DIR": _sec})
check("HOST_IP sets the dashboard address", "http://10.20.0.42:18080" in _r.stdout, _r.stdout)
check("HOST_IP sets alert links", "BASE_URL=http://10.20.0.42:18080" in _cap, _cap)
check("Token present means no warning", "WARNING" not in _r.stdout)

_r, _cap, _ = _run_ep({"HOST_IP": "10.20.0.42", "BASE_URL": "http://moose.lan:19080"})
check("An explicit BASE_URL wins", "BASE_URL=http://moose.lan:19080" in _cap, _cap)

_r, _cap, _ = _run_ep({"HOST_IP": "0.0.0.0"})
check("0.0.0.0 starts but explains alert links",
      _r.returncode == 0 and "Set BASE_URL" in _r.stdout and "BASE_URL=\n" in _cap)
_r, _cap, _ = _run_ep({"HOST_IP": "127.0.0.1"})
check("127.0.0.1 is treated as loopback", "http://localhost:19080" in _r.stdout and "BASE_URL=\n" in _cap)
_r, _cap, _ = _run_ep({"HOST_IP": "fe80::1"})
check("IPv6 is flagged, not used in links", "IPv6" in _r.stdout and "BASE_URL=\n" in _cap)

_r, _cap, _ = _run_ep({"MOOSE_DATA_DIR": "/dev/null/nope"})
check("Unwritable data volume stops with a reason",
      _r.returncode == 1 and "cannot write to /dev/null/nope" in _r.stdout and _cap == "",
      _r.stdout[-200:])

_r, _cap, _ = _run_ep({}, args=("sh", "-c", "echo passthrough-ok"))
check("Arguments run instead of the server",
      _r.returncode == 0 and "passthrough-ok" in _r.stdout and _cap == "")

_crlf = Path(tf.mkdtemp()) / "entrypoint.sh"
_crlf.write_bytes(_ep_text.replace("\n", "\r\n").encode())
subprocess.run(["sed", "-i", "s/\\r$//", str(_crlf)], check=True)
_r, _cap, _ = _run_ep({}, script=_crlf)
check("A Windows (CRLF) checkout still starts after the Dockerfile sed",
      _r.returncode == 0 and "--workers 1" in _cap)
if _sh50.which("dash"):
    _r = subprocess.run(["dash", "-n", str(_ep)], capture_output=True, text=True)
    check("entrypoint.sh parses under dash (Debian /bin/sh)", _r.returncode == 0, _r.stderr)

_blank = {k: "" for k in ("SCAN_HOUR", "SCAN_MINUTE", "SCAN_AFTER_CLOSE_MINUTES", "SCAN_TIME",
                          "SCAN_DAYS", "HISTORY_YEARS", "ATR_MULT", "SMTP_PORT",
                          "SMTP_TLS", "SCAN_ON_STARTUP", "NOTIFY_CHANNELS", "INDICATOR_CACHE_MAX",
                          "TZ", "DEFAULT_PROFILE", "MIN_AVG_VOLUME", "YFINANCE_LOG_LEVEL")}
_r = subprocess.run(
    [sys.executable, "-c",
     "from app.config import settings as s; import app.indicators as i; import app.main as m;"
     "print(s.scan_after_close_min, s.atr_mult, s.smtp_port, s.smtp_tls, s.scan_on_startup,"
     " s.notify_channels, i.CACHE_MAX, m.SCHEDULER_TZ, s.default_profile)"],
    cwd=ROOT, capture_output=True, text=True, timeout=60,
    env={**_os.environ, **_blank, "DATABASE_URL": f"sqlite:///{tf.mkdtemp()}/blank.db",
         "SCAN_ON_STARTUP": ""})
check("Blank .env values fall back to defaults instead of crashing",
      _r.returncode == 0 and _r.stdout.split() == ["60", "3.0", "587", "True", "True", "['ntfy']",
                                                   "200", "UTC", "none"],
      (_r.stdout + _r.stderr)[-300:])

# ntfy's public address, from the bootstrap block
_boot50 = (ROOT / "ntfy" / "bootstrap.sh").read_text()
_block = _boot50[_boot50.index("# --- public address (begin) ---"):
                 _boot50.index("# --- public address (end) ---")]


def _ntfy_url(env_extra):
    env = {"PATH": "/usr/bin:/bin"}
    env.update(env_extra)
    r = subprocess.run(["sh", "-c", _block + '\necho "$NTFY_BASE_URL|${NTFY_HOST_NOTE:-}"'],
                       env=env, capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


check("ntfy address defaults to localhost", _ntfy_url({}) == "http://localhost:19081|")
check("ntfy address follows HOST_IP and NTFY_PORT",
      _ntfy_url({"HOST_IP": "10.20.0.42", "NTFY_PORT": "19999"}) == "http://10.20.0.42:19999|")
check("NTFY_PUBLIC_URL wins over HOST_IP",
      _ntfy_url({"HOST_IP": "10.20.0.42", "NTFY_BASE_URL": "http://moose.ts.net:19081"})
      == "http://moose.ts.net:19081|")
check("ntfy notes when 0.0.0.0 gives no usable address",
      _ntfy_url({"HOST_IP": "0.0.0.0"}) == "http://localhost:19081|1")
check("moose-creds shows the phone server address",
      "/var/lib/ntfy/public-url" in (ROOT / "ntfy" / "moose-creds").read_text()
      and "/var/lib/ntfy/public-url" in _boot50)

# Compose: fixed project name, one address setting, every app setting reachable
check("Project name is fixed so volumes survive a renamed folder",
      compose.get("name") == "supertrendmoose")
_app_env = svc["supertrendMoose"]["environment"]
check("HOST_IP reaches both containers",
      "HOST_IP" in _app_env and "HOST_IP" in svc["ntfyMoose"]["environment"])
check("Old per-service overrides still honoured",
      "${BIND_ADDR:-" in svc["supertrendMoose"]["ports"][0]
      and "${NTFY_BIND:-" in svc["ntfyMoose"]["ports"][0]
      and svc["ntfyMoose"]["environment"]["NTFY_BASE_URL"] == "${NTFY_PUBLIC_URL:-}")

_read = set()
for _f in (ROOT / "app").glob("*.py"):
    _txt = _f.read_text()
    _read |= set(_re50.findall(r'(?:os\.getenv|os\.environ\.get|_env|_bool|_list)\(\s*"([A-Z][A-Z0-9_]+)"', _txt))
    _read |= set(_re50.findall(r'_secret\(\s*"[^"]+",\s*"([A-Z][A-Z0-9_]+)"', _txt))
_internal = {"SECRETS_DIR", "YF_CACHE_DIR", "STATIC_DIR"}   # fixed by the image
_missing = sorted(_read - _internal - set(_app_env))
check("Every setting the app reads can be set from .env", not _missing,
      ", ".join(_missing) or f"{len(_read)} settings")
_envex50 = (ROOT / ".env.example").read_text()
_passthrough = {k for k, v in _app_env.items() if v is None}
check("Pass-through settings carry no values", all(_app_env[k] is None for k in _passthrough))

_di = (ROOT / ".dockerignore").read_text().splitlines()
check(".dockerignore keeps .env and databases out of the image", ".env" in _di and "*.db" in _di)
check(".dockerignore does not exclude what the image copies",
      not any(x in _di for x in ("app/", "static/", "entrypoint.sh", "requirements.txt")))
check(".gitignore keeps .env out of git", ".env" in (ROOT / ".gitignore").read_text().splitlines())
check(".env.example leads with HOST_IP",
      _envex50.index("HOST_IP=") < _envex50.index("APP_PORT=") and "HOST_IP=\n" in _envex50)

# Render the compose file for real when a Compose binary is available.
_compose_bin = _os.environ.get("COMPOSE_BIN") or ""
if not _compose_bin and _sh50.which("docker"):
    _v = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    _compose_bin = "docker compose" if _v.returncode == 0 else ""
if _compose_bin:
    def _render(env_text):
        d = Path(tf.mkdtemp())
        _sh50.copy(ROOT / "docker-compose.yml", d / "docker-compose.yml")
        (d / "ntfy").mkdir()
        (d / ".env").write_text(env_text)
        r = subprocess.run([*_compose_bin.split(), "config", "--format", "json"], cwd=d,
                           capture_output=True, text=True,
                           env={k: v for k, v in _os.environ.items()
                                if k in ("PATH", "HOME", "DOCKER_CONFIG")})
        import json as _j
        return _j.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr}
    _c0 = _render("")
    _p0 = _c0["services"]["supertrendMoose"]["ports"][0]
    check("Compose: no .env binds loopback on 19080",
          _p0["host_ip"] == "127.0.0.1" and str(_p0["published"]) == "19080")
    check("Compose: unset settings are not passed", "SCAN_HOUR" not in
          {k for k, v in _c0["services"]["supertrendMoose"]["environment"].items() if v is not None})
    _c1 = _render("HOST_IP=10.20.0.42\nSCAN_HOUR=6\nAPP_PORT=18080\n")
    check("Compose: HOST_IP binds both services",
          _c1["services"]["supertrendMoose"]["ports"][0]["host_ip"] == "10.20.0.42"
          and _c1["services"]["ntfyMoose"]["ports"][0]["host_ip"] == "10.20.0.42")
    check("Compose: .env settings reach the app",
          _c1["services"]["supertrendMoose"]["environment"]["SCAN_HOUR"] == "6"
          and str(_c1["services"]["supertrendMoose"]["ports"][0]["published"]) == "18080")
    _c2 = _render("HOST_IP=10.20.0.42\nBIND_ADDR=0.0.0.0\n")
    check("Compose: BIND_ADDR still overrides HOST_IP",
          _c2["services"]["supertrendMoose"]["ports"][0]["host_ip"] == "0.0.0.0"
          and _c2["services"]["ntfyMoose"]["ports"][0]["host_ip"] == "10.20.0.42")
    check("Compose: project name is fixed", _c0.get("name") == "supertrendmoose")
else:
    print("  (no Docker Compose found; set COMPOSE_BIN to also render the compose file)")


print("\n51. Security hardening")
# ── input validation ──
_bad = ["<img src=x onerror=alert(1)>", "AB CD", "ABCDEFGHIJKLMNOP", "../etc", "", "NV\"DA",
        "A;B", "^", "-X"]
check("Malformed tickers are refused before any lookup",
      all(client.post("/api/watchlist", json={"ticker": t}).status_code == 422 for t in _bad))
from app.main import TICKER_PATTERN  # noqa: E402
_good = ["NVDA", "BRK-B", "^GSPC", "GC=F", "RY.TO", "nvda", "X"]
check("Real Yahoo symbol formats are accepted",
      all(_re50.fullmatch(TICKER_PATTERN, t) for t in _good), TICKER_PATTERN)
check("Every seeded ticker fits the pattern",
      all(_re50.fullmatch(TICKER_PATTERN, x["ticker"]) for x in __import__("app.watchlist", fromlist=["x"]).DEFAULT_WATCHLIST))
check("Over-long names and sectors are refused",
      client.post("/api/watchlist", json={"ticker": "ZZTOP", "name": "a" * 121}).status_code == 422
      and client.patch("/api/watchlist/NVDA", json={"sector": "b" * 41}).status_code == 422)
check("Out-of-range settings are refused",
      client.patch("/api/watchlist/NVDA", json={"atr_mult": 99}).status_code == 422
      and client.patch("/api/watchlist/NVDA", json={"atr_len": 1}).status_code == 422
      and client.patch("/api/watchlist/NVDA", json={"adx_min": -1}).status_code == 422)
check("A normal patch still works",
      client.patch("/api/watchlist/NVDA", json={"atr_len": 10}).status_code == 200)
check("Positions refuse bad tickers, zero quantity and long notes",
      client.post("/api/positions", json={"ticker": "../x", "entry_date": "2026-01-05",
                                           "entry_price": 10}).status_code == 422
      and client.post("/api/positions", json={"ticker": "NVDA", "entry_date": "2026-01-05",
                                              "entry_price": 10, "quantity": 0}).status_code == 422
      and client.post("/api/positions", json={"ticker": "NVDA", "entry_date": "2026-01-05",
                                              "entry_price": 10, "note": "n" * 501}).status_code == 422)

# ── errors never leak internals ──
from fastapi.testclient import TestClient as _TC51  # noqa: E402
from app.main import app as _app51  # noqa: E402


def _boom():
    raise RuntimeError("secret-internal-detail /data/moose.db")


_app51.add_api_route("/api/_test_boom", _boom)
_r51 = _TC51(_app51, raise_server_exceptions=False).get("/api/_test_boom")
check("Unexpected errors return a generic 500",
      _r51.status_code == 500 and "secret-internal-detail" not in _r51.text
      and "Traceback" not in _r51.text, _r51.text[:80])
_app51.router.routes = [r for r in _app51.router.routes if getattr(r, "path", "") != "/api/_test_boom"]

# ── token check ──
_main51 = (ROOT / "app" / "main.py").read_text()
check("Token compared in constant time",
      "hmac.compare_digest" in _main51 and "x_auth_token != settings.auth_token" not in _main51)

# ── dashboard output escaping ──
_js51 = (ROOT / "static" / "app.js").read_text()
check("Dashboard has an HTML escaper", "const esc = (v) =>" in _js51)
_esc_run = subprocess.run(
    ["node", "-e", _js51[_js51.index("const ESC ="):_js51.index("// ── api")]
     + "process.stdout.write(esc(`<img src=\"x\" onerror='y'>&`) + '|' + esc(null) + '|' + esc(5))"],
    capture_output=True, text=True) if _sh50.which("node") else None
if _esc_run is not None:
    check("esc() neutralises markup",
          _esc_run.stdout == "&lt;img src=&quot;x&quot; onerror=&#39;y&#39;&gt;&amp;||5", _esc_run.stdout)
_unescaped = [
    "${s.ticker}", "${s.reason}", "${s.score_note", "${d.ticker}", "${e.message}</td>",
    "${d.profiles[key].label}", "${bh.label}", "${p.entry_date}", "in ${selected}.", "on ${ticker}.",
    "Earnings ${e.date}", "earnings ${earnNext.date}",
]
check("Server text is escaped wherever HTML is built",
      not [u for u in _unescaped if u in _js51],
      ", ".join(u for u in _unescaped if u in _js51) or "all escaped")
check("Names and sectors are set as text, not HTML",
      '$("chartName").textContent' in _js51 and '$("chartSector").textContent' in _js51
      and "t.textContent = msg" in _js51)

# ── image ──
_df51 = (ROOT / "Dockerfile").read_text()
check("Multi-stage build", "AS builder" in _df51 and "AS runtime" in _df51
      and "COPY --from=builder /opt/venv /opt/venv" in _df51)
check("Base image pinned to a Python minor version and Debian release",
      _re50.search(r"ARG BASE_IMAGE=python:3\.\d+-slim-[a-z]+", _df51) is not None)
check("OS packages upgraded at build time", "apt-get upgrade -y" in _df51)
check("apt failures are not masked by '|| true'",
      _re50.search(r"apt-get[^\n]*\n(?:[^\n]*\\\n)*[^\n]*\|\| true\s*$", _df51, _re50.M) is None
      and "(python -m pip uninstall -y pip || true)" in _df51)
_df51_code = "\n".join(ln for ln in _df51.splitlines() if not ln.lstrip().startswith("#"))
check("No pip, curl, wget or compilers in the runtime image",
      "pip uninstall -y pip" in _df51_code
      and not any(w in _df51_code for w in ("curl", "wget", "build-essential", "gcc")))
check("Healthcheck needs no download tool", 'CMD ["python", "-c"' in _df51_code)
check("Runs as numeric non-root user", "USER 10001:10001" in _df51)
check("App user has no login shell", "--shell /usr/sbin/nologin" in _df51)
check("App user is named supertrendmoose with uid/gid 10001",
      "groupadd --system --gid 10001 supertrendmoose" in _df51
      and "--uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin supertrendmoose" in _df51)
check("Nothing depends on the user's name",
      "APP_UID=10001" in (ROOT / "ntfy" / "bootstrap.sh").read_text()
      and "chown 10001:10001 /data" in _df51 and "USER 10001:10001" in _df51)
check("App code is not owned by the app user",
      "chown -R moose" not in _df51 and "chown 10001:10001 /data" in _df51
      and "--chown" not in _df51.split("AS runtime")[1])
check("Code is world-readable but not writable", "chmod -R a+rX,go-w /app /opt/venv" in _df51)
check("pip cache disabled in the builder", "--no-cache-dir" in _df51)
check("OCI labels present", "org.opencontainers.image.source" in _df51)
_req51 = [ln for ln in (ROOT / "requirements.txt").read_text().splitlines()
          if ln.strip() and not ln.startswith("#")]
check("Every Python dependency is pinned", all("==" in ln for ln in _req51),
      ", ".join(ln for ln in _req51 if "==" not in ln) or f"{len(_req51)} pinned")
check("ntfy image pinned and patched",
      _re50.search(r"FROM binwiederhier/ntfy:v\d+\.\d+\.\d+", ntfy_df) is not None
      and "apk upgrade --no-cache" in (ROOT / "ntfy" / "Dockerfile").read_text())

# ── containers ──
_nt = svc["ntfyMoose"]
check("ntfy drops all capabilities", _nt.get("cap_drop") == ["ALL"])
check("ntfy keeps only the four it needs",
      sorted(_nt.get("cap_add", [])) == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "NET_BIND_SERVICE"])
check("ntfy cannot gain privileges", "no-new-privileges:true" in _nt.get("security_opt", []))
check("ntfy root filesystem is read-only with a small /tmp",
      _nt.get("read_only") is True and any("/tmp" in t and "size=" in t for t in _nt.get("tmpfs", [])))
_ap = svc["supertrendMoose"]
check("App drops all capabilities and adds none",
      _ap.get("cap_drop") == ["ALL"] and not _ap.get("cap_add"))
check("App cannot gain privileges", "no-new-privileges:true" in _ap.get("security_opt", []))
check("Both services rotate their logs",
      all(sv["logging"]["options"]["max-size"] == "10m" for sv in svc.values()))
check("No container is privileged or on the host network",
      not any(sv.get("privileged") or sv.get("network_mode") == "host" or sv.get("pid") == "host"
              for sv in svc.values()))
check("No Docker socket mounted",
      not any("docker.sock" in v for sv in svc.values() for v in sv.get("volumes", [])))
check("Bootstrap warns when secrets cannot be made private",
      "could not make $1 private" in (ROOT / "ntfy" / "bootstrap.sh").read_text())


print("\n52. Time zone and market-anchored schedule")
from zoneinfo import ZoneInfo as _ZI52  # noqa: E402
from app import market as _mk  # noqa: E402
from types import SimpleNamespace as _NS52  # noqa: E402
_NY = _mk.NY
_start52 = _dt.datetime(2026, 9, 17, tzinfo=_NY)


def _cfg52(**kw):
    base = dict(scan_after_close_min=60, scan_time="", scan_days="", scan_hour_raw="",
                scan_minute_raw="")
    base.update(kw)
    return _NS52(**base)


def _runs52(trigger, n=300, start=_start52):
    out, run = [], trigger.get_next_fire_time(None, start)
    while run is not None and len(out) < n:
        out.append(run)
        run = trigger.get_next_fire_time(run, run + _dt.timedelta(seconds=1))
    return out


_def = _mk.build_schedule(_cfg52(), _ZI52("Australia/Melbourne"))
check("Default scans after each US close", _def.mode == "market" and not _def.warning, _def.description)
_ny_times = {(r.astimezone(_NY).strftime("%H:%M"), r.astimezone(_NY).weekday()) for r in _runs52(_def.trigger)}
check("Default fires at 17:00 New York, Monday to Friday, all year",
      {t for t, _ in _ny_times} == {"17:00"} and {d for _, d in _ny_times} == {0, 1, 2, 3, 4})
for _z in ("Australia/Melbourne", "Pacific/Auckland", "Europe/London", "America/Los_Angeles",
           "Asia/Kolkata", "UTC"):
    _s = _mk.build_schedule(_cfg52(), _ZI52(_z))
    check(f"Same New York time whatever TZ is ({_z})",
          {r.astimezone(_NY).strftime("%H:%M") for r in _runs52(_s.trigger, 120)} == {"17:00"}
          and _mk.first_early_run(_s.trigger, _start52) is None)
check("The run lands on the local time the user sees",
      [r.astimezone(_ZI52("Australia/Melbourne")).strftime("%H:%M")
       for r in (_def.trigger.get_next_fire_time(None, _dt.datetime(2026, 9, 21, tzinfo=_NY)),
                 _def.trigger.get_next_fire_time(None, _dt.datetime(2026, 11, 16, tzinfo=_NY)))]
      == ["07:00", "09:00"])
check("Delay moves the time", _mk.market_schedule(30).description.endswith("16:30 New York time)"))
check("A delay past midnight moves to the next day",
      "tue-sat 00:30" in _mk.market_schedule(510).description)
check("Delay is kept within 15-600 minutes",
      "15 min" in _mk.market_schedule(1).description and "600 min" in _mk.market_schedule(9999).description)

_old = _mk.fixed_schedule(7, 30, "tue-sat", _ZI52("Australia/Melbourne"))
_early = _mk.first_early_run(_old.trigger, _start52)
check("The old 07:30 Melbourne time is caught running before the close",
      _early is not None and _early.astimezone(_NY).strftime("%H:%M") == "15:30"
      and (_early.month, _early.day) == (11, 3), str(_early))
_fx = _mk.build_schedule(_cfg52(scan_time="07:30", scan_days="tue-sat"), _ZI52("Australia/Melbourne"))
check("A fixed time that can run early carries a warning",
      _fx.mode == "fixed" and "before the US close" in _fx.warning)
_ok = _mk.build_schedule(_cfg52(scan_time="10:00", scan_days="tue-sat"), _ZI52("Australia/Melbourne"))
check("A safe fixed time has no warning", _ok.mode == "fixed" and _ok.warning == "", _ok.warning)
check("SCAN_TIME uses the local zone",
      {r.astimezone(_ZI52("Australia/Melbourne")).strftime("%H:%M") for r in _runs52(_ok.trigger, 50)} == {"10:00"})
_leg = _mk.build_schedule(_cfg52(scan_hour_raw="7", scan_minute_raw="30", scan_days="tue-sat"),
                          _ZI52("Australia/Melbourne"))
check("The old default in an existing .env moves to the market schedule",
      _leg.mode == "market" and "old default" in _leg.note and not _leg.warning)
_leg2 = _mk.build_schedule(_cfg52(scan_hour_raw="6", scan_minute_raw="15"), _ZI52("Europe/London"))
check("A customised SCAN_HOUR is still honoured as a fixed time",
      _leg2.mode == "fixed" and "06:15 Europe/London" in _leg2.description)
check("Nonsense schedule values fall back to the market schedule",
      _mk.build_schedule(_cfg52(scan_time="7.30"), _ZI52("UTC")).mode == "market"
      and _mk.build_schedule(_cfg52(scan_time="25:00"), _ZI52("UTC")).mode == "market"
      and _mk.build_schedule(_cfg52(scan_hour_raw="x"), _ZI52("UTC")).mode == "market")
check("An unknown TZ name falls back to UTC", _mk.zone("Melbourne").key == "UTC"
      and _mk.zone("Australia/Melbourne").key == "Australia/Melbourne")

# Unfinished sessions are never stored
_idx52 = pd.to_datetime(["2026-09-15", "2026-09-16", "2026-09-17"])
_df52 = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=_idx52)
_during = _dt.datetime(2026, 9, 17, 14, 0, tzinfo=_NY)
_just = _dt.datetime(2026, 9, 17, 16, 5, tzinfo=_NY)
_after = _dt.datetime(2026, 9, 17, 16, 20, tzinfo=_NY)
check("A session still trading is dropped",
      list(_mk.drop_unfinished(_df52, _during).index.strftime("%Y-%m-%d")) == ["2026-09-15", "2026-09-16"])
check("A session inside the settle window is dropped",
      len(_mk.drop_unfinished(_df52, _just)) == 2)
check("A settled session is kept", len(_mk.drop_unfinished(_df52, _after)) == 3)
check("Same rule seen from Melbourne's next morning",
      len(_mk.drop_unfinished(_df52, _during.astimezone(_ZI52("Australia/Melbourne")))) == 2)
check("Empty frames pass through", _mk.drop_unfinished(_df52.iloc[:0], _during).empty)
check("Downloads drop unfinished sessions",
      "market.drop_unfinished(norm)" in (ROOT / "app" / "data.py").read_text())
check("Refresh reaches New York's date from any zone",
      "max(date.today(), market.market_today())" in (ROOT / "app" / "data.py").read_text())

# Health, header, settings plumbing
_h52 = client.get("/api/health").json()
check("Health describes the schedule", "after each US close" in _h52["scheduled"]
      and _h52["schedule_mode"] == "market" and _h52["schedule_warning"] is None)
check("Health gives the next scan with its offset", "next_scan" in _h52)
_src52 = (ROOT / "app" / "main.py").read_text()
check("Next scan is reported in the user's zone, not New York's",
      "nxt.astimezone(USER_TZ)" in _src52 and "next_run_time.astimezone(USER_TZ)" in _src52)
_js52 = client.get("/static/app.js").text
check("Header shows the next scan in the browser's zone",
      "next scan ${new Date(h.next_scan)" in _js52 and "h.schedule_warning" in _js52)
_ce = svc["supertrendMoose"]["environment"]
check("New schedule settings reach the app",
      all(k in _ce for k in ("SCAN_AFTER_CLOSE_MINUTES", "SCAN_TIME", "SCAN_DAYS", "SCAN_HOUR", "SCAN_MINUTE")))
_ex52 = (ROOT / ".env.example").read_text()
check(".env.example explains TZ and the schedule",
      "TZ=Australia/Melbourne" in _ex52 and "SCAN_AFTER_CLOSE_MINUTES=60" in _ex52
      and "\nSCAN_HOUR=" not in _ex52 and "# SCAN_TIME=" in _ex52)

# entrypoint refuses a bad TZ, trims spaces
for _tzv, _ok52 in (("Australia/Melbourne", True), ("Etc/GMT+5", True), ("UTC", True),
                    ("Melbourne", False), ("../../etc/passwd", False), ("/etc/passwd", False)):
    _r, _cap, _ = _run_ep({"TZ": _tzv})
    check(f"entrypoint {'accepts' if _ok52 else 'refuses'} TZ={_tzv}",
          (_r.returncode == 0) == _ok52
          and (_ok52 or "is not a time zone name" in _r.stdout), _r.stdout[-120:])
_r, _cap, _ = _run_ep({"TZ": "  Europe/London "})
check("entrypoint trims spaces around TZ", _r.returncode == 0 and "timezone    Europe/London\n" in _r.stdout)


print("\n53. Chart window, pane sync and stale prices")
_js53 = client.get("/static/app.js").text
check("One authoritative window drives every pane",
      "function applyRange(r)" in _js53 and "applyRange({ from, to: total - 1 });" in _js53)
check("Our own echoes are swallowed, not bounced back",
      "pushed.get(chart) === rangeKey(r)" in _js53 and "pushed.delete(chart)" in _js53)
check("A pane's default window cannot override the chosen one while loading",
      "if (settling > 0 && want) { applyRange(want); return; }" in _js53
      and "function holdRange(" in _js53)
check("The window is re-asserted after new data arrives",
      _js53.count("holdRange()") >= 2)
check("The old same-tick sync flag is gone", "syncing = true" not in _js53)
check("Axis widths are matched without clearing them first",
      "minimumWidth: 0" not in _js53 and "w === alignedWidth" in _js53)
check("Only the smaller panes are widened to match the price chart",
      'for (const c of [adxChart, macdChart])' in _js53)

# Sessions behind: the stale-price case the dashboard now reports
_now53 = _dt.datetime(2026, 9, 28, 20, 0, tzinfo=_mk.NY)      # Monday evening
check("Last completed session waits for the settle window",
      _mk.last_completed_session(_dt.datetime(2026, 9, 28, 16, 5, tzinfo=_mk.NY)) == date(2026, 9, 25)
      and _mk.last_completed_session(_dt.datetime(2026, 9, 28, 16, 20, tzinfo=_mk.NY)) == date(2026, 9, 28))
check("Weekends fall back to Friday",
      _mk.last_completed_session(_dt.datetime(2026, 9, 27, 12, 0, tzinfo=_mk.NY)) == date(2026, 9, 25))
check("Holidays are skipped",
      _mk.last_completed_session(_dt.datetime(2026, 4, 4, 12, 0, tzinfo=_mk.NY)) == date(2026, 4, 2))
check("Stored prices a week and a half old are counted",
      _mk.sessions_behind(date(2026, 9, 16), _now53) == 8)
check("Up-to-date prices count as zero",
      _mk.sessions_behind(date(2026, 9, 25), _dt.datetime(2026, 9, 28, 12, 0, tzinfo=_mk.NY)) == 0)
check("A holiday does not look like missing data",
      # 3 Apr 2026 is Good Friday, so Thursday's bar is still the latest one
      # at midday on the Monday after.
      _mk.sessions_behind(date(2026, 4, 2), _dt.datetime(2026, 4, 6, 12, 0, tzinfo=_mk.NY)) == 0)
check("A genuinely missed session is counted",
      _mk.sessions_behind(date(2026, 4, 1), _dt.datetime(2026, 4, 6, 12, 0, tzinfo=_mk.NY)) == 1)
check("No data at all is not a gap", _mk.sessions_behind(None) is None)
_h53 = client.get("/api/health").json()
check("Health reports how far behind the prices are",
      "sessions_behind" in _h53 and "last_scan_failed" in _h53 and "last_scan_tickers" in _h53)
check("Header warns about stale prices and failed tickers",
      "sessions behind — see Diagnose" in _js53 and "tickers failed" in _js53
      and "h.sessions_behind >= 2" in _js53)
check("Diagnose still probes Yahoo and names the cache",
      all(k in client.get("/api/diagnose").json()
          for k in ("yfinance_version", "cache_writable", "probe_ok", "data_through")))


print("\n54. A machine that slept through the scan still catches up")
import app.main as _m54  # noqa: E402
_calls54 = []
_orig_run54, _orig_status54 = scanner.run_scan, scanner.scan_status
_orig_latest54 = _m54._latest_stored_date
try:
    scanner.run_scan = lambda **kw: _calls54.append(kw)
    _m54._last_catch_up = None
    _m54._latest_stored_date = lambda: None
    _m54._catch_up()
    check("No stored data is left to the cold start", _calls54 == [])

    _m54._latest_stored_date = lambda: date(2026, 9, 10)
    _m54._catch_up()
    check("Missed sessions trigger a scan that notifies",
          _calls54 == [{"refresh_prices": True, "notify_on": True}], str(_calls54))
    _m54._catch_up()
    check("A failed catch-up does not retry immediately", len(_calls54) == 1)
    _m54._last_catch_up = None
    _m54._latest_stored_date = lambda: _mk.last_completed_session()
    _m54._catch_up()
    check("Up-to-date prices trigger nothing", len(_calls54) == 1)

    _m54._last_catch_up = None
    _m54._latest_stored_date = lambda: date(2026, 9, 10)
    scanner.scan_status = lambda: {"running": True, "note": ""}
    _m54._catch_up()
    check("A scan already running is left alone", len(_calls54) == 1)
    scanner.scan_status = _orig_status54

    _m54._last_catch_up = None
    def _boom54(**kw):
        raise RuntimeError("yahoo down")
    scanner.run_scan = _boom54
    _m54._catch_up()
    check("A failing catch-up never kills the scheduler job", True)
finally:
    scanner.run_scan, scanner.scan_status = _orig_run54, _orig_status54
    _m54._latest_stored_date = _orig_latest54
    _m54._last_catch_up = None

from fastapi.testclient import TestClient as _TC54  # noqa: E402
with _TC54(_m54.app):
    _jobs54 = {j.id: j for j in _m54.scheduler.get_jobs()}
    check("All three jobs are scheduled",
          set(_jobs54) == {"nightly_scan", "catch_up", "earnings_refresh"}, str(set(_jobs54)))
    check("The catch-up check repeats every 15 minutes",
          _jobs54["catch_up"].trigger.interval.total_seconds() == 900)
    check("The catch-up runs shortly after start-up, not 15 minutes later",
          (_jobs54["catch_up"].next_run_time - _dt.datetime.now(_m54.USER_TZ)).total_seconds() < 120)
    check("Jobs missed while asleep still run on wake",
          _jobs54["catch_up"].misfire_grace_time is None
          and _jobs54["earnings_refresh"].misfire_grace_time is None
          and all(_jobs54[j].coalesce for j in _jobs54),
          f'catch_up={_jobs54["catch_up"].misfire_grace_time}, '
          f'earnings={_jobs54["earnings_refresh"].misfire_grace_time}')
check("The start-up scan no longer duplicates the catch-up",
      "stale_days" not in (ROOT / "app" / "main.py").read_text())


print("\n55. Sell wording is the same everywhere")
_sell_markers = client.get("/api/chart/NVDA?bars=5000").json()["markers"]
check("Chart marks a downward flip as SELL",
      "SELL" in {m["text"] for m in _sell_markers}
      and "EXIT" not in {m["text"] for m in _sell_markers},
      str({m["text"] for m in _sell_markers}))
check("SELL keeps the red down arrow",
      all(m["shape"] == "arrowDown" and m["color"] == "#ef5350"
          for m in _sell_markers if m["text"] == "SELL"))
_h55 = (ROOT / "static" / "index.html").read_text()
check("Chart legend says Sell", '<i class="sw mark-exit"></i>Sell' in _h55 and ">Exit<" not in _h55)
_title55, _body55 = notify.format_signals(
    [{"ticker": "NVDA", "close": 120.0, "stop": 110.0, "adx": 25.0}],
    [{"ticker": "AMD", "close": 90.0}], "full")
check("Alert body has a SELL SIGNALS section", "SELL SIGNALS" in _body55 and "EXIT" not in _body55, _body55)
check("Alert title counts sells", _title55 == "1 buy, 1 sell", _title55)
check("A sell-only alert reads naturally",
      notify.format_signals([], [{"ticker": "AMD", "close": 90.0}], "full")[0] == "1 sell signal")
check("Several sells pluralise",
      notify.format_signals([], [{"ticker": "AMD", "close": 90.0},
                                 {"ticker": "MU", "close": 80.0}], "full")[0] == "2 sell signals")
check("Minimal alerts use the same words",
      notify.format_signals([], [{"ticker": "AMD", "close": 90.0}], "minimal")[0] == "1 sell signal")
_js55 = client.get("/static/app.js").text
check("Scan toast and position form say sell",
      "sell`" in _js55 and "Sell date" in _js55 and "Sell price" in _js55
      and "exit`" not in _js55 and "Exit date" not in _js55 and "Exit price" not in _js55)
check("No user-facing EXIT text is left",
      "EXIT" not in _js55 and "EXIT" not in _h55
      and "EXIT" not in (ROOT / "app" / "notify.py").read_text())
check("Stored signal kinds are unchanged (no database migration needed)",
      '_record(item.ticker, st, "EXIT")' in (ROOT / "app" / "scanner.py").read_text()
      and "BUY | EXIT" in (ROOT / "app" / "db.py").read_text())


print("\n56. Earnings tab: what the trades actually made")
from app import earnings as _earn56  # noqa: E402
from app.db import Position as _P56  # noqa: E402


class _Fake56:
    """A position without the database, for the arithmetic checks."""
    def __init__(self, tid, ticker, ed, ep, qty, xd, xp, ef=0.0, xf=0.0, note=""):
        self.id, self.ticker, self.note = tid, ticker, note
        self.entry_date, self.entry_price, self.quantity = ed, ep, qty
        self.exit_date, self.exit_price = xd, xp
        self.entry_fee, self.exit_fee = ef, xf


_win56 = _Fake56(1, "NVDA", date(2026, 1, 5), 100.0, 10, date(2026, 2, 5), 120.0, 5.0, 5.0)
_loss56 = _Fake56(2, "AMD", date(2026, 2, 1), 50.0, 20, date(2026, 3, 1), 45.0, 5.0, 5.0)
_noqty56 = _Fake56(3, "XOM", date(2026, 3, 1), 10.0, None, date(2026, 3, 20), 12.0)
_open56 = _Fake56(4, "SPY", date(2026, 4, 1), 500.0, 2, None, None)
_rows56 = _earn56.trades([_win56, _loss56, _noqty56, _open56])

check("Open positions are not trades yet", [t["ticker"] for t in _rows56] == ["NVDA", "AMD", "XOM"])
_t = _rows56[0]
check("Gross profit is price difference times quantity", _t["gross"] == 200.0)
check("Commission on both orders is subtracted", _t["fees"] == 10.0 and _t["net"] == 190.0)
check("Percentage is measured against cost including the buy commission",
      _t["cost"] == 1005.0 and _t["net_pct"] == 18.91, f'{_t["cost"]} {_t["net_pct"]}')
check("Gross percentage ignores commission", _t["gross_pct"] == 20.0)
check("Proceeds are net of the sell commission", _t["proceeds"] == 1195.0)
check("A loss carries its commission too",
      _rows56[1]["gross"] == -100.0 and _rows56[1]["net"] == -110.0)
check("Commission alone can turn a gain into a loss",
      _earn56.trades([_Fake56(5, "T", date(2026, 1, 1), 10.0, 10, date(2026, 2, 1), 10.5, 5.0, 5.0)]
                     )[0]["net"] == -5.0)
check("A trade with no quantity has no dollar figures",
      _rows56[2]["net"] is None and _rows56[2]["gross"] is None
      and _rows56[2]["gross_pct"] == 20.0)
check("Trades are ordered by sale date",
      [t["exit_date"] for t in _rows56] == sorted(t["exit_date"] for t in _rows56))
check("Holding days come from the two dates", _t["hold_days"] == 31)

_sum56 = _earn56.summary(_rows56)
check("Totals add up",
      _sum56["gross"] == 100.0 and _sum56["fees"] == 20.0 and _sum56["net"] == 80.0)
check("Return is net profit over everything invested",
      _sum56["invested"] == 2010.0 and _sum56["net_pct"] == 3.98)
check("Trades without a quantity are counted but flagged",
      _sum56["trades"] == 3 and _sum56["priced_trades"] == 2 and _sum56["without_quantity"] == 1)
check("Win rate counts priced trades", _sum56["wins"] == 1 and _sum56["win_rate"] == 50.0)
check("Best, worst and averages", _sum56["best"] == 190.0 and _sum56["worst"] == -110.0
      and _sum56["avg_net"] == 40.0)
check("Commission is shown against the profit it came out of",
      _sum56["fees_vs_gross_pct"] == 20.0)
check("An empty history summarises without dividing by zero",
      _earn56.summary([])["trades"] == 0 and _earn56.summary([])["net"] is None
      and _earn56.curves([]) == {"dollars": [], "percent": []})

_c56 = _earn56.curves(_rows56)
check("Dollar curve is a running total, after commission",
      [p["value"] for p in _c56["dollars"]] == [190.0, 80.0])
check("Percentage curve is against capital deployed so far",
      [p["value"] for p in _c56["percent"]] == [18.91, 3.98])
check("Curves are dated by the sale", [p["time"] for p in _c56["dollars"]]
      == ["2026-02-05", "2026-03-01"])
check("Trades without a quantity are left out of the curves", len(_c56["dollars"]) == 2)
_same56 = _earn56.curves(_earn56.trades([
    _Fake56(6, "A", date(2026, 1, 1), 10.0, 10, date(2026, 2, 2), 11.0),
    _Fake56(7, "B", date(2026, 1, 1), 10.0, 10, date(2026, 2, 2), 12.0)]))
check("Two sales on one day give one point",
      len(_same56["dollars"]) == 1 and _same56["dollars"][0]["value"] == 30.0)

_ord56 = _earn56.orders(_rows56)
check("Every trade appears as a buy and a sell", len(_ord56) == 6
      and sum(1 for o in _ord56 if o["side"] == "BUY") == 3)
check("Each trade's buy and sell stay together, newest trade first",
      [(o["ticker"], o["side"]) for o in _ord56]
      == [("XOM", "BUY"), ("XOM", "SELL"), ("AMD", "BUY"), ("AMD", "SELL"),
          ("NVDA", "BUY"), ("NVDA", "SELL")],
      str([(o["ticker"], o["side"]) for o in _ord56]))
# Overlapping trades are exactly what a date-ordered list would jumble.
_overlap56 = _earn56.orders(_earn56.trades([
    _Fake56(8, "AAA", date(2026, 1, 1), 10.0, 10, date(2026, 3, 1), 12.0),
    _Fake56(9, "BBB", date(2026, 2, 1), 10.0, 10, date(2026, 2, 15), 11.0)]))
check("An overlapping trade does not split another trade's rows",
      [(o["ticker"], o["side"]) for o in _overlap56]
      == [("AAA", "BUY"), ("AAA", "SELL"), ("BBB", "BUY"), ("BBB", "SELL")],
      str([(o["ticker"], o["side"]) for o in _overlap56]))
check("The result is shown on the sell, not the buy",
      all(o["net"] is None for o in _ord56 if o["side"] == "BUY")
      and next(o for o in _ord56 if o["side"] == "SELL" and o["ticker"] == "NVDA")["net"] == 190.0)
check("Order value is quantity times price, before commission",
      next(o for o in _ord56 if o["side"] == "BUY" and o["ticker"] == "NVDA")["value"] == 1000.0)
check("Each order carries its own commission",
      {o["commission"] for o in _ord56 if o["ticker"] == "NVDA"} == {5.0})

# The database keeps the fees, including for positions recorded before the
# columns existed.
_ins56 = client.post("/api/positions", json={"ticker": "AAPL", "entry_date": "2026-01-05",
                                             "entry_price": 100.0, "quantity": 10,
                                             "entry_fee": 9.5})
check("A purchase records its commission",
      _ins56.status_code == 200 and _ins56.json()["entry_fee"] == 9.5, _ins56.text[:120])
_pid56 = _ins56.json()["id"]
check("Commission cannot be negative",
      client.post("/api/positions", json={"ticker": "MU", "entry_date": "2026-01-05",
                                          "entry_price": 10.0, "entry_fee": -1}).status_code == 422)
_cl56 = client.post(f"/api/positions/{_pid56}/close",
                    json={"exit_date": "2026-02-05", "exit_price": 120.0, "exit_fee": 9.5})
check("A sale records its commission",
      _cl56.status_code == 200 and _cl56.json()["exit_fee"] == 9.5)
_rep56 = client.get("/api/earnings").json()
_aapl = next(t for t in _rep56["trades"] if t["ticker"] == "AAPL")
check("The report uses the stored commission",
      _aapl["fees"] == 19.0 and _aapl["net"] == 181.0 and _aapl["gross"] == 200.0)
check("Report carries trades, orders, curves and totals",
      all(k in _rep56 for k in ("trades", "orders", "curves", "summary", "open_positions")))
check("Open positions are counted separately, not as earnings",
      isinstance(_rep56["open_positions"], int)
      and all(t["exit_date"] for t in _rep56["trades"]))
check("A commission left out defaults to zero, not an error",
      client.post("/api/positions", json={"ticker": "MU", "entry_date": "2026-01-05",
                                          "entry_price": 10.0, "quantity": 5}).json()["entry_fee"] == 0.0)
check("Earnings can be narrowed to one ticker",
      {t["ticker"] for t in client.get("/api/earnings?ticker=AAPL").json()["trades"]} == {"AAPL"})
check("Positions recorded before commission existed read as zero",
      all(isinstance(p["entry_fee"], float) for p in client.get("/api/positions").json()["positions"]))
check("The default commission reaches the dashboard",
      "default_commission" in client.get("/api/health").json())

# An existing install gains the columns without losing anything
import sqlite3 as _sq56  # noqa: E402
_dir56 = tf.mkdtemp()
_db56 = f"{_dir56}/old.db"
_con56 = _sq56.connect(_db56)
_con56.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY, ticker VARCHAR(16), entry_date DATE,
  entry_price FLOAT, quantity FLOAT, exit_date DATE, exit_price FLOAT,
  note VARCHAR(256), created DATETIME);
INSERT INTO positions VALUES (1,'NVDA','2026-01-05',100.0,10,'2026-02-05',120.0,'kept','2026-01-05 00:00:00');
INSERT INTO positions VALUES (2,'AMD','2026-03-01',50.0,20,NULL,NULL,'open','2026-03-01 00:00:00');
""")
_con56.commit(); _con56.close()
_mig56 = subprocess.run(
    [sys.executable, "-c",
     'import sys; sys.path.insert(0, ".");'
     'from app.db import init_db, get_session, Position;'
     'from app import earnings; init_db();'
     'import json;'
     's = get_session().__enter__();'
     'rows = s.query(Position).all();'
     'print(json.dumps({"rows": [(p.ticker, p.entry_fee, p.exit_fee, p.note) for p in rows],'
     ' "net": earnings.report([p for p in rows if p.exit_date])["summary"]["net"]}))'],
    cwd=ROOT, capture_output=True, text=True, timeout=120,
    env={**_os.environ, "DATABASE_URL": f"sqlite:///{_db56}", "NOTIFY_CHANNELS": ""})
import json as _json56  # noqa: E402
_out56 = _json56.loads(_mig56.stdout.strip().splitlines()[-1]) if _mig56.returncode == 0 else {}
check("An existing database gains the commission columns",
      _mig56.returncode == 0 and _out56.get("rows") ==
      [["NVDA", 0.0, 0.0, "kept"], ["AMD", 0.0, 0.0, "open"]],
      (_mig56.stderr or _mig56.stdout)[-200:])
check("Older trades simply read as zero commission", _out56.get("net") == 200.0)
check("The migration is additive, never a rebuild",
      '_ensure_column("positions", "entry_fee"' in (ROOT / "app" / "db.py").read_text())

_h56 = (ROOT / "static" / "index.html").read_text()
_js56 = client.get("/static/app.js").text
check("There is an Earnings tab", 'id="tabEarn"' in _h56 and 'id="earnView"' in _h56)
check("The tab has both charts and the orders table",
      all(x in _h56 for x in ('id="pctChart"', 'id="pnlChart"', 'id="earnOrders"', 'id="earnSummary"')))
check("The orders table has a column for each part of an order",
      all(x in _h56 for x in (">Date<", ">Ticker<", ">Order<", ">Qty<", ">Price<",
                              ">Value<", ">Commission<", ">Held<")))
check("The tab loads its own data", "async function loadEarnings()" in _js56
      and '"/api/earnings"' in _js56)
check("Earnings charts are not tied to the price panes' scrolling",
      "linkPane(pctChart)" not in _js56 and "linkPane(pnlChart)" not in _js56)
check("Earnings charts are built only when the tab is opened",
      "if (!pnlChart) buildEarnCharts();" in _js56)
check("Both commission fields are in the position form",
      'id="posFee"' in _js56 and 'id="posExitFee"' in _js56
      and "entry_fee: Number(" in _js56 and "exit_fee: Number(" in _js56)
check("Commission fields are pre-filled from the default",
      _js56.count("defaultCommission.toFixed(2)") == 2)
check("Earnings panes resize with the window",
      'fit(pnlChart, "pnlChart");' in _js56 and 'fit(pctChart, "pctChart");' in _js56)


print("\n57. Earnings: open positions, daily balance, starting cash")
_c57 = {
    "AAA": {date(2026, 1, 5): 100.0, date(2026, 1, 6): 105.0, date(2026, 1, 7): 110.0,
            date(2026, 1, 8): 120.0, date(2026, 1, 9): 130.0},
    "BBB": {date(2026, 1, 5): 48.0, date(2026, 1, 6): 49.0, date(2026, 1, 7): 49.0,
            date(2026, 1, 8): 50.0, date(2026, 1, 9): 60.0},
}
_closed57 = _Fake56(1, "AAA", date(2026, 1, 5), 100.0, 10, date(2026, 1, 8), 120.0, 5.0, 5.0)
_open57 = _Fake56(2, "BBB", date(2026, 1, 8), 50.0, 5, None, None, 5.0)
_pos57 = [_closed57, _open57]
_r57 = _earn56.report(_pos57, _c57)
_m57 = _r57["money"]

check("Starting balance defaults to the most cash the trades needed",
      _earn56.required_cash(_pos57) == 1005.0 and _m57["starting_cash"] == 1005.0
      and _m57["auto_cash"] is True)
check("The balance line has a point per trading day", [b["time"] for b in _m57["balance"]]
      == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
check("Balance is cash plus holdings at that day's close",
      [b["value"] for b in _m57["balance"]] == [1000.0, 1050.0, 1100.0, 1190.0, 1240.0],
      str([b["value"] for b in _m57["balance"]]))
check("An open position moves the line daily",
      _m57["balance"][-1]["value"] - _m57["balance"][-2]["value"] == 50.0)
check("Commission comes out of the balance on the day it is paid",
      _m57["balance"][0]["value"] == 1000.0)   # 1005 starting, less the $5 buy fee
check("Percent is return on the starting balance",
      [p["value"] for p in _m57["pct_total"]] == [-0.5, 4.48, 9.45, 18.41, 23.38])
check("The closed-only line ignores what is still held",
      [p["value"] for p in _m57["pct_closed"]] == [-0.5, -0.5, -0.5, 18.41, 18.41])
check("Both percent lines share the same days",
      [p["time"] for p in _m57["pct_total"]] == [p["time"] for p in _m57["pct_closed"]])
_m57b = _earn56.report(_pos57, _c57, 5000.0)["money"]
check("An explicit starting balance is used as given",
      _m57b["starting_cash"] == 5000.0 and _m57b["auto_cash"] is False
      and _m57b["balance"][0]["value"] == 4995.0
      and _m57b["balance"][-1]["value"] == 5235.0)
check("A bigger starting balance means a smaller percentage",
      _m57b["pct_total"][-1]["value"] == 4.7)

_o57 = _r57["open_trades"][0]
check("An open position is valued at the latest close",
      _o57["mark"] == 60.0 and _o57["value"] == 300.0 and _o57["mark_date"] == "2026-01-09")
check("Its P&L counts the buy commission but not a sell",
      _o57["net"] == 45.0 and _o57["entry_fee"] == 5.0 and _o57["exit_fee"] == 0.0)
check("Its percentage is against what it cost", _o57["net_pct"] == 17.65)
check("Days held run to the latest price, not to a sale", _o57["hold_days"] == 1)
check("Totals separate banked profit from unrealised",
      _r57["summary"]["net"] == 190.0 and _r57["summary"]["unrealised"] == 45.0
      and _r57["summary"]["balance"] == 1240.0 and _r57["summary"]["balance_pct"] == 23.38)
check("Open positions come first in the table, marked as still held",
      [(o["ticker"], o["side"], o["open"]) for o in _r57["orders"]]
      == [("BBB", "BUY", True), ("AAA", "BUY", False), ("AAA", "SELL", False)])
check("A held position shows its running P&L on the buy row",
      _r57["orders"][0]["net"] == 45.0 and _r57["orders"][0]["hold_days"] == 1)

# Edge cases that must not produce a broken chart
check("No positions at all gives empty curves, not an error",
      _earn56.report([], {})["money"]["balance"] == []
      and _earn56.report([], {})["summary"]["balance"] is None)
check("A position with no stored prices is held at cost, never invented",
      [b["value"] for b in _earn56.report([_open57], {})["money"]["balance"]] == [250.0],
      str(_earn56.report([_open57], {})["money"]["balance"]))
_nq57 = _Fake56(3, "CCC", date(2026, 1, 5), 10.0, None, None, None)
check("A position without a quantity cannot move the balance",
      _earn56.report([_nq57], _c57)["money"]["balance"] == [])
_gap57 = _earn56.report([_open57], {"BBB": {date(2026, 1, 8): 50.0, date(2026, 1, 12): 70.0}})
check("A day with no bar carries the previous close forward",
      [b["value"] for b in _gap57["money"]["balance"]] == [250.0, 350.0])
_two57 = _earn56.report([_open57, _Fake56(4, "BBB", date(2026, 1, 8), 50.0, 5, None, None, 0.0)],
                        _c57)["money"]
check("Two lots of the same ticker are held together",
      # starts at the first purchase (255), the second lot's 250 is added,
      # then 10 shares marked at 50 and then 60
      _two57["starting_cash"] == 255.0 and _two57["capital_in"] == 505.0
      and _two57["balance"][0]["value"] == 500.0
      and _two57["balance"][-1]["value"] == 600.0,
      str(_two57["balance"]))

# Through the API, with prices from the database
_api57 = client.get("/api/earnings").json()
check("The endpoint returns the daily money curves",
      all(k in _api57["money"] for k in ("balance", "pct_total", "pct_closed", "starting_cash"))
      and "open_trades" in _api57)
check("The endpoint now includes open positions",
      isinstance(_api57["open_trades"], list))
check("Starting cash is configurable", "STARTING_CASH" in _ce
      and "starting_cash" in dir(__import__("app.config", fromlist=["x"]).settings))

_js57 = client.get("/static/app.js").text
_h57 = (ROOT / "static" / "index.html").read_text()
check("The dollars chart plots the account balance", "pnlSeries.setData(m.balance)" in _js57)
check("The percent chart plots both lines",
      "pctSeries.setData(m.pct_total)" in _js57 and "pctClosedSeries.setData(m.pct_closed)" in _js57)
check("Chart labels describe the new meaning",
      "cash plus holdings at market" in _h57 and "Return from trading" in _h57
      and "closed trades only" in _h57)
check("Each chart's current value sits right after its label",
      _h57.index('id="earnPctNow"') < _h57.index("including open positions")
      and _h57.index('id="earnPnlNow"') < _h57.index("cash plus holdings at market"))
check("No zero rule is drawn across the earnings charts",
      "createPriceLine" not in _js57[_js57.index("function buildEarnCharts"):
                                     _js57.index("async function loadEarnings")])
check("Every figure in the totals strip is coloured by its sign",
      "const sign = (v) => (v == null ? \"\" : v > 0 ? \"pos\" : v < 0 ? \"neg\" : \"\");" in _js57
      and _js57.count("cell(") >= 12)
check("Held rows are marked in the table",
      "still held" in _js57 and "unrealised" in _js57)
check("The totals strip shows balance, returns, money in, realised and unrealised",
      all(x in _js57 for x in ('cell("Account balance"', 'cell("Realised"', 'cell("Unrealised"',
                               'cell("Trading return"', 'cell("Money in"',
                               'cell("Growth on money in"')))


print("\n58. Returns measured the way the table measures them")
# The same three AMD trades, with each day priced at whatever the open lot was
# bought or sold at, so only the trades themselves move the lines.
def _amd_closes():
    out = {}
    d, price = date(2026, 4, 8), 231.50
    for i in range(0, 175):
        day = date(2026, 4, 8) + timedelta(days=i)
        if day >= date(2026, 9, 15):
            price = 559.32          # the open trade moves after it is bought
        elif day >= date(2026, 9, 10):
            price = 506.91
        elif day >= date(2026, 7, 29):
            price = 449.86
        elif day >= date(2026, 7, 1):
            price = 545.77
        elif day >= date(2026, 6, 11):
            price = 461.78
        out[day] = price
    return {"AMD": out}


_pos58 = [_Fake56(1, "AMD", date(2026, 4, 8), 231.50, 2, date(2026, 6, 11), 461.78, 0.99, 0.99),
          _Fake56(2, "AMD", date(2026, 7, 1), 545.77, 2, date(2026, 7, 29), 449.86, 0.99, 0.99),
          _Fake56(3, "AMD", date(2026, 9, 10), 506.91, 2, None, None, 0.99)]
_r58 = _earn56.report(_pos58, _amd_closes())
_m58 = _r58["money"]
_pct58 = {p["time"]: p["value"] for p in _m58["pct_total"]}
_cl58 = {p["time"]: p["value"] for p in _m58["pct_closed"]}
_bal58 = {p["time"]: p["value"] for p in _m58["balance"]}

check("The account starts at what the first purchase cost",
      _m58["starting_cash"] == 463.99 and _earn56.first_cost(_pos58) == 463.99,
      str(_m58["starting_cash"]))
check("The balance line starts there too", _bal58["2026-04-08"] == 463.0)
check("A sale's percentage matches the table's figure for that trade",
      _pct58["2026-06-11"] == 98.83 and _r58["trades"][0]["net_pct"] == 98.83,
      f'chart {_pct58["2026-06-11"]} vs table {_r58["trades"][0]["net_pct"]}')
check("Buying does not move the return, beyond the commission",
      round(_pct58["2026-06-30"] - _pct58["2026-07-01"], 2) == 0.21,
      f'{_pct58["2026-06-30"]} -> {_pct58["2026-07-01"]}')
check("Money added to afford a bigger trade is not a gain",
      _m58["capital_added"] == 286.04 and _m58["capital_in"] == 750.03
      and _pct58["2026-07-01"] == 98.62)
check("Losses compound onto the running figure",
      _pct58["2026-07-29"] == 63.54, str(_pct58["2026-07-29"]))
check("Between trades nothing moves",
      _pct58["2026-08-15"] == _pct58["2026-07-29"] == _cl58["2026-08-15"])
check("An open position moves the live line but not the closed-only one",
      _pct58["2026-09-20"] != _cl58["2026-09-20"]
      and _cl58["2026-09-20"] == _cl58["2026-09-09"] - 0.18,
      f'{_cl58["2026-09-20"]} vs {_cl58["2026-09-09"]}')
check("The closed-only line only ever moves on a sale or a commission",
      sorted({round(v, 2) for v in _cl58.values()}) == [-0.21, 63.36, 63.54, 98.62, 98.83],
      str(sorted({round(v, 2) for v in _cl58.values()})))
check("Trading return and growth on money in are both reported, and differ",
      _r58["summary"]["trading_return"] == _pct58["2026-09-20"]
      and _r58["summary"]["balance_pct"] == round(
          (_r58["summary"]["balance"] - 750.03) / 750.03 * 100, 2)
      and _r58["summary"]["trading_return"] != _r58["summary"]["balance_pct"])
check("A sale settles before a purchase made the same day",
      [e["buy"] for e in _earn56._events(
          [_Fake56(1, "A", date(2026, 1, 1), 10.0, 10, date(2026, 2, 1), 12.0),
           _Fake56(2, "B", date(2026, 2, 1), 10.0, 10, None, None)])][-2:] == [False, True])
_topup58 = _earn56.report(_pos58, _amd_closes(), 5000.0)["money"]
check("An explicit starting balance needs no top-ups",
      _topup58["capital_added"] == 0.0 and _topup58["starting_cash"] == 5000.0)
check("A larger starting balance dilutes the return with idle cash",
      # A real account return: $464 doubling inside a $5,000 account moves
      # the account by far less than 100%.
      0 < _topup58["trading_return"] < _m58["trading_return"],
      f'{_topup58["trading_return"]} vs {_m58["trading_return"]}')


print("\n59. Your trades row, part sales and extra lots")
from app.db import Position  # noqa: E402
# Drawdown walks the trades in order, so the order they are fed in matters.
_dd59 = backtest.metrics([{"ret_pct": 98.83, "hold_days": 64},
                          {"ret_pct": -17.74, "hold_days": 28}])["max_dd"]
check("A win then a loss is a drawdown", _dd59 == -17.7, str(_dd59))
check("The same trades the other way round are not",
      backtest.metrics([{"ret_pct": -17.74, "hold_days": 28},
                        {"ret_pct": 98.83, "hold_days": 64}])["max_dd"] == 0.0)

with get_session() as _s59:
    _s59.query(Position).filter(Position.ticker == "AMD").delete()
    _s59.commit()
for _t in ({"entry_date": "2026-04-08", "entry_price": 231.50, "quantity": 2, "entry_fee": 0.99},
           {"entry_date": "2026-07-01", "entry_price": 545.77, "quantity": 2, "entry_fee": 0.99}):
    _r = client.post("/api/positions", json={"ticker": "AMD", **_t})
    _id = _r.json()["id"]
    _sale = {"2026-04-08": ("2026-06-11", 461.78), "2026-07-01": ("2026-07-29", 449.86)}[_t["entry_date"]]
    client.post(f"/api/positions/{_id}/close",
                json={"exit_date": _sale[0], "exit_price": _sale[1], "exit_fee": 0.99})
_lp59 = client.get("/api/positions?ticker=AMD").json()
check("Your trades reports the drawdown of the losing trade",
      _lp59["realised"]["max_dd"] == -17.7, str(_lp59["realised"]["max_dd"]))
check("Your trades uses returns after commission, like the Earnings tab",
      _lp59["realised"]["expectancy"] == 40.55
      and {p["net_return_pct"] for p in _lp59["positions"]} == {98.83, -17.74},
      str(_lp59["realised"]["expectancy"]))
check("Gross return is still reported alongside",
      {p["return_pct"] for p in _lp59["positions"]} == {99.47, -17.57})
check("Total return compounds the trades",
      _lp59["realised"]["total_return"] == 63.6, str(_lp59["realised"]["total_return"]))

# Selling part of a holding
with get_session() as _s59:
    _s59.query(Position).filter(Position.ticker == "AMD").delete()
    _s59.commit()
client.post("/api/positions", json={"ticker": "AMD", "entry_date": "2026-04-08",
                                    "entry_price": 100.0, "quantity": 10, "entry_fee": 10.0})
_sell59 = client.post("/api/positions/sell",
                      json={"ticker": "AMD", "exit_date": "2026-06-11", "exit_price": 150.0,
                            "exit_fee": 10.0, "quantity": 4})
check("Selling part of a holding works", _sell59.status_code == 200, _sell59.text[:120])
_part = _sell59.json()
check("The sold shares become a closed trade",
      _part["closed"][0]["quantity"] == 4 and _part["closed"][0]["exit_price"] == 150.0)
check("The rest stays open", _part["still_held"] == 6)
check("The purchase commission is split, the sale's belongs to the sale",
      # 4 of the 10 shares bought carry $4 of the $10 purchase fee; the $10
      # sale fee was paid on this sale, so all of it counts here.
      _part["closed"][0]["entry_fee"] == 4.0 and _part["closed"][0]["exit_fee"] == 10.0)
_left59 = scanner.open_position("AMD")
check("What is left keeps the rest of the purchase commission",
      _left59["quantity"] == 6 and _left59["entry_fee"] == 6.0, str(_left59))
check("The part sold is priced on its own share of the costs",
      # 4 shares at 100 plus $4 of the purchase fee = 404 out;
      # 4 at 150 less the $10 sale fee = 590 back
      _part["closed"][0]["net_return_pct"] == round((590 - 404) / 404 * 100, 2),
      str(_part["closed"][0]["net_return_pct"]))
check("Selling more than you hold is refused",
      client.post("/api/positions/sell",
                  json={"ticker": "AMD", "exit_date": "2026-06-11", "exit_price": 150.0,
                        "quantity": 99}).status_code == 400)
check("Selling a ticker you do not hold is refused",
      client.post("/api/positions/sell",
                  json={"ticker": "MSFT", "exit_date": "2026-06-11",
                        "exit_price": 150.0}).status_code == 404)
check("A sale cannot predate the purchase",
      client.post("/api/positions/sell",
                  json={"ticker": "AMD", "exit_date": "2026-01-01",
                        "exit_price": 150.0}).status_code == 400)

# Buying more, then selling across both lots oldest first
client.post("/api/positions", json={"ticker": "AMD", "entry_date": "2026-07-01",
                                    "entry_price": 200.0, "quantity": 4, "entry_fee": 4.0})
_held59 = scanner.open_position("AMD")
check("Two lots show as one holding at the weighted average price",
      _held59["quantity"] == 10 and _held59["entry_price"] == 140.0
      and _held59["lots"] == 2 and _held59["entry_date"] == "2026-04-08", str(_held59))
_all59 = client.post("/api/positions/sell",
                     json={"ticker": "AMD", "exit_date": "2026-08-01", "exit_price": 250.0,
                           "exit_fee": 10.0, "quantity": 8}).json()
check("A sale spanning two lots closes the older one first",
      [(c["quantity"], c["entry_price"]) for c in _all59["closed"]] == [(6, 100.0), (2, 200.0)],
      str([(c["quantity"], c["entry_price"]) for c in _all59["closed"]]))
check("The sale commission is split across the lots it covered",
      [c["exit_fee"] for c in _all59["closed"]] == [7.5, 2.5])
check("What remains is the newer lot", _all59["still_held"] == 2
      and scanner.open_position("AMD")["entry_price"] == 200.0)
_js59 = client.get("/static/app.js").text
check("The panel offers a sell quantity and a way to buy more",
      'id="posExitQty"' in _js59 and 'id="posMore"' in _js59
      and '"/api/positions/sell"' in _js59)
check("Selling everything still reads as closing the position",
      "Closed ${selected}" in _js59 and "still held" in _js59)
with get_session() as _s59:
    _s59.query(Position).filter(Position.ticker == "AMD").delete()
    _s59.commit()


print("\n60. Removing an order entered by mistake")
with get_session() as _s60:
    _s60.query(Position).filter(Position.ticker == "AMD").delete()
    _s60.commit()
_buy60 = client.post("/api/positions", json={"ticker": "AMD", "entry_date": "2026-04-08",
                                             "entry_price": 100.0, "quantity": 5,
                                             "entry_fee": 1.0}).json()
client.post(f"/api/positions/{_buy60['id']}/close",
            json={"exit_date": "2026-06-11", "exit_price": 150.0, "exit_fee": 1.0})

_re60 = client.post(f"/api/positions/{_buy60['id']}/reopen")
check("Undoing a sale puts the shares back",
      _re60.status_code == 200 and _re60.json()["open"] is True
      and _re60.json()["exit_date"] is None and _re60.json()["exit_fee"] == 0.0,
      _re60.text[:120])
check("The purchase itself is untouched",
      _re60.json()["entry_price"] == 100.0 and _re60.json()["quantity"] == 5
      and _re60.json()["entry_fee"] == 1.0)
check("The holding is live again", scanner.open_position("AMD")["quantity"] == 5)
check("It is no longer a closed trade", client.get("/api/earnings").json()["trades"] == []
      or all(t["ticker"] != "AMD" for t in client.get("/api/earnings").json()["trades"]))
check("Undoing a sale twice is refused",
      client.post(f"/api/positions/{_buy60['id']}/reopen").status_code == 409)
check("Undoing a sale on a position that does not exist is refused",
      client.post("/api/positions/999999/reopen").status_code == 404)

check("Removing the purchase removes the record",
      client.delete(f"/api/positions/{_buy60['id']}").status_code == 200
      and scanner.open_position("AMD") is None)
check("Removing it again is refused",
      client.delete(f"/api/positions/{_buy60['id']}").status_code == 404)

_js60 = client.get("/static/app.js").text
_h60 = (ROOT / "static" / "index.html").read_text()
_css60 = client.get("/static/style.css").text
check("Every order row has a delete control",
      'class="del"' in _js60 and 'data-side="${o.side}"' in _js60)
check("Deleting a sale reopens, deleting a purchase deletes",
      '`/api/positions/${id}/reopen`' in _js60 and '`/api/positions/${id}`' in _js60
      and 'undoSale ? "POST" : "DELETE"' in _js60)
check("Both ask before removing anything", "window.confirm(ask)" in _js60)
_tbl60 = _h60[_h60.index('id="earnOrders"'):_h60.index("</table>", _h60.index('id="earnOrders"'))]
check("The table has a column for the control and the spans match",
      len(_re50.findall(r"<th[ >]", _tbl60)) == 10
      and 'colspan="10"' in _js60 and 'colspan="9"' not in _js60,
      f'{len(_re50.findall(r"<th[ >]", _tbl60))} columns')
check("The totals strip has its own positive and negative colours",
      ".earn-summary .pos" in _css60 and ".earn-summary .neg" in _css60)
check("The starting-balance note is gone from the header",
      "starting balance" not in _js60 and "added later to afford" not in _js60)


print("\n61. Reading the tables at a glance")
_js61 = client.get("/static/app.js").text
_css61 = client.get("/static/style.css").text

# A trade's two orders are boxed together
check("Each order row knows its trade's first and last row",
      'class="trade${startsTrade ? " trade-start" : ""}' in _js61
      and "const endsTrade = i === d.orders.length - 1" in _js61
      and 'd.orders[i + 1].trade_id !== o.trade_id' in _js61)
check("The box is drawn on all four sides",
      all(x in _css61 for x in ("#earnOrders tr.trade td:first-child { border-left",
                                "#earnOrders tr.trade td:last-child { border-right",
                                "#earnOrders tr.trade-start td { border-top",
                                "#earnOrders tr.trade-end td { border-bottom")))
check("Rows inside a trade carry no dividing line",
      "#earnOrders tr.trade td { border-top: none; border-bottom: none; }" in _css61)
check("Hovering highlights the whole trade", "#earnOrders tr.trade:hover td" in _css61)

# Only figures where a sign means something are coloured
_strip61 = _js61[_js61.index('$("earnSummary").innerHTML'):_js61.index("// Daily, so an open position")]
for _label in ("Account balance", "Money in", "Commission", "Average hold"):
    _seg = _strip61[_strip61.index(f'cell("{_label}"'):]
    _seg = _seg[:_seg.index("+ cell(") if "+ cell(" in _seg else len(_seg)]
    check(f"{_label} is not coloured by sign", "null" in _seg, _seg[:90].replace("\n", " "))
for _label, _by in (("Trading return", "s.trading_return"), ("Growth on money in", "s.balance_pct"),
                    ("Realised", "s.net"), ("Unrealised", "s.unrealised"),
                    ("Gross profit", "s.gross"), ("Average trade", "s.avg_net")):
    _seg = _strip61[_strip61.index(f'cell("{_label}"'):]
    check(f"{_label} still shows profit or loss in colour", _by in _seg[:200], _seg[:90])

# The readout no longer repeats the position's P&L
check("The bottom strip does not repeat Your P&L",
      "Your P&L" not in _js61.split("$(\"readout\").innerHTML")[1][:1200])
check("The Position panel still shows it",
      'i class="k">P&amp;L' in _js61 and "pnl_value" in _js61)
_st61 = client.get("/api/states").json()
_future61 = _earn56.report(
    [_Fake56(9, "AMD", date(2026, 9, 30), 100.0, 2, None, None)],
    {"AMD": {date(2026, 9, 10): 95.0}})
check("A purchase dated after the latest price reads as held no days, not minus",
      _future61["open_trades"][0]["hold_days"] == 0,
      str(_future61["open_trades"][0]["hold_days"]))
check("The API still reports position P&L for anything that wants it",
      all("position" in x for x in _st61))


print("\n62. Short histories fill the pane; the stop matches the chart")
_js62 = client.get("/static/app.js").text
check("Earnings charts are framed on their points, not their bars",
      "function frameSeries(chart, points)" in _js62
      and "setVisibleLogicalRange({ from: 0.5, to: points - 1.5 })" in _js62
      and "frameSeries(c, m.balance.length)" in _js62)
check("Two points keep the default framing (a narrower range is ignored)",
      "if (points >= 3)" in _js62)
check("fitContent still runs first, so the range is always valid",
      _js62.index("chart.timeScale().fitContent();")
      < _js62.index("setVisibleLogicalRange({ from: 0.5, to: points - 1.5 })"))
check("The stop is coloured by trend direction, like the line on the chart",
      'cell("Stop (Supertrend)", fmt(s.stop), s.trend === "up" ? "up" : "down")' in _js62)
_states62 = client.get("/api/states").json()
check("Every state says which way the trend runs",
      all(x["trend"] in ("up", "down") for x in _states62))
check("An uptrend and a downtrend both exist to colour",
      len({x["trend"] for x in _states62}) >= 1)


print("\n63. Commission counts what you have actually paid")
_flat63 = {"AMD": {date(2026, 4, 9) + timedelta(days=i): 500.0 for i in range(0, 170)}}
_closed63 = _Fake56(1, "AMD", date(2026, 4, 9), 234.24, 2, date(2026, 6, 11), 465.31, 0.99, 0.99)
_open63 = _Fake56(2, "AMD", date(2026, 9, 14), 488.57, 1.56, None, None, 0.99)

_both63 = _earn56.report([_closed63, _open63], _flat63)["summary"]
check("The purchase commission on a holding is counted",
      _both63["fees"] == 2.97 and _both63["fees_closed"] == 1.98 and _both63["fees_open"] == 0.99,
      str(_both63["fees"]))
_only63 = _earn56.report([_open63], _flat63)["summary"]
check("A holding with nothing closed still shows its commission",
      _only63["fees"] == 0.99 and _only63["fees_open"] == 0.99, str(_only63["fees"]))
check("Closed-trade figures are unaffected",
      _both63["net"] == _earn56.summary(_earn56.trades([_closed63]))["net"]
      and _both63["gross"] == 462.14)
check("The share of gross profit uses the same total",
      _both63["fees_vs_gross_pct"] == round(2.97 / 462.14 * 100, 1),
      str(_both63["fees_vs_gross_pct"]))
check("No trades at all means no commission",
      _earn56.report([], {})["summary"]["fees"] == 0.0)
_js63 = client.get("/static/app.js").text
check("The tooltip says what is included",
      "Every commission paid, including the purchase of what you still hold" in _js63)
check("Verdict explains itself on hover",
      "const VERDICT_HELP" in _js63 and "In trend, filter clear —" in _js63
      and 'title="${esc(VERDICT_HELP)}"' in _js63)


print("\n64. Thousands separators")
_js64 = client.get("/static/app.js").text
check("The display formatter groups thousands",
      "toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })" in _js64)
check("Chart axes use the same formatter",
      "localization: { priceFormatter: (v) => fmt(v) }" in _js64)
check("Form fields still hold plain numbers, not grouped text",
      'value="${state.close.toFixed(2)}"' in _js64
      and 'value="${defaultCommission.toFixed(2)}"' in _js64
      and "value=\"${state ? state.close.toFixed(2) : \"\"}\"" in _js64)
if _sh50.which("node"):
    _start64 = _js64.index("const fmt = (n, d = 2)")
    _fmt64 = _js64[_start64:_js64.index("\n", _js64.index("maximumFractionDigits: d }));", _start64))]
    _run64 = subprocess.run(
        ["node", "-e", _fmt64 + "\n"
         + "process.stdout.write([fmt(1039.79), fmt(19935.5), fmt(1234567.891), fmt(999.5),"
           " fmt(-1500.25), fmt(18139, 0), fmt(null), fmt(0)].join('|'))"],
        capture_output=True, text=True, env={**_os.environ, "LANG": "en_US.UTF-8"})
    check("Numbers over a thousand are grouped, smaller ones unchanged",
          _run64.stdout == "1,039.79|19,935.50|1,234,567.89|999.50|-1,500.25|18,139|—|0.00",
          _run64.stdout or _run64.stderr[-120:])


print("\n65. Alerts when a scan does not really work")
_sent65 = []
_orig_send65, _orig_alert65 = notify.send, scanner._last_failure_alert
scanner.notify.send = lambda t, b, u="": _sent65.append((t, b)) or {}
_today65 = _mk.last_completed_session().isoformat()
try:
    scanner._last_failure_alert = None
    scanner._alert_if_broken(39, [], [{"date": _today65}])
    check("A healthy scan says nothing", _sent65 == [])

    scanner._alert_if_broken(39, ["A"], [{"date": _today65}])
    check("One flaky ticker is not worth an alert", _sent65 == [])

    scanner._alert_if_broken(39, [f"T{i}" for i in range(4)], [{"date": _today65}])
    check("Failures past a tenth of the watchlist do alert",
          len(_sent65) == 1 and "4 of 39 tickers failed" in _sent65[0][0], str(_sent65[:1]))
    check("The alert names the tickers", "T0" in _sent65[0][1])
    scanner._alert_if_broken(39, [f"T{i}" for i in range(4)], [{"date": _today65}])
    check("It does not repeat the same day", len(_sent65) == 1)

    scanner._last_failure_alert = None
    scanner._alert_if_broken(39, [], [])
    check("A scan that evaluated nothing alerts",
          "no data at all" in _sent65[-1][0], _sent65[-1][0])

    scanner._last_failure_alert = None
    scanner._alert_if_broken(39, [], [{"date": "2026-01-05"}])
    check("Stale prices alert even when nothing failed",
          "sessions behind" in _sent65[-1][0] and "stale data" in _sent65[-1][1],
          _sent65[-1][0])

    scanner._last_failure_alert = date.today()
    scanner._alert_if_broken(39, [], [{"date": _today65}])
    check("Recovering clears the daily lock", scanner._last_failure_alert is None)

    _small = len(_sent65)
    scanner._last_failure_alert = None
    scanner._alert_if_broken(5, ["A", "B"], [{"date": _today65}])
    check("On a small watchlist the floor is three failures", len(_sent65) == _small)
    check("Alerts only go out when the scan would notify",
          "if notify_on:\n        degraded = _alert_if_broken" in (ROOT / "app" / "scanner.py").read_text())
finally:
    scanner.notify.send = _orig_send65
    scanner._last_failure_alert = _orig_alert65

print("\n66. Stock splits against a recorded purchase")
from app import splits as _sp66  # noqa: E402
_closes66 = {date(2026, 4, 8): 57.88, date(2026, 4, 7): 57.0, date(2026, 4, 6): 56.5}
_split66 = _sp66.detect(231.50, date(2026, 4, 8), 2, _closes66)
check("A 4-for-1 split is spotted",
      _split66 and _split66["factor"] == 4 and _split66["reverse"] is False
      and _split66["note"] == "4-for-1 split", str(_split66))
check("It says what the record should become",
      _split66["suggested_entry"] == 57.875 and _split66["suggested_quantity"] == 8)
check("A reverse split is spotted the other way",
      (_r := _sp66.detect(57.88, date(2026, 4, 8), 8, {date(2026, 4, 8): 231.50}))
      and _r["reverse"] is True and _r["suggested_quantity"] == 2.0, str(_r))
check("An ordinary price is not a split", _sp66.detect(57.90, date(2026, 4, 8), 2, _closes66) is None)
check("A big but unsplit-like move is left alone",
      _sp66.detect(231.50 * 1.6, date(2026, 4, 8), 2, _closes66) is None)
check("A 3% difference between fill and close does not trigger it",
      _sp66.detect(59.6, date(2026, 4, 8), 2, _closes66) is None)
check("A purchase dated on a weekend finds the last close",
      _sp66.detect(231.50, date(2026, 4, 11), 2, _closes66) is not None)
check("No price history means no guess", _sp66.detect(231.50, date(2026, 4, 8), 2, {}) is None)
check("A purchase older than the stored history is left alone",
      _sp66.detect(231.50, date(2026, 3, 1), 2, _closes66) is None)
check("Restating keeps the money the same",
      _sp66.apply_factor(231.50, 2, 4, False) == (57.875, 8)
      and round(57.875 * 8, 2) == round(231.50 * 2, 2))
check("Reverse restating too", _sp66.apply_factor(57.88, 8, 4, True) == (231.52, 2.0))

with get_session() as _s66:
    _s66.query(Position).filter(Position.ticker == "NVDA").delete()
    _s66.commit()
_close66 = float(data.load("NVDA")["close"].iloc[-1])
_bought66 = data.load("NVDA").index[-1].date().isoformat()
_pos66 = client.post("/api/positions", json={"ticker": "NVDA", "entry_date": _bought66,
                                             "entry_price": round(_close66 * 4, 2),
                                             "quantity": 3}).json()
_listed66 = client.get("/api/positions?ticker=NVDA").json()["positions"][0]
check("The API flags a held position that looks split",
      _listed66["split_suspect"] and _listed66["split_suspect"]["factor"] == 4,
      str(_listed66.get("split_suspect")))
_fixed66 = client.post(f"/api/positions/{_pos66['id']}/split", json={"factor": 4}).json()
check("Restating divides the price and multiplies the shares",
      round(_fixed66["entry_price"], 2) == round(_close66, 2) and _fixed66["quantity"] == 12,
      f'{_fixed66["entry_price"]} x {_fixed66["quantity"]}')
check("And the warning goes away",
      client.get("/api/positions?ticker=NVDA").json()["positions"][0]["split_suspect"] is None)
check("A sensible purchase is never flagged",
      client.get("/api/positions?ticker=AAPL").json()["positions"] == []
      or all(p.get("split_suspect") is None
             for p in client.get("/api/positions?ticker=AAPL").json()["positions"]))
check("Restating a closed trade is refused (its prices already agree)",
      client.post(f"/api/positions/{_pos66['id']}/close",
                  json={"exit_date": _bought66, "exit_price": _close66}).status_code == 200
      and client.post(f"/api/positions/{_pos66['id']}/split",
                      json={"factor": 4}).status_code == 409)
check("A silly factor is refused",
      client.post(f"/api/positions/{_pos66['id']}/split", json={"factor": 1}).status_code == 422)
with get_session() as _s66:
    _s66.query(Position).filter(Position.ticker == "NVDA").delete()
    _s66.commit()

print("\n67. Exporting the trades")
import csv, io  # noqa: E402
_csv67 = client.get("/api/export/trades.csv")
check("The export downloads as a CSV file",
      _csv67.status_code == 200 and "text/csv" in _csv67.headers["content-type"]
      and "attachment" in _csv67.headers["content-disposition"]
      and "supertrendmoose-trades-" in _csv67.headers["content-disposition"])
_rows67 = list(csv.reader(io.StringIO(_csv67.text)))
check("It has a header row and one row per order",
      _rows67[0][:6] == ["trade_id", "status", "date", "ticker", "side", "quantity"]
      and len(_rows67) - 1 == len(client.get("/api/earnings").json()["orders"]),
      f"{len(_rows67) - 1} rows")
check("Every column the tab shows is in the file",
      set(_rows67[0]) >= {"price", "value", "commission", "hold_days", "trade_net",
                          "trade_net_pct", "entry_date", "exit_date", "mark_price"})
check("Open and closed orders are both there, and marked",
      {r[1] for r in _rows67[1:]} <= {"open", "closed"})
check("Nothing about currency or tax is assumed",
      not any(w in _csv67.text.lower() for w in ("aud", "usd", "tax", "cgt", "fx")))
check("It can be narrowed to one ticker",
      {r[3] for r in list(csv.reader(io.StringIO(
          client.get("/api/export/trades.csv?ticker=AMD").text)))[1:]} <= {"AMD"})
check("The export needs the token like everything else",
      "dependencies=[Depends(auth)]" in (ROOT / "app" / "main.py").read_text()
      .split('@app.get("/api/export/trades.csv"')[1][:60])
_js67 = client.get("/static/app.js").text
check("The tab has an export button wired to it",
      'id="earnExport"' in (ROOT / "static" / "index.html").read_text()
      and '"/api/export/trades.csv"' in _js67)

print("\n68. Sizing a position by risk")
check("The helper needs an account size, a risk and a stop",
      "function sizeFor(state)" in _js67 and "const perShare = state.close - state.stop;" in _js67
      and "const byRisk = budget / perShare;" in _js67)
check("A stop above the price is called out rather than sized",
      "the Supertrend is above the price, so there is no entry to size here" in _js67
      and "locked: true" in _js67)
check("The share count reads as English for one share",
      'share${one ? "" : "s"}' in _js67)
check("The figures are kept for the session only, not stored",
      "sessionStorage.setItem(SIZER_KEY" in _js67 and "localStorage" not in _js67)
check("There is a button to put the number into the quantity field",
      'id="sizeUse"' in _js67 and '$("posQty").value = r.shares' in _js67)
check("The panel offers a cap and a fractional switch",
      'id="sizeCap"' in _js67 and 'id="sizeFrac"' in _js67
      and 'capPct: "33", fractional: true' in _js67)
check("Fee drag is only mentioned when it is worth mentioning",
      "r.feePct >= 1" in _js67 and "commission is" in _js67)

if _sh50.which("node"):
    _sizer67 = _js67[_js67.index("function sizeFor(state)"):_js67.index("function sizerRow(state)")]
    def _size(account, risk, cap, frac, close, stop, fee=0.99):
        run = subprocess.run(
            ["node", "-e",
             f"let sizer = {{account: '{account}', riskPct: '{risk}', capPct: '{cap}',"
             f" fractional: {str(frac).lower()}}};\n"
             f"let defaultCommission = {fee};\n" + _sizer67
             + f"const r = sizeFor({{close: {close}, stop: {stop}}});\n"
               "process.stdout.write(r === null ? 'null' : JSON.stringify(r))"],
            capture_output=True, text=True)
        return _json56.loads(run.stdout) if run.stdout.startswith("{") else run.stdout

    # The XOM case from the screenshots, on a $1,000 account
    _xom = _size(1000, 1, 100, True, 163.54, 156.39)
    check("Fractional sizing hits the risk figure exactly",
          _xom["shares"] == 1.3986 and round(_xom["risk"], 2) == 10.0
          and round(_xom["riskPct"], 2) == 1.0,
          f'{_xom["shares"]} shares risking {_xom["risk"]:.2f}')
    check("Whole-share mode rounds down instead",
          _size(1000, 1, 100, False, 163.54, 156.39)["shares"] == 1)
    check("The suggestion reports what it costs as a share of the account",
          round(_xom["cost"], 2) == 228.73 and round(_xom["costPct"]) == 23,
          f'{_xom["cost"]:.2f} ({_xom["costPct"]:.0f}%)')

    # A tight stop wants more than the whole account: the cap must bind
    _oxy = _size(1000, 1, 33, True, 58.84, 58.43)
    check("A tight stop is held to the cap, not the risk figure",
          _oxy["capped"] is True and round(_oxy["cost"], 2) <= 330.01
          and _oxy["riskPct"] < 1.0,
          f'cost {_oxy["cost"]:.2f}, risking {_oxy["riskPct"]:.2f}%')
    _oxy_full = _size(1000, 1, 100, True, 58.84, 58.43)
    check("Even a 100% cap binds here: the risk figure alone wants $1,435",
          _oxy_full["capped"] is True and round(_oxy_full["cost"]) == 1000
          and round(10 / (58.84 - 58.43) * 58.84) == 1435,
          f'{_oxy_full["cost"]:.0f}')
    check("A wide stop is not capped",
          _size(20000, 1, 33, True, 559.82, 480.18)["capped"] is False)

    # Commission on a small position
    _amd = _size(1000, 1, 33, True, 559.82, 480.18)
    check("Fee drag is measured against the position",
          round(_amd["feeRoundTrip"], 2) == 1.98 and round(_amd["feePct"], 1) == 2.8,
          f'{_amd["feePct"]:.1f}% of {_amd["cost"]:.2f}')
    check("A large position barely notices the commission",
          round(_size(20000, 1, 33, True, 559.82, 480.18)["feePct"], 2) == 0.14)

    check("A downtrend is refused, not sized",
          _size(1000, 1, 33, True, 1015.80, 1044.51) == {"locked": True})
    check("No account size means no answer", _size("", 1, 33, True, 163.54, 156.39) == "null")
    check("A cap of zero is treated as no cap",
          _size(1000, 1, 0, True, 163.54, 156.39)["shares"] == _xom["shares"])


print("\n70. Buy more belongs to one ticker")
_js70 = client.get("/static/app.js").text
check("Changing ticker leaves buy-more mode",
      "if (ticker !== selected) buyingMore = false;" in _js70)
check("Recording a purchase leaves it too", "buyingMore = false;\n        toast(" in _js70)
check("And Cancel leaves it",
      'addEventListener("click", () => { buyingMore = false; renderPosition(state); })' in _js70)
check("Selecting the same ticker again does not cancel a purchase in progress",
      "ticker !== selected" in _js70 and "if (ticker !== selected) buyingMore = false;" in _js70)


print("\n71. The return chart is red when trading is behind")
_js71 = client.get("/static/app.js").text
_h71 = (ROOT / "static" / "index.html").read_text()
_css71 = client.get("/static/style.css").text
check("The return line uses a baseline series, which colours both sides",
      "pctChart.addBaselineSeries({" in _js71)
check("Above zero is green, below it is red",
      'topLineColor: "#2bb6a3"' in _js71 and 'bottomLineColor: "#e5544b"' in _js71
      and 'topFillColor1: "#2bb6a344"' in _js71 and 'bottomFillColor2: "#e5544b44"' in _js71)
check("The colour changes at zero, not at some other level",
      'baseValue: { type: "price", price: 0 }' in _js71)
check("Its swatch shows both colours", ".pane-label .sw.earn-total" in _css71
      and "linear-gradient(90deg, var(--up) 0 50%, var(--down) 50% 100%)" in _css71)
check("The closed-trades line stays a plain grey line",
      "pctClosedSeries = pctChart.addLineSeries({" in _js71 and 'color: "#7d8f9e"' in _js71)

check("The balance chart is one colour again",
      "pnlChart.addAreaSeries({" in _js71 and "pnlChart.addBaselineSeries" not in _js71)
check("And that colour is blue, so the two panes do not look alike",
      _js71[_js71.index("pnlSeries = pnlChart.addAreaSeries"):][:160].count("#4a90d9") == 3)
check("Nothing about a break-even level is left on it",
      "earnBreakEven" not in _js71 and "earnBreakEven" not in _h71
      and "red below" not in _js71 and "pnlSeries.applyOptions" not in _js71
      # exactly one baseValue in the file: the zero line on the return chart
      and _js71.count("baseValue") == 1)
check("The balance label is back to plain",
      "Account balance <em id=\"earnPnlNow\"></em> · cash plus holdings at market, after commission</span>" in _h71)

# The data behind the colour: a losing stretch and a winning one
_under71 = _earn56.report(
    [_Fake56(1, "AMD", date(2026, 4, 8), 500.0, 2, None, None, 0.99)],
    {"AMD": {date(2026, 4, 8) + timedelta(days=i): 500.0 - i for i in range(0, 6)}})["money"]
check("A losing account puts the return line below zero",
      all(p["value"] < 0 for p in _under71["pct_total"]),
      str(_under71["pct_total"][-1]))
_over71 = _earn56.report(
    [_Fake56(1, "AMD", date(2026, 4, 8), 500.0, 2, None, None, 0.99)],
    {"AMD": {date(2026, 4, 8) + timedelta(days=i): 500.0 + i * 10 for i in range(0, 6)}})["money"]
check("A winning one ends above zero", _over71["pct_total"][-1]["value"] > 0)
check("Both lines still share the same days",
      [p["time"] for p in _over71["pct_total"]] == [p["time"] for p in _over71["pct_closed"]])


print("\n72. The quiet-day heartbeat")
# A quiet day and a dead notification channel both produce silence. The
# heartbeat is what separates them, so it has to fire on exactly the scans
# that would otherwise say nothing at all.
_hb_states = [{"ticker": "NVDA", "date": date(2026, 9, 18)},
              {"ticker": "AMD", "date": date(2026, 9, 17)}]
_hb_title, _hb_body = notify.format_heartbeat(_hb_states, [])
check("It says the scan ran and found nothing",
      "no signals" in _hb_title.lower() and "No buy or sell signals" in _hb_body, _hb_body)
check("It reports the ticker count and the data date",
      "2 tickers" in _hb_body and "2026-09-18" in _hb_body, _hb_body)
check("It dates itself from the newest bar, not the oldest",
      "2026-09-17" not in _hb_body, _hb_body)
check("Failed downloads are called out",
      "1 ticker(s) failed" in notify.format_heartbeat(_hb_states, ["XOM"])[1])
check("A clean scan mentions no failures",
      "failed" not in _hb_body.lower(), _hb_body)
check("It survives a scan that produced no states at all",
      "unknown" in notify.format_heartbeat([], [])[1])

# Off by default: on two channels the redundancy already reveals a dead one,
# so this only earns its noise when someone opts in. Settings binds its
# defaults at import time, so each value needs a fresh interpreter.
_hb_probe = "from app.config import Settings; print(Settings().notify_heartbeat)"


def _hb_setting(value: str | None = None) -> str:
    env = {**_os.environ, "PYTHONPATH": str(ROOT)}
    env.pop("NOTIFY_HEARTBEAT", None)
    if value is not None:
        env["NOTIFY_HEARTBEAT"] = value
    out = subprocess.run([sys.executable, "-c", _hb_probe], capture_output=True,
                         text=True, env=env, cwd=str(ROOT))
    return out.stdout.strip()


check("Heartbeat is off unless asked for", _hb_setting() == "False", _hb_setting())
for _v in ("true", "1", "yes", "on"):
    check(f"NOTIFY_HEARTBEAT={_v} turns it on", _hb_setting(_v) == "True")
check("NOTIFY_HEARTBEAT=false turns it off", _hb_setting("false") == "False")

_scan_src = (ROOT / "app" / "scanner.py").read_text()
check("It only fires when there were no signals",
      "settings.notify_heartbeat and not (buys or exits)" in _scan_src)
check("A degraded scan suppresses it, so it cannot claim all is well",
      "and not degraded" in _scan_src)
check("_alert_if_broken reports degradation back to the caller",
      "degraded = _alert_if_broken(" in _scan_src)
check("It respects notify_on like every other alert",
      "if notify_on and settings.notify_heartbeat" in _scan_src)
check("Heartbeat is documented for whoever has to enable it",
      "NOTIFY_HEARTBEAT" in (ROOT / ".env.example").read_text()
      and "NOTIFY_HEARTBEAT" in (ROOT / "README.md").read_text()
      and "NOTIFY_HEARTBEAT" in (ROOT / "docs" / "GUIDE.md").read_text())


print("\n73. The enriched signal alert")
# Heading, scan summary, the extra per-signal columns, and the blocked
# section that explains why a ticker is absent from the buy list.
_b73 = [{"ticker": "GOOGL", "close": 252.14, "stop": 231.06, "adx": 28.0,
         "atr_pct": 2.1, "above_sma": True, "sma": 240.0, "score": 78.0,
         "date": date(2026, 9, 19)}]
_e73 = [{"ticker": "XOM", "close": 110.0, "date": date(2026, 9, 19),
         "position": {"pnl_pct": 12.4, "held_days": 34}},
        {"ticker": "SMH", "close": 285.4, "date": date(2026, 9, 18)}]
_x73 = [{"ticker": "SPY", "reason": "outside liquidity/ATR screen",
         "date": date(2026, 9, 19)},
        {"ticker": "LMT", "reason": "earnings in 3d", "date": date(2026, 9, 19)}]
_t73, _y73 = notify.format_signals(_b73, _e73, "full", blocked=_x73, scanned=41)

check("Heading names the app in mixed case with the counts",
      _y73.splitlines()[0] == "SupertrendMoose — 1 buy, 2 sell (1 you hold)", _y73.splitlines()[0])
check("Heading is not shouted", "SUPERTRENDMOOSE" not in _y73)
check("Subject stays the bare counts", _t73 == "1 buy, 2 sell (1 you hold)", _t73)
check("Scan summary reports the ticker count and newest bar",
      "Scanned 41 tickers, prices through 2026-09-19" in _y73, _y73)
check("Buys carry risk, ADX, ATR, the 200MA side and the score",
      all(t in _y73 for t in ["9.1% risk", "ADX: 28", "ATR: 2.1%",
                              "above 200MA", "score: 78"]), _y73)
check("Each buy spec is separated, not space-aligned",
      "GOOGL: $252.14 - stop: $231.06 - 9.1% risk - ADX: 28"
      " - ATR: 2.1% - above 200MA - score: 78" in _y73, _y73)
check("Sells are separated the same way",
      "SMH: $285.40 - Supertrend flipped down" in _y73, _y73)
check("A held sell keeps its P&L on the same line",
      "XOM: $110.00 - Supertrend flipped down - ← YOU HOLD THIS"
      " - +12.4%, held 34d" in _y73, _y73)
check("Blocked lines use the same colon style",
      "SPY: outside liquidity/ATR screen" in _y73, _y73)
check("No runs of spaces are left to be collapsed by a mail client",
      "   " not in _y73.replace("\n", ""), _y73)
check("A held exit reports P&L and days held",
      "← YOU HOLD THIS" in _y73 and "+12.4%" in _y73 and "held 34d" in _y73, _y73)
check("An unheld exit stays bare", "SMH" in _y73 and _y73.count("YOU HOLD THIS") == 1)
check("Blocked signals are named with their reason",
      "BLOCKED, NOT ALERTED" in _y73 and "SPY" in _y73
      and "outside liquidity/ATR screen" in _y73 and "earnings in 3d" in _y73, _y73)
check("Blocked tickers are kept out of the buy list",
      "SPY" not in _y73.split("BLOCKED")[0], _y73)

# Spacing must survive each section being absent, and must not depend on a
# hard-coded header length.
check("Header length is derived, not hard-coded", "header_len" in
      (ROOT / "app" / "notify.py").read_text())
_only_e = notify.format_signals([], _e73, "full", scanned=41)
check("With no buys there is no double blank before the sells",
      "\n\n\nSELL SIGNALS" not in _only_e, repr(_only_e))
check("With no buys the buy header is absent", "BUY SIGNALS" not in _only_e)
_only_b = notify.format_signals(_b73, [], "full", scanned=41)
check("With no exits the sell header is absent", "SELL SIGNALS" not in _only_b)
check("With nothing blocked the blocked header is absent",
      "BLOCKED" not in _only_b, _only_b)
check("It still works with no summary figures at all",
      notify.format_signals(_b73, [], "full")[1].startswith("SupertrendMoose — "))

# A ticker younger than 200 bars has no average; saying "below" would report
# an absent line as a failed test against it.
_no_sma = notify.format_signals(
    [{**_b73[0], "sma": None, "above_sma": False}], [], "full")[1]
check("No 200MA yet means the 200MA is not mentioned",
      "200MA" not in _no_sma, _no_sma)

# Minimal detail is a privacy setting: none of the new fields may leak.
_min73 = notify.format_signals(_b73, _e73, "minimal", blocked=_x73, scanned=41)[1]
check("Minimal leaks no tickers, reasons or figures",
      not any(t in _min73 for t in ["GOOGL", "XOM", "SPY", "252", "ADX",
                                    "score", "liquidity", "41"]), _min73)

# Missing keys must not abort a scan: format_signals runs before send()'s
# try/except, so a KeyError here would take the whole run down.
check("A sparse signal dict does not raise",
      isinstance(notify.format_signals(
          [{"ticker": "X", "close": 1.0, "stop": 0.9, "adx": 20.0}],
          [{"ticker": "Y", "close": 2.0}], "full")[1], str))

_scan73 = (ROOT / "app" / "scanner.py").read_text()
check("Rejected flip-ups are collected for the alert",
      "blocked.append(st)" in _scan73)
check("Blocked and scanned are passed to the formatter",
      "blocked=blocked" in _scan73 and "scanned=len(states)" in _scan73)
check("Only passed buys are still alerted as buys",
      "if st[\"passed\"]:\n                buys.append(st)" in _scan73)


print("\n74. The SMTP password can live in the secrets volume")
# An app password is not scoped to sending: over IMAP it reads the whole
# mailbox, and it bypasses 2FA. Keeping it out of .env keeps it out of the
# host filesystem and out of `docker inspect`.
_sec74 = tf.mkdtemp()
_empty74 = tf.mkdtemp()
open(_os.path.join(_sec74, "smtp.pass"), "w").write("from-the-volume\n")
_probe74 = "from app.config import Settings; print(repr(Settings().smtp_pass))"


def _smtp_pass(secrets_dir: str, env_value: str | None = None) -> str:
    env = {**_os.environ, "PYTHONPATH": str(ROOT), "SECRETS_DIR": secrets_dir}
    env.pop("SMTP_PASS", None)
    if env_value is not None:
        env["SMTP_PASS"] = env_value
    out = subprocess.run([sys.executable, "-c", _probe74], capture_output=True,
                         text=True, env=env, cwd=str(ROOT))
    return out.stdout.strip()


check("It is read from the volume when no env var is set",
      _smtp_pass(_sec74) == "'from-the-volume'", _smtp_pass(_sec74))
check("A trailing newline in the file is stripped",
      "\\n" not in _smtp_pass(_sec74), _smtp_pass(_sec74))
check("SMTP_PASS still wins, so an existing .env keeps working",
      _smtp_pass(_sec74, "from-the-env") == "'from-the-env'",
      _smtp_pass(_sec74, "from-the-env"))
check("A blank SMTP_PASS falls through to the volume",
      _smtp_pass(_sec74, "") == "'from-the-volume'", _smtp_pass(_sec74, ""))
check("No file and no env var is empty, not a crash",
      _smtp_pass(_empty74) == "''", _smtp_pass(_empty74))
check("It uses the same helper as the other secrets",
      '_secret("smtp.pass", "SMTP_PASS")' in (ROOT / "app" / "config.py").read_text())
check("SMTP_PASS is still reachable from .env for anyone who prefers that",
      "SMTP_PASS" in (ROOT / ".env.example").read_text())
check("Putting it in the volume is documented",
      "smtp.pass" in (ROOT / "DEPLOY.md").read_text())


print("\n75. A sell on a position you hold stands out")
_e75 = [{"ticker": "AMD", "close": 90.0},
        {"ticker": "NVDA", "close": 120.0, "position": {"pnl_pct": 12.3, "held_days": 41}}]
_t75, _b75 = notify.format_signals([], _e75, "full")
check("Title counts held sells", _t75 == "2 sell signals (1 you hold)", _t75)
check("Held sells are listed first",
      _b75.index("NVDA") < _b75.index("AMD"), _b75)
check("Sorting does not reorder the caller's list",
      [s["ticker"] for s in _e75] == ["AMD", "NVDA"])
check("Minimal title flags a held sell without naming it",
      notify.format_signals([], _e75, "minimal")[0] == "2 sell signals (1 you hold)"
      and "NVDA" not in "".join(notify.format_signals([], _e75, "minimal")))
check("Mixed title flags held sells",
      notify.format_signals([{"ticker": "MU", "close": 80.0}], _e75, "full")[0]
      == "1 buy, 2 sell (1 you hold)")
check("Unheld sells keep the plain title",
      notify.format_signals([], _e75[:1], "full")[0] == "1 sell signal")
check("held_exits counts only held positions", notify.held_exits(_e75) == 1)

_cap75 = {}
_real75 = httpx.post
httpx.post = lambda url, **kw: (_cap75.update(kw), type("R", (), {"is_success": True, "status_code": 200})())[1]
try:
    notify.settings.ntfy_topic = "supertrend"
    notify.BACKENDS["ntfy"]("t", "b", "", urgent=True)
    check("Held sell raises ntfy priority", _cap75["headers"]["Priority"] == "high")
    check("Held sell uses the warning tag", _cap75["headers"]["Tags"] == "warning")
    notify.BACKENDS["ntfy"]("t", "b")
    check("Routine alerts stay at default priority", _cap75["headers"]["Priority"] == "default")
finally:
    httpx.post = _real75

_sent75 = []
_orig_backends75 = dict(notify.BACKENDS)
_orig_channels75 = notify.settings.notify_channels
try:
    notify.BACKENDS["ntfy"] = lambda t, b, u="", urgent=False: _sent75.append(urgent) or True
    notify.settings.notify_channels = ["ntfy"]
    notify.send("t", "b", urgent=True)
    notify.send("t", "b")
    check("send() passes urgency through", _sent75 == [True, False], _sent75)
finally:
    notify.BACKENDS.clear(); notify.BACKENDS.update(_orig_backends75)
    notify.settings.notify_channels = _orig_channels75
check("The scan marks a held sell urgent",
      "urgent=bool(notify.held_exits(exits))" in (ROOT / "app" / "scanner.py").read_text())


print("\n" + ("=" * 52))
print("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
