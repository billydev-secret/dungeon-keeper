import { api, esc } from "../api.js";
import { makeHorizontalBarChart, renderChartTable, seriesColor } from "../charts.js";
import { renderEmpty, renderError } from "../states.js";
import { rangePicker } from "../report-helpers.js";

// Titles are declared once and reused for both the chart builder's `title`
// option (now ignored by charts.js, kept for backward compatibility — see
// makeHorizontalBarChart) and the HTML `.chart-caption` that actually renders
// them, so the two can never drift apart.
const REACH_CHART_TITLE = "Unique Members Reached";
const MSGS_CHART_TITLE = "Public Messages";

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Moderator Community Engagement</h2>
        <div class="subtitle">How mods are connecting with the broader community</div>
      </header>
      <div class="controls">
        <label data-slot="range"></label>
      </div>
      <!-- The loading spinner is a *child* of the body, never the body itself:
           .panel-loading is a centering flex box, so leaving the class on the
           container laid the whole report out as centred flex items that shrank
           to min-content — stat-tile headings collapsed to one character per
           line and the cards overlapped. -->
      <div data-body><div class="panel-loading">Loading…</div></div>
    </div>
  `;

  // Shared day-range picker so this report offers the same windows as the rest.
  const rangeCtl = rangePicker({ value: initialParams.days || 30, label: "Range" });
  const daysEl = rangeCtl.querySelector("select");
  daysEl.dataset.control = "days";
  container.querySelector('[data-slot="range"]').replaceWith(rangeCtl);
  const bodyEl = container.querySelector("[data-body]");
  const charts = [];

  function destroyCharts() {
    charts.forEach(c => c.destroy());
    charts.length = 0;
  }

  async function refresh() {
    const days = daysEl.value;
    history.replaceState(null, "", `#/health-mod-engagement?days=${days}`);
    bodyEl.innerHTML = `<div class="panel-loading">Loading…</div>`;
    destroyCharts();

    let d;
    try {
      d = await api("/api/health/mod-engagement", { days });
    } catch (err) {
      bodyEl.innerHTML = renderError(
        `Couldn't load moderator engagement — ${err.message}. Change the range to try again.`
      );
      return;
    }

    const windowLabel = `Last ${d.days || days} days`;

    if (!(d.mods || []).length) {
      bodyEl.innerHTML = renderEmpty(
        "No moderator messages in this window. Widen the range, or check that your moderator roles are set on the Role Grants page — this report only counts members with a mod role."
      );
      return;
    }

    const modRows = (d.mods || []).map((m, i) => {
      const initPct = m.public_messages
        ? Math.round((m.initiations / m.public_messages) * 100) : 0;
      return `
        <tr>
          <td>${i + 1}</td>
          <td>${esc(m.user_name || m.user_id)}</td>
          <td>${m.unique_reach}</td>
          <td>${m.public_messages}</td>
          <td>${m.initiations} <span class="home-dim">(${initPct}%)</span></td>
          <td>${m.channel_breadth}</td>
          <td>${m.reactions_received}</td>
          <td>${m.replies_received}</td>
          <td>${m.engagement_rate}</td>
          <td>${m.newcomer_touchpoints}</td>
        </tr>
      `;
    }).join("");

    bodyEl.innerHTML = `
      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          Measures each mod's public-channel presence, excluding mod/ticket/jail admin channels.
          <strong>Unique Reach</strong> — distinct members engaged via replies, mentions, or reactions.
          <strong>Initiations</strong> — messages that aren't replies (proactive vs. reactive posting).
          <strong>Channel Breadth</strong> — distinct public channels the mod posted in.
          <strong>Engagement Rate</strong> — (reactions + replies received) ÷ messages sent. Higher = more resonance.
          <strong>Newcomer Touchpoints</strong> — interactions with members who joined in the last 30 days (always 30d window).
          <strong>Engagement Gini</strong> — 0 = all mods equally engaged, 1 = one mod does everything.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Total Public Messages</div>
          <div class="home-card-big">${d.total_public_messages}</div>
          <div class="home-card-sub">${windowLabel}, all mods</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Avg Unique Reach</div>
          <div class="home-card-big">${d.avg_unique_reach}</div>
          <div class="home-card-sub">Distinct members per mod · ${windowLabel}</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Newcomer Touchpoints</div>
          <div class="home-card-big">${d.total_newcomer_touchpoints}</div>
          <div class="home-card-sub">Interactions with &lt;30d members · ${windowLabel}</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Engagement Gini</div>
          <div class="home-card-big">${d.engagement_gini}</div>
          <div class="home-card-sub">0 = all mods engaging equally</div>
        </div>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Unique Members Reached</div>
          <div class="chart-caption">${esc(REACH_CHART_TITLE)}</div>
          <div class="chart-wrap" style="min-height:260px"><canvas id="eng-reach-chart"></canvas></div>
          <div data-chart-table="reach"></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Public Messages &amp; Channel Breadth</div>
          <div class="chart-caption">${esc(MSGS_CHART_TITLE)}</div>
          <div class="chart-wrap" style="min-height:260px"><canvas id="eng-msgs-chart"></canvas></div>
          <div data-chart-table="msgs"></div>
        </div>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card" style="grid-column: 1 / -1;">
          <div class="home-card-label">Per-Moderator Breakdown</div>
          <div class="data-table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Moderator</th>
                <th>Unique Reach</th>
                <th>Public Msgs</th>
                <th>Initiations</th>
                <th>Ch. Breadth</th>
                <th>Reactions Rcvd</th>
                <th>Replies Rcvd</th>
                <th>Eng. Rate</th>
                <th>Newcomer Touches</th>
              </tr>
            </thead>
            <tbody>${modRows || '<tr><td colspan="10" class="home-dim">No data</td></tr>'}</tbody>
          </table>
          </div>
        </div>
      </div>
    `;

    const reachCanvas = bodyEl.querySelector("#eng-reach-chart");
    const reachTableEl = bodyEl.querySelector('[data-chart-table="reach"]');
    if (reachCanvas && d.mods?.length) {
      const reachLabels = d.mods.map(m => m.user_name || m.user_id);
      const reachData = d.mods.map(m => m.unique_reach);
      charts.push(makeHorizontalBarChart(reachCanvas, {
        labels: reachLabels,
        data: reachData,
        title: REACH_CHART_TITLE,
        xLabel: "Members",
        // seriesColor, not a local modulo — past 6 moderators this folds to the
        // shared neutral overflow instead of silently repeating a hue.
        colors: d.mods.map((_, i) => seriesColor(i)),
      }));
      // Single series — the caption above already names it, so no legend
      // (see "none for one" in the chart-restyle rules). Still needs a table:
      // a tooltip must never be the only way to read a value.
      renderChartTable(reachTableEl, {
        labels: reachLabels,
        datasets: [{ label: "Unique Reach", data: reachData }],
        indexLabel: "Moderator",
      });
    }

    const msgsCanvas = bodyEl.querySelector("#eng-msgs-chart");
    const msgsTableEl = bodyEl.querySelector('[data-chart-table="msgs"]');
    if (msgsCanvas && d.mods?.length) {
      const sorted = [...d.mods].sort((a, b) => b.public_messages - a.public_messages);
      const msgsLabels = sorted.map(m => m.user_name || m.user_id);
      const msgsData = sorted.map(m => m.public_messages);
      charts.push(makeHorizontalBarChart(msgsCanvas, {
        labels: msgsLabels,
        data: msgsData,
        title: MSGS_CHART_TITLE,
        xLabel: "Messages",
        colors: sorted.map((_, i) => seriesColor(i)),
      }));
      renderChartTable(msgsTableEl, {
        labels: msgsLabels,
        datasets: [{ label: "Public Messages", data: msgsData }],
        indexLabel: "Moderator",
      });
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { destroyCharts(); } };
}
