"use strict";
/* Samsung Electronics (005930.KS) price & VWAP dashboard.
 *
 * Fetches the two committed CSVs from the jsDelivr CDN (see repo README) so the
 * page reflects the latest daily GitHub-Actions run without being rebuilt.
 * Renders an ECharts chart mixing:
 *   - trading volume ........ bar    (right axis, shares)
 *   - closing price ......... points (left axis, KRW)
 *   - VWAP 7D / 1M / 2M ..... lines  (left axis, KRW)
 *   - VWAP mean ............. line   (left axis, KRW, emphasised)
 */

const DATA = {
  prices: "https://cdn.jsdelivr.net/gh/tindone/samsung-price@main/data/005930_kospi_prices.csv",
  vwap:   "https://cdn.jsdelivr.net/gh/tindone/samsung-price@main/data/005930_kospi_vwap.csv",
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

function setStat(field, value, opts = {}) {
  const el = document.querySelector(`.stat__value[data-field="${field}"], .stat__date[data-field="${field}"], .stat__meta[data-field="${field}"]`);
  if (el) el.textContent = value ?? "";
}

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

async function loadData() {
  const [prices, vwap] = await Promise.all([loadPrices(), loadVwap()]);

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
  return merged;
}

/* ---------- stat cards ---------- */
function renderStats(data) {
  if (!data.length) return;
  const last = data[data.length - 1];

  // latest row that actually has a vwap entry (vwap may lag the latest price day)
  let vrow = last;
  for (let i = data.length - 1; i >= 0; i--) {
    if (data[i].vwap_mean != null) { vrow = data[i]; break; }
  }

  setStat("close",  last.close != null ? "₩" + fmtKRW.format(last.close) : "—");
  setStat("as_of",  last.date);

  setStat("vwap_mean", vrow.vwap_mean != null ? "₩" + fmtKRW.format(vrow.vwap_mean) : "—");
  setStat("mean_date", vrow.vwap_as_of ? `as of ${vrow.vwap_as_of}` : "—");

  setStat("vwap_7d", vrow.vwap_7d != null ? "₩" + fmtKRW.format(vrow.vwap_7d) : "—");
  setStat("days_7d", vrow.days_7d != null ? `${vrow.days_7d} trading days` : "—");

  setStat("vwap_1m", vrow.vwap_1m != null ? "₩" + fmtKRW.format(vrow.vwap_1m) : "—");
  setStat("days_1m", vrow.days_1m != null ? `${vrow.days_1m} trading days` : "—");

  setStat("vwap_2m", vrow.vwap_2m != null ? "₩" + fmtKRW.format(vrow.vwap_2m) : "—");
  setStat("days_2m", vrow.days_2m != null ? `${vrow.days_2m} trading days` : "—");

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
    const data = await loadData();
    if (!data.length) throw new Error("no rows returned");
    renderStats(data);
    renderCompare(data);
    renderChart(data);
    status.textContent = "";
  } catch (err) {
    console.error(err);
    status.textContent = "Failed to load data: " + err.message;
    status.classList.add("error");
  }
}

document.addEventListener("DOMContentLoaded", init);
