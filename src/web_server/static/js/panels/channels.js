import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { renderEmpty, renderError } from "../states.js";
import {
  makeHorizontalBarChart, makeBarChart, makeDoughnutChart,
  renderPieLegend, renderChartTable,
} from "../charts.js";
import { renderSortableTable } from "../table.js";

// Score is printed as table text, so these are the text-safe steps, not the
// saturated fills of the same hues.
function scoreColor(score) {
  if (score >= 75) return "var(--green-text)";
  if (score >= 50) return "var(--yellow)";
  return "var(--red-text)";
}

const STATUS_COLORS = {
  healthy: "var(--green)",
  flagged: "var(--yellow)",
  dormant: "var(--ink-dim)",
  archive: "var(--red-text)",
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
      <div class="chart-caption" data-compare-caption></div>
      <div class="chart-wrap" data-compare-wrap><canvas data-chart></canvas></div>
      <div data-compare-table></div>
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
          <div class="chart-caption" data-status-caption></div>
          <div class="chart-wrap" style="height:260px"><canvas data-status-doughnut></canvas></div>
          <div data-status-legend></div>
          <div data-status-table></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Score Distribution</div>
          <div class="chart-caption" data-dist-caption></div>
          <div class="chart-wrap" style="height:260px"><canvas data-score-dist></canvas></div>
          <div data-dist-table></div>
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
      const statusTitle = "Channel Status";
      const statusLabels = ["Healthy", "Flagged", "Dormant", "Archive"];
      const statusData = [
        d.active_count - d.flagged_count,
        d.flagged_count,
        d.dormant_count,
        d.archive_count || 0,
      ];
      const statusChart = makeDoughnutChart(statusCanvas, {
        labels: statusLabels,
        data: statusData,
        title: statusTitle,
        colors: ["#7F8F3A", "#E6B84C", "#949ba4", "#9E3B2E"],
      });
      charts.push(statusChart);

      // The caption lives in HTML (see activity.js) rather than on the
      // canvas, so it wears the page's type and is readable/selectable.
      const statusCaptionEl = healthEl.querySelector("[data-status-caption]");
      if (statusCaptionEl) statusCaptionEl.textContent = statusTitle;

      // 4 slices — a legend earns its place (2+ series/slices).
      const statusLegendEl = healthEl.querySelector("[data-status-legend]");
      if (statusLegendEl) renderPieLegend(statusLegendEl, statusChart);

      const statusTableEl = healthEl.querySelector("[data-status-table]");
      if (statusTableEl) {
        renderChartTable(statusTableEl, {
          labels: statusLabels,
          datasets: [{ label: "Channels", data: statusData }],
          indexLabel: "Status",
        });
      }
    }

    const distCanvas = healthEl.querySelector("[data-score-dist]");
    const distCaptionEl = healthEl.querySelector("[data-dist-caption]");
    const distTableEl = healthEl.querySelector("[data-dist-table]");
    if (distCanvas && scores.length) {
      const buckets = [0, 0, 0, 0, 0]; // 0-20, 20-40, 40-60, 60-80, 80-100
      for (const s of scores) {
        const idx = Math.min(4, Math.floor(s / 20));
        buckets[idx]++;
      }
      const distTitle = "Score Distribution";
      const distLabels = ["0–20", "20–40", "40–60", "60–80", "80–100"];
      const distChart = makeBarChart(distCanvas, {
        labels: distLabels,
        data: buckets,
        title: distTitle,
        yLabel: "Channels",
        color: ["#9E3B2E", "#B88A2C", "#E6B84C", "#7F8F3A", "#7F8F3A"],
      });
      charts.push(distChart);

      if (distCaptionEl) distCaptionEl.textContent = distTitle;
      // One series (bucketed counts) — no legend; the caption already names
      // it, per "none for one".
      if (distTableEl) {
        renderChartTable(distTableEl, {
          labels: distLabels,
          datasets: [{ label: "Channels", data: buckets }],
          indexLabel: "Score range",
        });
      }
    } else {
      if (distCaptionEl) distCaptionEl.textContent = "";
      if (distTableEl) distTableEl.replaceChildren();
    }
  }

  // ── Windowed comparison (/api/reports/channel-comparison) ───────────
  const rangeEl = rangePicker({ value: initialParams.days || 1, allowAll: false, label: "Days" });
  container.querySelector(".controls").prepend(rangeEl);
  const daysEl   = rangeEl.querySelector("select");
  const metricEl = container.querySelector('[data-control="metric"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  // Queried once, outside the .chart-wrap innerHTML-reset path below, so they
  // survive every destroy/recreate of the canvas — same pattern as
  // activity.js's captionEl/tableEl.
  const compareCaptionEl = container.querySelector("[data-compare-caption]");
  const compareTableEl = container.querySelector("[data-compare-table]");

  async function refreshCompare() {
    const days   = parseInt(daysEl.value) || 1;
    const metric = metricEl.value;
    const metricDef = METRICS.find((m) => m.value === metric) || METRICS[0];

    const qs = new URLSearchParams({ days, metric });
    history.replaceState(null, "", `#/channels?${qs}`);

    // [data-compare-wrap], not a bare .chart-wrap class lookup: the health
    // section above (loadHealth()) injects two chart-wraps of its own
    // (Status Breakdown, Score Distribution) earlier in the DOM once it
    // finishes loading. A bare class match returns the FIRST .chart-wrap in
    // document order, so after that section loads, every subsequent
    // refreshCompare() (the user changes Days or Metric) was overwriting the
    // Status Breakdown doughnut's wrap instead of this chart's own — pinned
    // by tests/web/test_chart_conventions.py.
    const wrap = container.querySelector("[data-compare-wrap]");
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
        compareCaptionEl.textContent = "";
        compareTableEl.replaceChildren();
        return;
      }

      wrap.innerHTML = '<canvas data-chart></canvas>';
      const compareTitle = `${metricDef.label} by Channel (last ${days} days)`;
      compareChart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: channels.map((c) => c.channel_name || c.channel_id),
        data:   channels.map((c) => c[metric] ?? 0),
        title:  compareTitle,
        xLabel: metricDef.label,
      });

      // Drawn in HTML rather than on the canvas — see activity.js. One series
      // (one metric, one bar per channel) needs no legend: the caption above
      // already names it.
      compareCaptionEl.textContent = compareTitle;
      renderChartTable(compareTableEl, {
        labels: channels.map((c) => c.channel_name || c.channel_id),
        datasets: [{ label: metricDef.label, data: channels.map((c) => c[metric] ?? 0) }],
        indexLabel: "Channel",
      });

      renderSortableTable(tableWrap, {
        columns: [
          // Every numeric format guards null: the comparison endpoint omits a
          // metric a channel has no data for (a channel with no scored messages
          // has no gini/sentiment/trend), and a bare v.toFixed() threw inside
          // the table renderer, blanking the entire table rather than one cell.
          { key: "channel_name",  label: "Channel",   format: (v, r) => r.channel_name || r.channel_id },
          { key: "message_count", label: "Messages",  format: (v) => (v ?? 0).toLocaleString() },
          { key: "unique_authors",label: "Authors" },
          { key: "total_xp",      label: "XP",        format: (v) => Math.round(v ?? 0).toLocaleString() },
          { key: "gini",          label: "Gini",       format: (v) => (v == null ? "—" : v.toFixed(3)) },
          // html: colored figures only — the interpolated value is a number and
          // the color a fixed literal (see table.js ESCAPING).
          { key: "avg_sentiment", label: "Sentiment", html: true, format: (v) => {
            if (v == null) return "—";
            const color = v > 0.05 ? "var(--green-text)" : v < -0.05 ? "var(--red-text)" : "var(--ink)";
            return `<span style="color:${color}">${v.toFixed(3)}</span>`;
          }},
          { key: "trend_pct",     label: "Trend",     html: true, format: (v) => {
            if (v == null) return "—";
            const color = v > 0 ? "var(--green-text)" : v < 0 ? "var(--red-text)" : "var(--ink)";
            return `<span style="color:${color}">${v > 0 ? "+" : ""}${Number(v)}%</span>`;
          }},
        ],
        data: data.channels,
        defaultSort: metric,
        emptyMsg: "No channel activity in this window.",
      });
    } catch (err) {
      container.querySelector("[data-compare-wrap]").innerHTML = `<div class="error">Couldn’t load the channel comparison — try again. (${esc(err.message)})</div>`;
      tableWrap.innerHTML = "";
      compareCaptionEl.textContent = "";
      compareTableEl.replaceChildren();
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
