import { loadConfig, loadRoles, loadChannels, channelSelect, roleSelect, apiPut, apiDelete, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, roles, channels] = await Promise.all([
      loadConfig(),
      loadRoles(),
      loadChannels(),
    ]);
    render(
      container,
      config.booster_roles || [],
      config.booster_panel_channel_id || "0",
      roles,
      channels,
    );
  })();
}

function render(container, boosterRoles, panelChannelId, roles, channels) {
  function roleCard(r) {
    return `
      <form class="form card" style="margin-bottom:16px;" data-key="${r.role_key}">
        <div class="section-label">${r.role_key}</div>
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
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn btn-danger" data-remove="${r.role_key}">Remove</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Booster Roles</h2>
        <div class="subtitle">Custom color roles for server boosters. Use <code>/config booster</code> in Discord to sync swatches.</div>
      </header>
      <div data-roles>${boosterRoles.length ? boosterRoles.map(roleCard).join("") : '<div class="empty">No booster roles configured.</div>'}</div>

      <div class="section-label">Add Booster Role</div>
      <form class="form card" data-add-form>
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
          <button type="submit" class="btn btn-primary">Add</button>
          <span data-add-status></span>
        </div>
      </form>

      <div class="section-label">Repost Panel</div>
      <form class="form card" data-repost-form>
        <div class="field-hint" style="margin-bottom:10px;">
          Deletes the previously posted booster panel messages and posts a fresh
          set in the chosen channel.
        </div>
        <div class="field">
          <label>Channel</label>
          <select name="channel_id">${channelSelect(channels, panelChannelId, { allowNone: false })}</select>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary" data-repost-btn>Repost Panel</button>
          <span data-repost-status></span>
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
        render(
          container,
          fresh.booster_roles || [],
          fresh.booster_panel_channel_id || "0",
          roles,
          channels,
        );
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
      render(
        container,
        fresh.booster_roles || [],
        fresh.booster_panel_channel_id || "0",
        roles,
        channels,
      );
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });

  // Repost panel handler
  const repostForm = container.querySelector("[data-repost-form]");
  const repostBtn = container.querySelector("[data-repost-btn]");
  const repostStatus = container.querySelector("[data-repost-status]");
  repostForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(repostForm);
    const channelId = fd.get("channel_id");
    if (!channelId || channelId === "0") {
      showStatus(repostStatus, false, "Pick a channel");
      return;
    }
    repostBtn.disabled = true;
    try {
      const res = await fetch("/api/config/booster-roles/post-panel", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel_id: channelId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText);
      }
      const data = await res.json();
      showStatus(repostStatus, true, `Posted ${data.message_count || ""} message(s)`);
    } catch (err) {
      showStatus(repostStatus, false, err.message);
    } finally {
      repostBtn.disabled = false;
    }
  });
}
