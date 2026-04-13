import { api } from "../api.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

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
      const intensity = v / maxVal;
      const alpha = Math.max(intensity, 0.04);
      const bg = `rgba(230,184,76,${alpha.toFixed(2)})`;
      const text = showValues && v > 0 ? Math.round(v) : "";
      const textColor = intensity > 0.6 ? "var(--bg)" : "var(--text-dim)";
      html += `<td class="${cellClass}" style="background:${bg};color:${textColor}" title="${DOW[d]} ${h}:00 — ${v} msgs/hr">${text}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table></div>';
  return html;
}

function computeInsights(grid) {
  const insights = [];

  // Day-of-week totals
  const dayTotals = grid.map(row => row.reduce((a, b) => a + b, 0));
  const busiestDay = dayTotals.indexOf(Math.max(...dayTotals));
  const quietestDay = dayTotals.indexOf(Math.min(...dayTotals));
  const ratio = dayTotals[quietestDay] > 0
    ? (dayTotals[busiestDay] / dayTotals[quietestDay]).toFixed(1)
    : "∞";
  insights.push({
    icon: "📅",
    text: `<b>${DOW[busiestDay]}</b> is the busiest day (${Math.round(dayTotals[busiestDay])} msgs/hr total), <b>${DOW[quietestDay]}</b> is the quietest. ${ratio}× difference.`,
  });

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

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading heatmap...</div></div>';

  async function load() {
    const d = await api("/api/health/heatmap");
    const panel = container.querySelector(".panel");
    const insights = computeInsights(d.grid);

    const perChannelHTML = (d.per_channel || []).map(ch => {
      const name = ch.channel_name || ch.channel_id;
      return `<div class="home-card" style="margin-bottom:10px;">
        ${heatmapGridHTML(ch.grid, { label: "#" + name, compact: true })}
      </div>`;
    }).join("");

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

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Server-wide Heatmap</div>
        ${heatmapGridHTML(d.grid, { showValues: true })}
      </div>

      ${insights.length ? `
        <div class="home-card home-card-wide" style="margin-top:14px;">
          <div class="home-card-label">Insights</div>
          <div class="hm-insights">
            ${insights.map(i => `<div class="hm-insight">${i.icon} ${i.text}</div>`).join("")}
          </div>
        </div>
      ` : ""}

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Hourly Distribution</div>
          ${hourlyBarChartHTML(d.grid)}
        </div>
        <div class="home-card">
          <div class="home-card-label">Day of Week</div>
          ${dowBarChartHTML(d.grid)}
        </div>
      </div>

      ${perChannelHTML ? `
        <div style="margin-top:20px;">
          <div class="home-card-label" style="margin-bottom:10px;">Per-Channel Heatmaps</div>
          <div class="home-grid">
            ${perChannelHTML}
          </div>
        </div>
      ` : ""}
    `;
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() {} };
}
