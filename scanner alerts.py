#!/usr/bin/env python3
"""
Runs the momentum scan and sends an iPhone push notification (via the free
ntfy app) for every NEW stock that matches your rules today.

Designed to run on GitHub Actions every 10 minutes during US market hours.
Each stock is only alerted once per trading day (remembered in alerted.json).

Environment variables:
    NTFY_TOPIC   your private ntfy topic name (set as a GitHub secret)
    FORCE_RUN    "1" = run even when the market is closed and always send a
                 status message (used for the manual "Run workflow" test)
"""

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import momentum_scanner as scanner

STATE_FILE = Path(__file__).resolve().parent / "alerted.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
UK = ZoneInfo("Europe/London")


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
        if data.get("date") == today:
            return set(data.get("symbols", []))
    except (OSError, ValueError):
        pass
    return set()


def save_state(today, symbols):
    STATE_FILE.write_text(json.dumps({"date": today, "symbols": sorted(symbols)}, indent=2) + "\n")


def describe(h):
    lines = [f"${h['price']:.2f}  +{h['change']:.1f}%  |  RVOL {h['rvol']:.1f}x  |  "
             f"Vol {scanner.fmt_big(h['volume'])}  |  Float {scanner.fmt_big(h['float'])}"]
    for n in h["news"][:2]:
        when = n["time"].astimezone(UK).strftime("%H:%M UK")
        lines.append(f"- {n['title']} ({n['source']}, {when})")
    return "\n".join(lines)


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set. Add it as a GitHub secret (see README).")
    force = os.environ.get("FORCE_RUN") == "1"

    is_open, status = scanner.market_status()
    if not is_open and not force:
        print(f"Skipping: {status}")
        return

    today = dt.datetime.now(scanner.NY).strftime("%Y-%m-%d")
    already = load_state(today)

    try:
        hits = scanner.run_scan(require_news=True)
    except Exception as exc:
        print(f"Scan failed: {exc}")
        if force:
            send_push(topic, "Scanner error", f"The scan failed: {exc}", priority=3, tags=["warning"])
        sys.exit(1)

    print(f"{status} {len(hits)} match(es): {', '.join(h['symbol'] for h in hits) or 'none'}")

    new_hits = [h for h in hits if h["symbol"] not in already]
    for h in new_hits:
        title = f"{h['symbol']} +{h['change']:.0f}% - {h['name'][:40]}"
        send_push(topic, title, describe(h),
                  click=f"https://finance.yahoo.com/quote/{h['symbol']}",
                  priority=5 if h["change"] >= 30 else 4,
                  tags=["chart_with_upwards_trend"])
        already.add(h["symbol"])
        print(f"Alert sent: {h['symbol']}")

    if force and not new_hits:
        names = ", ".join(h["symbol"] for h in hits) or "none right now"
        send_push(topic, "Scanner is working",
                  f"{status}\nMatches: {names}\n(Already-alerted stocks are not repeated.)",
                  priority=3, tags=["white_check_mark"])

    if is_open:
        save_state(today, already)


if __name__ == "__main__":
    main()
