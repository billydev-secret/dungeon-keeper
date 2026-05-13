import { esc, fmtAgo, ACTION_LABELS } from "./tile-helpers.js";

export function renderTile(el, d) {
  const html = d.recent_actions.length
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

  el.innerHTML = `
    <div class="home-card-label">Recent Mod Actions</div>
    ${html}
  `;
}
