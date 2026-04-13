import { api } from "../api.js";
import { makeBarChart, makeDoughnutChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

const INTERVALS = [
  { value: "",   label: "All Time" },
  { value: "1",  label: "Last 24h" },
  { value: "7",  label: "Last 7 Days" },
  { value: "14", label: "Last 14 Days" },
  { value: "30", label: "Last 30 Days" },
  { value: "90", label: "Last 90 Days" },
];

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>XP Leaderboard</h2>
        <div class="subtitle">XP distribution, level spread, and top earners</div>
      </header>
      <div class="controls">
        <label>Time Period
          <select data-control="days">
            ${INTERVALS.map((i) => `<option value="${i.value}">${i.label}</option>`).join("")}
          </select>
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;">
        <div class="chart-wrap" style="flex:2;min-width:300px;"><canvas data-chart-levels></canvas></div>
        <div class="chart-wrap" style="flex:1;min-width:200px;"><canvas data-chart-sources></canvas></div>
      </div>
      <div class="chart-wrap" style="margin-top:12px;min-height:220px;"><canvas data-chart-histogram></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chartLevels = null;
  let chartSources = null;
  let chartHistogram = null;

  if (initialParams.days) daysEl.value = initialParams.days;

  function fmtXp(n) {
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return Math.round(n).toString();
  }

  async function refresh() {
    const params = {};
    if (daysEl.value) params.days = daysEl.value;
    history.replaceState(null, "", `#/xp-leaderboard${daysEl.value ? "?days=" + daysEl.value : ""}`);

    try {
      const data = await api("/api/reports/xp-leaderboard", params);
      if (chartLevels) { chartLevels.destroy(); chartLevels = null; }
      if (chartSources) { chartSources.destroy(); chartSources = null; }
      if (chartHistogram) { chartHistogram.destroy(); chartHistogram = null; }

      const label = INTERVALS.find((i) => i.value === daysEl.value)?.label || "All Time";
      statsEl.textContent = `${data.total_users} users tracked · ${label}`;

      // Level distribution
      const levelWrap = container.querySelector("[data-chart-levels]").parentElement;
      if (data.level_distribution.length) {
        levelWrap.innerHTML = '<canvas data-chart-levels></canvas>';
        chartLevels = makeBarChart(container.querySelector("[data-chart-levels]"), {
          labels: data.level_distribution.map((b) => `Lv ${b.level}`),
          data: data.level_distribution.map((b) => b.count),
          title: "Level Distribution",
          yLabel: "Members",
          color: "#E6B84C",
        });
      }

      // Source breakdown
      const srcWrap = container.querySelector("[data-chart-sources]").parentElement;
      const srcLabels = Object.keys(data.source_totals);
      if (srcLabels.length) {
        srcWrap.innerHTML = '<canvas data-chart-sources></canvas>';
        chartSources = makeDoughnutChart(container.querySelector("[data-chart-sources]"), {
          labels: srcLabels.map((s) => s.replace("_", " ")),
          data: srcLabels.map((s) => data.source_totals[s]),
          title: "XP by Source",
        });
      }

      // XP histogram – 10 buckets each spanning 10% of the range
      if (data.leaderboard.length > 1) {
        const xpValues = data.leaderboard.map((r) => r.total_xp);
        const minXp = Math.min(...xpValues);
        const maxXp = Math.max(...xpValues);
        const range = maxXp - minXp || 1;
        const bucketCount = 10;
        const bucketSize = range / bucketCount;
        const buckets = Array(bucketCount).fill(0);
        for (const xp of xpValues) {
          let idx = Math.floor((xp - minXp) / bucketSize);
          if (idx >= bucketCount) idx = bucketCount - 1;
          buckets[idx]++;
        }
        const histLabels = buckets.map((_, i) => {
          const lo = minXp + i * bucketSize;
          const hi = lo + bucketSize;
          return `${fmtXp(lo)}–${fmtXp(hi)}`;
        });
        const histWrap = container.querySelector("[data-chart-histogram]").parentElement;
        histWrap.innerHTML = '<canvas data-chart-histogram></canvas>';
        chartHistogram = makeBarChart(container.querySelector("[data-chart-histogram]"), {
          labels: histLabels,
          data: buckets,
          title: "XP Distribution",
          xLabel: "XP Range",
          yLabel: "Members",
          color: "#B36A92",
        });
      }

      if (data.leaderboard.length) {
        // Compute median total_xp
        const sorted = [...data.leaderboard].sort((a, b) => a.total_xp - b.total_xp);
        const mid = Math.floor(sorted.length / 2);
        const median = sorted.length % 2 === 0
          ? (sorted[mid - 1].total_xp + sorted[mid].total_xp) / 2
          : sorted[mid].total_xp;

        // Assign rank by total_xp descending
        const ranked = [...data.leaderboard].sort((a, b) => b.total_xp - a.total_xp);
        const rankMap = {};
        ranked.forEach((r, i) => { rankMap[r.user_id] = i + 1; });

        // Enrich rows
        for (const r of data.leaderboard) {
          r._rank = rankMap[r.user_id];
          r._diff = r.total_xp - median;
        }

        renderSortableTable(tableWrap, {
          columns: [
            { key: "_rank", label: "Rank" },
            { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
            { key: "level", label: "Level" },
            { key: "total_xp", label: "Total XP", format: (v) => fmtXp(v) },
            { key: "_diff", label: "vs Median", format: (v) => {
              const s = v >= 0 ? "+" + fmtXp(v) : "\u2212" + fmtXp(Math.abs(v));
              const color = v >= 0 ? "#7F8F3A" : "#9E3B2E";
              return `<span style="color:${color}">${s}</span>`;
            }},
            { key: "text_xp", label: "Text", format: (v) => fmtXp(v) },
            { key: "voice_xp", label: "Voice", format: (v) => fmtXp(v) },
            { key: "reply_xp", label: "Reply", format: (v) => fmtXp(v) },
            { key: "react_xp", label: "React", format: (v) => fmtXp(v) },
          ],
          data: data.leaderboard,
          defaultSort: "_rank",
          defaultAsc: true,
        });
      } else { tableWrap.innerHTML = ""; }
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector("[data-chart-levels]").parentElement.innerHTML = `<div class="error">${err.message}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return {
    unmount() {
      if (chartLevels) { chartLevels.destroy(); chartLevels = null; }
      if (chartSources) { chartSources.destroy(); chartSources = null; }
      if (chartHistogram) { chartHistogram.destroy(); chartHistogram = null; }
    },
  };
}
