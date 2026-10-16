#!/usr/bin/env python3
"""
MOMENTUM SCANNER v2
===================
Finds US small-cap momentum stocks and works out a trade plan for each one:
entry, stop-loss, two profit targets, and how many shares to buy so you only
risk a fixed % of your account.

Rules (change them in the settings block below):
  * Price $2-$20
  * Up 10%+ today (or gapping up 10%+ in pre-market)
  * Relative volume 5x+  (time-of-day adjusted, so early-session volume is judged fairly)
  * Float under 20 million shares (low supply = bigger moves)
  * News in the last 24 hours (the catalyst)
  * Flags recent share offerings / dilution filings from the SEC

Data: Yahoo Finance via the free `yfinance` library (may be ~15 min delayed)
and SEC EDGAR (free). The trade levels come from fixed rules, not
predictions. ALWAYS check the live chart in your broker before placing an order.

Educational tool only - not financial advice.

LOCAL USE (on a computer)
    pip install --upgrade yfinance
    python momentum_scanner.py          one scan, opens a report in your browser
    python momentum_scanner.py --watch 5
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
    from yfinance import EquityQuery
except ImportError:
    sys.exit("yfinance is not installed. Run:  pip install --upgrade yfinance")

# =============================================================================
# YOUR SETTINGS - change these numbers to tune the scanner
# =============================================================================
ACCOUNT_GBP = 300          # money in your trading account for this strategy
RISK_PCT = 1.0             # max % of the account you can lose on one trade

MIN_PRICE = 2.0            # dollars
MAX_PRICE = 20.0           # dollars
MIN_GAIN_PCT = 10.0        # % up today (regular hours)
MIN_GAP_PCT = 10.0         # % up in pre-market
MIN_REL_VOLUME = 5.0       # time-adjusted relative volume
MIN_PREMARKET_VOLUME = 100_000   # shares traded pre-market before we care
MAX_FLOAT = 20_000_000     # shares; set to None to switch the float filter off
NEWS_HOURS = 24            # how recent the catalyst must be

MAX_STOP_PCT = 10.0        # skip trades whose stop would be more than this % away
MIN_STOP_PCT = 1.5         # never place the stop closer than this (avoids noise stop-outs)
EXTENDED_PCT = 15.0        # price this far above VWAP = "extended, wait for a pullback"
TARGET_1_R = 2.0           # first target  = 2x your risk
TARGET_2_R = 3.0           # second target = 3x your risk

DILUTION_OFFERING_DAYS = 30    # 424B prospectus (shares being sold) within this many days
DILUTION_SHELF_DAYS = 180      # S-1/S-3/F-1/F-3 registration within this many days
# =============================================================================

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
REPORT_DIR = Path(__file__).resolve().parent / "scanner_reports"
OTC_MARKERS = ("OTC", "PNK", "Pink", "Other OTC")
FALLBACK_GBPUSD = 1.30
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "momentum-scanner personal-use scanner@users.noreply.github.com")


# ---------------------------------------------------------------------------
# Market clock
# ---------------------------------------------------------------------------
def session(now=None):
    """Return 'premarket', 'open', 'closed' plus a human message."""
    now = now or dt.datetime.now(NY)
    if now.weekday() >= 5:
        return "closed", "US market closed (weekend)."
    t = now.time()
    if dt.time(4, 0) <= t < dt.time(9, 30):
        return "premarket", "Pre-market session (opens 9:30am New York / 2:30pm UK)."
    if dt.time(9, 30) <= t < dt.time(16, 0):
        return "open", "US market is open."
    return "closed", "US market closed."


def market_status():
    """Back-compat helper used by older code: (is_open, message)."""
    s, msg = session()
    return s == "open", msg


def minutes_since_open(now=None):
    now = now or dt.datetime.now(NY)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(0.0, (now - open_t).total_seconds() / 60)


def expected_volume_fraction(minutes):
    """Rough share of a normal day's volume done after `minutes` of trading.
    Volume is front-loaded (busy open), so this curve rises faster than a
    straight line: ~10% after 15 min, ~27% after 1 hour, ~62% at midday."""
    minutes = min(max(minutes, 5.0), 390.0)
    return (minutes / 390.0) ** 0.7


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def gbpusd():
    try:
        rate = yf.Ticker("GBPUSD=X").fast_info["last_price"]
        if rate and 1.0 < float(rate) < 2.0:
            return float(rate)
    except Exception:
        pass
    return FALLBACK_GBPUSD


def float_shares(symbol, quote=None):
    """Free-float shares (falls back to shares outstanding)."""
    quote = quote or {}
    for key in ("floatShares", "sharesOutstanding"):
        if quote.get(key):
            return quote[key], key == "floatShares"
    try:
        info = yf.Ticker(symbol).get_info() or {}
        if info.get("floatShares"):
            return info["floatShares"], True
        if info.get("sharesOutstanding"):
            return info["sharesOutstanding"], False
    except Exception:
        pass
    return None, False


def parse_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=UTC)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_news(symbol, hours=NEWS_HOURS):
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        return []
    cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=hours)
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


def intraday_bars(symbol):
    """Today's 1-minute bars including pre-market, as a list of dicts."""
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
    except Exception:
        return []
    return bars_from_df(df)


def bars_from_df(df):
    bars = []
    if df is None or len(df) == 0:
        return bars
    for ts, row in df.iterrows():
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=NY)
        bars.append({"t": ts.astimezone(NY), "h": float(row["High"]), "l": float(row["Low"]),
                     "c": float(row["Close"]), "v": float(row["Volume"] or 0)})
    return bars


def session_bars(bars, which):
    if which == "premarket":
        return [b for b in bars if b["t"].time() < dt.time(9, 30)]
    return [b for b in bars if dt.time(9, 30) <= b["t"].time() < dt.time(16, 0)]


# ---------------------------------------------------------------------------
# SEC dilution check
# ---------------------------------------------------------------------------
_CIK_MAP = None


def _sec_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT,
                                               "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cik_for(symbol):
    global _CIK_MAP
    if _CIK_MAP is None:
        try:
            data = _sec_get("https://www.sec.gov/files/company_tickers.json")
            _CIK_MAP = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        except Exception:
            _CIK_MAP = {}
    return _CIK_MAP.get(symbol.upper())


def dilution_flags(symbol, today=None):
    """Return a list of warnings about recent share offerings / registrations."""
    today = today or dt.date.today()
    cik = cik_for(symbol)
    if not cik:
        return []
    try:
        data = _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    except Exception:
        return []
    recent = (data.get("filings") or {}).get("recent") or {}
    return classify_filings(recent.get("form", []), recent.get("filingDate", []), today)


def classify_filings(forms, dates, today):
    flags, seen = [], set()
    for form, date_s in zip(forms, dates):
        try:
            age = (today - dt.date.fromisoformat(date_s)).days
        except ValueError:
            continue
        f = form.upper()
        if f.startswith("424B") and age <= DILUTION_OFFERING_DAYS and "offering" not in seen:
            flags.append(f"Offering prospectus ({form}) filed {age}d ago - shares being sold")
            seen.add("offering")
        elif f.split("/")[0] in ("S-1", "S-3", "F-1", "F-3") and age <= DILUTION_SHELF_DAYS \
                and "shelf" not in seen:
            flags.append(f"{form} registration {age}d ago - can issue new shares")
            seen.add("shelf")
    return flags


# ---------------------------------------------------------------------------
# Trade plan
# ---------------------------------------------------------------------------
def trade_plan(bars, price, fx, account_gbp=ACCOUNT_GBP, risk_pct=RISK_PCT):
    """Entry / stop / targets / size from today's session bars.
    bars = the current session's 1-minute bars (dicts with h, l, c, v)."""
    plan = {"ok": False, "notes": []}
    if not bars:
        plan["notes"].append("No intraday data - check the chart manually")
        return plan

    vol = sum(b["v"] for b in bars)
    vwap = (sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / vol) if vol else price
    hod = max(b["h"] for b in bars)
    recent_low = min(b["l"] for b in bars[-10:])
    plan.update(vwap=vwap, hod=hod, recent_low=recent_low)

    entry = round(hod + max(0.01, hod * 0.002), 2)
    supports = [s for s in (vwap, recent_low) if s < price]
    if price < vwap:
        plan["notes"].append("Price is BELOW VWAP - weak, sellers in control")
    if supports:
        stop = round(max(supports) - 0.01, 2)
    else:
        stop = round(entry * (1 - MAX_STOP_PCT / 100), 2)
    min_gap = max(0.02, entry * MIN_STOP_PCT / 100)
    if entry - stop < min_gap:
        stop = round(entry - min_gap, 2)

    risk_ps = entry - stop
    risk_pct_move = risk_ps / entry * 100
    t1 = round(entry + TARGET_1_R * risk_ps, 2)
    t2 = round(entry + TARGET_2_R * risk_ps, 2)

    risk_usd = account_gbp * fx * risk_pct / 100
    max_shares_cash = math.floor(account_gbp * fx / entry)
    shares = min(math.floor(risk_usd / risk_ps), max_shares_cash)

    plan.update(entry=entry, stop=stop, t1=t1, t2=t2, risk_ps=risk_ps,
                stop_pct=risk_pct_move, shares=shares,
                risk_gbp=shares * risk_ps / fx,
                position_gbp=shares * entry / fx,
                t1_gain_gbp=shares * (t1 - entry) / fx,
                extended=price > vwap * (1 + EXTENDED_PCT / 100),
                cash_limited=shares == max_shares_cash and max_shares_cash < math.floor(risk_usd / risk_ps))

    if risk_pct_move > MAX_STOP_PCT:
        plan["notes"].append(f"SKIP - stop would be {risk_pct_move:.0f}% away (max {MAX_STOP_PCT:.0f}%)")
        return plan
    if shares < 1:
        plan["notes"].append("SKIP - account too small for this stop distance")
        return plan
    if plan["extended"]:
        plan["notes"].append(f"Extended {((price / vwap) - 1) * 100:.0f}% above VWAP - wait for a pullback near ${vwap:.2f}")
    if plan["cash_limited"]:
        plan["notes"].append("Size limited by account cash (risk is below 1%)")
    plan["ok"] = True
    return plan


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------
def _screen(query, sort_field, size=250, offset=0):
    resp = yf.screen(query, sortField=sort_field, sortAsc=False, size=size, offset=offset)
    return resp.get("quotes", []) if isinstance(resp, dict) else []


def regular_candidates():
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["percentchange", MIN_GAIN_PCT]),
        EquityQuery("btwn", ["intradayprice", MIN_PRICE, MAX_PRICE]),
    ])
    return _screen(query, "percentchange")


def premarket_candidates(pages=4):
    """Small/mid caps in the price band; filter by pre-market move client-side."""
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("btwn", ["intradayprice", MIN_PRICE * 0.6, MAX_PRICE]),
        EquityQuery("lt", ["intradaymarketcap", 2_000_000_000]),
        EquityQuery("gt", ["avgdailyvol3m", 20_000]),
    ])
    out = []
    for p in range(pages):
        try:
            batch = _screen(query, "avgdailyvol3m", size=250, offset=p * 250)
        except Exception:
            break
        out.extend(batch)
        if len(batch) < 250:
            break
    return out


def _is_otc(q):
    exch = str(q.get("fullExchangeName") or q.get("exchange") or "")
    return any(m in exch for m in OTC_MARKERS), exch


def run_scan(require_news=True, mode=None, now=None):
    """Returns (mode, hits). Each hit has stock stats, news, dilution and a trade plan."""
    now = now or dt.datetime.now(NY)
    mode = mode or session(now)[0]
    if mode == "closed":
        mode = "open"   # outside hours: show the last regular session
    fx = gbpusd()
    hits = []

    quotes = premarket_candidates() if mode == "premarket" else regular_candidates()
    for q in quotes:
        otc, exch = _is_otc(q)
        if otc:
            continue
        sym = q.get("symbol")
        if not sym:
            continue
        avg = q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day") or 0

        if mode == "premarket":
            price = q.get("preMarketPrice") or 0
            change = q.get("preMarketChangePercent") or 0
            if not (MIN_PRICE <= price <= MAX_PRICE and change >= MIN_GAP_PCT):
                continue
        else:
            price = q.get("regularMarketPrice") or 0
            change = q.get("regularMarketChangePercent") or 0
            if not (MIN_PRICE <= price <= MAX_PRICE and change >= MIN_GAIN_PCT):
                continue
            vol = q.get("regularMarketVolume") or 0
            frac = expected_volume_fraction(minutes_since_open(now)) if session(now)[0] == "open" else 1.0
            rvol = vol / (avg * frac) if avg else 0
            if rvol < MIN_REL_VOLUME:
                continue

        flt, is_float = float_shares(sym, q)
        if MAX_FLOAT and flt and flt > MAX_FLOAT:
            continue

        bars_all = intraday_bars(sym)
        bars = session_bars(bars_all, "premarket" if mode == "premarket" else "open")
        if mode == "premarket":
            vol = sum(b["v"] for b in bars)
            if vol < MIN_PREMARKET_VOLUME:
                continue
            rvol = vol / avg if avg else 0
            if bars:
                price = bars[-1]["c"]

        news = recent_news(sym)
        if require_news and not news:
            continue

        hits.append({
            "symbol": sym, "name": q.get("shortName") or q.get("longName") or "",
            "exchange": exch, "mode": mode, "price": price, "change": change,
            "rvol": rvol, "volume": vol, "avg_volume": avg,
            "float": flt, "float_is_real": is_float,
            "news": news, "dilution": dilution_flags(sym),
            "plan": trade_plan(bars, price, fx), "fx": fx,
        })
    hits.sort(key=lambda h: h["change"], reverse=True)
    return mode, hits


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def fmt_big(n):
    if not n:
        return "?"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def plan_lines(h):
    p = h["plan"]
    lines = []
    if p.get("entry"):
        lines.append(f"ENTRY  break above ${p['entry']:.2f} (high ${p['hod']:.2f})")
        lines.append(f"STOP   ${p['stop']:.2f}  (-{p['stop_pct']:.1f}%)  VWAP ${p['vwap']:.2f}")
        lines.append(f"TARGET ${p['t1']:.2f} (2:1)  /  ${p['t2']:.2f} (3:1)")
        if p.get("shares", 0) >= 1:
            lines.append(f"SIZE   {p['shares']} shares = ~GBP {p['position_gbp']:.0f}, "
                         f"risk GBP {p['risk_gbp']:.2f}, T1 gain ~GBP {p['t1_gain_gbp']:.2f}")
    lines.extend(f"! {n}" for n in p.get("notes", []))
    return lines


def summary_line(h):
    flt = fmt_big(h["float"]) + ("" if h["float_is_real"] else "*")
    if h["mode"] == "premarket":
        return (f"GAP +{h['change']:.0f}%  ${h['price']:.2f}  PM vol {fmt_big(h['volume'])} "
                f"({h['rvol']:.1f}x a normal day)  Float {flt}")
    return (f"UP +{h['change']:.0f}%  ${h['price']:.2f}  RVOL {h['rvol']:.1f}x  "
            f"Vol {fmt_big(h['volume'])}  Float {flt}")


def print_console(mode, hits, status):
    stamp = dt.datetime.now().strftime("%a %d %b %Y %H:%M")
    print(f"\n=== Momentum scan ({mode}) - {stamp} ===\n{status}\n")
    if not hits:
        print("No stocks match right now.\n")
        return
    for h in hits:
        print(f"{h['symbol']:<6} {summary_line(h)}  {h['name'][:30]}")
        for n in h["news"][:1]:
            print(f"   news: {n['title'][:90]}")
        for d in h["dilution"]:
            print(f"   DILUTION: {d}")
        for line in plan_lines(h):
            print(f"   {line}")
        print()


def write_report(mode, hits, status):
    REPORT_DIR.mkdir(exist_ok=True)
    now = dt.datetime.now()
    path = REPORT_DIR / f"scan_{now:%Y%m%d_%H%M}.html"
    e = html.escape
    cards = []
    for h in hits:
        news = "".join(f'<li><a href="{e(n["url"])}" target="_blank">{e(n["title"])}</a> '
                       f'<span class="m">{e(n["source"])}</span></li>' for n in h["news"][:3])
        dil = "".join(f'<div class="warn">&#9888; {e(d)}</div>' for d in h["dilution"])
        plan = "<br>".join(e(x) for x in plan_lines(h))
        cards.append(f"""<div class="card"><div class="top">
<a class="sym" href="https://finance.yahoo.com/quote/{e(h['symbol'])}" target="_blank">{e(h['symbol'])}</a>
<span class="chg">+{h['change']:.1f}%</span></div>
<div class="m">{e(h['name'])} &middot; {e(summary_line(h))}</div>{dil}
<pre>{plan}</pre><ul>{news}</ul></div>""")
    body = "".join(cards) or '<p class="m">No stocks match your rules right now.</p>'
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Momentum Scan</title>
<style>body{{font:15px system-ui;margin:0;background:#f5f6f8;color:#14171c}}main{{max-width:760px;margin:auto;padding:16px}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:12px 0}}
.top{{display:flex;justify-content:space-between}}.sym{{font-size:22px;font-weight:700;color:inherit}}
.chg{{color:#0a8f4d;font-weight:700}}.m{{color:#6b7280;font-size:13px}}.warn{{color:#b45309;font-weight:600;margin-top:6px}}
pre{{background:#f3f4f6;padding:10px;border-radius:8px;white-space:pre-wrap;font-size:13px}}</style></head>
<body><main><h1>Momentum Scan ({e(mode)})</h1><p class="m">{now:%d %b %Y %H:%M} &middot; {e(status)}<br>
Account GBP {ACCOUNT_GBP} &middot; risk {RISK_PCT:g}% per trade &middot; data may be ~15 min delayed.
Levels are rule-based, not advice - check the live chart first.</p>{body}</main></body></html>""",
                    encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="Small-cap momentum scanner v2")
    ap.add_argument("--watch", type=int, metavar="MIN")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    seen, first = set(), True
    while True:
        _, status = session()
        try:
            mode, hits = run_scan(require_news=not args.no_news)
        except Exception as exc:
            print(f"Scan failed: {exc}")
            mode, hits = None, None
        if hits is not None:
            print_console(mode, hits, status)
            report = write_report(mode, hits, status)
            print(f"Report saved: {report}")
            new = {h["symbol"] for h in hits} - seen
            seen |= new
            if not args.no_browser and (first or new):
                webbrowser.open(report.as_uri())
        first = False
        if not args.watch:
            break
        time.sleep(max(1, args.watch) * 60)


if __name__ == "__main__":
    main()
