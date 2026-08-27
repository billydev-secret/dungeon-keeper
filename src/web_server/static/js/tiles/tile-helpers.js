// Shared helpers for tile renderers.
import { esc } from "../api.js";
import { CHART_BAR } from "../charts.js";
export { esc };

const BADGE_COLORS = {
  excellent: "var(--green)",
  healthy: "var(--green)",
  needs_work: "var(--yellow)",
  warning: "var(--yellow)",
  critical: "var(--red)",
  clear: "var(--green)",
  active: "var(--red)",
  no_data: "var(--ink-dim)",
};

export function badgeHTML(badge) {
  const color = BADGE_COLORS[badge] || "var(--ink-dim)";
  const label = badge.replace(/_/g, " ");
  return `<span class="health-tile-badge" style="background:${color}">${label}</span>`;
}

// Colour goes through `style`, never an SVG presentation attribute, so a
// caller may pass a CSS custom property — `var(--yellow)` is meaningless
// in `stroke="..."` but works in `style="stroke:..."`. The area fill used
// to be `fill="${color}22"`, string-concatenating hex alpha, which broke
// outright for any non-hex value; fill-opacity does the same job for both.
//
// CHART_BAR, not a literal: this is the default single-series colour, which
// is exactly what CHART_BAR means. It was the pre-migration gold, so every
// sparkline that does not override it was drawing in a retired hue beside
// charts using the current one.
export function sparklineSVG(data, { width = 180, height = 32, color = CHART_BAR } = {}) {
  if (!data || !data.length) return "";
  const max = Math.max(...data, 1);
  const step = width / (data.length - 1 || 1);
  const points = data.map((v, i) => `${i * step},${height - (v / max) * (height - 4) - 2}`);
  const fill = [...points, `${width},${height}`, `0,${height}`].join(" ");
  return `
    <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" style="display:block;">
      <polygon points="${fill}" style="fill:${color};fill-opacity:0.13" stroke="none"/>
      <polyline points="${points.join(" ")}" style="fill:none;stroke:${color}" stroke-width="1.5" stroke-linejoin="round"/>
    </svg>
  `;
}

export function miniBarHTML(items, { maxVal, color = "var(--gold-solid)" } = {}) {
  if (!items || !items.length) return "";
  const mx = maxVal || Math.max(...items.map(i => i.value), 1);
  return items.map(i => {
    const pct = Math.round((i.value / mx) * 100);
    return `<div class="health-mini-bar-row">
      <span class="health-mini-bar-label">${esc(i.label)}</span>
      <div class="health-mini-bar-track">
        <div class="health-mini-bar-fill" style="width:${pct}%;background:${color}"></div>
      </div>
      <span class="health-mini-bar-val">${i.value}</span>
    </div>`;
  }).join("");
}

export function fmtNum(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

export function fmtAgo(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

export function presenceBar(p) {
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

export const ACTION_LABELS = {
  jail: "Jailed", unjail: "Unjailed", warn: "Warned", warn_revoke: "Revoked warning",
  ticket_open: "Opened ticket", ticket_close: "Closed ticket", ticket_reopen: "Reopened ticket",
  ticket_delete: "Deleted ticket", ticket_claim: "Claimed ticket", ticket_escalate: "Escalated ticket",
  pull: "Pulled user", remove: "Removed user",
};
