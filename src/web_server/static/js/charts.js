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

// Both of these were missed in the ROLE_COLORS/GENDER_COLORS migration and sat
// at their old, unvalidated values (poppy gold, warm mauve) until an audit of
// the panels that use them caught it — including activity.js's own members
// line, which had shipped ONE commit earlier still on the old mauve.
// Reusing ROLE_COLORS[0]/[4] rather than picking two more new hex values: it
// keeps "the brand default bar colour" and "the brand default line colour" as
// the SAME concept as "categorical series 1 and 5", not a third, parallel
// pair of hand-picked colours that could drift out of validation again.
export const CHART_BAR    = "#B58030"; // ROLE_COLORS[0], amber
export const CHART_ACCENT = "#9D79C3"; // ROLE_COLORS[4], orchid
export const CHART_TEXT   = "#dbdee1";
export const CHART_GRID   = "#3f4147";
// --bg-alt: the card a chart sits on. Painted between stacked segments to
// read as a 2px gap rather than a border.
export const CHART_SURFACE = "#2b2d31";

// ── Overlay charts ─────────────────────────────────────────────────────
// Two identities, not N: collapsing history into one envelope means this chart
// never needs a twelve-step ramp. Amber is the subject, teal the comparison —
// validated as a categorical pair against CHART_SURFACE (all six checks pass,
// worst adjacent dE 13.9 protan / 19.2 normal, both >= 3:1 on the surface).
// A neutral-grey band was rejected: it fails the chroma floor and would have
// read as a gridline rather than as data.
// Dashed median + solid current, so identity never rests on colour alone.
//
// "Now" leads with the brand amber; the historical band is teal, two steps
// round the wheel from it. The band's fill is that teal at 15% alpha — light
// enough that the lines drawn over it stay at full contrast.
export const OVERLAY_NOW  = CHART_BAR;      // ROLE_COLORS[0], amber
export const OVERLAY_PAST = ROLE_COLORS[2]; // teal
export const OVERLAY_BAND_FILL = OVERLAY_PAST + "26";
// Additional lines an overlay's caller can lay over the band, in order.
// Orchid first: it is the furthest hue from both amber and teal, and every one
// of these is dotted as well as coloured.
export const OVERLAY_EXTRA = [ROLE_COLORS[4], ROLE_COLORS[1], ROLE_COLORS[5]];

// ── Network-graph extension ────────────────────────────────────────────
// The Connection Graph is the one chart where series identity is NOT
// legend-matched across the page: communities are separated spatially, so
// the standard that applies is adjacent-pair separation plus secondary
// encoding (surface rings, prominence labels), not the all-pairs bar-chart
// rule that caps ROLE_COLORS at six. Folding cluster 7+ into
// SERIES_OVERFLOW is a real failure there, not a safe default — the
// palette validator scores that neutral at chroma 0.011 ("reads gray") and
// ΔE 4.6 from wine under deuteranopia, and both live servers detect eight
// communities.
//
// The two extra slots are the least-saturated pair that passes every
// validator check appended to the six (lightness band, chroma ≥ 0.1,
// adjacent CVD ΔE 8.4 worst, normal-vision ΔE 17.8 worst, against
// CHART_SURFACE). Eight mutually-distinguishable earthy hues are not
// achievable — the best all-pairs result is ΔE 14.7 against a floor of 15
// — which is why these are for spatially-separated graphs ONLY: never use
// GRAPH_CLUSTERS for legend-matched series, and past eight the tail still
// folds to the overflow neutral.
export const GRAPH_CLUSTERS = [...ROLE_COLORS, "#5C8547", "#685CA3"];

// Edge tint for the network canvas: edges composite at weight-scaled alpha
// over CHART_SURFACE, and CHART_BAR (4.0:1) muddies into the ground below
// ~0.3 alpha — the "greyed over" regression. This is amber lightened until
// the mark clears 5.7:1, the same hue family so a single-series chart and
// the graph still read as one brand.
export const GRAPH_EDGE = "#C9A24E";

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

export function makeLineChart(canvas, { labels, series, title: _title }) {
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
        // Both drawn in HTML by the caller instead: canvas text cannot use the
        // page's type, is unselectable, and does not exist for a screen
        // reader. See .chart-caption + renderChartLegend (both used by
        // activity.js) — every multi-series chart from this file should pair
        // with them, not rely on the canvas to speak for itself.
        title: { display: false },
        legend: { display: false },
      },
    }),
  });
  addResetZoom(chart);
  return chart;
}


// ── Bar chart (simple) ──────────────────────────────────────────────────

export function makeBarChart(canvas, { labels, data, title: _title, xLabel, yLabel, color }) {
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
        // In HTML by the caller — see the note on makeLineChart above. A
        // single series needs no legend (the caption already names it), which
        // is why this was already `display: false`; the title moves out for
        // the same accessibility reason, not because it needed a legend too.
        title: { display: false },
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

// `title` kept in the signature — panels still pass it for their own HTML
// caption — but charts.js no longer draws it, hence the underscore.
export function makeStackedBarChart(canvas, { labels, series, title: _title }) {
  const datasets = series.map((s, i) => ({
    // `label` as well as `gender`: this started life as the gender chart's
    // builder, but the same panel now stacks NudeNet's tag vocabulary through
    // it. Matching makeLineChart's accessor order keeps one series shape
    // working in both display modes.
    label: s.gender || s.label,
    data: s.counts,
    // Genders have one fixed colour each; an open-ended category list has
    // none, so it falls through to the validated palette rather than to the
    // overflow neutral — which would have painted every tag the same grey.
    backgroundColor: s.color || GENDER_COLORS[s.gender] || seriesColor(i),
    // A 2px gap in the surface colour between stacked segments, matching
    // activity.js. Without it two adjacent segments in a weak-CVD pair share a
    // hard edge with nothing separating them.
    borderColor: CHART_SURFACE,
    borderWidth: { top: 2 },
    borderSkipped: false,
  }));

  const chart = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: merge(COMMON_OPTIONS, {
      interaction: { mode: "index", intersect: false },
      plugins: {
        // In HTML by the caller — see the note on makeLineChart.
        title: { display: false },
        legend: { display: false },
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

export function makeHorizontalBarChart(canvas, { labels, data, title: _title, xLabel, yLabel, color, colors }) {
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
        // In HTML by the caller — see the note on makeLineChart.
        title: { display: false },
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

export function makeDoughnutChart(canvas, { labels, data, title: _title, colors }) {
  // A part-to-whole chart reads at a glance only up to ~6 segments; past that,
  // adjacent classes blur regardless of palette. Not enforced here (the caller
  // knows its own data), but it is why this stays a doughnut rather than
  // growing legend entries indefinitely — a 7-slice-plus report wants a table,
  // or renderPieLegend's "Other" fold, not a bigger wheel.
  return new Chart(canvas, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors || ROLE_COLORS,
        // The 2px gap between slices, in the surface colour rather than a
        // literal — see CHART_SURFACE.
        borderColor: CHART_SURFACE,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        // In HTML by the caller, via renderPieLegend — renderChartLegend
        // reads chart.data.DATASETS (one bar/line per series); a doughnut has
        // ONE dataset and many LABELS, so it needs its own legend function.
        title: { display: false },
        legend: { display: false },
        tooltip: { backgroundColor: "#18191c", borderColor: GRID, borderWidth: 1 },
      },
    },
  });
}


// ── Floating bar / candlestick (message cadence) ────────────────────────

/**
 * `title` is accepted for backward compatibility but no longer drawn on the
 * canvas — callers should show it as an HTML `.chart-caption` instead.
 */
export function makeCandlestickChart(canvas, { buckets, title: _title, noZoom: _noZoom }) {
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
        // In HTML by the caller, via renderChartLegend — Min-Max/P20-P80/
        // Median are three real datasets a reader may want to toggle, unlike
        // a doughnut's internal drawing primitives.
        //
        // (No panel currently calls makeCandlestickChart — it is exported and
        // unused. Fixed for consistency with the rest of this file anyway,
        // since a future caller inherits whatever is here.)
        title: { display: false },
        legend: { display: false },
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
      // A dataset can opt out of the legend entirely. Bands are drawn as a
      // *pair* of line datasets with a fill between them, and only one of the
      // pair should speak for the band — the other is scaffolding, and a
      // legend entry for it would invite toggling half a band off.
      if (ds.skipLegend) return;
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

      // Summing a dataset is right for a count and wrong for a percentile —
      // and on a partial period, right for both and comparable for neither.
      // `legendValue` states the number that actually compares; an explicit
      // null says this series has no meaningful total and shows none.
      if (ds.legendValue !== null) {
        const value = document.createElement("span");
        value.className = "chart-legend__value";
        value.textContent = _fmt(
          Number.isFinite(ds.legendValue) ? ds.legendValue : total(ds)
        );
        btn.appendChild(value);
      }

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
 * The doughnut/pie counterpart to renderChartLegend.
 *
 * A pie has ONE dataset whose `data[i]` values share ONE label array and ONE
 * `backgroundColor` array — there is no per-series dataset to iterate, so
 * renderChartLegend's approach (one entry per `chart.data.datasets[i]`) reads
 * a single, meaningless "Series 1" entry for a doughnut. Toggling a slice also
 * uses a different Chart.js API: `toggleDataVisibility(index)` at the CHART
 * level, not `setDatasetVisibility` at the dataset level.
 *
 * Each entry shows its share of the total, since "38%" is what a pie is for —
 * a bare count would make the reader do the division themselves.
 */
export function renderPieLegend(host, chart) {
  if (!host) return { refresh() {} };
  const ds = chart.data.datasets[0] || {};
  const colors = Array.isArray(ds.backgroundColor) ? ds.backgroundColor : [];
  const total = (ds.data || []).reduce((a, v) => a + (Number.isFinite(v) ? v : 0), 0);

  function paint() {
    host.className = "chart-legend";
    host.replaceChildren();
    (chart.data.labels || []).forEach((label, i) => {
      const visible = chart.getDataVisibility(i);
      const value = ds.data ? ds.data[i] : null;
      const pct = total > 0 && Number.isFinite(value) ? Math.round((value / total) * 100) : null;

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chart-legend__item";
      btn.setAttribute("aria-pressed", String(visible));

      const swatch = document.createElement("span");
      swatch.className = "chart-legend__swatch";
      swatch.style.background = colors[i] || SERIES_OVERFLOW;
      btn.appendChild(swatch);

      const lbl = document.createElement("span");
      lbl.className = "chart-legend__label";
      lbl.textContent = label;
      btn.appendChild(lbl);

      const val = document.createElement("span");
      val.className = "chart-legend__value";
      val.textContent = pct === null ? _fmt(value) : `${_fmt(value)} (${pct}%)`;
      btn.appendChild(val);

      btn.addEventListener("click", () => {
        chart.toggleDataVisibility(i);
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


/** The live edge's dash. Short enough to read as one line, not a row of ticks. */
const PROVISIONAL_DASH = [2, 2];

/**
 * Dataset props that draw a current-period line's live edge as provisional.
 *
 * The last bucket of a period in progress holds a real count of a fraction of
 * an hour, and drawn like every other point it reads as a crash — at 15:05 a
 * roaring hour and a dead one are the same five minutes of data. So the tail
 * is marked rather than dropped, which would throw away the one thing the
 * reader came for: what is happening right now.
 *
 * Two encodings. The dash needs explaining; the hollow ring at the end of the
 * line reads as "not closed yet" on its own, and survives both colour-vision
 * deficiency and the phone-sized render where a dash pattern is a smudge.
 *
 * `partialFrom` is the FIRST provisional index, which for a smoothed line sits
 * before the live edge — a centred mean has already pulled its neighbour
 * toward the partial hour. The ring goes on the last drawn point either way.
 *
 * An already-dashed line (an extra series) gets the ring only: a second dash
 * pattern beside the first says nothing a reader can decode.
 */
function liveEdgeProps(values, partialFrom, color, { dashed = false } = {}) {
  if (!Number.isInteger(partialFrom) || partialFrom < 0) return {};
  const points = Array.isArray(values) ? values : [];
  let last = -1;
  for (let i = 0; i < points.length; i += 1) {
    if (points[i] !== null && points[i] !== undefined) last = i;
  }
  if (last < partialFrom) return {};
  const props = {
    pointRadius: (ctx) => (ctx.dataIndex === last ? 4 : 0),
    pointBackgroundColor: CHART_SURFACE,
    pointBorderColor: color,
    pointBorderWidth: 2,
  };
  if (!dashed) {
    props.segment = {
      borderDash: (ctx) => (ctx.p1DataIndex > partialFrom - 1 ? PROVISIONAL_DASH : undefined),
    };
  }
  return props;
}

/**
 * The band chart: this period against a p25–p75 envelope over the last N.
 *
 * The envelope is a *pair* of line datasets with a fill between them, which is
 * how Chart.js draws a band. Only the upper one carries the legend entry — the
 * lower is scaffolding and opts out via `skipLegend`, so nobody can toggle off
 * half a band and be left with a stray line.
 *
 * Draw order matters: the band goes in first so the two lines sit on top of it
 * rather than under a translucent wash.
 *
 * `data.counts` is drawn as handed over: a caller smoothing the current line
 * passes the smoothed series here and names it in `currentNote`, while keeping
 * the raw one for the table and the totals.
 */
export function makeOverlayChart(
  canvas, data,
  { subject, typical, isWeek, currentTotal, typicalToDate, extraSeries = [], currentNote = "" }
) {
  const hasBand = (data.band_mid || []).length > 0;

  const datasets = [];
  if (hasBand) {
    datasets.push({
      label: `${typical} (p25–p75)`,
      data: data.band_high,
      borderColor: "transparent",
      backgroundColor: OVERLAY_BAND_FILL,
      borderWidth: 0,
      pointRadius: 0,
      pointHitRadius: 0,
      fill: "+1",
      tension: 0.2,
      // A spread is not a quantity you can add up.
      legendValue: null,
      order: 3,
    });
    datasets.push({
      label: `${typical} p25`,
      data: data.band_low,
      borderColor: "transparent",
      backgroundColor: "transparent",
      borderWidth: 0,
      pointRadius: 0,
      pointHitRadius: 0,
      fill: false,
      tension: 0.2,
      skipLegend: true,
      order: 3,
    });
    datasets.push({
      label: `${typical} (median)`,
      data: data.band_mid,
      borderColor: OVERLAY_PAST,
      backgroundColor: "transparent",
      // Dashed, so the two lines stay distinguishable without colour.
      borderDash: [6, 4],
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.2,
      legendValue: typicalToDate,
      order: 2,
    });
  }
  datasets.push({
    // `currentNote` names a transform the caller applied to this line — "3-hour
    // average". The legend is the only place a reader learns the line is not
    // the raw series, so it is a label, never a tooltip or a footnote.
    label: currentNote ? `${subject} so far (${currentNote})` : `${subject} so far`,
    data: data.counts,
    borderColor: OVERLAY_NOW,
    backgroundColor: "transparent",
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 4,
    tension: 0.2,
    // The current period stops at the hour we are in. Never bridge the gap —
    // a line drawn across unlived hours is a claim about the future.
    spanGaps: false,
    legendValue: currentTotal,
    order: 1,
    // ...and the hour we ARE in is drawn while incomplete, so it is marked
    // provisional rather than left to read as a collapse.
    ...liveEdgeProps(data.counts, data.partial_from, OVERLAY_NOW),
  });
  // Extra lines ride the SAME y-axis as everything else. A second scale would
  // make where this line sits relative to the band an artefact of autoscaling
  // rather than a fact — the exact misread _makeActivityChart split the
  // members chart out to avoid. A caller whose extra series is not in the same
  // unit as `data.counts` must draw its own chart, not pass it here.
  extraSeries.forEach((s, i) => {
    const extraColor = s.color || OVERLAY_EXTRA[i % OVERLAY_EXTRA.length];
    datasets.push({
      label: s.label,
      data: s.data,
      borderColor: extraColor,
      backgroundColor: "transparent",
      // Dotted, so this line separates from the solid current period and the
      // dashed median without relying on colour.
      borderDash: s.dash || [2, 3],
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.2,
      spanGaps: false,
      legendValue: s.total,
      order: 0,
      // An extra series runs to the same live edge the current line does — it
      // is the same period, sliced differently — so it wears the ring too.
      ...liveEdgeProps(
        s.data,
        s.partialFrom ?? data.partial_from,
        extraColor,
        { dashed: true },
      ),
    });
  });

  return new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels: data.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        // Both drawn in HTML by the caller — see the caption and legend hosts.
        title: { display: false },
        legend: { display: false },
        tooltip: {
          callbacks: {
            // 168 ticks cannot all be shown, so the tooltip is where the exact
            // hour lives.
            title: (items) => data.labels[items[0].dataIndex] || "",
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: CHART_TEXT,
            maxRotation: 0,
            autoSkip: false,
            // A tick per day across a week, every third hour across a day —
            // 168 labels would be a grey smear.
            callback(value, index) {
              if (isWeek) {
                return index % 24 === 0
                  ? (data.labels[index] || "").split(" ")[0]
                  : "";
              }
              return index % 3 === 0 ? data.labels[index] : "";
            },
          },
          grid: { color: CHART_GRID },
        },
        y: {
          ticks: { color: CHART_TEXT },
          grid: { color: CHART_GRID },
          beginAtZero: true,
          title: { display: true, text: data.y_label, color: CHART_TEXT },
        },
      },
    },
  });
}
