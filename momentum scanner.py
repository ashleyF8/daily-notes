#!/usr/bin/env python3
"""
MOMENTUM SCANNER
================
Finds US-listed stocks that match all of these:
  * Price between $2 and $20
  * Up at least 10% today
  * Relative volume of 5x or more (today's volume vs. normal daily volume)
  * Fresh news in the last 24 hours (the catalyst)

Data comes from Yahoo Finance via the free `yfinance` library - no account
or API key needed. Quotes can be delayed by around 15 minutes.

For education only. This is not financial advice.

SETUP (one time)
    1. Install Python 3.9+ from https://www.python.org (tick "Add to PATH").
    2. Open a terminal / Command Prompt and run:
           pip install --upgrade yfinance

RUN
    python momentum_scanner.py              one scan, opens a report in your browser
    python momentum_scanner.py --watch 5    rescan every 5 minutes, beeps on new hits
    python momentum_scanner.py --no-news    also show matches that have no news

WHEN TO RUN IT
    US market hours are 9:30am-4:00pm New York time. In the UK that is usually
    2:30pm-9:00pm (a few weeks a year it shifts by an hour when the clocks change
    on different dates). The first 30-60 minutes after the open is when this
    strategy usually finds its stocks.
"""

import argparse
import datetime as dt
import html
import sys
import time
import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
    from yfinance import EquityQuery
except ImportError:
    sys.exit("yfinance is not installed. Run:  pip install --upgrade yfinance")

# ---------------------------------------------------------------------------
# YOUR STRATEGY - change these numbers to tune the scanner
# ---------------------------------------------------------------------------
MIN_PRICE = 2.0          # dollars
MAX_PRICE = 20.0         # dollars
MIN_GAIN_PCT = 10.0      # % up today
MIN_REL_VOLUME = 5.0     # today's volume / average daily volume
NEWS_HOURS = 24          # how recent the news must be
MAX_NEWS_SHOWN = 3       # headlines per stock in the report
# ---------------------------------------------------------------------------

NY = ZoneInfo("America/New_York")
REPORT_DIR = Path(__file__).resolve().parent / "scanner_reports"
OTC_MARKERS = ("OTC", "PNK", "Pink", "Other OTC")


def market_status():
    now = dt.datetime.now(NY)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() >= 5:
        return False, "US market is closed (weekend) - results show the last session."
    if now < open_t:
        return False, "US market not open yet - % change shown is from the last session, not pre-market."
    if now > close_t:
        return False, "US market has closed - results show today's full session."
    return True, "US market is open."


def screen_candidates():
    """Ask Yahoo for US stocks priced $2-$20 that are up 10%+ today."""
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["percentchange", MIN_GAIN_PCT]),
        EquityQuery("btwn", ["intradayprice", MIN_PRICE, MAX_PRICE]),
    ])
    resp = yf.screen(query, sortField="percentchange", sortAsc=False, size=250)
    return resp.get("quotes", []) if isinstance(resp, dict) else []


def relative_volume(q):
    vol = q.get("regularMarketVolume") or 0
    avg = q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day") or 0
    return (vol / avg) if avg else 0.0, vol, avg


def parse_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_news(symbol, hours=NEWS_HOURS):
    """Headlines for a ticker published within the last `hours`."""
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        return []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    out = []
    for item in items:
        c = item.get("content", item) if isinstance(item, dict) else {}
        published = parse_time(c.get("pubDate") or c.get("displayTime")
                               or item.get("providerPublishTime"))
        if not published or published < cutoff:
            continue
        url = ((c.get("canonicalUrl") or {}).get("url")
               or (c.get("clickThroughUrl") or {}).get("url")
               or c.get("link") or item.get("link") or "")
        source = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
        out.append({"title": c.get("title") or "(no title)", "url": url,
                    "source": source, "time": published})
    out.sort(key=lambda n: n["time"], reverse=True)
    return out


def run_scan(require_news=True):
    hits = []
    for q in screen_candidates():
        exch = str(q.get("fullExchangeName") or q.get("exchange") or "")
        if any(m in exch for m in OTC_MARKERS):
            continue
        price = q.get("regularMarketPrice") or 0
        change = q.get("regularMarketChangePercent") or 0
        if not (MIN_PRICE <= price <= MAX_PRICE and change >= MIN_GAIN_PCT):
            continue
        rvol, vol, avg = relative_volume(q)
        if rvol < MIN_REL_VOLUME:
            continue
        news = recent_news(q["symbol"])
        if require_news and not news:
            continue
        hits.append({
            "symbol": q["symbol"],
            "name": q.get("shortName") or q.get("longName") or "",
            "price": price, "change": change, "rvol": rvol,
            "volume": vol, "avg_volume": avg,
            "float": q.get("floatShares") or q.get("sharesOutstanding"),
            "exchange": exch, "news": news,
        })
    hits.sort(key=lambda h: h["change"], reverse=True)
    return hits


def fmt_big(n):
    if not n:
        return "-"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def print_console(hits, status):
    stamp = dt.datetime.now().strftime("%a %d %b %Y %H:%M")
    print(f"\n=== Momentum scan - {stamp} ===\n{status}")
    print(f"Rules: ${MIN_PRICE:g}-${MAX_PRICE:g} | up {MIN_GAIN_PCT:g}%+ | "
          f"RVOL {MIN_REL_VOLUME:g}x+ | news in last {NEWS_HOURS}h\n")
    if not hits:
        print("No stocks match right now.\n")
        return
    print(f"{'TICKER':<7}{'PRICE':>8}{'CHG%':>8}{'RVOL':>7}{'VOLUME':>9}  NAME")
    for h in hits:
        print(f"{h['symbol']:<7}{h['price']:>8.2f}{h['change']:>7.1f}%"
              f"{h['rvol']:>6.1f}x{fmt_big(h['volume']):>9}  {h['name'][:30]}")
        for n in h["news"][:1]:
            print(f"        > {n['title'][:90]}")
    print()


def write_report(hits, status):
    REPORT_DIR.mkdir(exist_ok=True)
    now = dt.datetime.now()
    path = REPORT_DIR / f"scan_{now:%Y%m%d_%H%M}.html"
    e = html.escape
    rows = []
    for h in hits:
        news_html = "".join(
            f'<li><a href="{e(n["url"])}" target="_blank">{e(n["title"])}</a>'
            f'<span class="src"> {e(n["source"])} - '
            f'{n["time"].astimezone():%H:%M %d %b}</span></li>'
            for n in h["news"][:MAX_NEWS_SHOWN]) or "<li class='src'>No recent news</li>"
        rows.append(f"""
        <div class="card">
          <div class="top">
            <a class="sym" href="https://finance.yahoo.com/quote/{e(h['symbol'])}" target="_blank">{e(h['symbol'])}</a>
            <span class="chg">+{h['change']:.1f}%</span>
          </div>
          <div class="name">{e(h['name'])} &middot; {e(h['exchange'])}</div>
          <div class="stats">
            <div><b>${h['price']:.2f}</b><span>Price</span></div>
            <div><b>{h['rvol']:.1f}x</b><span>Rel. volume</span></div>
            <div><b>{fmt_big(h['volume'])}</b><span>Volume</span></div>
            <div><b>{fmt_big(h['float'])}</b><span>Float/shares</span></div>
          </div>
          <ul class="news">{news_html}</ul>
        </div>""")
    body = "".join(rows) or '<p class="empty">No stocks match your rules right now.</p>'
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Momentum Scan {now:%d %b %H:%M}</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#14171c;--muted:#6b7280;--up:#0a8f4d;--line:#e5e7eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#181b21;--ink:#e8eaed;--muted:#9aa0a6;--up:#34d399;--line:#2a2e36}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:4px 0}} .meta{{color:var(--muted);font-size:13px;margin-bottom:14px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}}
.top{{display:flex;justify-content:space-between;align-items:baseline}}
.sym{{font-size:22px;font-weight:700;color:var(--ink);text-decoration:none}}
.chg{{color:var(--up);font-weight:700;font-size:18px}} .name{{color:var(--muted);font-size:13px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}}
.stats div{{display:flex;flex-direction:column}} .stats span{{color:var(--muted);font-size:11px}}
.news{{margin:0;padding-left:18px}} .news li{{margin:4px 0}} .news a{{color:var(--ink)}}
.src{{color:var(--muted);font-size:12px}} .empty{{color:var(--muted);text-align:center;padding:40px 0}}
.warn{{font-size:12px;color:var(--muted);margin-top:18px}}
</style></head><body><main>
<h1>Momentum Scan</h1>
<div class="meta">{now:%A %d %B %Y, %H:%M} &middot; {len(hits)} match(es)<br>{e(status)}<br>
Rules: ${MIN_PRICE:g}-${MAX_PRICE:g} &middot; up {MIN_GAIN_PCT:g}%+ &middot; RVOL {MIN_REL_VOLUME:g}x+ &middot; news in last {NEWS_HOURS}h</div>
{body}
<p class="warn">Data from Yahoo Finance, may be delayed ~15 min. Relative volume compares today's volume so far
with the 3-month daily average. Educational tool only - not financial advice.</p>
</main></body></html>""", encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="Small-cap momentum scanner")
    ap.add_argument("--watch", type=int, metavar="MIN",
                    help="rescan every MIN minutes and beep on new tickers")
    ap.add_argument("--no-news", action="store_true",
                    help="include matches with no recent news")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open the HTML report")
    args = ap.parse_args()

    seen = set()
    first = True
    while True:
        _, status = market_status()
        try:
            hits = run_scan(require_news=not args.no_news)
        except Exception as exc:
            print(f"Scan failed: {exc}\nTry: pip install --upgrade yfinance")
            hits = None
        if hits is not None:
            print_console(hits, status)
            report = write_report(hits, status)
            print(f"Report saved: {report}")
            new = {h["symbol"] for h in hits} - seen
            if args.watch and new and not first:
                print("\a*** NEW: " + ", ".join(sorted(new)) + " ***")
            seen |= new
            if not args.no_browser and (first or new):
                webbrowser.open(report.as_uri())
        first = False
        if not args.watch:
            break
        time.sleep(max(1, args.watch) * 60)


if __name__ == "__main__":
    main()
