# samsung-price

Daily closing prices and volume-weighted average prices (VWAPs) for Samsung
Electronics (KRX: 005930, KOSPI), fetched from Yahoo Finance and updated daily
via GitHub Actions.

## Data files

Both CSVs are committed under [`docs/`](docs/) and served over the jsDelivr CDN,
so you can fetch them directly without cloning the repo.

### Prices — `docs/005930_kospi_prices.csv`

One row per trading day. Columns: `date, close, volume, adjclose, symbol, market`.

- Raw (GitHub): <https://github.com/tindone/samsung-price/blob/main/docs/005930_kospi_prices.csv>
- jsDelivr CDN: <https://cdn.jsdelivr.net/gh/tindone/samsung-price@main/docs/005930_kospi_prices.csv>

### VWAP — `docs/005930_kospi_vwap.csv`

Trailing calendar-window VWAPs computed for every trading day. Columns:
`as_of, vwap_7d, days_7d, vwap_1m, days_1m, vwap_2m, days_2m, vwap_mean`.

- Raw (GitHub): <https://github.com/tindone/samsung-price/blob/main/docs/005930_kospi_vwap.csv>
- jsDelivr CDN: <https://cdn.jsdelivr.net/gh/tindone/samsung-price@main/docs/005930_kospi_vwap.csv>

> jsDelivr caches aggressively; point at a specific commit (`@<sha>` instead of
> `@main`) if you need a pinned, instantly-refreshing snapshot.

## How it works

- [`scripts/fetch_prices.py`](scripts/fetch_prices.py) pulls OHLCV data from the
  Yahoo Finance chart API (no API key required), accumulates rows into the prices
  CSV (deduplicated by date), then computes trailing 7-day / 1-month / 2-month
  VWAPs for every trading day and writes them to the VWAP CSV.
- [`.github/workflows/update-prices.yml`](.github/workflows/update-prices.yml)
  runs the script on a daily schedule and commits the updated CSVs back to this
  branch.

## Buy me a coffee

If you find this useful, consider buying me a coffee — every cup helps keep the
data flowing.

<p align="center">
  <a href="https://www.buymeacoffee.com/lungo" target="_blank">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="52" />
  </a>
</p>
