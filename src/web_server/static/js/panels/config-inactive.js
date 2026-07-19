import { loadConfig, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const config = await loadConfig();
    const cfg = config.inactive;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Inactive Sweep</h2>
          <div class="subtitle">Idle-member sweep settings for the /inactive holding system</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Inactivity Threshold (days)</label>
            <input type="number" name="threshold_days" min="1" max="3650" step="1" value="${cfg.threshold_days}" />
            <div class="field-hint">Days idle before a member qualifies for a sweep (default 30)</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="auto_sweep" ${cfg.auto_sweep ? "checked" : ""} /> Enable automatic sweep</label>
            <div class="field-hint">Runs the sweep on its own every 6 hours; requires an inactive channel set via /inactive panel</div>
          </div>
          <div class="field">
            <label>Per-Run Cap</label>
            <input type="number" name="sweep_cap" min="1" max="200" step="1" value="${cfg.sweep_cap}" />
            <div class="field-hint">Max members moved in a single sweep run, manual or automatic (default 25)</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/inactive", {
          threshold_days: parseInt(fd.get("threshold_days"), 10),
          auto_sweep: form.querySelector('input[name="auto_sweep"]').checked,
          sweep_cap: parseInt(fd.get("sweep_cap"), 10),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
