import { api } from "../api.js";
import { makeHorizontalBarChart } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading channel health...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/channel-health");
    const panel = container.querySelector(".panel");

    const statusColors = { healthy: "var(--success)", flagged: "var(--warning)", dormant: "var(--text-dim)", archive: "var(--danger)" };

    const tableRows = (d.channels || []).filter(ch => ch.status === "healthy" || ch.status === "flagged").map(ch => `
      <tr>
        <td>#${esc(ch.channel_name || ch.channel_id)}</td>
        <td><span class="health-tile-badge" style="background:${statusColors[ch.status] || "var(--text-dim)"};font-size:11px;">${ch.status}</span></td>
        <td>${ch.score}</td>
        <td>${ch.msgs_per_day}</td>
        <td>${ch.unique_weekly_users}</td>
        <td>${ch.avg_thread_depth}</td>
        <td>${ch.gini}</td>
        <td>${ch.is_nsfw ? "yes" : ""}</td>
      </tr>
    `).join("");

    panel.innerHTML = `
      <header>
        <h2>Channel Health</h2>
        <div class="subtitle">${d.active_count} active &middot; ${d.flagged_count} flagged &middot; ${d.dormant_count} dormant &middot; ${d.archive_count || 0} archive candidates</div>
      </header>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Thread Depth by Channel</div>
        <div class="chart-wrap" style="min-height:300px"><canvas id="depth-chart"></canvas></div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;overflow-x:auto;">
        <div class="home-card-label">Channel Roster</div>
        <table class="data-table">
          <thead><tr>
            <th>Channel</th><th>Status</th><th>Score</th><th>Msgs/day</th>
            <th>Users</th><th>Depth</th><th>Gini</th><th>NSFW</th>
          </tr></thead>
          <tbody>${tableRows}</tbody>
        </table>
      </div>
    `;

    const depthCanvas = panel.querySelector("#depth-chart");
    if (depthCanvas && d.channels) {
      const sorted = [...d.channels].filter(c => c.avg_thread_depth > 0).sort((a, b) => b.avg_thread_depth - a.avg_thread_depth).slice(0, 20);
      charts.push(makeHorizontalBarChart(depthCanvas, {
        labels: sorted.map(c => "#" + (c.channel_name || c.channel_id)),
        data: sorted.map(c => c.avg_thread_depth),
        title: "Average Thread Depth",
        xLabel: "Avg replies per thread",
        color: "#7F8F3A",
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
