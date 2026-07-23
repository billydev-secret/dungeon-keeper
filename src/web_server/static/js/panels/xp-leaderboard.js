import { api } from "../api.js";
import { withLoading, rangePicker } from "../report-helpers.js";
import { makeBarChart, makeDoughnutChart, CHART_BAR, CHART_ACCENT, ROLE_COLORS } from "../charts.js";
import { renderSortableTable } from "../table.js";
import { renderError } from "../states.js";

// The table is unbounded server-side, so cap the DOM and say so (W-D14).
const MAX_TABLE_ROWS = 200;

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>XP Leaderboard</h2>
        <div class="subtitle">XP distribution, level spread, and top earners</div>
      </header>
      <div class="controls">
        <label data-slot="range"></label>
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

  // Shared day-range picker, so every report offers the same windows (W-D8).
  const rangeCtl = rangePicker({
    value: initialParams.days || "",
    allowAll: true,
    label: "Time Period",
  });
  const daysEl = rangeCtl.querySelector("select");
  daysEl.dataset.control = "days";
  container.querySelector('[data-slot="range"]').replaceWith(rangeCtl);
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chartLevels = null;
  let chartSources = null;
  let chartHistogram = null;


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
      const data = await withLoading(container.querySelector(".chart-wrap"), api("/api/reports/xp-leaderboard", params));
      if (chartLevels) { chartLevels.destroy(); chartLevels = null; }
      if (chartSources) { chartSources.destroy(); chartSources = null; }
      if (chartHistogram) { chartHistogram.destroy(); chartHistogram = null; }

      const label = daysEl.value
        ? `last ${daysEl.value} day${daysEl.value === "1" ? "" : "s"}`
        : "all time";
      statsEl.textContent = `${data.total_users} member${data.total_users === 1 ? "" : "s"} tracked · ${label}`;

      // Level distribution
      const levelWrap = container.querySelector("[data-chart-levels]").parentElement;
      if (data.level_distribution.length) {
        levelWrap.innerHTML = '<canvas data-chart-levels></canvas>';
        chartLevels = makeBarChart(container.querySelector("[data-chart-levels]"), {
          labels: data.level_distribution.map((b) => `Lv ${b.level}`),
          data: data.level_distribution.map((b) => b.count),
          title: "Level Distribution",
          yLabel: "Members",
          color: CHART_BAR,
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
          color: CHART_ACCENT,
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
              const color = v >= 0 ? ROLE_COLORS[2] : ROLE_COLORS[3];
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
          maxRows: MAX_TABLE_ROWS,
          emptyMsg: "No members have earned XP in this period.",
        });
      } else {
        renderSortableTable(tableWrap, {
          columns: [],
          data: [],
          emptyMsg: "No members have earned XP in this period. Try a longer time period, or check that XP tracking is enabled.",
        });
      }
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector("[data-chart-levels]").parentElement.innerHTML = renderError(
        `Couldn't load the XP leaderboard — ${err.message}. Change the time period to try again.`
      );
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
