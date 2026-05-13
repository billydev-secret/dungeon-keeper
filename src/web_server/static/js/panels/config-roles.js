import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, roleName, apiPut, apiDelete, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    render(container, config.roles, channels, roles);
  })();
}

function permLabel(perm, roles) {
  const prefix = perm.entity_type === "role" ? "Role" : "User";
  const name = perm.entity_type === "role" ? roleName(roles, perm.entity_id) : perm.entity_id;
  return `${prefix}: ${name}`;
}

function render(container, grants, channels, roles) {
  const names = Object.keys(grants);

  function renderRole(name) {
    const g = grants[name];
    const perms = g.permissions || [];
    const permListHTML = perms.length
      ? `<div class="tag-list">${perms.map((p, i) => `
          <span class="tag">
            ${permLabel(p, roles)}
            <button type="button" class="tag-remove perm-remove" data-grant="${name}" data-idx="${i}" title="Remove">&times;</button>
          </span>`).join("")}</div>`
      : '<em style="color:var(--ink-mute);">mod-only (no explicit permissions)</em>';

    return `
      <div class="role-card card" style="margin-bottom:16px;" data-grant="${name}">
        <div class="section-label">${name}</div>
        <form class="form" data-save-form="${name}">
          <div class="field">
            <label>Label</label>
            <input type="text" name="label" value="${g.label || name}" />
          </div>
          <div class="field">
            <label>Role</label>
            <select name="role_id">${roleSelect(roles, g.role_id)}</select>
          </div>
          <div class="field">
            <label>Log Channel</label>
            <select name="log_channel_id">${channelSelect(channels, g.log_channel_id)}</select>
          </div>
          <div class="field">
            <label>Announce Channel</label>
            <select name="announce_channel_id">${channelSelect(channels, g.announce_channel_id)}</select>
          </div>
          <div class="field">
            <label>Grant Message</label>
            <textarea name="grant_message">${g.grant_message}</textarea>
          </div>
          <div class="field">
            <label>Grant Permissions</label>
            <div data-perm-list="${name}" style="margin-bottom:8px;">${permListHTML}</div>
            <div style="display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
              <select data-perm-type="${name}" style="width:auto;">
                <option value="role">Role</option>
                <option value="user">User ID</option>
              </select>
              <select data-perm-role="${name}" style="width:auto;">${roleSelect(roles, "0", { allowNone: false })}</select>
              <input type="text" data-perm-user="${name}" placeholder="User ID" style="width:140px; display:none;" />
              <button type="button" class="btn btn-sm" data-perm-add="${name}">Add</button>
            </div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <button type="button" class="btn btn-danger" data-remove-role="${name}">Remove Role</button>
            <span data-status></span>
          </div>
        </form>
      </div>`;
  }

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Role Grants</h2>
        <div class="subtitle">Configure grant roles (denizen, nsfw, veteran, etc.)</div>
      </header>
      <div data-grants>${names.length ? names.map(renderRole).join("") : '<div class="empty">No grant roles configured.</div>'}</div>

      <div class="section-label">Add Grant Role</div>
      <form class="form card" data-add-role-form>
        <div class="field">
          <label>Key (short lowercase identifier)</label>
          <input type="text" name="grant_name" required placeholder="e.g. denizen, nsfw" pattern="[a-z0-9_]+" />
        </div>
        <div class="field">
          <label>Label</label>
          <input type="text" name="label" required placeholder="e.g. Denizen" />
        </div>
        <div class="field">
          <label>Role</label>
          <select name="role_id">${roleSelect(roles, "0")}</select>
        </div>
        <div class="field">
          <label>Log Channel</label>
          <select name="log_channel_id">${channelSelect(channels, "0")}</select>
        </div>
        <div class="field">
          <label>Announce Channel</label>
          <select name="announce_channel_id">${channelSelect(channels, "0")}</select>
        </div>
        <div class="field">
          <label>Grant Message</label>
          <textarea name="grant_message" placeholder="Welcome {member} to {role}!"></textarea>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Add</button>
          <span data-add-status></span>
        </div>
      </form>
    </div>`;

  // Permission type toggle (show role select vs user ID input)
  for (const name of names) {
    const typeSelect = container.querySelector(`[data-perm-type="${name}"]`);
    const roleEl = container.querySelector(`[data-perm-role="${name}"]`);
    const userEl = container.querySelector(`[data-perm-user="${name}"]`);
    typeSelect.addEventListener("change", () => {
      if (typeSelect.value === "role") {
        roleEl.style.display = "";
        userEl.style.display = "none";
      } else {
        roleEl.style.display = "none";
        userEl.style.display = "";
      }
    });
  }

  // Local add permission
  for (const name of names) {
    const g = grants[name];
    if (!g._perms) g._perms = (g.permissions || []).slice();

    container.querySelector(`[data-perm-add="${name}"]`).addEventListener("click", () => {
      const typeSelect = container.querySelector(`[data-perm-type="${name}"]`);
      const entityType = typeSelect.value;
      let entityId;
      if (entityType === "role") {
        entityId = container.querySelector(`[data-perm-role="${name}"]`).value;
      } else {
        entityId = container.querySelector(`[data-perm-user="${name}"]`).value.trim();
      }
      if (!entityId || entityId === "0") return;
      if (g._perms.some((p) => p.entity_type === entityType && p.entity_id === entityId)) return;
      g._perms.push({ entity_type: entityType, entity_id: entityId });
      refreshPermList(name);
    });
  }

  // Local remove permission
  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".perm-remove");
    if (!btn) return;
    const name = btn.dataset.grant;
    const idx = parseInt(btn.dataset.idx, 10);
    const g = grants[name];
    if (!g._perms) g._perms = (g.permissions || []).slice();
    g._perms.splice(idx, 1);
    refreshPermList(name);
  });

  function refreshPermList(name) {
    const g = grants[name];
    const perms = g._perms || [];
    const listEl = container.querySelector(`[data-perm-list="${name}"]`);
    if (!perms.length) {
      listEl.innerHTML = '<em style="color:var(--ink-mute);">mod-only (no explicit permissions)</em>';
      return;
    }
    const tagsHTML = perms.map((p, i) => `
      <span class="tag">
        ${permLabel(p, roles)}
        <button type="button" class="tag-remove perm-remove" data-grant="${name}" data-idx="${i}" title="Remove">&times;</button>
      </span>`).join("");
    listEl.innerHTML = `<div class="tag-list">${tagsHTML}</div>`;
  }

  // Save handlers
  for (const name of names) {
    const form = container.querySelector(`[data-save-form="${name}"]`);
    const status = form.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const g = grants[name];
      try {
        await apiPut(`/api/config/roles/${name}`, {
          label: fd.get("label"),
          role_id: fd.get("role_id"),
          log_channel_id: fd.get("log_channel_id"),
          announce_channel_id: fd.get("announce_channel_id"),
          grant_message: fd.get("grant_message"),
          permissions: g._perms || g.permissions || [],
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove role handlers
  container.querySelectorAll("[data-remove-role]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Remove grant role "${btn.dataset.removeRole}"? This cannot be undone.`)) return;
      try {
        await apiDelete(`/api/config/roles/${btn.dataset.removeRole}`);
        const fresh = await loadConfig();
        render(container, fresh.roles, channels, roles);
      } catch (err) {
        alert(err.message);
      }
    });
  });

  // Add new role handler
  const addForm = container.querySelector("[data-add-role-form]");
  const addStatus = container.querySelector("[data-add-status]");
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    const grantName = fd.get("grant_name").trim().toLowerCase().replace(/\s+/g, "_");
    if (!grantName) return;
    try {
      await apiPut(`/api/config/roles/${grantName}`, {
        label: fd.get("label"),
        role_id: fd.get("role_id"),
        log_channel_id: fd.get("log_channel_id"),
        announce_channel_id: fd.get("announce_channel_id"),
        grant_message: fd.get("grant_message"),
      });
      const fresh = await loadConfig();
      render(container, fresh.roles, channels, roles);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}
