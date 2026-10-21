#!/usr/bin/env python3
"""
Dip Tracker — scans the broad US stock market for "dips" and refreshes on an
interval. Two independent signals are tracked:

  1. Intraday drops: today's move vs. yesterday's close.
  2. Drawdowns: how far a stock has pulled back from its trailing 52-week high.

Data comes from Yahoo Finance via yfinance. It is NOT tick-by-tick real-time —
during market hours it typically lags by roughly a minute or so, which is fine
for a dip scanner but not for trading execution.

Universe: S&P 500 + S&P 400 (mid-cap) + S&P 600 (small-cap) = the S&P
Composite 1500, scraped from Wikipedia and cached locally. This is used as a
practical stand-in for "the whole market" — there's no free feed that covers
literally every US-listed ticker.
"""

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

# A handful of tickers routinely fail on Yahoo (delisted/renamed/moved
# exchange). scan() already skips any ticker with no data, so silence
# yfinance's per-ticker error spam instead of alarming the user with it.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TICKER_CACHE = os.path.join(SCRIPT_DIR, "tickers_cache.json")
ENRICH_CACHE = os.path.join(SCRIPT_DIR, "enrichment_cache.json")
DEFAULT_LOG = os.path.join(SCRIPT_DIR, "dip_alerts.csv")

WIKI_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) dip-tracker/1.0"}

WIKI_SOURCES = {
    "sp500": ["https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"],
    "sp1500": [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
    ],
}


def fetch_tickers(universe: str) -> list[str]:
    symbols: set[str] = set()
    for url in WIKI_SOURCES[universe]:
        resp = requests.get(url, headers=WIKI_HEADERS, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        table = tables[0]
        col = "Symbol" if "Symbol" in table.columns else table.columns[0]
        for sym in table[col].tolist():
            sym = str(sym).strip().replace(".", "-")
            if sym:
                symbols.add(sym)
    return sorted(symbols)


def load_tickers(universe: str, max_age_days: int = 7, force_refresh: bool = False) -> list[str]:
    if not force_refresh and os.path.exists(TICKER_CACHE):
        with open(TICKER_CACHE, "r") as f:
            cache = json.load(f)
        if cache.get("universe") == universe:
            fetched_at = datetime.fromisoformat(cache["fetched_at"])
            if datetime.now() - fetched_at < timedelta(days=max_age_days):
                return cache["tickers"]

    print(f"Fetching {universe} ticker list from Wikipedia...")
    tickers = fetch_tickers(universe)
    with open(TICKER_CACHE, "w") as f:
        json.dump({
            "universe": universe,
            "fetched_at": datetime.now().isoformat(),
            "tickers": tickers,
        }, f)
    print(f"Loaded {len(tickers)} tickers.")
    return tickers


def scan(tickers: list[str], min_price: float) -> pd.DataFrame:
    print(f"Scanning {len(tickers)} tickers... (normally a few seconds; can take much longer if Yahoo is rate-limiting)", flush=True)
    data = yf.download(
        tickers,
        period="1y",
        interval="1d",
        group_by="ticker",
        threads=True,
        progress=False,
        auto_adjust=False,
    )

    rows = []
    for tk in tickers:
        try:
            df = data[tk]
        except KeyError:
            continue
        closes = df["Close"].dropna()
        if len(closes) < 2:
            continue
        highs = df["High"].dropna()
        if highs.empty:
            continue

        current = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        year_high = float(highs.max())

        volumes = df["Volume"].dropna()
        volume = float(volumes.iloc[-1]) if not volumes.empty else 0.0
        # trailing 20-session average volume, excluding today, to spot unusual activity
        prior_volumes = volumes.iloc[:-1].tail(20)
        avg_volume = float(prior_volumes.mean()) if len(prior_volumes) >= 5 else float("nan")
        volume_ratio = (volume / avg_volume) if avg_volume and avg_volume > 0 else float("nan")

        if current < min_price or prev_close <= 0 or year_high <= 0:
            continue

        intraday_pct = (current - prev_close) / prev_close * 100.0
        drawdown_pct = (current - year_high) / year_high * 100.0

        rows.append({
            "ticker": tk,
            "price": current,
            "intraday_pct": intraday_pct,
            "drawdown_pct": drawdown_pct,
            "year_high": year_high,
            "volume": volume,
            "volume_ratio": volume_ratio,
        })

    return pd.DataFrame(rows)


def load_enrich_cache() -> dict:
    if os.path.exists(ENRICH_CACHE):
        try:
            with open(ENRICH_CACHE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_enrich_cache(cache: dict):
    with open(ENRICH_CACHE, "w") as f:
        json.dump(cache, f)


def fetch_one_enrichment(ticker: str) -> dict:
    """Analyst price target + last/next earnings date for one ticker.
    These hit a separately rate-limited Yahoo endpoint (unlike the bulk
    price download), so callers should cache this and only call it for a
    small, already-filtered set of tickers."""
    result = {
        "target_mean": None, "target_high": None, "target_low": None,
        "num_analysts": None, "recommendation": None,
        "last_earnings": None, "next_earnings": None,
        "fetched_at": datetime.now().isoformat(),
    }
    t = yf.Ticker(ticker)

    for attempt in range(2):
        try:
            info = t.info
            result["target_mean"] = info.get("targetMeanPrice")
            result["target_high"] = info.get("targetHighPrice")
            result["target_low"] = info.get("targetLowPrice")
            result["num_analysts"] = info.get("numberOfAnalystOpinions")
            result["recommendation"] = info.get("recommendationKey")
            break
        except Exception:
            if attempt == 0:
                time.sleep(3)

    for attempt in range(2):
        try:
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                now = pd.Timestamp.now(tz=ed.index.tz)
                past = ed.index[ed.index <= now]
                future = ed.index[ed.index > now]
                if len(past):
                    result["last_earnings"] = past.max().strftime("%Y-%m-%d")
                if len(future):
                    result["next_earnings"] = future.min().strftime("%Y-%m-%d")
            break
        except Exception:
            if attempt == 0:
                time.sleep(3)

    return result


def enrich_tickers(tickers: list[str], cache: dict, max_age_hours: float = 20.0, delay: float = 0.3) -> dict:
    now = datetime.now()
    out = {}
    dirty = False
    for tk in tickers:
        entry = cache.get(tk)
        if entry:
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
            if now - fetched_at < timedelta(hours=max_age_hours):
                out[tk] = entry
                continue
        entry = fetch_one_enrichment(tk)
        cache[tk] = entry
        out[tk] = entry
        dirty = True
        time.sleep(delay)
    if dirty:
        save_enrich_cache(cache)
    return out


def days_until(date_str):
    if not date_str:
        return None
    return (datetime.strptime(date_str, "%Y-%m-%d").date() - datetime.now().date()).days


def format_extras(ticker: str, row: pd.Series, enrichment: dict, volume_threshold: float, earnings_window: int, earnings_lookahead: int):
    """Returns (volume_str, target_str, earnings_str) for one row."""
    vr = row.get("volume_ratio")
    if pd.notna(vr):
        vol_str = f"{vr:.1f}x" + (" *" if vr >= volume_threshold else "")
    else:
        vol_str = "n/a"

    e = enrichment.get(ticker) or {}
    target = e.get("target_mean")
    if target:
        upside = (target - row["price"]) / row["price"] * 100.0
        target_str = f"${target:.0f} ({upside:+.0f}%)"
    else:
        target_str = "n/a"

    notes = []
    last_e = e.get("last_earnings")
    if last_e is not None:
        d = days_until(last_e)
        if d is not None and -earnings_window <= d <= 0:
            notes.append(f"earnings {abs(d)}d ago")
    next_e = e.get("next_earnings")
    if next_e is not None:
        d = days_until(next_e)
        if d is not None and 0 <= d <= earnings_lookahead:
            notes.append(f"earnings in {d}d")
    earnings_str = ", ".join(notes) if notes else "-"

    return vol_str, target_str, earnings_str


def make_table(title: str, df: pd.DataFrame, pct_col: str, top: int, enrichment: dict,
               volume_threshold: float, earnings_window: int, earnings_lookahead: int):
    df = df.sort_values(pct_col).head(top)
    if HAVE_RICH:
        table = Table(title=title, box=box.SIMPLE_HEAVY, title_style="bold")
        table.add_column("Ticker", style="bold")
        table.add_column("Price", justify="right")
        table.add_column("Intraday %", justify="right")
        table.add_column("From 52W High %", justify="right")
        table.add_column("Vol", justify="right")
        table.add_column("Analyst Target", justify="right")
        table.add_column("Earnings", justify="left")
        for _, r in df.iterrows():
            vol_str, target_str, earnings_str = format_extras(
                r["ticker"], r, enrichment, volume_threshold, earnings_window, earnings_lookahead)
            table.add_row(
                r["ticker"],
                f"${r['price']:.2f}",
                f"[{'red' if r['intraday_pct'] < 0 else 'green'}]{r['intraday_pct']:+.2f}%[/]",
                f"[{'red' if r['drawdown_pct'] < 0 else 'green'}]{r['drawdown_pct']:+.2f}%[/]",
                f"[yellow]{vol_str}[/]" if "*" in vol_str else vol_str,
                target_str,
                f"[cyan]{earnings_str}[/]" if earnings_str != "-" else earnings_str,
            )
        return table
    else:
        out = [title]
        for _, r in df.iterrows():
            vol_str, target_str, earnings_str = format_extras(
                r["ticker"], r, enrichment, volume_threshold, earnings_window, earnings_lookahead)
            out.append(
                f"  {r['ticker']:<6} ${r['price']:>8.2f}  intraday {r['intraday_pct']:+6.2f}%  "
                f"from52wHigh {r['drawdown_pct']:+6.2f}%  vol {vol_str:>6}  target {target_str:>12}  {earnings_str}"
            )
        return "\n".join(out)


def log_alerts(log_path: str, df: pd.DataFrame, intraday_thr: float, drawdown_thr: float, seen: set):
    new_rows = []
    now = datetime.now().isoformat(timespec="seconds")
    for _, r in df.iterrows():
        if r["intraday_pct"] <= intraday_thr:
            key = (r["ticker"], "intraday")
            if key not in seen:
                seen.add(key)
                new_rows.append([now, r["ticker"], "intraday_drop", f"{r['intraday_pct']:.2f}", f"{r['price']:.2f}"])
        if r["drawdown_pct"] <= drawdown_thr:
            key = (r["ticker"], "drawdown")
            if key not in seen:
                seen.add(key)
                new_rows.append([now, r["ticker"], "drawdown_from_high", f"{r['drawdown_pct']:.2f}", f"{r['price']:.2f}"])

    if not new_rows:
        return

    write_header = not os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "ticker", "type", "pct", "price"])
        writer.writerows(new_rows)


def run(args):
    tickers = load_tickers(args.universe, force_refresh=args.refresh_tickers)
    seen_alerts: set = set()
    console = Console() if HAVE_RICH else None
    enrich_cache = load_enrich_cache() if not args.no_enrich else {}

    def do_cycle():
        start = time.time()
        df = scan(tickers, args.min_price)
        elapsed = time.time() - start

        if df.empty:
            print("No data returned this cycle (market may be closed or rate-limited).")
            return

        intraday_dips = df[df["intraday_pct"] <= args.intraday_threshold].sort_values("intraday_pct").head(args.top)
        drawdown_dips = df[df["drawdown_pct"] <= args.drawdown_threshold].sort_values("drawdown_pct").head(args.top)

        log_alerts(args.log, df, args.intraday_threshold, args.drawdown_threshold, seen_alerts)

        enrichment = {}
        if not args.no_enrich:
            shown_tickers = sorted(set(intraday_dips["ticker"]) | set(drawdown_dips["ticker"]))
            if shown_tickers:
                enrichment = enrich_tickers(shown_tickers, enrich_cache)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"Dip Tracker | {stamp} | {len(tickers)} tickers scanned in {elapsed:.1f}s | {len(intraday_dips)} intraday dips, {len(drawdown_dips)} deep pullbacks shown"

        t1 = make_table(f"Biggest Intraday Drops (<= {args.intraday_threshold:.1f}%)", intraday_dips, "intraday_pct", args.top,
                         enrichment, args.volume_threshold, args.earnings_window, args.earnings_lookahead)
        t2 = make_table(f"Deepest Pullbacks from 52W High (<= {args.drawdown_threshold:.1f}%)", drawdown_dips, "drawdown_pct", args.top,
                         enrichment, args.volume_threshold, args.earnings_window, args.earnings_lookahead)

        if HAVE_RICH:
            console.print(header, style="bold")
            console.rule(characters="-")
            console.print(t1)
            console.print(t2)
        else:
            print(header)
            print(t1)
            print(t2)

    if args.once:
        do_cycle()
        return

    print(f"Starting live scan every {args.interval}s. Ctrl+C to stop. Alerts logged to {args.log}")
    try:
        while True:
            if HAVE_RICH:
                os.system("cls" if os.name == "nt" else "clear")
            do_cycle()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def parse_args():
    p = argparse.ArgumentParser(description="Track live dips across the US stock market.")
    p.add_argument("--universe", choices=["sp500", "sp1500"], default="sp1500",
                    help="sp500 = S&P 500 only (~500 tickers, faster). sp1500 = S&P 500+400+600 (~1500 tickers, broader). Default: sp1500")
    p.add_argument("--intraday-threshold", type=float, default=-3.0,
                    help="Flag stocks whose today-vs-yesterday-close move is at or below this %% (negative number). Default -3.0")
    p.add_argument("--drawdown-threshold", type=float, default=-10.0,
                    help="Flag stocks whose price is at or below this %% off their trailing 52-week high (negative number). Default -10.0")
    p.add_argument("--interval", type=int, default=300, help="Seconds between refreshes. Default 300 (5 min)")
    p.add_argument("--once", action="store_true", help="Run a single scan and exit instead of looping")
    p.add_argument("--top", type=int, default=25, help="Rows to show per table. Default 25")
    p.add_argument("--min-price", type=float, default=5.0, help="Ignore stocks priced below this. Default 5.0")
    p.add_argument("--log", default=DEFAULT_LOG, help=f"CSV path for new alerts. Default {DEFAULT_LOG}")
    p.add_argument("--refresh-tickers", action="store_true", help="Force re-fetch of the ticker universe from Wikipedia")
    p.add_argument("--volume-threshold", type=float, default=2.0,
                    help="Flag today's volume as a spike when it's at least this many times the trailing 20-day average. Default 2.0")
    p.add_argument("--earnings-window", type=int, default=5,
                    help="Note 'earnings Nd ago' if the ticker reported within this many days. Default 5")
    p.add_argument("--earnings-lookahead", type=int, default=5,
                    help="Note 'earnings in Nd' if the ticker reports within this many days. Default 5")
    p.add_argument("--no-enrich", action="store_true",
                    help="Skip analyst target / earnings-date lookups (faster, avoids the separate Yahoo rate limit on those endpoints)")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
