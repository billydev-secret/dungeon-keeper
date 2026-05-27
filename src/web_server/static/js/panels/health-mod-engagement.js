import { api } from "../api.js";
import { makeHorizontalBarChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading mod engagement data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/mod-engagement");
    const panel = container.querySelector(".panel");

    const modRows = (d.mods || []).map((m, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(m.user_name || m.user_id)}</td>
        <td>${m.unique_reach}</td>
        <td>${m.public_messages}</td>
        <td>${m.reactions_received}</td>
        <td>${m.replies_received}</td>
        <td>${m.newcomer_touchpoints}</td>
      </tr>
    `).join("");

    panel.innerHTML = `
      <header>
        <h2>Moderator Community Engagement</h2>
        <div class="subtitle">How mods are connecting with the broader community</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          Measures each mod's public-channel presence, excluding mod/ticket/jail admin channels.
          <strong>Unique Reach</strong> counts distinct members engaged via replies, mentions, or reactions in the last 7 days.
          <strong>Reactions Received</strong> and <strong>Replies Received</strong> show how much the community responds to mods.
          <strong>Newcomer Touchpoints</strong> counts interactions with members who joined in the last 30 days.
          <strong>Engagement Gini</strong> shows how evenly engagement is spread — close to 1 means one mod is doing almost all of it.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Total Public Messages (7d)</div>
          <div class="home-card-big">${d.total_public_messages_7d}</div>
          <div class="home-card-sub">Across all mods in public channels</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Avg Unique Reach (7d)</div>
          <div class="home-card-big">${d.avg_unique_reach_7d}</div>
          <div class="home-card-sub">Distinct members engaged per mod</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Newcomer Touchpoints (30d)</div>
          <div class="home-card-big">${d.total_newcomer_touchpoints_30d}</div>
          <div class="home-card-sub">Interactions with members who joined &lt;30d ago</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Engagement Gini</div>
          <div class="home-card-big">${d.engagement_gini}</div>
          <div class="home-card-sub">0 = all mods engaging equally, 1 = one mod does all</div>
        </div>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Unique Members Reached (7d)</div>
          <div class="chart-wrap" style="min-height:280px"><canvas id="eng-reach-chart"></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Public Messages (7d)</div>
          <div class="chart-wrap" style="min-height:280px"><canvas id="eng-msgs-chart"></canvas></div>
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
                <th>Reactions Rcvd</th>
                <th>Replies Rcvd</th>
                <th>Newcomer Touches</th>
              </tr>
            </thead>
            <tbody>${modRows || '<tr><td colspan="7" class="home-dim">No data</td></tr>'}</tbody>
          </table>
          </div>
        </div>
      </div>
    `;

    const reachCanvas = panel.querySelector("#eng-reach-chart");
    if (reachCanvas && d.mods && d.mods.length) {
      charts.push(makeHorizontalBarChart(reachCanvas, {
        labels: d.mods.map(m => m.user_name || m.user_id),
        data: d.mods.map(m => m.unique_reach),
        title: "Unique Members Reached (7d)",
        xLabel: "Members",
        colors: d.mods.map((_, i) => ROLE_COLORS[i % ROLE_COLORS.length]),
      }));
    }

    const msgsCanvas = panel.querySelector("#eng-msgs-chart");
    if (msgsCanvas && d.mods && d.mods.length) {
      const sorted = [...d.mods].sort((a, b) => b.public_messages - a.public_messages);
      charts.push(makeHorizontalBarChart(msgsCanvas, {
        labels: sorted.map(m => m.user_name || m.user_id),
        data: sorted.map(m => m.public_messages),
        title: "Public Messages (7d)",
        xLabel: "Messages",
        colors: sorted.map((_, i) => ROLE_COLORS[i % ROLE_COLORS.length]),
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
