// Command & Panel Usage — what actually gets used, and what never does.
//
// The headline is the "never used" pair at the top, not the leaderboards
// below it: those two lists are the only output of this report that tells you
// to *delete* something. Everything under them is supporting context.
import { api, esc, fmtTs } from "../api.js";
import { withLoading, rangePicker, syncHash } from "../report-helpers.js";
import { makeLineChart, makeBarChart } from "../charts.js";
import { allPageIds } from "../nav-registry.js";

const HOUR_LABELS = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, "0")}:00`);

function nameTable(rows, { nameHeader, showErrors }) {
  if (!rows.length) {
    return '<div class="empty">Nothing recorded in this range yet.</div>';
  }
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>${esc(nameHeader)}</th>
          <th class="num">Uses</th>
          <th class="num">People</th>
          ${showErrors ? '<th class="num">Errors</th>' : ""}
          <th>Last used</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((r) => `
          <tr>
            <td><code>${esc(r.name)}</code></td>
            <td class="num">${r.uses}</td>
            <td class="num">${r.users}</td>
            ${showErrors ? `<td class="num ${r.errors ? "num-err" : "num-dim"}">${r.errors}</td>` : ""}
            <td>${esc(fmtTs(r.last_ts))}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function userTable(rows, unitHeader) {
  if (!rows.length) {
    return '<div class="empty">Nobody recorded in this range yet.</div>';
  }
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Member</th>
          <th class="num">Total</th>
          <th class="num">${esc(unitHeader)}</th>
          <th>Last seen</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((r) => `
          <tr>
            <td>${esc(r.name || `User ${r.user_id}`)}</td>
            <td class="num">${r.uses}</td>
            <td class="num">${r.distinct_names}</td>
            <td>${esc(fmtTs(r.last_ts))}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function unusedList(names, emptyMsg) {
  if (!names.length) return `<div class="empty">${esc(emptyMsg)}</div>`;
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

  let dailyChart = null;
  let hoursChart = null;

  function destroyCharts() {
    if (dailyChart) { dailyChart.destroy(); dailyChart = null; }
    if (hoursChart) { hoursChart.destroy(); hoursChart = null; }
  }

  async function refresh() {
    const days = daysEl.value || "30";
    syncHash("usage-telemetry", { days });
    const panel = container.querySelector(".panel");
    try {
      const data = await withLoading(panel, api("/api/reports/usage", { days }));

      // The nav lives in app.js, so the browser is the only place that knows
      // every panel id that exists. Rather than shipping all ~139 ids up as a
      // query param, the server returns just the names it has seen and we
      // subtract here. Judged against all recorded history, not `days`.
      const seen = new Set(data.seen_panels || []);
      const unusedPanels = allPageIds().filter((id) => !seen.has(id)).sort();

      const t = data.totals || {};
      container.querySelector("[data-stats]").innerHTML = `
        <div class="stat"><div class="stat-value">${t.commands || 0}</div><div class="stat-label">Commands run</div></div>
        <div class="stat"><div class="stat-value">${t.panel_views || 0}</div><div class="stat-label">Panel opens</div></div>
        <div class="stat stat-info"><div class="stat-value">${t.distinct_users || 0}</div><div class="stat-label">People</div></div>
        <div class="stat ${t.command_errors ? "stat-warning" : ""}"><div class="stat-value">${t.command_errors || 0}</div><div class="stat-label">Command errors</div></div>
      `;

      container.querySelector("[data-unused]").innerHTML = `
        <h4>Slash commands never run <span class="chip chip-neutral">${data.unused_commands.length}</span></h4>
        ${unusedList(data.unused_commands, "Every registered command has been run at least once.")}
        <h4>Dashboard panels never opened <span class="chip chip-neutral">${unusedPanels.length}</span></h4>
        ${unusedList(unusedPanels, "Every panel has been opened at least once.")}
      `;

      container.querySelector("[data-commands]").innerHTML =
        nameTable(data.commands, { nameHeader: "Command", showErrors: true });
      container.querySelector("[data-panels]").innerHTML =
        nameTable(data.panels, { nameHeader: "Panel", showErrors: false });
      container.querySelector("[data-top-users]").innerHTML =
        userTable(data.top_users, "Distinct commands");
      container.querySelector("[data-dash-users]").innerHTML =
        userTable(data.dashboard_users, "Distinct panels");

      destroyCharts();
      dailyChart = makeLineChart(container.querySelector("[data-daily]"), {
        labels: data.daily_commands.map((p) => p.day),
        // makeLineChart reads `counts`, not `data`.
        series: [
          { label: "Commands", counts: data.daily_commands.map((p) => p.count) },
          { label: "Panel opens", counts: data.daily_panels.map((p) => p.count) },
        ],
        title: "Daily usage",
      });
      hoursChart = makeBarChart(container.querySelector("[data-hours]"), {
        labels: HOUR_LABELS,
        data: data.hours,
        title: "Commands by hour of day",
        yLabel: "Commands",
      });
    } catch (err) {
      container.querySelector("[data-stats]").innerHTML =
        `<div class="error">Couldn’t load usage — try again. (${esc(err.message)})</div>`;
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount: destroyCharts };
}
