import { loadConfig, apiPut, showStatus, guardForm } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const config = await loadConfig();
    const cfg = config.inactive;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Inactive Sweep</h2>
          <div class="subtitle">Moves members who have gone quiet into the holding area, so your active roster stays honest</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Who Counts as Inactive</div>
            <div class="field">
              <label for="ci-threshold">Inactivity Threshold (days)</label>
              <input type="number" name="threshold_days" id="ci-threshold" required
                min="1" max="3650" step="1" value="${cfg.threshold_days}" style="max-width:140px;" />
              <div class="field-hint">A member who has not posted for this many days
                becomes eligible to be swept. Shorter thresholds catch more people —
                30 days is the usual starting point.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Sweeping</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="auto_sweep" ${cfg.auto_sweep ? "checked" : ""} />
                Sweep inactive members automatically
              </label>
              <div class="field-hint">When checked, the sweep runs by itself every six
                hours and moves eligible members without anyone pressing a button. It
                does nothing until an inactive channel has been set from the
                <code>/inactive</code> panel in Discord. Unchecked, sweeps only happen
                when a moderator starts one.</div>
            </div>
            <div class="field">
              <label for="ci-cap">Members Moved Per Run</label>
              <input type="number" name="sweep_cap" id="ci-cap" required
                min="1" max="200" step="1" value="${cfg.sweep_cap}" style="max-width:140px;" />
              <div class="field-hint">The most members a single sweep will move,
                whether it was started by hand or ran automatically. A low cap keeps a
                first sweep from surprising half the server at once.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const threshold = parseInt(fd.get("threshold_days"), 10);
      if (!Number.isFinite(threshold) || threshold < 1 || threshold > 3650) {
        showStatus(status, false, "Inactivity Threshold must be a number of days from 1 to 3650");
        form.querySelector("[name=threshold_days]").focus();
        return;
      }
      const cap = parseInt(fd.get("sweep_cap"), 10);
      if (!Number.isFinite(cap) || cap < 1 || cap > 200) {
        showStatus(status, false, "Members Moved Per Run must be a number from 1 to 200");
        form.querySelector("[name=sweep_cap]").focus();
        return;
      }
      try {
        await apiPut("/api/config/inactive", {
          threshold_days: threshold,
          auto_sweep: form.querySelector('input[name="auto_sweep"]').checked,
          sweep_cap: cap,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
