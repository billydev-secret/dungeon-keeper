// Chart.js helpers. Palettes mirror services/activity_graphs.py.
//
// The exported tokens below are the ONE chart palette for the dashboard —
// panels that draw their own charts or canvases should import these instead
// of hard-coding hex values (W-D15):
//   ROLE_COLORS   — categorical series palette (lines, doughnut slices)
//   GENDER_COLORS — fixed gender→color mapping
//   CHART_BAR     — default single-series bar/fill color (gold)
//   CHART_ACCENT  — secondary series/overlay color (mauve)
//   CHART_TEXT    — axis/legend/label text color
//   CHART_GRID    — gridline/border/wick color

// The categorical series palette. Every value here was chosen by running the
// six-check validator against the dark chart surface (#2b2d31), not by eye —
// see tests/web/test_chart_palette.py, which re-runs those checks in CI.
//
// The palette it replaced was all warm: gold 85.6°, amber 81.2°, moss 118.7°,
// clay 30.1° — four of six hues inside a 90° wedge. Two of them, moss and
// amber, measured ΔE 1.8 under protanopia and 8.7 with NORMAL vision, against
// a floor of 15. They were indistinguishable to everyone and identical to a
// red-blind viewer, and the Activity chart stacked them adjacently.
//
// An all-warm palette cannot separate six series; that is arithmetic, not
// taste. These keep an earthy, desaturated feel (chroma capped at 0.115) and
// lead with amber so a single-series chart still reads as the brand, but the
// hues are spread around the wheel because they have to be.
export const ROLE_COLORS = [
  "#B58030", // amber
  "#4A7023", // moss
  "#00A29C", // teal
  "#2167A1", // slate
  "#9D79C3", // orchid
  "#97435C", // wine
];

// A 7th series is NOT a generated or recycled hue — past six, adjacent classes
// blur no matter what you pick. Callers with more series fold the tail into
// "Other", facet into small multiples, or switch to a table.
export const SERIES_OVERFLOW = "#6b7076";

/** Colour for series `i`, or the overflow neutral once the palette runs out. */
export function seriesColor(i) {
  return ROLE_COLORS[i] || SERIES_OVERFLOW;
}

export const GENDER_COLORS = {
  male:      "#B58030",
  female:    "#9D79C3",
  nonbinary: "#00A29C",
  unknown:   "#6b7076",
};

export const CHART_BAR    = "#E6B84C";
export const CHART_ACCENT = "#B36A92";
export const CHART_TEXT   = "#dbdee1";
export const CHART_GRID   = "#3f4147";
// --bg-alt: the card a chart sits on. Painted between stacked segments to
// read as a 2px gap rather than a border.
export const CHART_SURFACE = "#2b2d31";

const BAR     = CHART_BAR;
const ACCENT  = CHART_ACCENT;
const TEXT    = CHART_TEXT;
const GRID    = CHART_GRID;

Chart.defaults.color = TEXT;
Chart.defaults.borderColor = GRID;
// Canvas cannot read a CSS custom property, so the dashboard's body face is
// restated here. Without this every chart label was in the system font while
// the page around it was in Public Sans.
Chart.defaults.font.family = '"Public Sans", "Noto Sans", "Helvetica Neue", Helvetica, Arial, sans-serif';

// Keep the x-axis minimum pinned to the original (labeled) edge when zooming,
// instead of letting the zoom plugin center the visible range on the cursor.
function pinXMinOnZoom({ chart }) {
  const scale = chart.scales?.x;
  const bounds = chart.getInitialScaleBounds?.().x;
  if (!scale || !bounds) return;
  const range = scale.max - scale.min;
  const newMin = bounds.min;
  const newMax = Math.min(bounds.max, newMin + range);
  if (scale.min === newMin && scale.max === newMax) return;
  chart.options.scales.x.min = newMin;
  chart.options.scales.x.max = newMax;
  chart.update("none");
}

const ZOOM_OPTIONS = {
  zoom: {
    // Ctrl gates wheel-zoom so plain scrolling over a chart still scrolls
    // the page (W-D6). Pinch continues to zoom on touch.
    wheel: { enabled: true, modifierKey: "ctrl" },
    pinch: { enabled: true },
    mode: "x",
    onZoomComplete: pinXMinOnZoom,
  },
  pan: {
    enabled: true,
    mode: "x",
  },
  limits: {
    x: { minRange: 2 },
  },
};

const COMMON_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    tooltip: { backgroundColor: "#18191c", borderColor: GRID, borderWidth: 1 },
    zoom: ZOOM_OPTIONS,
  },
  scales: {
    x: { grid: { color: GRID }, ticks: { color: TEXT, maxRotation: 45, minRotation: 0 } },
    y: { grid: { color: GRID }, ticks: { color: TEXT, precision: 0 }, beginAtZero: true },
  },
};

/** Attach a reset-zoom button to a chart's parent container. */
export function addResetZoom(chart) {
  const wrap = chart.canvas.parentElement;
  if (!wrap || wrap.querySelector(".chart-reset-zoom")) return;
  const btn = document.createElement("button");
  btn.className = "chart-reset-zoom";
  btn.textContent = "Reset Zoom";
  btn.title = "Ctrl+scroll or pinch to zoom, drag to pan. Double-click the chart or press this button to reset.";
  btn.addEventListener("click", () => chart.resetZoom());
  wrap.style.position = "relative";
  wrap.appendChild(btn);
  // Also allow double-click on canvas to reset
  chart.canvas.addEventListener("dblclick", () => chart.resetZoom());
}

function cloneOpts(value) {
  // Deep clone for chart-options objects: handles plain objects + arrays,
  // passes functions and other non-cloneable values through by reference
  // (structuredClone can't handle functions like onZoomComplete).
  if (Array.isArray(value)) return value.map(cloneOpts);
  if (value && typeof value === "object" && value.constructor === Object) {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = cloneOpts(v);
    return out;
  }
  return value;
}

function merge(base, overrides) {
  // Shallow-ish merge good enough for chart options
  const result = cloneOpts(base);
  for (const [k, v] of Object.entries(overrides)) {
    if (v && typeof v === "object" && !Array.isArray(v) && result[k]) {
      result[k] = merge(result[k], v);
    } else {
      result[k] = v;
    }
  }
  return result;
}


// ── Multi-line (role growth) ────────────────────────────────────────────

export function makeLineChart(canvas, { labels, series, title }) {
  const datasets = series.map((s, i) => ({
    label: s.role || s.gender || s.label,
    data: s.counts,
    // seriesColor, not `i % length`: cycling silently gives series 7 the same
    // hue as series 1, so two different things share an identity.
    borderColor: s.color || seriesColor(i),
    backgroundColor: (s.color || seriesColor(i)) + "33",
    borderWidth: 2,
    pointRadius: 3,
    pointHoverRadius: 5,
    tension: 0.15,
  }));

  const chart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: merge(COMMON_OPTIONS, {
      interaction: { mode: "index", intersect: false },
      plugins: {
        title: title ? { display: true, text: title, color: TEXT, font: { size: 14 } } : { display: false },
        legend: { position: "bottom", labels: { color: TEXT } },
      },
    }),
  });
  addResetZoom(chart);
  return chart;
}


// ── Bar chart (simple) ──────────────────────────────────────────────────

export function makeBarChart(canvas, { labels, data, title, xLabel, yLabel, color }) {
  const chart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: color || BAR,
        borderWidth: 0,
        barPercentage: 0.85,
        categoryPercentage: 0.9,
      }],
    },
    options: merge(COMMON_OPTIONS, {
      plugins: {
        title: title ? { display: true, text: title, color: TEXT, font: { size: 14 } } : { display: false },
        legend: { display: false },
      },
      scales: {
        x: { grid: { color: GRID }, ticks: { color: TEXT, maxRotation: 45 }, title: xLabel ? { display: true, text: xLabel, color: TEXT } : undefined },
        y: { grid: { color: GRID }, ticks: { color: TEXT, precision: 0 }, beginAtZero: true, title: yLabel ? { display: true, text: yLabel, color: TEXT } : undefined },
      },
    }),
  });
  addResetZoom(chart);
  return chart;
}


// ── Stacked bar (nsfw-gender bar mode) ──────────────────────────────────

export function makeStackedBarChart(canvas, { labels, series, title }) {
  const datasets = series.map((s) => ({
    label: s.gender,
    data: s.counts,
    backgroundColor: s.color || GENDER_COLORS[s.gender] || "#949ba4",
    borderWidth: 0,
  }));

  const chart = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: merge(COMMON_OPTIONS, {
      interaction: { mode: "index", intersect: false },
      plugins: {
        title: title ? { display: true, text: title, color: TEXT, font: { size: 14 } } : { display: false },
        legend: { position: "bottom", labels: { color: TEXT } },
      },
      scales: {
        x: { stacked: true, grid: { color: GRID }, ticks: { color: TEXT, maxRotation: 45 } },
        y: { stacked: true, grid: { color: GRID }, ticks: { color: TEXT, precision: 0 }, beginAtZero: true },
      },
    }),
  });
  addResetZoom(chart);
  return chart;
}


// ── Horizontal bar chart ────────────────────────────────────────────

export function makeHorizontalBarChart(canvas, { labels, data, title, xLabel, yLabel, color, colors }) {
  // Size canvas so each bar gets at least 28px
  const minHeight = Math.max(200, labels.length * 28 + 60);
  canvas.parentElement.style.minHeight = `${minHeight}px`;

  const chart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors || color || BAR,
        borderWidth: 0,
        barPercentage: 0.85,
        categoryPercentage: 0.9,
      }],
    },
    options: merge(COMMON_OPTIONS, {
      indexAxis: "y",
      plugins: {
        title: title ? { display: true, text: title, color: TEXT, font: { size: 14 } } : { display: false },
        legend: { display: false },
      },
      scales: {
        x: { grid: { color: GRID }, ticks: { color: TEXT, precision: 0 }, beginAtZero: true, title: xLabel ? { display: true, text: xLabel, color: TEXT } : undefined },
        y: { grid: { color: GRID }, ticks: { color: TEXT }, title: yLabel ? { display: true, text: yLabel, color: TEXT } : undefined },
      },
    }),
  });
  addResetZoom(chart);
  return chart;
}


// ── Doughnut chart ─────────────────────────────────────────────────

export function makeDoughnutChart(canvas, { labels, data, title, colors }) {
  return new Chart(canvas, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors || ROLE_COLORS,
        borderColor: "#2b2d31",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        title: title ? { display: true, text: title, color: TEXT, font: { size: 14 } } : { display: false },
        legend: { position: "bottom", labels: { color: TEXT } },
        tooltip: { backgroundColor: "#18191c", borderColor: GRID, borderWidth: 1 },
      },
    },
  });
}


// ── Floating bar / candlestick (message cadence) ────────────────────────

export function makeCandlestickChart(canvas, { buckets, title, noZoom: _noZoom }) {
  // Chart.js "floating bars": data as [low, high] pairs.
  // We draw the body (p20 → p80) as a thick bar, the wick (min → max) as a
  // thin bar behind it, and mark the median with a line annotation.
  const labels = buckets.map((b) => b.label);

  const chart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Min–Max",
          data: buckets.map((b) => [b.min_gap, b.max_gap]),
          backgroundColor: GRID,
          borderWidth: 0,
          barPercentage: 0.3,
          categoryPercentage: 0.9,
          order: 2,
        },
        {
          label: "P20–P80",
          data: buckets.map((b) => [b.p20_gap, b.p80_gap]),
          backgroundColor: BAR,
          borderWidth: 0,
          barPercentage: 0.7,
          categoryPercentage: 0.9,
          order: 1,
        },
        {
          label: "Median",
          data: buckets.map((b) => b.median_gap),
          type: "line",
          borderColor: ACCENT,
          backgroundColor: ACCENT + "33",
          borderWidth: 2,
          pointRadius: 3,
          pointHoverRadius: 5,
          tension: 0.2,
          order: 0,
        },
      ],
    },
    options: merge(COMMON_OPTIONS, {
      interaction: { mode: "index", intersect: false },
      plugins: {
        title: title ? { display: true, text: title, color: TEXT, font: { size: 14 } } : { display: false },
        legend: { position: "bottom", labels: { color: TEXT } },
        tooltip: {
          backgroundColor: "#18191c", borderColor: GRID, borderWidth: 1,
          callbacks: {
            label(ctx) {
              const b = buckets[ctx.dataIndex];
              const fmt = (v) => v < 1 ? Math.round(v * 60) + "s" : v < 60 ? Math.round(v) + "m" : (v / 60).toFixed(1).replace(/\.0$/, "") + "h";
              if (ctx.datasetIndex === 0) return `Min: ${fmt(b.min_gap)}  Max: ${fmt(b.max_gap)}`;
              if (ctx.datasetIndex === 1) return `P20: ${fmt(b.p20_gap)}  P80: ${fmt(b.p80_gap)}`;
              return `Median: ${fmt(b.median_gap)}`;
            },
          },
        },
      },
      scales: {
        x: { grid: { color: GRID }, ticks: { color: TEXT, maxRotation: 45 } },
        y: {
          type: "logarithmic",
          reverse: true,
          grid: { color: GRID },
          afterBuildTicks(axis) {
            // Pseudo-decade ticks: 0.5s, 1s, 10s, 60s, 600s (in minutes)
            axis.ticks = [
              { value: 0.5 / 60 },   // 0.5s
              { value: 1 / 60 },      // 1s
              { value: 10 / 60 },     // 10s
              { value: 1 },           // 60s
              { value: 10 },          // 600s
            ];
          },
          ticks: {
            color: TEXT,
            callback(value) {
              const secs = value * 60;
              if (secs < 1) return secs.toFixed(1).replace(/\.0$/, "") + "s";
              if (secs < 60) return Math.round(secs) + "s";
              return Math.round(secs / 60) + "m";
            },
          },
          title: { display: true, text: "Time between messages (less = faster)", color: TEXT },
        },
      },
    }),
  });
  addResetZoom(chart);
  return chart;
}


// ── HTML legend + table view ────────────────────────────────────────────
//
// Chart.js's own legend is drawn onto the canvas: bordered boxes in the canvas
// font, unselectable and invisible to assistive tech. These build the same
// information in HTML — thin pill marks, the page's type, real buttons — and
// the table is the relief that the three sub-3:1 palette slots oblige.

const _fmt = (n) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : (Math.abs(n) >= 1000 ? Math.round(n).toLocaleString() : String(Math.round(n * 10) / 10));

/**
 * Render an HTML legend for `chart` into `host`, one entry per dataset, each
 * showing the series total. Clicking an entry toggles that series.
 * Returns a `refresh()` that re-reads the chart, for when data changes.
 */
export function renderChartLegend(host, chart) {
  if (!host) return { refresh() {} };

  function total(ds) {
    return (ds.data || []).reduce((a, v) => a + (Number.isFinite(v) ? v : 0), 0);
  }

  function paint() {
    host.className = "chart-legend";
    host.replaceChildren();
    chart.data.datasets.forEach((ds, i) => {
      const visible = chart.isDatasetVisible(i);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chart-legend__item";
      btn.setAttribute("aria-pressed", String(visible));

      const swatch = document.createElement("span");
      swatch.className = "chart-legend__swatch";
      // A line series carries its identity in borderColor, a bar in background.
      swatch.style.background =
        (typeof ds.backgroundColor === "string" && ds.backgroundColor !== "transparent"
          ? ds.backgroundColor
          : ds.borderColor) || SERIES_OVERFLOW;
      btn.appendChild(swatch);

      const label = document.createElement("span");
      label.className = "chart-legend__label";
      label.textContent = ds.label || `Series ${i + 1}`;
      btn.appendChild(label);

      const value = document.createElement("span");
      value.className = "chart-legend__value";
      value.textContent = _fmt(total(ds));
      btn.appendChild(value);

      btn.addEventListener("click", () => {
        chart.setDatasetVisibility(i, !chart.isDatasetVisible(i));
        chart.update();
        paint();
      });
      host.appendChild(btn);
    });
  }

  paint();
  return { refresh: paint };
}

/**
 * A "Show the numbers" disclosure holding every plotted value as a real table.
 * Tooltips enhance; they must never be the only way to read a value, and three
 * of the palette's slots are below 3:1 against the surface, which obliges this.
 */
export function renderChartTable(host, { labels, datasets, indexLabel = "Period" }) {
  if (!host) return;
  host.replaceChildren();

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "chart-table-toggle";
  const wrap = document.createElement("div");
  wrap.className = "chart-table-wrap";
  wrap.hidden = true;

  const setLabel = () => {
    toggle.textContent = wrap.hidden ? "Show the numbers" : "Hide the numbers";
    toggle.setAttribute("aria-expanded", String(!wrap.hidden));
  };
  toggle.addEventListener("click", () => {
    wrap.hidden = !wrap.hidden;
    setLabel();
  });
  setLabel();

  const table = document.createElement("table");
  table.className = "chart-table";
  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  for (const text of [indexLabel, ...datasets.map((d) => d.label || "Series")]) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = text;
    hrow.appendChild(th);
  }
  thead.appendChild(hrow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  labels.forEach((lab, r) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = lab;
    tr.appendChild(th);
    for (const ds of datasets) {
      const td = document.createElement("td");
      td.textContent = _fmt(ds.data ? ds.data[r] : null);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);

  host.appendChild(toggle);
  host.appendChild(wrap);
}
