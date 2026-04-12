import { api } from "../api.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function heatmapGridHTML(grid, label) {
  const maxVal = Math.max(...grid.flat(), 1);
  let html = `<div class="heatmap-container">`;
  if (label) html += `<div class="heatmap-label">${esc(label)}</div>`;
  html += `<div class="heatmap-hours"><div class="heatmap-corner"></div>`;
  for (let h = 0; h < 24; h += 3) {
    const hr = h % 12 || 12;
    const ap = h < 12 ? "a" : "p";
    html += `<div class="heatmap-hour-label">${hr}${ap}</div>`;
  }
  html += `</div>`;
  for (let d = 0; d < 7; d++) {
    html += `<div class="heatmap-row"><div class="heatmap-day-label">${DOW[d]}</div>`;
    for (let h = 0; h < 24; h++) {
      const v = grid[d][h];
      const intensity = v / maxVal;
      const bg = `rgba(230,184,76,${intensity.toFixed(2)})`;
      html += `<div class="heatmap-cell-full" style="background:${bg}" title="${DOW[d]} ${h}:00 — ${v} msgs/hr"></div>`;
    }
    html += `</div>`;
  }
  html += `</div>`;
  return html;
}

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading heatmap...</div></div>';

  async function load() {
    const d = await api("/api/health/heatmap");
    const panel = container.querySelector(".panel");

    const perChannelHTML = (d.per_channel || []).map(ch => {
      const name = ch.channel_name || ch.channel_id;
      return heatmapGridHTML(ch.grid, `#${name}`);
    }).join("");

    panel.innerHTML = `
      <header>
        <h2>Activity Heatmap</h2>
        <div class="subtitle">Message density by hour and day of week (30-day average)</div>
      </header>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Peak</div>
          <div class="home-card-big">${esc(d.peak_slot)}</div>
          <div class="home-card-sub">${d.peak_value} msgs/hr</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Quietest</div>
          <div class="home-card-big">${esc(d.quiet_slot)}</div>
          <div class="home-card-sub">${d.quiet_value} msgs/hr</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Dead Hours</div>
          <div class="home-card-big">${d.dead_hours}</div>
          <div class="home-card-sub">per week (&lt;1 msg/hr)</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Server-wide Heatmap</div>
        ${heatmapGridHTML(d.grid, null)}
      </div>

      ${perChannelHTML ? `
        <div style="margin-top:20px;">
          <h3 style="color:var(--text);margin-bottom:10px;">Per-Channel Heatmaps</h3>
          ${perChannelHTML}
        </div>
      ` : ""}
    `;
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() {} };
}
