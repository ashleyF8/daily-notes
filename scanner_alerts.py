#!/usr/bin/env python3
"""
Runs the scan and sends iPhone push notifications (ntfy app) with a full
trade plan: entry, stop-loss, targets and position size.

* EARLY: a softer first ping (up 5%+, 3x volume, news) so you start watching.
* Full signal: your full rules, with entry / stop / targets / size.
* NEW HIGH: re-alert (max 3 a day) when an alerted stock pushes higher.

Runs on GitHub Actions. With LOOP_MINUTES set it keeps scanning every
SCAN_EVERY_SECONDS until the US session ends or the time budget runs out.
Remembers what it has sent today in alerted.json.

Environment variables:
    NTFY_TOPIC          your private ntfy topic name (GitHub secret)
    ACCOUNT_GBP         account size (GitHub secret, read by the scanner)
    FORCE_RUN           "1" = single run even when closed, always sends a status
    LOOP_MINUTES        keep scanning for up to this many minutes (0 = one scan)
    SCAN_EVERY_SECONDS  pause between scans in loop mode (default 90)
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import momentum_scanner as scanner

STATE_FILE = Path(__file__).resolve().parent / "alerted.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
MAX_ALERTS_PER_STOCK = 3
REALERT_NEW_HIGH_PCT = 5.0   # re-alert if the high of day is this % above the last alert


def send_push(topic, title, message, click=None, priority=4, tags=None, actions=None):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions
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
    if h.get("tier") == "early":
        if record is None:
            return True, "early"
        return False, ""                 # early warning is sent once only
    if record is None or not record.get("count"):
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
    prefix = {"new": "", "open": "OPEN: ", "newhigh": "NEW HIGH: ",
              "early": "EARLY: "}.get(reason, "")
    if reason == "early":
        return f"{prefix}{h['symbol']} +{h['change']:.0f}% - watch" + (" [DILUTION]" if h["dilution"] else "")
    if h["mode"] == "premarket":
        prefix = "PRE-MKT: " + prefix
    verdict = ""
    if not p.get("ok"):
        verdict = " - SKIP"
    elif p.get("extended"):
        verdict = " - WAIT"
    warn = " [DILUTION]" if h["dilution"] else ""
    return f"{prefix}{h['symbol']} +{h['change']:.0f}%{verdict}{warn}"


def run_once(topic, force):
    """One scan. Returns False if the market is closed (nothing done)."""
    sess, status = scanner.session()
    if sess == "closed" and not force:
        print(f"Skipping: {status}")
        return False

    today = dt.datetime.now(scanner.NY).strftime("%Y-%m-%d")
    stocks = load_state(today)

    try:
        mode, hits = scanner.run_scan(require_news=True)
    except Exception as exc:
        print(f"Scan failed: {exc}")
        if force:
            send_push(topic, "Scanner error", f"The scan failed: {exc}", priority=3, tags=["warning"])
            sys.exit(1)
        return True          # in loop mode, try again next time

    print(f"[{mode}] {status} {len(hits)} match(es): {', '.join(h['symbol'] for h in hits) or 'none'}")
    print(scanner.funnel_text())

    sent = 0
    for h in hits:
        record = stocks.get(h["symbol"])
        ok, reason = should_alert(h, record)
        if not ok:
            continue
        p = h["plan"]
        if reason == "early":
            priority = 3
        else:
            priority = 5 if p.get("ok") and not p.get("extended") and not h["dilution"] else 3
        send_push(topic, build_title(h, reason), build_message(h),
                  click=scanner.tradingview_url(h["symbol"], h.get("exchange")),
                  actions=[
                      {"action": "view", "label": "Chart",
                       "url": scanner.tradingview_url(h["symbol"], h.get("exchange"))},
                      {"action": "view", "label": "News",
                       "url": f"https://finance.yahoo.com/quote/{h['symbol']}/news"},
                  ],
                  priority=priority,
                  tags=["warning"] if h["dilution"] else ["chart_with_upwards_trend"])
        record = record or {"count": 0, "premarket": False}
        if reason == "early":
            record["early"] = True
            stocks[h["symbol"]] = record
            sent += 1
            print(f"Alert sent: {h['symbol']} (early)")
            continue
        record["count"] = record.get("count", 0) + 1
        record["hod"] = p.get("hod") or record.get("hod", 0)
        if h["mode"] == "premarket":
            record["premarket"] = True
        else:
            record["open_alerted"] = True
        stocks[h["symbol"]] = record
        sent += 1
        print(f"Alert sent: {h['symbol']} ({reason})")

    # Daily check-ins so a quiet day is never a mystery:
    #   ~9:00am New York (2pm UK) pre-market, and ~10:30am New York (3:30pm UK).
    meta = stocks.setdefault("_meta", {})
    now_ny = dt.datetime.now(scanner.NY)
    slot = None
    # (GitHub often starts scheduled runs late, so these fire on the first run
    #  AFTER the time, whenever that is - not only inside a narrow window.)
    if sess == "open" and now_ny.time() >= dt.time(10, 30) and not meta.get("open_summary"):
        slot, meta["open_summary"] = "Market check", True
        meta["pm_summary"] = True
    elif now_ny.time() >= dt.time(9, 0) and not meta.get("pm_summary"):
        slot = "Pre-market check" if sess == "premarket" else "Morning check"
        meta["pm_summary"] = True
    if slot and not force:
        full = [h for h in hits if h.get("tier") == "full"]
        names = ", ".join(h["symbol"] for h in hits) or "none"
        send_push(topic, f"{slot}: {len(full)} signal(s), {len(hits) - len(full)} early",
                  f"{status}\nMatches: {names}\n\n{scanner.funnel_text()}",
                  priority=2, tags=["mag"])

    if force and not sent:
        names = ", ".join(h["symbol"] for h in hits) or "none right now"
        send_push(topic, "Scanner is working",
                  f"{status}\nMode: {mode}\nMatches: {names}\n\n{scanner.funnel_text()}\n\n"
                  f"Account GBP {scanner.ACCOUNT_GBP}, risk {scanner.RISK_PCT:g}% per trade.",
                  priority=3, tags=["white_check_mark"])

    if sess != "closed":
        save_state(today, stocks)
    return True


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set. Add it as a GitHub secret (see README).")
    force = os.environ.get("FORCE_RUN") == "1"
    loop_minutes = 0 if force else float(os.environ.get("LOOP_MINUTES") or 0)
    every = max(30, float(os.environ.get("SCAN_EVERY_SECONDS") or 90))

    deadline = time.time() + loop_minutes * 60
    while True:
        started = time.time()
        still_open = run_once(topic, force)
        if not loop_minutes or not still_open:
            break
        wait = every - (time.time() - started)
        if time.time() + max(wait, 0) >= deadline:
            print("Time budget used up - the next scheduled run takes over.")
            break
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    main()
