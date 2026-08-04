"use strict";
/* Samsung Electronics (005930.KS) price & VWAP dashboard.
 *
 * Fetches the three committed CSVs from the jsDelivr CDN (see repo README) so
 * the page reflects the latest daily GitHub-Actions run without being rebuilt:
 *   - prices CSV ......... daily OHLCV (close, volume)
 *   - vwap CSV ........... trailing 7D/1M/2M VWAPs + VWAP mean per day
 *   - psu CSV ............ precomputed PSU grant evaluation per day
 *        (diff_ratio, multiplier, cl1/cl2 & cl3/cl4 stocks + evaluation),
 *        so the PSU panel below just reads values instead of recomputing them.
 * Renders an ECharts chart mixing:
 *   - trading volume ........ bar    (right axis, shares)
 *   - closing price ......... points (left axis, KRW)
 *   - VWAP 7D / 1M / 2M ..... lines  (left axis, KRW)
 *   - VWAP mean ............. line   (left axis, KRW, emphasised)
 */

const DATA = {
  prices: "https://cdn.jsdelivr.net/gh/ssethj/samsung-price@main/data/005930_kospi_prices.csv",
  vwap:   "https://cdn.jsdelivr.net/gh/ssethj/samsung-price@main/data/005930_kospi_vwap.csv",
  psu:    "https://cdn.jsdelivr.net/gh/ssethj/samsung-price@main/data/005930_kospi_psu.csv",
};

// Reference date for the VWAP-mean comparison shown on the page.
const PINNED_DATE = "2025-10-13";

const COLORS = {
  volume: "#93a4c4",
  close:  "#111827",
  v7:     "#2563eb",
  v1:     "#f59e0b",
  v2:     "#10b981",
  mean:   "#ef4444",
  grid:   "#e5e7eb",
  ink:    "#6b7280",
};

/* ---------- CSV helpers (no deps; handles CRLF + quoted fields) ---------- */
function parseCSV(text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }      // escaped quote
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\r") { /* ignore; \n ends the row */ }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  // drop trailing blank line
  if (rows.length && rows[rows.length - 1].length === 1 && rows[rows.length - 1][0] === "") rows.pop();
  return rows;
}

function toObjects(rows) {
  if (!rows.length) return [];
  const headers = rows[0].map(h => h.trim());
  const out = [];
  for (let r = 1; r < rows.length; r++) {
    const cells = rows[r];
    if (cells.length === 1 && cells[0] === "") continue;
    const obj = {};
    headers.forEach((h, i) => { obj[h] = (cells[i] ?? "").trim(); });
    out.push(obj);
  }
  return out;
}

function num(v) {
  if (v === "" || v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/* ---------- formatting ---------- */
const fmtKRW = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const fmtVol = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

/* ---------- data load + join ---------- */
async function loadPrices() {
  const res = await fetch(DATA.prices, { cache: "no-store" });
  if (!res.ok) throw new Error(`prices HTTP ${res.status}`);
  return toObjects(parseCSV(await res.text()));
}
async function loadVwap() {
  const res = await fetch(DATA.vwap, { cache: "no-store" });
  if (!res.ok) throw new Error(`vwap HTTP ${res.status}`);
  return toObjects(parseCSV(await res.text()));
}
async function loadPsu() {
  const res = await fetch(DATA.psu, { cache: "no-store" });
  if (!res.ok) throw new Error(`psu HTTP ${res.status}`);
  return toObjects(parseCSV(await res.text()));
}

async function loadData() {
  const [prices, vwap] = await Promise.all([loadPrices(), loadVwap()]);
  // PSU CSV is optional: if it isn't published yet (or fails to load), the PSU
  // panel just shows placeholders rather than breaking the whole dashboard.
  const psu = await loadPsu().catch(() => []);

  // index vwap rows by as_of for an O(n) join
  const vmap = new Map();
  for (const v of vwap) vmap.set(v.as_of, v);

  // prices has every trading day; attach matching vwap row (if any)
  const merged = prices.map(p => {
    const v = vmap.get(p.date);
    return {
      date: p.date,
      ts: Date.parse(p.date + "T00:00:00Z"),
      close: num(p.close),
      volume: num(p.volume),
      vwap_7d:   v ? num(v.vwap_7d)   : null,
      vwap_1m:   v ? num(v.vwap_1m)   : null,
      vwap_2m:   v ? num(v.vwap_2m)   : null,
      vwap_mean: v ? num(v.vwap_mean) : null,
      days_7d:   v ? num(v.days_7d)   : null,
      days_1m:   v ? num(v.days_1m)   : null,
      days_2m:   v ? num(v.days_2m)   : null,
      vwap_as_of: v ? v.as_of : null,
    };
  });
  return { merged, psu };
}

/* ---------- stat cards ----------
 * The top-of-page stat cards were removed; this now only updates the header
 * sub-text with the trading-day span and last close. Kept as its own function
 * so the data flow stays clear.
 */
function renderStats(data) {
  if (!data.length) return;
  const last = data[data.length - 1];
  const sub = document.getElementById("last-updated");
  sub.textContent = `${data.length} trading days · ${data[0].date} → ${last.date} · last close ₩${fmtKRW.format(last.close ?? 0)}`;
}

/* ---------- VWAP mean comparison (pinned date vs latest) ---------- */
function renderCompare(data) {
  // latest row that actually has a vwap_mean entry
  let latest = null;
  for (let i = data.length - 1; i >= 0; i--) {
    if (data[i].vwap_mean != null) { latest = data[i]; break; }
  }
  const pinned = data.find(d => d.date === PINNED_DATE && d.vwap_mean != null) || null;

  const elPinned  = document.querySelector('[data-field="cmp_pinned_mean"]');
  const elDate    = document.querySelector('[data-field="cmp_latest_date"]');
  const elLatest  = document.querySelector('[data-field="cmp_latest_mean"]');
  const elChange  = document.querySelector('[data-field="cmp_change"]');

  if (!latest || !pinned) {
    if (elChange) elChange.textContent = "—";
    return;
  }

  if (elPinned) elPinned.textContent = "₩" + fmtKRW.format(pinned.vwap_mean);
  if (elDate)   elDate.textContent   = latest.date;
  if (elLatest) elLatest.textContent = "₩" + fmtKRW.format(latest.vwap_mean);

  const pct = (latest.vwap_mean - pinned.vwap_mean) / pinned.vwap_mean * 100;
  const sign = pct >= 0 ? "+" : "";
  if (elChange) {
    elChange.textContent = sign + pct.toFixed(2) + "%";
    elChange.classList.toggle("is-up", pct >= 0);
    elChange.classList.toggle("is-down", pct < 0);
  }
}

/* ---------- PSU expected grant ----------
 * Values are read straight from the precomputed PSU CSV (one row per trading
 * day, latest = last row): diff_ratio, multiplier, cl1/cl2 & cl3/cl4 expected
 * stocks and their evaluation at that day's close. No client-side math — the
 * script (fetch_prices.py) owns the tier/multiplier logic and writes the file.
 */
function renderPsu(psu) {
  const elDiff  = document.querySelector('[data-field="psu_diff"]');
  const elMult  = document.querySelector('[data-field="psu_mult"]');
  const elClose = document.querySelector('[data-field="psu_close"]');
  const el12Stk = document.querySelector('[data-field="psu_cl12_stocks"]');
  const el12Ev  = document.querySelector('[data-field="psu_cl12_eval"]');
  const el34Stk = document.querySelector('[data-field="psu_cl34_stocks"]');
  const el34Ev  = document.querySelector('[data-field="psu_cl34_eval"]');

  if (!psu || !psu.length) {
    [elDiff, elMult, elClose, el12Stk, el12Ev, el34Stk, el34Ev]
      .forEach(el => { if (el) el.textContent = "—"; });
    return;
  }

  const row    = psu[psu.length - 1];
  const diff   = num(row.diff_ratio);
  const mult   = num(row.multiplier);
  const close  = num(row.close);
  const stk12  = num(row.cl12_stocks);
  const ev12   = num(row.cl12_eval);
  const stk34  = num(row.cl34_stocks);
  const ev34   = num(row.cl34_eval);

  if (elDiff)  elDiff.textContent  = diff != null ? (diff >= 0 ? "+" : "") + diff.toFixed(2) + "%" : "—";
  if (elMult)  elMult.textContent  = mult != null ? "×" + mult.toFixed(1) : "—";
  if (elClose) elClose.textContent = close != null ? fmtKRW.format(close) : "—";
  if (el12Stk) el12Stk.textContent = stk12 != null ? stk12 + " stocks" : "—";
  if (el12Ev)  el12Ev.textContent  = ev12 != null ? "₩" + fmtKRW.format(ev12) : "—";
  if (el34Stk) el34Stk.textContent = stk34 != null ? stk34 + " stocks" : "—";
  if (el34Ev)  el34Ev.textContent  = ev34 != null ? "₩" + fmtKRW.format(ev34) : "—";
}

/* ---------- chart ---------- */
let chart;
function renderChart(data) {
  if (typeof echarts === "undefined") {
    throw new Error("ECharts failed to load from CDN");
  }
  chart = echarts.init(document.getElementById("chart"), null, { renderer: "canvas" });

  // points/lines need [ts, value]; omit nulls so lines start where data begins
  const pick = key => data
    .filter(d => d[key] != null)
    .map(d => [d.ts, d[key]]);

  const volData  = data.filter(d => d.volume != null).map(d => [d.ts, d.volume]);
  const closePts = pick("close");
  const v7  = pick("vwap_7d");
  const v1  = pick("vwap_1m");
  const v2  = pick("vwap_2m");
  const vmean = pick("vwap_mean");

  const option = {
    backgroundColor: "transparent",
    color: [COLORS.v7, COLORS.v1, COLORS.v2, COLORS.mean, COLORS.close, COLORS.volume],
    legend: {
      top: 6,
      icon: "roundRect",
      itemWidth: 14,
      itemHeight: 8,
      textStyle: { color: COLORS.ink, fontSize: 12 },
      data: ["Close", "VWAP 7D", "VWAP 1M", "VWAP 2M", "VWAP Mean", "Volume"],
    },
    grid: { left: 64, right: 64, top: 44, bottom: 88 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
      backgroundColor: "rgba(17,24,39,0.92)",
      borderWidth: 0,
      textStyle: { color: "#fff", fontSize: 12 },
      order: "valueDesc",
      formatter: (params) => {
        if (!params || !params.length) return "";
        const d = new Date(params[0].value[0]);
        const head = `<div style="font-weight:700;margin-bottom:4px">${d.toISOString().slice(0, 10)}</div>`;
        const body = params
          .filter(p => p.value && p.value[1] != null)
          .map(p => {
            const isVol = p.seriesName === "Volume";
            const val = isVol ? fmtVol.format(p.value[1]) : "₩" + fmtKRW.format(p.value[1]);
            const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${p.color};margin-right:6px"></span>`;
            return `${dot}${p.seriesName}: <b>${val}</b>`;
          })
          .join("<br/>");
        return head + body;
      },
    },
    xAxis: {
      type: "time",
      boundaryGap: false,
      axisLine: { lineStyle: { color: COLORS.grid } },
      axisLabel: { color: COLORS.ink, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: [
      {
        type: "value",
        name: "Price (KRW)",
        position: "left",
        scale: true,
        nameTextStyle: { color: COLORS.ink, padding: [0, 0, 0, 6] },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: COLORS.grid, type: "dashed" } },
        axisLabel: {
          color: COLORS.ink,
          formatter: v => "₩" + fmtVol.format(v),
        },
      },
      {
        type: "value",
        name: "Volume",
        position: "right",
        scale: true,
        nameTextStyle: { color: COLORS.ink, padding: [0, 6, 0, 0] },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: {
          color: COLORS.ink,
          formatter: v => fmtVol.format(v),
        },
      },
    ],
    dataZoom: [
      { type: "inside", start: 0, end: 100 },
      {
        type: "slider",
        bottom: 20,
        height: 22,
        borderColor: "transparent",
        backgroundColor: "#f1f5f9",
        fillerColor: "rgba(148,163,184,0.25)",
        handleStyle: { color: COLORS.ink },
        textStyle: { color: COLORS.ink },
        labelFormatter: v => new Date(v).toISOString().slice(0, 10),
      },
    ],
    series: [
      {
        name: "Volume",
        type: "bar",
        yAxisIndex: 1,
        data: volData,
        barWidth: "70%",
        itemStyle: { color: COLORS.volume, opacity: 0.45, borderRadius: [2, 2, 0, 0] },
        emphasis: { itemStyle: { color: COLORS.volume, opacity: 0.7 } },
        z: 1,
      },
      {
        name: "VWAP 7D",
        type: "line",
        yAxisIndex: 0,
        data: v7,
        showSymbol: false,
        symbol: "none",
        lineStyle: { width: 2, color: COLORS.v7 },
        z: 3,
      },
      {
        name: "VWAP 1M",
        type: "line",
        yAxisIndex: 0,
        data: v1,
        showSymbol: false,
        symbol: "none",
        lineStyle: { width: 2, color: COLORS.v1 },
        z: 3,
      },
      {
        name: "VWAP 2M",
        type: "line",
        yAxisIndex: 0,
        data: v2,
        showSymbol: false,
        symbol: "none",
        lineStyle: { width: 2, color: COLORS.v2 },
        z: 3,
      },
      {
        name: "VWAP Mean",
        type: "line",
        yAxisIndex: 0,
        data: vmean,
        showSymbol: false,
        symbol: "none",
        lineStyle: { width: 3.5, color: COLORS.mean },
        z: 4,
      },
      {
        name: "Close",
        type: "scatter",
        yAxisIndex: 0,
        data: closePts,
        symbolSize: 4,
        itemStyle: { color: COLORS.close, opacity: 0.85 },
        z: 5,
      },
    ],
  };

  chart.setOption(option);
  window.addEventListener("resize", () => chart && chart.resize());
}

/* ---------- entry point ---------- */
async function init() {
  const status = document.getElementById("status");
  try {
    status.textContent = "Loading market data…";
    const { merged: data, psu } = await loadData();
    if (!data.length) throw new Error("no rows returned");
    renderStats(data);
    renderCompare(data);
    renderPsu(psu);
    renderChart(data);
    status.textContent = "";
  } catch (err) {
    console.error(err);
    status.textContent = "Failed to load data: " + err.message;
    status.classList.add("error");
  }
}

document.addEventListener("DOMContentLoaded", init);
