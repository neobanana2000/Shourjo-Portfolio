#!/usr/bin/env python3
"""
Keeps data/*.csv current, then rebuilds data/series.json.
Run by .github/workflows/charts.yml after each US close.

TWO SOURCES, IN ORDER
---------------------
1. Yahoo (primary). Returns a date RANGE, so every run re-fetches the trailing
   WINDOW_DAYS and merges by date. That means the data repairs itself: a run
   that failed on Friday is filled in on Monday, a revised close overwrites the
   stale one, and dividend/split adjustments propagate into AdjClose. Yahoo has
   no official API and does throttle datacenter IPs, so it is expected to fail
   sometimes -- that is what step 2 is for.

2. Finnhub (fallback, per ticker). Only ever knows "right now", so it can write
   today's row but can never recover a missed day. Used only where Yahoo came
   back empty, which keeps a Yahoo outage from costing a day entirely.

The merge is what makes this robust: rows are keyed by date, so re-running is
idempotent and a partial failure never corrupts what is already there.
"""
import csv, json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

SITE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(SITE, "data")
KEY = os.environ.get("FINNHUB_KEY", "")

WINDOW_DAYS = 30          # how far back each run re-fetches and repairs
HDR = ["Date", "Open", "High", "Low", "Close", "AdjClose", "Volume"]

# Yahoo rejects urllib's default User-Agent, so present as a browser.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

INDEXES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "EFA": "MSCI EAFE",
           "EWJ": "Japan", "VGK": "Europe", "ACWI": "World"}
COLORS = {"SPY": "#2a78d6", "QQQ": "#7b4fc9", "EFA": "#eb6834",
          "EWJ": "#c02b2b", "VGK": "#1baf7a", "ACWI": "#eda100"}


def get_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fmt(v):
    return "" if v is None else f"{float(v):.4f}"


# ---------------------------------------------------------------- sources
def from_yahoo(ticker, p1, p2):
    """{date: row} for the window, or None if Yahoo gave us nothing usable."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={p1}&period2={p2}&interval=1d")
    try:
        j = get_json(url, {"User-Agent": UA})
    except Exception as e:                      # HTTPError, URLError, timeout, bad JSON
        return None, f"{type(e).__name__}"

    try:
        res = j["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    except (KeyError, IndexError, TypeError):
        return None, "unexpected shape"

    out = {}
    for i, t in enumerate(ts):
        c = q["close"][i] if i < len(q.get("close", [])) else None
        if c is None:
            continue
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        g = lambda k: (q[k][i] if i < len(q.get(k, [])) and q[k][i] is not None else c)
        a = adj[i] if i < len(adj) and adj[i] is not None else c
        v = q["volume"][i] if i < len(q.get("volume", [])) and q["volume"][i] is not None else None
        out[d] = [d, fmt(g("open")), fmt(g("high")), fmt(g("low")),
                  fmt(c), fmt(a), "" if v is None else str(int(v))]
    return (out, "ok") if out else (None, "empty")


def from_finnhub(ticker, today):
    """Just today's row, or None. Cannot backfill -- that is the whole point of
    preferring Yahoo."""
    if not KEY:
        return None
    try:
        q = get_json(f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={KEY}")
    except Exception:
        return None
    c = q.get("c")
    if not c or c <= 0:
        return None
    o, h, l = q.get("o") or c, q.get("h") or c, q.get("l") or c
    return {today: [today, fmt(o), fmt(h), fmt(l), fmt(c), fmt(c), ""]}


# ---------------------------------------------------------------- csv io
def read_csv(path):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    return {r[0]: r for r in rows[1:] if r and r[0]}


def write_csv(path, by_date):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HDR)
        for d in sorted(by_date):
            w.writerow(by_date[d])


def us_today():
    """Date in US/Eastern. The runner is UTC; a cron firing near midnight UTC
    would otherwise stamp tomorrow onto today's close."""
    return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- main
def main():
    if not os.path.isdir(DATA):
        sys.exit(f"no data directory at {DATA}")

    today = us_today()
    now = int(time.time())
    p1, p2 = now - WINDOW_DAYS * 86400, now + 86400

    tickers = sorted(f[:-4] for f in os.listdir(DATA) if f.endswith(".csv"))
    print(f"{len(tickers)} tickers | window {WINDOW_DAYS}d | today {today}\n")

    stat = {"yahoo": 0, "finnhub": 0, "none": 0}
    changed = repaired = 0

    for tk in tickers:
        path = os.path.join(DATA, f"{tk}.csv")
        have = read_csv(path)
        before = len(have)

        fresh, why = from_yahoo(tk, p1, p2)
        src = "yahoo"
        if fresh is None:
            fresh = from_finnhub(tk, today)
            src = "finnhub" if fresh else "none"
            print(f"  {tk}: yahoo {why} -> {src}")

        stat[src] += 1
        if not fresh:
            continue

        # merge by date: new rows fill gaps, existing dates get corrected values
        touched = sum(1 for d, row in fresh.items() if have.get(d) != row)
        if touched:
            gaps = sum(1 for d in fresh if d not in have)
            have.update(fresh)
            write_csv(path, have)
            changed += 1
            repaired += max(0, touched - gaps)

        time.sleep(0.5)                      # polite to both APIs, inside Finnhub's 60/min

    print(f"\nsources: yahoo {stat['yahoo']}, finnhub {stat['finnhub']}, "
          f"failed {stat['none']} | files changed {changed} | rows corrected {repaired}")

    if stat["none"] == len(tickers):
        sys.exit("every ticker failed on both sources -- leaving series.json alone")

    rebuild_series()


def rebuild_series():
    """Regenerate the single file both charts load."""
    holdings = {}
    with open(os.path.join(SITE, "portfolio.csv")) as f:
        for r in csv.DictReader(f):
            holdings[r["ticker"]] = {"buy": r["buy_date"], "buyPx": float(r["buy_price"]),
                                     "sh": float(r["shares"])}

    with open(os.path.join(SITE, "performance.html")) as f:
        m = re.search(r"const PERF = (\{.*?\});", f.read(), re.S)
    names = {p["t"]: p for p in json.loads(m.group(1))["positions"]} if m else {}

    closes = {}
    for fn in sorted(os.listdir(DATA)):
        if fn.endswith(".csv"):
            rows = read_csv(os.path.join(DATA, fn))
            closes[fn[:-4]] = {d: float(r[4]) for d, r in rows.items() if r[4]}

    dates = sorted({d for c in closes.values() for d in c})
    series, meta = {}, {}
    for tk in sorted(closes):
        c = closes[tk]
        series[tk] = [round(c[d], 4) if d in c else None for d in dates]
        if tk in INDEXES:
            meta[tk] = {"kind": "index", "label": INDEXES[tk], "color": COLORS[tk]}
        else:
            h, nm = holdings.get(tk, {}), names.get(tk, {})
            meta[tk] = {"kind": "holding", "label": nm.get("n", tk),
                        "buy": h.get("buy"), "buyPx": h.get("buyPx"), "sh": h.get("sh"),
                        "sector": nm.get("s", ""), "region": nm.get("r", "")}

    out = os.path.join(DATA, "series.json")
    with open(out, "w") as f:
        json.dump({"asOf": dates[-1], "dates": dates, "meta": meta, "series": series},
                  f, separators=(",", ":"))
    print(f"series.json: {len(dates)} dates through {dates[-1]}, "
          f"{len(series)} series, {os.path.getsize(out)//1024} KB")


if __name__ == "__main__":
    main()
