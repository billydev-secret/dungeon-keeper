import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { loadConfig } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";

const DAY = 86400 * 1000;

function daysSince(prunedAt) {
  if (prunedAt == null) return "";
  return Math.round((Date.now() - prunedAt * 1000) / DAY);
}

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Grant Audit</h2>
        <div class="subtitle">Members missing a grant role, split by why</div>
      </header>
      <div class="controls">
        <label>Grant Role
          <select data-control="grant"><option value="">Loading grant roles…</option></select>
        </label>
        <label>Minimum Level
          <input data-control="min-level" type="number" min="1" max="100" value="${parseInt(initialParams.min_level) || 5}" style="width:5em;">
        </label>
      </div>
      <div data-status></div>
      <section data-bucket="waiting" style="margin-top:16px;">
        <h3 style="margin-bottom:4px;">Waiting for First Grant</h3>
        <div class="subtitle">At or above the level bar, never granted, never stripped by the prune loop</div>
        <div data-table-wrap style="margin-top:8px; max-height:320px; overflow-y:auto;"></div>
      </section>
      <section data-bucket="returned" style="margin-top:16px;">
        <h3 style="margin-bottom:4px;">Stripped but Came Back</h3>
        <div class="subtitle">Pruned for inactivity, active again, still not re-granted</div>
        <div data-table-wrap style="margin-top:8px; max-height:320px; overflow-y:auto;"></div>
      </section>
      <section data-bucket="inactive" style="margin-top:16px;">
        <h3 style="margin-bottom:4px;">Last 10 Inactive Stripped</h3>
        <div class="subtitle">Most recently pruned members who are still inactive — working as intended</div>
        <div data-table-wrap style="margin-top:8px; max-height:320px; overflow-y:auto;"></div>
      </section>
    </div>
  `;
  container.innerHTML = html;

  const grantEl = container.querySelector('[data-control="grant"]');
  const minLevelEl = container.querySelector('[data-control="min-level"]');
  const statusEl = container.querySelector("[data-status]");
  const wraps = {
    waiting: container.querySelector('[data-bucket="waiting"] [data-table-wrap]'),
    returned: container.querySelector('[data-bucket="returned"] [data-table-wrap]'),
    inactive: container.querySelector('[data-bucket="inactive"] [data-table-wrap]'),
  };

  (async () => {
    let names = [];
    try {
      const config = await loadConfig();
      names = Object.entries(config.roles || {});
    } catch (err) {
      statusEl.className = "error";
      statusEl.textContent = "Couldn’t load your grant roles — reload the page to try again.";
    }
    const wanted = initialParams.grant_name || "nsfw";
    grantEl.innerHTML = names
      .map(([name, cfg]) => {
        const sel = name === wanted ? " selected" : "";
        return `<option value="${esc(name)}"${sel}>${esc(cfg.label || name)}</option>`;
      })
      .join("") || '<option value="">No grant roles configured yet</option>';
    if (grantEl.value) refresh();
  })();

  function renderBucket(wrap, rows, { withPruned, emptyMsg }) {
    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">${emptyMsg}</div>`;
      return;
    }
    const columns = [
      { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
      { key: "level", label: "Level", format: (v) => (v == null ? "—" : v) },
    ];
    if (withPruned) {
      columns.push({
        key: "pruned_at",
        label: "Days Since Stripped",
        // null = implicit strip: grant recorded, removal never was (bot downtime)
        format: (v) => (v == null ? "Not recorded" : daysSince(v)),
      });
    }
    renderSortableTable(wrap, {
      columns,
      data: rows,
      defaultSort: withPruned ? "pruned_at" : "level",
      emptyMsg,
      maxRows: 200,
    });
  }

  async function refresh() {
    if (!grantEl.value) return;
    const minLevel = Math.max(1, parseInt(minLevelEl.value) || 5);
    statusEl.className = "";
    statusEl.textContent = "Loading grant audit…";
    history.replaceState(
      null,
      "",
      `#/grant-audit?grant_name=${encodeURIComponent(grantEl.value)}&min_level=${minLevel}`
    );
    try {
      const data = await withLoading(
        wraps.waiting,
        api("/api/reports/grant-audit", { grant_name: grantEl.value, min_level: minLevel })
      );
      statusEl.textContent =
        `${data.label} — ${data.waiting_first_grant.length} waiting for a first grant, ` +
        `${data.stripped_returned.length} stripped but back, ` +
        `${data.recent_inactive.length} recently stripped and still quiet. ` +
        `Inactivity window: ${data.inactivity_days} days.`;
      renderBucket(wraps.waiting, data.waiting_first_grant, {
        withPruned: false,
        emptyMsg: "Nobody is waiting — everyone at or above the level bar already has the role.",
      });
      renderBucket(wraps.returned, data.stripped_returned, {
        withPruned: true,
        emptyMsg: "Nobody who was stripped for inactivity has come back and stayed unre-granted.",
      });
      renderBucket(wraps.inactive, data.recent_inactive, {
        withPruned: true,
        emptyMsg: "Nobody has been stripped for inactivity recently.",
      });
    } catch (err) {
      statusEl.className = "";
      statusEl.textContent = "";
      const msg = `Couldn’t load the grant audit — try again. (${err.message})`;
      for (const wrap of Object.values(wraps)) {
        wrap.innerHTML = `<div class="error">${esc(msg)}</div>`;
      }
    }
  }

  grantEl.addEventListener("change", refresh);
  minLevelEl.addEventListener("change", refresh);
  return { unmount() {} };
}
