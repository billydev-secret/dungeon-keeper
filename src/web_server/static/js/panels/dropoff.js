import { api, esc } from "../api.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Drop-off</h2>
        <div class="subtitle">Members whose message volume dropped most this period vs the period before</div>
      </header>
      <div class="controls">
        <label>Period
          <select data-control="period">
            <option value="hour">Hour</option>
            <option value="day">Day</option>
            <option value="week" selected>Week</option>
            <option value="month">Month</option>
          </select>
        </label>
        <label>Limit
          <input type="number" data-control="limit" min="1" max="50" value="${initialParams.limit || 10}" />
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px;"></div>
    </div>
  `;
  container.innerHTML = html;

  const periodEl = container.querySelector('[data-control="period"]');
  const limitEl = container.querySelector('[data-control="limit"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');
  if (initialParams.period) periodEl.value = initialParams.period;

  async function refresh() {
    const params = { period: periodEl.value, limit: parseInt(limitEl.value) || 10 };
    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/dropoff?${qs}`);

    statusEl.textContent = "Loading…";
    try {
      const data = await api("/api/reports/dropoff", params);
      statusEl.textContent = `${data.period_label} — ${data.entries.length} members ranked by drop %`;
      if (!data.entries.length) {
        tableWrap.textContent = "No drop-off candidates for this window.";
        return;
      }
      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "msgs_prev", label: "Prev" },
          { key: "msgs_recent", label: "Recent" },
          { key: "drop_pct", label: "Drop %", format: (v) => `${v}%` },
          { key: "channels_recent", label: "Channels" },
          { key: "replies_recent", label: "Replies" },
          { key: "initiations_recent", label: "Initiated" },
          { key: "deep_convos_recent", label: "Deep" },
        ],
        data: data.entries,
        defaultSort: "drop_pct",
      });
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
      tableWrap.textContent = "";
    }
  }
  periodEl.addEventListener("change", refresh);
  limitEl.addEventListener("change", refresh);
  refresh();
  return { unmount() {} };
}
