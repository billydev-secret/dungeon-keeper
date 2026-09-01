import { api } from "../api.js";
import { withLoading, rangePicker } from "../report-helpers.js";
import {
  makeBarChart, makeDoughnutChart, renderPieLegend, renderChartTable,
  CHART_BAR, CHART_ACCENT,
} from "../charts.js";
import { renderSortableTable } from "../table.js";
import { renderError, renderEmpty } from "../states.js";

// The table is unbounded server-side, so cap the DOM and say so (W-D14).
const MAX_TABLE_ROWS = 200;

/**
 * XP Leaderboard — the level/rank report (Reports → Engagement).
 * Moderator-level information — no gating applies here. The curve and reward
 * dials live on Config → Members → XP & Leveling (config-xp), cross-linked
 * via `related:`.
 */
export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>XP Leaderboard</h2>
        <div class="subtitle">Who is earning XP, and where members rank</div>
      </header>
      <div data-region="leaderboard"></div>
    </div>
  `;
  return mountLeaderboard(container.querySelector('[data-region="leaderboard"]'), initialParams);
}

/**
 * The leaderboard body. Returns an object with unmount() so the caller can
 * destroy the charts.
 *
 * Laid out as its own stack of `.card` tiles — level distribution and source
 * breakdown side by side, then the XP histogram, then the member table —
 * rather than one undifferentiated block, so each reads as a distinct report
 * rather than one giant tile (Billy).
 */
export function mountLeaderboard(container, initialParams) {
  container.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:var(--s-5);">
      <div>
        <div class="controls">
          <label data-slot="range"></label>
        </div>
        <div data-stats class="subtitle" style="margin-top:8px;"></div>
      </div>

      <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr));">
        <section class="card">
          <div class="section-label">Level Distribution</div>
          <div class="field-hint" data-caption-levels></div>
          <div class="chart-wrap"><canvas data-chart-levels></canvas></div>
          <div data-chart-table-levels></div>
        </section>
        <section class="card">
          <div class="section-label">XP by Source</div>
          <div class="chart-wrap"><canvas data-chart-sources></canvas></div>
          <div data-legend-sources></div>
          <div data-chart-table-sources></div>
        </section>
      </div>

      <section class="card">
        <div class="section-label">XP Distribution</div>
        <div class="chart-wrap" style="min-height:220px;"><canvas data-chart-histogram></canvas></div>
        <div data-chart-table-histogram></div>
      </section>

      <section class="card">
        <div class="section-label">Leaderboard</div>
        <div data-table-wrap style="max-height:400px; overflow-y:auto;"></div>
      </section>
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
  // Not a chart title (the card's own section-label is that) — this is the
  // "active in the last N days" scope note, so the reader can see the
  // distribution isn't counting the whole membership (Billy, part b).
  const levelsCaptionEl = container.querySelector("[data-caption-levels]");
  const levelsTableEl = container.querySelector("[data-chart-table-levels]");
  const sourcesLegendEl = container.querySelector("[data-legend-sources]");
  const sourcesTableEl = container.querySelector("[data-chart-table-sources]");
  const histTableEl = container.querySelector("[data-chart-table-histogram]");
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

      // Level distribution — scoped server-side to members active in the
      // trailing window (level_distribution_active_days), independent of the
      // range picker above: that note stays visible even when the chart is
      // empty, since it's the answer to "why is this empty".
      const activeDays = data.level_distribution_active_days;
      const activeWindowNote = `Members active in the last ${activeDays} day${activeDays === 1 ? "" : "s"}.`;
      levelsCaptionEl.textContent = activeWindowNote;
      const levelWrap = container.querySelector("[data-chart-levels]").parentElement;
      if (data.level_distribution.length) {
        levelWrap.innerHTML = '<canvas data-chart-levels></canvas>';
        const levelLabels = data.level_distribution.map((b) => `Lv ${b.level}`);
        const levelCounts = data.level_distribution.map((b) => b.count);
        chartLevels = makeBarChart(container.querySelector("[data-chart-levels]"), {
          labels: levelLabels,
          data: levelCounts,
          title: "Level Distribution",
          yLabel: "Members",
          color: CHART_BAR,
        });
        renderChartTable(levelsTableEl, {
          labels: levelLabels,
          datasets: [{ label: "Members", data: levelCounts }],
          indexLabel: "Level",
        });
      } else {
        levelWrap.innerHTML = renderEmpty(`No members have been active in the last ${activeDays} days.`);
        levelsTableEl.replaceChildren();
      }

      // Source breakdown — a doughnut: legend + table once there's more than
      // one slice to distinguish (a single slice is just a full circle, so a
      // legend would repeat the card title for no reason).
      const srcWrap = container.querySelector("[data-chart-sources]").parentElement;
      const srcLabels = Object.keys(data.source_totals);
      if (srcLabels.length) {
        srcWrap.innerHTML = '<canvas data-chart-sources></canvas>';
        const srcDisplayLabels = srcLabels.map((s) => s.replace("_", " "));
        const srcData = srcLabels.map((s) => data.source_totals[s]);
        chartSources = makeDoughnutChart(container.querySelector("[data-chart-sources]"), {
          labels: srcDisplayLabels,
          data: srcData,
          title: "XP by Source",
        });
        sourcesLegendEl.replaceChildren();
        if (srcLabels.length > 1) renderPieLegend(sourcesLegendEl, chartSources);
        renderChartTable(sourcesTableEl, {
          labels: srcDisplayLabels,
          datasets: [{ label: "XP", data: srcData }],
          indexLabel: "Source",
        });
      } else {
        srcWrap.innerHTML = renderEmpty("No XP has been earned in this period.");
        sourcesLegendEl.replaceChildren();
        sourcesTableEl.replaceChildren();
      }

      // XP histogram – 10 buckets each spanning 10% of the range
      const histWrap = container.querySelector("[data-chart-histogram]").parentElement;
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
        histWrap.innerHTML = '<canvas data-chart-histogram></canvas>';
        chartHistogram = makeBarChart(container.querySelector("[data-chart-histogram]"), {
          labels: histLabels,
          data: buckets,
          title: "XP Distribution",
          xLabel: "XP Range",
          yLabel: "Members",
          color: CHART_ACCENT,
        });
        renderChartTable(histTableEl, {
          labels: histLabels,
          datasets: [{ label: "Members", data: buckets }],
          indexLabel: "XP Range",
        });
      } else {
        histWrap.innerHTML = renderEmpty("Not enough members in this period to show a spread.");
        histTableEl.replaceChildren();
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
            // html: colored ± figure, no user-supplied text (table.js ESCAPING).
            { key: "_diff", label: "vs Median", html: true, format: (v) => {
              const s = v >= 0 ? "+" + fmtXp(v) : "\u2212" + fmtXp(Math.abs(v));
              // Table text, not a mark: the chart palette is fitted against the chart
              // surface and drops to 2.31:1 as type here.
              const color = v >= 0 ? "var(--green-text)" : "var(--red-text)";
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
      // A failed fetch throws before this function's own destroy calls run, so
      // all three chart instances from the last successful load are still
      // alive here. Only the levels wrap used to be replaced with an error —
      // the other two kept showing a stale, now-untitled chart image (their
      // captions were cleared but the canvas underneath was not), which read
      // as more broken than a plain error message. Destroy and replace all
      // three, the same way, so the failure state is consistent everywhere.
      if (chartLevels) { chartLevels.destroy(); chartLevels = null; }
      if (chartSources) { chartSources.destroy(); chartSources = null; }
      if (chartHistogram) { chartHistogram.destroy(); chartHistogram = null; }
      const errMsg = renderError(
        `Couldn't load the XP leaderboard — ${err.message}. Change the time period to try again.`
      );
      container.querySelector("[data-chart-levels]").parentElement.innerHTML = errMsg;
      container.querySelector("[data-chart-sources]").parentElement.innerHTML = errMsg;
      container.querySelector("[data-chart-histogram]").parentElement.innerHTML = errMsg;
      levelsCaptionEl.textContent = "";
      levelsTableEl.replaceChildren();
      sourcesLegendEl.replaceChildren();
      sourcesTableEl.replaceChildren();
      histTableEl.replaceChildren();
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
