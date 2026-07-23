import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { makeBarChart } from "../charts.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Invite Effectiveness</h2>
        <div class="subtitle">Which inviters bring members that stick around</div>
      </header>
      <div class="controls">
        <label>Counts as Active Within (Days)
          <input type="number" data-control="active_days" min="1" max="365" value="${initialParams.active_days || 30}" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:600px; overflow-y:auto;"></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.days || "", allowAll: true, label: "Range" });
  container.querySelector(".controls").prepend(rangeEl);
  const daysEl = rangeEl.querySelector("select");
  const activeEl = container.querySelector('[data-control="active_days"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;
  let sortKey = "invite_count";
  let sortAsc = false;
  let currentData = [];
  const expanded = new Set();

  const COLS = [
    { key: "inviter_name", label: "Inviter" },
    { key: "invite_count", label: "Invites" },
    { key: "still_active", label: "Still Active" },
    { key: "retention_pct", label: "Retention" },
  ];

  function renderTable() {
    if (!currentData.length) {
      tableWrap.innerHTML = '<div class="empty">No invites recorded in this window. '
        + 'Dungeon Keeper needs the Manage Server permission to read invites, and only '
        + 'counts joins from after it started tracking.</div>';
      return;
    }

    const sorted = [...currentData].sort((a, b) => {
      let av = a[sortKey], bv = b[sortKey];
      if (av == null) av = "";
      if (bv == null) bv = "";
      if (typeof av === "string" && typeof bv === "string")
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      return sortAsc ? av - bv : bv - av;
    });

    const headCells = COLS.map(c => {
      const cls = c.key === sortKey ? (sortAsc ? "sort-asc" : "sort-desc") : "";
      return `<th data-sort="${c.key}" class="${cls}">${c.label}</th>`;
    }).join("") + `<th></th>`;

    const bodyRows = sorted.map(row => {
      const isOpen = expanded.has(row.inviter_id);
      // All user-supplied values passed through esc() before innerHTML insertion
      const cells = [
        `<td>${esc(row.inviter_name || row.inviter_id)}</td>`,
        `<td>${row.invite_count}</td>`,
        `<td>${row.still_active}</td>`,
        `<td>${row.retention_pct}%</td>`,
        `<td><button class="expand-btn" data-id="${esc(row.inviter_id)}" title="${isOpen ? "Collapse" : "Show invitees"}">${isOpen ? "\u25b2" : "\u25bc"}</button></td>`,
      ].join("");

      const mainRow = `<tr class="inviter-row" data-id="${esc(row.inviter_id)}">${cells}</tr>`;

      if (!isOpen) return mainRow;

      const invitees = row.invitees || [];
      const inviteeRows = invitees.length
        ? invitees.map(i => `
            <tr class="invitee-row">
              <td colspan="2" style="padding-left:2rem">${esc(i.invitee_name || i.invitee_id)}</td>
              <td colspan="3" style="color:${i.active ? "var(--color-success, #57f287)" : "var(--ink-mute)"}">
                ${i.active ? "Active" : "Inactive"}
              </td>
            </tr>`).join("")
        : `<tr class="invitee-row"><td colspan="5" style="padding-left:2rem; color:var(--ink-mute)">No joins recorded for this inviter</td></tr>`;

      return mainRow + inviteeRows;
    }).join("");

    tableWrap.innerHTML = `
      <table class="data-table">
        <thead><tr>${headCells}</tr></thead>
        <tbody>${bodyRows}</tbody>
      </table>
    `;
  }

  tableWrap.addEventListener("click", e => {
    const btn = e.target.closest(".expand-btn");
    if (btn) {
      const id = btn.dataset.id;
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      renderTable();
      return;
    }
    const th = e.target.closest("th[data-sort]");
    if (th) {
      const key = th.dataset.sort;
      if (sortKey === key) {
        sortAsc = !sortAsc;
      } else {
        sortKey = key;
        const sample = currentData.find(d => d[key] != null);
        sortAsc = sample && typeof sample[key] === "string";
      }
      renderTable();
    }
  });

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
      const data = await withLoading(container.querySelector(".chart-wrap"), api("/api/reports/invite-effectiveness", params));
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = data.total_invites
        ? `Total invites: ${data.total_invites}  \u00b7  Still active: ${data.total_active}  \u00b7  Retention: ${data.overall_retention_pct}%`
        : "No invites recorded in this window.";

      const wrap = container.querySelector(".chart-wrap");
      const inviters = data.inviters.slice(0, 20);
      if (!inviters.length) {
        wrap.innerHTML = `<div class="empty">No invites recorded in this window. Widen the range to see older invites.</div>`;
        currentData = [];
        renderTable();
        return;
      }
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: inviters.map(i => i.inviter_name || i.inviter_id),
        data: inviters.map(i => i.invite_count),
        title: "Invites by User",
        yLabel: "Invites",
      });

      currentData = data.inviters;
      expanded.clear();
      renderTable();
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">Couldn’t load invite effectiveness — try again. (${esc(err.message)})</div>`;
      currentData = [];
      renderTable();
    }
  }

  daysEl.addEventListener("change", refresh);
  activeEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
