import { api } from "../api.js";

const ACTION_LABELS = {
  jail: "Jailed", unjail: "Unjailed", warn: "Warned", warn_revoke: "Revoked warning",
  ticket_open: "Opened ticket", ticket_close: "Closed ticket", ticket_reopen: "Reopened ticket",
  ticket_delete: "Deleted ticket", ticket_claim: "Claimed ticket", ticket_escalate: "Escalated ticket",
  pull: "Pulled user", remove: "Removed user",
};

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}

function fmtAgo(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function sparklineSVG(data, { width = 200, height = 36, color = "#E6B84C" } = {}) {
  const max = Math.max(...data, 1);
  const step = width / (data.length - 1 || 1);
  const points = data.map((v, i) => `${i * step},${height - (v / max) * (height - 4) - 2}`);
  const fill = [...points, `${width},${height}`, `0,${height}`].join(" ");
  return `
    <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" style="display:block;">
      <polygon points="${fill}" fill="${color}22" stroke="none"/>
      <polyline points="${points.join(" ")}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>
    </svg>
  `;
}

function presenceBar(p) {
  const total = p.online + p.idle + p.dnd + p.offline;
  if (!total) return "";
  const pct = (v) => ((v / total) * 100).toFixed(1);
  return `
    <div class="home-presence-bar">
      <div class="home-presence-seg" style="width:${pct(p.online)}%;background:#7F8F3A;" title="Online: ${p.online}"></div>
      <div class="home-presence-seg" style="width:${pct(p.idle)}%;background:#E6B84C;" title="Idle: ${p.idle}"></div>
      <div class="home-presence-seg" style="width:${pct(p.dnd)}%;background:#9E3B2E;" title="DND: ${p.dnd}"></div>
      <div class="home-presence-seg" style="width:${pct(p.offline)}%;background:#949ba4;" title="Offline: ${p.offline}"></div>
    </div>
    <div class="home-presence-legend">
      <span><i style="background:#7F8F3A;"></i> ${p.online} online</span>
      <span><i style="background:#E6B84C;"></i> ${p.idle} idle</span>
      <span><i style="background:#9E3B2E;"></i> ${p.dnd} dnd</span>
      <span><i style="background:#949ba4;"></i> ${p.offline} offline</span>
    </div>
  `;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel home-panel">
      <div class="home-loading">Loading dashboard...</div>
    </div>
  `;

  let refreshTimer = null;

  async function load() {
    try {
      const d = await api("/api/home");
      render(d);
    } catch (err) {
      container.querySelector(".panel").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  function render(d) {
    const guildName = d.guild?.name || "Server";
    const memberCount = d.guild?.member_count || "—";

    const voiceHTML = d.voice_channels.length
      ? d.voice_channels.map((vc) => `
          <div class="home-voice-ch">
            <div class="home-voice-ch-name">${esc(vc.channel_name)}</div>
            <div class="home-voice-ch-members">${vc.members.map((m) => esc(m.user_name)).join(", ")}</div>
          </div>
        `).join("")
      : '<div class="home-dim">No one in voice</div>';

    const topChHTML = d.top_channels.length
      ? d.top_channels.map((c, i) => `
          <div class="home-rank-row">
            <span class="home-rank-pos">${i + 1}</span>
            <span class="home-rank-name">#${esc(c.channel_name || c.channel_id)}</span>
            <span class="home-rank-val">${c.count}</span>
          </div>
        `).join("")
      : '<div class="home-dim">No messages this hour</div>';

    const topUsersHTML = d.top_users.length
      ? d.top_users.map((u, i) => `
          <div class="home-rank-row">
            <span class="home-rank-pos">${i + 1}</span>
            <span class="home-rank-name">${esc(u.user_name || u.user_id)}</span>
            <span class="home-rank-val">${u.count}</span>
          </div>
        `).join("")
      : '<div class="home-dim">No messages this hour</div>';

    const actionsHTML = d.recent_actions.length
      ? d.recent_actions.map((a) => {
          const label = ACTION_LABELS[a.action] || a.action;
          const target = a.target_name || a.target_id || "";
          return `
            <div class="home-action-row">
              <span class="home-action-label">${esc(label)}</span>
              <span class="home-action-detail">${esc(a.actor_name || a.actor_id)}${target ? " \u2192 " + esc(target) : ""}</span>
              <span class="home-action-time">${fmtAgo(a.created_at)}</span>
            </div>
          `;
        }).join("")
      : '<div class="home-dim">No recent actions</div>';

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>${esc(guildName)}</h2>
        <div class="subtitle">${memberCount} members &middot; updated ${new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}</div>
      </header>

      <div class="home-grid">

        <div class="home-card">
          <div class="home-card-label">Messages (24h)</div>
          <div class="home-card-big">${d.msgs_24h.toLocaleString()}</div>
          <div class="home-sparkline">${sparklineSVG(d.msg_sparkline)}</div>
          <div class="home-card-sub">${d.msgs_1h} in the last hour &middot; ${d.unique_today} unique users today</div>
        </div>

        <div class="home-card">
          <div class="home-card-label">NSFW (24h)</div>
          <div class="home-card-big">${d.nsfw_24h.toLocaleString()}</div>
          <div class="home-sparkline">${sparklineSVG(d.nsfw_sparkline, { color: "#9E3B2E" })}</div>
          <div class="home-card-sub">${d.nsfw_1h} in the last hour &middot; ${d.nsfw_unique} unique users today</div>
        </div>

        <div class="home-card">
          <div class="home-card-label">Presence</div>
          ${presenceBar(d.presence)}
        </div>

        <div class="home-card">
          <div class="home-card-label">XP Today</div>
          <div class="home-card-big">${d.xp_today.toLocaleString()}</div>
          <div class="home-card-sub">${d.xp_users_today} users earned XP</div>
        </div>

        <div class="home-card">
          <div class="home-card-label">Recent Joins (7d)</div>
          <div class="home-card-big">${d.recent_joins}</div>
        </div>

        <div class="home-card">
          <div class="home-card-label">Moderation</div>
          <div class="home-mod-stats">
            <span class="home-mod-pill home-mod-danger">${d.active_jails} jailed</span>
            <span class="home-mod-pill home-mod-info">${d.open_tickets} tickets</span>
            <span class="home-mod-pill home-mod-warn">${d.active_warnings} warnings</span>
          </div>
        </div>

        <div class="home-card">
          <div class="home-card-label">In Voice Now</div>
          ${voiceHTML}
        </div>

        <div class="home-card">
          <div class="home-card-label">Hottest Channels (1h)</div>
          ${topChHTML}
        </div>

        <div class="home-card">
          <div class="home-card-label">Most Active Users (1h)</div>
          ${topUsersHTML}
        </div>

        <div class="home-card home-card-wide">
          <div class="home-card-label">Recent Mod Actions</div>
          ${actionsHTML}
        </div>

      </div>
    `;
  }

  load();
  refreshTimer = setInterval(load, 60_000);

  return {
    unmount() {
      if (refreshTimer) clearInterval(refreshTimer);
    },
  };
}
