#!/usr/bin/env python3
"""
Runs the momentum scan and sends iPhone push notifications (ntfy app) with a
full trade plan: entry, stop-loss, targets and position size.

* Pre-market (9am-2:30pm UK): alerts on stocks gapping up with news.
* Market hours: alerts on new momentum stocks, and again (max 3 a day) if a
  stock already alerted pushes to a new high.

Runs on GitHub Actions. Remembers what it has sent today in alerted.json.

Environment variables:
    NTFY_TOPIC   your private ntfy topic name (GitHub secret)
    FORCE_RUN    "1" = run even when the market is closed and always send a
                 status message (used by the manual "Run workflow" button)
"""

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

import momentum_scanner as scanner

STATE_FILE = Path(__file__).resolve().parent / "alerted.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
MAX_ALERTS_PER_STOCK = 3
REALERT_NEW_HIGH_PCT = 5.0   # re-alert if the high of day is this % above the last alert


def send_push(topic, title, message, click=None, priority=4, tags=None):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        NTFY_SERVER, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def load_state(today):
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if data.get("date") != today:
        return {}
    stocks = data.get("stocks")
    if stocks is None:   # old v1 format: plain list of symbols
        stocks = {s: {"count": 1, "hod": 0, "premarket": False} for s in data.get("symbols", [])}
    return stocks


def save_state(today, stocks):
    STATE_FILE.write_text(json.dumps({"date": today, "stocks": stocks}, indent=2) + "\n")


def should_alert(h, record):
    """Returns (send?, reason)."""
    if record is None:
        return True, "new"
    if h["mode"] == "open" and record.get("premarket") and not record.get("open_alerted"):
        return True, "open"              # was a pre-market gapper, now trading live
    if record.get("count", 0) >= MAX_ALERTS_PER_STOCK:
        return False, ""
    hod = h["plan"].get("hod") or 0
    last = record.get("hod") or 0
    if h["mode"] == "open" and last and hod >= last * (1 + REALERT_NEW_HIGH_PCT / 100):
        return True, "newhigh"
    return False, ""


def build_message(h):
    lines = [scanner.summary_line(h)]
    for d in h["dilution"]:
        lines.append(f"DILUTION: {d}")
    lines.extend(scanner.plan_lines(h))
    for n in h["news"][:1]:
        when = n["time"].astimezone(scanner.ZoneInfo("Europe/London")).strftime("%H:%M")
        lines.append(f"News {when}: {n['title']}")
    return "\n".join(lines)


def build_title(h, reason):
    p = h["plan"]
    prefix = {"new": "", "open": "OPEN: ", "newhigh": "NEW HIGH: "}.get(reason, "")
    if h["mode"] == "premarket":
        prefix = "PRE-MKT: " + prefix
    verdict = ""
    if not p.get("ok"):
        verdict = " - SKIP"
    elif p.get("extended"):
        verdict = " - WAIT"
    warn = " [DILUTION]" if h["dilution"] else ""
    return f"{prefix}{h['symbol']} +{h['change']:.0f}%{verdict}{warn}"


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set. Add it as a GitHub secret (see README).")
    force = os.environ.get("FORCE_RUN") == "1"

    sess, status = scanner.session()
    if sess == "closed" and not force:
        print(f"Skipping: {status}")
        return

    today = dt.datetime.now(scanner.NY).strftime("%Y-%m-%d")
    stocks = load_state(today)

    try:
        mode, hits = scanner.run_scan(require_news=True)
    except Exception as exc:
        print(f"Scan failed: {exc}")
        if force:
            send_push(topic, "Scanner error", f"The scan failed: {exc}", priority=3, tags=["warning"])
        sys.exit(1)

    print(f"[{mode}] {status} {len(hits)} match(es): {', '.join(h['symbol'] for h in hits) or 'none'}")

    sent = 0
    for h in hits:
        record = stocks.get(h["symbol"])
        ok, reason = should_alert(h, record)
        if not ok:
            continue
        p = h["plan"]
        priority = 5 if p.get("ok") and not p.get("extended") and not h["dilution"] else 3
        send_push(topic, build_title(h, reason), build_message(h),
                  click=f"https://finance.yahoo.com/quote/{h['symbol']}",
                  priority=priority,
                  tags=["warning"] if h["dilution"] else ["chart_with_upwards_trend"])
        record = record or {"count": 0, "premarket": False}
        record["count"] = record.get("count", 0) + 1
        record["hod"] = p.get("hod") or record.get("hod", 0)
        if h["mode"] == "premarket":
            record["premarket"] = True
        else:
            record["open_alerted"] = True
        stocks[h["symbol"]] = record
        sent += 1
        print(f"Alert sent: {h['symbol']} ({reason})")

    if force and not sent:
        names = ", ".join(h["symbol"] for h in hits) or "none right now"
        send_push(topic, "Scanner v2 is working",
                  f"{status}\nMode: {mode}\nMatches: {names}\n"
                  f"Account GBP {scanner.ACCOUNT_GBP}, risk {scanner.RISK_PCT:g}% per trade.",
                  priority=3, tags=["white_check_mark"])

    if sess != "closed":
        save_state(today, stocks)


if __name__ == "__main__":
    main()
