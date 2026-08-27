import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle, mountReloadable } from "../report-helpers.js";
import {
  makeLineChart, makeHorizontalBarChart, makeDoughnutChart,
  renderChartLegend, renderPieLegend, renderChartTable,
  CHART_BAR, ROLE_COLORS, seriesColor,
} from "../charts.js";


export function mount(container) {
  let includeBots = false;
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading Gini data…</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/gini", includeBots ? { include_bots: "true" } : undefined);
    const panel = container.querySelector(".panel");


    // `tiers` is a dict with five fixed keys, so it has no `.length` and is
    // never empty — asking it whether anyone posted always answered "no", and
    // this panel showed the empty state over a server with 41k messages.
    // `posters` is the count of distinct authors in the window, which is what
    // the empty state actually claims. Compared against 0 rather than falsy so
    // a cached payload from before the field existed renders its numbers
    // instead of a fresh false empty state.
    if (d.posters === 0) {
      panel.innerHTML = `<header><h2>Participation Gini</h2><div class="subtitle">Message distribution inequality</div></header>` +
        renderEmpty("No messages in the last 30 days, so there's no distribution to measure. This needs about a week of messages from a handful of members.");
      return;
    }

    const tiers = d.tiers || {};

    panel.innerHTML = `
      <header>
        <h2>Participation Gini</h2>
        <div class="subtitle">Message distribution inequality &middot; ${d.gini} (${d.badge})</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          The <strong>Gini coefficient</strong> measures how evenly messages are spread across members.
          0 means everyone posts equally; 1 means one person writes everything. Most healthy communities land between 0.5–0.75.
          The <strong>Lorenz curve</strong> visualizes this — the further it bows from the diagonal, the more concentrated activity is.
          The <strong>Palma ratio</strong> compares the top 10% to the bottom 40% — a high ratio means a small group dominates conversation.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Gini Coefficient</div>
          <div class="home-card-big">${d.gini}</div>
          <div class="home-card-sub">0 = equal, 1 = one person posts all</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Top 5% Share</div>
          <div class="home-card-big">${d.top5_share}%</div>
          <div class="home-card-sub">Top 10%: ${d.top10_share}%</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Palma Ratio</div>
          <div class="home-card-big">${d.palma}</div>
          <div class="home-card-sub">Top 10% / Bottom 40%. Target: &lt;4.0</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Weighted Gini</div>
          <div class="home-card-big">${d.weighted_gini}</div>
          <div class="home-card-sub">Msgs + reactions + voice</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">XP Gini</div>
          <div class="home-card-big">${d.xp_gini}</div>
          <div class="home-card-sub">XP distribution inequality</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Gini Over Time</div>
          <div class="chart-caption" data-caption="hist"></div>
          <div class="chart-wrap" style="height:260px"><canvas id="gini-history-chart"></canvas></div>
          <div data-chart-table="hist"></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Lorenz Curve</div>
          <div class="chart-caption" data-caption="lorenz"></div>
          <div class="chart-wrap" style="height:320px"><canvas id="lorenz-chart"></canvas></div>
          <div data-legend="lorenz"></div>
          <div data-chart-table="lorenz"></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Participation Tiers</div>
          <div class="chart-caption" data-caption="tier"></div>
          <div class="chart-wrap" style="height:260px"><canvas id="tier-chart"></canvas></div>
          <div data-legend="tier"></div>
          <div data-chart-table="tier"></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Per-Channel Gini</div>
          <div class="chart-caption" data-caption="ch-gini"></div>
          <div class="chart-wrap" style="min-height:260px"><canvas id="ch-gini-chart"></canvas></div>
          <div data-chart-table="ch-gini"></div>
        </div>
      </div>
    `;

    const captionHist   = panel.querySelector('[data-caption="hist"]');
    const tableHist      = panel.querySelector('[data-chart-table="hist"]');
    const captionLorenz = panel.querySelector('[data-caption="lorenz"]');
    const legendLorenz  = panel.querySelector('[data-legend="lorenz"]');
    const tableLorenz    = panel.querySelector('[data-chart-table="lorenz"]');
    const captionTier   = panel.querySelector('[data-caption="tier"]');
    const legendTier    = panel.querySelector('[data-legend="tier"]');
    const tableTier      = panel.querySelector('[data-chart-table="tier"]');
    const captionChGini = panel.querySelector('[data-caption="ch-gini"]');
    const tableChGini    = panel.querySelector('[data-chart-table="ch-gini"]');

    // Gini over time — one series ("Gini"): caption + table, no legend (the
    // caption already names the one line on the chart).
    const histCanvas = panel.querySelector("#gini-history-chart");
    if (histCanvas && d.gini_history?.length) {
      const histTitle = "Weekly Gini coefficient (12 weeks)";
      const histLabels = d.gini_history.map(p => p.label);
      const histValues = d.gini_history.map(p => p.gini);
      charts.push(makeLineChart(histCanvas, {
        labels: histLabels,
        series: [
          { label: "Gini", counts: histValues, color: CHART_BAR },
        ],
        title: histTitle,
      }));
      captionHist.textContent = histTitle;
      renderChartTable(tableHist, {
        labels: histLabels,
        datasets: [{ label: "Gini", data: histValues }],
        indexLabel: "Week",
      });
    }

    // Lorenz curve — two series (Equality, Actual): caption + legend + table.
    const lorenzCanvas = panel.querySelector("#lorenz-chart");
    if (lorenzCanvas && d.lorenz) {
      const lorenzTitle = "Lorenz Curve (cumulative messages vs population)";
      const labels = d.lorenz.map(p => p.x + "%");
      const equality = d.lorenz.map(p => p.x);
      const actual = d.lorenz.map(p => p.y);
      const lorenzChart = makeLineChart(lorenzCanvas, {
        labels,
        series: [
          { label: "Equality", counts: equality, color: "#949ba4" },
          { label: "Actual", counts: actual, color: CHART_BAR },
        ],
        title: lorenzTitle,
      });
      charts.push(lorenzChart);
      captionLorenz.textContent = lorenzTitle;
      legendLorenz.replaceChildren();
      renderChartLegend(legendLorenz, lorenzChart);
      renderChartTable(tableLorenz, {
        labels,
        datasets: [
          { label: "Equality", data: equality },
          { label: "Actual", data: actual },
        ],
        indexLabel: "% of members",
      });
    }

    // Tier doughnut — five slices (within the ~6-7 legibility guidance):
    // caption + pie legend + table.
    const tierCanvas = panel.querySelector("#tier-chart");
    if (tierCanvas) {
      const tierTitle = "Participation Tiers";
      const tierLabels = ["Lurker (0)", "Light (1-5/wk)", "Moderate (6-20)", "Active (21-50)", "Power (50+)"];
      const tierValues = [tiers.lurker || 0, tiers.light || 0, tiers.moderate || 0, tiers.active || 0, tiers.power || 0];
      const tierChart = makeDoughnutChart(tierCanvas, {
        labels: tierLabels,
        data: tierValues,
        title: tierTitle,
        // seriesColor(0..4), not a hand-mixed array: three of these five slots were
        // still the old, unvalidated hex literals, and one of them (#7F8F3A,
        // retired moss) sat directly next to the new CHART_BAR amber — the
        // exact pair the palette migration was meant to separate, recreated
        // by half-migrating this one array.
        colors: tierLabels.map((_, i) => seriesColor(i)),
      });
      charts.push(tierChart);
      captionTier.textContent = tierTitle;
      legendTier.replaceChildren();
      renderPieLegend(legendTier, tierChart);
      renderChartTable(tableTier, {
        labels: tierLabels,
        datasets: [{ label: "Members", data: tierValues }],
        indexLabel: "Tier",
      });
    }

    // Per-channel Gini — one series (one bar per channel, colored by a fixed
    // good/warning/critical threshold ramp rather than per-series identity):
    // caption + table, no legend.
    const chCanvas = panel.querySelector("#ch-gini-chart");
    if (chCanvas && d.per_channel) {
      const chTitle = "Gini by Channel";
      const chLabels = d.per_channel.map(c => "#" + (c.channel_name || c.channel_id));
      const chValues = d.per_channel.map(c => c.gini);
      charts.push(makeHorizontalBarChart(chCanvas, {
        labels: chLabels,
        data: chValues,
        title: chTitle,
        xLabel: "Gini coefficient",
        // A genuine 3-step severity ramp (bad/mid/good), built from three ALREADY
        // validated ROLE_COLORS members rather than new literals — wine reads as
        // a muted red without colliding with anything else in the set, teal
        // reads as a clear "good". (Reusing #9E3B2E/#7F8F3A here, the exact
        // retired hexes, measured ΔE 1.7 under protanopia against the new
        // CHART_BAR — worse than the pair the whole palette migration fixed,
        // because a validated new hue had been placed next to an unvalidated
        // old one.)
        colors: d.per_channel.map(c => c.gini > 0.85 ? ROLE_COLORS[5] : c.gini > 0.7 ? CHART_BAR : ROLE_COLORS[2]),
      }));
      captionChGini.textContent = chTitle;
      renderChartTable(tableChGini, {
        labels: chLabels,
        datasets: [{ label: "Gini coefficient", data: chValues }],
        indexLabel: "Channel",
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
    load, decorate, renderError, describe: "the participation spread",
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
