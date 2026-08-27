import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle, mountReloadable } from "../report-helpers.js";
import { makeLineChart, renderChartLegend, renderChartTable } from "../charts.js";


export function mount(container) {
  let includeBots = false;
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading retention data…</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/cohort-retention", includeBots ? { include_bots: "true" } : undefined);
    const panel = container.querySelector(".panel");


    if (!(d.cohorts || []).length) {
      panel.innerHTML = `<header><h2>Cohort Retention</h2><div class="subtitle">How well each week's arrivals stick around</div></header>` +
        renderEmpty("No cohorts to chart yet. A cohort appears once a group of members has joined and had at least a day to come back.");
      return;
    }

    // Cohort table
    const checkpoints = ["d1", "d7", "d14", "d30", "d60", "d90"];
    const headerCells = checkpoints.map(c => `<th>${c.toUpperCase()}</th>`).join("");
    const tableRows = (d.cohorts || []).map(c => {
      const cells = checkpoints.map(cp => {
        const val = c[cp];
        if (val === null || val === undefined) {
          return `<td style="color:var(--ink-mute)" title="Cohort hasn't aged to this checkpoint yet">—</td>`;
        }
        const bg = val >= 60 ? "rgba(127,143,58,0.3)" : val >= 30 ? "rgba(230,184,76,0.3)" : val > 0 ? "rgba(158,59,46,0.3)" : "transparent";
        return `<td style="background:${bg}">${val}%</td>`;
      }).join("");
      return `<tr><td>${esc(c.label)}</td><td>${c.size}</td>${cells}</tr>`;
    }).join("");

    const fmt = v => (v === null || v === undefined) ? "—" : `${v}%`;
    const headlineSub = d.d7_cohort_label
      ? `D7: ${fmt(d.d7)} &middot; D30: ${fmt(d.d30)} <span style="opacity:0.7">(cohort ${esc(d.d7_cohort_label)})</span>`
      : `D7: ${fmt(d.d7)} &middot; D30: ${fmt(d.d30)}`;

    panel.innerHTML = `
      <header>
        <h2>Cohort Retention</h2>
        <div class="subtitle">${headlineSub}</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          Members are grouped into weekly <strong>cohorts</strong> by when they joined.
          "D7 Retention" means the percentage of a cohort that sent at least one message 7 days after joining — and so on for D30, D60, D90.
          The table below tracks each cohort week over time, and the curves show whether your onboarding is improving or declining.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">D7 Retention</div>
          <div class="home-card-big">${fmt(d.d7)}</div>
          <div class="home-card-sub">Still active 7 days after joining (target: &gt;60%)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">D30 Retention</div>
          <div class="home-card-big">${fmt(d.d30)}</div>
          <div class="home-card-sub">Still active 30 days after joining (target: &gt;40%)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">D90 Retention</div>
          <div class="home-card-big">${fmt(d.d90)}</div>
          <div class="home-card-sub">Still active 90 days after joining (target: &gt;25%)</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Retention Curves by Cohort</div>
        <div class="chart-caption" data-caption></div>
        <div class="chart-wrap" style="height:320px"><canvas id="retention-curves"></canvas></div>
        <div data-legend></div>
        <div data-chart-table></div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Cohort Retention Table</div>
        <div class="data-table-scroll">
        <table class="data-table">
          <thead><tr><th>Cohort</th><th>Size</th>${headerCells}</tr></thead>
          <tbody>${tableRows}</tbody>
        </table>
        </div>
      </div>
    `;

    // Multi-line retention curves — up to 6 cohorts, one line each.
    const curvesCanvas = panel.querySelector("#retention-curves");
    const curvesCaptionEl = panel.querySelector("[data-caption]");
    const curvesLegendEl  = panel.querySelector("[data-legend]");
    const curvesTableEl   = panel.querySelector("[data-chart-table]");
    if (curvesCanvas && d.cohorts && d.cohorts.length) {
      const checkpointLabels = ["Join", "D1", "D7", "D14", "D30", "D60", "D90"];
      const nullToNull = v => (v === null || v === undefined) ? null : v;
      // No explicit `color:` here — makeLineChart already falls back to
      // seriesColor(i) per series, which is the same palette this modulo was
      // reimplementing, minus the bug: slice(-6) happens to match
      // ROLE_COLORS.length today, but the moment either number changes this
      // would start silently repeating a hue instead of folding to the
      // shared overflow neutral.
      const series = d.cohorts.slice(-6).map((c) => ({
        label: c.label,
        counts: [100, nullToNull(c.d1), nullToNull(c.d7), nullToNull(c.d14), nullToNull(c.d30), nullToNull(c.d60), nullToNull(c.d90)],
      }));
      const title = "Retention by Weekly Cohort";
      const curvesChart = makeLineChart(curvesCanvas, { labels: checkpointLabels, series, title });
      charts.push(curvesChart);

      // The caption lives in HTML so it wears the page's type and is
      // selectable/readable, rather than living only inside the canvas.
      curvesCaptionEl.textContent = title;

      // A legend earns its place once there's more than one cohort line to
      // tell apart; with a single cohort the caption already names it.
      curvesLegendEl.replaceChildren();
      if (series.length > 1) renderChartLegend(curvesLegendEl, curvesChart);

      // Tooltips enhance; they must never be the only way to read a value.
      renderChartTable(curvesTableEl, {
        labels: checkpointLabels,
        datasets: series.map(s => ({ label: s.label, data: s.counts })),
        indexLabel: "Checkpoint",
      });
    }
  }

  // Bots are excluded from every metric by default; this is the per-report
  // opt-in. Re-injected after each render because load() rewrites the panel.
  function decorate() {
    mountBotToggle(container, includeBots, (v) => {
      includeBots = v;
      reload();
    });
  }

  // Every pass is guarded, not just the first — see mountReloadable.
  const reload = mountReloadable(container, {
    load, decorate, renderError, describe: "retention",
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
