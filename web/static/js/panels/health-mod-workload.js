import { api } from "../api.js";
import { makeBarChart, makeHorizontalBarChart, makeDoughnutChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading mod workload data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/mod-workload");
    const panel = container.querySelector(".panel");

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

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          Combines audit-log actions (jails, warns, ticket ops) with mod-channel messages to give a full picture of who's active.
          <strong style="color:var(--text-normal, #dbdee1);">Workload Gini</strong> shows whether the work is shared evenly — close to 1 means one mod is doing almost everything.
          <strong style="color:var(--text-normal, #dbdee1);">Escalation rate</strong> tracks how often warnings lead to jails. <strong style="color:var(--text-normal, #dbdee1);">Recidivism</strong> tracks repeat offenders within 14 days.
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
          <div class="chart-wrap" style="min-height:280px"><canvas id="mod-bar-chart"></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Action Type Breakdown</div>
          <div class="chart-wrap" style="height:280px"><canvas id="action-type-chart"></canvas></div>
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

    // Mod actions horizontal bar chart
    const modCanvas = panel.querySelector("#mod-bar-chart");
    if (modCanvas && d.mod_actions && d.mod_actions.length) {
      charts.push(makeHorizontalBarChart(modCanvas, {
        labels: d.mod_actions.map(m => m.user_name || m.user_id),
        data: d.mod_actions.map(m => m.count),
        title: "Activity per Moderator (7d)",
        xLabel: "Activity",
        colors: d.mod_actions.map((_, i) => ROLE_COLORS[i % ROLE_COLORS.length]),
      }));
    }

    // Action type doughnut
    const typeCanvas = panel.querySelector("#action-type-chart");
    if (typeCanvas && d.action_types && d.action_types.length) {
      const top8 = d.action_types.slice(0, 8);
      charts.push(makeDoughnutChart(typeCanvas, {
        labels: top8.map(a => a.action),
        data: top8.map(a => a.count),
        title: "Action Types (7d)",
        colors: top8.map((_, i) => ROLE_COLORS[i % ROLE_COLORS.length]),
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
