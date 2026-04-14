// Chart.js helpers. Palettes mirror services/activity_graphs.py.

export const ROLE_COLORS = [
  "#E6B84C", // poppy gold
  "#B36A92", // warm mauve
  "#7F8F3A", // golden moss
  "#9E3B2E", // clay red
  "#B88A2C", // shadow amber
  "#949ba4", // muted
];

export const GENDER_COLORS = {
  male:      "#E6B84C",
  female:    "#B36A92",
  nonbinary: "#7F8F3A",
  unknown:   "#949ba4",
};

const BAR     = "#E6B84C";
const ACCENT  = "#B36A92";
const TEXT    = "#dbdee1";
const GRID    = "#3f4147";

Chart.defaults.color = TEXT;
Chart.defaults.borderColor = GRID;
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif";

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
    wheel: { enabled: true },
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
  btn.title = "Double-click chart or press this button to reset zoom";
  btn.addEventListener("click", () => chart.resetZoom());
  wrap.style.position = "relative";
  wrap.appendChild(btn);
  // Also allow double-click on canvas to reset
  chart.canvas.addEventListener("dblclick", () => chart.resetZoom());
}

function merge(base, overrides) {
  // Shallow-ish merge good enough for chart options
  const result = structuredClone(base);
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
    borderColor: s.color || ROLE_COLORS[i % ROLE_COLORS.length],
    backgroundColor: (s.color || ROLE_COLORS[i % ROLE_COLORS.length]) + "33",
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
