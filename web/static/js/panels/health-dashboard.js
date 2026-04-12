// Health dashboard — tile grid with live status bar.
import { api } from "../api.js";

// Tile renderer imports (each exports renderTile(el, data, names?))
import { renderTile as compositeScore } from "../tiles/composite-score.js";
import { renderTile as dauMau }         from "../tiles/dau-mau.js";
import { renderTile as heatmap }        from "../tiles/heatmap.js";
import { renderTile as gini }           from "../tiles/gini.js";
import { renderTile as channelHealth }  from "../tiles/channel-health.js";
import { renderTile as socialGraph }    from "../tiles/social-graph.js";
import { renderTile as sentiment }      from "../tiles/sentiment.js";
import { renderTile as newcomerFunnel } from "../tiles/newcomer-funnel.js";
import { renderTile as cohortRetention } from "../tiles/cohort-retention.js";
import { renderTile as churnRisk }      from "../tiles/churn-risk.js";
import { renderTile as modWorkload }    from "../tiles/mod-workload.js";
import { renderTile as incidents }      from "../tiles/incidents.js";

const TILE_CONFIG = [
  { key: "composite",        renderer: compositeScore, wide: true,  nav: "health-composite-score", label: "Community Health" },
  { key: "dau_mau",          renderer: dauMau,                       nav: "health-dau-mau",         label: "DAU/MAU" },
  { key: "heatmap",          renderer: heatmap,                      nav: "health-heatmap",         label: "Activity Heatmap" },
  { key: "gini",             renderer: gini,                         nav: "health-gini",            label: "Participation Gini" },
  { key: "channel_health",   renderer: channelHealth, names: true,   nav: "health-channel-health",  label: "Channel Health" },
  { key: "social_graph",     renderer: socialGraph,                   nav: "health-social-graph",    label: "Social Graph" },
  { key: "sentiment",        renderer: sentiment,                     nav: "health-sentiment",       label: "Sentiment & Tone" },
  { key: "newcomer_funnel",  renderer: newcomerFunnel,                nav: "health-newcomer-funnel", label: "Newcomer Funnel" },
  { key: "cohort_retention", renderer: cohortRetention,               nav: "health-cohort-retention",label: "Cohort Retention" },
  { key: "churn_risk",       renderer: churnRisk,                     nav: "health-churn-risk",      label: "Churn Risk" },
  { key: "mod_workload",     renderer: modWorkload, names: true,      nav: "health-mod-workload",    label: "Mod Workload" },
  { key: "incidents",        renderer: incidents,                     nav: "health-incidents",       label: "Incidents" },
];

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = `
    <div class="panel health-panel">
      <div class="health-status-bar">Loading...</div>
      <div class="health-grid"></div>
    </div>
  `;

  let refreshTimer = null;

  async function load() {
    try {
      const d = await api("/api/health/tiles");
      render(d);
    } catch (err) {
      container.querySelector(".health-panel").innerHTML =
        `<div class="error">${esc(err.message)}</div>`;
    }
  }

  function render(d) {
    const sb = d.status_bar || {};
    const bar = container.querySelector(".health-status-bar");
    bar.innerHTML = `
      <span><b>${sb.active_users_1h || 0}</b> active users</span>
      <span><b>${sb.active_channels_1h || 0}</b> channels</span>
      <span><b>${sb.voice_active || 0}</b> in voice</span>
      <span><b>${sb.recent_joins_today || 0}</b> joined today</span>
      <span>${sb.member_count || 0} members</span>
    `;

    const grid = container.querySelector(".health-grid");
    grid.innerHTML = "";

    const names = {
      channels: d.channel_names || {},
      users: d.user_names || {},
    };

    for (const tile of TILE_CONFIG) {
      const data = (d.tiles || {})[tile.key];
      if (!data) continue;

      const card = document.createElement("div");
      card.className = "health-tile" + (tile.wide ? " health-tile-wide" : "");
      card.style.cursor = "pointer";
      card.addEventListener("click", () => {
        window.location.hash = `#/${tile.nav}`;
      });

      try {
        if (tile.names) {
          tile.renderer(card, data, names);
        } else {
          tile.renderer(card, data);
        }
      } catch (e) {
        card.innerHTML = `<div class="health-tile-header"><span class="health-tile-label">${tile.label}</span></div><div class="error">Render error</div>`;
      }

      grid.appendChild(card);
    }
  }

  load();
  refreshTimer = setInterval(load, 60_000);

  return {
    unmount() {
      if (refreshTimer) clearInterval(refreshTimer);
    },
  };
}
