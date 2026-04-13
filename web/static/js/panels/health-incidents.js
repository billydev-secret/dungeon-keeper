import { api } from "../api.js";
import { makeLineChart, makeBarChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const SEVERITY_COLORS = { critical: "#9E3B2E", high: "#E6B84C", medium: "#B88A2C", low: "#949ba4" };

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading incident data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/incidents");
    const panel = container.querySelector(".panel");

    const catEntries = Object.entries(d.categories || {});
    const catHTML = catEntries.length
      ? catEntries.map(([type, count]) =>
          `<div class="incident-cat"><span class="incident-cat-name">${esc(type.replace(/_/g, " "))}</span><span class="incident-cat-count">${count}</span></div>`
        ).join("")
      : '<div class="home-dim">None this week</div>';

    const logRows = (d.incident_log || []).map(i => `
      <tr>
        <td>${fmtTime(i.detected_at)}</td>
        <td><span class="risk-tier risk-${i.severity || "medium"}">${esc(i.type.replace(/_/g, " "))}</span></td>
        <td>${i.severity || "—"}</td>
        <td>${i.channel_name ? "#" + esc(i.channel_name) : i.channel_id || "—"}</td>
        <td>${i.resolved_at ? fmtTime(i.resolved_at) : '<span class="risk-tier risk-critical">Active</span>'}</td>
        <td>${i.duration_min != null ? i.duration_min + "m" : "—"}</td>
      </tr>
    `).join("");

    panel.innerHTML = `
      <header>
        <h2>Incident Detection</h2>
        <div class="subtitle">${d.active_count} active incident${d.active_count !== 1 ? "s" : ""}</div>
      </header>

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          Incidents are automatically detected when activity patterns deviate sharply from normal — things like sudden message spikes, mass joins/leaves, or sentiment crashes.
          Each incident is categorized by type and severity. Active incidents haven't been resolved yet and may need moderator attention.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Active Incidents</div>
          <div class="home-card-big" style="color:${d.active_count > 0 ? "#9E3B2E" : "#7F8F3A"}">${d.active_count}</div>
          <div class="home-card-sub">${d.badge === "clear" ? "All clear" : "Needs attention"}</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Incidents This Week</div>
          <div class="home-card-big">${(d.incident_log || []).length}</div>
          <div class="home-card-sub">Detected anomalies and mod events</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Alert Categories (7d)</div>
          <div class="incident-cats">${catHTML}</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">7-Day Incident Timeline</div>
        <div class="chart-wrap" style="height:240px"><canvas id="incident-timeline"></canvas></div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Incident Log</div>
        <div class="data-table-scroll">
        <table class="data-table">
          <thead><tr><th>Detected</th><th>Type</th><th>Severity</th><th>Channel</th><th>Resolved</th><th>Duration</th></tr></thead>
          <tbody>${logRows || '<tr><td colspan="6" class="home-dim">No incidents this week</td></tr>'}</tbody>
        </table>
        </div>
      </div>
    `;

    // Timeline bar chart
    const tlCanvas = panel.querySelector("#incident-timeline");
    if (tlCanvas && d.timeline) {
      const dayLabels = [];
      for (let i = 6; i >= 0; i--) {
        const dt = new Date(Date.now() - i * 86400000);
        dayLabels.push(dt.toLocaleDateString([], { weekday: "short" }));
      }
      charts.push(makeBarChart(tlCanvas, {
        labels: dayLabels,
        datasets: [{
          label: "Incidents",
          data: d.timeline,
          backgroundColor: d.timeline.map(v => v > 0 ? "#E6B84C" : "rgba(148,155,164,0.3)"),
        }],
        title: "Daily Incidents",
        yLabel: "Count",
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
