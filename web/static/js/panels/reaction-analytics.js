import { api } from "../api.js";
import { makeBarChart, makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Reaction Analytics</h2>
        <div class="subtitle">Top emoji, biggest givers, and most-reacted members</div>
      </header>
      <div class="controls">
        <label>Days (empty = all time)
          <input type="number" data-control="days" min="1" max="3650" value="${initialParams.days || ""}" placeholder="all" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart-emoji></canvas></div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;">
        <div data-givers style="flex:1;min-width:280px;max-height:350px;overflow-y:auto;"></div>
        <div data-receivers style="flex:1;min-width:280px;max-height:350px;overflow-y:auto;"></div>
      </div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const statsEl = container.querySelector("[data-stats]");
  const giversWrap = container.querySelector("[data-givers]");
  const receiversWrap = container.querySelector("[data-receivers]");
  let chart = null;

  async function refresh() {
    const params = {};
    const d = parseInt(daysEl.value);
    if (!isNaN(d) && d > 0) params.days = d;

    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    history.replaceState(null, "", `#/reaction-analytics?${qs}`);

    try {
      const data = await api("/api/reports/reaction-analytics", params);
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = `Total reactions: ${data.total_reactions.toLocaleString()}`;

      const wrap = container.querySelector(".chart-wrap");
      if (data.top_emoji.length) {
        wrap.innerHTML = '<canvas data-chart-emoji></canvas>';
        chart = makeBarChart(container.querySelector("[data-chart-emoji]"), {
          labels: data.top_emoji.map((e) => e.emoji),
          data: data.top_emoji.map((e) => e.total_count),
          title: "Top Emoji",
          yLabel: "Uses",
          color: "#E6B84C",
        });
      } else {
        wrap.innerHTML = `<div class="empty">No reaction data.</div>`;
      }

      if (data.top_givers.length) {
        giversWrap.innerHTML = `<h3 style="color:#dbdee1;font-size:13px;margin:0 0 6px;">Top Givers</h3><div data-givers-table></div>`;
        renderSortableTable(giversWrap.querySelector("[data-givers-table]"), {
          columns: [
            { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
            { key: "given", label: "Given" },
          ],
          data: data.top_givers, defaultSort: "given",
        });
      } else { giversWrap.innerHTML = ""; }

      if (data.top_receivers.length) {
        receiversWrap.innerHTML = `<h3 style="color:#dbdee1;font-size:13px;margin:0 0 6px;">Top Receivers</h3><div data-receivers-table></div>`;
        renderSortableTable(receiversWrap.querySelector("[data-receivers-table]"), {
          columns: [
            { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
            { key: "received", label: "Received" },
          ],
          data: data.top_receivers, defaultSort: "received",
        });
      } else { receiversWrap.innerHTML = ""; }
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
      giversWrap.innerHTML = "";
      receiversWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
