# SupertrendMoose

**Big antlers, sharper entries.**

SupertrendMoose is a containerised FastAPI application for scanning US equities for Supertrend swing-trading signals. It tracks a watchlist, computes Supertrend, ADX and the 200-day average with Wilder's smoothing, alerts you after the US close when a signal fires, and serves TradingView-style charts so you can make your own call before you buy.

Built for long-only daily-bar swing trading. It never places orders.

---

## ✨ Features

- **Containerised Deployment:** Everything runs in Docker with a single `docker compose up` command.
- **Self-generating Credentials:** Passwords and the dashboard token are created inside the containers on first start. Nothing to paste, nothing to commit.
- **Daily Scanning:** Runs automatically 60 minutes after every US close, wherever you are and whatever daylight saving is doing.
- **Push Alerts:** A bundled, locked-down ntfy server sends buy and sell signals to your phone. Telegram, Discord and email are also supported.
- **Interactive Charts:** Candles, Supertrend bands, 200 SMA, volume, earnings badges, an ADX subplot and an optional MACD pane.
- **Per-ticker Filters:** Five filter profiles, assigned per ticker rather than as one blanket rule.
- **Position Tracking:** Record what you actually bought, with live P&L, risk-to-stop, commission and a risk-based position sizer.
- **Backtesting & Tuning:** Every profile against buy-and-hold per ticker, plus walk-forward parameter validation.
- **Live Pricing:** Fetches daily bars from Yahoo Finance via yfinance (with retry/backoff logic).
- **41-ticker Universe:** Energy, mega-cap tech, semis, high-ATR names, industrials, financials, healthcare, defence and index ETFs, seeded on first start.

---

## 💡 Requirements

- Docker (20.10+)
- Docker Compose v2 (the `docker compose` command)
- Docker Engine or Docker Desktop must be running
- Git, to clone the repository

---

## 🚀 Installation & Usage

- **Clone the repository**

```
git clone https://github.com/theblackmoose/supertrendMoose.git
```

```
cd supertrendMoose
```

  Note: For Windows users, you will be required to first install [Git for Windows](https://git-scm.com/downloads/win) to be able to run `git clone`.

- **Start Docker**

  Ensure Docker Engine or Docker Desktop is running on your machine.

- **Choose where it is reached** (optional)

  With no settings at all, it is reachable from this machine only, at <http://localhost:19080>. To open it to other devices on your network, give it this machine's IP address:

```
cp .env.example .env
```

```
# then set in .env, for example:
#   HOST_IP=192.168.1.50
#   TZ=Europe/London
#   DEFAULT_COMMISSION=0.99
```

  See [Choosing the address](#-choosing-the-address) for what that one setting controls.

- **Start with Docker Compose**

```
docker compose up -d --build
```

  * **What happens:**
    + The Python environment and dependencies are installed inside the Docker image via the Dockerfile and requirements.txt.
    + The ntfy container generates its own credentials and prints them once.
    + Uvicorn launches the FastAPI application with a single worker (the scan scheduler runs in-process).
    + Five years of history is downloaded for 41 tickers. Yahoo Finance rate-limits, so give it several minutes.

- **Get your credentials**

```
docker compose exec ntfyMoose moose-creds
```

  This prints the ntfy server address, topic, username, password and your dashboard token. Run it again any time.

- **Access the Dashboard**

  Open a web browser at <http://localhost:19080>, or at the address printed in the log, and paste the dashboard token when asked.

  The first scan starts on its own — you do not need to press **Scan now**. The header says `first scan running, this takes a few minutes` while it works.

- **View application logs** (Optional)

```
docker compose logs -f supertrendMoose
```

- **Update to the latest version**

```
git pull
```

```
docker compose up -d --build
```

- **Stopping**

```
docker compose stop
```

- **Remove containers** (data persists)

```
docker compose down
```

- **Remove containers AND volumes** (All data is deleted, including your recorded positions)

```
docker compose down -v
```

---

<!--
  Screenshots go here, in the same style as marketMoose:
  upload the images to static/ and link them as
  [![](https://github.com/theblackmoose/supertrendMoose/raw/main/static/<file>.png)](…)
-->

---

## 🌐 Choosing the address

One setting in `.env` decides where SupertrendMoose is reached:

```
HOST_IP=192.168.1.50
```

Use any address that belongs to this machine: a LAN IP, a VM's own IP, or a Tailscale IP. Find yours with `ip -4 addr` (Linux), `ipconfig` (Windows) or `ipconfig getifaddr en0` (macOS). Then apply it with `docker compose up -d`.

That one value sets:

| What | Becomes |
| ---- | ------- |
| Where the dashboard listens | `192.168.1.50:19080` |
| Where ntfy listens | `192.168.1.50:19081` |
| The server address for the ntfy phone app | `http://192.168.1.50:19081` |
| Links in alerts | `http://192.168.1.50:19080` |

| `HOST_IP` | Result |
| --------- | ------ |
| blank (default) or `127.0.0.1` | This machine only, at `localhost`. No links in alerts. |
| an address of this machine | Reachable from other devices on that address. |
| `0.0.0.0` | Listens on every interface. Also set `BASE_URL` and `NTFY_PUBLIC_URL`, since `0.0.0.0` is not an address a phone can use. |

If the address is not one this machine has, Docker refuses to start with `cannot assign requested address`. Check the IP and try again. Only IPv4 addresses are supported.

To use a hostname instead (for example a Tailscale MagicDNS name), keep `HOST_IP` as the machine's IP and add `BASE_URL` and `NTFY_PUBLIC_URL` pointing at the name.

---

## 📱 Notifications

The bundled ntfy server runs with `auth-default-access: deny-all` and creates two locked-down accounts: `moose` is **write-only** and publishes alerts, `notifications` is **read-only** and receives them. Nobody can inject a fake buy signal into your morning alerts.

- **On your phone:** install the ntfy app, add the server address from `moose-creds`, sign in as `notifications`, and subscribe to your topic (`SupertrendMoose` by default, case-sensitive).

- **In a browser:** open <http://localhost:19081>, log in as `notifications`, and subscribe to the topic.

- **Send a test alert:**

```
curl -X POST http://localhost:19080/api/test-notification \
  -H "X-Auth-Token: <your dashboard token>"
```

  On Windows PowerShell:

```
Invoke-RestMethod -Method Post http://localhost:19080/api/test-notification `
  -Headers @{ "X-Auth-Token" = "<your dashboard token>" }
```

- **Prefer email, Telegram or Discord?** `NOTIFY_CHANNELS` takes any comma-separated combination, and running two is a reasonable default for a once-daily alert. [DEPLOY.md](DEPLOY.md#-email-alerts-optional) walks through email via Gmail, including how to keep the password out of `.env` and the outbound port 587 your firewall must allow.

---

## ⚙️ Testing & Configuration

- **View Docker containers**:

```
docker compose ps
```

- **Shell Access**:

```
docker compose exec supertrendMoose bash
```

- **Run the offline test suite**:

```
python tests/smoke_test.py
```

  Runs fully offline against synthetic prices — no network, no API keys.

---

Place runtime settings in the `.env` file. Every setting is optional; `docker compose up -d --build` works with no `.env` at all. Key variables:

| Variable | Description | Default |
| -------- | ----------- | ------- |
| `HOST_IP` | Address both services listen on, and the address used in alerts | (blank, loopback only) |
| `APP_PORT` | Host port for the dashboard | `19080` |
| `NTFY_PORT` | Host port for ntfy | `19081` |
| `TZ` | Your time zone, for log times and the weekly earnings refresh | `Australia/Melbourne` |
| `SCAN_AFTER_CLOSE_MINUTES` | Minutes after the 16:00 New York close to scan (15–600) | `60` |
| `SCAN_ON_STARTUP` | Scan on container start if stored data is out of date | `true` |
| `NOTIFY_CHANNELS` | Comma-separated: `ntfy`, `telegram`, `discord`, `email` | `ntfy` |
| `NOTIFY_DETAIL` | `full` names tickers and prices, `minimal` sends counts only | `full` |
| `NOTIFY_HEARTBEAT` | Send a "no signals today" message on quiet days, so silence means the alerting broke | `false` |
| `NTFY_TOPIC` | ntfy topic name (case-sensitive) | `SupertrendMoose` |
| `AUTH_TOKEN` | Dashboard token. Generated for you; set only to use your own | (generated) |
| `DEFAULT_PROFILE` | Filter seeded onto new tickers: `none`, `regime`, `adx_rising`, `adx_only`, `full` | `none` |
| `ATR_LEN` / `ATR_MULT` | Supertrend ATR length and stop width | `10` / `3.0` |
| `ADX_MIN` | Minimum trend strength for the filter profiles | `20` |
| `HISTORY_YEARS` | Years of daily bars downloaded per ticker | `5` |
| `DEFAULT_COMMISSION` | Pre-fills your broker's flat fee on the buy and sell forms | (none) |
| `STARTING_CASH` | Opening balance for the Earnings tab's account curve | (derived) |

The full annotated list, including the Telegram, Discord and SMTP settings, is in [.env.example](.env.example).

---

## 💾 Persistent Files & Volumes

- **Persistent Files**:

Docker Compose uses named volumes stored in the following locations:

moose-data → mounted at /data (moose.db: prices, watchlist, positions, scan history)

ntfy-data → mounted at /var/lib/ntfy (ntfy's user and message database)

moose-secrets → mounted at /secrets (generated passwords and the dashboard token)

These survive restarts and rebuilds, using the `docker compose down` command.
They are only removed if you explicitly delete the volumes, using the `docker compose down -v` command.

The volumes are always named `supertrendmoose_…`, whatever the cloned folder is called, so renaming or moving the folder on the same host keeps your data.

- **List files**:

```
docker volume ls

docker compose exec supertrendMoose ls -lah /data
```

- **Backup & Restore volumes**:

Prices re-download in minutes. **Your recorded positions do not.** They live in the `moose-data` volume.

Backup on Linux:

```
mkdir -p ~/supertrendmoose_backups

docker run --rm -v supertrendmoose_moose-data:/data -v ~/supertrendmoose_backups:/backup alpine \
  tar czf /backup/moose-data_$(date +%Y%m%d).tar.gz -C /data .

docker run --rm -v supertrendmoose_ntfy-data:/ntfy -v ~/supertrendmoose_backups:/backup alpine \
  tar czf /backup/ntfy-data_$(date +%Y%m%d).tar.gz -C /ntfy .
```

Restore on Linux (stop the app first, so the database is not in use):

```
docker compose stop supertrendMoose

docker run --rm -v supertrendmoose_moose-data:/data -v ~/supertrendmoose_backups:/backup alpine \
  sh -lc 'cd /data && tar xzf /backup/moose-data_YYYYMMDD.tar.gz'

docker compose start supertrendMoose
```

Verify the restore worked:

```
docker run --rm -v supertrendmoose_moose-data:/data alpine ls -lah /data
```

A weekly cron entry is enough on a server:

```
0 3 * * 0 docker run --rm \
  -v supertrendmoose_moose-data:/data -v /home/<user>/backups:/backup alpine \
  tar czf /backup/moose-data_$(date +\%Y\%m\%d).tar.gz -C /data .
```

---

## 🔒 Security

Nothing here needs configuring. It ships hardened.

| | App (`supertrendMoose`) | ntfy (`ntfyMoose`) |
| --- | --- | --- |
| Runs as | uid 10001, no login shell | root (it creates the shared secrets) |
| Linux capabilities | none | only `CHOWN`, `FOWNER`, `DAC_OVERRIDE`, `NET_BIND_SERVICE` |
| Can gain privileges | no (`no-new-privileges`) | no (`no-new-privileges`) |
| Root filesystem | read-only | read-only |
| Writable | `/data`, a 128 MB `/tmp` | its two volumes, a 16 MB `/tmp` |
| Secrets | mounted read-only | generated here, mode 600 |
| Logs | rotated, 3 × 10 MB | rotated, 3 × 10 MB |

- Both ports bind to `127.0.0.1` until you set `HOST_IP`. Nothing is privileged, nothing uses the host network, the Docker socket is never mounted, and there are no bind mounts from the host.
- Two-stage build: no pip, compilers, `curl` or `wget` in the final image. The application code is owned by root, so a compromised app cannot rewrite itself.
- Every API route except `/api/health` needs the dashboard token, compared in constant time. The token travels in a header and lives in session storage, so it never appears in URLs or access logs.
- Secrets are generated inside the containers and stored in the `moose-secrets` volume. They never touch the host filesystem, your shell history or the project folder, so they cannot be committed to git.
- There are no broker or financial credentials anywhere in this application.

---

## 📖 Documentation

- **[docs/GUIDE.md](docs/GUIDE.md)** — the user guide. How to read every number on the dashboard, what the filter profiles do and why, positions and sizing, the Earnings tab, backtesting, tuning, the schedule, and the API reference.
- **[DEPLOY.md](DEPLOY.md)** — running it on a Linux server with its own IP: guest setup, Docker install, firewall rules, backups and upgrades.

---

## 📄 Docker Compose Reference

```
name: supertrendmoose

services:
  ntfyMoose:
    build: ./ntfy
    image: supertrendmoose-ntfy:latest
    container_name: ntfyMoose
    restart: unless-stopped
    environment:
      TZ: ${TZ:-Australia/Melbourne}
      NTFY_AUTH_DEFAULT_ACCESS: deny-all   # nobody reads or writes without an account
      NTFY_ENABLE_SIGNUP: "false"
      NTFY_TOPIC: ${NTFY_TOPIC:-SupertrendMoose}
    volumes:
      - ntfy-data:/var/lib/ntfy
      - moose-secrets:/secrets
    ports:
      - "${NTFY_BIND:-${HOST_IP:-127.0.0.1}}:${NTFY_PORT:-19081}:80"
    cap_drop: [ALL]
    cap_add: [CHOWN, FOWNER, DAC_OVERRIDE, NET_BIND_SERVICE]
    security_opt: ["no-new-privileges:true"]
    read_only: true
    tmpfs: ["/tmp:rw,size=16m,mode=1777"]

  supertrendMoose:
    build: .
    image: supertrendmoose:latest
    container_name: supertrendMoose
    restart: unless-stopped
    depends_on:
      ntfyMoose: { condition: service_healthy }
    environment:
      TZ: ${TZ:-Australia/Melbourne}
      DATABASE_URL: ${DATABASE_URL:-sqlite:////data/moose.db}
      HOST_IP: ${HOST_IP:-}
      APP_PORT: ${APP_PORT:-19080}
      NTFY_URL: ${NTFY_URL:-http://ntfymoose:80}   # internal network, never crosses the LAN
    ports:
      - "${BIND_ADDR:-${HOST_IP:-127.0.0.1}}:${APP_PORT:-19080}:8000"
    volumes:
      - moose-data:/data
      - moose-secrets:/secrets:ro
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    read_only: true
    tmpfs: ["/tmp:rw,size=128m,mode=1777"]

volumes:
  moose-data:
  ntfy-data:
  moose-secrets:
```

Abridged for readability — see [docker-compose.yml](docker-compose.yml) for the full file with comments.

The `supertrendMoose` service runs `exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1` by default, as defined in the entrypoint.sh.

---

## ⚠️ Known Limits

- **yfinance is an unofficial scraper.** It breaks periodically when Yahoo changes its endpoints. A failed ticker is logged and skipped, never fatal, but if it breaks badly, swapping `app/data.py` for an Alpaca or Polygon client is the fix. Nothing else in the codebase touches the data provider.
- **Daily bars only.** Signals reflect the previous close.
- **Earnings dates** come from yfinance and refresh weekly. They are sometimes wrong or missing. Check before you hold through a report.
- **Not financial advice.** This is a personal tool that reports what an indicator did. It has no opinion on whether a trade is a good idea, and it never places orders. Use it at your own risk.

---

## 📜 License

Released under the [GNU General Public License v3.0](LICENSE).

Bundles [TradingView Lightweight Charts™](https://github.com/tradingview/lightweight-charts) v4.2.0, © TradingView, Inc., used under the Apache License 2.0. Price data comes from Yahoo Finance via [yfinance](https://github.com/ranaroussi/yfinance); push alerts use [ntfy](https://github.com/binwiederhier/ntfy).

---

## 👨‍💻 Contact

**theblackmoose** – [GitHub](https://github.com/theblackmoose)
