# Deploying SupertrendMoose on a Linux Server

**Give it its own address, then forget about it.**

This walks through running SupertrendMoose on a Linux server so it is reachable from your LAN at an address like `http://10.20.0.42:19080`, with your phone receiving ntfy alerts. It assumes a fresh Debian or Ubuntu machine — a VM, an LXC container, a Raspberry Pi or a bare-metal box all work the same way.

For running it on your own desktop instead, the [README](README.md) is all you need.

---

## 💡 Requirements

- A Linux host with a static IP address (or a DHCP reservation)
- 2 vCPU, 2 GB RAM, 16 GB disk is comfortable — the workload is a few seconds of pandas once a day, and the database stays under 100 MB for 41 tickers
- Outbound HTTPS, so it can reach Yahoo Finance. No inbound access from the internet is needed at all

---

## 🚀 Deployment

### 1. Prepare the host

Give the machine a static address and confirm it before going further:

```
ip -4 addr show
```

### 2. Install Docker

```
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
```

  Note: These are your distribution's own packages, so they update with the rest of the system. If you are not running as root, prefix the `docker` commands below with `sudo`.

### 3. Clone the repository

```
cd ~
```

```
git clone https://github.com/theblackmoose/supertrendMoose.git
```

```
cd supertrendMoose
```

### 4. Configure for network access

This is the one part that differs from running it on a desktop. By default both services bind to `127.0.0.1`, which on a server means nothing outside the machine can reach them. One setting changes that:

```
cp .env.example .env
nano .env
```

Set the server's own address and your time zone:

```
# Both services listen here, the phone app is given http://10.20.0.42:19081,
# and alert links point at http://10.20.0.42:19080.
HOST_IP=10.20.0.42

# For log times. Scans follow the US close regardless.
TZ=Australia/Melbourne
```

The dashboard token is generated for you — step 6 shows how to print it.

Both services speak plain HTTP on that address, so the token crosses that network unencrypted. On a VLAN you trust that is defensible. To encrypt it, see **Encrypting access (HTTPS)** below.

### 5. Start it

```
docker compose up -d --build
```

```
docker compose logs -f supertrendMoose
```

The log starts with the address it worked out:

```
SupertrendMoose: starting (uid 10001)
  dashboard   http://10.20.0.42:19080
  timezone    Australia/Melbourne
  data        /data
  alert links http://10.20.0.42:19080
```

If Docker reports `cannot assign requested address`, `HOST_IP` is not an address this machine has. Compare it with `ip -4 addr show`.

The first scan starts on its own and takes a few minutes while five years of history downloads. The dashboard says so while it works.

### 6. Get the credentials and subscribe

```
docker compose exec ntfyMoose moose-creds
```

In the ntfy app: add the `ntfy server` address printed above (`http://10.20.0.42:19081`), sign in with the `notifications` account and its password, then subscribe to the topic `SupertrendMoose` (case-sensitive).

Test it end to end before relying on it:

```
curl -X POST http://10.20.0.42:19080/api/test-notification \
  -H "X-Auth-Token: <dashboard token from moose-creds>"
```

Then open the dashboard at `http://10.20.0.42:19080` and paste the same token when asked.

---

## 📧 Email Alerts (optional)

**ntfy is the default and needs no setup** — it ships configured, generates its own credentials, and the steps above already cover it. Email is an alternative or an addition, worth having because it works from anywhere without a VPN, adds no daemon to your phone, and leaves you a searchable archive of every signal that a push notification does not.

**You do not need to run a mail server.** SupertrendMoose is an SMTP *client*: it only needs a relay to hand the message to. Running your own would be worse, not better — outbound port 25 is blocked on most home connections, and a fresh IP with no reputation lands in spam.

Gmail is the relay documented here, because it is free, needs no domain, and is the one route that reliably delivers.

### Why not send from your own mail provider?

Since February 2024 the large providers publish strict DMARC policies, and a message claiming to come from their domain but sent through a third-party relay fails authentication. Practically:

- **Proton, iCloud, Yahoo and most free-mail addresses cannot be used as a sender** at a relay like Brevo or Mailjet. Those services refuse to add the sender at all, because authenticating the domain means publishing DKIM records in DNS you do not control. Proton additionally has no SMTP endpoint of its own; the only route is Proton Mail Bridge, which needs a paid plan and holds a session with full read and write access to your mailbox.
- **Gmail through Gmail's own servers is different.** Google is the authorised sender for `gmail.com`, so SPF and DKIM both pass and align. The same address sent through a third-party relay would be quarantined.

You can still *deliver* anywhere. Send through Gmail and set `SMTP_TO` to your Proton address, or any other.

### Setting it up

**1. Enable 2-Step Verification** on the Google account, at <https://myaccount.google.com/security>. Without it the App Passwords page does not exist.

**2. Generate an app password** at <https://myaccount.google.com/apppasswords>. Google shows a 16-character code once — copy it immediately, and strip the spaces. Your normal account password will not work; the "Less Secure Apps" option was removed years ago.

  Note: despite the label you give it, **an app password is not scoped to sending.** It also grants IMAP access to the whole mailbox and bypasses 2-Step Verification. Prefer a Google account that holds nothing over your main one — account isolation is the only scoping available.

**3. Put the settings in `.env`:**

```ini
NOTIFY_CHANNELS=ntfy,email
NOTIFY_HEARTBEAT=true

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_FROM=you@gmail.com
SMTP_TO=where-you-want-it@example.com
SMTP_TLS=true
```

`SMTP_FROM` must match `SMTP_USER` — Gmail only sends as the authenticated account or an alias it has verified, and rejects anything else. Keep `SMTP_TO` different from `SMTP_FROM`: a message claiming to be from the mailbox it arrives at looks like spoofing to a spam filter, and some providers file it outside the Inbox.

**4. Put the password in the secrets volume, not in `.env`.** This is the step worth taking. `.env` sits on the host filesystem and its contents show up in `docker inspect` and `docker compose config` — the output people paste when asking for help. The secrets volume never touches the host.

Run this from the folder containing `docker-compose.yml`. The prompt runs on the host, where hidden input is reliable, and `moose-set-smtp-pass` inside the container stores what it receives:

**Linux / macOS (bash or zsh):**

```bash
read -rsp 'App password: ' p; echo; printf '%s' "$p" | docker compose exec -T ntfyMoose moose-set-smtp-pass; unset p
```

**Windows PowerShell:**

```powershell
$p = Read-Host -AsSecureString 'App password'; [Net.NetworkCredential]::new('', $p).Password | docker compose exec -T ntfyMoose moose-set-smtp-pass; Remove-Variable p
```

Paste the app password at the prompt — nothing is shown — and press **Enter once**. You should see `Saved 16 characters`. Spaces are stripped, so Google's `abcd efgh ijkl mnop` format is fine as pasted. For Gmail, any other count means the paste went wrong; run it again. It replaces any password stored before.

If your mail provider's password really contains spaces, add `--keep-spaces` after `moose-set-smtp-pass`; then only line breaks are removed.

Why this form:

- **The password never appears on a command line**, so it stays out of your shell history.
- **`-T` is required.** The container's terminal handles hidden input badly through `exec`: it can echo the password, swallow keystrokes and save a partial value. `moose-set-smtp-pass` refuses to read from a terminal for that reason, and prints the right command instead.
- **If the command errors immediately** (for example "no configuration file provided" because you are in the wrong folder), nothing was saved. Nothing you type afterwards reaches the container either, so check your shell history for a stray password and remove it (`history -d <line>`).
- **The file is handed to the app's user** (uid 10001) at mode 600, and written in full before it replaces the old one, so a failed run never leaves a half-written password behind.

To confirm it without printing it:

```bash
docker compose exec ntfyMoose moose-creds
```

Expect `smtp password   stored (16 characters)`.

Then restart the app so it reads the password — it is loaded once at startup:

```
docker compose up -d --force-recreate supertrendMoose
```

This also picks up any `.env` changes. Plain `docker compose restart` does not: it restarts the container with the settings it already had.

**5. Allow outbound TCP 587 from the server.** If your firewall restricts what the server can reach — a common homelab setup is to allow only 80 and 443 — email will fail until you add a pass rule for the server's IP to TCP 587. On pfSense or OPNsense: *Firewall → Rules → [the server's interface]*, pass TCP from the server to port 587, above any block rule. The destination can be *any*, or a host alias for `smtp.gmail.com`; Google rotates the addresses behind that name often, so an alias can occasionally miss a newly resolved address.

To check from the server:

```bash
timeout 5 bash -c '</dev/tcp/smtp.gmail.com/587' && echo open || echo blocked
```

  `SMTP_PASS` in `.env` still works and still takes precedence, so an existing setup keeps running unchanged. The volume is simply the better place for it. To move an existing password across, store it as above, delete the `SMTP_PASS` line from `.env`, then run `docker compose up -d --force-recreate supertrendMoose`.

  To remove a stored password: `docker compose exec ntfyMoose moose-set-smtp-pass --clear`, then recreate the app the same way. `docker compose down -v` also deletes it, along with everything else in the volumes.

### Running email as your only channel

`NOTIFY_CHANNELS=email` on its own is a reasonable choice, but a single channel has a failure mode worth closing: a quiet day and a broken relay look identical, because both produce silence.

`NOTIFY_HEARTBEAT=true` closes it. On any scan that produces no signals, a short "no signals today" message goes out with the ticker count and the date the prices run through. Something arrives every trading day, so **silence means the alerting is broken** rather than the market being calm.

It is suppressed when the scan was degraded — stale prices, or most tickers failing to download — because the separate problem alert has already gone out and "no signals" would be misleading beside it.

`NOTIFY_DETAIL=minimal` also applies to email, if you would rather not have tickers and prices sitting in a mailbox.

### Testing it

```
docker compose up -d
```

```
curl -X POST http://10.20.0.42:19080/api/test-notification \
  -H "X-Auth-Token: <dashboard token from moose-creds>"
```

The response names each channel and whether it succeeded. If email reports `false`, the reason is in the log:

```
docker compose logs --tail=30 supertrendMoose | grep -i email
```

| Symptom | Cause |
| ------- | ----- |
| `535` authentication failed | Wrong app password, or 2-Step Verification is off |
| `534` / "application-specific password required" | Using the account password instead of an app password |
| `[Errno 101] Network is unreachable` | Outbound TCP 587 is blocked (see step 5). Python reports only the last address it tried, usually IPv6, which Docker has no route for, so this hides the real IPv4 timeout |
| `timed out` / `Connection refused` | Outbound TCP 587 is blocked at the host, firewall or ISP (see step 5) |
| Reports `false` with no SMTP error | `SMTP_HOST` or `SMTP_TO` unset — both are required before email is attempted |
| **Reports `true`, nothing arrives** | The relay accepted it and delivery failed later. Check Gmail's *Sent* folder, then Gmail's *Inbox* for a bounce notice |
| Relay shows it delivered, still not in the inbox | Your provider filed it outside the Inbox. Search *All mail*, and *Sent* if the From matches your own address |

**A `true` result only means the relay accepted the message.** Everything after that happens out of the app's sight. A failing email channel can never abort a scan — it is logged and the scan continues.

---

## 🔥 Firewall Rules

Allow from your client networks to the server address:

| Source | Destination | Port | Purpose |
| ------ | ----------- | ---- | ------- |
| your workstation VLAN | server IP | 19080/tcp | dashboard |
| your phone's VLAN | server IP | 19081/tcp | ntfy alerts |

Allow outbound from the server:

| Source | Destination | Port | Purpose |
| ------ | ----------- | ---- | ------- |
| server IP | any | 443/tcp | Yahoo Finance price data |
| server IP | any (or `smtp.gmail.com` alias) | 587/tcp | email alerts — only if `email` is in `NOTIFY_CHANNELS` |

Nothing else outbound is required, and nothing inbound from the internet.

If the phone sits on a guest or IoT VLAN that cannot reach services, either move it or add the single 19081 rule. ntfy will not work over the internet in this setup, which is deliberate: it stays on the LAN.

If email is your only alert channel, nothing needs to reach ntfy from outside: set `NTFY_BIND=127.0.0.1` in `.env` and skip the 19081 rule. The app still reaches ntfy over Docker's internal network.

With Tailscale Serve (see **Encrypting access (HTTPS)**), neither inbound rule is needed: both ports stay on loopback and traffic arrives over Tailscale.

---

## 💾 Backups

Prices re-download in minutes. **Your recorded positions do not.** They live in the `supertrendmoose_moose-data` volume, which is always named that whatever the project folder is called.

**Dump the positions table: fully restorable**

This copies just the positions table as SQL. It uses a throwaway Alpine container that installs the sqlite tool, and it mounts the data volume read-only (:ro) so the backup can't change anything:

Backup on Linux:

```
mkdir -p "$HOME/supertrendmoose_backups"

docker run --rm -v supertrendmoose_moose-data:/data:ro -v "$HOME/supertrendmoose_backups":/backup \
  alpine sh -c 'apk add -q sqlite && sqlite3 /data/moose.db ".dump positions" > \
  /backup/positions_$(date +%Y%m%d).sql && grep -c "^INSERT" /backup/positions_$(date +%Y%m%d).sql'
```
The number it prints is how many trades were saved. The file is plain text and only a few KB, so you can open it to check. The date in the file name comes from the container, which runs on UTC, so it can be a day off from your local date.

To restore just the trades, replacing the current trades and leaving the watchlist, signals and prices alone:

```
docker compose stop supertrendMoose

docker run --rm -v supertrendmoose_moose-data:/data alpine cp -p /data/moose.db /data/moose.db.before-restore

docker run --rm -v supertrendmoose_moose-data:/data -v "$HOME/supertrendmoose_backups":/backup alpine \
  sh -c 'apk add -q sqlite && awk "/^CREATE TABLE positions/{print \"DROP TABLE IF EXISTS positions;\"}1" \
  /backup/positions_YYYYMMDD.sql | sqlite3 /data/moose.db && sqlite3 /data/moose.db "SELECT COUNT(*) FROM positions"'

docker compose start supertrendMoose
```
The `awk` step adds a "drop the current table" line inside the dump's own transaction, so the swap is all-or-nothing: if anything fails partway, your current trades are left untouched. The safety copy (`moose.db.before-restore`) covers you if you restore the wrong file. Delete it once you've checked the Earnings tab.

**An older dump restores fine into a newer version of the app.** On startup the app adds any columns introduced since (the commission fields were added this way), and fills them with defaults.

Verify the restore worked:

```
docker run --rm -v supertrendmoose_moose-data:/data:ro -v "$HOME/supertrendmoose_backups":/backup:ro alpine \
  sh -c 'apk add -q sqlite && sqlite3 /data/moose.db ".dump positions" | diff /backup/positions_YYYYMMDD.sql - \
  && echo IDENTICAL'
```

If the dump came from an older version, differences are expected; compare the counts instead.

If anything looks wrong, you can undo the restore with the safety copy:

```
docker compose stop supertrendMoose

docker run --rm -v supertrendmoose_moose-data:/data alpine sh -c 'cp -p /data/moose.db.before-restore /data/moose.db'

docker compose start supertrendMoose
```

Clean up once you're satisfied:

```
docker run --rm -v supertrendmoose_moose-data:/data alpine rm /data/moose.db.before-restore
```

**Full database backup (trades, watchlist, tuning, signal history)**

```
mkdir -p "$HOME/supertrendmoose_backups"

docker run --rm -v supertrendmoose_moose-data:/data:ro -v "$HOME/supertrendmoose_backups":/backup alpine tar czf /backup/moose-db_$(date +%Y%m%d).tar.gz -C /data moose.db
```

Restoring rolls **everything** back to the backup date: trades, watchlist and signal history. Prices re-download on their own. To roll back only trades, use the positions dump above instead.

```
docker compose stop supertrendMoose

docker run --rm -v supertrendmoose_moose-data:/data -v "$HOME/supertrendmoose_backups":/backup alpine sh -c \
  'rm -f /data/moose.db-wal /data/moose.db-shm && tar xzf /backup/moose-db_YYYYMMDD.tar.gz -C /data && ls -ln /data'

docker compose start supertrendMoose
```

`moose.db` should show owner `10001`.

**A weekly cron entry is enough on a server.** Add it with `crontab -e` as **one line**; cron does not support `\` line continuations:

```
0 3 * * 0 mkdir -p /home/<user>/supertrendmoose_backups && docker run --rm -v supertrendmoose_moose-data:/data:ro -v /home/<user>/supertrendmoose_backups:/backup alpine tar czf /backup/moose-db_$(date +\%Y\%m\%d).tar.gz -C /data moose.db
```

If your user needs `sudo` to run Docker, add it to root's crontab instead (`sudo crontab -e`).

---

## 🔄 Upgrading

```
cd ~/supertrendMoose
```

```
git pull
```

```
docker compose up -d --build
```

Your `.env` is not tracked by git, so `git pull` never touches it. Your data stays in its volumes.

`docker compose down -v` destroys the volumes, **including your positions and the ntfy credentials**. Use plain `down` unless you intend to start over.

---

## 🔐 Encrypting access (HTTPS)

The dashboard and ntfy speak plain HTTP. Whatever sits between your device and the server can read the dashboard token and your alerts, so how much this matters depends on how you reach it:

| How you reach it | What is encrypted |
| ---------------- | ----------------- |
| Default (`HOST_IP` blank) | Nothing leaves the machine, so there is nothing to intercept. |
| `HOST_IP` set to a LAN address | Nothing. The token crosses the LAN in cleartext. |
| A VPN that ends on your firewall (a Tailscale subnet router or WireGuard on pfSense/OPNsense) | Your device to the firewall. The last hop, firewall to server, is plain HTTP on the server's VLAN. |
| `HOST_IP` set to this machine's Tailscale IP | All of it — WireGuard encrypts it end to end — but see the note below. |
| Tailscale Serve | All of it, with a real HTTPS certificate. Recommended. |

### Recommended: Tailscale Serve

The app stays on `127.0.0.1`, and Tailscale publishes it to your tailnet at `https://<machine>.<tailnet>.ts.net` with a certificate it issues and renews itself. Only devices signed in to your tailnet can reach it.

It needs Tailscale on the machine running SupertrendMoose — the Docker host itself, not a container — and on each device you use it from.

**1.** In the Tailscale admin console, under **DNS**, enable **MagicDNS** and **HTTPS Certificates**. The machine's full name, `<machine>.<tailnet>.ts.net`, is shown on its page under **Machines**.

**2.** In `.env`, keep both services on loopback and point alert links at the new name:

```
HOST_IP=127.0.0.1
BASE_URL=https://<machine>.<tailnet>.ts.net
```

```
docker compose up -d
```

**3.** Publish the dashboard:

```
sudo tailscale serve --bg --https=443 http://127.0.0.1:19080
```

Use `127.0.0.1`, not `localhost`: Docker listens on IPv4 loopback only, and `localhost` can resolve to the IPv6 `::1`. `--bg` keeps it running in the background and across reboots. The first request can take a minute while the certificate is issued.

Open `https://<machine>.<tailnet>.ts.net` and paste the dashboard token as before. `tailscale serve status` shows what is published; `sudo tailscale serve --https=443 off` removes it.

**4. Only if you use the ntfy phone app:** publish ntfy on a second port, and tell the app its new address:

```
sudo tailscale serve --bg --https=8443 http://127.0.0.1:19081
```

In `.env`, add `NTFY_PUBLIC_URL=https://<machine>.<tailnet>.ts.net:8443`, run `docker compose up -d`, then re-add the server in the ntfy app with that address. The phone needs Tailscale connected to receive alerts. If email is your only channel, skip this step: ntfy stays on loopback and the app still reaches it internally.

### Behind a VPN on your firewall

If you already reach the server through a VPN that ends on the firewall, leaving `HOST_IP` on the LAN address is a reasonable setup: only the last hop is unencrypted. Keep that hop contained:

- Allow 19080 (and 19081, if you use ntfy) to the server **from the VPN interface only**, not from your other VLANs. Otherwise a device on another VLAN can reach the plain HTTP ports directly.
- Keep the server's VLAN to hosts you trust, since anything able to see traffic on it could read the token.

For end-to-end encryption on top of that, add Tailscale Serve on the server as above.

### Why not set `HOST_IP` to the Tailscale IP?

It works, and the traffic is encrypted. The catch is start-up order: after a reboot, Docker can start the containers before Tailscale has brought its address up, and they then fail with `cannot assign requested address` and can stay down until you start them by hand. Serve avoids that, because loopback always exists. It also gives a real `https://` address, which the Tailscale IP does not.

### Alternative: Caddy

If you would rather use your own name, such as `https://moose.lab.internal`, put Caddy in front. It issues an internal certificate automatically:

```
moose.lab.internal {
    reverse_proxy 127.0.0.1:19080
}
```

Then point a DNS host override at the Caddy host, and set `BASE_URL=https://moose.lab.internal` in `.env` so alert links use it.

- **Run Caddy on the same host and set `HOST_IP=127.0.0.1`,** as in the example. If you leave `HOST_IP` on the LAN address, the plain HTTP port stays open beside the HTTPS one and nothing has really changed.
- **Every device must trust Caddy's internal root certificate,** or browsers show a warning each time. Install it once on each device you use.
- Keep the ntfy port as it is, or proxy it the same way and set `NTFY_PUBLIC_URL` to match: the phone must be able to resolve, reach and trust whatever you put there.

---

## 🩺 Troubleshooting

### Running a missed scan by hand

The scan runs itself after every US close, and a `_catch_up` job checks every 15 minutes whether the stored prices are behind the last finished session and scans if they are. So a machine that was asleep or off catches up on its own, and you rarely need this.

The dashboard's **Scan now** button does the same. It alerts on any signal that has not been alerted yet, so a signal is never lost because a manual scan happened to find it first, and never sent twice because a scan ran twice on the same bar.

To force it from the command line:

```
curl -X POST "http://10.20.0.42:19080/api/scan?send_alerts=true" \
  -H "X-Auth-Token: <dashboard token from moose-creds>"
```

On Windows PowerShell:

```
Invoke-RestMethod -Method Post "http://localhost:19080/api/scan?send_alerts=true" `
  -Headers @{ "X-Auth-Token" = "<your dashboard token>" }
```

`send_alerts=true` is the part that matters. Without it the scan runs and updates the dashboard but sends nothing, which is also the default for a cold start — the first scan on an empty database never alerts, because every ticker's last bar would look like a fresh signal and you would get a burst of stale ones.

Each signal is alerted once. To send one again — this morning's alert, say, after fixing a notification channel — add `&resend=true`.

Prices are refreshed first unless you add `&refresh_prices=false`, which re-evaluates the stored bars without going out to Yahoo. Useful for testing an alert format change without waiting on a download.

Signals are not deduplicated between runs, so running this twice sends the same alert twice. The response lists the tickers it found and a `notified` object saying which channels accepted the message.

### Checks

| Command | Checks |
| ------- | ------ |
| `docker compose ps` | Both containers up |
| `docker compose logs --tail=50 supertrendMoose` | Scan errors |
| `curl -s http://<ip>:19080/api/health` | `data_through` and whether a scan is running |
| `curl -s http://<ip>:19080/api/diagnose -H "X-Auth-Token: <token>"` | Probes the Yahoo price pipeline directly |

`data_through` lagging by more than one session usually means `TZ` is wrong or Yahoo rejected the fetch. `/api/diagnose` distinguishes the two. A single missing session is normal on a US holiday.

If `docker compose` fails with `unknown shorthand flag: 'd'` or `'compose' is not a docker command`, the Compose v2 plugin is missing. Install `docker-compose-plugin` (Docker's packages) or `docker-compose-v2` (Ubuntu's).

---

## 📦 Moving from a Tarball Install

If you installed from a copied folder rather than git, switch over like this. Your data stays in its Docker volumes throughout.

```
# 1. Check the volume names. They should start with supertrendmoose_
docker volume ls | grep moose
```

```
# 2. Stop the old copy (plain down, never down -v)
cd ~/supertrendmoose && docker compose down
```

```
# 3. Clone next to it and bring your settings across
cd ~
git clone https://github.com/theblackmoose/supertrendMoose.git
cp ~/supertrendmoose/.env ~/supertrendMoose/.env 2>/dev/null || true
```

```
# 4. Start the new copy
cd ~/supertrendMoose && docker compose up -d --build
```

If step 1 showed a different prefix, because the old folder had another name, copy the data into the new volume names before step 4:

```
OLD=<old prefix>     # e.g. moose, for moose_moose-data
for v in moose-data ntfy-data moose-secrets; do
  docker volume create supertrendmoose_$v
  docker run --rm -v ${OLD}_$v:/from -v supertrendmoose_$v:/to alpine \
    sh -c 'cp -a /from/. /to/'
done
```

Once the new copy is working, the old folder can be deleted.

---

## 👨‍💻 Contact

**theblackmoose** – [GitHub](https://github.com/theblackmoose)
