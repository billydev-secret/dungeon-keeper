import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import {
  makeHorizontalBarChart, makeDoughnutChart, renderPieLegend, renderChartTable, seriesColor,
} from "../charts.js";

// Chart title text, shown as the HTML caption above each chart (charts.js no
// longer draws these on the canvas) and still passed to the builder calls
// below — harmless there, but kept as one source of truth for both.
const MOD_CHART_TITLE = "Activity per Moderator (7d)";
const TYPE_CHART_TITLE = "Action Types (7d)";


export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading mod workload data…</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/mod-workload");
    const panel = container.querySelector(".panel");


    if (!(d.mod_actions || []).length) {
      panel.innerHTML = `<header><h2>Moderator Workload</h2><div class="subtitle">Who is carrying the moderation load</div></header>` +
        renderEmpty("No moderator actions on record yet. Warnings, jails, timeouts, and deletions show up here as your team uses them.");
      return;
    }

    const modRows = (d.mod_actions || []).map((m, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(m.user_name || m.user_id)}</td>
        <td>${m.count}</td>
        <td>${m.actions ?? "—"}</td>
        <td>${m.messages ?? "—"}</td>
      </tr>
    `).join("");

    const actionRows = (d.action_types || []).map(a => `
      <tr>
        <td>${esc(a.action)}</td>
        <td>${a.count}</td>
      </tr>
    `).join("");

    panel.innerHTML = `
      <header>
        <h2>Moderator Workload</h2>
        <div class="subtitle">${d.total_actions_7d} total activity this week</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          Combines audit-log actions (jails, warns, ticket ops) with mod-channel messages to give a full picture of who's active.
          <strong>Workload Gini</strong> shows whether the work is shared evenly — close to 1 means one mod is doing almost everything.
          <strong>Escalation rate</strong> tracks how often warnings lead to jails. <strong>Recidivism</strong> tracks repeat offenders within 14 days.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Median Response Time</div>
          <div class="home-card-big">${d.median_response_time}m</div>
          <div class="home-card-sub">Time to first mod action. P95: ${d.p95_response_time}m</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Total Activity (7d)</div>
          <div class="home-card-big">${d.total_actions_7d}</div>
          <div class="home-card-sub">${d.total_audit_actions_7d ?? 0} actions · ${d.total_messages_7d ?? 0} messages</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Workload Gini</div>
          <div class="home-card-big">${d.workload_gini}</div>
          <div class="home-card-sub">0 = equal, 1 = one mod does all</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Escalation Rate</div>
          <div class="home-card-big">${d.escalation_rate}%</div>
          <div class="home-card-sub">Warns leading to jails (30d)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Recidivism Rate</div>
          <div class="home-card-big">${d.recidivism_rate}%</div>
          <div class="home-card-sub">Repeat offenders within 14d</div>
        </div>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Activity per Moderator</div>
          <div class="chart-caption" data-caption="mod"></div>
          <div class="chart-wrap" style="min-height:280px"><canvas id="mod-bar-chart"></canvas></div>
          <div data-chart-table="mod"></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Action Type Breakdown</div>
          <div class="chart-caption" data-caption="type"></div>
          <div class="chart-wrap" style="height:280px"><canvas id="action-type-chart"></canvas></div>
          <div data-legend="type"></div>
          <div data-chart-table="type"></div>
        </div>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Moderator Leaderboard</div>
          <div class="data-table-scroll">
          <table class="data-table">
            <thead><tr><th>#</th><th>Moderator</th><th>Total</th><th>Actions</th><th>Messages</th></tr></thead>
            <tbody>${modRows || '<tr><td colspan="5" class="home-dim">No data</td></tr>'}</tbody>
          </table>
          </div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Action Types</div>
          <div class="data-table-scroll">
          <table class="data-table">
            <thead><tr><th>Action</th><th>Count</th></tr></thead>
            <tbody>${actionRows || '<tr><td colspan="2" class="home-dim">No data</td></tr>'}</tbody>
          </table>
          </div>
        </div>
      </div>
    `;

    // Mod actions horizontal bar chart. One series (Activity per moderator) —
    // per the "none for one" rule this gets a caption and a table, no legend;
    // the per-bar rainbow coloring is unchanged from before this pass.
    const modCanvas = panel.querySelector("#mod-bar-chart");
    if (modCanvas && d.mod_actions && d.mod_actions.length) {
      const modLabels = d.mod_actions.map(m => m.user_name || m.user_id);
      const modChart = makeHorizontalBarChart(modCanvas, {
        labels: modLabels,
        data: d.mod_actions.map(m => m.count),
        title: MOD_CHART_TITLE,
        xLabel: "Activity",
        colors: d.mod_actions.map((_, i) => seriesColor(i)),
      });
      charts.push(modChart);

      const modCaptionEl = panel.querySelector('[data-caption="mod"]');
      if (modCaptionEl) modCaptionEl.textContent = MOD_CHART_TITLE;
      renderChartTable(panel.querySelector('[data-chart-table="mod"]'), {
        labels: modLabels,
        datasets: [{ label: "Activity", data: modChart.data.datasets[0].data }],
        indexLabel: "Moderator",
      });
    }

    // Action type doughnut. Multiple slices — caption, pie legend, and table.
    const typeCanvas = panel.querySelector("#action-type-chart");
    if (typeCanvas && d.action_types && d.action_types.length) {
      const top8 = d.action_types.slice(0, 8);
      const typeChart = makeDoughnutChart(typeCanvas, {
        labels: top8.map(a => a.action),
        data: top8.map(a => a.count),
        title: TYPE_CHART_TITLE,
        // seriesColor, not a local modulo — SERIES_OVERFLOW past 6 slices
        // instead of two action types silently sharing a colour.
        colors: top8.map((_, i) => seriesColor(i)),
      });
      charts.push(typeChart);

      const typeCaptionEl = panel.querySelector('[data-caption="type"]');
      if (typeCaptionEl) typeCaptionEl.textContent = TYPE_CHART_TITLE;
      const typeLegendEl = panel.querySelector('[data-legend="type"]');
      if (typeLegendEl) renderPieLegend(typeLegendEl, typeChart);
      renderChartTable(panel.querySelector('[data-chart-table="type"]'), {
        labels: top8.map(a => a.action),
        datasets: [{ label: "Count", data: typeChart.data.datasets[0].data }],
        indexLabel: "Action",
      });
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = renderError(
      `Couldn't load moderator workload — ${err.message}. Reload the page to try again.`
    );
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
