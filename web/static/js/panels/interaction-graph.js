import { api, esc } from "../api.js";
import { makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Interaction Graph</h2>
        <div class="subtitle">Who talks to whom — top pairs and most connected members</div>
      </header>
      <div class="controls">
        <label>Days (empty = all time)
          <input type="number" data-control="days" min="1" max="3650" value="${initialParams.days || 2}" placeholder="all" />
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-pairs style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
      <div data-nodes style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const pairsWrap = container.querySelector("[data-pairs]");
  const nodesWrap = container.querySelector("[data-nodes]");
  let chart = null;

  // Inject focus-user selector into controls
  const focusLabel = document.createElement("label");
  focusLabel.textContent = "Focus user ";
  const focusSel = document.createElement("select");
  focusSel.dataset.control = "focus-user";
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = "— all pairs —";
  focusSel.appendChild(defaultOpt);
  focusLabel.appendChild(focusSel);
  container.querySelector(".controls").appendChild(focusLabel);

  let lastNodes = [];
  let lastPairs = [];

  async function refresh() {
    const params = {};
    const d = parseInt(daysEl.value);
    if (!isNaN(d) && d > 0) params.days = d;

    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    history.replaceState(null, "", `#/interaction-graph?${qs}`);

    try {
      const data = await api("/api/reports/interaction-graph", params);
      if (chart) { chart.destroy(); chart = null; }

      const wrap = container.querySelector(".chart-wrap");
      const pairs = data.top_pairs.slice(0, 15);
      if (!pairs.length) {
        wrap.innerHTML = `<div class="empty">No interaction data.</div>`;
        pairsWrap.innerHTML = "";
        nodesWrap.innerHTML = "";
        return;
      }
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: pairs.map((p) => `${p.from_name || p.from_id} ↔ ${p.to_name || p.to_id}`),
        data: pairs.map((p) => p.weight),
        title: "Top Interaction Pairs",
        xLabel: "Interactions",
      });

      for (const p of data.top_pairs) p.pair_name = `${p.from_name || p.from_id} ↔ ${p.to_name || p.to_id}`;
      lastPairs = data.top_pairs;
      lastNodes = data.nodes;

      // Rebuild focus-user options from current node list
      const prevFocus = focusSel.value;
      while (focusSel.options.length > 1) focusSel.remove(1);
      const sorted = [...data.nodes].sort((a, b) =>
        (a.user_name || a.user_id).localeCompare(b.user_name || b.user_id)
      );
      for (const n of sorted) {
        const opt = document.createElement("option");
        opt.value = n.user_id;
        opt.textContent = n.user_name || n.user_id;
        focusSel.appendChild(opt);
      }
      if (prevFocus) focusSel.value = prevFocus;

      renderPairsTable();

      renderSortableTable(nodesWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "total_outbound", label: "Outbound" },
          { key: "total_inbound", label: "Inbound" },
          { key: "unique_partners", label: "Partners" },
        ],
        data: data.nodes,
        defaultSort: "total_outbound",
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      pairsWrap.innerHTML = "";
      nodesWrap.innerHTML = "";
    }
  }

  function renderPairsTable() {
    const uid = focusSel.value;
    if (!uid) {
      renderSortableTable(pairsWrap, {
        columns: [
          { key: "pair_name", label: "Pair" },
          { key: "weight", label: "Interactions" },
        ],
        data: lastPairs,
        defaultSort: "weight",
      });
      return;
    }

    // Compute total weight for this user across all their pairs
    const userPairs = lastPairs.filter((p) => p.from_id === uid || p.to_id === uid);
    const total = userPairs.reduce((s, p) => s + p.weight, 0);

    const userName = (lastNodes.find((n) => n.user_id === uid) || {}).user_name || uid;
    const rows = userPairs.map((p) => ({
      ...p,
      pct_share: total > 0 ? (p.weight / total) * 100 : 0,
    }));

    renderSortableTable(pairsWrap, {
      columns: [
        { key: "pair_name", label: "Pair" },
        { key: "weight", label: "Interactions" },
        { key: "pct_share", label: `% of ${userName}'s total`, format: (v) => v.toFixed(1) + "%" },
      ],
      data: rows,
      defaultSort: "pct_share",
    });
  }

  daysEl.addEventListener("change", refresh);
  focusSel.addEventListener("change", renderPairsTable);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
