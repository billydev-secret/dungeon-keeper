import { api } from "../api.js";
import { makeHorizontalBarChart, makeBarChart, makeDoughnutChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function scoreColor(score) {
  if (score >= 75) return "var(--success)";
  if (score >= 50) return "var(--warning)";
  return "var(--danger)";
}

const STATUS_COLORS = {
  healthy: "var(--success)",
  flagged: "var(--warning)",
  dormant: "var(--text-dim)",
  archive: "var(--danger)",
};

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading channel health...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/channel-health");
    const panel = container.querySelector(".panel");
    const chs = d.channels || [];

    // Derived stats
    const scores = chs.filter(c => c.status === "healthy" || c.status === "flagged").map(c => c.score);
    const avgScore = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(1) : "—";
    const totalMsgsDay = chs.reduce((s, c) => s + c.msgs_per_day, 0).toFixed(0);

    // Table rows — show all channels grouped by status
    const statusOrder = { healthy: 0, flagged: 1, dormant: 2, archive: 3 };
    const sorted = [...chs].sort((a, b) => (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9) || b.score - a.score);

    const tableRows = sorted.map(ch => `
      <tr class="ch-row-${ch.status}">
        <td>#${esc(ch.channel_name || ch.channel_id)}</td>
        <td><span class="health-tile-badge" style="background:${STATUS_COLORS[ch.status] || "var(--text-dim)"};font-size:11px;">${ch.status}</span></td>
        <td style="color:${scoreColor(ch.score)}">${ch.score}</td>
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

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          Each channel gets a health score (0–100) based on message volume, unique users, conversation depth, and activity distribution.
          <strong style="color:var(--text-normal, #dbdee1);">Healthy</strong> channels have regular activity from multiple people.
          <strong style="color:var(--text-normal, #dbdee1);">Flagged</strong> channels are still active but declining or dominated by very few people.
          <strong style="color:var(--text-normal, #dbdee1);">Dormant</strong> channels have little to no recent activity. <strong style="color:var(--text-normal, #dbdee1);">Archive candidates</strong> have been dead long enough to consider removing.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Active Channels</div>
          <div class="home-card-big">${d.active_count}</div>
          <div class="home-card-sub">${d.flagged_count} need attention</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Average Score</div>
          <div class="home-card-big">${avgScore}</div>
          <div class="home-card-sub">Across active channels</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Total Msgs / Day</div>
          <div class="home-card-big">${totalMsgsDay}</div>
          <div class="home-card-sub">Server-wide (30d avg)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Dormant + Archive</div>
          <div class="home-card-big">${d.dormant_count + (d.archive_count || 0)}</div>
          <div class="home-card-sub">${d.dormant_count} dormant &middot; ${d.archive_count || 0} archive</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Status Breakdown</div>
          <div class="chart-wrap" style="height:260px"><canvas id="status-doughnut"></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Score Distribution</div>
          <div class="chart-wrap" style="height:260px"><canvas id="score-dist-chart"></canvas></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Thread Depth by Channel</div>
          <div class="chart-wrap" style="min-height:300px"><canvas id="depth-chart"></canvas></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Channel Roster</div>
          <div class="data-table-scroll">
          <table class="data-table">
            <thead><tr>
              <th>Channel</th><th>Status</th><th>Score</th><th>Msgs/day</th>
              <th>Users</th><th>Depth</th><th>Gini</th><th>NSFW</th>
            </tr></thead>
            <tbody>${tableRows}</tbody>
          </table>
          </div>
        </div>
      </div>
    `;

    // Status doughnut
    const statusCanvas = panel.querySelector("#status-doughnut");
    if (statusCanvas) {
      charts.push(makeDoughnutChart(statusCanvas, {
        labels: ["Healthy", "Flagged", "Dormant", "Archive"],
        data: [
          d.active_count - d.flagged_count,
          d.flagged_count,
          d.dormant_count,
          d.archive_count || 0,
        ],
        title: "Channel Status",
        colors: ["#7F8F3A", "#E6B84C", "#949ba4", "#9E3B2E"],
      }));
    }

    // Score distribution histogram
    const distCanvas = panel.querySelector("#score-dist-chart");
    if (distCanvas && scores.length) {
      const buckets = [0, 0, 0, 0, 0]; // 0-20, 20-40, 40-60, 60-80, 80-100
      for (const s of scores) {
        const idx = Math.min(4, Math.floor(s / 20));
        buckets[idx]++;
      }
      charts.push(makeBarChart(distCanvas, {
        labels: ["0–20", "20–40", "40–60", "60–80", "80–100"],
        data: buckets,
        title: "Score Distribution",
        yLabel: "Channels",
        color: ["#9E3B2E", "#B88A2C", "#E6B84C", "#7F8F3A", "#7F8F3A"],
      }));
    }

    // Thread depth bar chart
    const depthCanvas = panel.querySelector("#depth-chart");
    if (depthCanvas && chs.length) {
      const depthSorted = [...chs].filter(c => c.avg_thread_depth > 0).sort((a, b) => b.avg_thread_depth - a.avg_thread_depth).slice(0, 20);
      charts.push(makeHorizontalBarChart(depthCanvas, {
        labels: depthSorted.map(c => "#" + (c.channel_name || c.channel_id)),
        data: depthSorted.map(c => c.avg_thread_depth),
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
