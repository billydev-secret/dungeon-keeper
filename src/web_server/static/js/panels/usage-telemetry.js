// Command & Panel Usage — what actually gets used, and what never does.
//
// The headline is the "never used" pair at the top, not the leaderboards
// below it: those two lists are the only output of this report that tells you
// to *delete* something. Everything under them is supporting context.
import { api, esc, fmtTs } from "../api.js";
import { withLoading, rangePicker, syncHash } from "../report-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderEmpty, renderError } from "../states.js";
import { makeLineChart, makeBarChart } from "../charts.js";
import { allPageIds } from "../nav-registry.js";

const HOUR_LABELS = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, "0")}:00`);

const CODE = (v) => `<code>${esc(v)}</code>`;
// table.js escapes cell text itself now; fmtTs output goes through as text.
const TS = (v) => fmtTs(v);

// Shared by the command and panel tables; the command one appends Errors.
// `html: true` on the name column — CODE wraps the value in <code> and does its
// own esc() of the interpolated name (see table.js ESCAPING).
const nameColumns = (label) => [
  { key: "name", label, html: true, format: CODE },
  { key: "uses", label: "Uses", cls: "num" },
  { key: "users", label: "People", cls: "num" },
];

const COMMAND_COLUMNS = [
  ...nameColumns("Command"),
  {
    key: "errors", label: "Errors", cls: "num", html: true,
    format: (v) => `<span class="${v ? "num-err" : "num-dim"}">${Number(v) || 0}</span>`,
  },
  { key: "last_ts", label: "Last used", format: TS },
];

const PANEL_COLUMNS = [
  ...nameColumns("Panel"),
  { key: "last_ts", label: "Last used", format: TS },
];

const userColumns = (unitLabel) => [
  { key: "name", label: "Member", format: (v, row) => v || `User ${row.user_id}` },
  { key: "uses", label: "Total", cls: "num" },
  { key: "distinct_names", label: unitLabel, cls: "num" },
  { key: "last_ts", label: "Last seen", format: TS },
];

function unusedList(names, emptyMsg) {
  if (!names.length) return renderEmpty(emptyMsg);
  return `<div class="chip-row">${names
    .map((n) => `<span class="chip chip-warning"><code>${esc(n)}</code></span>`)
    .join(" ")}</div>`;
}

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Command &amp; Panel Usage</h2>
        <div class="subtitle">
          Which slash commands members run and which dashboard panels get opened.
          Recording started when this feature shipped — anything older isn't here.
        </div>
      </header>
      <div class="controls" data-controls></div>

      <div class="card-grid" data-stats></div>

      <h3 class="section-label">Never used</h3>
      <p class="subtitle">
        Judged against all recorded history, not the range above — a command last
        run months ago is unpopular, not unused. These are the deletion candidates.
      </p>
      <div data-unused></div>

      <h3 class="section-label">Slash commands</h3>
      <div data-commands></div>

      <h3 class="section-label">Dashboard panels</h3>
      <div data-panels></div>

      <h3 class="section-label">Over time</h3>
      <div class="chart-wrap"><canvas data-daily></canvas></div>
      <div class="chart-wrap"><canvas data-hours></canvas></div>

      <h3 class="section-label">Busiest members</h3>
      <div data-top-users></div>

      <h3 class="section-label">Dashboard visitors</h3>
      <div data-dash-users></div>
    </div>
  `;

  const controlsEl = container.querySelector("[data-controls]");
  const picker = rangePicker({ value: initialParams.days || "30", label: "Range" });
  controlsEl.appendChild(picker);
  const daysEl = picker.querySelector("select");

  // Markup is static after mount, so resolve the slots once rather than on
  // every range change.
  const el = Object.fromEntries(
    ["stats", "unused", "commands", "panels", "top-users", "dash-users", "daily", "hours"]
      .map((k) => [k, container.querySelector(`[data-${k}]`)]),
  );
  const panelEl = container.querySelector(".panel");

  let dailyChart = null;
  let hoursChart = null;

  function destroyCharts() {
    if (dailyChart) { dailyChart.destroy(); dailyChart = null; }
    if (hoursChart) { hoursChart.destroy(); hoursChart = null; }
  }

  async function refresh() {
    const days = daysEl.value || "30";
    syncHash("usage-telemetry", { days });
    try {
      const data = await withLoading(panelEl, api("/api/reports/usage", { days }));

      // The nav lives in app.js, so the browser is the only place that knows
      // every panel id that exists. Rather than shipping all ~139 ids up as a
      // query param, the server returns just the names it has seen and we
      // subtract here. Judged against all recorded history, not `days`.
      const seen = new Set(data.seen_panels || []);
      const unusedPanels = allPageIds().filter((id) => !seen.has(id)).sort();

      const t = data.totals || {};
      el.stats.innerHTML = `
        <div class="stat"><div class="stat-value">${t.commands || 0}</div><div class="stat-label">Commands run</div></div>
        <div class="stat"><div class="stat-value">${t.panel_views || 0}</div><div class="stat-label">Panel opens</div></div>
        <div class="stat stat-info"><div class="stat-value">${t.distinct_users || 0}</div><div class="stat-label">People</div></div>
        <div class="stat ${t.command_errors ? "stat-warning" : ""}"><div class="stat-value">${t.command_errors || 0}</div><div class="stat-label">Command errors</div></div>
      `;

      el.unused.innerHTML = `
        <h4>Slash commands never run <span class="chip chip-neutral">${data.unused_commands.length}</span></h4>
        ${unusedList(data.unused_commands, "Every registered command has been run at least once.")}
        <h4>Dashboard panels never opened <span class="chip chip-neutral">${unusedPanels.length}</span></h4>
        ${unusedList(unusedPanels, "Every panel has been opened at least once.")}
      `;

      // Sortable, like every other report table — "which command errors most"
      // is one click rather than a code change.
      const table = (slot, columns, rows, defaultSort, emptyMsg) =>
        renderSortableTable(slot, { columns, data: rows, defaultSort, emptyMsg });

      table(el.commands, COMMAND_COLUMNS, data.commands, "uses",
        "Nothing recorded in this range yet.");
      table(el.panels, PANEL_COLUMNS, data.panels, "uses",
        "Nothing recorded in this range yet.");
      table(el["top-users"], userColumns("Distinct commands"), data.top_users, "uses",
        "Nobody recorded in this range yet.");
      table(el["dash-users"], userColumns("Distinct panels"), data.dashboard_users, "uses",
        "Nobody recorded in this range yet.");

      destroyCharts();
      dailyChart = makeLineChart(el.daily, {
        labels: data.daily_commands.map((p) => p.day),
        // makeLineChart reads `counts`, not `data`.
        series: [
          { label: "Commands", counts: data.daily_commands.map((p) => p.count) },
          { label: "Panel opens", counts: data.daily_panels.map((p) => p.count) },
        ],
        title: "Daily usage",
      });
      hoursChart = makeBarChart(el.hours, {
        labels: HOUR_LABELS,
        data: data.hours,
        title: "Commands by hour of day",
        yLabel: "Commands",
      });
    } catch (err) {
      el.stats.innerHTML = renderError(err);
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount: destroyCharts };
}
