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

After each run the script also computes three volume-weighted average prices
(VWAP = sum(close * volume) / sum(volume)) from the accumulated data, using
CALENDAR-day windows (not business days; only trading days that fall inside each
window count). VWAPs are computed for EVERY trading day in the dataset, each
anchored at that day (trailing window ending on it):
    7-day    : trailing 7 calendar days   [ref-6d .. ref]
    1-month  : trailing 1 calendar month  [ref-1mo .. ref]
    2-month  : trailing 2 calendar months [ref-2mo .. ref]
These are written (one row per trading day) to:
    docs/<TICKER>_<MARKET>_vwap.csv
        columns: as_of, vwap_7d, days_7d, vwap_1m, days_1m, vwap_2m, days_2m,
                 vwap_mean
where vwap_mean is the arithmetic mean of the available VWAPs (only non-empty
VWAPs are averaged; empty when all three are empty).

After the VWAP CSV is updated the script also computes a PSU (Performance Share
Unit) evaluation for every trading day that has a VWAP mean, comparing that
day's VWAP mean against the VWAP mean of a pinned anchor date
(PSU_PINNED_DATE = 2025-10-13) to get a percentage difference (diff_ratio). A
tiered multiplier is derived from the ratio:
    pct >= 100 -> x2.0 | >= 80 -> x1.7 | >= 60 -> x1.3 |
    >= 40 -> x1.0 | >= 20 -> x0.5 | else x0.0
and applied to a per-class stock base (cl1/cl2 = 200 shares, cl3/cl4 = 300
shares), valued at the day's closing price. These are written (one row per
trading day) to a separate file:
    docs/<TICKER>_<MARKET>_psu.csv
        columns: as_of, close, pinned_date, pinned_vwap_mean, vwap_mean,
                 diff_ratio, multiplier, cl12_base, cl12_stocks, cl12_eval,
                 cl34_base, cl34_stocks, cl34_eval
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS_DIR = os.path.join(ROOT, "data")

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


VWAP_FIELDS = ["as_of", "vwap_7d", "days_7d", "vwap_1m", "days_1m", "vwap_2m", "days_2m", "vwap_mean"]


def subtract_months(d: datetime, months: int) -> datetime:
    """Subtract a number of calendar months from d, clamping the day for short
    months (e.g. Mar 31 - 1 month -> Feb 28/29)."""
    m = d.month - months
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def _row_volume(row: dict) -> int | None:
    v = row.get("volume", "")
    if v == "" or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def compute_vwap(rows: list[dict], window_start: datetime, ref_date: datetime) -> tuple[float | None, int]:
    """Volume-weighted average close over trading days whose date falls inside
    the calendar window [window_start, ref_date] (inclusive on both ends).

    Days with missing/zero volume are skipped for the price*volume sum but still
    counted toward the number of trading days in the window.

    Returns (vwap_or_None, n_trading_days_in_window). vwap is None when no
    trading day in the window had a usable volume.
    """
    w_start = window_start.strftime("%Y-%m-%d")
    w_end = ref_date.strftime("%Y-%m-%d")
    pv = 0.0
    vol_sum = 0
    n_days = 0
    for row in rows:
        date = row["date"]
        if date < w_start or date > w_end:
            continue
        n_days += 1
        vol = _row_volume(row)
        if not vol:
            continue
        pv += float(row["close"]) * vol
        vol_sum += vol
    vwap = (pv / vol_sum) if vol_sum else None
    return vwap, n_days


def compute_vwaps(sorted_rows: list[dict], ref_date: datetime) -> dict:
    """Compute the 7-day / 1-month / 2-month calendar-window VWAPs for a single
    anchor day.

    Window definitions (calendar days, anchored at ref_date = the trading day
    whose VWAP we want):
        7-day    : [ref_date - 6 days, ref_date]   (7 calendar days)
        1-month  : [ref_date - 1 calendar month, ref_date]
        2-month  : [ref_date - 2 calendar months, ref_date]
    """
    win_7d = ref_date - timedelta(days=6)
    win_1m = subtract_months(ref_date, 1)
    win_2m = subtract_months(ref_date, 2)

    vwap_7d, n7 = compute_vwap(sorted_rows, win_7d, ref_date)
    vwap_1m, n1 = compute_vwap(sorted_rows, win_1m, ref_date)
    vwap_2m, n2 = compute_vwap(sorted_rows, win_2m, ref_date)

    r7 = round(vwap_7d, 2) if vwap_7d is not None else None
    r1 = round(vwap_1m, 2) if vwap_1m is not None else None
    r2 = round(vwap_2m, 2) if vwap_2m is not None else None
    available = [v for v in (r7, r1, r2) if v is not None]
    vwap_mean = round(sum(available) / len(available), 2) if available else None

    return {
        "as_of": ref_date.strftime("%Y-%m-%d"),
        "vwap_7d": r7 if r7 is not None else "",
        "days_7d": n7,
        "vwap_1m": r1 if r1 is not None else "",
        "days_1m": n1,
        "vwap_2m": r2 if r2 is not None else "",
        "days_2m": n2,
        "vwap_mean": vwap_mean if vwap_mean is not None else "",
    }


def load_existing_as_of(path: str) -> set[str]:
    """Read just the set of as_of dates already present in the VWAP CSV.
    Used to skip days that were already computed in a previous run.
    """
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["as_of"] for row in csv.DictReader(f)}


def write_vwap_summaries(path: str, summaries: list[dict]) -> None:
    """Merge VWAP rows (one per trading day) into the per-symbol VWAP CSV,
    deduplicated by as_of date (newest input wins), sorted chronologically.
    """
    existing: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["as_of"]] = row
    for summary in summaries:
        existing[summary["as_of"]] = summary
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VWAP_FIELDS)
        writer.writeheader()
        for as_of in sorted(existing):
            writer.writerow(existing[as_of])


# ---------------------------------------------------------------------------
# PSU (Performance Share Unit) evaluation
#
# Mirrors the logic in docs/app.js (renderPsu / psuMultiplier): the VWAP mean of
# the latest trading day is compared against the VWAP mean of a pinned anchor
# date (PSU_PINNED_DATE) to get a percentage difference (diff_ratio). A tiered
# multiplier is derived from that ratio, then applied to a per-class stock base
# (cl1/cl2 = 200 shares, cl3/cl4 = 300 shares) and valued at the day's closing
# price. One row per trading day is written to a dedicated PSU CSV (separate from
# the prices and VWAP CSVs), accumulated incrementally and deduplicated by
# as_of, so the history of grant evaluations is preserved across runs.
# ---------------------------------------------------------------------------
PSU_PINNED_DATE = "2025-10-13"
PSU_BASE = {"cl12": 200, "cl34": 300}
PSU_FIELDS = [
    "as_of", "close",
    "pinned_date", "pinned_vwap_mean", "vwap_mean",
    "diff_ratio", "multiplier",
    "cl12_base", "cl12_stocks", "cl12_eval",
    "cl34_base", "cl34_stocks", "cl34_eval",
]


def psu_multiplier(pct: float) -> float:
    """Tiered PSU multiplier from the VWAP-mean difference ratio (percent)."""
    if pct >= 100:
        return 2.0
    if pct >= 80:
        return 1.7
    if pct >= 60:
        return 1.3
    if pct >= 40:
        return 1.0
    if pct >= 20:
        return 0.5
    return 0.0


def _fnum(v) -> float | None:
    """Parse a CSV cell into float, or None when empty/invalid."""
    if v == "" or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_psu_row(as_of: str, close: float, pinned_vwap: float, vwap_mean: float) -> dict:
    """Build a single PSU evaluation row for one trading day."""
    diff = (vwap_mean - pinned_vwap) / pinned_vwap * 100
    mult = psu_multiplier(diff)
    cl12_stocks = round(PSU_BASE["cl12"] * mult)
    cl34_stocks = round(PSU_BASE["cl34"] * mult)
    return {
        "as_of": as_of,
        "close": round(close, 2),
        "pinned_date": PSU_PINNED_DATE,
        "pinned_vwap_mean": round(pinned_vwap, 2),
        "vwap_mean": round(vwap_mean, 2),
        "diff_ratio": round(diff, 2),
        "multiplier": mult,
        "cl12_base": PSU_BASE["cl12"],
        "cl12_stocks": cl12_stocks,
        "cl12_eval": round(cl12_stocks * close, 2),
        "cl34_base": PSU_BASE["cl34"],
        "cl34_stocks": cl34_stocks,
        "cl34_eval": round(cl34_stocks * close, 2),
    }


def load_vwap_index(path: str) -> dict[str, dict]:
    """Read the VWAP CSV into {as_of: row} for PSU lookups."""
    index: dict[str, dict] = {}
    if not os.path.exists(path):
        return index
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            index[row["as_of"]] = row
    return index


def load_existing_psu_as_of(path: str) -> set[str]:
    """Read just the set of as_of dates already present in the PSU CSV."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["as_of"] for row in csv.DictReader(f)}


def write_psu_summaries(path: str, summaries: list[dict]) -> None:
    """Merge PSU rows into the per-symbol PSU CSV, deduplicated by as_of."""
    existing: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["as_of"]] = row
    for summary in summaries:
        existing[summary["as_of"]] = summary
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PSU_FIELDS)
        writer.writeheader()
        for as_of in sorted(existing):
            writer.writerow(existing[as_of])


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

    # VWAPs are computed only for trading days present in the price data but
    # NOT yet in the VWAP CSV (incremental). This skips days already computed
    # in a previous run, while still allowing newly fetched historical days to
    # be filled in. Each computed day d uses trailing-window data on or before
    # d (compute_vwap filters rows to [window_start, d]).
    sorted_rows = [existing[d] for d in sorted(existing)]
    vwap_path = os.path.join(args.docs_dir, f"{args.ticker}_{args.market}_vwap.csv")
    existing_as_of = load_existing_as_of(vwap_path)
    needed = [d for d in sorted(existing) if d not in existing_as_of]

    if needed:
        summaries: list[dict] = []
        for d in needed:
            ref_date = datetime.strptime(d, "%Y-%m-%d")
            summaries.append(compute_vwaps(sorted_rows, ref_date))
        write_vwap_summaries(vwap_path, summaries)
        latest = summaries[-1]
        print(f"VWAPs computed for {len(summaries)} new day(s) "
              f"({summaries[0]['as_of']} .. {latest['as_of']}); latest:")
        print(f"  7-day   : {latest['vwap_7d']!s:>12}  ({latest['days_7d']} trading days)")
        print(f"  1-month : {latest['vwap_1m']!s:>12}  ({latest['days_1m']} trading days)")
        print(f"  2-month : {latest['vwap_2m']!s:>12}  ({latest['days_2m']} trading days)")
        print(f"  mean    : {latest['vwap_mean']!s:>12}")
        print(f"{vwap_path}: VWAP summary updated")
    else:
        print(f"No new VWAP rows to compute "
              f"({len(existing_as_of)} day(s) already present in {vwap_path})")

    # PSU evaluation: difference ratio, tiered multiplier and grant evaluation
    # for cl1/cl2 (base 200) and cl3/cl4 (base 300), comparing each trading
    # day's VWAP mean against the pinned anchor date (PSU_PINNED_DATE) and
    # valuing the granted shares at that day's close. One row per trading day
    # is written to a dedicated PSU CSV (separate from the prices and VWAP
    # CSVs), accumulated incrementally and deduplicated by as_of.
    psu_path = os.path.join(args.docs_dir, f"{args.ticker}_{args.market}_psu.csv")
    vwap_index = load_vwap_index(vwap_path)
    pinned_row = vwap_index.get(PSU_PINNED_DATE)
    pinned_vwap = _fnum(pinned_row["vwap_mean"]) if pinned_row else None

    if pinned_vwap is None:
        print(f"PSU skipped: pinned anchor {PSU_PINNED_DATE} has no VWAP mean "
              f"in {vwap_path}")
    else:
        existing_psu_as_of = load_existing_psu_as_of(psu_path)
        psu_summaries: list[dict] = []
        for as_of in sorted(vwap_index):
            if as_of in existing_psu_as_of:
                continue
            vwap_mean = _fnum(vwap_index[as_of]["vwap_mean"])
            if vwap_mean is None:
                continue
            price_row = existing.get(as_of)
            close = _fnum(price_row["close"]) if price_row else None
            if close is None:
                continue
            psu_summaries.append(
                compute_psu_row(as_of, close, pinned_vwap, vwap_mean)
            )

        if psu_summaries:
            write_psu_summaries(psu_path, psu_summaries)
            latest = psu_summaries[-1]
            print(f"PSU evaluated for {len(psu_summaries)} new day(s) "
                  f"({psu_summaries[0]['as_of']} .. {latest['as_of']}); latest "
                  f"(vs pinned {PSU_PINNED_DATE} @ {latest['pinned_vwap_mean']}):")
            print(f"  diff_ratio : {latest['diff_ratio']:>+.2f}%")
            print(f"  multiplier : x{latest['multiplier']}")
            print(f"  close      : {latest['close']}")
            print(f"  cl1/cl2    : {latest['cl12_stocks']} stocks  "
                  f"(eval {latest['cl12_eval']})")
            print(f"  cl3/cl4    : {latest['cl34_stocks']} stocks  "
                  f"(eval {latest['cl34_eval']})")
            print(f"{psu_path}: PSU summary updated")
        else:
            print(f"No new PSU rows to compute "
                  f"({len(existing_psu_as_of)} day(s) already present "
                  f"in {psu_path})")


if __name__ == "__main__":
    main()
