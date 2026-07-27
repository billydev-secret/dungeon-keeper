import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { renderEmpty, renderError } from "../states.js";
import { makeHorizontalBarChart, makeBarChart, makeDoughnutChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

function scoreColor(score) {
  if (score >= 75) return "var(--green)";
  if (score >= 50) return "var(--yellow)";
  return "var(--red)";
}

const STATUS_COLORS = {
  healthy: "var(--green)",
  flagged: "var(--yellow)",
  dormant: "var(--ink-dim)",
  archive: "var(--red)",
};

const METRICS = [
  { value: "message_count",  label: "Messages" },
  { value: "total_xp",       label: "XP Earned" },
  { value: "gini",           label: "Gini Coefficient (Conversation Spread)" },
  { value: "avg_sentiment",  label: "Average Sentiment" },
  { value: "unique_authors", label: "Unique Authors" },
  { value: "trend_pct",      label: "Trend (Percent Change)" },
];

export function mount(container, initialParams) {
  const defaultMetric = initialParams.metric || "message_count";
  const metricOptions = METRICS.map(
    (m) => `<option value="${m.value}"${m.value === defaultMetric ? " selected" : ""}>${m.label}</option>`
  ).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Channels</h2>
        <div class="subtitle">Which channels are alive — 30-day health status, plus windowed side-by-side comparison</div>
      </header>
      <div data-health-section>
        <div class="panel-loading">Loading channel health…</div>
      </div>
      <h3 style="margin:20px 0 8px;">Compare Channels</h3>
      <div class="controls">
        <label style="max-width:100%; min-width:0;">Metric
          <select data-control="metric" style="max-width:100%;">${metricOptions}</select>
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const charts = [];
  let compareChart = null;

  // ── Health overview (30d status/score, /api/health/channel-health) ──
  const healthEl = container.querySelector("[data-health-section]");

  async function loadHealth() {
    const d = await api("/api/health/channel-health");
    const chs = d.channels || [];
    if (!chs.length) {
      healthEl.innerHTML = renderEmpty(
        "No channel activity on record yet. This report fills in once members have been posting for about a week."
      );
      return;
    }

    const scores = chs.filter(c => c.status === "healthy" || c.status === "flagged").map(c => c.score);
    const avgScore = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(1) : "—";
    const totalMsgsDay = chs.reduce((s, c) => s + c.msgs_per_day, 0).toFixed(0);

    const statusOrder = { healthy: 0, flagged: 1, dormant: 2, archive: 3 };
    const sorted = [...chs].sort((a, b) => (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9) || b.score - a.score);

    const tableRows = sorted.map(ch => `
      <tr class="ch-row-${ch.status}">
        <td>#${esc(ch.channel_name || ch.channel_id)}</td>
        <td><span class="health-tile-badge" style="background:${STATUS_COLORS[ch.status] || "var(--ink-dim)"};font-size:11px;">${ch.status}</span></td>
        <td style="color:${scoreColor(ch.score)}">${ch.score}</td>
        <td>${ch.msgs_per_day}</td>
        <td>${ch.unique_weekly_users}</td>
        <td>${ch.avg_thread_depth}</td>
        <td>${ch.gini}</td>
        <td>${ch.is_nsfw ? "yes" : ""}</td>
      </tr>
    `).join("");

    healthEl.innerHTML = `
      <div class="subtitle" style="margin-bottom:8px;">${d.active_count} active &middot; ${d.flagged_count} flagged &middot; ${d.dormant_count} dormant &middot; ${d.archive_count || 0} archive candidates</div>

      <details class="panel-about">
        <summary>About the health score</summary>
        <div class="note">
          Each channel gets a health score (0–100) based on message volume, unique users, conversation depth, and activity distribution.
          <strong>Healthy</strong> channels have regular activity from multiple people.
          <strong>Flagged</strong> channels are still active but declining or dominated by very few people.
          <strong>Dormant</strong> channels have little to no recent activity. <strong>Archive candidates</strong> have been dead long enough to consider removing.
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
          <div class="chart-wrap" style="height:260px"><canvas data-status-doughnut></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Score Distribution</div>
          <div class="chart-wrap" style="height:260px"><canvas data-score-dist></canvas></div>
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

    const statusCanvas = healthEl.querySelector("[data-status-doughnut]");
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

    const distCanvas = healthEl.querySelector("[data-score-dist]");
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
  }

  // ── Windowed comparison (/api/reports/channel-comparison) ───────────
  const rangeEl = rangePicker({ value: initialParams.days || 1, allowAll: false, label: "Days" });
  container.querySelector(".controls").prepend(rangeEl);
  const daysEl   = rangeEl.querySelector("select");
  const metricEl = container.querySelector('[data-control="metric"]');
  const tableWrap = container.querySelector("[data-table-wrap]");

  async function refreshCompare() {
    const days   = parseInt(daysEl.value) || 1;
    const metric = metricEl.value;
    const metricDef = METRICS.find((m) => m.value === metric) || METRICS[0];

    const qs = new URLSearchParams({ days, metric });
    history.replaceState(null, "", `#/channels?${qs}`);

    const wrap = container.querySelector(".chart-wrap");
    try {
      const data = await withLoading(wrap, api("/api/reports/channel-comparison", { days }));
      if (compareChart) { compareChart.destroy(); compareChart = null; }

      const sorted = [...data.channels].sort((a, b) => {
        const av = a[metric] ?? -Infinity;
        const bv = b[metric] ?? -Infinity;
        return bv - av;
      });
      const channels = sorted.slice(0, 25);

      if (!channels.length) {
        wrap.innerHTML = `<div class="empty">No channel activity in this window. Pick a longer range, or check that Dungeon Keeper can read your busy channels.</div>`;
        tableWrap.innerHTML = "";
        return;
      }

      wrap.innerHTML = '<canvas data-chart></canvas>';
      compareChart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: channels.map((c) => c.channel_name || c.channel_id),
        data:   channels.map((c) => c[metric] ?? 0),
        title:  `${metricDef.label} by Channel (last ${days} days)`,
        xLabel: metricDef.label,
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "channel_name",  label: "Channel",   format: (v, r) => r.channel_name || r.channel_id },
          { key: "message_count", label: "Messages",  format: (v) => v.toLocaleString() },
          { key: "unique_authors",label: "Authors" },
          { key: "total_xp",      label: "XP",        format: (v) => Math.round(v).toLocaleString() },
          { key: "gini",          label: "Gini",       format: (v) => v.toFixed(3) },
          { key: "avg_sentiment", label: "Sentiment", format: (v) => {
            if (v == null) return "—";
            const color = v > 0.05 ? "#7F8F3A" : v < -0.05 ? "#9E3B2E" : "#dbdee1";
            return `<span style="color:${color}">${v.toFixed(3)}</span>`;
          }},
          { key: "trend_pct",     label: "Trend",     format: (v) => {
            const color = v > 0 ? "#7F8F3A" : v < 0 ? "#9E3B2E" : "#dbdee1";
            return `<span style="color:${color}">${v > 0 ? "+" : ""}${v}%</span>`;
          }},
        ],
        data: data.channels,
        defaultSort: metric,
        emptyMsg: "No channel activity in this window.",
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">Couldn’t load the channel comparison — try again. (${esc(err.message)})</div>`;
      tableWrap.innerHTML = "";
    }
  }

  loadHealth().catch(err => {
    healthEl.innerHTML = renderError(
      `Couldn't load channel health — ${err.message}. Reload the page to try again.`
    );
  });
  daysEl.addEventListener("change", refreshCompare);
  metricEl.addEventListener("change", refreshCompare);
  refreshCompare();

  return { unmount() {
    charts.forEach(c => c.destroy());
    if (compareChart) { compareChart.destroy(); compareChart = null; }
  } };
}
