import { esc, fmtAgo } from "./tile-helpers.js";

function modRow(count, label, cssClass, latest, detailKey) {
  const pill = `<span class="home-mod-pill ${cssClass}">${count} ${label}</span>`;
  if (!latest) {
    return `<div class="home-mod-row">${pill}<span class="home-mod-detail home-dim">none</span></div>`;
  }
  const name = esc(latest.user_name || latest.user_id);
  const detail = esc(latest[detailKey] || "");
  const sep = detail ? " \u00b7 " : "";
  const time = fmtAgo(latest.created_at);
  return `<div class="home-mod-row">${pill}<span class="home-mod-detail">${name}${sep}${detail}</span><span class="home-mod-time">${time}</span></div>`;
}

export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">Moderation</div>
    ${modRow(d.active_jails, "jailed", "home-mod-danger", d.latest_jail, "reason")}
    ${modRow(d.open_tickets, "tickets", "home-mod-info", d.latest_ticket, "description")}
    ${modRow(d.active_warnings, "warnings", "home-mod-warn", d.latest_warning, "reason")}
  `;
}
