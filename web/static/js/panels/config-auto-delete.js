import { loadConfig, loadChannels, channelSelect, channelName, apiPut, apiDelete, showStatus } from "../config-helpers.js";

const UNITS = [
  { label: "Minutes", seconds: 60 },
  { label: "Hours", seconds: 3600 },
  { label: "Days", seconds: 86400 },
];

function bestUnit(totalSeconds) {
  if (totalSeconds % 86400 === 0) return { value: totalSeconds / 86400, unit: 86400 };
  if (totalSeconds % 3600 === 0) return { value: totalSeconds / 3600, unit: 3600 };
  return { value: Math.round(totalSeconds / 60), unit: 60 };
}

function unitOptions(selectedSeconds) {
  return UNITS.map(
    (u) => `<option value="${u.seconds}"${u.seconds === selectedSeconds ? " selected" : ""}>${u.label}</option>`
  ).join("");
}

function formatTs(ts) {
  if (!ts || ts <= 0) return "never";
  return new Date(ts * 1000).toLocaleString();
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    render(container, config.auto_delete || [], channels);
  })();
}

function render(container, rules, channels) {
  function ruleRow(r) {
    const age = bestUnit(r.max_age_seconds);
    const interval = bestUnit(r.interval_seconds);
    const chName = channelName(channels, r.channel_id);
    return `
      <form class="config-form" style="margin-bottom:24px; padding:16px; background:var(--bg-alt); border-radius:6px;" data-channel="${r.channel_id}">
        <h3 style="margin:0 0 8px; font-size:15px;">${chName}</h3>
        <div style="display:flex; gap:12px; flex-wrap:wrap;">
          <div class="field" style="flex:1; min-width:180px;">
            <label>Delete Age</label>
            <div style="display:flex; gap:4px;">
              <input type="number" name="age_value" value="${age.value}" min="1" style="width:80px;" />
              <select name="age_unit">${unitOptions(age.unit)}</select>
            </div>
          </div>
          <div class="field" style="flex:1; min-width:180px;">
            <label>Run Interval</label>
            <div style="display:flex; gap:4px;">
              <input type="number" name="interval_value" value="${interval.value}" min="1" style="width:80px;" />
              <select name="interval_unit">${unitOptions(interval.unit)}</select>
            </div>
          </div>
        </div>
        <div class="field-hint" style="margin-bottom:8px;">Last run: ${formatTs(r.last_run_ts)}</div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit">Save</button>
          <button type="button" class="btn-danger" data-remove="${r.channel_id}">Remove</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  container.innerHTML = `
    <div class="panel" style="overflow-y:auto;">
      <header>
        <h2>Auto-Delete Schedules</h2>
        <div class="subtitle">Manage recurring message cleanup per channel.</div>
      </header>
      <div data-rules>${rules.length ? rules.map(ruleRow).join("") : '<div class="empty">No auto-delete schedules configured.</div>'}</div>
      <hr style="margin:24px 0; border-color:var(--border);" />
      <form class="config-form" data-add-form style="padding:16px; background:var(--bg-alt); border-radius:6px;">
        <h3 style="margin:0 0 8px; font-size:15px;">Add Schedule</h3>
        <div class="field">
          <label>Channel</label>
          <select name="channel_id">${channelSelect(channels, "0", { allowNone: false })}</select>
        </div>
        <div style="display:flex; gap:12px; flex-wrap:wrap;">
          <div class="field" style="flex:1; min-width:180px;">
            <label>Delete Age</label>
            <div style="display:flex; gap:4px;">
              <input type="number" name="age_value" value="30" min="1" style="width:80px;" />
              <select name="age_unit">${unitOptions(86400)}</select>
            </div>
          </div>
          <div class="field" style="flex:1; min-width:180px;">
            <label>Run Interval</label>
            <div style="display:flex; gap:4px;">
              <input type="number" name="interval_value" value="1" min="1" style="width:80px;" />
              <select name="interval_unit">${unitOptions(86400)}</select>
            </div>
          </div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit">Add</button>
          <span data-add-status></span>
        </div>
      </form>
    </div>`;

  // Save handlers for existing rules
  for (const r of rules) {
    const form = container.querySelector(`[data-channel="${r.channel_id}"]`);
    const status = form.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut(`/api/config/auto-delete/${r.channel_id}`, {
          max_age_seconds: parseInt(fd.get("age_value"), 10) * parseInt(fd.get("age_unit"), 10),
          interval_seconds: parseInt(fd.get("interval_value"), 10) * parseInt(fd.get("interval_unit"), 10),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove handlers
  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this auto-delete schedule?")) return;
      try {
        await apiDelete(`/api/config/auto-delete/${btn.dataset.remove}`);
        const fresh = await loadConfig();
        render(container, fresh.auto_delete || [], channels);
      } catch (err) {
        alert(err.message);
      }
    });
  });

  // Add handler
  const addForm = container.querySelector("[data-add-form]");
  const addStatus = container.querySelector("[data-add-status]");
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    const channelId = fd.get("channel_id");
    if (!channelId || channelId === "0") return;
    try {
      await apiPut(`/api/config/auto-delete/${channelId}`, {
        max_age_seconds: parseInt(fd.get("age_value"), 10) * parseInt(fd.get("age_unit"), 10),
        interval_seconds: parseInt(fd.get("interval_value"), 10) * parseInt(fd.get("interval_unit"), 10),
      });
      const fresh = await loadConfig();
      render(container, fresh.auto_delete || [], channels);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}
