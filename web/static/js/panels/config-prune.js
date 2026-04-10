import { loadConfig, loadRoles, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const p = config.prune;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Inactivity Prune</h2>
          <div class="subtitle">Automatically remove a role from inactive members</div>
        </header>
        <form class="config-form" data-form>
          <div class="field">
            <label>Role to Prune</label>
            <select name="role_id">${roleSelect(roles, p.role_id)}</select>
            <div class="field-hint">Set to (none) to disable pruning</div>
          </div>
          <div class="field">
            <label>Inactivity Threshold (days)</label>
            <input type="number" name="inactivity_days" min="1" max="365" value="${p.inactivity_days || ""}" placeholder="30" />
          </div>
          <div><button type="submit">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/prune", {
          role_id: fd.get("role_id"),
          inactivity_days: parseInt(fd.get("inactivity_days")) || 0,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
