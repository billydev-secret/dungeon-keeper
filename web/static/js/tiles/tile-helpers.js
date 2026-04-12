// Shared helpers for tile renderers.

const BADGE_COLORS = {
  excellent: "var(--success)",
  healthy: "var(--success)",
  needs_work: "var(--warning)",
  warning: "var(--warning)",
  critical: "var(--danger)",
  clear: "var(--success)",
  active: "var(--danger)",
  no_data: "var(--text-dim)",
};

export function badgeHTML(badge) {
  const color = BADGE_COLORS[badge] || "var(--text-dim)";
  const label = badge.replace(/_/g, " ");
  return `<span class="health-tile-badge" style="background:${color}">${label}</span>`;
}

export function sparklineSVG(data, { width = 180, height = 32, color = "#E6B84C" } = {}) {
  if (!data || !data.length) return "";
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

export function miniBarHTML(items, { maxVal, height = 6, color = "var(--accent)" } = {}) {
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

export function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

export function fmtNum(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}
