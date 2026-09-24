"""Notification backends. Enable with NOTIFY_CHANNELS=ntfy,telegram,...

Every sender returns True/False rather than raising: a broken webhook
must never abort a scan.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import settings

log = logging.getLogger("notify")


def _ntfy(title: str, body: str, url: str = "", urgent: bool = False) -> bool:
    if not settings.ntfy_topic:
        log.warning("ntfy enabled but NTFY_TOPIC is unset")
        return False
    # An urgent alert is a sell on something you hold. High priority makes the
    # phone buzz and sound where a routine alert would sit silently, and the
    # warning tag replaces the chart icon so it is recognisable at a glance.
    headers = {
        "Title": title,
        "Tags": "warning" if urgent else "chart_with_upwards_trend",
        "Priority": "high" if urgent else "default",
    }
    if url:
        headers["Click"] = url

    # Self-hosted instances with ACLs need credentials; ntfy.sh public topics don't.
    auth = None
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    elif settings.ntfy_user:
        auth = (settings.ntfy_user, settings.ntfy_password)

    r = httpx.post(
        f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}",
        content=body.encode(), headers=headers, auth=auth, timeout=15,
    )
    if r.status_code in (401, 403):
        log.warning("ntfy rejected the credentials (%s). Check NTFY_TOKEN and the topic ACL.",
                    r.status_code)
    return r.is_success


def _telegram(title: str, body: str, url: str = "", urgent: bool = False) -> bool:
    if not (settings.telegram_token and settings.telegram_chat_id):
        return False
    text = f"*{title}*\n{body}" + (f"\n{url}" if url else "")
    r = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
        json={"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    return r.is_success


def _discord(title: str, body: str, url: str = "", urgent: bool = False) -> bool:
    if not settings.discord_webhook:
        return False
    content = f"**{title}**\n```\n{body}\n```" + (f"\n{url}" if url else "")
    r = httpx.post(settings.discord_webhook, json={"content": content[:1900]}, timeout=15)
    return r.is_success


def _email(title: str, body: str, url: str = "", urgent: bool = False) -> bool:
    if not (settings.smtp_host and settings.smtp_to):
        return False
    msg = EmailMessage()
    msg["Subject"] = title
    if urgent:
        # Honoured by Outlook, Thunderbird and most desktop clients; ignored
        # harmlessly by the rest. The subject line carries the message anyway.
        msg["Importance"] = "High"
        msg["X-Priority"] = "1"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(settings.smtp_to)
    msg.set_content(body + (f"\n\n{url}" if url else ""))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as sm:
        if settings.smtp_tls:
            sm.starttls()
        if settings.smtp_user:
            sm.login(settings.smtp_user, settings.smtp_pass)
        sm.send_message(msg)
    return True


BACKENDS = {"ntfy": _ntfy, "telegram": _telegram, "discord": _discord, "email": _email}


def send(title: str, body: str, url: str = "", urgent: bool = False) -> dict[str, bool]:
    """Fan out to every configured channel. Never raises.

    urgent marks an alert that needs acting on: today, a sell signal on a
    position you hold. Channels that support it raise its priority.
    """
    results: dict[str, bool] = {}
    for name in settings.notify_channels:
        fn = BACKENDS.get(name.strip().lower())
        if not fn:
            log.warning("unknown notify channel: %s", name)
            continue
        try:
            results[name] = fn(title, body, url or settings.base_url, urgent=urgent)
        except Exception as e:  # noqa: BLE001
            log.warning("%s notification failed: %s", name, e)
            results[name] = False
    return results


def _counts_title(n_b: int, n_e: int, n_held: int = 0) -> str:
    # Held sells lead the title: it is all a lock screen shows, and a sell on
    # something you own is the one alert that needs acting on. It names a
    # count, never a ticker, so it is safe in minimal mode too.
    held = f" ({n_held} you hold)" if n_held else ""
    if n_b and n_e:
        return f"{n_b} buy, {n_e} sell{held}"
    if n_b:
        return f"{n_b} buy signal" + ("s" if n_b > 1 else "")
    return f"{n_e} sell signal" + ("s" if n_e > 1 else "") + held


def held_exits(exits: list[dict]) -> int:
    """How many sell signals are on positions you hold."""
    return sum(1 for s in exits if s.get("position"))


def format_signals(
    buys: list[dict], exits: list[dict], detail: str | None = None,
    blocked: list[dict] | None = None, scanned: int = 0,
) -> tuple[str, str]:
    """Build the alert title and body from a scan's results.

    detail="full" (default) names the tickers, prices and stops.
    detail="minimal" sends counts only, so an intercepted alert reveals
    nothing about what you are trading.

    blocked carries flip-ups the screen or the blackout rejected. They are
    listed but never alerted on, so that a ticker missing from the buy list
    is explained in the message rather than only on the dashboard.
    """
    detail = (detail or settings.notify_detail).strip().lower()
    n_b, n_e = len(buys), len(exits)
    counts = _counts_title(n_b, n_e, held_exits(exits))
    # Held positions first; sorted() is stable, so watchlist order is kept
    # within each group.
    exits = sorted(exits, key=lambda s: not s.get("position"))

    if detail == "minimal":
        return counts, "Open the scanner for details."

    # .get() throughout: this runs before send()'s try/except, so a missing
    # key here would abort the whole scan rather than just an alert.
    through = max((str(s["date"]) for s in buys + exits + list(blocked or [])
                   if s.get("date")), default="")
    lines = [f"SupertrendMoose — {counts}"]
    if scanned or through:
        summary = f"Scanned {scanned} tickers" if scanned else "Scan complete"
        lines.append(f"{summary}, prices through {through}" if through else summary)
    lines.append("")
    header_len = len(lines)   # so a later header line cannot break the spacing

    if buys:
        lines.append("BUY SIGNALS")
        for s in buys:
            # Hyphen-separated rather than space-aligned: mail clients and the
            # ntfy app collapse runs of spaces, so columns do not survive the
            # trip. Explicit separators read the same everywhere.
            stop = s.get("stop")
            bits = [f"{s['ticker']}: ${s['close']:.2f}"]
            if stop:
                bits.append(f"stop: ${stop:.2f}")
                bits.append(f"{(s['close'] / stop - 1) * 100:.1f}% risk")
            if s.get("adx") is not None:
                bits.append(f"ADX: {s['adx']:.0f}")
            if s.get("atr_pct") is not None:
                bits.append(f"ATR: {s['atr_pct']:.1f}%")
            # A ticker with under 200 bars has no average yet. Saying "below"
            # would report an absent line as a failed test against it.
            if s.get("sma") is not None:
                bits.append("above 200MA" if s.get("above_sma") else "below 200MA")
            if s.get("score") is not None:
                bits.append(f"score: {s['score']:.0f}")
            lines.append("  " + " - ".join(bits))
    if exits:
        if len(lines) > header_len:
            lines.append("")
        lines.append("SELL SIGNALS")
        for s in exits:
            pos = s.get("position") or {}
            bits = [f"{s['ticker']}: ${s['close']:.2f}", "Supertrend flipped down"]
            if pos:
                bits.append("← YOU HOLD THIS")
                held = ""
                if pos.get("pnl_pct") is not None:
                    held = f"{pos['pnl_pct']:+.1f}%"
                if pos.get("held_days") is not None:
                    held += f", held {pos['held_days']}d" if held else f"held {pos['held_days']}d"
                if held:
                    bits.append(held)
            lines.append("  " + " - ".join(bits))
    if blocked:
        lines.append("")
        lines.append("BLOCKED, NOT ALERTED")
        for s in blocked:
            lines.append(f"  {s['ticker']}: {s.get('reason') or 'filtered out'}")

    if settings.base_url:
        lines.append("")
        lines.append(f"Dashboard: {settings.base_url}")

    return counts, "\n".join(lines) or "No signals."


def format_heartbeat(states: list[dict], failed: list[str]) -> tuple[str, str]:
    """Build the 'scan ran, nothing to do' message.

    Its only job is to be predictable. A quiet day is indistinguishable from a
    broken notification channel unless something arrives to say the scan ran,
    so this goes out on every scan that produced no signals. If it stops
    arriving, the alerting itself has broken.
    """
    through = max((str(st["date"]) for st in states), default="unknown")
    body = ["Scan completed. No buy or sell signals.",
            f"  {len(states)} tickers, prices through {through}"]
    if failed:
        body.append(f"  {len(failed)} ticker(s) failed to download")
    return "no signals today", "\n".join(body)
