import { loadConfig, loadRoles, roleSelect, apiPut, apiDelete, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    render(container, config.booster_roles || [], roles);
  })();
}

function render(container, boosterRoles, roles) {
  function roleCard(r) {
    return `
      <form class="config-form" style="margin-bottom:24px; padding:16px; background:var(--bg-alt); border-radius:6px;" data-key="${r.role_key}">
        <h3 style="margin:0 0 8px; font-size:15px;">${r.role_key}</h3>
        <div class="field">
          <label>Label</label>
          <input type="text" name="label" value="${r.label}" />
        </div>
        <div class="field">
          <label>Role</label>
          <select name="role_id">${roleSelect(roles, r.role_id)}</select>
        </div>
        <div class="field">
          <label>Image Path</label>
          <input type="text" name="image_path" value="${r.image_path || ""}" />
        </div>
        <div class="field">
          <label>Sort Order</label>
          <input type="number" name="sort_order" value="${r.sort_order}" min="0" />
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit">Save</button>
          <button type="button" class="btn-danger" data-remove="${r.role_key}">Remove</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  container.innerHTML = `
    <div class="panel" style="overflow-y:auto;">
      <header>
        <h2>Booster Roles</h2>
        <div class="subtitle">Custom color roles for server boosters. Use <code>/config booster</code> in Discord to sync swatches.</div>
      </header>
      <div data-roles>${boosterRoles.length ? boosterRoles.map(roleCard).join("") : '<div class="empty">No booster roles configured.</div>'}</div>
      <hr style="margin:24px 0; border-color:var(--border);" />
      <form class="config-form" data-add-form style="padding:16px; background:var(--bg-alt); border-radius:6px;">
        <h3 style="margin:0 0 8px; font-size:15px;">Add Booster Role</h3>
        <div class="field">
          <label>Key (short lowercase identifier)</label>
          <input type="text" name="role_key" required placeholder="e.g. ruby, sapphire" pattern="[a-z0-9_]+" />
        </div>
        <div class="field">
          <label>Label</label>
          <input type="text" name="label" required placeholder="e.g. Ruby" />
        </div>
        <div class="field">
          <label>Role</label>
          <select name="role_id">${roleSelect(roles, "0")}</select>
        </div>
        <div class="field">
          <label>Image Path</label>
          <input type="text" name="image_path" placeholder="/path/to/image.png" />
        </div>
        <div class="field">
          <label>Sort Order</label>
          <input type="number" name="sort_order" value="0" min="0" />
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit">Add</button>
          <span data-add-status></span>
        </div>
      </form>
    </div>`;

  // Save handlers for existing roles
  for (const r of boosterRoles) {
    const form = container.querySelector(`[data-key="${r.role_key}"]`);
    const status = form.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut(`/api/config/booster-roles/${r.role_key}`, {
          label: fd.get("label"),
          role_id: fd.get("role_id"),
          image_path: fd.get("image_path"),
          sort_order: parseInt(fd.get("sort_order"), 10) || 0,
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
      if (!confirm(`Remove booster role "${btn.dataset.remove}"?`)) return;
      try {
        await apiDelete(`/api/config/booster-roles/${btn.dataset.remove}`);
        const fresh = await loadConfig();
        render(container, fresh.booster_roles || [], roles);
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
    const key = fd.get("role_key").trim().toLowerCase().replace(/\s+/g, "_");
    if (!key) return;
    try {
      await apiPut(`/api/config/booster-roles/${key}`, {
        label: fd.get("label"),
        role_id: fd.get("role_id"),
        image_path: fd.get("image_path"),
        sort_order: parseInt(fd.get("sort_order"), 10) || 0,
      });
      const fresh = await loadConfig();
      render(container, fresh.booster_roles || [], roles);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}
