import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle, mountReloadable } from "../report-helpers.js";
import { mountTabs } from "../tabs.js";


const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function fmtHour(h) {
  const hr = h % 12 || 12;
  return `${hr}${h < 12 ? "a" : "p"}`;
}

function heatmapGridHTML(grid, { label = null, showValues = false, compact = false } = {}) {
  const maxVal = Math.max(...grid.flat(), 1);
  const cellClass = compact ? "hm-cell hm-cell-sm" : "hm-cell";

  let html = '<div class="hm-grid-wrap">';
  if (label) html += `<div class="hm-grid-label">${esc(label)}</div>`;
  html += '<table class="hm-table"><thead><tr><th></th>';
  for (let h = 0; h < 24; h++) {
    html += `<th>${fmtHour(h)}</th>`;
  }
  html += '</tr></thead><tbody>';
  for (let d = 0; d < 7; d++) {
    html += `<tr><td class="hm-day">${DOW[d]}</td>`;
    for (let h = 0; h < 24; h++) {
      const v = grid[d][h];
      const alpha = HEAT_STOPS[heatBucket(v, maxVal)];
      const bg = `rgba(230,184,76,${alpha})`;
      const text = showValues && v > 0 ? Math.round(v) : "";
      const textColor = cellInk(alpha);
      html += `<td class="${cellClass}" style="background:${bg};color:${textColor}" title="${DOW[d]} ${h}:00 — ${v} msgs/hr">${text}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  // Quantizing buys legible numbers, but a bucket only means something with a
  // scale to read it against — and this panel never had one.
  html += heatLegendHTML(maxVal);
  html += '</div>';
  return html;
}

function computeInsights(grid) {
  const insights = [];

  // Day-of-week totals
  const dayTotals = grid.map(row => row.reduce((a, b) => a + b, 0));
  const busiestDay = dayTotals.indexOf(Math.max(...dayTotals));
  const quietestDay = dayTotals.indexOf(Math.min(...dayTotals));
  // A busiest day of zero means the whole grid is empty — no ratio to state,
  // and certainly not the "∞× difference" this used to print.
  if (dayTotals[busiestDay] > 0) {
    const diff = dayTotals[quietestDay] > 0
      ? ` ${(dayTotals[busiestDay] / dayTotals[quietestDay]).toFixed(1)}× difference.`
      : ` No messages at all on ${DOW[quietestDay]}.`;
    insights.push({
      icon: "📅",
      text: `<b>${DOW[busiestDay]}</b> is the busiest day (${Math.round(dayTotals[busiestDay])} msgs/hr total), <b>${DOW[quietestDay]}</b> is the quietest.${diff}`,
    });
  }

  // Weekday vs weekend
  const wdAvg = dayTotals.slice(0, 5).reduce((a, b) => a + b, 0) / 5;
  const weAvg = dayTotals.slice(5).reduce((a, b) => a + b, 0) / 2;
  if (weAvg > 0) {
    const wdweRatio = (wdAvg / weAvg).toFixed(1);
    if (wdweRatio > 1.3) {
      insights.push({ icon: "💼", text: `Weekdays are <b>${wdweRatio}×</b> busier than weekends.` });
    } else if (wdweRatio < 0.8) {
      insights.push({ icon: "🎉", text: `Weekends are <b>${(1/wdweRatio).toFixed(1)}×</b> busier than weekdays.` });
    } else {
      insights.push({ icon: "⚖️", text: "Activity is <b>evenly split</b> between weekdays and weekends." });
    }
  }

  // Peak hours cluster
  const hourTotals = Array(24).fill(0);
  for (let d = 0; d < 7; d++) {
    for (let h = 0; h < 24; h++) {
      hourTotals[h] += grid[d][h];
    }
  }
  const hourAvg = hourTotals.reduce((a, b) => a + b, 0) / 24;
  const peakHours = hourTotals
    .map((v, h) => ({ h, v }))
    .filter(x => x.v > hourAvg * 1.5)
    .sort((a, b) => b.v - a.v);
  if (peakHours.length >= 2) {
    const range = peakHours.map(x => fmtHour(x.h)).join(", ");
    insights.push({ icon: "🔥", text: `Peak hours: <b>${range}</b> (>1.5× average).` });
  }

  // Dead zone detection
  const deadSlots = [];
  for (let d = 0; d < 7; d++) {
    for (let h = 0; h < 24; h++) {
      if (grid[d][h] < 1) deadSlots.push({ d, h });
    }
  }
  if (deadSlots.length > 0 && deadSlots.length <= 30) {
    // Find contiguous dead ranges
    const deadByDay = {};
    for (const s of deadSlots) {
      if (!deadByDay[s.d]) deadByDay[s.d] = [];
      deadByDay[s.d].push(s.h);
    }
    const ranges = [];
    for (const [d, hours] of Object.entries(deadByDay)) {
      hours.sort((a, b) => a - b);
      let start = hours[0], end = hours[0];
      for (let i = 1; i < hours.length; i++) {
        if (hours[i] === end + 1) { end = hours[i]; }
        else { ranges.push({ d: Number(d), start, end }); start = hours[i]; end = hours[i]; }
      }
      ranges.push({ d: Number(d), start, end });
    }
    // Show the longest dead range
    ranges.sort((a, b) => (b.end - b.start) - (a.end - a.start));
    const longest = ranges[0];
    if (longest.end - longest.start >= 2) {
      insights.push({
        icon: "🌙",
        text: `Longest quiet stretch: <b>${DOW[longest.d]} ${fmtHour(longest.start)}–${fmtHour(longest.end + 1)}</b> (${longest.end - longest.start + 1}h under 1 msg/hr).`,
      });
    }
  }

  return insights;
}

function hourlyBarChartHTML(grid) {
  // Sum each hour across all days
  const hourTotals = Array(24).fill(0);
  for (let d = 0; d < 7; d++) {
    for (let h = 0; h < 24; h++) {
      hourTotals[h] += grid[d][h];
    }
  }
  const hourAvg = hourTotals.map(v => Math.round(v / 7 * 10) / 10);
  const max = Math.max(...hourAvg, 1);

  let html = '<div class="hm-bar-chart">';
  for (let h = 0; h < 24; h++) {
    const pct = (hourAvg[h] / max * 100).toFixed(1);
    html += `
      <div class="hm-bar-col" title="${fmtHour(h)}: ${hourAvg[h]} msgs/hr avg">
        <div class="hm-bar-fill" style="height:${pct}%"></div>
        <div class="hm-bar-label">${h % 3 === 0 ? fmtHour(h) : ""}</div>
      </div>`;
  }
  html += '</div>';
  return html;
}

function dowBarChartHTML(grid) {
  const dayTotals = grid.map(row => Math.round(row.reduce((a, b) => a + b, 0)));
  const max = Math.max(...dayTotals, 1);

  let html = '<div class="hm-dow-chart">';
  for (let d = 0; d < 7; d++) {
    const pct = (dayTotals[d] / max * 100).toFixed(1);
    html += `
      <div class="hm-dow-row">
        <span class="hm-dow-label">${DOW[d]}</span>
        <div class="hm-dow-track"><div class="hm-dow-fill" style="width:${pct}%"></div></div>
        <span class="hm-dow-val">${dayTotals[d]}</span>
      </div>`;
  }
  html += '</div>';
  return html;
}

// A continuous alpha ramp of gold over the card has to cross a band of mid
// luminance where NEITHER house ink clears 4.5:1 against it — the best any
// pairing achieves at the crossover is 3.85:1, which is what the previous fix
// reached. Five buckets straddle that band instead of walking through it.
//
// The stops were searched, not chosen: they maximise the weakest step between
// neighbouring buckets (1.59:1, and evenly spaced, so the scale reads as a
// scale) subject to every bucket's better ink clearing 4.5:1. The obvious
// alternative — stops picked purely for text contrast — reaches 5.26:1 on text
// but collapses the top three buckets to 1.28:1 of each other, which trades a
// contrast problem for an encoding one.
const HEAT_STOPS = [0.04, 0.25, 0.46, 0.69, 0.96];

const _GOLD = [230, 184, 76];
const _CARD = [43, 45, 49];

function _lum(rgb) {
  const [r, g, b] = rgb.map((c) => {
    c /= 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Which bucket a cell falls in. 0 is "nothing happened", never a rounding of it. */
function heatBucket(value, maxVal) {
  if (!value) return 0;
  const step = Math.ceil((value / maxVal) * (HEAT_STOPS.length - 1));
  return Math.min(HEAT_STOPS.length - 1, Math.max(1, step));
}

// The ink whose luminance sits furthest from the cell's. The crossover is
// where both score the same: sqrt((L_bright + .05)(L_rail + .05)) - .05, with
// --ink-bright at 0.896 and --bg-rail at 0.014.
function cellInk(alpha) {
  const bg = _lum(_GOLD.map((c, i) => c * alpha + _CARD[i] * (1 - alpha)));
  return bg < 0.1955 ? "var(--ink-bright)" : "var(--bg-rail)";
}

/** The scale, so a bucket can be read back as a number of messages. */
function heatLegendHTML(maxVal) {
  const per = maxVal / (HEAT_STOPS.length - 1);
  const swatches = HEAT_STOPS.map((alpha, i) => {
    const label = i === 0
      ? "none"
      : `${Math.round(per * (i - 1)) + (i === 1 ? 1 : 0)}–${Math.round(per * i)}`;
    return `<span class="hm-key-item">`
      + `<span class="hm-key-swatch" style="background:rgba(230,184,76,${alpha})"></span>`
      + `${esc(label)}</span>`;
  }).join("");
  return `<div class="hm-key" role="img" aria-label="Colour scale: darker is fewer messages per hour, gold is more">`
    + `<span class="hm-key-label">msgs/hr</span>${swatches}</div>`;
}

export function mount(container) {
  let includeBots = false;
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading heatmap…</div></div>';

  async function load() {
    const d = await api("/api/health/heatmap", includeBots ? { include_bots: "true" } : undefined);
    const panel = container.querySelector(".panel");

    const grid = d.grid || [];
    if (!grid.some(row => row.some(v => v > 0))) {
      panel.innerHTML = `<header><h2>Activity Heatmap</h2><div class="subtitle">When your server is most active, by hour and day (30-day average)</div></header>` +
        renderEmpty("No messages in the last 30 days, so every slot is empty. The heatmap becomes readable after about a week of conversation.");
      return;
    }

    const insights = computeInsights(grid);
    const channels = d.per_channel || [];

    // Every top-level box below is its own .home-grid (even the "wide"
    // single-card ones) purely so the shared .panel > box + box rule in
    // app.css puts a consistent gap between them — no per-section inline
    // margin-top to keep in sync by hand.
    panel.innerHTML = `
      <header>
        <h2>Activity Heatmap</h2>
        <div class="subtitle">When your server is most active, by hour and day (30-day average)</div>
      </header>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Peak Slot</div>
          <div class="home-card-big">${esc(d.peak_slot)}</div>
          <div class="home-card-sub">${d.peak_value} msgs/hr avg</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Quietest Slot</div>
          <div class="home-card-big">${esc(d.quiet_slot)}</div>
          <div class="home-card-sub">${d.quiet_value} msgs/hr avg</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Dead Hours / Week</div>
          <div class="home-card-big">${d.dead_hours}</div>
          <div class="home-card-sub">slots under 1 msg/hr</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Server-wide Heatmap</div>
          ${heatmapGridHTML(d.grid, { showValues: true })}
        </div>
      </div>

      ${insights.length ? `
        <div class="home-grid">
          <div class="home-card home-card-wide">
            <div class="home-card-label">Insights</div>
            <div class="hm-insights">
              ${insights.map(i => `<div class="hm-insight">${i.icon} ${i.text}</div>`).join("")}
            </div>
          </div>
        </div>
      ` : ""}

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Hourly Distribution</div>
          ${hourlyBarChartHTML(d.grid)}
        </div>
        <div class="home-card">
          <div class="home-card-label">Day of Week</div>
          ${dowBarChartHTML(d.grid)}
        </div>
      </div>

      ${channels.length ? `
        <div class="home-grid">
          <div class="home-card home-card-wide">
            <div class="home-card-label">Per-Channel Heatmaps</div>
            <div data-channel-tabs></div>
          </div>
        </div>
      ` : ""}
    `;

    // One card per channel used to stack into a wall the grid wrapped
    // raggedly (a last row that never quite filled) — tabbing through them
    // keeps the panel to one channel's heatmap at a time, so there's no grid
    // to go uneven. Every channel's grid is already in hand from the single
    // /api/health/heatmap fetch above, so each tab's render is a synchronous
    // innerHTML write, not a fetch — mountTabs still guards it the same way.
    if (channels.length) {
      mountTabs(panel.querySelector("[data-channel-tabs]"), channels.map((ch, i) => ({
        key: String(ch.channel_id ?? i),
        label: "#" + (ch.channel_name || ch.channel_id),
        render: (pane) => {
          pane.innerHTML = heatmapGridHTML(ch.grid, { compact: true });
        },
      })), { ariaLabel: "Per-channel heatmaps" });
    }
  }

  // Bots are excluded from every metric by default; this is the per-report
  // opt-in. Re-injected after each render because load() rewrites the panel.
  function decorate() {
    mountBotToggle(container, includeBots, (v) => {
      includeBots = v;
      reload();
    });
  }

  // Every pass is guarded, not just the first — see mountReloadable.
  const reload = mountReloadable(container, {
    load, decorate, renderError, describe: "the activity heatmap",
  });

  return { unmount() {} };
}
