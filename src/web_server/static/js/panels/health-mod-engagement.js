import { api } from "../api.js";
import { makeHorizontalBarChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

const INTERVALS = [
  { value: "7",  label: "Last 7 days" },
  { value: "14", label: "Last 14 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
];

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Moderator Community Engagement</h2>
        <div class="subtitle">How mods are connecting with the broader community</div>
      </header>
      <div class="controls">
        <label>Time Window
          <select data-control="days">
            ${INTERVALS.map(i => `<option value="${i.value}">${esc(i.label)}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="panel-loading" data-body>Loading...</div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const bodyEl = container.querySelector("[data-body]");
  const charts = [];

  if (initialParams.days && INTERVALS.some(i => i.value === initialParams.days)) {
    daysEl.value = initialParams.days;
  }

  function destroyCharts() {
    charts.forEach(c => c.destroy());
    charts.length = 0;
  }

  async function refresh() {
    const days = daysEl.value;
    history.replaceState(null, "", `#/health-mod-engagement?days=${days}`);
    bodyEl.innerHTML = `<div class="panel-loading">Loading...</div>`;
    destroyCharts();

    let d;
    try {
      d = await api("/api/health/mod-engagement", { days });
    } catch (err) {
      bodyEl.innerHTML = `<div class="error">${esc(err.message)}</div>`;
      return;
    }

    const windowLabel = INTERVALS.find(i => i.value === String(d.days || days))?.label ?? `Last ${days} days`;

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
          <div class="chart-wrap" style="min-height:260px"><canvas id="eng-reach-chart"></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Public Messages &amp; Channel Breadth</div>
          <div class="chart-wrap" style="min-height:260px"><canvas id="eng-msgs-chart"></canvas></div>
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
    if (reachCanvas && d.mods?.length) {
      charts.push(makeHorizontalBarChart(reachCanvas, {
        labels: d.mods.map(m => m.user_name || m.user_id),
        data: d.mods.map(m => m.unique_reach),
        title: "Unique Members Reached",
        xLabel: "Members",
        colors: d.mods.map((_, i) => ROLE_COLORS[i % ROLE_COLORS.length]),
      }));
    }

    const msgsCanvas = bodyEl.querySelector("#eng-msgs-chart");
    if (msgsCanvas && d.mods?.length) {
      const sorted = [...d.mods].sort((a, b) => b.public_messages - a.public_messages);
      charts.push(makeHorizontalBarChart(msgsCanvas, {
        labels: sorted.map(m => m.user_name || m.user_id),
        data: sorted.map(m => m.public_messages),
        title: "Public Messages",
        xLabel: "Messages",
        colors: sorted.map((_, i) => ROLE_COLORS[i % ROLE_COLORS.length]),
      }));
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { destroyCharts(); } };
}
