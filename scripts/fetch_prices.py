#!/usr/bin/env python3
"""Fetch daily closing prices and trading volumes for a Korean-listed stock
(KOSPI or KOSDAQ) over a date range and accumulate the rows into a CSV file
under docs/.

Data source: Yahoo Finance chart API (no API key required). Korean tickers are
mapped to Yahoo symbols with the market suffix: KOSPI -> ".KS", KOSDAQ -> ".KQ"
(e.g. Samsung Electronics 005930 on KOSPI becomes "005930.KS").

Usage:
    python3 scripts/fetch_prices.py --ticker 005930 --market kospi \
        --start 2026-01-01 --end 2026-07-23

Output (accumulated, one file per symbol):
    docs/<TICKER>_<MARKET>_prices.csv
        columns: date, close, volume, adjclose, symbol, market

Re-running with a new/overlapping range merges new rows into the existing file
(deduplicated by date), so data accumulates across runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS_DIR = os.path.join(ROOT, "docs")

MARKET_SUFFIX = {"kospi": ".KS", "kosdaq": ".KQ"}
CSV_FIELDS = ["date", "close", "volume", "adjclose", "symbol", "market"]
USER_AGENT = "Mozilla/5.0 (Linux; Android 15) samsung-price/1.0"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d&includeAdjustedClose=true"


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_date(s: str, name: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        die(f"--{name} must be YYYY-MM-DD, got {s!r}")


def to_unix(dt: datetime) -> int:
    # Yahoo expects seconds since epoch; treat the date as start/end of day UTC.
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def fetch_chart(symbol: str, start_ts: int, end_ts: int, retries: int = 3) -> dict:
    url = CHART_URL.format(sym=symbol, p1=start_ts, p2=end_ts)
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            # 429 / 5xx are transient; back off. 4xx (other than 429) -> give up.
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(2 * attempt)
                continue
            die(f"Yahoo Finance request failed for {symbol}: {last_err}")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = str(e)
            time.sleep(2 * attempt)
            continue
    die(f"Yahoo Finance request failed for {symbol} after {retries} tries: {last_err}")


def extract_rows(payload: dict, symbol: str, market: str) -> list[dict]:
    """Turn the Yahoo chart response into flat row dicts keyed by date."""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjclose = result["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
    except (KeyError, IndexError, TypeError):
        die("unexpected Yahoo Finance response shape (no chart data)")

    rows: list[dict] = []
    for i, ts in enumerate(timestamps):
        close = quote["close"][i] if i < len(quote["close"]) else None
        volume = quote["volume"][i] if i < len(quote["volume"]) else None
        adj = adjclose[i] if i < len(adjclose) else None
        # Skip days with no close (holidays / non-trading gaps Yahoo may pad).
        if close is None:
            continue
        rows.append(
            {
                "date": time.strftime("%Y-%m-%d", time.gmtime(ts)),
                "close": int(close),
                "volume": int(volume) if volume is not None else "",
                "adjclose": round(adj, 2) if adj is not None else "",
                "symbol": symbol,
                "market": market,
            }
        )
    return rows


def load_existing(path: str) -> dict[str, dict]:
    """Read existing CSV (if any) into {date: row} for de-dup merge."""
    existing: dict[str, dict] = {}
    if not os.path.exists(path):
        return existing
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing[row["date"]] = row
    return existing


def merge_and_write(path: str, existing: dict[str, dict], new_rows: list[dict]) -> int:
    """Merge new rows into existing (new wins on date collision) and write back.

    Returns the number of rows actually added or updated.
    """
    changed = 0
    for row in new_rows:
        prev = existing.get(row["date"])
        if prev != row:
            existing[row["date"]] = row
            changed += 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for date in sorted(existing):
            writer.writerow(existing[date])
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True, help="Korean stock ticker, e.g. 005930 (Samsung Electronics)")
    ap.add_argument("--market", required=True, choices=sorted(MARKET_SUFFIX), help="Exchange: kospi or kosdaq")
    ap.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    ap.add_argument("--docs-dir", default=DOCS_DIR, help=f"Output directory (default: {DOCS_DIR})")
    args = ap.parse_args()

    start_dt = parse_date(args.start, "start")
    end_dt = parse_date(args.end, "end")
    if end_dt < start_dt:
        die("--end must be on or after --start")

    suffix = MARKET_SUFFIX[args.market]
    symbol = f"{args.ticker}{suffix}"
    # Yahoo period2 is exclusive-ish for the day boundary; add a day's worth of
    # seconds so the requested end date is included.
    start_ts = to_unix(start_dt)
    end_ts = to_unix(end_dt) + 86399

    payload = fetch_chart(symbol, start_ts, end_ts)
    rows = extract_rows(payload, symbol, args.market)
    if not rows:
        die(f"no trading data returned for {symbol} between {args.start} and {args.end}")

    out_path = os.path.join(args.docs_dir, f"{args.ticker}_{args.market}_prices.csv")
    existing = load_existing(out_path)
    changed = merge_and_write(out_path, existing, rows)

    print(f"{symbol} ({args.market}): fetched {len(rows)} trading days "
          f"({rows[0]['date']} .. {rows[-1]['date']})")
    print(f"{out_path}: {changed} row(s) added/updated, "
          f"{len(existing)} total row(s) now stored")


if __name__ == "__main__":
    main()
