import { api, esc, fmtTs, fmtAge } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Greeter Response</h2>
        <div class="subtitle">Join-to-greeting timing from the join / leave log and greeter chat</div>
      </header>
      <div class="controls"></div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.days || 10, allowAll: true, label: "Range" });
  container.querySelector(".controls").appendChild(rangeEl);
  const daysEl = rangeEl.querySelector("select");
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  function statusLabel(status) {
    if (status === "left_before_greeting") return "Left before greeting";
    if (status === "awaiting_greeting") return "Awaiting greeting";
    return "Greeted";
  }

  function renderTable(entries) {
    if (!entries || !entries.length) {
      tableWrap.innerHTML = "";
      return;
    }
    renderSortableTable(tableWrap, {
      columns: [
        { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
        { key: "joined_at", label: "Joined", format: (v) => fmtTs(v) },
        { key: "status", label: "Status", format: (v) => statusLabel(v) },
        { key: "wait_seconds", label: "Wait", format: (v) => v == null ? "—" : fmtAge(v) },
        { key: "greeted_at", label: "Greeted", format: (v) => fmtTs(v) },
        { key: "greeter_name", label: "Greeted By", format: (v, r) => r.greeter_name || r.greeter_id || "—" },
        { key: "left_at", label: "Left", format: (v) => fmtTs(v) },
      ],
      data: entries,
      defaultSort: "joined_at",
    });
  }

  async function refresh() {
    const raw = parseInt(daysEl.value);
    const params = {};
    if (!isNaN(raw) && raw > 0) params.days = raw;
    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    history.replaceState(null, "", `#/greeter-response?${qs}`);

    try {
      const data = await withLoading(container.querySelector(".chart-wrap"), api("/api/reports/greeter-response", params));
      if (chart) {
        chart.destroy();
        chart = null;
      }

      if (data.count) {
        const pct = data.total_joins ? Math.round(data.count / data.total_joins * 100) : 0;
        statsEl.textContent = `Median: ${fmtAge(data.median_seconds)}  ·  Mean: ${fmtAge(data.mean_seconds)}  ·  ${data.count} of ${data.total_joins} greeted (${pct}%)  ·  ${data.left_before_greeting_count || 0} left before greeting  ·  ${data.awaiting_greeting_count || 0} still waiting`;
      } else {
        statsEl.textContent = `${data.left_before_greeting_count || 0} left before greeting  ·  ${data.awaiting_greeting_count || 0} still waiting`;
      }

      const wrap = container.querySelector(".chart-wrap");
      if (!data.histogram.length || data.count === 0) {
        wrap.innerHTML = `<div class="empty">No greeted joins for the selected period.</div>`;
        renderTable(data.entries || []);
        return;
      }

      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: data.histogram.map((b) => b.label),
        data: data.histogram.map((b) => b.count),
        title: `Greeter Response Time — ${data.window_label}`,
        yLabel: "Joins",
      });

      renderTable(data.entries || []);
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return {
    unmount() {
      if (chart) {
        chart.destroy();
        chart = null;
      }
    },
  };
}
