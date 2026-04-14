import { api, esc } from "../api.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Invite Effectiveness</h2>
        <div class="subtitle">Which inviters bring members that stick around</div>
      </header>
      <div class="controls">
        <label>Days (empty = all time)
          <input type="number" data-control="days" min="1" max="3650" value="${initialParams.days || ""}" placeholder="all" />
        </label>
        <label>Active window (days)
          <input type="number" data-control="active_days" min="1" max="365" value="${initialParams.active_days || 30}" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const activeEl = container.querySelector('[data-control="active_days"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  async function refresh() {
    const params = {};
    const d = parseInt(daysEl.value);
    if (!isNaN(d) && d > 0) params.days = d;
    params.active_days = parseInt(activeEl.value) || 30;

    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    qs.set("active_days", params.active_days);
    history.replaceState(null, "", `#/invite-effectiveness?${qs}`);

    try {
      const data = await api("/api/reports/invite-effectiveness", params);
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = data.total_invites
        ? `Total invites: ${data.total_invites}  ·  Still active: ${data.total_active}  ·  Retention: ${data.overall_retention_pct}%`
        : "No invite data found.";

      const wrap = container.querySelector(".chart-wrap");
      const inviters = data.inviters.slice(0, 20);
      if (!inviters.length) {
        wrap.innerHTML = `<div class="empty">No invite data for the selected period.</div>`;
        tableWrap.innerHTML = "";
        return;
      }
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: inviters.map((i) => i.inviter_name || i.inviter_id),
        data: inviters.map((i) => i.invite_count),
        title: "Invites by User",
        yLabel: "Invites",
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "inviter_name", label: "Inviter", format: (v, r) => r.inviter_name || r.inviter_id },
          { key: "invite_count", label: "Invites" },
          { key: "still_active", label: "Still Active" },
          { key: "retention_pct", label: "Retention", format: (v) => `${v}%` },
        ],
        data: data.inviters,
        defaultSort: "invite_count",
      });
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  activeEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
