import { api, esc } from "../api.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Member Retention</h2>
        <div class="subtitle">Members who slowed down or stopped posting</div>
      </header>
      <div class="controls">
        <label>Period (days)
          <input type="number" data-control="period_days" min="1" max="365" value="${initialParams.period_days || 3}" />
        </label>
        <label>Min previous msgs
          <input type="number" data-control="min_previous" min="1" max="100" value="${initialParams.min_previous || 5}" />
        </label>
        <label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;">
          <input type="checkbox" data-control="normalize" />
          Normalize for server activity
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const periodEl = container.querySelector('[data-control="period_days"]');
  const minEl = container.querySelector('[data-control="min_previous"]');
  const normalizeEl = container.querySelector('[data-control="normalize"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;
  let cachedData = null;

  function fmtDate(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric",
    });
  }

  function render(data) {
    if (chart) { chart.destroy(); chart = null; }

    const normalize = normalizeEl.checked;
    const dropKey = normalize ? "normalized_drop_pct" : "drop_pct";
    const serverChg = data.server_activity_change_pct;
    const normNote = normalize
      ? ` (normalized \u2014 server activity ${serverChg >= 0 ? "+" : ""}${serverChg}%)`
      : "";

    statsEl.textContent = `${data.total_dropoffs} members with activity drops over ${data.period_days}-day window${normNote}`;

    const sorted = [...data.entries].sort((a, b) => b[dropKey] - a[dropKey]);

    const wrap = container.querySelector(".chart-wrap");
    const entries = sorted.slice(0, 20);
    if (!entries.length) {
      wrap.innerHTML = `<div class="empty">No dropoffs detected for this period.</div>`;
      tableWrap.innerHTML = "";
      return;
    }
    wrap.innerHTML = '<canvas data-chart></canvas>';
    chart = makeBarChart(container.querySelector("[data-chart]"), {
      labels: entries.map((e) => e.user_name || e.user_id),
      data: entries.map((e) => e[dropKey]),
      title: normalize ? "Normalized Activity Drop %" : "Activity Drop %",
      yLabel: "Drop %",
      color: "#9E3B2E",
    });

    renderSortableTable(tableWrap, {
      columns: [
        { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
        { key: "msgs_prev", label: "Previous" },
        { key: "msgs_recent", label: "Recent" },
        { key: "drop_pct", label: "Raw Drop", format: (v) => `<span style="color:#9E3B2E">${v}%</span>` },
        { key: "normalized_drop_pct", label: "Normalized", format: (v) => `<span style="color:#9E3B2E">${v}%</span>` },
        { key: "days_active_prev", label: "Days (prev)" },
        { key: "days_active_recent", label: "Days (recent)" },
        { key: "last_seen_ts", label: "Last Seen", format: (v) => fmtDate(v) },
        { key: "level", label: "Level" },
      ],
      data: sorted,
      defaultSort: dropKey,
    });
  }

  async function refresh() {
    const params = {
      period_days: parseInt(periodEl.value) || 30,
      min_previous: parseInt(minEl.value) || 5,
    };
    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/retention?${qs}`);

    try {
      cachedData = await api("/api/reports/retention", params);
      render(cachedData);
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  periodEl.addEventListener("change", refresh);
  minEl.addEventListener("change", refresh);
  normalizeEl.addEventListener("change", () => { if (cachedData) render(cachedData); });
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
